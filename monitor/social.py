"""
Social media monitor — scrapes public X/Twitter search results via Playwright.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
Scraping X/Twitter may violate their Terms of Service. This code is provided
purely for educational study of how social signal detection works technically.
Consider using the official Twitter API v2 (free tier: 500k tweets/month) instead.

Technical notes:
  - X requires login for most search features. We use the public Nitter mirrors
    (open-source Twitter front-end) which don't require authentication.
  - Nitter instances are community-run and may be unavailable at any time.
  - Alternative: search Google for "site:twitter.com <term>" via Playwright.
  - The social score is one of the lower-weight signals (0.15 weight by default).
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Browser, BrowserContext

from utils.logger import get_logger
from utils.helpers import content_hash, load_config, utcnow_iso

logger = get_logger(__name__)

# Public Nitter instances (community-run, no auth required)
# These may go offline — the code falls back down the list.
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.1d4.us",
]


@dataclass
class SocialMention:
    """A social media mention of a potential meme coin."""

    source: str = "social"
    search_term: str = ""
    platform: str = "twitter"
    text: str = ""
    author: str = ""
    timestamp: str = ""
    url: str = ""
    # Extracted token info (if found in text)
    mentioned_address: str = ""
    mentioned_symbol: str = ""
    detected_at: str = field(default_factory=utcnow_iso)

    @property
    def content_hash(self) -> str:
        return content_hash("social", self.url, self.search_term)


@dataclass
class SocialSignalSummary:
    """Aggregated social signal for a token or search term."""

    search_term: str = ""
    mention_count: int = 0
    unique_authors: int = 0
    earliest_mention: str = ""
    latest_mention: str = ""
    mentions: list[SocialMention] = field(default_factory=list)
    extracted_addresses: list[str] = field(default_factory=list)


class SocialMonitor:
    """
    Scrapes public Nitter instances and other social proxies for token mentions.

    Provides signals for:
      - How many people are talking about a specific token
      - Whether contract addresses are being shared
      - Velocity of mentions (growing vs stable)
    """

    def __init__(self):
        cfg = load_config()
        social_cfg = cfg.get("monitor", {}).get("social", {})
        self._search_terms = social_cfg.get("search_terms", [
            "solana meme coin launch",
            "$SOL new token",
            "just launched solana",
        ])
        self._results_per_term = social_cfg.get("results_per_term", 20)
        self._min_mentions = social_cfg.get("min_mentions", 3)
        self._headless = True
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._playwright = None
        self._nitter_idx = 0  # tracks which instance to use

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        return self

    async def __aexit__(self, *args):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ------------------------------------------------------------------
    # Core search
    # ------------------------------------------------------------------

    async def search_term(self, term: str) -> SocialSignalSummary:
        """
        Search Nitter for a given term and return aggregated social signal.
        Falls back through available Nitter instances on failure.
        """
        summary = SocialSignalSummary(search_term=term)
        mentions = await self._try_nitter_search(term)

        if not mentions:
            # Final fallback: Google search for twitter mentions
            mentions = await self._google_search_fallback(term)

        summary.mentions = mentions
        summary.mention_count = len(mentions)
        summary.unique_authors = len({m.author for m in mentions if m.author})

        addresses = []
        for m in mentions:
            addrs = _extract_solana_addresses(m.text)
            addresses.extend(addrs)
            if addrs:
                m.mentioned_address = addrs[0]
        summary.extracted_addresses = list(set(addresses))

        if mentions:
            times = [m.timestamp for m in mentions if m.timestamp]
            if times:
                summary.earliest_mention = min(times)
                summary.latest_mention = max(times)

        return summary

    async def run_once(self) -> list[SocialSignalSummary]:
        """
        Run one search cycle for all configured search terms.
        Returns summaries with at least min_mentions.
        """
        results = []
        for term in self._search_terms:
            try:
                summary = await self.search_term(term)
                if summary.mention_count >= self._min_mentions:
                    results.append(summary)
                logger.info(
                    f"Social [{term}]: {summary.mention_count} mentions, "
                    f"{summary.unique_authors} unique authors"
                )
            except Exception as exc:
                logger.error(f"Social search failed for '{term}': {exc}")

        return results

    # ------------------------------------------------------------------
    # Nitter scraping
    # ------------------------------------------------------------------

    async def _try_nitter_search(self, term: str) -> list[SocialMention]:
        """Try each Nitter instance in order until one succeeds."""
        for i in range(len(NITTER_INSTANCES)):
            instance_idx = (self._nitter_idx + i) % len(NITTER_INSTANCES)
            instance = NITTER_INSTANCES[instance_idx]
            try:
                mentions = await self._scrape_nitter(instance, term)
                if mentions is not None:
                    self._nitter_idx = instance_idx  # stick to working instance
                    return mentions
            except Exception as exc:
                logger.debug(f"Nitter instance {instance} failed: {exc}")
        return []

    async def _scrape_nitter(self, base_url: str, term: str) -> list[SocialMention] | None:
        """
        Scrape a Nitter search results page.
        Returns None if the instance is unreachable.
        """
        if not self._context:
            return []

        # Nitter search URL format: /search?q=<term>&f=tweets&since=24h
        params = urlencode({"q": term, "f": "tweets"})
        url = f"{base_url}/search?{params}"

        page = await self._context.new_page()
        mentions = []

        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            if not response or response.status != 200:
                return None

            await page.wait_for_timeout(1500)

            # Nitter tweet items are in .timeline-item containers
            tweet_elements = await page.query_selector_all(".timeline-item")

            for el in tweet_elements[: self._results_per_term]:
                mention = await self._parse_nitter_tweet(el, base_url, term)
                if mention:
                    mentions.append(mention)

        except Exception as exc:
            logger.debug(f"Nitter scrape error ({base_url}): {exc}")
            return None
        finally:
            await page.close()

        return mentions

    async def _parse_nitter_tweet(self, el, base_url: str, search_term: str) -> SocialMention | None:
        """Parse a single Nitter tweet element."""
        try:
            # Tweet text
            content_el = await el.query_selector(".tweet-content")
            text = await content_el.inner_text() if content_el else ""
            if not text:
                return None

            # Author
            author_el = await el.query_selector(".username")
            author = await author_el.inner_text() if author_el else ""

            # Tweet link
            link_el = await el.query_selector("a.tweet-link")
            tweet_href = await link_el.get_attribute("href") if link_el else ""
            tweet_url = f"{base_url}{tweet_href}" if tweet_href else ""

            # Timestamp
            time_el = await el.query_selector("span.tweet-date a")
            timestamp = await time_el.get_attribute("title") if time_el else ""

            return SocialMention(
                search_term=search_term,
                text=text.strip(),
                author=author.strip().lstrip("@"),
                timestamp=timestamp,
                url=tweet_url,
            )
        except Exception as exc:
            logger.debug(f"Nitter tweet parse error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Google fallback
    # ------------------------------------------------------------------

    async def _google_search_fallback(self, term: str) -> list[SocialMention]:
        """
        Search Google for Twitter mentions of the term as a last resort.
        Much less reliable but doesn't require a specific Nitter instance.
        """
        if not self._context:
            return []

        query = f"site:twitter.com {term}"
        url = f"https://www.google.com/search?q={urlencode({'q': query})}&num=20"

        page = await self._context.new_page()
        mentions = []

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(2000)

            # Google search results contain .g elements with title+snippet
            results = await page.query_selector_all(".g")
            for result in results[: self._results_per_term]:
                try:
                    title_el = await result.query_selector("h3")
                    snippet_el = await result.query_selector(".VwiC3b")
                    title = await title_el.inner_text() if title_el else ""
                    snippet = await snippet_el.inner_text() if snippet_el else ""
                    text = f"{title} {snippet}".strip()
                    if text:
                        mentions.append(SocialMention(
                            search_term=term,
                            platform="twitter_via_google",
                            text=text,
                        ))
                except Exception:
                    pass

        except Exception as exc:
            logger.debug(f"Google fallback failed: {exc}")
        finally:
            await page.close()

        return mentions


# ---------------------------------------------------------------------------
# Utility: extract Solana token addresses from free text
# ---------------------------------------------------------------------------

# Solana addresses are base58, 32–44 chars, no 0/O/I/l
SOLANA_ADDR_PATTERN = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def _extract_solana_addresses(text: str) -> list[str]:
    """Extract all plausible Solana addresses from a text string."""
    candidates = SOLANA_ADDR_PATTERN.findall(text)
    # Further filter: must start with a letter (not a number for common false positives)
    return [c for c in candidates if len(c) >= 32]


def aggregate_address_mentions(summaries: list[SocialSignalSummary]) -> dict[str, int]:
    """
    Across all social summaries, count how many times each address was mentioned.
    Used to boost the score of a token if its address is spreading organically.
    """
    counts: dict[str, int] = {}
    for summary in summaries:
        for addr in summary.extracted_addresses:
            counts[addr] = counts.get(addr, 0) + 1
    return counts
