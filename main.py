from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import asyncio
import random
import re
import os
import time
from typing import Optional
from dotenv import load_dotenv

load_dotenv(".env.local")  # loads TOPDECK_API_KEY locally; no-op in production

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI()

# Simple in-memory caches (reset on server restart)
_deck_cache: dict = {}
_image_cache: dict = {}
_commander_cache: dict = {}
_matchup_cache: dict = {}  # key -> (result, timestamp)
_MATCHUP_TTL = 3600  # 1 hour

CARD_BACK = "https://cards.scryfall.io/normal/back/0/0/0aeebaf5-8c7d-4636-9e82-8c27447861f7.jpg"
# IMPORTANT: if this repo is ever made public, rotate the TOPDECK_API_KEY in Vercel env vars first
TOPDECK_API_KEY = os.environ.get("TOPDECK_API_KEY", "")


class DeckRequest(BaseModel):
    decklist: str  # Raw pasted decklist text


class CardNamesRequest(BaseModel):
    names: list[str]


def extract_image_url(card: dict) -> Optional[str]:
    if "image_uris" in card:
        return card["image_uris"].get("normal")
    if "card_faces" in card and card["card_faces"]:
        face = card["card_faces"][0]
        if "image_uris" in face:
            return face["image_uris"].get("normal")
    return None


def parse_decklist(text: str) -> dict:
    """
    Parse a pasted decklist. Supports:
      - Moxfield export format (has section headers like 'Commander (1)', 'Deck (99)')
      - Plain MTGO format: '1 Card Name' per line
    Returns {"commander": str, "cards": [str, ...]}
    """
    lines = text.strip().splitlines()

    commanders: list[str] = []
    mainboard: list[str] = []
    current_section = "main"

    SECTION_HEADERS = re.compile(
        r"^(Commander|Deck|Sideboard|Maybeboard|Companion|Attractions|Stickers)",
        re.IGNORECASE,
    )
    CARD_LINE = re.compile(r"^(\d+)\s+(.+)$")

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # Section header detection (e.g. "Commander (1)", "Deck (99)")
        if SECTION_HEADERS.match(line):
            header = line.lower()
            if header.startswith("commander"):
                current_section = "commander"
            elif header.startswith("deck"):
                current_section = "main"
            else:
                current_section = "skip"
            continue

        m = CARD_LINE.match(line)
        if not m:
            continue

        qty = int(m.group(1))
        name = m.group(2).strip()
        # Strip trailing set/collector info like " (NEO) 123"
        name = re.sub(r"\s+\([A-Z0-9]+\)\s+\d+.*$", "", name).strip()

        if current_section == "commander":
            commanders.extend([name] * qty)
        elif current_section == "main":
            mainboard.extend([name] * qty)

    if not mainboard and not commanders:
        raise ValueError("Could not parse any cards. Make sure you copied the full decklist.")

    commander_str = " / ".join(commanders) if commanders else "Unknown Commander"
    return {"commander": commander_str, "cards": mainboard}


@app.post("/api/deck")
async def get_deck(request: DeckRequest):
    text = request.decklist.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Decklist is empty.")

    try:
        parsed = parse_decklist(text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "commander": parsed["commander"],
        "cards": parsed["cards"],
        "card_count": len(parsed["cards"]),
        "name": parsed["commander"],  # use commander as display name
    }


