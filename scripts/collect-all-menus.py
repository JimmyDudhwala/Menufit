#!/usr/bin/env python3
"""
Collect menu screenshots for EVERY Adajan restaurant still missing a local file.

No LLM extraction — store only.

Priority:
1. Swiggy / Zomato (fix dineout/book → menu/order URL) via Playwright screenshot
2. Existing website/petpooja URLs via Playwright
3. Google Maps place page screenshot as last resort

Usage:
  python3 scripts/collect-all-menus.py
  python3 scripts/collect-all-menus.py --limit 10
  python3 scripts/collect-all-menus.py --bucket delivery
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "data" / "menu-assets"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return (s[:60] or "restaurant")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_menu_url(url: str) -> str:
    """Turn dineout/book links into pages more likely to show food menu."""
    if not url:
        return url
    u = url.strip()
    # Swiggy dineout → restaurant root
    if "swiggy.com" in u:
        u = re.sub(r"/dineout.*$", "", u)
        u = u.split("?")[0].rstrip("/")
        return u
    # Zomato book → order (or menu)
    if "zomato.com" in u:
        u = re.sub(r"/book.*$", "/order", u)
        if "/menu" not in u and "/order" not in u:
            parsed = urlparse(u)
            path = parsed.path.rstrip("/")
            if not path.endswith("/order") and not path.endswith("/menu"):
                path = path + "/order"
            u = urlunparse(parsed._replace(path=path, query="", fragment=""))
        return u.split("?")[0]
    return u.split("#")[0]


def restaurants_missing_file(cur, bucket: str | None):
    cur.execute(
        """
        SELECT r.id, r.name, r.kgmid, r.maps_link
        FROM restaurants r
        WHERE NOT EXISTS (
          SELECT 1 FROM menu_assets a
          WHERE a.restaurant_id = r.id
            AND a.local_path IS NOT NULL
            AND a.extract_status = 'pending'
        )
        ORDER BY r.user_ratings_total DESC NULLS LAST
        """
    )
    rows = cur.fetchall()
    out = []
    for rid, name, kgmid, maps_link in rows:
        cur.execute(
            """
            SELECT source_type, source_url FROM menu_assets
            WHERE restaurant_id = %s
            ORDER BY
              CASE source_type
                WHEN 'petpooja' THEN 1
                WHEN 'website_menu' THEN 2
                WHEN 'swiggy' THEN 3
                WHEN 'zomato' THEN 4
                WHEN 'website' THEN 5
                ELSE 9
              END
            """,
            (rid,),
        )
        sources = cur.fetchall()
        # also pull from menu_sources table (may have more)
        cur.execute(
            """
            SELECT source_type, source_url FROM menu_sources
            WHERE restaurant_id = %s
            ORDER BY priority
            """,
            (rid,),
        )
        for st, su in cur.fetchall():
            if (st, su) not in sources:
                sources.append((st, su))

        has_delivery = any(st in ("swiggy", "zomato") for st, _ in sources)
        has_web = any(st in ("petpooja", "website_menu", "website") for st, _ in sources)

        if bucket == "delivery" and not has_delivery:
            continue
        if bucket == "web" and (has_delivery or not has_web):
            continue
        if bucket == "maps" and (has_delivery or has_web):
            continue

        out.append(
            {
                "id": rid,
                "name": name,
                "kgmid": kgmid,
                "maps_link": maps_link,
                "sources": sources,
                "has_delivery": has_delivery,
                "has_web": has_web,
            }
        )
    return out


def pick_urls(rest: dict) -> list[tuple[str, str]]:
    picks: list[tuple[str, str]] = []
    for st, url in rest["sources"]:
        if st in ("swiggy", "zomato", "petpooja", "website_menu", "website"):
            picks.append((st, normalize_menu_url(url)))
    if not picks and rest.get("maps_link"):
        picks.append(("maps", rest["maps_link"]))
    # unique preserve order
    seen = set()
    out = []
    for st, u in picks:
        if u in seen:
            continue
        seen.add(u)
        out.append((st, u))
    return out[:3]  # try up to 3


def upsert_image_asset(
    cur,
    *,
    restaurant_id: int,
    source_type: str,
    source_url: str,
    local_path: str,
    nbytes: int,
    digest: str,
    notes: str,
) -> None:
    # Prefer updating existing url row; else insert with slightly unique source_url for image
    cur.execute(
        """
        SELECT id FROM menu_assets
        WHERE restaurant_id = %s AND source_url = %s
        LIMIT 1
        """,
        (restaurant_id, source_url),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            """
            UPDATE menu_assets
            SET asset_kind = 'image',
                local_path = %s,
                bytes = %s,
                file_sha256 = %s,
                extract_status = 'pending',
                notes = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (local_path, nbytes, digest, notes, row[0]),
        )
        return

    image_url = source_url + ("&" if "?" in source_url else "?") + "menufit_asset=screenshot"
    cur.execute(
        """
        INSERT INTO menu_assets (
          restaurant_id, source_type, source_url, asset_kind,
          local_path, file_sha256, bytes, extract_status, notes, updated_at
        ) VALUES (%s,%s,%s,'image',%s,%s,%s,'pending',%s,NOW())
        ON CONFLICT DO NOTHING
        """,
        (restaurant_id, source_type, image_url, local_path, digest, nbytes, notes),
    )


