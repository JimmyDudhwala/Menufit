#!/usr/bin/env python3
"""
Collect Google Maps "Menu" photos for every Adajan restaurant from the full CSV.

Store only — no LLM extraction.

Usage:
  python3 scripts/collect-gmaps-menu-photos.py
  python3 scripts/collect-gmaps-menu-photos.py --limit 20
  python3 scripts/collect-gmaps-menu-photos.py --only-missing
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
FULL_CSV = ROOT / "restaurant-in-surat-gujarat-india.csv"
ADAJAN_CSV = ROOT / "data" / "adajan-restaurants.csv"
ASSETS_DIR = ROOT / "data" / "menu-assets"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MenufitCollector/0.2"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return (s[:60] or "restaurant")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_menu_image(img: dict) -> bool:
    about = str(img.get("about") or "").strip().lower()
    if about == "menu" or about.startswith("menu"):
        return True
    # some entries use category-like labels
    if "menu" in about and len(about) < 40:
        return True
    return False


def download(url: str, timeout: int = 45) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only restaurants without a pending local file yet",
    )
    parser.add_argument("--max-photos", type=int, default=8, help="Max menu photos per restaurant")
    args = parser.parse_args()

    adajan = {
        row["kgmid"]: row
        for row in csv.DictReader(ADAJAN_CSV.open(encoding="utf-8", newline=""))
    }

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT id, kgmid FROM restaurants")
    id_by_kgmid = {kgmid: rid for rid, kgmid in cur.fetchall()}

    missing_ids: set[int] | None = None
    if args.only_missing:
        cur.execute(
            """
            SELECT id FROM restaurants r
            WHERE NOT EXISTS (
              SELECT 1 FROM menu_assets a
              WHERE a.restaurant_id = r.id
                AND a.local_path IS NOT NULL
                AND a.extract_status = 'pending'
            )
            """
        )
        missing_ids = {r[0] for r in cur.fetchall()}

    # Build kgmid -> menu image urls from FULL csv
    menu_photos: dict[str, list[str]] = {}
    with FULL_CSV.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            kgmid = row.get("kgmid")
            if kgmid not in adajan:
                continue
            try:
                images = json.loads(row.get("images") or "[]")
            except json.JSONDecodeError:
                images = []
            if not isinstance(images, list):
                continue
            urls = []
            for img in images:
                if not isinstance(img, dict):
                    continue
                if not is_menu_image(img):
                    continue
                link = img.get("link")
                if link:
                    urls.append(link)
            if urls:
                menu_photos[kgmid] = urls[: args.max_photos]

    targets = list(menu_photos.items())
    if args.only_missing and missing_ids is not None:
        targets = [
            (k, v)
            for k, v in targets
            if id_by_kgmid.get(k) in missing_ids
        ]
    if args.limit:
        targets = targets[: args.limit]

    print(f"Restaurants with GMaps Menu photos to collect: {len(targets)}")
    print(f"(total Adajan with menu-tagged photos in CSV: {len(menu_photos)})")

    stats = {"restaurants_ok": 0, "photos": 0, "failed_photos": 0, "skipped": 0}

    for i, (kgmid, urls) in enumerate(targets, 1):
        rid = id_by_kgmid.get(kgmid)
        row = adajan[kgmid]
        if not rid:
            continue

        folder = ASSETS_DIR / f"{slugify(row['name'])}__{kgmid.replace('/', '_')}"
        folder.mkdir(parents=True, exist_ok=True)

        saved = 0
        for idx, url in enumerate(urls, 1):
            source_url = f"{url}#menufit_gmaps_menu_{idx}"
            cur.execute(
                """
                SELECT 1 FROM menu_assets
                WHERE restaurant_id = %s AND source_url = %s
                  AND local_path IS NOT NULL AND extract_status = 'pending'
                LIMIT 1
                """,
                (rid, source_url),
            )
            if cur.fetchone():
                continue

            data = download(url)
            if not data or len(data) < 2000:
                stats["failed_photos"] += 1
                continue

            ext = ".jpg"
            if data[:8].startswith(b"\x89PNG"):
                ext = ".png"
            elif data[:3] == b"GIF":
                ext = ".gif"
            dest = folder / f"gmaps_menu_{idx}{ext}"
            dest.write_bytes(data)
            digest = sha256_bytes(data)

            cur.execute(
                """
                INSERT INTO menu_assets (
                  restaurant_id, source_type, source_url, asset_kind,
                  local_path, file_sha256, bytes, extract_status, notes, updated_at
                ) VALUES (%s,'vision_photo',%s,'image',%s,%s,%s,'pending',%s,NOW())
                ON CONFLICT DO NOTHING
                """,
                (
                    rid,
                    source_url,
                    str(dest.relative_to(ROOT)),
                    digest,
                    len(data),
                    "Google Maps photo labeled Menu — store only, LLM later",
                ),
            )
            saved += 1
            stats["photos"] += 1
            time.sleep(0.15)

        if saved:
            stats["restaurants_ok"] += 1
            print(f"[{i}/{len(targets)}] {row['name'][:45]:45} +{saved} menu photos")
            conn.commit()
        else:
            stats["skipped"] += 1

    print("\n=== gmaps menu photo collect ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    cur.execute(
        """
        SELECT COUNT(DISTINCT restaurant_id)
        FROM menu_assets
        WHERE local_path IS NOT NULL AND extract_status = 'pending'
        """
    )
    with_file = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM restaurants")
    total = cur.fetchone()[0]
    print(f"  coverage: {with_file} / {total} restaurants have local files")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
