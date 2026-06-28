"""
On-chain Metaplex metadata fetcher — resolves the full metadata JSON for a token.

DISCLAIMER: Educational system for Solana devnet only.

Pipeline:
  1. Derive the Metaplex metadata PDA from mint address.
  2. Call getAccountInfo on the PDA to get the raw account data.
  3. Parse the Borsh-encoded metadata to extract the off-chain URI field.
  4. HTTP GET the URI (IPFS/Arweave/HTTP) to fetch the full metadata JSON.
  5. Return image URL, description, attributes, and the full JSON.

This enables the mimicry engine to clone tokens with high fidelity because we
have the full original metadata to work from.
"""

import asyncio
import base64
import json
import struct
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from utils.logger import get_logger
from utils.rpc import SolanaRPCClient

logger = get_logger(__name__)

# Metaplex Token Metadata program on all clusters
TOKEN_METADATA_PROGRAM_ID = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

# Common IPFS gateways to try in order
IPFS_GATEWAYS = [
    "https://nftstorage.link/ipfs/",
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
]


@dataclass
class FetchedMetadata:
    """Result of fetching a token's on-chain + off-chain metadata."""

    token_address: str = ""
    metadata_pda: str = ""
    metadata_uri: str = ""         # The URI stored on-chain
    image_uri: str = ""            # From the JSON's "image" field
    name: str = ""
    symbol: str = ""
    description: str = ""
    attributes: list[dict] = field(default_factory=list)
    metadata_json: dict = field(default_factory=dict)
    # Status flags
    pda_found: bool = False
    uri_resolved: bool = False
    json_fetched: bool = False
    error: str = ""


