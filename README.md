# MenuFit — Local data plane (Adajan, Surat)

Laptop owns **data collection + enrichment**. Server later serves users/subscriptions only (no scraping).

## Pipeline

| Step | What | Status |
|------|------|--------|
| **1a. Collect** | Store menu PDFs / images / HTML | Done for Adajan (~260 restaurants) |
| **1b. Extract** | LLM → name, price, category, veg | Next (needs API key) |
| **2. Enrich** | Ingredients, macros, spice, sugar, tags | After extract |
| **3. Sync** | Push published rows to server | Later |

Details: **[docs/EXTRACT_PRIORITY.md](docs/EXTRACT_PRIORITY.md)**

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # only if re-running browser collect
cp .env.example .env
docker compose up -d
python3 scripts/import-adajan.py
```

Postgres: `localhost:5433` (user/pass/db: `menufit`).

---

## Repo vs local-only data

| Path | In git? | Why |
|------|---------|-----|
| `data/adajan-restaurants.csv` | Yes | Filtered Adajan list |
| `data/adajan-menu-jobs.json` | Yes | Source job queue |
| `sql/`, `scripts/`, `docs/` | Yes | Code + docs |
| `data/menu-assets/` | **No** | Large binary menus (~250MB) — keep on disk |
| `restaurant-in-surat-gujarat-india.csv` | **No** | ~229MB full scrape — local only |
| `.venv/`, `.env` | **No** | Local |

After clone: restore assets from your machine/backup, or re-run collect scripts.

---

## Collect again (if needed)

```bash
python3 scripts/collect-menu-assets.py
python3 scripts/collect-gmaps-menu-photos.py --only-missing
python3 scripts/collect-fallback-photos.py
python3 scripts/fix-empty-assets.py --refetch
python3 scripts/asset-status.py
```

---

## Extract (not wired yet)

```bash
# .env → ANTHROPIC_API_KEY=...
python3 scripts/extract-menus-llm.py   # TODO
```

Pass 1 = P0 fields from image/PDF. Pass 2 = P1/P2 enrichment. See extract doc.

---

## Notes

- **Swiggy** headless screenshots are blocked; URLs kept in `menu_assets`. Prefer GMaps menu photos + PDFs + LLM.
- Do not invent prices. Mark all generated nutrition as estimated.
# Menufit
