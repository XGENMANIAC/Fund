"""
pump.fun monitor — uses Playwright to scrape the pump.fun trending page.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
pump.fun's ToS prohibits automated scraping. This code is provided purely for
educational study of how such scrapers would work technically.
Use a personal devnet environment and do not hit pump.fun in automated production loops.

Technical notes:
  - pump.fun is a heavily JS-rendered SPA (Next.js).
  - We use Playwright with a real Chromium browser (not requests/BeautifulSoup).
  - Data is extracted from rendered DOM elements after dynamic load.
  - Alternatively, pump.fun exposes an undocumented websocket at wss://frontend-api.pump.fun
    which emits new coin creation events — we include a WS listener below.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

from utils.logger import get_logger
from utils.helpers import content_hash, load_config, utcnow_iso

logger = get_logger(__name__)

PUMPFUN_URL = "https://pump.fun"
PUMPFUN_API = "https://frontend-api.pump.fun"


@dataclass
class PumpFunAlert:
    """Detection from pump.fun."""

    source: str = "pumpfun"
    token_address: str = ""
    token_name: str = ""
    token_symbol: str = ""
    description: str = ""
    image_uri: str = ""
    creator: str = ""
    created_timestamp: int = 0
    age_hours: float = 0.0
    market_cap_usd: float = 0.0
    reply_count: int = 0
    raydium_pool: str = ""          # Non-empty if already migrated to Raydium
    king_of_hill: bool = False      # True if token is currently on the leaderboard
    detected_at: str = field(default_factory=utcnow_iso)
    raw: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return content_hash("pumpfun", self.token_address)

    @property
    def is_migrated(self) -> bool:
        """True if this pump.fun coin has already graduated to Raydium."""
        return bool(self.raydium_pool)


class PumpFunMonitor:
    """
    Scrapes pump.fun using Playwright and listens to the undocumented WS API.

    Two modes:
      1. Playwright page scraping (slower, catches visual leaderboard).
      2. WebSocket listener (faster, catches all new coin creation events).
    """

    def __init__(self):
        cfg = load_config()
        pf_cfg = cfg.get("monitor", {}).get("pumpfun", {})
        self._scroll_rounds = pf_cfg.get("scroll_rounds", 3)
        self._load_wait_ms = pf_cfg.get("load_wait_ms", 2000)
        self._headless = True
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._playwright = None

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch the Playwright Chromium browser."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",   # crucial in Docker/VPS environments
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        logger.info("Playwright browser started (pump.fun monitor)")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ------------------------------------------------------------------
    # Playwright page scraper
    # ------------------------------------------------------------------

    async def scrape_trending(self) -> list[PumpFunAlert]:
        """
        Scrape the pump.fun main page for trending/recently created coins.
        Returns a list of PumpFunAlert objects.
        """
        if not self._context:
            raise RuntimeError("Browser not started. Use 'async with PumpFunMonitor():'")

        page = await self._context.new_page()
        alerts: list[PumpFunAlert] = []

        try:
            logger.info(f"Navigating to {PUMPFUN_URL}")
            await page.goto(PUMPFUN_URL, wait_until="networkidle", timeout=30_000)
            await page.wait_for_timeout(self._load_wait_ms)

            # Scroll down to load more tokens
            for _ in range(self._scroll_rounds):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1000)

            # Extract token cards from the DOM
            # pump.fun renders cards with data-testid or class patterns
            # These selectors may change with pump.fun UI updates
            cards = await page.query_selector_all("[class*='token-card'], [data-testid='token-card'], a[href*='/coin/']")
            logger.info(f"Found {len(cards)} token cards on pump.fun")

            for card in cards:
                alert = await self._parse_card(card)
                if alert:
                    alerts.append(alert)

        except Exception as exc:
            logger.error(f"pump.fun scrape failed: {exc}")
        finally:
            await page.close()

        return alerts

    async def _parse_card(self, card) -> PumpFunAlert | None:
        """Extract token data from a single pump.fun card element."""
        try:
            # Extract href to get token address
            href = await card.get_attribute("href") or ""
            token_addr = ""
            if "/coin/" in href:
                token_addr = href.split("/coin/")[-1].strip("/").split("?")[0]

            if not token_addr or len(token_addr) < 32:
                return None

            # Try to get text content for name/symbol
            text = await card.inner_text()
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            name = lines[0] if lines else ""
            symbol = ""
            # Symbol often appears in parentheses: "TokenName (SYMBOL)"
            sym_match = re.search(r"\(([A-Z0-9]+)\)", text)
            if sym_match:
                symbol = sym_match.group(1)

            # Market cap if present
            market_cap = 0.0
            mc_match = re.search(r"market cap[:\s]*\$?([\d,\.]+[KMB]?)", text, re.I)
            if mc_match:
                market_cap = self._parse_number_with_suffix(mc_match.group(1))

            return PumpFunAlert(
                token_address=token_addr,
                token_name=name[:32],
                token_symbol=symbol[:10],
                market_cap_usd=market_cap,
                age_hours=0.0,  # Playwright doesn't easily get exact age
            )

        except Exception as exc:
            logger.debug(f"Card parse error: {exc}")
            return None

    # ------------------------------------------------------------------
    # REST API fallback (undocumented but public)
    # ------------------------------------------------------------------

    async def fetch_via_api(self, limit: int = 50) -> list[PumpFunAlert]:
        """
        Use pump.fun's undocumented REST API for richer data.
        Returns recently created coins sorted by creation time.

        Note: This endpoint is not documented and may break without notice.
        """
        import aiohttp

        api_url = f"{PUMPFUN_API}/coins"
        params = {
            "limit": limit,
            "offset": 0,
            "sort": "creation_time",
            "order": "DESC",
            "includeNsfw": "false",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"Accept": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"pump.fun API returned {resp.status}")
                        return []
                    data = await resp.json()
        except Exception as exc:
            logger.error(f"pump.fun API fetch failed: {exc}")
            return []

        alerts = []
        for coin in data if isinstance(data, list) else []:
            alert = self._parse_api_coin(coin)
            if alert:
                alerts.append(alert)

        logger.info(f"pump.fun API: {len(alerts)} coins fetched")
        return alerts

    def _parse_api_coin(self, coin: dict) -> PumpFunAlert | None:
        """Parse a single coin dict from the pump.fun REST API."""
        try:
            mint = coin.get("mint", "")
            if not mint:
                return None

            created_ts = coin.get("created_timestamp", 0)
            age_hours = (time.time() - created_ts / 1000) / 3600 if created_ts else 0

            return PumpFunAlert(
                token_address=mint,
                token_name=coin.get("name", "")[:32],
                token_symbol=coin.get("symbol", "")[:10],
                description=coin.get("description", "")[:256],
                image_uri=coin.get("image_uri", ""),
                creator=coin.get("creator", ""),
                created_timestamp=created_ts,
                age_hours=age_hours,
                market_cap_usd=float(coin.get("usd_market_cap", 0) or 0),
                reply_count=int(coin.get("reply_count", 0) or 0),
                raydium_pool=coin.get("raydium_pool", "") or "",
                king_of_hill=bool(coin.get("king_of_the_hill_timestamp")),
                raw=coin,
            )
        except Exception as exc:
            logger.debug(f"API coin parse error: {exc}")
            return None

    # ------------------------------------------------------------------
    # WebSocket listener (undocumented real-time stream)
    # ------------------------------------------------------------------

    async def listen_websocket(
        self,
        on_new_coin=None,
        on_trade=None,
    ) -> None:
        """
        Connect to pump.fun's real-time WebSocket and stream events.

        Events received:
          - "newCoinCreated": fires when a new coin is deployed to pump.fun
          - "tradeCreated": fires on every trade

        Args:
            on_new_coin: async callback(PumpFunAlert) for new coin events
            on_trade: async callback(dict) for trade events

        Note: The WS endpoint and message format are undocumented and may change.
        This is provided purely for educational understanding of how real-time
        meme coin detection would work in practice.
        """
        import websockets  # type: ignore

        ws_url = "wss://frontend-api.pump.fun"
        retry_delay = 2

        while True:
            try:
                logger.info(f"Connecting to pump.fun WebSocket: {ws_url}")
                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    extra_headers={"Origin": "https://pump.fun"},
                ) as ws:
                    retry_delay = 2  # reset on success

                    # Subscribe to new coin events
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    logger.info("Subscribed to pump.fun new token events")

                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            event_type = msg.get("txType") or msg.get("method", "")

                            if "newCoinCreated" in event_type or msg.get("mint"):
                                alert = self._parse_api_coin(msg)
                                if alert and on_new_coin:
                                    await on_new_coin(alert)

                            elif "tradeCreated" in event_type and on_trade:
                                await on_trade(msg)

                        except json.JSONDecodeError:
                            pass

            except Exception as exc:
                logger.warning(
                    f"pump.fun WS disconnected ({exc}), "
                    f"reconnecting in {retry_delay}s"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_number_with_suffix(value: str) -> float:
        """Parse '1.5K', '2.3M', '500' into float."""
        value = value.replace(",", "").strip()
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        for suffix, mult in multipliers.items():
            if value.upper().endswith(suffix):
                try:
                    return float(value[:-1]) * mult
                except ValueError:
                    return 0.0
        try:
            return float(value)
        except ValueError:
            return 0.0

    async def run_once(self) -> list[PumpFunAlert]:
        """
        Run one poll cycle using REST API (preferred) + page scrape as fallback.
        """
        alerts = await self.fetch_via_api()
        if not alerts:
            logger.info("pump.fun REST API failed, falling back to Playwright scrape")
            alerts = await self.scrape_trending()
        return alerts
