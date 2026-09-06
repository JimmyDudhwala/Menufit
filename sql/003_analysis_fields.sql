-- Align analysis table with docs/EXTRACT_PRIORITY.md (P1/P2 fields)

ALTER TABLE menu_item_analysis
  ADD COLUMN IF NOT EXISTS fiber_g NUMERIC(8, 2),
  ADD COLUMN IF NOT EXISTS sugar_g NUMERIC(8, 2),
  ADD COLUMN IF NOT EXISTS sodium_mg NUMERIC(8, 2),
  ADD COLUMN IF NOT EXISTS spice_level SMALLINT
    CHECK (spice_level IS NULL OR (spice_level >= 0 AND spice_level <= 5)),
  ADD COLUMN IF NOT EXISTS sugar_level SMALLINT
    CHECK (sugar_level IS NULL OR (sugar_level >= 0 AND sugar_level <= 5)),
  ADD COLUMN IF NOT EXISTS oiliness SMALLINT
    CHECK (oiliness IS NULL OR (oiliness >= 0 AND oiliness <= 5)),
  ADD COLUMN IF NOT EXISTS cooking_method TEXT,
  ADD COLUMN IF NOT EXISTS jain_possible BOOLEAN,
  ADD COLUMN IF NOT EXISTS allergens TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS ingredients JSONB NOT NULL DEFAULT '[]',
  ADD COLUMN IF NOT EXISTS portion_assumption TEXT,
  ADD COLUMN IF NOT EXISTS recipe_summary TEXT,
  ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT FALSE;

-- analysis_method already exists; ensure allowed values stay documented in EXTRACT_PRIORITY.md
