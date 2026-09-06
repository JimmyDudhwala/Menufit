-- Raw menu assets (collect first, LLM extract later)

CREATE TABLE IF NOT EXISTS menu_assets (
  id              BIGSERIAL PRIMARY KEY,
  restaurant_id   BIGINT NOT NULL REFERENCES restaurants (id) ON DELETE CASCADE,
  source_type     TEXT NOT NULL,
  source_url      TEXT,
  asset_kind      TEXT NOT NULL
                  CHECK (asset_kind IN (
                    'pdf', 'image', 'html', 'url_only', 'folder'
                  )),
  local_path      TEXT,
  file_sha256     TEXT,
  bytes           BIGINT,
  page_count      INTEGER,
  extract_status  TEXT NOT NULL DEFAULT 'pending'
                  CHECK (extract_status IN (
                    'pending', 'queued', 'extracted', 'failed', 'skipped'
                  )),
  notes           TEXT,
  collected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS menu_assets_restaurant_idx ON menu_assets (restaurant_id);
CREATE INDEX IF NOT EXISTS menu_assets_extract_idx ON menu_assets (extract_status);
CREATE UNIQUE INDEX IF NOT EXISTS menu_assets_unique_url
  ON menu_assets (restaurant_id, source_url)
  WHERE source_url IS NOT NULL;