def capture_page(page, url: str, dest_png: Path, dest_html: Path) -> tuple[bool, str]:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2.5)

        # dismiss common popups / location prompts lightly
        for sel in [
            "button:has-text('Accept')",
            "button:has-text('OK')",
            "button:has-text('Got it')",
            "button:has-text('Continue')",
            "[aria-label='Close']",
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1000)
            except Exception:
                pass

        dest_png.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(dest_png), full_page=True)
        dest_html.write_text(page.content(), encoding="utf-8")

        if dest_png.stat().st_size < 15000:
            return False, "screenshot_too_small"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--bucket",
        choices=["all", "delivery", "web", "maps"],
        default="all",
        help="Which gap set to fill first",
    )
    args = parser.parse_args()

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    missing = restaurants_missing_file(cur, None if args.bucket == "all" else args.bucket)
    if args.limit:
        missing = missing[: args.limit]

    print(f"Restaurants still missing local menu file: {len(missing)}")
    if not missing:
        return

    stats = {"ok": 0, "fail": 0, "skip": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 1800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        for i, rest in enumerate(missing, 1):
            urls = pick_urls(rest)
            if not urls:
                stats["skip"] += 1
                print(f"[{i}/{len(missing)}] SKIP no url  {rest['name']}")
                continue

            folder = ASSETS_DIR / f"{slugify(rest['name'])}__{rest['kgmid'].replace('/', '_')}"
            folder.mkdir(parents=True, exist_ok=True)

            success = False
            last_err = ""
            for st, url in urls:
                dest_png = folder / f"{st}_menu.png"
                dest_html = folder / f"{st}_menu.html"
                print(f"[{i}/{len(missing)}] TRY [{st}] {rest['name'][:40]}")
                print(f"         {url[:100]}")
                ok, info = capture_page(page, url, dest_png, dest_html)
                if not ok:
                    last_err = info
                    print(f"         fail: {info}")
                    continue

                digest = sha256_file(dest_png)
                upsert_image_asset(
                    cur,
                    restaurant_id=rest["id"],
                    source_type=st,
                    source_url=url,
                    local_path=str(dest_png.relative_to(ROOT)),
                    nbytes=dest_png.stat().st_size,
                    digest=digest,
                    notes=f"playwright full-page screenshot; html={dest_html.relative_to(ROOT)}",
                )
                conn.commit()
                success = True
                stats["ok"] += 1
                print(f"         OK {dest_png.stat().st_size} bytes")
                break

            if not success:
                stats["fail"] += 1
                # record failure note on first source if any
                st, url = urls[0]
                cur.execute(
                    """
                    INSERT INTO menu_assets (
                      restaurant_id, source_type, source_url, asset_kind,
                      extract_status, notes, updated_at
                    ) VALUES (%s,%s,%s,'url_only','failed',%s,NOW())
                    ON CONFLICT DO NOTHING
                    """,
                    (rest["id"], st, url + "?menufit_try=1", f"screenshot_failed: {last_err}"),
                )
                conn.commit()

            time.sleep(1.2)

        browser.close()

    print("\n=== collect-all-menus summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    cur.execute(
        """
        SELECT COUNT(DISTINCT restaurant_id)
        FROM menu_assets
        WHERE local_path IS NOT NULL AND extract_status = 'pending'
        """
    )
    print(f"  restaurants with pending local files now: {cur.fetchone()[0]} / 260")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
