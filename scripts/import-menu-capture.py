#!/usr/bin/env python3
"""Import a raw menu capture JSON file into menu_captures."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://menufit:menufit@localhost:5433/menufit",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Store a raw menu capture in local DB")
    parser.add_argument("file", type=Path, help="Path to capture JSON file")
    parser.add_argument(
        "--status",
        default="captured",
        choices=["captured", "needs_review", "approved", "rejected"],
    )
    args = parser.parse_args()

    payload = json.loads(args.file.read_text(encoding="utf-8"))
    kgmid = payload.get("kgmid")
    if not kgmid:
        print("Capture JSON must include 'kgmid'", file=sys.stderr)
        sys.exit(1)

    source_type = payload.get("source_type", "manual")
    source_url = payload.get("source_url")
    capture_format = payload.get("capture_format", "json")

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM restaurants WHERE kgmid = %s", (kgmid,))
        row = cur.fetchone()
        if not row:
            print(f"No restaurant with kgmid {kgmid}", file=sys.stderr)
            sys.exit(1)
        restaurant_id = row[0]

        cur.execute(
            """
            INSERT INTO menu_captures (
              restaurant_id, source_type, source_url, capture_format,
              raw_payload, file_path, capture_status, notes, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
            """,
            (
                restaurant_id,
                source_type,
                source_url,
                capture_format,
                Json(payload),
                str(args.file.resolve()),
                args.status,
                payload.get("notes"),
            ),
        )
        capture_id = cur.fetchone()[0]
        conn.commit()
        print(f"Stored menu_capture id={capture_id} for {kgmid}")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
