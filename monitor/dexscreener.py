"""
DexScreener monitor — uses the free, public DexScreener API.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
DexScreener ToS: https://dexscreener.com/terms. This uses their public API
endpoints which are not rate-restricted for reasonable use.

Key endpoints used (no API key required):
  - /latest/dex/tokens/recently-added      — newest Solana tokens
  - /latest/dex/search/?q=<symbol>         — search by symbol/name
  - /latest/dex/pairs/solana/<pair_addr>   — specific pair data

Limitations:
  - Data is ~30 seconds behind real-time on-chain state.
  - Rate limit: ~300 requests/minute from a single IP.
  - Some new pairs may not appear until they have at least one trade.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from utils.logger import get_logger
from utils.helpers import (
    content_hash,
    format_usd,
    hours_ago,
    load_config,
    utcnow_iso,
)

logger = get_logger(__name__)

BASE_URL = "https://api.dexscreener.com"


@dataclass
class DexTokenAlert:
    """Structured detection output from DexScreener."""

    source: str = "dexscreener"
    token_address: str = ""
    pair_address: str = ""
    token_name: str = ""
    token_symbol: str = ""
    chain: str = "solana"
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    volume_24h: float = 0.0
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    holders: int = 0
    age_hours: float = 0.0
    social_mentions: int = 0
    dex_url: str = ""
    # v2: metadata fields for mimicry
    image_uri: str = ""
    metadata_uri: str = ""
    original_description: str = ""
    detected_at: str = field(default_factory=utcnow_iso)
    raw: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return content_hash("dexscreener", self.token_address, self.pair_address)


class DexScreenerMonitor:
    """
    Polls DexScreener's free public API for new Solana tokens.

    Usage:
        monitor = DexScreenerMonitor()
        async for alert in monitor.stream():
            print(alert)
    """

    def __init__(self):
        cfg = load_config()
        dex_cfg = cfg.get("monitor", {}).get("dexscreener", {})
        self._base_url = dex_cfg.get("base_url", BASE_URL)
        self._max_results = dex_cfg.get("max_results", 50)
        filters = cfg.get("filters", {})
        self._min_liquidity = filters.get("min_liquidity_usd", 1000)
        self._min_volume_5m = filters.get("min_volume_5m", 100)
        self._max_age_hours = filters.get("max_age_hours", 4)
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "Mozilla/5.0 (educational-research-bot)"},
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def _get(self, endpoint: str, params: dict | None = None) -> Any:
        """Make a GET request, return parsed JSON or None on error."""
        url = f"{self._base_url}{endpoint}"
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 429:
                    logger.warning("DexScreener rate limit hit — sleeping 10s")
                    await asyncio.sleep(10)
                    return None
                if resp.status != 200:
                    logger.warning(f"DexScreener returned {resp.status} for {url}")
                    return None
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error(f"DexScreener request failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def fetch_recently_added(self) -> list[DexTokenAlert]:
        """
        Fetch recently added Solana pairs.
        Returns a filtered list of DexTokenAlert objects.
        """
        data = await self._get("/latest/dex/tokens/recently-added")
        if not data:
            return []

        pairs = data if isinstance(data, list) else data.get("pairs", [])
        alerts = []

        for pair in pairs[: self._max_results]:
            alert = self._parse_pair(pair)
            if alert and self._passes_basic_filter(alert):
                alerts.append(alert)

        logger.info(
            f"DexScreener: {len(alerts)} new alerts from "
            f"{len(pairs)} pairs (recently-added)"
        )
        return alerts

    async def fetch_trending_solana(self) -> list[DexTokenAlert]:
        """
        Fetch currently trending Solana pairs by searching for 'solana'.
        Useful for catching coins gaining momentum rather than just new ones.
        """
        data = await self._get("/latest/dex/search/", params={"q": "solana"})
        if not data:
            return []

        pairs = data.get("pairs", [])
        # Filter to Solana chain only (results may include other chains)
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        alerts = []
        for pair in sol_pairs[: self._max_results]:
            alert = self._parse_pair(pair)
            if alert and self._passes_basic_filter(alert):
                alerts.append(alert)

        logger.info(f"DexScreener: {len(alerts)} trending Solana alerts")
        return alerts

    async def fetch_pair(self, pair_address: str) -> DexTokenAlert | None:
        """Fetch a specific pair by address — used for post-launch monitoring."""
        data = await self._get(f"/latest/dex/pairs/solana/{pair_address}")
        if not data:
            return None
        pairs = data.get("pairs", [])
        if pairs:
            return self._parse_pair(pairs[0])
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_pair(self, pair: dict) -> DexTokenAlert | None:
        """
        Parse a DexScreener pair dict into a DexTokenAlert.
        Returns None if essential fields are missing.
        """
        try:
            base = pair.get("baseToken", {})
            token_addr = base.get("address", "")
            if not token_addr:
                return None

            # Age calculation: pairCreatedAt is Unix ms
            created_ms = pair.get("pairCreatedAt", 0)
            age_hours = 0.0
            if created_ms:
                age_hours = (time.time() - created_ms / 1000) / 3600

            volume = pair.get("volume", {})
            price_change = pair.get("priceChange", {})
            liquidity = pair.get("liquidity", {})

            # v2: extract image, description, metadata from the info block
            info = pair.get("info", {})
            image_uri = info.get("imageUrl", "")
            # DexScreener sometimes puts description in info.description
            original_description = info.get("description", "")
            # metadata_uri: first website URL if available (often points to token page)
            websites = info.get("websites", [])
            metadata_uri = websites[0].get("url", "") if websites else ""

            return DexTokenAlert(
                token_address=token_addr,
                pair_address=pair.get("pairAddress", ""),
                token_name=base.get("name", ""),
                token_symbol=base.get("symbol", ""),
                price_usd=float(pair.get("priceUsd", 0) or 0),
                liquidity_usd=float(liquidity.get("usd", 0) or 0),
                volume_5m=float(volume.get("m5", 0) or 0),
                volume_1h=float(volume.get("h1", 0) or 0),
                volume_24h=float(volume.get("h24", 0) or 0),
                price_change_5m=float(price_change.get("m5", 0) or 0),
                price_change_1h=float(price_change.get("h1", 0) or 0),
                price_change_24h=float(price_change.get("h24", 0) or 0),
                age_hours=age_hours,
                dex_url=pair.get("url", ""),
                image_uri=image_uri,
                metadata_uri=metadata_uri,
                original_description=original_description,
                raw=pair,
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.debug(f"Failed to parse DexScreener pair: {exc}")
            return None

    def _passes_basic_filter(self, alert: DexTokenAlert) -> bool:
        """Quick pre-score filter to avoid sending low-quality data downstream."""
        if alert.liquidity_usd < self._min_liquidity:
            return False
        if alert.volume_5m < self._min_volume_5m:
            return False
        if self._max_age_hours > 0 and alert.age_hours > self._max_age_hours:
            return False
        return True

    # ------------------------------------------------------------------
    # Streaming generator
    # ------------------------------------------------------------------

    async def run_once(self) -> list[DexTokenAlert]:
        """
        Run one poll cycle, returning all alerts from both endpoints.
        De-duplicates by token_address within the cycle.
        """
        seen = set()
        alerts = []

        for batch in [
            await self.fetch_recently_added(),
            await self.fetch_trending_solana(),
        ]:
            for alert in batch:
                if alert.token_address not in seen:
                    seen.add(alert.token_address)
                    alerts.append(alert)

        return alerts