async def _load_commander_cache(time_period: str) -> None:
    """Fetch commanders from edhtop16 and populate the cache for the given time period."""
    if time_period in _commander_cache:
        return

    query = (
        "{ commanders(first: 200, sortBy: POPULARITY, timePeriod: "
        + time_period
        + ") { edges { node { name stats(filters: { timePeriod: "
        + time_period
        + ", minSize: 50 }) { metaShare count winRate } } } } }"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://edhtop16.com/api/graphql",
            json={"query": query},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()

    edges = data.get("data", {}).get("commanders", {}).get("edges", [])
    raw = []
    for edge in edges:
        node = edge.get("node", {})
        stats = node.get("stats") or {}
        meta = stats.get("metaShare") or 0
        if meta > 0:
            raw.append(
                {
                    "name": node["name"],
                    "meta_share": round(meta * 100, 2),
                    "count": stats.get("count") or 0,
                    "win_rate": round((stats.get("winRate") or 0) * 100, 1),
                }
            )

    # Deduplicate DFC commanders: "Name // Backside" and "Name" are the same card.
    # Key by front-face name; prefer the entry that includes the "//" full name.
    seen: dict[str, dict] = {}
    for c in raw:
        front = c["name"].split(" // ")[0].strip()
        if front not in seen:
            seen[front] = c
        else:
            existing = seen[front]
            # Prefer the version that carries the full DFC name (has "//")
            if " // " in c["name"] and " // " not in existing["name"]:
                seen[front] = c
            elif " // " in c["name"] == " // " in existing["name"] and c["meta_share"] > existing["meta_share"]:
                seen[front] = c

    _commander_cache[time_period] = list(seen.values())


@app.get("/api/commanders")
async def get_commanders(time_period: str = "THREE_MONTHS"):
    valid = {"ONE_MONTH", "THREE_MONTHS", "SIX_MONTHS", "ONE_YEAR", "ALL_TIME"}
    if time_period not in valid:
        time_period = "THREE_MONTHS"
    await _load_commander_cache(time_period)
    return {"commanders": [c["name"] for c in _commander_cache[time_period]]}


# Commanders that should always be displayed as the primary (front) card in a partner pair.
_PREFERRED_PRIMARY = {"Tymna the Weaver"}


def normalize_partner_order(name: str) -> str:
    """If a partner pair contains a preferred-primary commander, put it first."""
    if " / " not in name or " // " in name:
        return name
    parts = [p.strip() for p in name.split(" / ", 1)]
    if parts[1] in _PREFERRED_PRIMARY and parts[0] not in _PREFERRED_PRIMARY:
        return f"{parts[1]} / {parts[0]}"
    return name


def find_commander_stats(commander: str, cache: list) -> dict | None:
    """Look up a commander's stats in the cache, tolerating partner-order differences."""
    query_names = {n.strip().lower() for n in commander.replace(" // ", " / ").split(" / ")}
    for c in cache:
        cache_names = {n.strip().lower() for n in c["name"].replace(" // ", " / ").split(" / ")}
        if query_names == cache_names:
            return c
    return None


def card_image_names(commander_name: str) -> list[str]:
    """Return the Scryfall card name(s) needed to fetch images for a commander.
    ' / '  = partner pair → two separate card names
    ' // ' = double-faced card → front face only (one card)
    Partner order is normalized so preferred-primary commanders come first.
    """
    if " // " in commander_name:
        return [commander_name.split(" // ")[0].strip()]
    elif " / " in commander_name:
        normalized = normalize_partner_order(commander_name)
        return [p.strip() for p in normalized.split(" / ")]
    return [commander_name]


async def _fetch_images_fuzzy(names: list[str]) -> None:
    """Fetch and cache Scryfall images for a list of card names using fuzzy lookup.
    Fetches all uncached names concurrently. Only caches successes.
    """
    uncached = list(dict.fromkeys(n for n in names if n not in _image_cache))
    if not uncached:
        return

    async def fetch_one(client: httpx.AsyncClient, name: str) -> None:
        try:
            r = await client.get(
                "https://api.scryfall.com/cards/named",
                params={"fuzzy": name},
                timeout=10.0,
            )
            if r.status_code == 200:
                url = extract_image_url(r.json())
                if url:
                    _image_cache[name] = url
        except Exception:
            pass  # Don't cache failures — allow retry on next request

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[fetch_one(client, name) for name in uncached])


