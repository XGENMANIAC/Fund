"""
Copying viability scorer — separate from the virality score.

DISCLAIMER: Educational system for Solana devnet only.

The copy_viability score (0–100) answers:
  "How well can we clone this token with high fidelity?"

It's a prerequisite check BEFORE running the mimicry engine, measuring whether
we have enough raw material (image, description, metadata) to produce a
convincing educational clone.

Sub-scores (each 0–25):
  - image_score:       25 if a fetchable image URL exists
  - description_score: 25 if a non-trivial description exists (>20 chars)
  - metadata_score:    25 if a fetchable metadata URI exists
  - name_score:        25 based on name quality (length, non-generic)

Tokens with copy_viability < 40 are still processed but flagged as low-mimicry.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CopyViabilityResult:
    """Detailed copy viability assessment for one token."""

    token_address: str = ""
    token_name: str = ""
    token_symbol: str = ""

    # Sub-scores (0–25 each)
    image_score: float = 0.0
    description_score: float = 0.0
    metadata_score: float = 0.0
    name_score: float = 0.0

    # Boolean flags (stored to DB)
    has_image: bool = False
    has_description: bool = False
    has_metadata_uri: bool = False
    metadata_fetchable: bool = False

    # Composite
    copy_viability: float = 0.0

    # What we found
    image_uri: str = ""
    description: str = ""
    metadata_uri: str = ""

    notes: list[str] = field(default_factory=list)


class CopyScorer:
    """
    Evaluates how "clone-ready" a detected token is.

    Usage:
        scorer = CopyScorer()
        async with scorer:
            result = await scorer.score(
                token_address="...",
                token_name="BONK",
                token_symbol="BONK",
                image_uri="https://...",
                description="The original bonk dog",
                metadata_uri="https://arweave.net/...",
            )
        print(result.copy_viability)
    """

    def __init__(self, probe_timeout: float = 5.0):
        self._timeout = probe_timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Main scoring method
    # ------------------------------------------------------------------

    async def score(
        self,
        token_address: str = "",
        token_name: str = "",
        token_symbol: str = "",
        image_uri: str = "",
        description: str = "",
        metadata_uri: str = "",
    ) -> CopyViabilityResult:
        """
        Score a token's copying viability.
        Performs async HEAD requests to check URI reachability.
        """
        result = CopyViabilityResult(
            token_address=token_address,
            token_name=token_name,
            token_symbol=token_symbol,
            image_uri=image_uri,
            description=description,
            metadata_uri=metadata_uri,
        )

        # Run sub-scores concurrently
        img_task = asyncio.create_task(self._score_image(image_uri, result))
        desc_task = asyncio.create_task(self._score_description(description, result))
        meta_task = asyncio.create_task(self._score_metadata(metadata_uri, result))
        name_task = asyncio.create_task(self._score_name(token_name, token_symbol, result))

        await asyncio.gather(img_task, desc_task, meta_task, name_task)

        result.copy_viability = (
            result.image_score
            + result.description_score
            + result.metadata_score
            + result.name_score
        )

        logger.debug(
            f"CopyScore {token_symbol}: viability={result.copy_viability:.0f} "
            f"img={result.image_score:.0f} desc={result.description_score:.0f} "
            f"meta={result.metadata_score:.0f} name={result.name_score:.0f}"
        )
        return result

    async def score_batch(
        self, tokens: list[dict], concurrency: int = 8
    ) -> list[CopyViabilityResult]:
        """Score a batch of token dicts concurrently."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _guarded(tok: dict) -> CopyViabilityResult:
            async with semaphore:
                return await self.score(
                    token_address=tok.get("token_address", ""),
                    token_name=tok.get("token_name", ""),
                    token_symbol=tok.get("token_symbol", ""),
                    image_uri=tok.get("image_uri", ""),
                    description=tok.get("description", "") or tok.get("original_description", ""),
                    metadata_uri=tok.get("metadata_uri", ""),
                )

        tasks = [_guarded(tok) for tok in tokens]
        return await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # Sub-scorers
    # ------------------------------------------------------------------

    async def _score_image(self, image_uri: str, result: CopyViabilityResult) -> None:
        """Score: 0 if no image, 10 if URI present, 25 if URI is reachable."""
        if not image_uri:
            result.notes.append("no image_uri")
            return

        result.has_image = True
        reachable = await self._probe_url(image_uri)
        if reachable:
            result.image_score = 25.0
            result.notes.append(f"image OK: {image_uri[:60]}")
        else:
            result.image_score = 10.0
            result.notes.append(f"image URI present but unreachable: {image_uri[:60]}")

    async def _score_description(self, description: str, result: CopyViabilityResult) -> None:
        """Score: 0 if missing, 15 if short, 25 if rich (>100 chars)."""
        if not description or len(description.strip()) < 5:
            result.notes.append("no description")
            return

        result.has_description = True
        length = len(description.strip())
        if length >= 100:
            result.description_score = 25.0
        elif length >= 20:
            result.description_score = 15.0
        else:
            result.description_score = 8.0
        result.notes.append(f"description length={length}")

    async def _score_metadata(self, metadata_uri: str, result: CopyViabilityResult) -> None:
        """Score: 0 if absent, 10 if present, 25 if fetchable JSON."""
        if not metadata_uri:
            result.notes.append("no metadata_uri")
            return

        result.has_metadata_uri = True
        reachable = await self._probe_url(metadata_uri)
        if reachable:
            result.metadata_score = 25.0
            result.metadata_fetchable = True
            result.notes.append(f"metadata OK: {metadata_uri[:60]}")
        else:
            result.metadata_score = 10.0
            result.notes.append(f"metadata URI present but unreachable: {metadata_uri[:60]}")

    async def _score_name(
        self, token_name: str, token_symbol: str, result: CopyViabilityResult
    ) -> None:
        """
        Score name quality for mimicry:
          - Empty name/symbol → 0
          - Very short (<3 chars) → 5
          - Generic single-word → 15
          - Multi-word or distinctive → 25
        """
        if not token_name and not token_symbol:
            result.notes.append("no name or symbol")
            return

        name = token_name or token_symbol
        length = len(name)

        if length < 3:
            result.name_score = 5.0
        elif length < 6:
            result.name_score = 15.0
        else:
            result.name_score = 20.0

        # Bonus for multi-word names (more mimicry surface area)
        if " " in name:
            result.name_score = min(25.0, result.name_score + 5.0)

        result.notes.append(f"name={name!r} len={length}")

    # ------------------------------------------------------------------
    # URL probing
    # ------------------------------------------------------------------

    async def _probe_url(self, url: str) -> bool:
        """
        Cheap reachability check: HEAD request, returns True if 2xx/3xx.
        Falls back to GET if server rejects HEAD.
        """
        if not self._session or not url or not url.startswith("http"):
            return False

        # Try HEAD first (cheap)
        try:
            async with self._session.head(url, allow_redirects=True) as resp:
                if resp.status < 400:
                    return True
        except Exception:
            pass

        # Fallback: GET with small read
        try:
            async with self._session.get(url) as resp:
                if resp.status < 400:
                    return True
        except Exception:
            pass

        return False
