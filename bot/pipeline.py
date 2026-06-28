"""
Full bot pipeline — wires monitor → analyze → approve → deploy → monitor.

DISCLAIMER: Educational system for Solana devnet only.
Do NOT use on mainnet without full legal and compliance review.

This is the heart of the system. One cycle does:
  1. Run all monitors (DexScreener, pump.fun, social, on-chain)
  2. Score detections for virality + rug risk
  3. Generate token specs for qualifying candidates
  4. Present to operator for manual approval
  5. Deploy approved tokens to Raydium devnet
  6. Simulate post-launch "promotion" (print only — no real promotion)
  7. Start monitoring the deployed pool
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from monitor.aggregator import MonitorAggregator, TokenDetection
from analysis.scorer import TokenScorer, ScoredCandidate
from analysis.generator import TokenGenerator, GeneratedTokenSpec
from bot.approval import ApprovalGate
from deploy.pipeline import deploy_approved_token
from storage.database import log_bot_run
from utils.logger import get_logger, log_banner
from utils.helpers import load_config, utcnow_iso

logger = get_logger(__name__)


@dataclass
class CycleResult:
    """Summary of one bot run cycle."""

    cycle_number: int = 0
    started_at: str = field(default_factory=utcnow_iso)
    duration_seconds: float = 0.0
    detections_found: int = 0
    candidates_scored: int = 0
    candidates_approved: int = 0
    deployments_attempted: int = 0
    deployments_succeeded: int = 0
    errors: list[str] = field(default_factory=list)


class BotPipeline:
    """
    Orchestrates one full cycle of the meme coin detection + deployment system.

    Used by main.py in a loop.
    """

    def __init__(self, mode: str = "full"):
        """
        Args:
            mode: "monitor-only" | "analyze-only" | "full"
                  "monitor-only" — only detect, no scoring or deployment
                  "analyze-only" — detect + score, no deployment
                  "full" — complete pipeline with approval gate
        """
        cfg = load_config()
        if cfg.get("network") == "mainnet-beta":
            raise ValueError("MAINNET BLOCKED: devnet only.")

        self._mode = mode
        self._approval_gate = ApprovalGate()
        self._cycle_number = 0

    async def run_cycle(self) -> CycleResult:
        """Run one complete detection → deploy cycle."""
        self._cycle_number += 1
        cycle_start = time.monotonic()
        result = CycleResult(cycle_number=self._cycle_number)

        log_banner(logger, f"Cycle #{self._cycle_number} starting ({self._mode} mode)")

        try:
            # Phase 1: Detection
            detections = await self._run_detection()
            result.detections_found = len(detections)
            logger.info(f"Detections: {len(detections)} tokens found")

            if self._mode == "monitor-only" or not detections:
                return result

            # Phase 2: Scoring + filtering
            candidates = await self._score_detections(detections)
            result.candidates_scored = len(candidates)
            logger.info(f"Candidates after scoring: {len(candidates)}")

            if self._mode == "analyze-only" or not candidates:
                return result

            # Phase 3: Token generation + approval + deployment
            for candidate in candidates:
                try:
                    approved, spec = await self._generate_and_approve(candidate)
                    if approved and spec:
                        result.candidates_approved += 1
                        deploy_ok = await self._deploy(candidate, spec)
                        result.deployments_attempted += 1
                        if deploy_ok:
                            result.deployments_succeeded += 1
                            self._simulate_promotion(spec)
                except Exception as exc:
                    logger.error(f"Pipeline error for {candidate.token_address}: {exc}")
                    result.errors.append(str(exc))

        except Exception as exc:
            logger.exception(f"Cycle #{self._cycle_number} failed: {exc}")
            result.errors.append(str(exc))
        finally:
            result.duration_seconds = time.monotonic() - cycle_start
            self._log_cycle(result)

        return result

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    async def _run_detection(self) -> list[TokenDetection]:
        """Run all monitors and return deduplicated detections."""
        async with MonitorAggregator() as aggregator:
            return await aggregator.run_cycle()

    async def _score_detections(
        self, detections: list[TokenDetection]
    ) -> list[ScoredCandidate]:
        """Score detections and return those above threshold."""
        scorer = TokenScorer()
        return await scorer.score_batch(detections)

    async def _generate_and_approve(
        self, candidate: ScoredCandidate
    ) -> tuple[bool, Optional[GeneratedTokenSpec]]:
        """
        Generate a token spec and ask for operator approval.
        Returns (approved, spec) — spec is None if not approved.
        """
        generator = TokenGenerator()
        spec = generator.generate(candidate)
        spec.db_id = generator.persist(spec, candidate.db_id or 0)

        logger.info(
            f"Generated: {spec.new_name} ({spec.new_symbol}) "
            f"[inspired by {candidate.token_name}]"
        )

        # Approval gate (CLI interactive)
        approved = await self._approval_gate.request_approval_cli(candidate, spec)
        return approved, spec if approved else None

    async def _deploy(
        self, candidate: ScoredCandidate, spec: GeneratedTokenSpec
    ) -> bool:
        """Run the deployment pipeline. Returns True on success."""
        logger.info(f"Deploying: {spec.new_name} on devnet")
        result = await deploy_approved_token(candidate, spec)
        success = result.get("success", False)
        if success:
            logger.info(
                f"✓ Deployed {spec.new_name}: "
                f"pool={result.get('pool_id', 'N/A')}"
            )
        else:
            logger.error(
                f"✗ Deployment failed: {result.get('error', 'unknown error')}"
            )
        return success

    def _simulate_promotion(self, spec: GeneratedTokenSpec) -> None:
        """
        Simulate post-launch "promotion" by printing what would be posted.
        In an educational context this is just console output — NO actual posting.
        """
        post = self._generate_promo_text(spec)
        border = "-" * 50
        logger.info(f"\n{border}")
        logger.info("[SIMULATED PROMOTION TEXT — NOT ACTUALLY POSTED]")
        logger.info(f"{border}")
        logger.info(post)
        logger.info(f"{border}\n")

    def _generate_promo_text(self, spec: GeneratedTokenSpec) -> str:
        """Generate example social post text for educational display."""
        return (
            f"🚀 {spec.new_name} (${spec.new_symbol}) is LIVE on Solana devnet! 🌙\n"
            f"Total supply: {spec.new_supply:,}\n"
            f"80% in the pool, 0 team tokens in circulation.\n"
            f"#Solana #MemeCoin #DEVNET_ONLY\n"
            f"\n[This is an educational simulation. "
            f"This token is on devnet and has no real value.]"
        )

    def _log_cycle(self, result: CycleResult) -> None:
        """Persist the cycle summary to the DB."""
        log_bot_run(
            cycle_number=result.cycle_number,
            detections_found=result.detections_found,
            candidates_scored=result.candidates_scored,
            deployments_attempted=result.deployments_attempted,
            duration_seconds=result.duration_seconds,
            status="ok" if not result.errors else "error",
            error_message="; ".join(result.errors[:3]),
        )
        logger.info(
            f"Cycle #{result.cycle_number} done in {result.duration_seconds:.1f}s — "
            f"detections={result.detections_found} "
            f"candidates={result.candidates_scored} "
            f"deployed={result.deployments_succeeded}"
        )
