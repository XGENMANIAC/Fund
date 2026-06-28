"""
Full bot pipeline — wires monitor → analyze → approve → deploy → monitor.

DISCLAIMER: Educational system for Solana devnet only.
Do NOT use on mainnet without full legal and compliance review.

This is the heart of the system. One cycle does:
  1. Run all monitors (DexScreener, pump.fun, Birdeye, social, on-chain)
  2. Score detections for virality + copy viability
  3. Generate token specs using the mimicry engine
  4. Present to operator for manual approval
  5. Deploy approved tokens to Raydium devnet
  6. Simulate post-launch "promotion" (print only — no real promotion)
  7. Start live monitoring loop for deployed pools (v2)
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from monitor.aggregator import MonitorAggregator, TokenDetection
from analysis.scorer import TokenScorer, ScoredCandidate
from analysis.generator import TokenGenerator, GeneratedTokenSpec
from bot.approval import ApprovalGate
from deploy.pipeline import deploy_approved_token
from storage.database import (
    log_bot_run,
    insert_monitoring_snapshot,
    get_deployments,
    get_connection,
)
from utils.logger import get_logger, log_banner
from utils.helpers import load_config, utcnow_iso

logger = get_logger(__name__)

# How long to run the monitoring loop per deployment (default 2 hours)
DEFAULT_MONITOR_DURATION_SECS = 7200
# Snapshot interval
DEFAULT_SNAPSHOT_INTERVAL_SECS = 60


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
        """
        cfg = load_config()
        if cfg.get("network") == "mainnet-beta":
            raise ValueError("MAINNET BLOCKED: devnet only.")

        self._mode = mode
        self._approval_gate = ApprovalGate()
        self._cycle_number = 0
        self._cfg = cfg

        # Background monitoring tasks keyed by deployment_id
        self._monitor_tasks: dict[int, asyncio.Task] = {}

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
                        deploy_result, dep_id = await self._deploy(candidate, spec)
                        result.deployments_attempted += 1
                        if deploy_result:
                            result.deployments_succeeded += 1
                            self._simulate_promotion(spec)
                            # Start post-launch monitoring in background
                            if dep_id is not None:
                                self._start_pool_monitor(dep_id, spec)
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
        Generate a mimicry token spec and ask for operator approval.
        Returns (approved, spec).
        """
        generator = TokenGenerator()
        # Use async variant to attempt image re-upload
        spec = await generator.generate_async(candidate)
        spec.db_id = generator.persist(spec, candidate.db_id or 0)

        logger.info(
            f"Generated: {spec.new_name} ({spec.new_symbol}) "
            f"[mimicry={spec.mimicry_score:.0f}%] "
            f"[inspired by {candidate.token_name}]"
        )

        approved = await self._approval_gate.request_approval_cli(candidate, spec)
        return approved, spec if approved else None

    async def _deploy(
        self, candidate: ScoredCandidate, spec: GeneratedTokenSpec
    ) -> tuple[bool, Optional[int]]:
        """
        Run the deployment pipeline.
        Returns (success, deployment_id).
        """
        logger.info(f"Deploying: {spec.new_name} on devnet")
        result = await deploy_approved_token(candidate, spec)
        success = result.get("success", False)
        dep_id = result.get("deployment_id")

        if success:
            logger.info(
                f"✓ Deployed {spec.new_name}: "
                f"mint={result.get('mint_address', 'N/A')} "
                f"pool={result.get('pool_id', 'N/A')}"
            )
        else:
            logger.error(f"✗ Deployment failed: {result.get('error', 'unknown error')}")

        return success, dep_id

    def _simulate_promotion(self, spec: GeneratedTokenSpec) -> None:
        """
        Simulate post-launch "promotion" — console output only.
        NO actual posting to social media or any external service.
        """
        post = self._generate_promo_text(spec)
        border = "-" * 50
        logger.info(f"\n{border}")
        logger.info("[SIMULATED PROMOTION TEXT — NOT ACTUALLY POSTED]")
        logger.info(f"{border}")
        logger.info(post)
        logger.info(f"{border}\n")

    def _generate_promo_text(self, spec: GeneratedTokenSpec) -> str:
        return (
            f"🚀 {spec.new_name} (${spec.new_symbol}) is LIVE on Solana devnet! 🌙\n"
            f"Total supply: {spec.new_supply:,}\n"
            f"80% in the pool, 0 team tokens in circulation.\n"
            f"#Solana #MemeCoin #DEVNET_ONLY\n"
            f"\n[This is an educational simulation. "
            f"This token is on devnet and has no real value.]"
        )

    # ------------------------------------------------------------------
    # Post-launch pool monitoring (v2)
    # ------------------------------------------------------------------

    def _start_pool_monitor(
        self,
        deployment_id: int,
        spec: GeneratedTokenSpec,
    ) -> None:
        """
        Launch a background asyncio task that periodically snapshots the
        deployed pool's state and writes to monitoring_snapshots table.

        The task runs until duration expires or the pipeline shuts down.
        Uses DexScreener's free API to get pool metrics.
        """
        if deployment_id in self._monitor_tasks:
            existing = self._monitor_tasks[deployment_id]
            if not existing.done():
                logger.debug(f"Monitor already running for deployment {deployment_id}")
                return

        task = asyncio.create_task(
            self._pool_monitor_loop(deployment_id, spec),
            name=f"pool-monitor-{deployment_id}",
        )
        self._monitor_tasks[deployment_id] = task
        logger.info(f"Started pool monitor for deployment {deployment_id}")

    async def _pool_monitor_loop(
        self,
        deployment_id: int,
        spec: GeneratedTokenSpec,
    ) -> None:
        """
        Continuously snapshots pool metrics from DexScreener for up to
        DEFAULT_MONITOR_DURATION_SECS seconds.
        """
        cfg_monitor = self._cfg.get("monitoring", {})
        duration = cfg_monitor.get("duration_seconds", DEFAULT_MONITOR_DURATION_SECS)
        interval = cfg_monitor.get("snapshot_interval_seconds", DEFAULT_SNAPSHOT_INTERVAL_SECS)

        # Fetch pool_id from deployment record
        pool_id = await self._get_pool_id(deployment_id)
        if not pool_id:
            logger.warning(f"No pool_id found for deployment {deployment_id} — monitor skipped")
            return

        deadline = time.monotonic() + duration
        snapshot_count = 0

        logger.info(
            f"Pool monitor started: pool={pool_id[:12]}… "
            f"interval={interval}s duration={duration}s"
        )

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            while time.monotonic() < deadline:
                try:
                    metrics = await self._fetch_pool_metrics(session, pool_id)
                    if metrics:
                        insert_monitoring_snapshot(
                            deployment_id=deployment_id,
                            pool_id=pool_id,
                            **metrics,
                        )
                        snapshot_count += 1
                        logger.debug(
                            f"Snapshot #{snapshot_count} for {pool_id[:12]}…: "
                            f"price=${metrics.get('price_usd', 0):.6f} "
                            f"liq=${metrics.get('liquidity_usd', 0):,.0f}"
                        )
                except Exception as exc:
                    logger.warning(f"Pool monitor snapshot error: {exc}")

                await asyncio.sleep(interval)

        logger.info(
            f"Pool monitor completed for deployment {deployment_id}: "
            f"{snapshot_count} snapshots over {duration}s"
        )

    async def _fetch_pool_metrics(
        self, session: aiohttp.ClientSession, pool_id: str
    ) -> Optional[dict]:
        """
        Fetch current pool metrics from DexScreener's free pair endpoint.
        Returns a dict suitable for insert_monitoring_snapshot kwargs.
        """
        url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pool_id}"
        try:
            async with session.get(url) as resp:
                if resp.status == 429:
                    await asyncio.sleep(15)
                    return None
                if resp.status != 200:
                    return None
                data = await resp.json()

            pairs = data.get("pairs", [])
            if not pairs:
                return None

            pair = pairs[0]
            volume = pair.get("volume", {})
            liquidity = pair.get("liquidity", {})
            price_change = pair.get("priceChange", {})

            return {
                "price_usd": float(pair.get("priceUsd", 0) or 0),
                "liquidity_usd": float(liquidity.get("usd", 0) or 0),
                "volume_1h": float(volume.get("h1", 0) or 0),
                "volume_24h": float(volume.get("h24", 0) or 0),
                "price_change_1h": float(price_change.get("h1", 0) or 0),
                "market_cap_usd": float(pair.get("fdv", 0) or 0),
                "tx_count_1h": int(pair.get("txns", {}).get("h1", {}).get("buys", 0)
                                   + pair.get("txns", {}).get("h1", {}).get("sells", 0)),
            }
        except Exception:
            return None

    async def _get_pool_id(self, deployment_id: int) -> Optional[str]:
        """Look up the pool_id from the deployments table."""
        try:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT pool_id FROM deployments WHERE id=?",
                    (deployment_id,),
                ).fetchone()
            return row["pool_id"] if row and row["pool_id"] else None
        except Exception:
            return None

    async def stop_all_monitors(self) -> None:
        """Cancel all background pool monitoring tasks (call on shutdown)."""
        for dep_id, task in list(self._monitor_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._monitor_tasks.clear()
        logger.info("All pool monitors stopped")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

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
