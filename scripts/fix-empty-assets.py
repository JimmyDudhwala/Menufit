#!/usr/bin/env python3
"""Mark near-empty / JS-shell HTML assets as failed, then optionally re-fetch with Playwright."""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)

SHELL_MARKERS = (
    "you need to enable javascript",
    "enable javascript to run this app",
    "noscript",
)


def plain_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_empty_asset(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, "file_missing"
    size = path.stat().st_size
    if size < 800:
        return True, f"too_small_{size}b"
    if path.suffix.lower() in {".html", ".htm"}:
        html = path.read_text(encoding="utf-8", errors="replace")
        plain = plain_text(html)
        low = plain.lower()
        if any(m in low for m in SHELL_MARKERS) and len(plain) < 300:
            return True, "js_shell_empty"
        if len(plain) < 120:
            return True, f"almost_no_text_{len(plain)}"
    return False, "ok"


def mark_empty(cur) -> list[dict]:
    cur.execute(
        """
        SELECT a.id, r.name, a.local_path, a.source_url, a.asset_kind
        FROM menu_assets a
        JOIN restaurants r ON r.id = a.restaurant_id
        WHERE a.local_path IS NOT NULL AND a.extract_status = 'pending'
        """
    )
    emptied = []
    for asset_id, name, local_path, source_url, kind in cur.fetchall():
        path = ROOT / local_path
        bad, reason = is_empty_asset(path)
        if not bad:
            continue
        cur.execute(
            """
            UPDATE menu_assets
            SET extract_status = 'failed',
                notes = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (f"empty_asset: {reason}", asset_id),
        )
        emptied.append(
            {
                "id": asset_id,
                "name": name,
                "path": local_path,
                "url": source_url,
                "kind": kind,
                "reason": reason,
            }
        )
        print(f"MARK empty [{reason}] {name}")
    return emptied


def refetch_with_playwright(emptied: list[dict], cur) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed — skip browser refetch")
        return 0

    fixed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for item in emptied:
            if not item["url"] or item["kind"] != "html":
                continue
            dest = ROOT / item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                page.goto(item["url"], wait_until="networkidle", timeout=45000)
                time.sleep(2)
                html = page.content()
                dest.write_text(html, encoding="utf-8")
                plain = plain_text(html)
                if len(plain) < 200 or any(m in plain.lower() for m in SHELL_MARKERS):
                    # also save screenshot for later LLM vision
                    shot = dest.with_suffix(".png")
                    page.screenshot(path=str(shot), full_page=True)
                    cur.execute(
                        """
                        UPDATE menu_assets
                        SET notes = %s, bytes = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            f"playwright_refetch_still_thin text={len(plain)}; screenshot={shot.relative_to(ROOT)}",
                            dest.stat().st_size,
                            item["id"],
                        ),
                    )
                    print(f"THIN after browser {item['name']} (screenshot saved)")
                else:
                    cur.execute(
                        """
                        UPDATE menu_assets
                        SET extract_status = 'pending',
                            bytes = %s,
                            notes = 'playwright_refetch_ok — LLM extract pending',
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (dest.stat().st_size, item["id"]),
                    )
                    fixed += 1
                    print(f"FIXED {item['name']} text={len(plain)} bytes={dest.stat().st_size}")
            except Exception as exc:  # noqa: BLE001
                cur.execute(
                    """
                    UPDATE menu_assets
                    SET notes = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (f"playwright_failed: {exc}", item["id"]),
                )
                print(f"FAIL {item['name']}: {exc}")
        browser.close()
    return fixed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refetch", action="store_true", help="Re-open empty HTML with Playwright")
    args = parser.parse_args()

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    emptied = mark_empty(cur)
    conn.commit()
    print(f"\nMarked empty: {len(emptied)}")

    if args.refetch and emptied:
        fixed = refetch_with_playwright(emptied, cur)
        conn.commit()
        print(f"Fixed via Playwright: {fixed}")

    cur.execute(
        """
        SELECT extract_status, COUNT(*)
        FROM menu_assets
        WHERE local_path IS NOT NULL
        GROUP BY 1
        """
    )
    print("\nFiles by extract_status:")
    for status, n in cur.fetchall():
        print(f"  {status}: {n}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
