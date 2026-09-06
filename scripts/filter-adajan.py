#!/usr/bin/env python3
"""Filter Surat scraper CSV → Adajan MVP restaurant list."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "restaurant-in-surat-gujarat-india.csv"
OUTPUT = ROOT / "data" / "adajan-restaurants.csv"

ADAJAN = dict(latMin=21.175, latMax=21.215, lngMin=72.765, lngMax=72.815)
MIN_RATING = 4.0
MIN_REVIEWS = 50

EXCLUDE_CATEGORIES = (
    "supermarket",
    "grocery",
    "movie theater",
    "cinema",
    "hotel",
    "hospital",
    "school",
    "bank",
    "atm",
    "petrol",
    "gas station",
    "clothing",
    "jewellery",
    "jewelry",
    "pharmacy",
    "dentist",
    "doctor",
)

FOOD_KEYWORDS = (
    "restaurant",
    "cafe",
    "bakery",
    "dhaba",
    "biryani",
    "pizza",
    "burger",
    "thali",
    "food",
    "dining",
    "kitchen",
    "bistro",
    "bar",
    "lounge",
    "sweet",
    "snack",
    "chaat",
    "dosa",
    "south indian",
    "punjabi",
    "gujarati",
    "fast food",
    "meal",
    "canteen",
    "ice cream",
    "juice",
)

KEEP_COLUMNS = [
    "place_id",
    "kgmid",
    "name",
    "main_category",
    "categories",
    "rating",
    "reviews",
    "phone",
    "website",
    "address",
    "detailed_address",
    "coordinates",
    "link",
    "menu",
    "order_online_links",
    "featured_image",
    "price_range",
    "hours",
]


def parse_coord(raw: str | None) -> tuple[float, float] | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        lat = obj.get("latitude") or obj.get("lat")
        lng = obj.get("longitude") or obj.get("lng")
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def in_adajan(row: dict[str, str]) -> bool:
    blob = f"{row.get('detailed_address', '')} {row.get('address', '')}".lower()
    if "adajan" in blob:
        return True
    coord = parse_coord(row.get("coordinates"))
    if not coord:
        return False
    lat, lng = coord
    return (
        ADAJAN["latMin"] <= lat <= ADAJAN["latMax"]
        and ADAJAN["lngMin"] <= lng <= ADAJAN["lngMax"]
    )


def is_food_place(row: dict[str, str]) -> bool:
    blob = f"{row.get('main_category', '')} {row.get('categories', '')}".lower()
    if any(x in blob for x in EXCLUDE_CATEGORIES):
        return False
    return any(k in blob for k in FOOD_KEYWORDS)


def passes_mvp(row: dict[str, str]) -> bool:
    try:
        rating = float(row.get("rating") or 0)
        reviews = int(float(row.get("reviews") or 0))
    except ValueError:
        return False
    return rating >= MIN_RATING and reviews >= MIN_REVIEWS


def dedupe(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        key = row.get("kgmid") or row.get("place_id") or row.get("name", "")
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def main() -> None:
    if not INPUT.exists():
        print(f"Missing input: {INPUT}", file=sys.stderr)
        sys.exit(1)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    matched: list[dict[str, str]] = []
    with INPUT.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            if not in_adajan(row) or not passes_mvp(row) or not is_food_place(row):
                continue
            matched.append({col: row.get(col, "") for col in KEEP_COLUMNS})

    matched = dedupe(matched)
    matched.sort(key=lambda r: int(float(r.get("reviews") or 0)), reverse=True)

    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=KEEP_COLUMNS)
        writer.writeheader()
        writer.writerows(matched)

    with_menu = sum(1 for r in matched if (r.get("menu") or "").strip())
    with_order = sum(1 for r in matched if (r.get("order_online_links") or "").strip() not in ("", "[]"))

    print(f"Wrote {len(matched)} restaurants → {OUTPUT}")
    print(f"  with menu link: {with_menu}")
    print(f"  with order-online links: {with_order}")


if __name__ == "__main__":
    main()
