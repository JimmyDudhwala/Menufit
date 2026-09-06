#!/usr/bin/env python3
"""Import Adajan restaurants + menu source URLs into local Postgres."""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "adajan-restaurants.csv"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)


def parse_json(raw: str):
    if not raw or raw.strip() in ("", "[]"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def parse_coord(raw: str) -> tuple[float | None, float | None]:
    obj = parse_json(raw)
    if not isinstance(obj, dict):
        return None, None
    lat = obj.get("latitude") or obj.get("lat")
    lng = obj.get("longitude") or obj.get("lng")
    return (float(lat) if lat is not None else None, float(lng) if lng is not None else None)


def collect_sources(row: dict) -> list[tuple[str, str, int]]:
    """Return (source_type, url, priority) lowest priority = try first."""
    sources: list[tuple[str, str, int]] = []
    priority = 1

    menu_field = parse_json(row.get("menu", ""))
    if isinstance(menu_field, dict) and menu_field.get("link"):
        link = menu_field["link"]
        src = menu_field.get("source", "")
        stype = "petpooja" if "petpooja" in link or "petpooja" in src else "website_menu"
        sources.append((stype, link, priority))
        priority += 1
    elif isinstance(menu_field, list):
        for item in menu_field:
            if isinstance(item, dict) and item.get("link"):
                link = item["link"]
                stype = "petpooja" if "petpooja" in link else "website_menu"
                sources.append((stype, link, priority))
                priority += 1

    for item in parse_json(row.get("order_online_links", "")) or []:
        if not isinstance(item, dict):
            continue
        link = item.get("link", "")
        if "zomato.com" in link:
            sources.append(("zomato", link, priority))
            priority += 1
        elif "swiggy.com" in link:
            sources.append(("swiggy", link, priority))
            priority += 1

    if row.get("website"):
        sources.append(("website", row["website"], priority))
        priority += 1

    if row.get("featured_image"):
        sources.append(("vision_photo", row["featured_image"], priority))

    return sources


def main() -> None:
    if not CSV_PATH.exists():
        print(f"Missing {CSV_PATH}. Run: python3 scripts/filter-adajan.py", file=sys.stderr)
        sys.exit(1)

    sql_path = ROOT / "sql" / "001_schema.sql"
    schema_sql = sql_path.read_text(encoding="utf-8")

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        cur.execute(schema_sql)

        restaurant_count = 0
        source_count = 0

        with CSV_PATH.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                lat, lng = parse_coord(row.get("coordinates", ""))
                cur.execute(
                    """
                    INSERT INTO restaurants (
                      kgmid, place_id, name, area, main_category, categories,
                      rating, user_ratings_total, phone, website, address,
                      detailed_address, lat, lng, maps_link, featured_image,
                      price_range, hours, updated_at
                    ) VALUES (
                      %s, %s, %s, 'Adajan', %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s,
                      %s, %s, NOW()
                    )
                    ON CONFLICT (kgmid) DO UPDATE SET
                      name = EXCLUDED.name,
                      main_category = EXCLUDED.main_category,
                      categories = EXCLUDED.categories,
                      rating = EXCLUDED.rating,
                      user_ratings_total = EXCLUDED.user_ratings_total,
                      phone = EXCLUDED.phone,
                      website = EXCLUDED.website,
                      address = EXCLUDED.address,
                      detailed_address = EXCLUDED.detailed_address,
                      lat = EXCLUDED.lat,
                      lng = EXCLUDED.lng,
                      maps_link = EXCLUDED.maps_link,
                      featured_image = EXCLUDED.featured_image,
                      price_range = EXCLUDED.price_range,
                      hours = EXCLUDED.hours,
                      updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        row.get("kgmid"),
                        row.get("place_id"),
                        row.get("name"),
                        row.get("main_category"),
                        Json(parse_json(row.get("categories", ""))),
                        float(row["rating"]) if row.get("rating") else None,
                        int(float(row["reviews"])) if row.get("reviews") else None,
                        row.get("phone"),
                        row.get("website"),
                        row.get("address"),
                        Json(parse_json(row.get("detailed_address", ""))),
                        lat,
                        lng,
                        row.get("link"),
                        row.get("featured_image"),
                        row.get("price_range"),
                        Json(parse_json(row.get("hours", ""))),
                    ),
                )
                restaurant_id = cur.fetchone()[0]
                restaurant_count += 1

                for source_type, source_url, priority in collect_sources(row):
                    cur.execute(
                        """
                        INSERT INTO menu_sources (restaurant_id, source_type, source_url, priority)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (restaurant_id, source_type, source_url) DO NOTHING
                        """,
                        (restaurant_id, source_type, source_url, priority),
                    )
                    source_count += 1

        conn.commit()
        print(f"Imported {restaurant_count} restaurants")
        print(f"Registered {source_count} menu source URLs")
        print(f"Database: {DATABASE_URL}")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
