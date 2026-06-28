"""
Python wrapper for the Node.js Raydium deployment scripts.

DISCLAIMER: Educational system for Solana devnet only.
Do NOT use on mainnet without full legal and compliance review.

Raydium's official SDK is JavaScript/TypeScript-only. This module bridges
Python → Node.js via subprocess, passing parameters as JSON and parsing
the structured JSON output.

Flow:
  1. Build a params dict from the GeneratedTokenSpec + SPL deploy result
  2. Write params to a temp JSON file
  3. Invoke: node js/raydium_deploy.js <params.json>
  4. Parse the "__RESULT__" JSON line from stdout
  5. Update the deployment DB record
  6. Return structured result

This approach is idiomatic in Solana Python tooling where the SDKs are
not fully ported from JS.
"""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from analysis.generator import GeneratedTokenSpec
from storage.database import update_deployment
from utils.logger import get_logger
from utils.helpers import load_config

logger = get_logger(__name__)


class RaydiumDeployer:
    """
    Deploys a Raydium AMM pool by delegating to the Node.js SDK scripts.

    Usage:
        deployer = RaydiumDeployer()
        result = await deployer.deploy(spec, mint_address, deployment_id)
    """

    def __init__(self):
        cfg = load_config()
        network = cfg.get("network", "devnet")
        if network == "mainnet-beta":
            raise ValueError("MAINNET BLOCKED: devnet only.")

        self._network = network
        deploy_cfg = cfg.get("deploy", {})
        self._initial_sol = deploy_cfg.get("initial_sol", 0.1)
        self._js_dir = Path(__file__).parent.parent / "js"

    async def deploy(
        self,
        spec: GeneratedTokenSpec,
        mint_address: str,
        deployment_id: Optional[int] = None,
        initial_tokens: Optional[int] = None,
    ) -> dict:
        """
        Run the full Raydium deploy pipeline (market + pool creation).

        Args:
            spec: The generated token spec.
            mint_address: The deployed SPL token mint address.
            deployment_id: DB record to update with results.
            initial_tokens: Override token amount for the pool.

        Returns:
            Dict with market_id, pool_id, lp_mint, txids, success flag.
        """
        tokens_in_pool = initial_tokens or spec.pool_tokens

        params = {
            "mint_address": mint_address,
            "token_name": spec.new_name,
            "token_symbol": spec.new_symbol,
            "decimals": spec.new_decimals,
            "initial_sol": self._initial_sol,
            "initial_tokens": tokens_in_pool,
            "network": self._network,
        }

        logger.info(
            f"[DEVNET] Raydium deploy: {spec.new_name} ({spec.new_symbol}) "
            f"pool_tokens={tokens_in_pool:,} initial_sol={self._initial_sol}"
        )

        result = await self._call_node_deployer(params)

        if deployment_id and result.get("success"):
            update_deployment(
                deployment_id,
                market_id=result.get("market_id", ""),
                pool_id=result.get("pool_id", ""),
                pool_tx=",".join(result.get("pool_txids", [])),
                sol_added=self._initial_sol,
                tokens_added=tokens_in_pool,
            )

        return result

    async def _call_node_deployer(self, params: dict) -> dict:
        """
        Write params to a temp JSON file, invoke node, parse stdout for __RESULT__.
        Runs the subprocess in a thread pool (non-blocking for asyncio).
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._run_node_sync, params)

    def _run_node_sync(self, params: dict) -> dict:
        """Synchronous subprocess call — run via run_in_executor."""
        # Check Node.js is available
        if not self._check_node():
            return {
                "success": False,
                "error": "Node.js not found. Install Node.js 18+ to run Raydium deployment.",
            }

        # Check npm dependencies
        node_modules = self._js_dir / "node_modules"
        if not node_modules.exists():
            logger.error("Node.js dependencies not installed. Run: cd js && npm install")
            return {
                "success": False,
                "error": "Node.js dependencies not installed. Run: cd js && npm install",
            }

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(params, f)
            params_path = f.name

        try:
            env = {
                **os.environ,
                "NETWORK": self._network,
                "PRIMARY_RPC": self._get_rpc_url(),
                "WALLET_KEYPAIR_PATH": os.getenv(
                    "WALLET_KEYPAIR_PATH",
                    str(Path.home() / ".config/solana/devnet-test.json"),
                ),
            }

            logger.info(f"Invoking Node.js deployer: node raydium_deploy.js")

            proc = subprocess.run(
                ["node", "raydium_deploy.js", params_path],
                capture_output=True,
                text=True,
                cwd=str(self._js_dir),
                env=env,
                timeout=300,  # 5 minutes max
            )

            # Log Node.js output for debugging
            if proc.stdout:
                for line in proc.stdout.splitlines():
                    logger.debug(f"[node] {line}")
            if proc.stderr:
                for line in proc.stderr.splitlines():
                    logger.warning(f"[node-err] {line}")

            # Extract the structured JSON result
            result = self._parse_node_result(proc.stdout)

            if proc.returncode != 0 and not result.get("success"):
                result["error"] = result.get("error") or "Node.js process exited non-zero"

            return result

        except subprocess.TimeoutExpired:
            logger.error("Node.js deployment timed out after 5 minutes")
            return {"success": False, "error": "Deployment timed out"}
        except FileNotFoundError:
            return {"success": False, "error": "Node.js (node) not found in PATH"}
        finally:
            try:
                os.unlink(params_path)
            except OSError:
                pass

    def _parse_node_result(self, stdout: str) -> dict:
        """
        Parse the JSON block after '__RESULT__' in the Node.js output.
        Falls back to a failed result if not found.
        """
        lines = stdout.splitlines()
        result_start = None
        for i, line in enumerate(lines):
            if "__RESULT__" in line:
                result_start = i + 1
                break

        if result_start is None:
            return {"success": False, "error": "No __RESULT__ marker in Node.js output"}

        result_json = "\n".join(lines[result_start:])
        try:
            return json.loads(result_json.strip())
        except json.JSONDecodeError as exc:
            return {"success": False, "error": f"JSON parse error: {exc}"}

    def _check_node(self) -> bool:
        """Return True if node is available in PATH."""
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _get_rpc_url(self) -> str:
        cfg = load_config()
        return cfg.get("rpc", {}).get("endpoints", ["https://api.devnet.solana.com"])[0]