@app.get("/api/pod")
async def get_pod(time_period: str = "THREE_MONTHS", exclude: str = "", commander: str = ""):
    valid = {"ONE_MONTH", "THREE_MONTHS", "SIX_MONTHS", "ONE_YEAR", "ALL_TIME"}
    if time_period not in valid:
        time_period = "THREE_MONTHS"
    await _load_commander_cache(time_period)

    commanders = _commander_cache[time_period]

    if len(commanders) < 3:
        raise HTTPException(status_code=500, detail="Not enough commanders found in meta data.")

    weights = [c["meta_share"] for c in commanders]
    # Allow duplicates — mirror matches can happen in real pods
    opponents = [dict(c) for c in random.choices(commanders, weights=weights, k=3)]

    # Collect all image names needed: opponents + user's commander
    opponent_img_names = [n for opp in opponents for n in card_image_names(opp["name"])]
    user_img_names = card_image_names(commander) if commander else []
    await _fetch_images_fuzzy(opponent_img_names + user_img_names)

    for opp in opponents:
        opp["name"] = normalize_partner_order(opp["name"])
        names = card_image_names(opp["name"])
        opp["image_url"] = _image_cache.get(names[0])
        opp["image_url2"] = _image_cache.get(names[1]) if len(names) > 1 else None

    result: dict = {"opponents": opponents}
    if user_img_names:
        result["commander_image_url"] = _image_cache.get(user_img_names[0])
        result["commander_image_url2"] = _image_cache.get(user_img_names[1]) if len(user_img_names) > 1 else None
        # Return the normalized commander name so the frontend name/image order matches
        result["normalized_commander"] = normalize_partner_order(commander)
        # Look up commander stats from the cache
        stats = find_commander_stats(commander, commanders)
        if stats:
            result["commander_meta_share"] = stats["meta_share"]
            result["commander_win_rate"] = stats["win_rate"]

    return result


@app.post("/api/card-images")
async def get_card_images(request: CardNamesRequest):
    # Use fuzzy lookup for all cards — handles new cards, DFCs, and name variations.
    # _fetch_images_fuzzy only caches successes, so failures are retried on next request.
    primaries = [name.split(" / ")[0].strip() for name in request.names]
    await _fetch_images_fuzzy(primaries)
    return {name: _image_cache.get(name.split(" / ")[0].strip()) for name in request.names}




@app.get("/api/moxfield")
async def get_moxfield_deck(url: str):
    import re as _re
    m = _re.search(r"moxfield\.com/decks/([A-Za-z0-9_-]+)", url)
    if not m:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid Moxfield URL.")
    deck_id = m.group(1)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api2.moxfield.com/v2/decks/all/{deck_id}",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.moxfield.com/",
            },
            timeout=10.0,
        )
    if not resp.is_success:
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail=f"Moxfield returned {resp.status_code}.")
    data = resp.json()
    commanders = [
        e["card"]["name"]
        for e in (data.get("commanders") or {}).values()
        for _ in range(e.get("quantity", 1))
    ]
    mainboard = [
        e["card"]["name"]
        for e in (data.get("mainboard") or {}).values()
        for _ in range(e.get("quantity", 1))
    ]
    if not mainboard and not commanders:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Deck appears empty or private.")
    return {
        "commander": " / ".join(commanders) or "Unknown Commander",
        "cards": mainboard,
        "card_count": len(mainboard),
        "name": data.get("name", ""),
    }


@app.get("/api/matchups")
async def get_matchups(commander: str, time_period: str = "THREE_MONTHS"):
    """Compute head-to-head matchup stats for a commander vs all opponents it has faced."""
    return await _compute_matchups(commander, time_period)


