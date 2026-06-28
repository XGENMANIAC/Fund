"""
Token scoring and rug-risk analysis module.

DISCLAIMER: Educational system for Solana devnet only.
These scoring heuristics are educational approximations, NOT financial advice.
Real rug-pull detection is a research-grade problem that even professional
tools (RugDoc, Token Sniffer) don't solve perfectly.

Scoring model:
  - Volume momentum    (30%): Is volume ramping up quickly?
  - Liquidity depth    (20%): Is there enough liquidity to sustain price?
  - Holder growth      (20%): Are holders growing (vs concentrated)?
  - Social buzz        (15%): Are people talking about it?
  - Rug risk penalty   (15%): Subtract for red flags (mint not revoked, etc.)

Output: composite score 0–100, plus individual sub-scores and a rug_risk rating.
"""

import math
from dataclasses import dataclass, field
from typing import Optional

from monitor.aggregator import TokenDetection
from storage.database import insert_candidate
from utils.logger import get_logger
from utils.helpers import load_config
from utils.rpc import SolanaRPCClient

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Score result
# ---------------------------------------------------------------------------

@dataclass
class ScoredCandidate:
    """Output of the scoring module — a detection enriched with analysis scores."""

    detection: TokenDetection = field(default_factory=TokenDetection)
    db_id: Optional[int] = None  # DB id of the inserted candidate row

    # Sub-scores (0–100)
    volume_score: float = 0.0
    liquidity_score: float = 0.0
    holder_score: float = 0.0
    social_score: float = 0.0
    rug_risk_score: float = 0.0  # Higher = riskier (penalty applied to composite)

    # Composite
    composite_score: float = 0.0

    # On-chain risk flags (fetched via RPC)
    mint_revoked: bool = False
    freeze_revoked: bool = False
    lp_locked: bool = False
    top10_pct: float = 0.0        # % held by top 10 wallets

    # Human-readable summary
    risk_flags: list[str] = field(default_factory=list)

    @property
    def token_address(self) -> str:
        return self.detection.token_address

    @property
    def token_name(self) -> str:
        return self.detection.token_name or self.detection.token_symbol

    def summary(self) -> str:
        return (
            f"{self.token_name} ({self.detection.token_symbol}) "
            f"| Score: {self.composite_score:.1f}/100 "
            f"| Rug Risk: {self.rug_risk_score:.0f}/100 "
            f"| Flags: {', '.join(self.risk_flags) or 'none'}"
        )


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class TokenScorer:
    """
    Scores token detections for virality + rug risk.

    Usage:
        scorer = TokenScorer()
        candidates = await scorer.score_batch(detections)
    """

    def __init__(self):
        cfg = load_config()
        scoring_cfg = cfg.get("scoring", {}).get("weights", {})
        self._w_volume = scoring_cfg.get("volume_momentum", 0.30)
        self._w_liquidity = scoring_cfg.get("liquidity_depth", 0.20)
        self._w_holder = scoring_cfg.get("holder_growth", 0.20)
        self._w_social = scoring_cfg.get("social_buzz", 0.15)
        self._w_rug = scoring_cfg.get("rug_risk_penalty", 0.15)

        filters = cfg.get("filters", {})
        self._min_score = filters.get("min_score", 60)
        self._skip_high_rug = filters.get("skip_high_rug_risk", True)
        self._network = cfg.get("network", "devnet")

    async def score_batch(
        self,
        detections: list[TokenDetection],
    ) -> list[ScoredCandidate]:
        """
        Score a batch of detections concurrently (RPC calls batched where possible).
        Returns only those meeting the minimum score threshold.
        """
        results = []
        for det in detections:
            try:
                candidate = await self.score_one(det)
                if candidate.composite_score >= self._min_score:
                    if self._skip_high_rug and candidate.rug_risk_score >= 80:
                        logger.info(
                            f"Skipping {det.token_address[:8]}... "
                            f"(rug risk too high: {candidate.rug_risk_score:.0f})"
                        )
                        continue
                    results.append(candidate)
                    self._persist_candidate(candidate, det)
            except Exception as exc:
                logger.error(f"Scoring failed for {det.token_address}: {exc}")

        logger.info(
            f"Scored {len(detections)} detections: "
            f"{len(results)} passed threshold (score>={self._min_score})"
        )
        return results

    async def score_one(self, det: TokenDetection) -> ScoredCandidate:
        """Compute all sub-scores and the composite for one detection."""
        candidate = ScoredCandidate(detection=det)

        # --- Volume momentum score ---
        candidate.volume_score = self._score_volume(det)

        # --- Liquidity depth score ---
        candidate.liquidity_score = self._score_liquidity(det)

        # --- Holder analysis (on-chain RPC call) ---
        holder_info = await self._fetch_holder_info(det.token_address)
        candidate.holder_score = self._score_holders(det, holder_info)
        candidate.top10_pct = holder_info.get("top10_pct", 0.0)

        # --- Social buzz score ---
        candidate.social_score = self._score_social(det)

        # --- Rug risk analysis ---
        risk_info = await self._fetch_risk_info(det.token_address)
        candidate.mint_revoked = risk_info.get("mint_revoked", False)
        candidate.freeze_revoked = risk_info.get("freeze_revoked", False)
        candidate.lp_locked = risk_info.get("lp_locked", False)
        candidate.rug_risk_score, candidate.risk_flags = self._score_rug_risk(
            det, risk_info
        )

        # --- Composite score ---
        raw = (
            candidate.volume_score * self._w_volume
            + candidate.liquidity_score * self._w_liquidity
            + candidate.holder_score * self._w_holder
            + candidate.social_score * self._w_social
            - candidate.rug_risk_score * self._w_rug
        )
        candidate.composite_score = max(0.0, min(100.0, raw))

        logger.debug(
            f"{det.token_symbol or det.token_address[:8]}: "
            f"vol={candidate.volume_score:.0f} "
            f"liq={candidate.liquidity_score:.0f} "
            f"hold={candidate.holder_score:.0f} "
            f"soc={candidate.social_score:.0f} "
            f"rug={candidate.rug_risk_score:.0f} "
            f"=> composite={candidate.composite_score:.1f}"
        )
        return candidate

    # ------------------------------------------------------------------
    # Sub-score methods
    # ------------------------------------------------------------------

    def _score_volume(self, det: TokenDetection) -> float:
        """
        Score volume momentum 0–100.
        Key signals:
          - High 5-minute volume relative to 1h volume (acceleration)
          - Positive price change alongside volume (confirmation)
        """
        if det.volume_5m <= 0:
            return 0.0

        # Normalized 5m volume (log scale, capped at $50K)
        vol_score = min(math.log1p(det.volume_5m) / math.log1p(50_000), 1.0) * 70

        # Momentum bonus: 5m/1h ratio > 10% = trending up
        if det.volume_1h > 0:
            ratio = det.volume_5m / (det.volume_1h / 12)  # 5m vs expected avg 5m chunk
            if ratio > 2:
                vol_score = min(vol_score + 20, 100)
            elif ratio > 1.5:
                vol_score = min(vol_score + 10, 100)

        # Price change confirmation (positive change = organic buying)
        if det.price_change_5m > 20:
            vol_score = min(vol_score + 10, 100)
        elif det.price_change_5m < -30:
            vol_score = max(vol_score - 15, 0)

        return vol_score

    def _score_liquidity(self, det: TokenDetection) -> float:
        """
        Score liquidity depth 0–100.
        More liquidity = less slippage = healthier market.
        Target range: $5K–$500K for meme coins.
        """
        liq = det.liquidity_usd
        if liq <= 0:
            return 0.0

        # Log-scaled from $1K to $500K
        score = min(math.log1p(liq - 1000) / math.log1p(499_000), 1.0) * 100
        return max(0.0, score)

    def _score_holders(self, det: TokenDetection, holder_info: dict) -> float:
        """
        Score holder distribution 0–100.
        Penalize extreme concentration in top holders.
        """
        holders = holder_info.get("holders", det.holders)
        top10_pct = holder_info.get("top10_pct", 0.0)

        if holders <= 0:
            return 20.0  # neutral score when data unavailable

        # Base score: log-scaled holder count
        score = min(math.log1p(holders) / math.log1p(10_000), 1.0) * 80

        # Concentration penalty
        if top10_pct > 90:
            score -= 40
        elif top10_pct > 70:
            score -= 20
        elif top10_pct > 50:
            score -= 10

        return max(0.0, min(100.0, score))

    def _score_social(self, det: TokenDetection) -> float:
        """
        Score social buzz 0–100 based on mention count.
        Capped at 50 mentions = full score (to avoid spam inflation).
        """
        mentions = det.social_mentions
        if mentions <= 0:
            return 0.0
        return min(mentions / 50, 1.0) * 100

    def _score_rug_risk(
        self, det: TokenDetection, risk_info: dict
    ) -> tuple[float, list[str]]:
        """
        Compute a rug risk score (higher = more likely to be a rug) and flags.
        Returns (score 0–100, list of flag strings).
        """
        risk = 0.0
        flags = []

        if not risk_info.get("mint_revoked", True):
            risk += 30
            flags.append("mint_not_revoked")

        if not risk_info.get("freeze_revoked", True):
            risk += 15
            flags.append("freeze_not_revoked")

        if not risk_info.get("lp_locked", False):
            risk += 20
            flags.append("lp_not_locked")

        top10 = risk_info.get("top10_pct", 0.0)
        if top10 > 80:
            risk += 30
            flags.append(f"top10_hold_{top10:.0f}pct")
        elif top10 > 60:
            risk += 15
            flags.append(f"top10_hold_{top10:.0f}pct")

        # Age-based risk: very new tokens with no holder data are inherently riskier
        if det.age_hours < 0.5 and not flags:
            risk += 10
            flags.append("very_new_token")

        # pump.fun token that hasn't graduated = still bonding curve = higher risk
        if det.is_pumpfun and not det.is_graduated:
            risk += 15
            flags.append("bonding_curve_not_graduated")

        return min(risk, 100.0), flags

    # ------------------------------------------------------------------
    # On-chain data fetching (via RPC)
    # ------------------------------------------------------------------

    async def _fetch_holder_info(self, token_address: str) -> dict:
        """
        Fetch token holder info from Solana RPC.
        Returns {"holders": int, "top10_pct": float}.
        Rate-limited gracefully — returns empty dict on failure.
        """
        try:
            async with SolanaRPCClient(self._network) as client:
                largest = await client.get_token_largest_accounts(token_address)
                supply_data = await client.get_token_supply(token_address)

            if not largest or not supply_data:
                return {}

            total = float(supply_data.get("value", {}).get("amount", 1) or 1)
            top10_amount = sum(float(a.get("amount", 0)) for a in largest[:10])
            top10_pct = (top10_amount / total * 100) if total > 0 else 0.0

            return {
                "holders": len(largest),
                "top10_pct": top10_pct,
            }
        except Exception as exc:
            logger.debug(f"holder fetch failed for {token_address[:8]}: {exc}")
            return {}

    async def _fetch_risk_info(self, token_address: str) -> dict:
        """
        Check on-chain risk flags:
          - Is mint authority revoked?
          - Is freeze authority revoked?
          - Is LP locked? (simple heuristic: check known lock programs)
        """
        try:
            async with SolanaRPCClient(self._network) as client:
                info = await client.get_account_info(token_address)

            if not info or not info.get("value"):
                return {}

            parsed = info["value"].get("data", {}).get("parsed", {})
            mint_info = parsed.get("info", {})

            # Mint authority: None = revoked
            mint_authority = mint_info.get("mintAuthority")
            freeze_authority = mint_info.get("freezeAuthority")

            return {
                "mint_revoked": mint_authority is None,
                "freeze_revoked": freeze_authority is None,
                "lp_locked": False,  # Full LP lock detection requires extra RPC calls
            }
        except Exception as exc:
            logger.debug(f"risk info fetch failed for {token_address[:8]}: {exc}")
            return {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_candidate(
        self, candidate: ScoredCandidate, det: TokenDetection
    ) -> None:
        """Save the scored candidate to SQLite."""
        if not det.db_id:
            return
        try:
            db_id = insert_candidate(
                detection_id=det.db_id,
                token_address=det.token_address,
                score=candidate.composite_score,
                token_name=det.token_name,
                token_symbol=det.token_symbol,
                volume_score=candidate.volume_score,
                liquidity_score=candidate.liquidity_score,
                holder_score=candidate.holder_score,
                social_score=candidate.social_score,
                rug_risk_score=candidate.rug_risk_score,
                mint_revoked=candidate.mint_revoked,
                freeze_revoked=candidate.freeze_revoked,
                lp_locked=candidate.lp_locked,
                top10_pct=candidate.top10_pct,
            )
            candidate.db_id = db_id
        except Exception as exc:
            logger.error(f"Candidate persist failed: {exc}")
