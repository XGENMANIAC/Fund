"""
Full deployment pipeline — orchestrates SPL token + metadata + Raydium pool.

DISCLAIMER: Educational system for Solana devnet only.
All operations must be on devnet/testnet only.

Pipeline sequence:
  1. Serve metadata locally (or upload to IPFS)
  2. Deploy SPL token mint + mint supply
  3. Attach Metaplex Token Metadata
  4. Deploy Raydium pool (market + AMM)
  5. Update DB with all results
  6. Return summary

This pipeline is only triggered AFTER manual approval by the operator.
"""

import asyncio
import json
import os
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

from analysis.generator import GeneratedTokenSpec, TokenGenerator
from analysis.scorer import ScoredCandidate
from deploy.spl_token import SPLTokenDeployer
from deploy.metadata import MetadataAttacher
from deploy.raydium import RaydiumDeployer
from storage.database import (
    update_deployment,
    insert_deployment,
    update_candidate_status,
    approve_generated_token,
)
from utils.logger import get_logger, log_banner
from utils.helpers import load_config

logger = get_logger(__name__)

NETWORK = os.getenv("NETWORK", "devnet").lower()


class DeploymentPipeline:
    """
    Orchestrates the full token deployment sequence.

    Usage:
        pipeline = DeploymentPipeline()
        result = await pipeline.run(spec, generated_token_db_id)
    """

    def __init__(self):
        cfg = load_config()
        network_raw = cfg.get("network", "devnet")
        if isinstance(network_raw, dict):
            config_network = network_raw.get("name", "devnet")
        else:
            config_network = network_raw
        self._network = os.getenv("NETWORK", config_network).lower()

        if self._network == "mainnet":
            confirm = input(
                "⚠️  YOU ARE DEPLOYING TO MAINNET - REAL MONEY. "
                "Type 'YES_MAINNET' to continue: "
            )
            if confirm != "YES_MAINNET":
                raise SystemExit("Mainnet deployment aborted.")

        self._metadata_provider = cfg.get("metadata", {}).get("provider", "local_uri")
        self._local_port = cfg.get("metadata", {}).get("local_server_port", 8765)
        self._metadata_server: Optional[HTTPServer] = None

    # ------------------------------------------------------------------
    # Main pipeline entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        spec: GeneratedTokenSpec,
        candidate_db_id: int,
    ) -> dict:
        """
        Run the complete deployment pipeline.

        Returns a result dict with success flag and all deployment details.
        """
        log_banner(logger, f"DEPLOYING: {spec.new_name} ({spec.new_symbol})")

        deployment_id = insert_deployment(
            generated_token_id=spec.db_id or 0,
            network=self._network,
        )
        result = {
            "success": False,
            "deployment_id": deployment_id,
            "network": self._network,
            "token_name": spec.new_name,
            "token_symbol": spec.new_symbol,
            "mint_address": None,
            "pool_id": None,
            "market_id": None,
            "steps_completed": [],
        }

        try:
            metadata_uri = await self._prepare_metadata(spec)
            spec.metadata_uri = metadata_uri
            result["steps_completed"].append("metadata")
            logger.info(f"Metadata URI: {metadata_uri}")

            token_deployer = SPLTokenDeployer()
            token_result = await token_deployer.deploy_token(spec, deployment_id)

            mint_address = token_result["mint_address"]
            mint_keypair = token_result["mint_keypair"]
            result["mint_address"] = mint_address
            result["steps_completed"].append("spl_token")

            update_deployment(
                deployment_id,
                mint_address=mint_address,
                mint_tx=token_result.get("mint_tx", ""),
                mint_revoked=int(token_result.get("revoke_mint_tx") is not None),
                freeze_revoked=int(token_result.get("revoke_freeze_tx") is not None),
                status="partial",
            )

            logger.info("Attaching on-chain metadata via Metaplex...")
            meta_attacher = MetadataAttacher()
            metadata_tx = await meta_attacher.attach(
                mint_address=mint_address,
                name=spec.new_name,
                symbol=spec.new_symbol,
                uri=metadata_uri,
            )
            update_deployment(deployment_id, metadata_tx=metadata_tx)
            result["steps_completed"].append("metadata_onchain")

            logger.info("Creating Raydium pool...")
            raydium_deployer = RaydiumDeployer()
            raydium_result = await raydium_deployer.deploy(
                spec=spec,
                mint_address=mint_address,
                deployment_id=deployment_id,
            )

            if raydium_result.get("success"):
                result["pool_id"] = raydium_result.get("pool_id")
                result["market_id"] = raydium_result.get("market_id")
                result["steps_completed"].append("raydium_market")
                result["steps_completed"].append("raydium_pool")
                update_deployment(deployment_id, status="complete")
                result["success"] = True
            else:
                error = raydium_result.get("error", "Unknown Raydium error")
                logger.error(f"Raydium deploy failed: {error}")
                update_deployment(
                    deployment_id,
                    status="failed",
                    error_message=f"Raydium: {error}",
                )
                result["error"] = error

        except Exception as exc:
            logger.exception(f"Deployment pipeline failed: {exc}")
            update_deployment(deployment_id, status="failed", error_message=str(exc))
            result["error"] = str(exc)

        finally:
            self._stop_local_metadata_server()

        if result["success"]:
            log_banner(logger, f"DEPLOYED SUCCESSFULLY: {spec.new_name}")
            logger.info(f"  Mint:    {result['mint_address']}")
            logger.info(f"  Pool:    {result['pool_id']}")
            logger.info(f"  Market:  {result['market_id']}")
        else:
            logger.error(f"Deployment failed after steps: {result['steps_completed']}")

        return result

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    async def _prepare_metadata(self, spec: GeneratedTokenSpec) -> str:
        """
        Prepare metadata based on configured provider.
        Returns the metadata URI to embed in the on-chain record.
        """
        generator = TokenGenerator()

        if self._metadata_provider == "local_uri":
            local_path = generator.save_metadata_locally(spec)
            uri = self._start_local_metadata_server(spec.new_symbol)
            return uri

        elif self._metadata_provider == "nft_storage":
            uri = await generator.upload_to_nft_storage(spec)
            return uri

        else:
            import base64
            json_bytes = json.dumps(spec.metadata_json).encode()
            b64 = base64.b64encode(json_bytes).decode()
            return f"data:application/json;base64,{b64}"

    def _start_local_metadata_server(self, symbol: str) -> str:
        """
        Spin up a tiny HTTP server to serve the metadata JSON.
        Only accessible from localhost — fine for devnet testing.
        """
        metadata_dir = Path("data/metadata")
        port = self._local_port

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(metadata_dir), **kwargs)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._metadata_server = server
        logger.info(f"Local metadata server running on port {port}")

        return f"http://localhost:{port}/{symbol.lower()}.json"

    def _stop_local_metadata_server(self) -> None:
        if self._metadata_server:
            self._metadata_server.shutdown()
            self._metadata_server = None


# ---------------------------------------------------------------------------
# Convenience function for the bot pipeline
# ---------------------------------------------------------------------------

async def deploy_approved_token(
    candidate: ScoredCandidate,
    spec: GeneratedTokenSpec,
) -> dict:
    """
    Top-level function called after approval.
    Runs the full deployment pipeline.
    """
    if not spec.db_id or not candidate.db_id:
        raise ValueError("Spec and candidate must be persisted to DB before deployment.")

    approve_generated_token(spec.db_id)
    update_candidate_status(candidate.db_id, "deployed")

    pipeline = DeploymentPipeline()
    return await pipeline.run(spec, candidate.db_id)
