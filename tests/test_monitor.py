"""
Tests for the monitoring modules.

DISCLAIMER: Educational system for Solana devnet only.

These tests use mocked HTTP responses and don't hit real APIs.
They validate the parsing logic and data transformation.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from monitor.dexscreener import DexScreenerMonitor, DexTokenAlert
from monitor.pumpfun import PumpFunMonitor, PumpFunAlert
from monitor.aggregator import MonitorAggregator, TokenDetection


# ---------------------------------------------------------------------------
# DexScreener tests
# ---------------------------------------------------------------------------

class TestDexScreenerMonitor:

    def setup_method(self):
        self.monitor = DexScreenerMonitor()

    def test_parse_pair_valid(self):
        """Test that a well-formed DexScreener pair dict is parsed correctly."""
        pair = {
            "chainId": "solana",
            "pairAddress": "PairABC123",
            "baseToken": {
                "address": "TokenXYZ456",
                "name": "Moon Dog",
                "symbol": "MDOG",
            },
            "priceUsd": "0.00001234",
            "liquidity": {"usd": 15000},
            "volume": {"m5": 800, "h1": 5000, "h24": 25000},
            "priceChange": {"m5": 12.5, "h1": 45.0, "h24": 120.0},
            "pairCreatedAt": 1700000000000,  # ~2023 Unix ms
            "url": "https://dexscreener.com/solana/PairABC123",
        }

        alert = self.monitor._parse_pair(pair)
        assert alert is not None
        assert alert.token_address == "TokenXYZ456"
        assert alert.token_name == "Moon Dog"
        assert alert.token_symbol == "MDOG"
        assert alert.price_usd == pytest.approx(0.00001234)
        assert alert.liquidity_usd == 15000.0
        assert alert.volume_5m == 800.0
        assert alert.price_change_5m == 12.5
        assert alert.source == "dexscreener"

    def test_parse_pair_missing_address_returns_none(self):
        """Test that a pair missing the base token address is rejected."""
        pair = {
            "chainId": "solana",
            "baseToken": {"name": "Missing", "symbol": "MISS"},
            "priceUsd": "0.001",
        }
        assert self.monitor._parse_pair(pair) is None

    def test_parse_pair_missing_price_is_ok(self):
        """Missing price should not cause a crash — default to 0.0."""
        pair = {
            "chainId": "solana",
            "baseToken": {"address": "SomeAddr123", "name": "Test", "symbol": "TST"},
            "liquidity": {"usd": 5000},
            "volume": {"m5": 0},
            "priceChange": {},
        }
        alert = self.monitor._parse_pair(pair)
        assert alert is not None
        assert alert.price_usd == 0.0

    def test_basic_filter_rejects_low_liquidity(self):
        """Tokens below min_liquidity_usd should be filtered out."""
        alert = DexTokenAlert(
            token_address="abc",
            liquidity_usd=100,   # below default min of 1000
            volume_5m=500,
            age_hours=1.0,
        )
        assert not self.monitor._passes_basic_filter(alert)

    def test_basic_filter_rejects_too_old(self):
        """Tokens older than max_age_hours should be filtered out."""
        alert = DexTokenAlert(
            token_address="abc",
            liquidity_usd=10_000,
            volume_5m=1000,
            age_hours=10.0,  # older than default 4h
        )
        assert not self.monitor._passes_basic_filter(alert)

    def test_basic_filter_passes_valid(self):
        """A fresh, liquid token should pass the basic filter."""
        alert = DexTokenAlert(
            token_address="abc123XYZ",
            liquidity_usd=10_000,
            volume_5m=1000,
            age_hours=0.5,
        )
        assert self.monitor._passes_basic_filter(alert)

    def test_content_hash_is_deterministic(self):
        """Same token+pair should always produce the same content hash."""
        alert1 = DexTokenAlert(token_address="AAA", pair_address="BBB")
        alert2 = DexTokenAlert(token_address="AAA", pair_address="BBB")
        assert alert1.content_hash == alert2.content_hash

    def test_content_hash_differs_for_different_tokens(self):
        alert1 = DexTokenAlert(token_address="AAA", pair_address="BBB")
        alert2 = DexTokenAlert(token_address="CCC", pair_address="DDD")
        assert alert1.content_hash != alert2.content_hash


# ---------------------------------------------------------------------------
# pump.fun tests
# ---------------------------------------------------------------------------

class TestPumpFunMonitor:

    def setup_method(self):
        self.monitor = PumpFunMonitor()

    def test_parse_api_coin_valid(self):
        """Test that a pump.fun API coin dict is parsed correctly."""
        coin = {
            "mint": "PumpTokenMint123",
            "name": "Degen Cat",
            "symbol": "DGCAT",
            "description": "The most degen cat on Solana",
            "image_uri": "https://example.com/cat.png",
            "creator": "CreatorWallet123",
            "created_timestamp": 1700000000000,
            "usd_market_cap": 50000.0,
            "reply_count": 25,
            "raydium_pool": "",
            "king_of_the_hill_timestamp": None,
        }

        alert = self.monitor._parse_api_coin(coin)
        assert alert is not None
        assert alert.token_address == "PumpTokenMint123"
        assert alert.token_name == "Degen Cat"
        assert alert.token_symbol == "DGCAT"
        assert alert.market_cap_usd == 50000.0
        assert not alert.is_migrated
        assert alert.source == "pumpfun"

    def test_parse_api_coin_missing_mint_returns_none(self):
        coin = {"name": "No Mint", "symbol": "NM"}
        assert self.monitor._parse_api_coin(coin) is None

    def test_parse_api_coin_graduated(self):
        """A coin with a raydium_pool is considered 'graduated'."""
        coin = {
            "mint": "GradToken123",
            "name": "Graduated",
            "symbol": "GRAD",
            "created_timestamp": 1700000000000,
            "usd_market_cap": 100000,
            "raydium_pool": "RaydiumPoolAddr123",
        }
        alert = self.monitor._parse_api_coin(coin)
        assert alert.is_migrated

    def test_parse_number_with_suffix(self):
        assert PumpFunMonitor._parse_number_with_suffix("1.5K") == 1500.0
        assert PumpFunMonitor._parse_number_with_suffix("2.3M") == 2_300_000.0
        assert PumpFunMonitor._parse_number_with_suffix("500") == 500.0
        assert PumpFunMonitor._parse_number_with_suffix("1B") == 1_000_000_000.0
        assert PumpFunMonitor._parse_number_with_suffix("invalid") == 0.0


# ---------------------------------------------------------------------------
# Aggregator tests
# ---------------------------------------------------------------------------

class TestMonitorAggregator:

    def test_merge_or_add_new_token(self):
        """A new token should be added to the detection map."""
        aggregator = MonitorAggregator()
        detection_map = {}
        det = TokenDetection(
            token_address="NewToken123",
            token_name="New Token",
            token_symbol="NEW",
            sources=["dexscreener"],
            liquidity_usd=10_000,
        )
        aggregator._merge_or_add(detection_map, det)
        assert "NewToken123" in detection_map
        assert detection_map["NewToken123"].liquidity_usd == 10_000

    def test_merge_or_add_existing_token_merges_sources(self):
        """A second detection of the same token should merge sources."""
        aggregator = MonitorAggregator()
        det1 = TokenDetection(
            token_address="DualToken",
            sources=["dexscreener"],
            liquidity_usd=8_000,
        )
        det2 = TokenDetection(
            token_address="DualToken",
            sources=["pumpfun"],
            liquidity_usd=12_000,  # higher value
            token_name="Dual Token",
        )
        detection_map = {}
        aggregator._merge_or_add(detection_map, det1)
        aggregator._merge_or_add(detection_map, det2)

        merged = detection_map["DualToken"]
        assert set(merged.sources) == {"dexscreener", "pumpfun"}
        assert merged.liquidity_usd == 12_000   # takes higher
        assert merged.token_name == "Dual Token"  # filled in from det2

    def test_merge_ignores_empty_address(self):
        """Detections without a token address are silently ignored."""
        aggregator = MonitorAggregator()
        detection_map = {}
        aggregator._merge_or_add(detection_map, TokenDetection(token_address=""))
        assert len(detection_map) == 0
