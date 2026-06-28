"""
Manual approval gate — MUST be passed before any token is deployed.

DISCLAIMER: Educational system for Solana devnet only.
The approval gate is a critical safety mechanism. It is NEVER auto-bypassed.

Approval modes:
  1. CLI interactive: Operator reviews candidate and types the confirm phrase.
  2. Dashboard: Operator clicks "Approve" in the Streamlit UI (updates DB).
  3. API: (future) a simple HTTP endpoint for external approval.

The approval gate enforces:
  - Human review of the token details and scores.
  - Explicit typed confirmation phrase ("I_CONFIRM_DEVNET_ONLY").
  - DB status update to prevent double-deployment.
  - Timeout (default 5 minutes) to prevent stale approvals.
"""

import asyncio
import os
import time
from typing import Optional

from analysis.scorer import ScoredCandidate
from analysis.generator import GeneratedTokenSpec
from storage.database import update_candidate_status, approve_generated_token
from utils.logger import get_logger
from utils.helpers import load_config

logger = get_logger(__name__)

REQUIRE_APPROVAL = os.getenv("REQUIRE_APPROVAL", "1") == "1"


class ApprovalGate:
    """
    Manages the manual approval process for token deployments.

    The gate blocks deployment until a human explicitly approves.
    In monitor-only mode, the gate is never reached.
    """

    def __init__(self):
        cfg = load_config()
        approval_cfg = cfg.get("approval", {})
        self._enabled = approval_cfg.get("enabled", True)
        self._timeout = approval_cfg.get("timeout_seconds", 300)
        self._confirm_phrase = approval_cfg.get(
            "confirm_phrase", "I_CONFIRM_DEVNET_ONLY"
        )

        if not self._enabled:
            logger.warning(
                "⚠️  APPROVAL GATE IS DISABLED in config. "
                "This should only be disabled for automated testing — never in production."
            )

    async def request_approval_cli(
        self,
        candidate: ScoredCandidate,
        spec: GeneratedTokenSpec,
    ) -> bool:
        """
        Display candidate details in the terminal and wait for operator confirmation.

        Returns True if approved, False if rejected or timed out.
        """
        if not self._enabled:
            logger.warning("Approval gate disabled — auto-approving (NOT RECOMMENDED)")
            return True

        self._display_candidate(candidate, spec)

        loop = asyncio.get_event_loop()
        try:
            approved = await asyncio.wait_for(
                loop.run_in_executor(None, self._get_cli_approval),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Approval timed out after {self._timeout}s — rejecting")
            self._update_rejected(candidate, "approval_timeout")
            return False

        if approved:
            self._update_approved(candidate, spec)
        else:
            self._update_rejected(candidate, "operator_rejected")

        return approved

    def _display_candidate(
        self,
        candidate: ScoredCandidate,
        spec: GeneratedTokenSpec,
    ) -> None:
        """Print a formatted candidate review for the operator."""
        border = "=" * 60
        print(f"\n{border}")
        print(f"  *** DEPLOYMENT APPROVAL REQUIRED ***")
        print(f"  DEVNET ONLY — No real funds involved")
        print(border)
        print(f"\nSource token (detected):")
        print(f"  Name:     {candidate.token_name}")
        print(f"  Address:  {candidate.token_address}")
        print(f"  Score:    {candidate.composite_score:.1f}/100")
        print(f"  Volume:   ${candidate.detection.volume_5m:.0f} (5m)")
        print(f"  Liq:      ${candidate.detection.liquidity_usd:.0f}")
        print(f"  Sources:  {candidate.detection.source_string}")
        print(f"\nRisk flags: {', '.join(candidate.risk_flags) or 'none'}")
        print(f"Rug risk score: {candidate.rug_risk_score:.0f}/100")
        print(f"\nProposed new token (educational clone):")
        print(f"  Name:     {spec.new_name}")
        print(f"  Symbol:   {spec.new_symbol}")
        print(f"  Supply:   {spec.new_supply:,}")
        print(f"  Pool:     {spec.pool_tokens:,} tokens ({int(spec.pool_tokens/spec.new_supply*100)}%)")
        print(f"  Sol:      {load_config().get('deploy', {}).get('initial_sol', 0.1)} SOL devnet")
        print(f"\nDescription: {spec.new_description[:120]}...")
        print(f"\n{border}")
        print(f"  To APPROVE: type exactly: {self._confirm_phrase}")
        print(f"  To REJECT:  type anything else or press Ctrl+C")
        print(f"  Timeout:    {self._timeout}s")
        print(f"{border}\n")

    def _get_cli_approval(self) -> bool:
        """Blocking input — runs in executor."""
        try:
            response = input("Your decision: ").strip()
            return response == self._confirm_phrase
        except (EOFError, KeyboardInterrupt):
            return False

    def _update_approved(
        self,
        candidate: ScoredCandidate,
        spec: GeneratedTokenSpec,
    ) -> None:
        logger.info(f"✓ Approved by operator: {spec.new_name} ({spec.new_symbol})")
        if candidate.db_id:
            update_candidate_status(candidate.db_id, "approved")
        if spec.db_id:
            approve_generated_token(spec.db_id)

    def _update_rejected(self, candidate: ScoredCandidate, reason: str) -> None:
        logger.info(f"✗ Rejected: {candidate.token_name} — reason: {reason}")
        if candidate.db_id:
            update_candidate_status(candidate.db_id, "rejected", rejection_reason=reason)

    async def check_dashboard_approval(
        self, generated_token_db_id: int, poll_interval: float = 5.0
    ) -> bool:
        """
        Poll the database for dashboard-based approval (set by the Streamlit UI).
        Useful when running the bot in headless mode with the dashboard open separately.
        Returns True if approved via DB update before timeout.
        """
        from storage.database import get_connection
        deadline = time.monotonic() + self._timeout

        while time.monotonic() < deadline:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT status FROM generated_tokens WHERE id=?",
                    (generated_token_db_id,),
                ).fetchone()

            if row and row["status"] == "approved":
                return True
            if row and row["status"] == "rejected":
                return False

            await asyncio.sleep(poll_interval)

        logger.warning("Dashboard approval timed out")
        return False
