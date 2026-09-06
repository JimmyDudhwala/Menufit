#!/usr/bin/env python3
"""
Collect raw menu assets for Adajan restaurants.

Phase A (this script): DOWNLOAD + STORE files only. No LLM extraction.
Phase B (later): LLM API key → extract items from stored assets.

Usage:
  python3 scripts/collect-menu-assets.py           # all downloadable
  python3 scripts/collect-menu-assets.py --limit 5
  python3 scripts/collect-menu-assets.py --kind pdf
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "adajan-restaurants.csv"
ASSETS_DIR = ROOT / "data" / "menu-assets"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) MenufitCollector/0.1"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return (s[:60] or "restaurant")


def parse_json(raw: str):
    if not raw or raw.strip() in ("", "[]"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def drive_file_id(url: str) -> str | None:
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def classify_url(url: str) -> tuple[str, str] | None:
    """Return (asset_kind, download_url) or None if skip."""
    u = url.strip()
    low = u.lower()

    if "drive.google.com/drive/folders/" in low:
        return ("folder", u)

    fid = drive_file_id(u)
    if fid:
        return ("pdf", f"https://drive.google.com/uc?export=download&id={fid}")

    if low.endswith(".pdf") or ".pdf?" in low or ".pdf#" in low:
        return ("pdf", u)

    if any(x in low for x in ("petpooja.com", "/menu", "mcdelivery", "dominos", "subway.in/menu")):
        return ("html", u)

    if any(x in low for x in ("swiggy.com", "zomato.com")):
        return ("url_only", u)

    if "googleusercontent.com" in low or low.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return ("image", u)

    return None


def collect_candidate_urls(row: dict) -> list[tuple[str, str]]:
    """(source_type_label, url) in priority order."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(stype: str, url: str) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        out.append((stype, url))

    menu = parse_json(row.get("menu", "") or "")
    if isinstance(menu, dict) and menu.get("link"):
        link = menu["link"]
        st = "petpooja" if "petpooja" in link else "website_menu"
        add(st, link)
    elif isinstance(menu, list):
        for it in menu:
            if isinstance(it, dict) and it.get("link"):
                link = it["link"]
                st = "petpooja" if "petpooja" in link else "website_menu"
                add(st, link)

    for it in parse_json(row.get("order_online_links", "") or "") or []:
        if not isinstance(it, dict):
            continue
        link = it.get("link", "")
        if "zomato.com" in link:
            add("zomato", link)
        elif "swiggy.com" in link:
            add("swiggy", link)

    if row.get("website"):
        add("website", row["website"])

    # featured image last — usually not a menu, but keep for completeness
    if row.get("featured_image"):
        add("vision_photo", row["featured_image"])

    return out


def download(
    url: str,
    dest: Path,
    *,
    expect_kind: str,
    timeout: int = 60,
) -> tuple[bool, str]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()

        is_html = (
            b"<!DOCTYPE html>" in data[:300]
            or b"<html" in data[:300].lower()
            or "text/html" in ctype
        )
        is_pdf = data[:4] == b"%PDF" or "pdf" in ctype

        # Drive sometimes returns an HTML interstitial instead of the PDF
        if expect_kind == "pdf" and is_html and not is_pdf:
            return False, "drive_html_interstitial_not_pdf"

        # HTML menu pages are supposed to be HTML — save them
        dest.write_bytes(data)
        return True, ctype
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def pdf_page_count(path: Path) -> int | None:
    try:
        import pymupdf

        return pymupdf.open(path).page_count
    except Exception:
        return None


def ensure_schema(cur) -> None:
    sql = (ROOT / "sql" / "002_menu_assets.sql").read_text(encoding="utf-8")
    cur.execute(sql)


