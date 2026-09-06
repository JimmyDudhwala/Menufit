-- MenuFit local data plane (laptop)
-- Phase 1: store raw menu captures
-- Phase 2: extract → menu_items
-- Phase 3: analyze → menu_item_analysis

CREATE TABLE IF NOT EXISTS restaurants (
  id                 BIGSERIAL PRIMARY KEY,
  kgmid              TEXT NOT NULL UNIQUE,
  place_id           TEXT,
  name               TEXT NOT NULL,
  area               TEXT NOT NULL DEFAULT 'Adajan',
  main_category      TEXT,
  categories         JSONB,
  rating             NUMERIC(2, 1),
  user_ratings_total INTEGER,
  phone              TEXT,
  website            TEXT,
  address            TEXT,
  detailed_address   JSONB,
  lat                DOUBLE PRECISION,
  lng                DOUBLE PRECISION,
  maps_link          TEXT,
  featured_image     TEXT,
  price_range        TEXT,
  hours              JSONB,
  publish_status     TEXT NOT NULL DEFAULT 'draft'
                     CHECK (publish_status IN ('draft', 'published')),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS restaurants_area_idx ON restaurants (area);
CREATE INDEX IF NOT EXISTS restaurants_publish_idx ON restaurants (publish_status);

-- All candidate URLs per restaurant (priority order for capture attempts)
CREATE TABLE IF NOT EXISTS menu_sources (
  id            BIGSERIAL PRIMARY KEY,
  restaurant_id BIGINT NOT NULL REFERENCES restaurants (id) ON DELETE CASCADE,
  source_type   TEXT NOT NULL
                CHECK (source_type IN (
                  'petpooja', 'swiggy', 'zomato', 'website_menu',
                  'website', 'vision_photo', 'manual'
                )),
  source_url    TEXT NOT NULL,
  priority      INTEGER NOT NULL DEFAULT 1,
  UNIQUE (restaurant_id, source_type, source_url)
);

CREATE INDEX IF NOT EXISTS menu_sources_restaurant_idx ON menu_sources (restaurant_id);

-- Phase 1: raw menu storage (scrape paste, HTML snapshot, manual JSON, vision output)
CREATE TABLE IF NOT EXISTS menu_captures (
  id             BIGSERIAL PRIMARY KEY,
  restaurant_id  BIGINT NOT NULL REFERENCES restaurants (id) ON DELETE CASCADE,
  source_type    TEXT NOT NULL,
  source_url     TEXT,
  capture_format TEXT NOT NULL DEFAULT 'json'
                 CHECK (capture_format IN ('json', 'html', 'text', 'image_url')),
  raw_payload    JSONB NOT NULL,
  file_path      TEXT,
  capture_status TEXT NOT NULL DEFAULT 'captured'
                 CHECK (capture_status IN (
                   'captured', 'needs_review', 'approved', 'rejected'
                 )),
  notes          TEXT,
  captured_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS menu_captures_restaurant_idx ON menu_captures (restaurant_id);
CREATE INDEX IF NOT EXISTS menu_captures_status_idx ON menu_captures (capture_status);

-- Phase 2: structured items (filled by extract script, not yet implemented)
CREATE TABLE IF NOT EXISTS menu_categories (
  id            BIGSERIAL PRIMARY KEY,
  restaurant_id BIGINT NOT NULL REFERENCES restaurants (id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  UNIQUE (restaurant_id, name)
);

CREATE TABLE IF NOT EXISTS menu_items (
  id            BIGSERIAL PRIMARY KEY,
  restaurant_id BIGINT NOT NULL REFERENCES restaurants (id) ON DELETE CASCADE,
  category_id   BIGINT REFERENCES menu_categories (id) ON DELETE SET NULL,
  capture_id    BIGINT REFERENCES menu_captures (id) ON DELETE SET NULL,
  name          TEXT NOT NULL,
  price         NUMERIC(10, 2),
  veg_flag      BOOLEAN,
  description   TEXT,
  extract_status TEXT NOT NULL DEFAULT 'draft'
                 CHECK (extract_status IN ('draft', 'reviewed', 'published')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS menu_items_restaurant_idx ON menu_items (restaurant_id);

-- Phase 3: nutrition / tags (filled by analyze script, not yet implemented)
CREATE TABLE IF NOT EXISTS menu_item_analysis (
  id                  BIGSERIAL PRIMARY KEY,
  menu_item_id        BIGINT NOT NULL UNIQUE REFERENCES menu_items (id) ON DELETE CASCADE,
  calories_kcal       NUMERIC(8, 2),
  protein_g           NUMERIC(8, 2),
  carbs_g             NUMERIC(8, 2),
  fat_g               NUMERIC(8, 2),
  tags                TEXT[] NOT NULL DEFAULT '{}',
  analysis_method     TEXT CHECK (analysis_method IN ('chain_db', 'llm_estimate', 'manual')),
  analysis_confidence NUMERIC(3, 2),
  analyzed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Track pushes to production server (later)
CREATE TABLE IF NOT EXISTS sync_log (
  id          BIGSERIAL PRIMARY KEY,
  sync_type   TEXT NOT NULL,
  row_count   INTEGER,
  notes       TEXT,
  synced_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