async def _compute_matchups(commander: str, time_period: str):
    valid = {"ONE_MONTH", "THREE_MONTHS", "SIX_MONTHS", "ONE_YEAR", "ALL_TIME"}
    if time_period not in valid:
        time_period = "THREE_MONTHS"

    cache_key = f"{commander}|{time_period}"
    cached = _matchup_cache.get(cache_key)
    if cached and time.time() - cached[1] < _MATCHUP_TTL:
        return cached[0]

    # Step 1: fetch tournament entries from edhtop16
    gql_query = """
    query GetEntries($name: String!, $timePeriod: TimePeriod!) {
      commander(name: $name) {
        entries(first: 500, sortBy: NEW, filters: { timePeriod: $timePeriod, minEventSize: 0 }) {
          edges { node { player { topdeckProfile } tournament { TID } } }
        }
      }
    }
    """
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                "https://edhtop16.com/api/graphql",
                json={"query": gql_query, "variables": {"name": commander, "timePeriod": time_period}},
                timeout=20.0,
            )
            resp.raise_for_status()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"edhtop16 error: {e}")

    edges = (resp.json().get("data") or {}).get("commander", {}).get("entries", {}).get("edges", []) or []

    # Build TID → set of player profile IDs
    tid_to_players: dict[str, set] = {}
    for edge in edges:
        node = edge.get("node", {})
        tid = (node.get("tournament") or {}).get("TID")
        pid = (node.get("player") or {}).get("topdeckProfile")
        if tid and pid:
            tid_to_players.setdefault(tid, set()).add(pid)

    if not tid_to_players:
        return {"matchups": {}, "tournaments": 0, "message": "no_entries"}

    # Step 2: fetch round data concurrently for all tournaments
    async def fetch_rounds(c: httpx.AsyncClient, tid: str):
        try:
            r = await c.get(
                f"https://topdeck.gg/api/v2/tournaments/{tid}/rounds",
                headers={"Authorization": TOPDECK_API_KEY},
                timeout=12.0,
            )
            return tid, r.json() if r.status_code == 200 else None
        except BaseException:
            return tid, None

    stats: dict[str, dict] = {}
    tids = list(tid_to_players.keys())
    batch_size = 50
    async with httpx.AsyncClient() as client:
        for i in range(0, len(tids), batch_size):
            batch = tids[i:i + batch_size]
            results = await asyncio.gather(*[fetch_rounds(client, tid) for tid in batch])
            for tid, rounds in results:
                if not rounds:
                    continue
                target_pids = tid_to_players[tid]
                for round_data in (rounds if isinstance(rounds, list) else []):
                    for table in round_data.get("tables", []):
                        if table.get("status") != "Completed":
                            continue
                        players = [p for p in table.get("players", []) if p]
                        if len(players) < 2:
                            continue
                        pod = {
                            p["id"]: " / ".join(sorted((p.get("deckObj") or {}).get("Commanders", {}).keys()))
                            for p in players if p.get("id")
                        }
                        target_at_table = {pid for pid in target_pids if pid in pod}
                        if not target_at_table:
                            continue
                        winner_id = table.get("winner_id")
                        is_draw = not winner_id or winner_id == "Draw"
                        target_won = bool(winner_id and winner_id != "Draw" and winner_id in target_at_table)
                        for pid, opp in pod.items():
                            if pid in target_at_table or not opp:
                                continue
                            if opp not in stats:
                                stats[opp] = {"pods": 0, "wins": 0, "draws": 0}
                            stats[opp]["pods"] += 1
                            if target_won:
                                stats[opp]["wins"] += 1
                            if is_draw:
                                stats[opp]["draws"] += 1
                        # Mirror match: multiple target players at the same table
                        if len(target_at_table) > 1:
                            mirror_name = next(
                                (pod[pid] for pid in target_at_table if pid in pod and pod[pid]), None
                            )
                            if mirror_name:
                                if mirror_name not in stats:
                                    stats[mirror_name] = {"pods": 0, "wins": 0, "draws": 0}
                                stats[mirror_name]["pods"] += 1
                                if target_won:
                                    stats[mirror_name]["wins"] += 1
                                if is_draw:
                                    stats[mirror_name]["draws"] += 1

    result = {
        opp: {
            "pods": s["pods"],
            "win_rate": round(s["wins"] / s["pods"] * 100, 1),
            "draw_rate": round(s["draws"] / s["pods"] * 100, 1),
        }
        for opp, s in stats.items()
    }

    out = {"matchups": result, "tournaments": len(tid_to_players), "entries": len(edges), "raw_opponents": len(stats)}
    _matchup_cache[cache_key] = (out, time.time())
    return out


@app.get("/")
async def root():
    return FileResponse(
        os.path.join(_STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
