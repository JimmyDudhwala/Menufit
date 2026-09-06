#!/usr/bin/env python3
"""
Pass 1 (P0): extract observed menu facts from stored assets.

Writes menu_captures + menu_categories + menu_items.
Does not invent prices. Does not estimate nutrition (that's Pass 2).

Usage:
  source .venv/bin/activate
  # .env must set ANTHROPIC_API_KEY
  python3 scripts/extract-menus-llm.py --dry-run
  python3 scripts/extract-menus-llm.py --limit 3
  python3 scripts/extract-menus-llm.py --name "Simply Madras"
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = "postgresql://menufit:menufit@localhost:5433/menufit"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_IMAGE_DIM = 1568
JPEG_QUALITY = 75
MAX_HTML_CHARS = 80_000
MAX_IMAGES_PER_CALL = 12


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DB)


def apply_sql(cur, rel: str) -> None:
    cur.execute((ROOT / rel).read_text(encoding="utf-8"))


def html_to_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def downscale_rgb(img, max_dim: int = MAX_IMAGE_DIM):
    w, h = img.size
    longest = max(w, h)
    if longest <= max_dim:
        return img
    scale = max_dim / longest
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))))


def rgb_to_jpeg_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def prepare_image(path: Path) -> bytes | None:
    from PIL import Image

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            if min(img.size) < 40:
                return None
            return rgb_to_jpeg_bytes(downscale_rgb(img))
    except Exception:
        return None


def render_pdf_pages(path: Path, max_pages: int) -> list[bytes]:
    import pymupdf
    from PIL import Image

    doc = pymupdf.open(path)
    pages: list[bytes] = []
    matrix = pymupdf.Matrix(1.6, 1.6)
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        pages.append(rgb_to_jpeg_bytes(downscale_rgb(img)))
    return pages


def build_parts(assets: list[dict], max_pages: int) -> tuple[list[dict], list[str]]:
    """Return (Anthropic content parts after the prompt, human labels)."""
    parts: list[dict] = []
    labels: list[str] = []

    for asset in assets:
        path = ROOT / asset["local_path"]
        if not path.exists():
            labels.append(f"missing {asset['local_path']}")
            continue
        kind = asset["asset_kind"]

        if kind == "image":
            data = prepare_image(path)
            if not data:
                labels.append(f"unreadable image {path.name}")
                continue
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
            labels.append(f"image {path.name} ({len(data)} bytes)")

        elif kind == "pdf":
            pages = render_pdf_pages(path, max_pages=max_pages)
            if not pages:
                labels.append(f"empty pdf {path.name}")
                continue
            for i, data in enumerate(pages, 1):
                parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
                labels.append(f"pdf {path.name} p{i}/{len(pages)} ({len(data)} bytes)")

        elif kind == "html":
            raw = path.read_text(encoding="utf-8", errors="replace")
            text = html_to_text(raw)[:MAX_HTML_CHARS]
            if len(text) < 80:
                labels.append(f"html too empty {path.name}")
                continue
            parts.append(
                {
                    "type": "text",
                    "text": f"HTML menu page ({path.name}) as visible text:\n{text}",
                }
            )
            labels.append(f"html {path.name} ({len(text)} chars)")

    return parts, labels


P0_SYSTEM = """You extract restaurant menu items that are actually printed or shown.

P0 fields only:
- name: dish name as written (keep original language/spelling)
- price: number in INR if a price is printed; otherwise null. NEVER invent or guess a price.
- category: section header on the menu when present; else null
- veg_flag: true/false from veg/non-veg marks or an explicit veg/non-veg section; else null
- description: only if a short line is printed under the name; else null

