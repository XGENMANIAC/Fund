"""
SPL Token mint creation module.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
Use exclusively on Solana devnet/testnet.
Do NOT use on mainnet without full legal and compliance review.

This module:
  1. Loads the devnet wallet keypair.
  2. Creates a new SPL Token mint with specified supply and decimals.
  3. Creates the associated token account for the deployer.
  4. Mints the full supply to the deployer's account.
  5. Optionally revokes the mint and freeze authorities (for fair-launch simulation).

Library used: solana-py (free, open-source)
  https://github.com/michaelhly/solana-py

The Metaplex Token Metadata program is called via a separate instruction
(see metadata.py) to attach name, symbol, and URI to the mint.
"""

import asyncio
import os
from pathlib import Path
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.system_program import CreateAccountParams, create_account
from spl.token.async_client import AsyncToken
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import (
    create_associated_token_account,
    get_associated_token_address,
    initialize_mint,
    InitializeMintParams,
    mint_to,
    MintToParams,
    set_authority,
    SetAuthorityParams,
    AuthorityType,
)
from solders.rent import Rent

from analysis.generator import GeneratedTokenSpec
from storage.database import update_deployment
from utils.logger import get_logger
from utils.helpers import load_config

logger = get_logger(__name__)


def _load_keypair(path: str | None = None) -> Keypair:
    """Load a Solana keypair from a JSON file (array of 64 bytes)."""
    keypair_path = path or os.getenv("WALLET_KEYPAIR_PATH", "~/.config/solana/devnet-test.json")
    keypair_path = str(Path(keypair_path).expanduser())

    with open(keypair_path) as f:
        import json
        key_bytes = json.load(f)

    return Keypair.from_bytes(bytes(key_bytes))


def _get_rpc_url() -> str:
    cfg = load_config()
    network = cfg.get("network", "devnet")
    endpoints = cfg.get("rpc", {}).get("endpoints", ["https://api.devnet.solana.com"])

    if network == "mainnet-beta":
        raise ValueError(
            "MAINNET BLOCKED: This educational system refuses to operate on mainnet. "
            "Set network=devnet in config.yaml."
        )

    return endpoints[0]


