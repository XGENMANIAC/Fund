"""
Metaplex Token Metadata attachment module.

DISCLAIMER: Educational system for Solana devnet only.
Do NOT use on mainnet without full legal and compliance review.

This module attaches on-chain metadata (name, symbol, URI) to an SPL token
mint using the Metaplex Token Metadata program (mpl-token-metadata).

The metadata URI points to a JSON file (Metaplex standard) hosted:
  - Locally (for devnet testing)
  - On IPFS via NFT.storage free tier (optional)

The Metaplex metadata PDA (Program Derived Address) is derived from:
  seeds = ["metadata", TOKEN_METADATA_PROGRAM_ID, mint_address]

We construct and send the CreateMetadataAccountV3 instruction directly
via raw transaction building (no Anchor needed for this specific instruction).
"""

import asyncio
import hashlib
import os
import struct
from typing import Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction

from utils.logger import get_logger
from utils.helpers import load_config

logger = get_logger(__name__)

# Metaplex Token Metadata Program ID (same on devnet and mainnet)
TOKEN_METADATA_PROGRAM_ID = Pubkey.from_string(
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
)

# System Program
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# Sysvar Rent
SYSVAR_RENT_PUBKEY = Pubkey.from_string("SysvarRent111111111111111111111111111111111")


def find_metadata_pda(mint: Pubkey) -> tuple[Pubkey, int]:
    """
    Derive the Metaplex metadata PDA for a given mint address.
    PDA seeds: ["metadata", TOKEN_METADATA_PROGRAM_ID, mint]
    """
    seeds = [
        b"metadata",
        bytes(TOKEN_METADATA_PROGRAM_ID),
        bytes(mint),
    ]
    return Pubkey.find_program_address(seeds, TOKEN_METADATA_PROGRAM_ID)


def _encode_create_metadata_v3_ix(
    name: str,
    symbol: str,
    uri: str,
    seller_fee_basis_points: int = 0,
    creators: Optional[list] = None,
    is_mutable: bool = True,
) -> bytes:
    """
    Manually encode the CreateMetadataAccountV3 instruction data.

    Instruction discriminator: [33, 132, 147, 227, 182, 97, 115, 41] (Anchor hash)
    Followed by:
      - DataV2 struct (name, symbol, uri, seller_fee_bps, creators, collection, uses)

    This is a simplified encoder — a production system would use the
    @metaplex-foundation/mpl-token-metadata JS SDK or anchorpy.
    """
    # Instruction discriminant for CreateMetadataAccountV3
    # Computed as: sha256("global:create_metadata_accounts_v3")[:8]
    discriminant = bytes([33, 132, 147, 227, 182, 97, 115, 41])

    def encode_string(s: str) -> bytes:
        """Borsh string encoding: 4-byte little-endian length + UTF-8 bytes."""
        b = s.encode("utf-8")
        return struct.pack("<I", len(b)) + b

    def encode_option_none() -> bytes:
        return b"\x00"

    # DataV2 encoding
    data = (
        encode_string(name[:32])
        + encode_string(symbol[:10])
        + encode_string(uri[:200])
        + struct.pack("<H", seller_fee_basis_points)
        # creators: Option<Vec<Creator>> — None for simplicity
        + encode_option_none()
        # collection: Option<Collection> — None
        + encode_option_none()
        # uses: Option<Uses> — None
        + encode_option_none()
    )

    # is_mutable flag
    is_mutable_byte = b"\x01" if is_mutable else b"\x00"
    # collection_details: Option — None
    collection_details = encode_option_none()

    return discriminant + data + is_mutable_byte + collection_details


class MetadataAttacher:
    """
    Attaches Metaplex Token Metadata to a newly created SPL token mint.

    Usage:
        attacher = MetadataAttacher()
        tx = await attacher.attach(mint_pubkey, payer_keypair, name, symbol, uri)
    """

    def __init__(self, keypair_path: str | None = None):
        cfg = load_config()
        network = cfg.get("network", "devnet")
        if network == "mainnet-beta":
            raise ValueError("MAINNET BLOCKED: devnet only.")

        endpoints = cfg.get("rpc", {}).get("endpoints", ["https://api.devnet.solana.com"])
        self._rpc_url = endpoints[0]

        # Load payer
        import json
        from pathlib import Path
        path = keypair_path or os.getenv("WALLET_KEYPAIR_PATH", "~/.config/solana/devnet-test.json")
        path = str(Path(path).expanduser())
        with open(path) as f:
            self._payer = Keypair.from_bytes(bytes(json.load(f)))

    async def attach(
        self,
        mint_address: str,
        name: str,
        symbol: str,
        uri: str,
        is_mutable: bool = True,
    ) -> str:
        """
        Attach metadata to a mint. Returns the transaction signature.

        Args:
            mint_address: The SPL Token mint address.
            name: Token name (max 32 chars).
            symbol: Token symbol (max 10 chars).
            uri: Metadata JSON URI (max 200 chars).
            is_mutable: If False, metadata cannot be updated later.
        """
        mint = Pubkey.from_string(mint_address)
        metadata_pda, _bump = find_metadata_pda(mint)

        logger.info(
            f"Attaching metadata to {mint_address[:8]}... "
            f"PDA={str(metadata_pda)[:8]}..."
        )

        # Build the instruction
        ix_data = _encode_create_metadata_v3_ix(
            name=name, symbol=symbol, uri=uri, is_mutable=is_mutable
        )

        accounts = [
            # metadata account (PDA — writable, not signer)
            AccountMeta(pubkey=metadata_pda, is_signer=False, is_writable=True),
            # mint (readonly)
            AccountMeta(pubkey=mint, is_signer=False, is_writable=False),
            # mint_authority (signer)
            AccountMeta(pubkey=self._payer.pubkey(), is_signer=True, is_writable=False),
            # payer (signer, writable — pays for PDA rent)
            AccountMeta(pubkey=self._payer.pubkey(), is_signer=True, is_writable=True),
            # update_authority (signer)
            AccountMeta(pubkey=self._payer.pubkey(), is_signer=True, is_writable=False),
            # system_program
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
            # rent sysvar
            AccountMeta(pubkey=SYSVAR_RENT_PUBKEY, is_signer=False, is_writable=False),
        ]

        ix = Instruction(
            program_id=TOKEN_METADATA_PROGRAM_ID,
            accounts=accounts,
            data=ix_data,
        )

        async with AsyncClient(self._rpc_url, commitment=Confirmed) as client:
            blockhash_resp = await client.get_latest_blockhash()
            blockhash = blockhash_resp.value.blockhash

            tx = Transaction(
                fee_payer=self._payer.pubkey(),
                instructions=[ix],
                recent_blockhash=blockhash,
            )
            tx.sign(self._payer)

            resp = await client.send_transaction(tx)
            sig = str(resp.value)

            await self._wait_for_confirmation(client, sig)
            logger.info(f"Metadata attached: tx={sig[:8]}...")
            return sig

    async def _wait_for_confirmation(
        self, client: AsyncClient, signature: str, retries: int = 30
    ) -> None:
        for _ in range(retries):
            resp = await client.get_signature_statuses([signature])
            status = resp.value[0] if resp.value else None
            if status and not status.err:
                if status.confirmation_status in ("confirmed", "finalized"):
                    return
            await asyncio.sleep(2)
        raise TimeoutError(f"Metadata tx {signature} not confirmed")
