from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import random
import re
from typing import Optional

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


@app.get("/api/pod")
async def get_pod(time_period: str = "THREE_MONTHS", exclude: str = ""):
    valid = {"ONE_MONTH", "THREE_MONTHS", "SIX_MONTHS", "ONE_YEAR", "ALL_TIME"}
    if time_period not in valid:
        time_period = "THREE_MONTHS"

    if time_period not in _commander_cache:
        query = (
            "{ commanders(first: 200, sortBy: POPULARITY, timePeriod: "
            + time_period
            + ") { edges { node { name stats(filters: { timePeriod: "
            + time_period
            + " }) { metaShare count winRate } } } } }"
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
        commanders = []
        for edge in edges:
            node = edge.get("node", {})
            stats = node.get("stats") or {}
            meta = stats.get("metaShare") or 0
            if meta > 0:
                commanders.append(
                    {
                        "name": node["name"],
                        "meta_share": round(meta * 100, 2),
                        "count": stats.get("count") or 0,
                        "win_rate": round((stats.get("winRate") or 0) * 100, 1),
                    }
                )
        _commander_cache[time_period] = commanders

    commanders = _commander_cache[time_period]
    exclude_lower = exclude.lower()
    available = [c for c in commanders if exclude_lower not in c["name"].lower()]

    if len(available) < 3:
        raise HTTPException(status_code=500, detail="Not enough commanders found in meta data.")

    weights = [c["meta_share"] for c in available]
    seen, opponents = set(), []
    for c in random.choices(available, weights=weights, k=len(available)):
        if c["name"] not in seen:
            seen.add(c["name"])
            opponents.append(dict(c))
        if len(opponents) == 3:
            break

    # Fetch Scryfall images for commanders
    uncached = [
        opp["name"].split(" / ")[0].strip()
        for opp in opponents
        if opp["name"].split(" / ")[0].strip() not in _image_cache
    ]
    if uncached:
        async with httpx.AsyncClient() as client:
            for name in uncached:
                try:
                    r = await client.get(
                        "https://api.scryfall.com/cards/named",
                        params={"fuzzy": name},
                        timeout=5.0,
                    )
                    _image_cache[name] = extract_image_url(r.json())
                except Exception:
                    _image_cache[name] = None

    for opp in opponents:
        primary = opp["name"].split(" / ")[0].strip()
        opp["image_url"] = _image_cache.get(primary)

    return {"opponents": opponents}


@app.post("/api/card-images")
async def get_card_images(request: CardNamesRequest):
    result: dict = {}
    uncached: list[tuple[str, str]] = []

    for name in request.names:
        primary = name.split(" / ")[0].strip()
        if primary in _image_cache:
            result[name] = _image_cache[primary]
        else:
            uncached.append((name, primary))

    if uncached:
        identifiers = [{"name": primary} for _, primary in uncached]
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    "https://api.scryfall.com/cards/collection",
                    json={"identifiers": identifiers},
                    headers={"Content-Type": "application/json"},
                    timeout=10.0,
                )
                scry_data = resp.json().get("data", [])
                scry_lookup = {c["name"]: extract_image_url(c) for c in scry_data}
            except Exception:
                scry_lookup = {}

        for name, primary in uncached:
            url = scry_lookup.get(primary)
            _image_cache[primary] = url
            result[name] = url

    return result


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
