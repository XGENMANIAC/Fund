"""
Monitor aggregator — combines outputs from all detection sources.

DISCLAIMER: Educational system for Solana devnet only.

Responsibilities:
  1. Run all configured monitors concurrently.
  2. Deduplicate detections across sources (same token from DexScreener + pump.fun).
  3. Merge social signals into detections (boost score if address mentioned socially).
  4. Persist detections to SQLite.
  5. Return a clean, deduplicated list of TokenDetection objects for the analysis module.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from monitor.dexscreener import DexScreenerMonitor, DexTokenAlert
from monitor.pumpfun import PumpFunMonitor, PumpFunAlert
from monitor.social import SocialMonitor, aggregate_address_mentions
from monitor.onchain import OnChainMonitor, OnChainAlert
from storage.database import insert_detection, initialize_db
from utils.logger import get_logger
from utils.helpers import content_hash, load_config, utcnow_iso

logger = get_logger(__name__)


@dataclass
class TokenDetection:
    """
    Unified detection record — the single data structure passed downstream
    to the analysis and scoring module.
    """

    # Identification
    token_address: str = ""
    token_name: str = ""
    token_symbol: str = ""
    chain: str = "solana"

    # Sources that detected this token in this cycle
    sources: list[str] = field(default_factory=list)

    # Market metrics (best values across all sources)
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    volume_24h: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0

    # Token metadata
    age_hours: float = 0.0
    holders: int = 0
    social_mentions: int = 0

    # pump.fun specific
    market_cap_usd: float = 0.0
    description: str = ""
    image_uri: str = ""
    is_pumpfun: bool = False
    is_graduated: bool = False  # True if migrated to Raydium

    # On-chain
    pool_address: str = ""
    on_chain_confirmed: bool = False

    # Metadata
    detected_at: str = field(default_factory=utcnow_iso)
    db_id: Optional[int] = None  # Set after DB insert

    @property
    def source_string(self) -> str:
        return "+".join(sorted(set(self.sources)))

    @property
    def content_hash(self) -> str:
        return content_hash(self.token_address, self.chain)


class MonitorAggregator:
    """
    Runs all monitors concurrently and returns deduplicated TokenDetection objects.

    Usage:
        aggregator = MonitorAggregator()
        async with aggregator:
            detections = await aggregator.run_cycle()
            for d in detections:
                print(d.token_address, d.score)
    """

    def __init__(self):
        cfg = load_config()
        monitor_cfg = cfg.get("monitor", {})
        self._enable_dexscreener = monitor_cfg.get("enable_dexscreener", True)
        self._enable_pumpfun = monitor_cfg.get("enable_pumpfun", True)
        self._enable_social = monitor_cfg.get("enable_social", True)
        self._enable_onchain = monitor_cfg.get("enable_onchain", False)  # WS optional

        self._dex_monitor: Optional[DexScreenerMonitor] = None
        self._pf_monitor: Optional[PumpFunMonitor] = None
        self._social_monitor: Optional[SocialMonitor] = None
        self._onchain_monitor: Optional[OnChainMonitor] = None

        # Deduplication: track tokens seen this cycle
        self._seen_this_cycle: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self):
        initialize_db()  # ensure tables exist

        if self._enable_dexscreener:
            self._dex_monitor = DexScreenerMonitor()
            await self._dex_monitor.__aenter__()

        if self._enable_pumpfun:
            self._pf_monitor = PumpFunMonitor()
            await self._pf_monitor.__aenter__()

        if self._enable_social:
            self._social_monitor = SocialMonitor()
            await self._social_monitor.__aenter__()

        if self._enable_onchain:
            self._onchain_monitor = OnChainMonitor()
            await self._onchain_monitor.start()

        return self

    async def __aexit__(self, *args):
        if self._dex_monitor:
            await self._dex_monitor.__aexit__(*args)
        if self._pf_monitor:
            await self._pf_monitor.__aexit__(*args)
        if self._social_monitor:
            await self._social_monitor.__aexit__(*args)
        if self._onchain_monitor:
            await self._onchain_monitor.stop()

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    async def run_cycle(self) -> list[TokenDetection]:
        """
        Run one full detection cycle:
          1. Fire all monitors concurrently.
          2. Deduplicate + merge.
          3. Persist to DB.
          4. Return unified list.
        """
        self._seen_this_cycle.clear()

        # Run all enabled monitors concurrently
        tasks = {}
        if self._dex_monitor:
            tasks["dex"] = asyncio.create_task(self._dex_monitor.run_once())
        if self._pf_monitor:
            tasks["pf"] = asyncio.create_task(self._pf_monitor.run_once())
        if self._social_monitor:
            tasks["social"] = asyncio.create_task(self._social_monitor.run_once())
        if self._onchain_monitor:
            tasks["onchain"] = asyncio.create_task(
                self._onchain_monitor.get_alerts(max_wait=0.5)
            )

        results = {}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except Exception as exc:
                logger.error(f"Monitor '{name}' failed: {exc}")
                results[name] = []

        # --- Process social signals first (used to boost other detections) ---
        social_summaries = results.get("social", [])
        address_mentions = aggregate_address_mentions(social_summaries)

        # --- Build detection map: address -> TokenDetection ---
        detection_map: dict[str, TokenDetection] = {}

        # DexScreener detections
        for alert in results.get("dex", []):
            d = self._from_dex_alert(alert)
            self._merge_or_add(detection_map, d)

        # pump.fun detections
        for alert in results.get("pf", []):
            d = self._from_pf_alert(alert)
            self._merge_or_add(detection_map, d)

        # On-chain detections
        for alert in results.get("onchain", []):
            if alert.token_address:
                d = self._from_onchain_alert(alert)
                self._merge_or_add(detection_map, d)

        # Inject social mention counts
        for addr, count in address_mentions.items():
            if addr in detection_map:
                detection_map[addr].social_mentions += count
            # If a socially-mentioned address isn't in our map yet, we could add it
            # but we'd have no market data — skip for now.

        # --- Persist to DB ---
        detections = list(detection_map.values())
        for det in detections:
            try:
                db_id = insert_detection(
                    source=det.source_string,
                    token_address=det.token_address,
                    content_hash=det.content_hash,
                    token_name=det.token_name,
                    token_symbol=det.token_symbol,
                    price_usd=det.price_usd,
                    liquidity_usd=det.liquidity_usd,
                    volume_5m=det.volume_5m,
                    volume_1h=det.volume_1h,
                    volume_24h=det.volume_24h,
                    price_change_5m=det.price_change_5m,
                    price_change_1h=det.price_change_1h,
                    holders=det.holders,
                    age_hours=det.age_hours,
                    social_mentions=det.social_mentions,
                )
                det.db_id = db_id
            except Exception as exc:
                logger.error(f"DB insert failed for {det.token_address}: {exc}")

        new_count = sum(1 for d in detections if d.db_id is not None)
        logger.info(
            f"Cycle complete: {len(detections)} tokens detected, "
            f"{new_count} new (not seen before)"
        )
        return detections

    # ------------------------------------------------------------------
    # Source-specific parsers
    # ------------------------------------------------------------------

    def _from_dex_alert(self, alert: DexTokenAlert) -> TokenDetection:
        return TokenDetection(
            token_address=alert.token_address,
            token_name=alert.token_name,
            token_symbol=alert.token_symbol,
            sources=["dexscreener"],
            price_usd=alert.price_usd,
            liquidity_usd=alert.liquidity_usd,
            volume_5m=alert.volume_5m,
            volume_1h=alert.volume_1h,
            volume_24h=alert.volume_24h,
            price_change_5m=alert.price_change_5m,
            price_change_1h=alert.price_change_1h,
            age_hours=alert.age_hours,
            pool_address=alert.pair_address,
        )

    def _from_pf_alert(self, alert: PumpFunAlert) -> TokenDetection:
        return TokenDetection(
            token_address=alert.token_address,
            token_name=alert.token_name,
            token_symbol=alert.token_symbol,
            sources=["pumpfun"],
            market_cap_usd=alert.market_cap_usd,
            description=alert.description,
            image_uri=alert.image_uri,
            age_hours=alert.age_hours,
            is_pumpfun=True,
            is_graduated=alert.is_migrated,
            pool_address=alert.raydium_pool,
        )

    def _from_onchain_alert(self, alert: OnChainAlert) -> TokenDetection:
        return TokenDetection(
            token_address=alert.token_address,
            sources=["onchain"],
            pool_address=alert.pool_address,
            on_chain_confirmed=True,
        )

    def _merge_or_add(
        self,
        detection_map: dict[str, TokenDetection],
        new: TokenDetection,
    ) -> None:
        """
        If we already have this token, merge the new detection's data into it.
        Otherwise add it fresh. Takes the best (highest) numeric values.
        """
        addr = new.token_address
        if not addr:
            return

        if addr not in detection_map:
            detection_map[addr] = new
            return

        existing = detection_map[addr]

        # Merge sources list
        existing.sources = list(set(existing.sources + new.sources))

        # Prefer higher market data (more sources = better picture)
        existing.liquidity_usd = max(existing.liquidity_usd, new.liquidity_usd)
        existing.volume_5m = max(existing.volume_5m, new.volume_5m)
        existing.volume_1h = max(existing.volume_1h, new.volume_1h)
        existing.volume_24h = max(existing.volume_24h, new.volume_24h)
        existing.holders = max(existing.holders, new.holders)
        existing.social_mentions += new.social_mentions

        # Fill in missing name/symbol
        if not existing.token_name and new.token_name:
            existing.token_name = new.token_name
        if not existing.token_symbol and new.token_symbol:
            existing.token_symbol = new.token_symbol
        if not existing.description and new.description:
            existing.description = new.description
        if not existing.pool_address and new.pool_address:
            existing.pool_address = new.pool_address

        # On-chain confirmation from any source
        existing.on_chain_confirmed = existing.on_chain_confirmed or new.on_chain_confirmed
        existing.is_graduated = existing.is_graduated or new.is_graduated
