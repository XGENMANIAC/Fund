-- =============================================================================
-- Database Schema — Solana Meme Coin Educational System (v2 — Mimicry Update)
-- =============================================================================
-- DISCLAIMER: For educational and testing purposes only. Devnet use only.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Detected tokens (raw from all monitors)
-- v2: added image_uri, metadata_uri, original_description, metadata_json
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS detections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at         TEXT NOT NULL DEFAULT (datetime('now')),
    source              TEXT NOT NULL,          -- dexscreener | pumpfun | birdeye | onchain | social
    token_address       TEXT NOT NULL,
    token_name          TEXT,
    token_symbol        TEXT,
    chain               TEXT NOT NULL DEFAULT 'solana',
    price_usd           REAL,
    liquidity_usd       REAL,
    volume_5m           REAL,
    volume_1h           REAL,
    volume_24h          REAL,
    price_change_5m     REAL,
    price_change_1h     REAL,
    holders             INTEGER,
    age_hours           REAL,
    social_mentions     INTEGER DEFAULT 0,
    -- Metadata fields (v2 addition)
    image_uri           TEXT,                   -- Original token image (scraped)
    metadata_uri        TEXT,                   -- On-chain metadata URI (e.g. IPFS/Arweave)
    original_description TEXT,                  -- Full token description from source
    metadata_json       TEXT,                   -- Full metadata JSON fetched from metadata_uri
    raw_data            TEXT,                   -- Raw scraped data blob (JSON)
    content_hash        TEXT NOT NULL,
    UNIQUE(content_hash)
);

CREATE INDEX IF NOT EXISTS idx_detections_token ON detections(token_address);
CREATE INDEX IF NOT EXISTS idx_detections_at ON detections(detected_at);
CREATE INDEX IF NOT EXISTS idx_detections_source ON detections(source);

-- ---------------------------------------------------------------------------
-- Scored / analyzed candidates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id        INTEGER NOT NULL REFERENCES detections(id),
    scored_at           TEXT NOT NULL DEFAULT (datetime('now')),
    token_address       TEXT NOT NULL,
    token_name          TEXT,
    token_symbol        TEXT,
    score               REAL NOT NULL,          -- Virality composite 0–100
    copy_viability      REAL DEFAULT 0,         -- v2: Copying viability 0–100
    volume_score        REAL,
    liquidity_score     REAL,
    holder_score        REAL,
    social_score        REAL,
    rug_risk_score      REAL,
    -- On-chain risk flags
    mint_revoked        INTEGER DEFAULT 0,
    freeze_revoked      INTEGER DEFAULT 0,
    lp_locked           INTEGER DEFAULT 0,
    top10_pct           REAL,
    -- Copy viability sub-scores (v2)
    has_image           INTEGER DEFAULT 0,
    has_description     INTEGER DEFAULT 0,
    has_metadata_uri    INTEGER DEFAULT 0,
    metadata_fetchable  INTEGER DEFAULT 0,
    -- Status
    status              TEXT NOT NULL DEFAULT 'pending',
    rejection_reason    TEXT,
    UNIQUE(detection_id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_score ON candidates(score DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_copy ON candidates(copy_viability DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);

-- ---------------------------------------------------------------------------
-- Generated token specs (the educational "close clone")
-- v2: added mimicry fields, variation rule tracking, source image
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS generated_tokens (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id            INTEGER NOT NULL REFERENCES candidates(id),
    generated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    -- Source (original) token
    source_address          TEXT NOT NULL,
    source_name             TEXT,
    source_symbol           TEXT,
    source_description      TEXT,               -- v2: full original description
    source_image_uri        TEXT,               -- v2: original image URL
    source_metadata_uri     TEXT,               -- v2: original on-chain metadata URI
    -- Generated (clone) token
    new_name                TEXT NOT NULL,
    new_symbol              TEXT NOT NULL,
    new_description         TEXT,
    new_supply              INTEGER NOT NULL,
    new_decimals            INTEGER NOT NULL DEFAULT 6,
    -- Mimicry metadata (v2)
    variation_rule          TEXT,               -- e.g. "capitalize_first | add_suffix_! | emoji_append"
    name_diff               TEXT,               -- human-readable diff: "BONK" → "B0NK"
    symbol_diff             TEXT,               -- "BONK" → "BONK!"
    description_diff        TEXT,               -- what changed in description
    mimicry_score           REAL DEFAULT 0,     -- 0–100: how similar is clone to original
    -- Uploaded metadata
    metadata_uri            TEXT,               -- Final URI (IPFS/Arweave/local)
    image_uri               TEXT,               -- Final image URI (re-uploaded or mirrored)
    uploaded_image_uri      TEXT,               -- v2: URI of re-uploaded image
    metadata_provider       TEXT DEFAULT 'local_uri',
    -- Status
    status                  TEXT NOT NULL DEFAULT 'draft',
    approval_time           TEXT,
    approved_by             TEXT DEFAULT 'human_operator',
    UNIQUE(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_generated_status ON generated_tokens(status);

-- ---------------------------------------------------------------------------
-- Deployment records
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deployments (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_token_id      INTEGER NOT NULL REFERENCES generated_tokens(id),
    deployed_at             TEXT NOT NULL DEFAULT (datetime('now')),
    network                 TEXT NOT NULL DEFAULT 'devnet',
    mint_address            TEXT,
    mint_tx                 TEXT,
    metadata_tx             TEXT,
    market_id               TEXT,
    market_tx               TEXT,
    pool_id                 TEXT,
    pool_tx                 TEXT,
    sol_added               REAL,
    tokens_added            INTEGER,
    mint_revoked            INTEGER DEFAULT 0,
    freeze_revoked          INTEGER DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'pending',
    error_message           TEXT,
    UNIQUE(generated_token_id)
);

CREATE INDEX IF NOT EXISTS idx_deployments_network ON deployments(network);
CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status);

-- ---------------------------------------------------------------------------
-- Post-launch monitoring snapshots (v2: expanded fields)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   INTEGER NOT NULL REFERENCES deployments(id),
    snapshot_at     TEXT NOT NULL DEFAULT (datetime('now')),
    pool_id         TEXT NOT NULL,
    price_usd       REAL,
    liquidity_usd   REAL,
    volume_1h       REAL,
    volume_24h      REAL,
    holders         INTEGER,
    tx_count_1h     INTEGER,
    price_change_1h REAL,
    market_cap_usd  REAL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_pool ON monitoring_snapshots(pool_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_deployment ON monitoring_snapshots(deployment_id);

-- ---------------------------------------------------------------------------
-- Variation rule audit log (v2) — tracks every mimicry rule applied
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS variation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_id    INTEGER NOT NULL REFERENCES generated_tokens(id),
    logged_at       TEXT NOT NULL DEFAULT (datetime('now')),
    field           TEXT NOT NULL,              -- "name" | "symbol" | "description" | "image"
    rule_name       TEXT NOT NULL,              -- e.g. "capitalize_toggle" | "add_suffix_bang"
    original_value  TEXT,
    modified_value  TEXT,
    similarity_pct  REAL                        -- Levenshtein similarity 0–100
);

-- ---------------------------------------------------------------------------
-- Bot run log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    cycle_number            INTEGER NOT NULL,
    detections_found        INTEGER DEFAULT 0,
    candidates_scored       INTEGER DEFAULT 0,
    deployments_attempted   INTEGER DEFAULT 0,
    duration_seconds        REAL,
    error_message           TEXT,
    status                  TEXT NOT NULL DEFAULT 'ok'
);

-- ---------------------------------------------------------------------------
-- Schema migrations guard (v2)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO schema_version (version) VALUES (2);
