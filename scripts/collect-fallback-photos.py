#!/usr/bin/env python3
"""Fill restaurants still missing local files using Food/featured photos from full CSV."""

from __future__ import annotations

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
ASSETS = ROOT / "data" / "menu-assets"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)
UA = "Mozilla/5.0 MenufitCollector/0.2"


def slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")[:60] or "r"


def download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.read()
    except Exception:
        return None


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.id, r.name, r.kgmid, r.featured_image
        FROM restaurants r
        WHERE NOT EXISTS (
          SELECT 1 FROM menu_assets a
          WHERE a.restaurant_id = r.id
            AND a.local_path IS NOT NULL
            AND a.extract_status = 'pending'
        )
        """
    )
    missing = {kgmid: (rid, name, feat) for rid, name, kgmid, feat in cur.fetchall()}
    print(f"Still missing local files: {len(missing)}")

    cands: dict[str, list[str]] = {}
    with FULL_CSV.open(encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            kgmid = row.get("kgmid")
            if kgmid not in missing:
                continue
            urls: list[str] = []
            try:
                images = json.loads(row.get("images") or "[]")
            except json.JSONDecodeError:
                images = []
            for img in images if isinstance(images, list) else []:
                if not isinstance(img, dict) or not img.get("link"):
                    continue
                about = str(img.get("about") or "").lower()
                if (
                    about in ("menu", "food & drink", "food", "latest")
                    or "food" in about
                    or "menu" in about
                ):
                    urls.append(img["link"])
            if not urls:
                for img in (images if isinstance(images, list) else [])[:5]:
                    if not isinstance(img, dict) or not img.get("link"):
                        continue
                    about = str(img.get("about") or "").lower()
                    if about in ("videos", "street view & 360deg", "street view & 360°"):
                        continue
                    urls.append(img["link"])
            feat = missing[kgmid][2] or row.get("featured_image")
            if feat:
                urls = [feat] + [u for u in urls if u != feat]
            cands[kgmid] = urls[:5]

    ok = 0
    photos = 0
    for i, (kgmid, (rid, name, feat)) in enumerate(missing.items(), 1):
        urls = cands.get(kgmid) or ([feat] if feat else [])
        if not urls:
            print(f"[{i}] NO URL {name}")
            continue
        folder = ASSETS / f"{slugify(name)}__{kgmid.replace('/', '_')}"
        folder.mkdir(parents=True, exist_ok=True)
        saved = 0
        for idx, url in enumerate(urls, 1):
            data = download(url)
            if not data or len(data) < 2000:
                continue
            ext = ".png" if data.startswith(b"\x89PNG") else ".jpg"
            dest = folder / f"fallback_photo_{idx}{ext}"
            dest.write_bytes(data)
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
                    f"{url}#menufit_fallback_{idx}",
                    str(dest.relative_to(ROOT)),
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    "fallback photos (no Menu tag) — store for later LLM/review",
                ),
            )
            saved += 1
            photos += 1
            time.sleep(0.12)
        if saved:
            ok += 1
            print(f"[{i}/{len(missing)}] {name[:45]:45} +{saved}")
            conn.commit()
        else:
            print(f"[{i}/{len(missing)}] FAIL {name}")

    cur.execute(
        """
        SELECT COUNT(DISTINCT restaurant_id)
        FROM menu_assets
        WHERE local_path IS NOT NULL AND extract_status = 'pending'
        """
    )
    print(f"\nCoverage: {cur.fetchone()[0]} / 260")
    print(f"Filled now: {ok} restaurants, {photos} photos")
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
