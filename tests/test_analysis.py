"""
Tests for analysis modules (scorer + generator).

DISCLAIMER: Educational system for Solana devnet only.
Tests use mocked RPC calls to avoid hitting real endpoints.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from monitor.aggregator import TokenDetection
from analysis.scorer import TokenScorer, ScoredCandidate
from analysis.generator import TokenGenerator, GeneratedTokenSpec


# ---------------------------------------------------------------------------
# Scorer tests
# ---------------------------------------------------------------------------

class TestTokenScorer:

    def setup_method(self):
        self.scorer = TokenScorer()

    def test_score_volume_zero_volume(self):
        """Zero volume should produce zero volume score."""
        det = TokenDetection(volume_5m=0)
        assert self.scorer._score_volume(det) == 0.0

    def test_score_volume_high_volume(self):
        """High volume should produce a high score."""
        det = TokenDetection(volume_5m=50_000, price_change_5m=25.0)
        score = self.scorer._score_volume(det)
        assert score > 70

    def test_score_volume_negative_price_change_penalized(self):
        """Negative price change should lower the volume score."""
        det_positive = TokenDetection(volume_5m=10_000, price_change_5m=20.0)
        det_negative = TokenDetection(volume_5m=10_000, price_change_5m=-40.0)
        assert self.scorer._score_volume(det_positive) > self.scorer._score_volume(det_negative)

    def test_score_liquidity_zero(self):
        det = TokenDetection(liquidity_usd=0)
        assert self.scorer._score_liquidity(det) == 0.0

    def test_score_liquidity_scaling(self):
        """Higher liquidity should produce higher score."""
        low = TokenDetection(liquidity_usd=2_000)
        high = TokenDetection(liquidity_usd=100_000)
        assert self.scorer._score_liquidity(high) > self.scorer._score_liquidity(low)

    def test_score_social_no_mentions(self):
        det = TokenDetection(social_mentions=0)
        assert self.scorer._score_social(det) == 0.0

    def test_score_social_capped_at_100(self):
        det = TokenDetection(social_mentions=1000)
        assert self.scorer._score_social(det) == 100.0

    def test_score_holders_no_data(self):
        """No holder data should return neutral score (not 0)."""
        score = self.scorer._score_holders(TokenDetection(), {})
        assert score == 20.0

    def test_score_holders_high_concentration_penalized(self):
        """Top 10 holding 95% should massively penalize the holder score."""
        score_concentrated = self.scorer._score_holders(
            TokenDetection(), {"holders": 500, "top10_pct": 95.0}
        )
        score_distributed = self.scorer._score_holders(
            TokenDetection(), {"holders": 500, "top10_pct": 30.0}
        )
        assert score_distributed > score_concentrated

    def test_rug_risk_no_flags(self):
        """A token with all authority revoked has low rug risk."""
        risk_info = {
            "mint_revoked": True,
            "freeze_revoked": True,
            "lp_locked": True,
            "top10_pct": 25.0,
        }
        det = TokenDetection(age_hours=2.0)
        score, flags = self.scorer._score_rug_risk(det, risk_info)
        assert score == 0.0
        assert flags == []

    def test_rug_risk_mint_not_revoked(self):
        """Mint authority not revoked adds 30 to rug risk."""
        risk_info = {"mint_revoked": False, "freeze_revoked": True, "lp_locked": True, "top10_pct": 20.0}
        det = TokenDetection()
        score, flags = self.scorer._score_rug_risk(det, risk_info)
        assert score >= 30
        assert "mint_not_revoked" in flags

    def test_rug_risk_pumpfun_not_graduated(self):
        """pump.fun token on bonding curve should add rug risk."""
        det = TokenDetection(is_pumpfun=True, is_graduated=False)
        risk_info = {"mint_revoked": True, "freeze_revoked": True, "lp_locked": False, "top10_pct": 20.0}
        score, flags = self.scorer._score_rug_risk(det, risk_info)
        assert "bonding_curve_not_graduated" in flags or "lp_not_locked" in flags

    @pytest.mark.asyncio
    async def test_score_one_with_mocked_rpc(self):
        """Test score_one with mocked RPC holder and risk data."""
        det = TokenDetection(
            token_address="MockToken123",
            token_name="Mock Token",
            token_symbol="MOCK",
            liquidity_usd=20_000,
            volume_5m=2_000,
            volume_1h=8_000,
            price_change_5m=15.0,
            social_mentions=10,
            age_hours=1.0,
        )

        with patch.object(self.scorer, '_fetch_holder_info', new_callable=AsyncMock) as mock_holders, \
             patch.object(self.scorer, '_fetch_risk_info', new_callable=AsyncMock) as mock_risk:

            mock_holders.return_value = {"holders": 200, "top10_pct": 45.0}
            mock_risk.return_value = {"mint_revoked": True, "freeze_revoked": True, "lp_locked": False}

            candidate = await self.scorer.score_one(det)

            assert 0 <= candidate.composite_score <= 100
            assert candidate.volume_score > 0
            assert candidate.liquidity_score > 0
            assert candidate.holder_score > 0


# ---------------------------------------------------------------------------
# Generator tests
# ---------------------------------------------------------------------------

class TestTokenGenerator:

    def setup_method(self):
        self.generator = TokenGenerator()

    def _make_candidate(self) -> ScoredCandidate:
        from analysis.scorer import ScoredCandidate
        from monitor.aggregator import TokenDetection
        candidate = ScoredCandidate(
            detection=TokenDetection(
                token_address="SourceToken123",
                token_name="Bonk Inu",
                token_symbol="BONKINU",
                liquidity_usd=50_000,
            ),
            composite_score=75.0,
        )
        candidate.db_id = 1
        return candidate

    def test_generate_returns_valid_spec(self):
        """generate() should return a valid GeneratedTokenSpec."""
        candidate = self._make_candidate()
        spec = self.generator.generate(candidate)

        assert spec.new_name != ""
        assert spec.new_symbol != ""
        assert spec.new_supply == 1_000_000_000
        assert spec.new_decimals == 6
        assert spec.pool_tokens > 0
        assert spec.pool_tokens + spec.retained_tokens == spec.new_supply
        assert "EDUCATIONAL" in spec.new_description.upper()
        assert spec.source_address == "SourceToken123"

    def test_name_not_identical_to_source(self):
        """Generated name should differ from source name."""
        candidate = self._make_candidate()
        spec = self.generator.generate(candidate)
        assert spec.new_name != candidate.detection.token_name

    def test_symbol_not_identical_to_source(self):
        """Generated symbol should differ from source symbol."""
        candidate = self._make_candidate()
        spec = self.generator.generate(candidate)
        assert spec.new_symbol != candidate.detection.token_symbol

    def test_metadata_json_has_required_fields(self):
        """Metadata JSON should have all Metaplex-required fields."""
        candidate = self._make_candidate()
        spec = self.generator.generate(candidate)
        meta = spec.metadata_json

        assert "name" in meta
        assert "symbol" in meta
        assert "description" in meta
        assert "image" in meta
        assert "properties" in meta

    def test_description_always_has_disclaimer(self):
        """Educational disclaimer must always be present in description."""
        candidate = self._make_candidate()
        for strategy in ["append_suffix", "synonym_swap", "ai_riff"]:
            self.generator._name_strategy = strategy
            spec = self.generator.generate(candidate)
            assert "EDUCATIONAL" in spec.new_description.upper() or \
                   "educational" in spec.new_description.lower()

    def test_clean_symbol_removes_special_chars(self):
        from utils.helpers import clean_symbol
        assert clean_symbol("BON!K-INU 2.0") == "BONKINU20"
        assert clean_symbol("$MOON$") == "MOON"

    def test_pool_allocation(self):
        """pool_tokens should be 80% of supply by default."""
        candidate = self._make_candidate()
        spec = self.generator.generate(candidate)
        expected_pool = int(spec.new_supply * 0.80)
        assert abs(spec.pool_tokens - expected_pool) <= 1  # allow rounding
