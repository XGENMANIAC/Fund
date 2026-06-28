"""
Image and metadata re-upload pipeline for the mimicry system.

DISCLAIMER: Educational system for Solana devnet only.

This module downloads the source token's image and re-uploads it to a
permanent storage provider so the clone token can reference its own
hosted copy of the image (important for on-chain metadata credibility).

Provider priority:
  1. NFT.storage (free IPFS pinning, requires NFT_STORAGE_API_KEY)
  2. web3.storage (free IPFS pinning, requires WEB3_STORAGE_TOKEN)
  3. Local file serving (always available as final fallback)

IPFS CID ensures the same image stays permanently accessible even if
the original source disappears. The clone gets its own IPFS CID so
it's not dependent on the original's hosting.
"""

import asyncio
import io
import json
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp

from utils.logger import get_logger

logger = get_logger(__name__)

UPLOAD_TIMEOUT = 30  # seconds
DOWNLOAD_TIMEOUT = 15  # seconds
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB cap


@dataclass
class UploadResult:
    """Result of uploading an image or metadata JSON."""

    success: bool = False
    uri: str = ""           # Final accessible URI (https:// or ipfs://)
    cid: str = ""           # IPFS CID (if applicable)
    provider: str = ""      # "nft_storage" | "web3_storage" | "local"
    bytes_uploaded: int = 0
    error: str = ""


