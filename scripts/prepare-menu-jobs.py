#!/usr/bin/env python3
"""Build menu extraction job list from Adajan restaurant CSV."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "adajan-restaurants.csv"
OUTPUT = ROOT / "data" / "adajan-menu-jobs.json"


def parse_json_field(raw: str) -> list[dict]:
    if not raw or raw.strip() in ("", "[]"):
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        if raw.startswith("http"):
            return [{"link": raw, "source": "raw"}]
        return []


def menu_source(row: dict[str, str]) -> dict | None:
  menu_items = parse_json_field(row.get("menu", ""))
  if menu_items:
      link = menu_items[0].get("link") if isinstance(menu_items[0], dict) else None
      if link:
          src = menu_items[0].get("source", "")
          if "petpooja" in link or "petpooja" in src:
              return {"type": "petpooja", "url": link}
          return {"type": "website_menu", "url": link}

  for item in parse_json_field(row.get("order_online_links", "")):
      link = item.get("link", "")
      if "zomato.com" in link:
          return {"type": "zomato", "url": link}
      if "swiggy.com" in link:
          return {"type": "swiggy", "url": link}

  if row.get("website"):
      return {"type": "website", "url": row["website"]}

  if row.get("featured_image"):
      return {"type": "vision_photo", "url": row["featured_image"]}

  return None


def main() -> None:
    if not INPUT.exists():
        print(f"Run filter-adajan.py first. Missing: {INPUT}", file=sys.stderr)
        sys.exit(1)

    jobs = []
    counts: dict[str, int] = {}

    with INPUT.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            source = menu_source(row)
            if not source:
                continue
            source_type = source["type"]
            counts[source_type] = counts.get(source_type, 0) + 1
            jobs.append(
                {
                    "kgmid": row.get("kgmid"),
                    "place_id": row.get("place_id"),
                    "name": row.get("name"),
                    "rating": row.get("rating"),
                    "reviews": row.get("reviews"),
                    "source_type": source_type,
                    "source_url": source["url"],
                }
            )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {len(jobs)} menu jobs → {OUTPUT}")
    for kind, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {kind}: {n}")


if __name__ == "__main__":
    main()
