from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import random
import re
import os
from typing import Optional

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI()

# Simple in-memory caches (reset on server restart)
_deck_cache: dict = {}
_image_cache: dict = {}
_commander_cache: dict = {}

CARD_BACK = "https://cards.scryfall.io/normal/back/0/0/0aeebaf5-8c7d-4636-9e82-8c27447861f7.jpg"


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
    Only caches successes — failed fetches are not stored so they are retried later.
    """
    # Deduplicate while preserving order so each name is fetched at most once
    uncached = list(dict.fromkeys(n for n in names if n not in _image_cache))
    if not uncached:
        return
    async with httpx.AsyncClient() as client:
        for name in uncached:
            try:
                r = await client.get(
                    "https://api.scryfall.com/cards/named",
                    params={"fuzzy": name},
                    timeout=8.0,
                )
                if r.status_code == 200:
                    url = extract_image_url(r.json())
                    if url:
                        _image_cache[name] = url
            except Exception:
                pass  # Don't cache failures — allow retry on next request


@app.get("/api/pod")
async def get_pod(time_period: str = "THREE_MONTHS", exclude: str = "", commander: str = ""):
    valid = {"ONE_MONTH", "THREE_MONTHS", "SIX_MONTHS", "ONE_YEAR", "ALL_TIME"}
    if time_period not in valid:
        time_period = "THREE_MONTHS"
    await _load_commander_cache(time_period)

    commanders = _commander_cache[time_period]
    exclude_lower = exclude.lower()
    available = [c for c in commanders if exclude_lower not in c["name"].lower()]

    if len(available) < 3:
        raise HTTPException(status_code=500, detail="Not enough commanders found in meta data.")

    weights = [c["meta_share"] for c in available]
    # Allow duplicates — the same commander can appear multiple times in a real pod
    opponents = [dict(c) for c in random.choices(available, weights=weights, k=3)]

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


@app.get("/")
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
