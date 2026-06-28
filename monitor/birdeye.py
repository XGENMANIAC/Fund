"""
Birdeye monitor — scrapes birdeye.so for trending Solana tokens.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
Birdeye ToS: https://birdeye.so/terms. This code demonstrates how automated
scraping of token discovery sites works. Do not use in production loops that
would violate Birdeye's ToS.

Two modes:
  1. Public API (no key) — /defi/tokenlist endpoint is rate-limited but accessible.
  2. Playwright scrape — for the Discover/Trending page when API is unavailable.

Birdeye public API base: https://public-api.birdeye.so
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from utils.logger import get_logger
from utils.helpers import content_hash, load_config, utcnow_iso

logger = get_logger(__name__)

BIRDEYE_API = "https://public-api.birdeye.so"
BIRDEYE_URL = "https://birdeye.so"


@dataclass
class BirdeyeAlert:
    """Detection from Birdeye."""

    source: str = "birdeye"
    token_address: str = ""
    token_name: str = ""
    token_symbol: str = ""
    chain: str = "solana"
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_24h: float = 0.0
    price_change_24h: float = 0.0
    market_cap_usd: float = 0.0
    holders: int = 0
    age_hours: float = 0.0
    # Metadata for mimicry
    image_uri: str = ""
    metadata_uri: str = ""
    description: str = ""
    # Birdeye-specific
    birdeye_url: str = ""
    detected_at: str = field(default_factory=utcnow_iso)
    raw: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return content_hash("birdeye", self.token_address)


class BirdeyeMonitor:
    """
    Polls Birdeye for trending/new Solana tokens.

    Usage:
        monitor = BirdeyeMonitor()
        async with monitor:
            alerts = await monitor.run_once()
    """

    def __init__(self):
        cfg = load_config()
        be_cfg = cfg.get("monitor", {}).get("birdeye", {})
        filters = cfg.get("filters", {})

        self._min_liquidity = filters.get("min_liquidity_usd", 1000)
        self._max_results = be_cfg.get("max_results", 50)
        self._api_key = be_cfg.get("api_key", "")  # optional — works without for basic endpoints
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (educational-research-bot)",
        }
        if self._api_key:
            headers["X-API-KEY"] = self._api_key

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers=headers,
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # API methods (no API key needed for basic tokenlist)
    # ------------------------------------------------------------------

    async def fetch_trending(self) -> list[BirdeyeAlert]:
        """
        Fetch trending tokens by 24h volume via Birdeye's public tokenlist API.
        No API key needed for this endpoint — rate limited to ~60 req/min.
        """
        params = {
            "sort_by": "v24hUSD",
            "sort_type": "desc",
            "offset": 0,
            "limit": self._max_results,
            "min_liquidity": max(self._min_liquidity, 1000),
        }

        data = await self._get("/defi/tokenlist", params=params)
        if not data:
            return []

        tokens = data.get("data", {}).get("tokens", [])
        alerts = [self._parse_token(t) for t in tokens]
        alerts = [a for a in alerts if a is not None]

        logger.info(f"Birdeye: {len(alerts)} trending tokens from tokenlist API")
        return alerts

    async def fetch_new_listings(self) -> list[BirdeyeAlert]:
        """
        Fetch recently listed tokens sorted by creation time.
        """
        params = {
            "sort_by": "mc",
            "sort_type": "asc",
            "offset": 0,
            "limit": self._max_results,
            "min_liquidity": 500,
        }

        data = await self._get("/defi/tokenlist", params=params)
        if not data:
            return []

        tokens = data.get("data", {}).get("tokens", [])
        alerts = [self._parse_token(t) for t in tokens]
        alerts = [a for a in alerts if a is not None]

        logger.info(f"Birdeye: {len(alerts)} new listings")
        return alerts

    async def fetch_token_overview(self, token_address: str) -> Optional[BirdeyeAlert]:
        """
        Fetch detailed overview for a single token including metadata URI.
        Requires API key for detailed fields.
        """
        data = await self._get(
            "/defi/token_overview",
            params={"address": token_address},
        )
        if not data or not data.get("data"):
            return None

        return self._parse_overview(data["data"], token_address)

    # ------------------------------------------------------------------
    # Playwright scrape fallback
    # ------------------------------------------------------------------

    async def scrape_discover_page(self) -> list[BirdeyeAlert]:
        """
        Scrape Birdeye's /discover/token page using Playwright.
        Used when the API returns errors or we want the visual trending list.

        DISCLAIMER: This bypasses the API and directly scrapes rendered HTML.
        Educational/research use only.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed — skipping Birdeye scrape")
            return []

        alerts: list[BirdeyeAlert] = []
        url = f"{BIRDEYE_URL}/discover/token"

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()

                logger.info(f"Navigating to {url}")
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                await page.wait_for_timeout(3000)

                # Birdeye renders a table of tokens — extract rows
                # Selectors may break with UI updates (typical scraping caveat)
                rows = await page.query_selector_all("tr[class*='token-row'], tbody tr")

                if not rows:
                    # Try alternate: look for links to /token/<address>
                    links = await page.query_selector_all("a[href*='/token/So']")
                    links += await page.query_selector_all("a[href*='/token/E']")
                    for link in links[:self._max_results]:
                        href = await link.get_attribute("href") or ""
                        addr = href.split("/token/")[-1].split("?")[0] if "/token/" in href else ""
                        if addr and len(addr) > 32:
                            text = await link.inner_text()
                            lines = [l.strip() for l in text.split("\n") if l.strip()]
                            alert = BirdeyeAlert(
                                token_address=addr,
                                token_name=lines[0] if lines else addr[:8],
                                token_symbol=lines[1] if len(lines) > 1 else "",
                                birdeye_url=f"{BIRDEYE_URL}/token/{addr}",
                            )
                            alerts.append(alert)
                else:
                    for row in rows[:self._max_results]:
                        try:
                            text = await row.inner_text()
                            # Try to get the address from a link in the row
                            link = await row.query_selector("a[href*='/token/']")
                            href = await link.get_attribute("href") if link else ""
                            addr = (
                                href.split("/token/")[-1].split("?")[0]
                                if href and "/token/" in href
                                else ""
                            )
                            if addr and len(addr) > 32:
                                lines = [l.strip() for l in text.split("\n") if l.strip()]
                                alerts.append(
                                    BirdeyeAlert(
                                        token_address=addr,
                                        token_name=lines[0] if lines else "",
                                        birdeye_url=f"{BIRDEYE_URL}/token/{addr}",
                                    )
                                )
                        except Exception:
                            pass

                await browser.close()

        except Exception as exc:
            logger.error(f"Birdeye Playwright scrape failed: {exc}")

        logger.info(f"Birdeye scrape: {len(alerts)} tokens found")
        return alerts

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_token(self, token: dict) -> Optional[BirdeyeAlert]:
        """Parse a token dict from the Birdeye tokenlist endpoint."""
        try:
            addr = token.get("address", "")
            if not addr:
                return None

            liquidity = float(token.get("liquidity", 0) or 0)
            if liquidity < self._min_liquidity:
                return None

            return BirdeyeAlert(
                token_address=addr,
                token_name=token.get("name", ""),
                token_symbol=token.get("symbol", ""),
                price_usd=float(token.get("price", 0) or 0),
                liquidity_usd=liquidity,
                volume_24h=float(token.get("v24hUSD", 0) or 0),
                price_change_24h=float(token.get("v24hChangePercent", 0) or 0),
                market_cap_usd=float(token.get("mc", 0) or 0),
                holders=int(token.get("holder", 0) or 0),
                image_uri=token.get("logoURI", ""),
                birdeye_url=f"{BIRDEYE_URL}/token/{addr}",
                raw=token,
            )
        except Exception as exc:
            logger.debug(f"Birdeye token parse error: {exc}")
            return None

    def _parse_overview(self, data: dict, token_address: str) -> BirdeyeAlert:
        """Parse the detailed token overview response."""
        extensions = data.get("extensions", {})
        return BirdeyeAlert(
            token_address=token_address,
            token_name=data.get("name", ""),
            token_symbol=data.get("symbol", ""),
            price_usd=float(data.get("price", 0) or 0),
            liquidity_usd=float(data.get("liquidity", 0) or 0),
            volume_24h=float(data.get("v24hUSD", 0) or 0),
            price_change_24h=float(data.get("priceChange24hPercent", 0) or 0),
            market_cap_usd=float(data.get("mc", 0) or 0),
            holders=int(data.get("holder", 0) or 0),
            image_uri=data.get("logoURI", ""),
            description=extensions.get("description", ""),
            metadata_uri=extensions.get("coingeckoId", ""),  # or website
            birdeye_url=f"{BIRDEYE_URL}/token/{token_address}",
            raw=data,
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get(self, endpoint: str, params: dict | None = None) -> Any:
        """Make an authenticated GET request to Birdeye API."""
        if not self._session:
            return None

        url = f"{BIRDEYE_API}{endpoint}"
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 429:
                    logger.warning("Birdeye rate limit hit — sleeping 15s")
                    await asyncio.sleep(15)
                    return None
                if resp.status == 401:
                    logger.warning("Birdeye API key missing or invalid")
                    return None
                if resp.status != 200:
                    logger.warning(f"Birdeye returned {resp.status} for {endpoint}")
                    return None
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error(f"Birdeye request failed: {exc}")
            return None

    # ------------------------------------------------------------------
    # Run cycle
    # ------------------------------------------------------------------

    async def run_once(self) -> list[BirdeyeAlert]:
        """
        One full detection cycle.
        Tries API first, falls back to Playwright scrape.
        """
        alerts = await self.fetch_trending()

        if not alerts:
            logger.info("Birdeye API returned no data, trying Playwright scrape")
            alerts = await self.scrape_discover_page()

        # De-duplicate by address within this cycle
        seen: set[str] = set()
        unique = []
        for alert in alerts:
            if alert.token_address not in seen:
                seen.add(alert.token_address)
                unique.append(alert)

        return unique