class ImageReuploader:
    """
    Downloads an image from a URL and re-uploads it to IPFS or local storage.

    Usage:
        async with ImageReuploader() as uploader:
            result = await uploader.reupload("https://original-image-url.png")
            print(result.uri)  # New IPFS URI
    """

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._nft_storage_key = os.getenv("NFT_STORAGE_API_KEY", "")
        self._web3_storage_token = os.getenv("WEB3_STORAGE_TOKEN", "")
        self._local_dir = Path("data/images")

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        self._local_dir.mkdir(parents=True, exist_ok=True)
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def reupload(self, source_url: str, filename_hint: str = "") -> UploadResult:
        """
        Download image from source_url and re-upload to storage.
        Returns UploadResult with the new URI.
        """
        if not source_url:
            return UploadResult(error="no source URL provided")

        # Step 1: Download
        image_data, content_type = await self._download(source_url)
        if not image_data:
            return UploadResult(error=f"failed to download: {source_url[:80]}")

        logger.info(
            f"Downloaded image: {len(image_data):,} bytes "
            f"type={content_type} from {source_url[:60]}…"
        )

        # Step 2: Upload (try providers in order)
        if self._nft_storage_key:
            result = await self._upload_nft_storage(image_data, content_type, filename_hint)
            if result.success:
                return result

        if self._web3_storage_token:
            result = await self._upload_web3_storage(image_data, content_type, filename_hint)
            if result.success:
                return result

        # Fallback: save locally
        return self._save_locally(image_data, content_type, filename_hint)

    async def upload_metadata_json(
        self, metadata: dict, symbol: str = "token"
    ) -> UploadResult:
        """
        Upload a metadata JSON dict to IPFS.
        Returns UploadResult with IPFS URI.
        """
        json_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")

        if self._nft_storage_key:
            result = await self._upload_nft_storage(
                json_bytes, "application/json", f"{symbol}.json"
            )
            if result.success:
                return result

        if self._web3_storage_token:
            result = await self._upload_web3_storage(
                json_bytes, "application/json", f"{symbol}.json"
            )
            if result.success:
                return result

        # Local fallback
        local_path = Path("data/metadata") / f"{symbol.lower()}_{int(time.time())}.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(json_bytes)
        return UploadResult(
            success=True,
            uri=f"file://{local_path.absolute()}",
            provider="local",
            bytes_uploaded=len(json_bytes),
        )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def _download(self, url: str) -> tuple[Optional[bytes], str]:
        """Download image bytes and detect content type. Returns (data, mime)."""
        if not self._session:
            return None, ""

        resolved = self._resolve_uri(url)
        if not resolved:
            return None, ""

        try:
            async with self._session.get(
                resolved,
                timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
            ) as resp:
                if resp.status >= 400:
                    logger.warning(f"Image download returned {resp.status}: {resolved[:80]}")
                    return None, ""

                content_type = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()

                # Stream with size cap
                chunks = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        logger.warning(f"Image too large (>{MAX_IMAGE_BYTES/1e6:.0f}MB), truncating")
                        break
                    chunks.append(chunk)

                return b"".join(chunks), content_type

        except asyncio.TimeoutError:
            logger.warning(f"Image download timed out: {resolved[:80]}")
            return None, ""
        except aiohttp.ClientError as exc:
            logger.warning(f"Image download error: {exc}")
            return None, ""

    # ------------------------------------------------------------------
    # Upload providers
    # ------------------------------------------------------------------

    async def _upload_nft_storage(
        self, data: bytes, content_type: str, filename: str = ""
    ) -> UploadResult:
        """
        Upload to NFT.storage free IPFS pinning.
        API docs: https://nft.storage/api-docs
        """
        if not self._session:
            return UploadResult(error="no session")

        try:
            async with self._session.post(
                "https://api.nft.storage/upload",
                data=data,
                headers={
                    "Authorization": f"Bearer {self._nft_storage_key}",
                    "Content-Type": content_type,
                },
                timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT),
            ) as resp:
                body = await resp.json()
                if resp.status == 200 and body.get("ok"):
                    cid = body["value"]["cid"]
                    uri = f"https://nftstorage.link/ipfs/{cid}"
                    logger.info(f"NFT.storage upload OK → {uri}")
                    return UploadResult(
                        success=True,
                        uri=uri,
                        cid=cid,
                        provider="nft_storage",
                        bytes_uploaded=len(data),
                    )
                error = body.get("error", {}).get("message", str(resp.status))
                return UploadResult(error=f"nft.storage: {error}")

        except Exception as exc:
            return UploadResult(error=f"nft.storage exception: {exc}")

    async def _upload_web3_storage(
        self, data: bytes, content_type: str, filename: str = "file"
    ) -> UploadResult:
        """
        Upload to web3.storage (Storacha) free IPFS pinning.
        Uses the simple /upload endpoint.
        """
        if not self._session:
            return UploadResult(error="no session")

        ext = mimetypes.guess_extension(content_type) or ""
        safe_filename = (filename or f"file{ext}").replace(" ", "_")

        form = aiohttp.FormData()
        form.add_field(
            "file",
            io.BytesIO(data),
            filename=safe_filename,
            content_type=content_type,
        )

        try:
            async with self._session.post(
                "https://api.web3.storage/upload",
                data=form,
                headers={"Authorization": f"Bearer {self._web3_storage_token}"},
                timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT),
            ) as resp:
                body = await resp.json()
                if resp.status == 200:
                    cid = body.get("cid", "")
                    uri = f"https://w3s.link/ipfs/{cid}"
                    logger.info(f"web3.storage upload OK → {uri}")
                    return UploadResult(
                        success=True,
                        uri=uri,
                        cid=cid,
                        provider="web3_storage",
                        bytes_uploaded=len(data),
                    )
                return UploadResult(error=f"web3.storage: {resp.status}")

        except Exception as exc:
            return UploadResult(error=f"web3.storage exception: {exc}")

    def _save_locally(
        self, data: bytes, content_type: str, filename: str = ""
    ) -> UploadResult:
        """Save image file locally as fallback. Returns file:// URI."""
        ext = mimetypes.guess_extension(content_type) or ".png"
        safe_name = (filename or f"img_{int(time.time())}").replace(" ", "_")
        if not safe_name.endswith(ext):
            safe_name += ext

        out_path = self._local_dir / safe_name
        out_path.write_bytes(data)

        uri = f"file://{out_path.absolute()}"
        logger.info(f"Image saved locally: {out_path}")
        return UploadResult(
            success=True,
            uri=uri,
            provider="local",
            bytes_uploaded=len(data),
        )

    # ------------------------------------------------------------------
    # URI helpers
    # ------------------------------------------------------------------

    def _resolve_uri(self, uri: str) -> str:
        """Resolve ipfs:// and ar:// to https://."""
        if uri.startswith("https://") or uri.startswith("http://"):
            return uri
        if uri.startswith("ipfs://"):
            cid = uri[len("ipfs://"):]
            return f"https://nftstorage.link/ipfs/{cid}"
        if uri.startswith("ar://"):
            tx_id = uri[len("ar://"):]
            return f"https://arweave.net/{tx_id}"
        return uri


# ------------------------------------------------------------------
# Module-level convenience
# ------------------------------------------------------------------

async def reupload_image(source_url: str, filename_hint: str = "") -> UploadResult:
    """One-shot image re-upload."""
    async with ImageReuploader() as uploader:
        return await uploader.reupload(source_url, filename_hint)


async def upload_metadata(metadata: dict, symbol: str = "token") -> UploadResult:
    """One-shot metadata JSON upload."""
    async with ImageReuploader() as uploader:
        return await uploader.upload_metadata_json(metadata, symbol)
