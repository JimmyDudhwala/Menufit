#!/usr/bin/env python3
"""Show raw menu asset collection progress (before LLM extract)."""

from __future__ import annotations

import os

import psycopg2

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM restaurants")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT restaurant_id) FROM menu_assets")
    with_asset = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(DISTINCT restaurant_id) FROM menu_assets
        WHERE local_path IS NOT NULL AND extract_status = 'pending'
        """
    )
    files_pending_extract = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(DISTINCT restaurant_id) FROM menu_assets
        WHERE extract_status = 'extracted'
        """
    )
    extracted = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*) FROM restaurants r
        WHERE NOT EXISTS (SELECT 1 FROM menu_assets a WHERE a.restaurant_id = r.id)
        """
    )
    missing = cur.fetchone()[0]

    print(f"Restaurants:              {total}")
    print(f"With any menu_asset row:  {with_asset}")
    print(f"Local files ready for LLM:{files_pending_extract}")
    print(f"Already LLM-extracted:    {extracted}")
    print(f"Still no asset at all:    {missing}")
    print()

    cur.execute(
        """
        SELECT asset_kind, COUNT(*), COUNT(local_path) AS with_file
        FROM menu_assets
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    print("By asset_kind:")
    for kind, n, with_file in cur.fetchall():
        print(f"  {kind:10} rows={n:4}  files_on_disk={with_file}")

    cur.execute(
        """
        SELECT r.name, a.asset_kind, a.local_path, a.bytes, a.page_count
        FROM menu_assets a
        JOIN restaurants r ON r.id = a.restaurant_id
        WHERE a.local_path IS NOT NULL
        ORDER BY a.collected_at DESC
        LIMIT 10
        """
    )
    print()
    print("Latest stored files:")
    for name, kind, path, nbytes, pages in cur.fetchall():
        print(f"  [{kind}] {name}")
        print(f"         {path}  ({nbytes} bytes, pages={pages})")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