Rules:
- Skip headers, timings, GST/tax notes, allergen lists, addresses, social links, packing charges.
- "MRP" / "market price" → price null.
- Size variants with labeled prices (Plain/Butter 70/75) → separate items with the variant in the name.
- Unlabeled price pairs → price null and put the printed pair in description.
- If the file is not a menu (food photo, ambience, logo), set looks_like_menu=false and items=[].
- Deduplicate identical name+price+category.
- Do not add ingredients, calories, spice, or tags."""

TOOLS = [
    {
        "name": "save_menu_items",
        "description": "Store observed P0 menu items from the provided menu assets.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "looks_like_menu": {"type": "boolean"},
                "notes": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "price": {"type": ["number", "null"]},
                            "category": {"type": ["string", "null"]},
                            "veg_flag": {"type": ["boolean", "null"]},
                            "description": {"type": ["string", "null"]},
                        },
                        "required": ["name", "price", "category", "veg_flag"],
                    },
                },
            },
            "required": ["looks_like_menu", "items"],
        },
    }
]


def extract_with_claude(
    *,
    client,
    model: str,
    restaurant_name: str,
    parts: list[dict],
) -> dict[str, Any]:
    user_content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Restaurant: {restaurant_name}\n"
                "Extract every dish you can read from the following menu assets. "
                "Call save_menu_items."
            ),
        },
        *parts,
    ]
    msg = client.messages.create(
        model=model,
        max_tokens=12000,
        system=P0_SYSTEM,
        tools=TOOLS,
        tool_choice={"type": "tool", "name": "save_menu_items"},
        messages=[{"role": "user", "content": user_content}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == "save_menu_items":
            return {"result": block.input, "stop_reason": msg.stop_reason, "usage": {
                "input_tokens": msg.usage.input_tokens,
                "output_tokens": msg.usage.output_tokens,
            }}
    raise RuntimeError(f"No tool payload from model (stop={msg.stop_reason})")


def chunk(seq: list, size: int) -> list[list]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def merge_extracts(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict] = []
    seen: set[tuple] = set()
    notes: list[str] = []
    looks = False
    for payload in payloads:
        if payload.get("looks_like_menu"):
            looks = True
        if payload.get("notes"):
            notes.append(str(payload["notes"]))
        for item in payload.get("items") or []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            key = (
                name.lower(),
                item.get("price"),
                (item.get("category") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return {
        "looks_like_menu": looks or bool(items),
        "notes": " | ".join(notes),
        "items": items,
    }


def clean_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price > 100_000:
        return None
    return round(price, 2)


def persist(
    cur,
    *,
    restaurant_id: int,
    source_type: str,
    source_url: str | None,
    file_path: str | None,
    asset_ids: list[int],
    extract: dict[str, Any],
    model: str,
    usage: dict,
    labels: list[str],
) -> tuple[int, int]:
    items = extract.get("items") or []
    looks = bool(extract.get("looks_like_menu"))
    capture_status = "captured" if items else "needs_review"
    notes = extract.get("notes") or None
    if not looks and not items:
        notes = (notes + " | " if notes else "") + "not_a_menu"

    payload = {
        "extract_pass": "p0",
        "model": model,
        "asset_ids": asset_ids,
        "asset_labels": labels,
        "usage": usage,
        "looks_like_menu": looks,
        "items": items,
    }
    cur.execute(
        """
        INSERT INTO menu_captures (
          restaurant_id, source_type, source_url, capture_format,
          raw_payload, file_path, capture_status, notes, updated_at
        ) VALUES (%s, %s, %s, 'json', %s, %s, %s, %s, NOW())
        RETURNING id
        """,
        (
            restaurant_id,
            source_type,
            source_url,
            Json(payload),
            file_path,
            capture_status,
            notes,
        ),
    )
    capture_id = cur.fetchone()[0]

    inserted = 0
    cat_ids: dict[str, int] = {}
    for sort_order, item in enumerate(items):
        name = (item.get("name") or "").strip()
        if not name:
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
                name,
                clean_price(item.get("price")),
                veg,
                desc,
            ),
        )
        inserted += 1

    if items:
        asset_status = "extracted"
        asset_note = f"p0 extracted capture_id={capture_id} items={inserted}"
    elif looks:
        asset_status = "extracted"
        asset_note = f"p0 menu detected but 0 items capture_id={capture_id}"
    else:
        asset_status = "skipped"
        asset_note = f"p0 not a menu capture_id={capture_id}"

    cur.execute(
        """
        UPDATE menu_assets
        SET extract_status = %s, notes = %s, updated_at = NOW()
        WHERE id = ANY(%s)
        """,
        (asset_status, asset_note, asset_ids),
    )
    return capture_id, inserted


def already_extracted(cur, restaurant_id: int) -> bool:
    cur.execute(
        "SELECT 1 FROM menu_items WHERE restaurant_id = %s LIMIT 1",
        (restaurant_id,),
    )
    return cur.fetchone() is not None


def reset_restaurant(cur, restaurant_id: int, asset_ids: list[int]) -> None:
    cur.execute("DELETE FROM menu_items WHERE restaurant_id = %s", (restaurant_id,))
    cur.execute("DELETE FROM menu_categories WHERE restaurant_id = %s", (restaurant_id,))
    cur.execute(
        """
        DELETE FROM menu_captures
        WHERE restaurant_id = %s
          AND raw_payload->>'extract_pass' = 'p0'
        """,
        (restaurant_id,),
    )
    cur.execute(
        """
        UPDATE menu_assets
        SET extract_status = 'pending', updated_at = NOW()
        WHERE id = ANY(%s)
        """,
        (asset_ids,),
    )


def load_queue(cur, *, name: str | None, kind: str, force: bool) -> list[dict]:
    params: list[Any] = []
    filters = [
        "a.local_path IS NOT NULL",
        "a.asset_kind IN ('pdf', 'image', 'html')",
    ]
    if not force:
        filters.append("a.extract_status = 'pending'")
    else:
        filters.append("a.extract_status IN ('pending', 'extracted', 'skipped', 'failed')")
    if kind != "all":
        filters.append("a.asset_kind = %s")
        params.append(kind)
    if name:
        filters.append("r.name ILIKE %s")
        params.append(f"%{name}%")

    cur.execute(
        f"""
        SELECT
          r.id, r.name, r.kgmid, r.user_ratings_total,
          a.id, a.source_type, a.source_url, a.asset_kind,
          a.local_path, a.page_count, a.bytes
        FROM restaurants r
        JOIN menu_assets a ON a.restaurant_id = r.id
        WHERE {" AND ".join(filters)}
        ORDER BY r.user_ratings_total DESC NULLS LAST, r.id, a.id
        """,
        params,
    )
    grouped: dict[int, dict] = {}
    for row in cur.fetchall():
        (
            rid,
            rname,
            kgmid,
            reviews,
            aid,
            source_type,
            source_url,
            asset_kind,
            local_path,
            page_count,
            nbytes,
        ) = row
        if rid not in grouped:
            grouped[rid] = {
                "restaurant_id": rid,
                "name": rname,
                "kgmid": kgmid,
                "reviews": reviews,
                "assets": [],
            }
        grouped[rid]["assets"].append(
            {
                "id": aid,
                "source_type": source_type,
                "source_url": source_url,
                "asset_kind": asset_kind,
                "local_path": local_path,
                "page_count": page_count,
                "bytes": nbytes,
            }
        )
    return list(grouped.values())


def call_with_retries(fn, *, retries: int = 4):
    delay = 2.0
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc).lower()
            retryable = any(s in msg for s in ("429", "rate", "overloaded", "timeout", "529"))
            if not retryable or attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise last_exc or RuntimeError("retry failed")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="P0 LLM extract from stored menu assets")
    parser.add_argument("--limit", type=int, default=0, help="Max restaurants")
    parser.add_argument("--name", help="Substring filter on restaurant name")
    parser.add_argument("--kind", choices=["all", "image", "pdf", "html"], default="all")
    parser.add_argument("--max-pages", type=int, default=16, help="Max PDF pages per file")
    parser.add_argument("--sleep", type=float, default=0.8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-extract even if items exist")
    args = parser.parse_args()

    conn = psycopg2.connect(database_url())
    cur = conn.cursor()
    apply_sql(cur, "sql/002_menu_assets.sql")
    apply_sql(cur, "sql/003_analysis_fields.sql")
    conn.commit()

    queue = load_queue(cur, name=args.name, kind=args.kind, force=args.force)
    if not args.force:
        queue = [row for row in queue if not already_extracted(cur, row["restaurant_id"])]
    if args.limit:
        queue = queue[: args.limit]

    print(f"Restaurants queued: {len(queue)}")
    for row in queue:
        kinds = defaultdict(int)
        for asset in row["assets"]:
            kinds[asset["asset_kind"]] += 1
        summary = ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        print(f"  {row['name'][:48]:48}  {summary}")

    if args.dry_run:
        cur.close()
        conn.close()
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ANTHROPIC_API_KEY is empty. Add it to .env", file=sys.stderr)
        sys.exit(1)

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    stats = {
        "restaurants": 0,
        "items": 0,
        "not_menu": 0,
        "failed": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }

    for i, row in enumerate(queue, 1):
        rid = row["restaurant_id"]
        assets = row["assets"]
        asset_ids = [a["id"] for a in assets]
        print(f"\n[{i}/{len(queue)}] {row['name']}")
        try:
            parts, labels = build_parts(assets, max_pages=args.max_pages)
            print("  assets: " + "; ".join(labels) if labels else "  assets: none usable")
            if not parts:
                cur.execute(
                    """
                    UPDATE menu_assets
                    SET extract_status = 'failed', notes = %s, updated_at = NOW()
                    WHERE id = ANY(%s)
                    """,
                    ("p0 no usable file content", asset_ids),
                )
                conn.commit()
                stats["failed"] += 1
                continue

            if args.force:
                reset_restaurant(cur, rid, asset_ids)

            payloads = []
            usage_sum = {"input_tokens": 0, "output_tokens": 0}
            # Keep text parts with every image chunk so HTML context is not dropped.
            text_parts = [p for p in parts if p.get("type") == "text"]
            image_parts = [p for p in parts if p.get("type") == "image"]
            batches = chunk(image_parts, MAX_IMAGES_PER_CALL) or [[]]
            for batch in batches:
                batch_parts = text_parts + batch
                if not batch_parts:
                    continue
                out = call_with_retries(
                    lambda bp=batch_parts: extract_with_claude(
                        client=client,
                        model=model,
                        restaurant_name=row["name"],
                        parts=bp,
                    )
                )
                payloads.append(out["result"])
                usage_sum["input_tokens"] += out["usage"]["input_tokens"]
                usage_sum["output_tokens"] += out["usage"]["output_tokens"]

            extract = merge_extracts(payloads)
            source_type = assets[0]["source_type"]
            source_url = next((a["source_url"] for a in assets if a.get("source_url")), None)
            file_path = assets[0]["local_path"]
            capture_id, n_items = persist(
                cur,
                restaurant_id=rid,
                source_type=source_type,
                source_url=source_url,
                file_path=file_path,
                asset_ids=asset_ids,
                extract=extract,
                model=model,
                usage=usage_sum,
                labels=labels,
            )
            conn.commit()
            stats["restaurants"] += 1
            stats["items"] += n_items
            stats["input_tokens"] += usage_sum["input_tokens"]
            stats["output_tokens"] += usage_sum["output_tokens"]
            if n_items == 0:
                stats["not_menu"] += 1
            print(
                f"  capture={capture_id} items={n_items} "
                f"looks_like_menu={extract.get('looks_like_menu')} "
                f"tokens={usage_sum['input_tokens']}+{usage_sum['output_tokens']}"
            )
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            stats["failed"] += 1
            print(f"  FAILED: {exc}")
            try:
                cur.execute(
                    """
                    UPDATE menu_assets
                    SET notes = %s, updated_at = NOW()
                    WHERE id = ANY(%s)
                    """,
                    (f"p0 error: {exc}"[:500], asset_ids),
                )
                conn.commit()
            except Exception:
                conn.rollback()
        time.sleep(args.sleep)

    print("\n=== P0 extract summary ===")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    cur.execute("SELECT COUNT(*) FROM menu_items")
    print(f"  menu_items total: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(DISTINCT restaurant_id) FROM menu_items")
    print(f"  restaurants with items: {cur.fetchone()[0]}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