class MetadataFetcher:
    """
    Fetches Metaplex metadata for any Solana SPL token.

    Usage:
        async with MetadataFetcher() as fetcher:
            meta = await fetcher.fetch("So11111111111111111111111111111111111111112")
            print(meta.image_uri, meta.description)
    """

    def __init__(self):
        self._rpc = SolanaRPCClient()
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch(self, token_address: str) -> FetchedMetadata:
        """
        Full pipeline: PDA → account data → off-chain JSON.
        Never raises — errors are captured in result.error.
        """
        result = FetchedMetadata(token_address=token_address)

        try:
            pda = self._derive_metadata_pda(token_address)
            result.metadata_pda = pda

            account_data = await self._rpc.get_account_info(pda)
            if not account_data:
                result.error = "metadata PDA account not found (token may lack on-chain metadata)"
                return result

            result.pda_found = True
            uri, name, symbol = self._parse_metadata_account(account_data)
            result.metadata_uri = uri
            result.name = name
            result.symbol = symbol

            if not uri:
                result.error = "metadata URI is empty in on-chain account"
                return result

            result.uri_resolved = True

            metadata_json = await self._fetch_json(uri)
            if metadata_json is None:
                result.error = f"failed to fetch JSON from URI: {uri}"
                return result

            result.json_fetched = True
            result.metadata_json = metadata_json
            result.image_uri = metadata_json.get("image", "")
            result.description = metadata_json.get("description", "")
            result.attributes = metadata_json.get("attributes", [])
            # Fill name/symbol from JSON if on-chain was empty
            if not result.name:
                result.name = metadata_json.get("name", "")
            if not result.symbol:
                result.symbol = metadata_json.get("symbol", "")

            logger.info(
                f"Fetched metadata for {token_address[:8]}…: "
                f"name={result.name!r} image={'✓' if result.image_uri else '✗'}"
            )

        except Exception as exc:
            result.error = str(exc)
            logger.warning(f"MetadataFetcher error for {token_address}: {exc}")

        return result

    async def fetch_batch(
        self, addresses: list[str], concurrency: int = 5
    ) -> list[FetchedMetadata]:
        """Fetch metadata for multiple tokens concurrently."""
        semaphore = asyncio.Semaphore(concurrency)

        async def _guarded(addr: str) -> FetchedMetadata:
            async with semaphore:
                return await self.fetch(addr)

        tasks = [_guarded(addr) for addr in addresses]
        return await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # PDA derivation
    # ------------------------------------------------------------------

    def _derive_metadata_pda(self, mint: str) -> str:
        """
        Derive the Metaplex metadata PDA using the canonical seeds:
            ["metadata", TOKEN_METADATA_PROGRAM_ID, mint]

        This is equivalent to the JS:
            PublicKey.findProgramAddressSync(
                [Buffer.from("metadata"), TOKEN_METADATA_PROGRAM_ID.toBuffer(), mint.toBuffer()],
                TOKEN_METADATA_PROGRAM_ID
            )
        """
        from solders.pubkey import Pubkey  # type: ignore

        metadata_program = Pubkey.from_string(TOKEN_METADATA_PROGRAM_ID)
        mint_pubkey = Pubkey.from_string(mint)

        seeds = [
            b"metadata",
            bytes(metadata_program),
            bytes(mint_pubkey),
        ]
        pda, _ = Pubkey.find_program_address(seeds, metadata_program)
        return str(pda)

    # ------------------------------------------------------------------
    # Account data parsing (Borsh)
    # ------------------------------------------------------------------

    def _parse_metadata_account(self, account_data: dict) -> tuple[str, str, str]:
        """
        Parse the Metaplex metadata account to extract URI, name, symbol.

        The Metaplex Metadata V1 layout (Borsh-encoded):
          [0]    1 byte  — key (4 = MetadataV1)
          [1-33] 32 bytes — update authority pubkey
          [33-65] 32 bytes — mint pubkey
          [65]   4 bytes  — name length (u32 le)
          [69..] name string (padded to 36 chars)
          [...]  4 bytes  — symbol length
          [...]  symbol string (padded to 14 chars)
          [...]  4 bytes  — uri length
          [...]  uri string (padded to 204 chars)

        We parse the base64-encoded data from the RPC response.
        """
        try:
            # account_data comes from getAccountInfo response
            data_field = account_data.get("data", [])
            if isinstance(data_field, list) and len(data_field) >= 1:
                raw = base64.b64decode(data_field[0])
            elif isinstance(data_field, str):
                raw = base64.b64decode(data_field)
            else:
                return "", "", ""

            offset = 1 + 32 + 32  # skip key + update_authority + mint

            # Name (u32 len + padded string)
            name_len = struct.unpack_from("<I", raw, offset)[0]
            offset += 4
            name = raw[offset: offset + name_len].decode("utf-8", errors="replace").rstrip("\x00")
            offset += 36  # name is always padded to 36 bytes

            # Symbol (u32 len + padded string)
            sym_len = struct.unpack_from("<I", raw, offset)[0]
            offset += 4
            symbol = raw[offset: offset + sym_len].decode("utf-8", errors="replace").rstrip("\x00")
            offset += 14  # symbol padded to 14 bytes

            # URI (u32 len + padded string)
            uri_len = struct.unpack_from("<I", raw, offset)[0]
            offset += 4
            uri = raw[offset: offset + uri_len].decode("utf-8", errors="replace").rstrip("\x00")

            return uri.strip(), name.strip(), symbol.strip()

        except Exception as exc:
            logger.debug(f"Metadata account parse error: {exc}")
            return "", "", ""

    # ------------------------------------------------------------------
    # JSON fetching
    # ------------------------------------------------------------------

    async def _fetch_json(self, uri: str) -> Optional[dict]:
        """
        Fetch the JSON at the metadata URI.
        Handles: https://, ipfs://, ar:// (Arweave).
        """
        resolved_url = self._resolve_uri(uri)
        if not resolved_url:
            return None

        for url in self._uri_candidates(resolved_url, uri):
            result = await self._try_fetch_json(url)
            if result is not None:
                return result

        return None

    async def _try_fetch_json(self, url: str) -> Optional[dict]:
        """Attempt to GET a URL and parse as JSON."""
        if not self._session:
            return None
        try:
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return json.loads(text)
        except (aiohttp.ClientError, json.JSONDecodeError, asyncio.TimeoutError):
            pass
        return None

    def _resolve_uri(self, uri: str) -> str:
        """Convert ipfs:// and ar:// URIs to https:// URLs."""
        if not uri:
            return ""

        if uri.startswith("https://") or uri.startswith("http://"):
            return uri

        if uri.startswith("ipfs://"):
            cid = uri[len("ipfs://"):]
            return f"{IPFS_GATEWAYS[0]}{cid}"

        if uri.startswith("ar://"):
            tx_id = uri[len("ar://"):]
            return f"https://arweave.net/{tx_id}"

        return uri

    def _uri_candidates(self, resolved: str, original: str) -> list[str]:
        """
        Return a list of URLs to try for an IPFS resource,
        cycling through gateways for redundancy.
        """
        urls = [resolved]

        # If it's an IPFS URL, add gateway fallbacks
        if "ipfs/" in resolved or "ipfs.io" in resolved or "nftstorage" in resolved:
            # Extract CID
            for part in ["/ipfs/", "ipfs/"]:
                if part in resolved:
                    cid_path = resolved.split(part, 1)[-1]
                    for gw in IPFS_GATEWAYS:
                        candidate = f"{gw}{cid_path}"
                        if candidate not in urls:
                            urls.append(candidate)
                    break

        return urls


# ------------------------------------------------------------------
# Module-level convenience function
# ------------------------------------------------------------------

async def fetch_token_metadata(token_address: str) -> FetchedMetadata:
    """Convenience wrapper — creates a one-shot fetcher session."""
    async with MetadataFetcher() as fetcher:
        return await fetcher.fetch(token_address)
