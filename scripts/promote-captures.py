#!/usr/bin/env python3
"""Turn menu_captures.items JSON into menu_categories + menu_items (no LLM)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def clean_price(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price > 100_000:
        return None
    return round(price, 2)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Promote capture JSON → structured items")
    parser.add_argument("--force", action="store_true", help="Replace existing draft items")
    args = parser.parse_args()

    conn = psycopg2.connect(
        os.environ.get("DATABASE_URL", "postgresql://menufit:menufit@localhost:5433/menufit")
    )
    cur = conn.cursor()
    cur.execute((ROOT / "sql" / "003_analysis_fields.sql").read_text(encoding="utf-8"))

    cur.execute(
        """
        SELECT mc.id, mc.restaurant_id, r.name, mc.raw_payload
        FROM menu_captures mc
        JOIN restaurants r ON r.id = mc.restaurant_id
        WHERE mc.capture_status IN ('captured', 'needs_review', 'approved')
          AND jsonb_typeof(mc.raw_payload->'items') = 'array'
          AND jsonb_array_length(mc.raw_payload->'items') > 0
        ORDER BY mc.id
        """
    )
    rows = cur.fetchall()
    print(f"Captures with items: {len(rows)}")

    restaurants = 0
    items_total = 0
    skipped = 0

    for capture_id, restaurant_id, name, payload in rows:
        cur.execute(
            "SELECT 1 FROM menu_items WHERE restaurant_id = %s LIMIT 1",
            (restaurant_id,),
        )
        has_items = cur.fetchone() is not None
        if has_items and not args.force:
            print(f"  skip {name} (already has items)")
            skipped += 1
            continue
        if has_items and args.force:
            cur.execute("DELETE FROM menu_items WHERE restaurant_id = %s", (restaurant_id,))
            cur.execute("DELETE FROM menu_categories WHERE restaurant_id = %s", (restaurant_id,))

        cat_ids: dict[str, int] = {}
        inserted = 0
        for sort_order, item in enumerate(payload.get("items") or []):
            item_name = (item.get("name") or "").strip()
            if not item_name:
                continue
            category = (item.get("category") or "").strip() or "Uncategorized"
            if category not in cat_ids:
                cur.execute(
                    """
                    INSERT INTO menu_categories (restaurant_id, name, sort_order)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (restaurant_id, name) DO UPDATE
                      SET sort_order = EXCLUDED.sort_order
                    RETURNING id
                    """,
                    (restaurant_id, category, sort_order),
                )
                cat_ids[category] = cur.fetchone()[0]
            veg = item.get("veg_flag")
            if veg is not True and veg is not False:
                veg = None
            desc = item.get("description")
            if isinstance(desc, str):
                desc = desc.strip() or None
            else:
                desc = None
            cur.execute(
                """
                INSERT INTO menu_items (
                  restaurant_id, category_id, capture_id, name, price,
                  veg_flag, description, extract_status, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', NOW())
                """,
                (
                    restaurant_id,
                    cat_ids[category],
                    capture_id,
                    item_name,
                    clean_price(item.get("price")),
                    veg,
                    desc,
                ),
            )
            inserted += 1

        cur.execute(
            """
            UPDATE menu_assets
            SET extract_status = 'extracted',
                notes = %s,
                updated_at = NOW()
            WHERE restaurant_id = %s
              AND local_path IS NOT NULL
              AND extract_status = 'pending'
            """,
            (f"p0 promoted from capture_id={capture_id} items={inserted}", restaurant_id),
        )
        restaurants += 1
        items_total += inserted
        print(f"  {name}: {inserted} items from capture {capture_id}")

    conn.commit()
    print()
    print(f"Promoted {items_total} items across {restaurants} restaurants ({skipped} skipped)")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
