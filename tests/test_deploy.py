"""
Tests for deployment modules.

DISCLAIMER: Educational system for Solana devnet only.
All deployment tests are mocked — no real on-chain calls are made.
"""

import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from analysis.generator import GeneratedTokenSpec
from deploy.raydium import RaydiumDeployer
from utils.helpers import load_config


class TestRaydiumDeployer:

    def setup_method(self):
        # Patch the config to ensure devnet
        self.mock_cfg = {
            "network": "devnet",
            "rpc": {"endpoints": ["https://api.devnet.solana.com"]},
            "deploy": {"initial_sol": 0.1},
        }

    def _make_spec(self) -> GeneratedTokenSpec:
        spec = GeneratedTokenSpec(
            source_address="Source123",
            new_name="Test Token",
            new_symbol="TST",
            new_supply=1_000_000_000,
            new_decimals=6,
            pool_tokens=800_000_000,
            retained_tokens=200_000_000,
            db_id=1,
        )
        return spec

    def test_mainnet_blocked(self):
        """RaydiumDeployer should raise if network is mainnet-beta."""
        with patch("deploy.raydium.load_config") as mock_cfg:
            mock_cfg.return_value = {
                "network": "mainnet-beta",
                "rpc": {"endpoints": []},
                "deploy": {},
            }
            with pytest.raises(ValueError, match="MAINNET BLOCKED"):
                RaydiumDeployer()

    def test_parse_node_result_success(self):
        """_parse_node_result should extract JSON after __RESULT__ marker."""
        with patch("deploy.raydium.load_config") as mock_cfg:
            mock_cfg.return_value = self.mock_cfg
            deployer = RaydiumDeployer()

        result_data = {
            "success": True,
            "pool_id": "PoolXYZ123",
            "market_id": "MarketABC456",
            "lp_mint": "LPMint789",
        }
        stdout = f"Some log output\n__RESULT__\n{json.dumps(result_data)}"
        parsed = deployer._parse_node_result(stdout)

        assert parsed["success"] is True
        assert parsed["pool_id"] == "PoolXYZ123"
        assert parsed["market_id"] == "MarketABC456"

    def test_parse_node_result_missing_marker(self):
        """If __RESULT__ is missing, should return failed result."""
        with patch("deploy.raydium.load_config") as mock_cfg:
            mock_cfg.return_value = self.mock_cfg
            deployer = RaydiumDeployer()

        result = deployer._parse_node_result("No result here")
        assert not result["success"]
        assert "No __RESULT__" in result["error"]

    def test_parse_node_result_invalid_json(self):
        """Invalid JSON after __RESULT__ should return failed result."""
        with patch("deploy.raydium.load_config") as mock_cfg:
            mock_cfg.return_value = self.mock_cfg
            deployer = RaydiumDeployer()

        result = deployer._parse_node_result("__RESULT__\nnot valid json{{")
        assert not result["success"]

    def test_check_node_returns_bool(self):
        """_check_node should return True/False without raising."""
        with patch("deploy.raydium.load_config") as mock_cfg:
            mock_cfg.return_value = self.mock_cfg
            deployer = RaydiumDeployer()

        # Should not raise regardless of whether node is installed
        result = deployer._check_node()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_deploy_mocked_node_success(self):
        """Test full deploy path with mocked subprocess call."""
        success_result = {
            "success": True,
            "network": "devnet",
            "mint_address": "TestMint123",
            "pool_id": "Pool456",
            "market_id": "Market789",
            "lp_mint": "LP000",
            "pool_txids": ["tx1", "tx2"],
        }

        with patch("deploy.raydium.load_config") as mock_cfg, \
             patch.object(RaydiumDeployer, "_run_node_sync", return_value=success_result):

            mock_cfg.return_value = self.mock_cfg
            deployer = RaydiumDeployer()
            spec = self._make_spec()
            result = await deployer.deploy(spec, "TestMint123")

        assert result["success"]
        assert result["pool_id"] == "Pool456"

    @pytest.mark.asyncio
    async def test_deploy_mocked_node_failure(self):
        """Test that node failure is properly propagated."""
        fail_result = {"success": False, "error": "Insufficient SOL"}

        with patch("deploy.raydium.load_config") as mock_cfg, \
             patch.object(RaydiumDeployer, "_run_node_sync", return_value=fail_result):

            mock_cfg.return_value = self.mock_cfg
            deployer = RaydiumDeployer()
            spec = self._make_spec()
            result = await deployer.deploy(spec, "TestMint123")

        assert not result["success"]
        assert "SOL" in result.get("error", "")


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------

class TestMetadataAttacher:

    def test_find_metadata_pda_is_deterministic(self):
        """The same mint should always produce the same PDA."""
        from deploy.metadata import find_metadata_pda
        from solders.pubkey import Pubkey

        # Use a known devnet mint address for testing
        mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
        pda1, bump1 = find_metadata_pda(mint)
        pda2, bump2 = find_metadata_pda(mint)

        assert str(pda1) == str(pda2)
        assert bump1 == bump2

    def test_encode_metadata_produces_bytes(self):
        """Encoding metadata instruction data should produce non-empty bytes."""
        from deploy.metadata import _encode_create_metadata_v3_ix

        data = _encode_create_metadata_v3_ix(
            name="Test Token",
            symbol="TST",
            uri="https://example.com/metadata.json",
        )
        assert isinstance(data, bytes)
        assert len(data) > 8  # at least discriminant + some data


# ---------------------------------------------------------------------------
# Approval gate tests
# ---------------------------------------------------------------------------

class TestApprovalGate:

    def test_approval_gate_disabled_auto_approves(self):
        """When the gate is disabled, request_approval_cli should auto-approve."""
        from bot.approval import ApprovalGate

        with patch("bot.approval.load_config") as mock_cfg:
            mock_cfg.return_value = {
                "approval": {"enabled": False, "timeout_seconds": 5, "confirm_phrase": "TEST"},
                "deploy": {"initial_sol": 0.1},
            }
            gate = ApprovalGate()

        assert not gate._enabled

    @pytest.mark.asyncio
    async def test_approval_gate_timeout_rejects(self):
        """A gate with 0.1s timeout should auto-reject via timeout."""
        from bot.approval import ApprovalGate
        from analysis.scorer import ScoredCandidate
        from analysis.generator import GeneratedTokenSpec
        from monitor.aggregator import TokenDetection

        with patch("bot.approval.load_config") as mock_cfg:
            mock_cfg.return_value = {
                "approval": {
                    "enabled": True,
                    "timeout_seconds": 0.1,  # Very short timeout
                    "confirm_phrase": "CONFIRM",
                },
                "deploy": {"initial_sol": 0.1},
            }
            gate = ApprovalGate()

        candidate = ScoredCandidate(
            detection=TokenDetection(token_address="Test", token_name="Test", token_symbol="TST"),
            composite_score=80,
        )
        spec = GeneratedTokenSpec(
            new_name="Test Clone",
            new_symbol="TCLONE",
            new_supply=1_000_000_000,
            new_description="Educational test.",
            pool_tokens=800_000_000,
        )

        # Mock the blocking CLI input to just timeout
        with patch("builtins.input", side_effect=TimeoutError):
            with patch.object(gate, '_update_rejected'):
                result = await gate.request_approval_cli(candidate, spec)

        assert not result