def already_have(cur, restaurant_id: int, source_url: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM menu_assets
        WHERE restaurant_id = %s AND source_url = %s
          AND extract_status <> 'failed'
        LIMIT 1
        """,
        (restaurant_id, source_url),
    )
    return cur.fetchone() is not None


def insert_asset(
    cur,
    *,
    restaurant_id: int,
    source_type: str,
    source_url: str,
    asset_kind: str,
    local_path: str | None,
    file_sha: str | None,
    nbytes: int | None,
    page_count: int | None,
    notes: str | None,
    extract_status: str = "pending",
) -> None:
    cur.execute(
        """
        INSERT INTO menu_assets (
          restaurant_id, source_type, source_url, asset_kind,
          local_path, file_sha256, bytes, page_count,
          extract_status, notes, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT DO NOTHING
        """,
        (
            restaurant_id,
            source_type,
            source_url,
            asset_kind,
            local_path,
            file_sha,
            nbytes,
            page_count,
            extract_status,
            notes,
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Max restaurants to process")
    parser.add_argument(
        "--kind",
        choices=["pdf", "html", "image", "url_only", "all"],
        default="all",
    )
    parser.add_argument(
        "--include-featured",
        action="store_true",
        help="Also download featured_image (usually NOT a menu)",
    )
    args = parser.parse_args()

    if not CSV_PATH.exists():
        print(f"Missing {CSV_PATH}", file=sys.stderr)
        sys.exit(1)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    ensure_schema(cur)
    conn.commit()

    # Map kgmid -> restaurant id
    cur.execute("SELECT id, kgmid FROM restaurants")
    id_by_kgmid = {kgmid: rid for rid, kgmid in cur.fetchall()}

    stats = {
        "restaurants": 0,
        "downloaded": 0,
        "url_only": 0,
        "skipped_exists": 0,
        "failed": 0,
        "no_source": 0,
    }

    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8", newline="")))
    if args.limit:
        rows = rows[: args.limit]

    for row in rows:
        kgmid = row.get("kgmid")
        rid = id_by_kgmid.get(kgmid)
        if not rid:
            continue

        stats["restaurants"] += 1
        folder = ASSETS_DIR / f"{slugify(row.get('name', 'r'))}__{kgmid.replace('/', '_')}"
        candidates = collect_candidate_urls(row)

        # Prefer real menu assets; only take featured if nothing else (or flag set)
        preferred = []
        featured = []
        for stype, url in candidates:
            classified = classify_url(url)
            if not classified:
                continue
            kind, dl = classified
            if stype == "vision_photo":
                featured.append((stype, url, kind, dl))
            else:
                preferred.append((stype, url, kind, dl))

        queue = preferred
        if not queue and args.include_featured:
            queue = featured
        elif not queue:
            # Still register best delivery/featured as url_only pending
            if featured:
                stype, url, kind, dl = featured[0]
                if not already_have(cur, rid, url):
                    insert_asset(
                        cur,
                        restaurant_id=rid,
                        source_type=stype,
                        source_url=url,
                        asset_kind="url_only",
                        local_path=None,
                        file_sha=None,
                        nbytes=None,
                        page_count=None,
                        notes="No menu PDF/page found — featured image only (extract later / need real menu)",
                        extract_status="skipped",
                    )
                    stats["url_only"] += 1
                else:
                    stats["skipped_exists"] += 1
            else:
                stats["no_source"] += 1
            conn.commit()
            continue

        stored_one = False
        for stype, url, kind, dl in queue:
            if args.kind != "all" and kind != args.kind and not (
                args.kind == "pdf" and kind == "pdf"
            ):
                continue

            if already_have(cur, rid, url):
                stats["skipped_exists"] += 1
                stored_one = True
                continue

            if kind == "url_only" or kind == "folder":
                insert_asset(
                    cur,
                    restaurant_id=rid,
                    source_type=stype,
                    source_url=url,
                    asset_kind=kind,
                    local_path=None,
                    file_sha=None,
                    nbytes=None,
                    page_count=None,
                    notes="URL saved — download/screenshot later",
                    extract_status="pending",
                )
                stats["url_only"] += 1
                stored_one = True
                # for delivery links keep going to also store other links
                continue

            # Download pdf/html/image
            ext = { "pdf": ".pdf", "html": ".html", "image": ".jpg" }.get(kind, ".bin")
            dest = folder / f"{stype}{ext}"
            # Retry failed rows: delete failed placeholder so we can re-insert
            cur.execute(
                """
                DELETE FROM menu_assets
                WHERE restaurant_id = %s AND source_url = %s AND extract_status = 'failed'
                """,
                (rid, url),
            )

            ok, info = download(dl, dest, expect_kind=kind)
            if not ok:
                insert_asset(
                    cur,
                    restaurant_id=rid,
                    source_type=stype,
                    source_url=url,
                    asset_kind=kind,
                    local_path=None,
                    file_sha=None,
                    nbytes=None,
                    page_count=None,
                    notes=f"download_failed: {info}",
                    extract_status="failed",
                )
                stats["failed"] += 1
                time.sleep(0.3)
                continue

            # Fix extension if PDF magic
            data_head = dest.read_bytes()[:5]
            if data_head.startswith(b"%PDF") and dest.suffix != ".pdf":
                new_dest = dest.with_suffix(".pdf")
                dest.rename(new_dest)
                dest = new_dest
                kind = "pdf"

            digest = sha256_file(dest)
            pages = pdf_page_count(dest) if kind == "pdf" or dest.suffix == ".pdf" else None
            insert_asset(
                cur,
                restaurant_id=rid,
                source_type=stype,
                source_url=url,
                asset_kind="pdf" if dest.suffix == ".pdf" else kind,
                local_path=str(dest.relative_to(ROOT)),
                file_sha=digest,
                nbytes=dest.stat().st_size,
                page_count=pages,
                notes="raw asset stored — LLM extract pending",
                extract_status="pending",
            )
            stats["downloaded"] += 1
            stored_one = True
            print(f"OK  {row.get('name')[:40]:40}  {kind:5}  {dest.name}  ({dest.stat().st_size} bytes)")
            time.sleep(0.4)

            # One good PDF/HTML is enough per restaurant for now
            if kind in ("pdf", "html"):
                break

        if not stored_one:
            stats["no_source"] += 1

        conn.commit()

    print()
    print("=== Collect summary ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    cur.execute(
        """
        SELECT asset_kind, extract_status, COUNT(*)
        FROM menu_assets
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )
    print()
    print("menu_assets by kind/status:")
    for kind, status, n in cur.fetchall():
        print(f"  {kind:10} {status:10} {n}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
