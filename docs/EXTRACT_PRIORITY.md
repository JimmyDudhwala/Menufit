# MenuFit — Extract & enrichment priorities

Adajan data plane: **collect assets first → extract menu facts → enrich estimated nutrition**.

Never invent **prices**. Always label generated fields as `estimated`.

---

## Pipeline

```
menu_assets (PDFs / images / HTML)     ← collect done for Adajan
        ↓ Pass 1 — EXTRACT (from image/PDF)
menu_captures / menu_items             ← name, price?, category, veg
        ↓ Pass 2 — ENRICH (LLM / chain DB)
menu_item_analysis                     ← macros, ingredients, spice, tags
        ↓ Pass 3 — review low confidence
published → sync to server later
```

---

## Field priorities

### P0 — Must extract from menu (observed)

| Field | Rule |
|-------|------|
| `name` | As written on menu |
| `price` | Only if printed; else `null` (**never invent**) |
| `category` | From section headers when possible |
| `veg_flag` | From veg mark / name; else `unknown` |
| `source` + asset link | Provenance |

### P1 — Core MenuFit value (generate if missing)

| Field | If on menu | If missing |
|-------|------------|------------|
| `ingredients[]` | Extract | **Generate** typical for dish + Indian restaurant portion |
| `calories_kcal` | Rare | **Estimate** |
| `protein_g`, `carbs_g`, `fat_g` | Rare | **Estimate** |
| `portion_assumption` | Rare | e.g. `1 plate (restaurant)` |
| `spice_level` (0–5) | If mild/spicy noted | **Estimate** |
| `sugar_g` or `sugar_level` (0–5) | Drinks/desserts clearer | **Estimate** |
| `tags[]` | — | `fried`, `creamy`, `high_protein`, `jain_possible`, … |

### P2 — Trust / filters (generate OK)

| Field | Notes |
|-------|--------|
| `description` | Menu line or short generated blurb |
| `serving_size` | `1 plate` / `2 pcs` / `330 ml` |
| `allergens[]` | dairy, gluten, nuts, soy, egg |
| `fiber_g`, `sodium_mg` | Estimated |
| `cooking_method` | tandoor / fried / grilled / curry / steamed |
| `jain_possible` / onion-garlic | Important for Surat |
| `confidence_score` | 0.0–1.0 |
| `analysis_method` | `menu_extract` \| `llm_estimate` \| `chain_db` \| `manual` |

### P3 — Later (do not block MVP)

- Full multi-step **recipes** (optional short `recipe_summary` only if needed)
- Micronutrients, glycemic index
- Exact brand oils / commercial formulations

---

## Target JSON shape (per item)

```json
{
  "name": "Paneer Tikka",
  "price": 280,
  "category": "Starters",
  "veg_flag": true,
  "description": null,
  "ingredients": [
    { "name": "paneer", "amount": null, "source": "estimated" },
    { "name": "yogurt marinade", "amount": null, "source": "estimated" }
  ],
  "nutrition": {
    "calories_kcal": 320,
    "protein_g": 18,
    "carbs_g": 12,
    "fat_g": 22,
    "fiber_g": 2,
    "sugar_g": 4,
    "sodium_mg": 680,
    "source": "llm_estimate",
    "portion": "1 plate (restaurant)"
  },
  "attributes": {
    "spice_level": 3,
    "sugar_level": 1,
    "oiliness": 3,
    "cooking_method": "tandoor",
    "jain_possible": false,
    "allergens": ["dairy"]
  },
  "tags": ["high_protein", "grilled", "north_indian"],
  "confidence": 0.72,
  "needs_review": false
}
```

---

## Honesty rules (product trust)

1. **Price** — menu / official chain only.
2. **Macros, ingredients, recipe, spice, sugar** — estimated unless printed; UI shows **Estimated**.
3. Enrich items even when `price` is null (still useful for “what’s in this dish”).
4. Chains later: prefer `chain_db` nutrition when available (higher confidence).

---

## Agreed MVP defaults

| Topic | Decision |
|-------|----------|
| Recipe | Defer full recipes; optional short summary later |
| Spice / sugar | `spice_level` 0–5 + `sugar_g` (and optional `sugar_level` 0–5) |
| Missing price | Still run enrichment |
| Swiggy scrape | Not primary; local optional filler later (blocked in headless today) |

---

## DB mapping (local Postgres)

| Table | Role |
|-------|------|
| `restaurants` | Adajan master list |
| `menu_assets` | Raw PDFs / images / HTML (`extract_status=pending`) |
| `menu_captures` | Raw extract JSON drafts |
| `menu_categories` / `menu_items` | Structured P0 |
| `menu_item_analysis` | P1/P2 nutrition + attributes + tags |

See `sql/001_schema.sql` and `sql/002_menu_assets.sql`.