class SPLTokenDeployer:
    """
    Deploys a new SPL Token mint on Solana devnet.

    Full pipeline:
      1. create_mint()       — allocate mint account
      2. create_ata()        — create associated token account for deployer
      3. mint_supply()       — mint full supply to deployer ATA
      4. revoke_authorities() — optional: revoke mint + freeze for fairness
    """

    def __init__(self, keypair_path: str | None = None):
        cfg = load_config()
        self._network = cfg.get("network", "devnet")
        if self._network == "mainnet-beta":
            raise ValueError("MAINNET BLOCKED: devnet only.")

        self._rpc_url = _get_rpc_url()
        self._payer = _load_keypair(keypair_path)
        deploy_cfg = cfg.get("deploy", {})
        self._revoke_mint = deploy_cfg.get("revoke_mint_authority", True)
        self._revoke_freeze = deploy_cfg.get("revoke_freeze_authority", True)

    async def deploy_token(
        self,
        spec: GeneratedTokenSpec,
        deployment_id: Optional[int] = None,
    ) -> dict:
        """
        Full token deployment pipeline.

        Returns a dict with:
          mint_address, mint_tx, ata_address, supply_tx
          (and revoke_tx if authorities were revoked)
        """
        logger.info(
            f"[DEVNET] Deploying token: {spec.new_name} ({spec.new_symbol}) "
            f"supply={spec.new_supply:,} decimals={spec.new_decimals}"
        )

        async with AsyncClient(self._rpc_url, commitment=Confirmed) as client:
            # Step 1: Create the mint account
            mint_kp = Keypair()
            mint_address = str(mint_kp.pubkey())
            logger.info(f"New mint keypair: {mint_address}")

            mint_tx = await self._create_mint_account(
                client, mint_kp, spec.new_decimals
            )
            logger.info(f"Mint created: tx={mint_tx[:8]}...")

            if deployment_id:
                update_deployment(deployment_id, mint_address=mint_address, mint_tx=mint_tx)

            # Step 2: Create deployer's associated token account
            ata = get_associated_token_address(self._payer.pubkey(), mint_kp.pubkey())
            ata_tx = await self._create_ata(client, mint_kp.pubkey())
            logger.info(f"ATA created: {ata} tx={ata_tx[:8]}...")

            # Step 3: Mint full supply to deployer ATA
            supply_tx = await self._mint_supply(
                client,
                mint_kp,
                ata,
                spec.new_supply,
                spec.new_decimals,
            )
            logger.info(
                f"Minted {spec.new_supply:,} tokens: tx={supply_tx[:8]}..."
            )

            result = {
                "mint_address": mint_address,
                "mint_keypair": mint_kp,  # needed for authority revocation
                "ata_address": str(ata),
                "mint_tx": mint_tx,
                "supply_tx": supply_tx,
            }

            # Step 4: Revoke authorities (optional)
            if self._revoke_mint:
                rev_tx = await self._revoke_mint_authority(client, mint_kp)
                result["revoke_mint_tx"] = rev_tx
                logger.info(f"Mint authority revoked: tx={rev_tx[:8]}...")

            if self._revoke_freeze:
                rev_tx = await self._revoke_freeze_authority(client, mint_kp)
                result["revoke_freeze_tx"] = rev_tx
                logger.info(f"Freeze authority revoked: tx={rev_tx[:8]}...")

            return result

    # ------------------------------------------------------------------
    # Step implementations
    # ------------------------------------------------------------------

    async def _create_mint_account(
        self,
        client: AsyncClient,
        mint_kp: Keypair,
        decimals: int,
    ) -> str:
        """
        Create and initialize the SPL Token mint account.
        This is a 2-instruction transaction:
          1. system_program::create_account — allocate account space
          2. token::initialize_mint        — set decimals + authorities
        """
        # Get minimum rent for a mint account (82 bytes)
        resp = await client.get_minimum_balance_for_rent_exemption(82)
        lamports = resp.value

        blockhash_resp = await client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash

        # Instruction 1: Create system account for the mint
        create_ix = create_account(
            CreateAccountParams(
                from_pubkey=self._payer.pubkey(),
                to_pubkey=mint_kp.pubkey(),
                lamports=lamports,
                space=82,
                owner=TOKEN_PROGRAM_ID,
            )
        )

        # Instruction 2: Initialize the mint
        init_ix = initialize_mint(
            InitializeMintParams(
                program_id=TOKEN_PROGRAM_ID,
                mint=mint_kp.pubkey(),
                decimals=decimals,
                mint_authority=self._payer.pubkey(),
                freeze_authority=self._payer.pubkey(),
            )
        )

        tx = Transaction(
            fee_payer=self._payer.pubkey(),
            instructions=[create_ix, init_ix],
            recent_blockhash=blockhash,
        )
        tx.sign(self._payer, mint_kp)

        resp = await client.send_transaction(tx)
        sig = str(resp.value)
        await self._wait_for_confirmation(client, sig)
        return sig

    async def _create_ata(self, client: AsyncClient, mint: Pubkey) -> str:
        """Create the deployer's associated token account for this mint."""
        blockhash_resp = await client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash

        create_ata_ix = create_associated_token_account(
            payer=self._payer.pubkey(),
            owner=self._payer.pubkey(),
            mint=mint,
        )

        tx = Transaction(
            fee_payer=self._payer.pubkey(),
            instructions=[create_ata_ix],
            recent_blockhash=blockhash,
        )
        tx.sign(self._payer)

        resp = await client.send_transaction(tx)
        sig = str(resp.value)
        await self._wait_for_confirmation(client, sig)
        return sig

    async def _mint_supply(
        self,
        client: AsyncClient,
        mint_kp: Keypair,
        destination: Pubkey,
        supply: int,
        decimals: int,
    ) -> str:
        """Mint the full token supply to the deployer's ATA."""
        blockhash_resp = await client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash

        # Amount in smallest units (supply * 10^decimals)
        amount = supply * (10 ** decimals)

        mint_ix = mint_to(
            MintToParams(
                program_id=TOKEN_PROGRAM_ID,
                mint=mint_kp.pubkey(),
                dest=destination,
                mint_authority=self._payer.pubkey(),
                amount=amount,
                signers=[],
            )
        )

        tx = Transaction(
            fee_payer=self._payer.pubkey(),
            instructions=[mint_ix],
            recent_blockhash=blockhash,
        )
        tx.sign(self._payer)

        resp = await client.send_transaction(tx)
        sig = str(resp.value)
        await self._wait_for_confirmation(client, sig)
        return sig

    async def _revoke_mint_authority(
        self, client: AsyncClient, mint_kp: Keypair
    ) -> str:
        """
        Revoke the mint authority — no more tokens can ever be minted.
        This is a key "fair launch" signal. After this, token supply is fixed.
        """
        blockhash_resp = await client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash

        revoke_ix = set_authority(
            SetAuthorityParams(
                program_id=TOKEN_PROGRAM_ID,
                account=mint_kp.pubkey(),
                authority=AuthorityType.MINT_TOKENS,
                current_authority=self._payer.pubkey(),
                new_authority=None,  # None = revoked
                signers=[],
            )
        )

        tx = Transaction(
            fee_payer=self._payer.pubkey(),
            instructions=[revoke_ix],
            recent_blockhash=blockhash,
        )
        tx.sign(self._payer)

        resp = await client.send_transaction(tx)
        sig = str(resp.value)
        await self._wait_for_confirmation(client, sig)
        return sig

    async def _revoke_freeze_authority(
        self, client: AsyncClient, mint_kp: Keypair
    ) -> str:
        """
        Revoke the freeze authority — no account can be frozen.
        Prevents the deployer from rug-pulling by freezing holder accounts.
        """
        blockhash_resp = await client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash

        revoke_ix = set_authority(
            SetAuthorityParams(
                program_id=TOKEN_PROGRAM_ID,
                account=mint_kp.pubkey(),
                authority=AuthorityType.FREEZE_ACCOUNT,
                current_authority=self._payer.pubkey(),
                new_authority=None,
                signers=[],
            )
        )

        tx = Transaction(
            fee_payer=self._payer.pubkey(),
            instructions=[revoke_ix],
            recent_blockhash=blockhash,
        )
        tx.sign(self._payer)

        resp = await client.send_transaction(tx)
        sig = str(resp.value)
        await self._wait_for_confirmation(client, sig)
        return sig

    # ------------------------------------------------------------------
    # Transaction confirmation
    # ------------------------------------------------------------------

    async def _wait_for_confirmation(
        self,
        client: AsyncClient,
        signature: str,
        max_retries: int = 30,
        sleep_s: float = 2.0,
    ) -> bool:
        """Poll until a transaction is confirmed or max_retries is reached."""
        for _ in range(max_retries):
            resp = await client.get_signature_statuses([signature])
            statuses = resp.value
            if statuses and statuses[0]:
                status = statuses[0]
                if status.err:
                    raise RuntimeError(f"Transaction failed: {status.err}")
                if status.confirmation_status in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(sleep_s)

        raise TimeoutError(f"Transaction {signature} not confirmed in time")
