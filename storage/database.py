"""
SQLite database layer — all persistence operations for the educational system.

DISCLAIMER: Educational system for Solana devnet only.
Do not store real private keys or mainnet wallet data in this database.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

from utils.logger import get_logger
from utils.helpers import ensure_data_dir, load_env, utcnow_iso

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# DB connection helpers
# ---------------------------------------------------------------------------

def _get_db_path() -> str:
    load_env()
    return os.getenv("DB_PATH", "data/meme_detector.db")


def _get_schema_sql() -> str:
    schema_path = Path(__file__).parent / "schema.sql"
    with open(schema_path) as f:
        return f.read()


@contextmanager
def get_connection(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager yielding an SQLite connection with row_factory set.
    Auto-commits on clean exit, rolls back on exception.
    """
    path = db_path or _get_db_path()
    ensure_data_dir()

    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row       # rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_db(db_path: str | None = None) -> None:
    """
    Create tables if they don't exist yet. Safe to call on every startup.
    """
    path = db_path or _get_db_path()
    ensure_data_dir()
    schema = _get_schema_sql()

    with get_connection(path) as conn:
        conn.executescript(schema)

    logger.info(f"Database initialized at: {path}")


# ---------------------------------------------------------------------------
# Detection CRUD
# ---------------------------------------------------------------------------

def insert_detection(
    source: str,
    token_address: str,
    content_hash: str,
    *,
    token_name: str = "",
    token_symbol: str = "",
    price_usd: float = 0.0,
    liquidity_usd: float = 0.0,
    volume_5m: float = 0.0,
    volume_1h: float = 0.0,
    volume_24h: float = 0.0,
    price_change_5m: float = 0.0,
    price_change_1h: float = 0.0,
    holders: int = 0,
    age_hours: float = 0.0,
    social_mentions: int = 0,
    raw_data: dict | None = None,
) -> int | None:
    """
    Insert a new token detection. Returns the new row id, or None if duplicate.
    """
    sql = """
        INSERT OR IGNORE INTO detections (
            source, token_address, content_hash,
            token_name, token_symbol,
            price_usd, liquidity_usd,
            volume_5m, volume_1h, volume_24h,
            price_change_5m, price_change_1h,
            holders, age_hours, social_mentions, raw_data
        ) VALUES (
            :source, :token_address, :content_hash,
            :token_name, :token_symbol,
            :price_usd, :liquidity_usd,
            :volume_5m, :volume_1h, :volume_24h,
            :price_change_5m, :price_change_1h,
            :holders, :age_hours, :social_mentions, :raw_data
        )
    """
    params = {
        "source": source,
        "token_address": token_address,
        "content_hash": content_hash,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "price_usd": price_usd,
        "liquidity_usd": liquidity_usd,
        "volume_5m": volume_5m,
        "volume_1h": volume_1h,
        "volume_24h": volume_24h,
        "price_change_5m": price_change_5m,
        "price_change_1h": price_change_1h,
        "holders": holders,
        "age_hours": age_hours,
        "social_mentions": social_mentions,
        "raw_data": json.dumps(raw_data) if raw_data else None,
    }
    with get_connection() as conn:
        cursor = conn.execute(sql, params)
        return cursor.lastrowid if cursor.rowcount > 0 else None


