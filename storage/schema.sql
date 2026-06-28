-- =============================================================================
-- Database Schema — Solana Meme Coin Educational System
-- =============================================================================
-- DISCLAIMER: For educational and testing purposes only. Devnet use only.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Detected tokens (raw from all monitors)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS detections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at     TEXT NOT NULL DEFAULT (datetime('now')),
    source          TEXT NOT NULL,          -- dexscreener | pumpfun | onchain | social
    token_address   TEXT NOT NULL,
    token_name      TEXT,
    token_symbol    TEXT,
    chain           TEXT NOT NULL DEFAULT 'solana',
    price_usd       REAL,
    liquidity_usd   REAL,
    volume_5m       REAL,
    volume_1h       REAL,
    volume_24h      REAL,
    price_change_5m REAL,
    price_change_1h REAL,
    holders         INTEGER,
    age_hours       REAL,
    social_mentions INTEGER DEFAULT 0,
    raw_data        TEXT,                   -- JSON blob of raw scraped data
    content_hash    TEXT NOT NULL,          -- For deduplication across sources
    UNIQUE(content_hash)
);

CREATE INDEX IF NOT EXISTS idx_detections_token ON detections(token_address);
CREATE INDEX IF NOT EXISTS idx_detections_at ON detections(detected_at);

-- ---------------------------------------------------------------------------
-- Scored / analyzed candidates
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id    INTEGER NOT NULL REFERENCES detections(id),
    scored_at       TEXT NOT NULL DEFAULT (datetime('now')),
    token_address   TEXT NOT NULL,
    token_name      TEXT,
    token_symbol    TEXT,
    -- Composite score 0–100
    score           REAL NOT NULL,
    -- Sub-scores
    volume_score    REAL,
    liquidity_score REAL,
    holder_score    REAL,
    social_score    REAL,
    rug_risk_score  REAL,               -- 0=safe, 100=obvious rug
    -- Derived flags
    mint_revoked    INTEGER DEFAULT 0,  -- 1 if mint authority revoked
    freeze_revoked  INTEGER DEFAULT 0,
    lp_locked       INTEGER DEFAULT 0,
    top10_pct       REAL,               -- % held by top 10 wallets
    -- Status
    status          TEXT NOT NULL DEFAULT 'pending',
    -- pending | approved | rejected | deployed | skipped
    rejection_reason TEXT,
    UNIQUE(detection_id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_score ON candidates(score DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);

-- ---------------------------------------------------------------------------
-- Generated token specs (what we'll deploy as the educational "clone")
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS generated_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- Inspired-by token info
    source_address  TEXT NOT NULL,
    source_name     TEXT,
    source_symbol   TEXT,
    -- New token spec
    new_name        TEXT NOT NULL,
    new_symbol      TEXT NOT NULL,
    new_description TEXT,
    new_supply      INTEGER NOT NULL,
    new_decimals    INTEGER NOT NULL DEFAULT 6,
    -- Metadata
    metadata_uri    TEXT,               -- URI to the JSON metadata file
    image_uri       TEXT,
    -- Status
    status          TEXT NOT NULL DEFAULT 'draft',
    -- draft | approved | deployed | failed
    approval_time   TEXT,
    approved_by     TEXT DEFAULT 'human_operator',
    UNIQUE(candidate_id)
);

-- ---------------------------------------------------------------------------
-- Deployment records
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deployments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_token_id  INTEGER NOT NULL REFERENCES generated_tokens(id),
    deployed_at         TEXT NOT NULL DEFAULT (datetime('now')),
    network             TEXT NOT NULL DEFAULT 'devnet',
    -- Mint / token
    mint_address        TEXT,
    mint_tx             TEXT,           -- Transaction signature
    metadata_tx         TEXT,
    -- OpenBook market
    market_id           TEXT,
    market_tx           TEXT,
    -- Raydium pool
    pool_id             TEXT,
    pool_tx             TEXT,
    -- Liquidity added
    sol_added           REAL,
    tokens_added        INTEGER,
    -- Authority revocations
    mint_revoked        INTEGER DEFAULT 0,
    freeze_revoked      INTEGER DEFAULT 0,
    -- Status
    status              TEXT NOT NULL DEFAULT 'pending',
    -- pending | partial | complete | failed
    error_message       TEXT,
    UNIQUE(generated_token_id)
);

CREATE INDEX IF NOT EXISTS idx_deployments_network ON deployments(network);
CREATE INDEX IF NOT EXISTS idx_deployments_status ON deployments(status);

-- ---------------------------------------------------------------------------
-- Post-launch monitoring snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS monitoring_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id   INTEGER NOT NULL REFERENCES deployments(id),
    snapshot_at     TEXT NOT NULL DEFAULT (datetime('now')),
    pool_id         TEXT NOT NULL,
    price_usd       REAL,
    liquidity_usd   REAL,
    volume_1h       REAL,
    holders         INTEGER,
    tx_count_1h     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_snapshots_pool ON monitoring_snapshots(pool_id);

-- ---------------------------------------------------------------------------
-- Bot run log (each full monitoring cycle)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    cycle_number    INTEGER NOT NULL,
    detections_found INTEGER DEFAULT 0,
    candidates_scored INTEGER DEFAULT 0,
    deployments_attempted INTEGER DEFAULT 0,
    duration_seconds REAL,
    error_message   TEXT,
    status          TEXT NOT NULL DEFAULT 'ok'    -- ok | error | partial
);
