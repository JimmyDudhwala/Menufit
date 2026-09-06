#!/usr/bin/env python3
"""Show menu capture progress for Adajan restaurants."""

from __future__ import annotations

import os

import psycopg2

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)

# Lower = try this source first when capturing
SOURCE_RANK = {
    "petpooja": 1,
    "website_menu": 2,
    "swiggy": 3,
    "zomato": 4,
    "website": 5,
    "vision_photo": 6,
    "manual": 7,
}


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM restaurants")
    total = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(DISTINCT restaurant_id)
        FROM menu_captures
        WHERE capture_status IN ('captured', 'needs_review', 'approved')
        """
    )
    with_capture = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(DISTINCT restaurant_id)
        FROM menu_captures
        WHERE capture_status = 'approved'
        """
    )
    approved = cur.fetchone()[0]

    # One best source per restaurant (not every URL)
    cur.execute(
        """
        WITH best_source AS (
          SELECT DISTINCT ON (ms.restaurant_id)
            ms.restaurant_id,
            ms.source_type,
            ms.source_url
          FROM menu_sources ms
          ORDER BY
            ms.restaurant_id,
            CASE ms.source_type
              WHEN 'petpooja' THEN 1
              WHEN 'website_menu' THEN 2
              WHEN 'swiggy' THEN 3
              WHEN 'zomato' THEN 4
              WHEN 'website' THEN 5
              WHEN 'vision_photo' THEN 6
              ELSE 7
            END,
            ms.priority
        )
        SELECT r.name, r.kgmid, bs.source_type, bs.source_url
        FROM restaurants r
        JOIN best_source bs ON bs.restaurant_id = r.id
        LEFT JOIN menu_captures mc ON mc.restaurant_id = r.id
          AND mc.capture_status IN ('captured', 'needs_review', 'approved')
        WHERE mc.id IS NULL
        ORDER BY r.user_ratings_total DESC NULLS LAST
        LIMIT 15
        """
    )
    pending = cur.fetchall()

    print(f"Restaurants:        {total}")
    print(f"With menu capture:  {with_capture} ({with_capture}/{total})")
    print(f"Approved captures:  {approved}")
    print(f"Still need menu:    {total - with_capture}")
    print()
    print("Next 15 to capture (best source per restaurant, by review count):")
    print()
    for name, kgmid, stype, url in pending:
        print(f"  {name}")
        print(f"    kgmid:  {kgmid}")
        print(f"    use:    [{stype}]")
        print(f"    open:   {url}")
        print()

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