def get_recent_detections(limit: int = 100) -> list[dict]:
    """Return the most recent detections, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM detections ORDER BY detected_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_detection_by_address(token_address: str) -> dict | None:
    """Return the most recent detection for a token address."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM detections WHERE token_address=? ORDER BY detected_at DESC LIMIT 1",
            (token_address,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Candidate CRUD
# ---------------------------------------------------------------------------

def insert_candidate(
    detection_id: int,
    token_address: str,
    score: float,
    *,
    token_name: str = "",
    token_symbol: str = "",
    volume_score: float = 0.0,
    liquidity_score: float = 0.0,
    holder_score: float = 0.0,
    social_score: float = 0.0,
    rug_risk_score: float = 0.0,
    mint_revoked: bool = False,
    freeze_revoked: bool = False,
    lp_locked: bool = False,
    top10_pct: float = 0.0,
) -> int | None:
    """Insert a scored candidate. Returns row id or None if already exists."""
    sql = """
        INSERT OR IGNORE INTO candidates (
            detection_id, token_address, token_name, token_symbol,
            score, volume_score, liquidity_score, holder_score,
            social_score, rug_risk_score,
            mint_revoked, freeze_revoked, lp_locked, top10_pct
        ) VALUES (
            :detection_id, :token_address, :token_name, :token_symbol,
            :score, :volume_score, :liquidity_score, :holder_score,
            :social_score, :rug_risk_score,
            :mint_revoked, :freeze_revoked, :lp_locked, :top10_pct
        )
    """
    with get_connection() as conn:
        cursor = conn.execute(sql, {
            "detection_id": detection_id,
            "token_address": token_address,
            "token_name": token_name,
            "token_symbol": token_symbol,
            "score": score,
            "volume_score": volume_score,
            "liquidity_score": liquidity_score,
            "holder_score": holder_score,
            "social_score": social_score,
            "rug_risk_score": rug_risk_score,
            "mint_revoked": int(mint_revoked),
            "freeze_revoked": int(freeze_revoked),
            "lp_locked": int(lp_locked),
            "top10_pct": top10_pct,
        })
        return cursor.lastrowid if cursor.rowcount > 0 else None


def update_candidate_status(
    candidate_id: int,
    status: str,
    rejection_reason: str = "",
) -> None:
    """Update a candidate's status (pending/approved/rejected/deployed/skipped)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE candidates SET status=?, rejection_reason=? WHERE id=?",
            (status, rejection_reason, candidate_id),
        )


def get_pending_candidates() -> list[dict]:
    """Return all candidates awaiting approval."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM candidates WHERE status='pending' ORDER BY score DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Generated token CRUD
# ---------------------------------------------------------------------------

def insert_generated_token(
    candidate_id: int,
    source_address: str,
    new_name: str,
    new_symbol: str,
    new_supply: int,
    *,
    source_name: str = "",
    source_symbol: str = "",
    new_description: str = "",
    new_decimals: int = 6,
    metadata_uri: str = "",
    image_uri: str = "",
) -> int:
    """Insert a generated token spec. Returns row id."""
    sql = """
        INSERT INTO generated_tokens (
            candidate_id, source_address, source_name, source_symbol,
            new_name, new_symbol, new_description,
            new_supply, new_decimals, metadata_uri, image_uri
        ) VALUES (
            :candidate_id, :source_address, :source_name, :source_symbol,
            :new_name, :new_symbol, :new_description,
            :new_supply, :new_decimals, :metadata_uri, :image_uri
        )
    """
    with get_connection() as conn:
        cursor = conn.execute(sql, {
            "candidate_id": candidate_id,
            "source_address": source_address,
            "source_name": source_name,
            "source_symbol": source_symbol,
            "new_name": new_name,
            "new_symbol": new_symbol,
            "new_description": new_description,
            "new_supply": new_supply,
            "new_decimals": new_decimals,
            "metadata_uri": metadata_uri,
            "image_uri": image_uri,
        })
        return cursor.lastrowid


def approve_generated_token(token_id: int) -> None:
    """Mark a generated token as approved for deployment."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE generated_tokens SET status='approved', approval_time=? WHERE id=?",
            (utcnow_iso(), token_id),
        )


# ---------------------------------------------------------------------------
# Deployment CRUD
# ---------------------------------------------------------------------------

def insert_deployment(generated_token_id: int, network: str = "devnet") -> int:
    """Create a deployment record and return its id."""
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO deployments (generated_token_id, network) VALUES (?, ?)",
            (generated_token_id, network),
        )
        return cursor.lastrowid


def update_deployment(
    deployment_id: int,
    **fields,
) -> None:
    """
    Update arbitrary fields on a deployment record.
    Example: update_deployment(1, mint_address="...", mint_tx="...", status="partial")
    """
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [deployment_id]
    with get_connection() as conn:
        conn.execute(
            f"UPDATE deployments SET {set_clause} WHERE id=?", values
        )


def get_deployments(network: str = "devnet") -> list[dict]:
    """Return all deployments for a given network."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM deployments WHERE network=? ORDER BY deployed_at DESC",
            (network,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Post-launch monitoring
# ---------------------------------------------------------------------------

def insert_monitoring_snapshot(
    deployment_id: int,
    pool_id: str,
    *,
    price_usd: float = 0.0,
    liquidity_usd: float = 0.0,
    volume_1h: float = 0.0,
    holders: int = 0,
    tx_count_1h: int = 0,
) -> None:
    """Record a post-launch monitoring snapshot."""
    sql = """
        INSERT INTO monitoring_snapshots
            (deployment_id, pool_id, price_usd, liquidity_usd, volume_1h, holders, tx_count_1h)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        conn.execute(sql, (
            deployment_id, pool_id,
            price_usd, liquidity_usd, volume_1h, holders, tx_count_1h,
        ))


def get_monitoring_history(deployment_id: int) -> list[dict]:
    """Return all snapshots for a deployment, oldest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM monitoring_snapshots WHERE deployment_id=? ORDER BY snapshot_at ASC",
            (deployment_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Bot run log
# ---------------------------------------------------------------------------

def log_bot_run(
    cycle_number: int,
    detections_found: int = 0,
    candidates_scored: int = 0,
    deployments_attempted: int = 0,
    duration_seconds: float = 0.0,
    status: str = "ok",
    error_message: str = "",
) -> None:
    """Record a bot run cycle in the audit log."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO bot_runs
                (cycle_number, detections_found, candidates_scored,
                 deployments_attempted, duration_seconds, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (cycle_number, detections_found, candidates_scored,
             deployments_attempted, duration_seconds, status, error_message),
        )


# ---------------------------------------------------------------------------
# CLI entry point for database initialization
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if "--init" in sys.argv:
        initialize_db()
        print("Database initialized successfully.")
    else:
        print("Usage: python -m storage.database --init")
