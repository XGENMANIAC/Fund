"""
Token name, symbol, and metadata generator — mimicry-first mode (v2).

DISCLAIMER: Educational system for Solana devnet only.
Generated tokens are purely for simulating the mechanics of token creation.
Do NOT deploy to mainnet. Do NOT promote generated tokens.

v2 changes:
  - Primary strategy: MimicryEngine (high-similarity clone with minor variations)
  - Fallback: suffix/riff strategies (when source has no useful metadata)
  - Passes mimicry fields (variation_rule, name_diff, mimicry_score, etc.) to DB
  - Image re-upload via deploy/arweave.py for IPFS-hosted clone image
  - Always includes educational disclaimer in description
"""

import asyncio
import json
import os
import random
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from analysis.mimicry import MimicryEngine, MimicryResult
from analysis.scorer import ScoredCandidate
from storage.database import insert_generated_token, log_variation
from utils.logger import get_logger
from utils.helpers import clean_name, clean_symbol, load_config, utcnow_iso

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Legacy fallback pools (used when source has no usable metadata)
# ---------------------------------------------------------------------------

SUFFIXES = [
    "2.0", "Reloaded", "Classic", "OG", "Genesis", "Pro",
    "Ultra", "Mega", "Plus", "V2", "Reborn", "Legacy",
    "X", "Max", "Core", "Prime", "Elite",
]

MEME_ADJECTIVES = [
    "Baby", "Super", "Mega", "Ultra", "Degen", "Turbo",
    "Gigachad", "Bonk", "Pepe", "Wojak", "Moon", "Based",
]

MEME_NOUNS = [
    "Doge", "Cat", "Frog", "Ape", "Bull", "Bear", "Shib",
    "Floki", "Inu", "Wif", "Bonk", "Pepe",
]


@dataclass
class GeneratedTokenSpec:
    """Complete specification for a new educational token."""

    # Source (what we're cloning)
    source_address: str = ""
    source_name: str = ""
    source_symbol: str = ""
    source_description: str = ""      # v2
    source_image_uri: str = ""        # v2
    source_metadata_uri: str = ""     # v2

    # New token identity
    new_name: str = ""
    new_symbol: str = ""
    new_description: str = ""
    new_supply: int = 1_000_000_000
    new_decimals: int = 6

    # Allocation
    pool_tokens: int = 0
    retained_tokens: int = 0

    # Mimicry metadata (v2)
    variation_rule: str = ""
    name_diff: str = ""
    symbol_diff: str = ""
    description_diff: str = ""
    mimicry_score: float = 0.0

    # Uploaded metadata
    metadata_uri: str = ""
    image_uri: str = ""
    uploaded_image_uri: str = ""      # v2: re-uploaded clone image
    metadata_json: dict = field(default_factory=dict)

    # DB id after persistence
    db_id: Optional[int] = None
    generated_at: str = field(default_factory=utcnow_iso)


class TokenGenerator:
    """
    Generates an educational token spec inspired by a detected meme coin.

    v2: Primary path uses MimicryEngine to produce a >85% similar clone.
    Falls back to legacy creative strategies when source metadata is sparse.

    The generated name always:
      1. Avoids exact duplication of the source name/symbol.
      2. Includes an educational disclaimer in the description.
      3. Produces valid Metaplex-compatible metadata JSON.
    """

    def __init__(self):
        cfg = load_config()
        gen_cfg = cfg.get("token_generation", {})
        self._name_strategy = gen_cfg.get("name_strategy", "mimicry")
        self._custom_suffixes = gen_cfg.get("suffixes", SUFFIXES)
        self._default_supply = gen_cfg.get("default_supply", 1_000_000_000)
        self._decimals = gen_cfg.get("decimals", 6)
        self._pool_pct = gen_cfg.get("pool_supply_pct", 0.80)
        self._retained_pct = gen_cfg.get("retained_pct", 0.20)

        meta_cfg = cfg.get("metadata", {})
        self._meta_provider = meta_cfg.get("provider", "local_uri")
        self._local_port = meta_cfg.get("local_server_port", 8765)

        self._mimicry = MimicryEngine()

    def generate(self, candidate: ScoredCandidate) -> GeneratedTokenSpec:
        """
        Generate a complete token spec from a scored candidate.
        Returns a GeneratedTokenSpec ready for deployment.
        """
        det = candidate.detection
        source_name = det.token_name or det.token_symbol or "Unknown"
        source_symbol = det.token_symbol or "???"
        source_description = getattr(det, "description", "") or ""
        source_image_uri = getattr(det, "image_uri", "") or ""
        source_metadata_uri = getattr(det, "metadata_uri", "") or ""

        supply = self._default_supply
        pool_tokens = int(supply * self._pool_pct)
        retained_tokens = supply - pool_tokens

        # --- Mimicry-first path ---
        use_mimicry = (
            self._name_strategy == "mimicry"
            and bool(source_name and source_name != "Unknown")
        )

        if use_mimicry:
            mimicry_result = self._mimicry.generate_clone(
                name=source_name,
                symbol=source_symbol,
                description=source_description,
                image_uri=source_image_uri,
            )
            new_name = mimicry_result.new_name
            new_symbol = mimicry_result.new_symbol
            new_description = self._wrap_description(mimicry_result.new_description, source_name)
            variation_rule = mimicry_result.variation_summary
            name_diff = f"{source_name!r} → {new_name!r}"
            symbol_diff = f"{source_symbol!r} → {new_symbol!r}"
            description_diff = (
                f"{mimicry_result.description_variation.rule_name}"
                if mimicry_result.description_variation
                else "copy_verbatim"
            )
            mimicry_score = mimicry_result.overall_similarity
        else:
            # Legacy fallback
            new_name = self._generate_name_legacy(source_name)
            new_symbol = self._generate_symbol_legacy(source_symbol)
            new_description = self._generate_description_legacy(source_name, new_name)
            variation_rule = "legacy_suffix"
            name_diff = f"{source_name!r} → {new_name!r}"
            symbol_diff = f"{source_symbol!r} → {new_symbol!r}"
            description_diff = "educational_override"
            mimicry_score = 0.0
            mimicry_result = None

        # Build metadata JSON
        metadata_json = self._build_metadata_json(
            name=new_name,
            symbol=new_symbol,
            description=new_description,
            source_image_uri=source_image_uri,
        )

        spec = GeneratedTokenSpec(
            source_address=det.token_address,
            source_name=source_name,
            source_symbol=source_symbol,
            source_description=source_description,
            source_image_uri=source_image_uri,
            source_metadata_uri=source_metadata_uri,
            new_name=new_name,
            new_symbol=new_symbol,
            new_description=new_description,
            new_supply=supply,
            new_decimals=self._decimals,
            pool_tokens=pool_tokens,
            retained_tokens=retained_tokens,
            variation_rule=variation_rule,
            name_diff=name_diff,
            symbol_diff=symbol_diff,
            description_diff=description_diff,
            mimicry_score=mimicry_score,
            metadata_json=metadata_json,
            image_uri=source_image_uri,  # Will be replaced after re-upload
        )

        spec.metadata_uri = self._predict_metadata_uri(new_symbol)

        logger.info(
            f"Generated token spec: {new_name} ({new_symbol}) "
            f"[mimicry={mimicry_score:.0f}%] "
            f"supply={supply:,} pool={pool_tokens:,}"
        )
        return spec

    async def generate_async(self, candidate: ScoredCandidate) -> GeneratedTokenSpec:
        """
        Async variant: also attempts to re-upload the source image to IPFS
        so the clone has its own hosted copy.
        """
        spec = self.generate(candidate)

        if spec.source_image_uri:
            try:
                from deploy.arweave import reupload_image
                upload = await reupload_image(
                    spec.source_image_uri,
                    filename_hint=f"{spec.new_symbol.lower()}_image",
                )
                if upload.success:
                    spec.uploaded_image_uri = upload.uri
                    spec.image_uri = upload.uri
                    spec.metadata_json["image"] = upload.uri
                    if spec.metadata_json.get("properties", {}).get("files"):
                        spec.metadata_json["properties"]["files"][0]["uri"] = upload.uri
                    logger.info(f"Image re-uploaded: {upload.uri}")
            except Exception as exc:
                logger.warning(f"Image re-upload skipped: {exc}")

        return spec

    # ------------------------------------------------------------------
    # Legacy name strategies (fallback when source metadata is sparse)
    # ------------------------------------------------------------------

    def _generate_name_legacy(self, source_name: str) -> str:
        if self._name_strategy == "synonym_swap":
            return self._synonym_swap(source_name)
        elif self._name_strategy == "ai_riff":
            return self._riff(source_name)
        return self._append_suffix(source_name)

    def _append_suffix(self, source_name: str) -> str:
        suffix = random.choice(self._custom_suffixes)
        return clean_name(f"{source_name} {suffix}")

    def _synonym_swap(self, source_name: str) -> str:
        words = source_name.split()
        if not words:
            return self._append_suffix(source_name)
        for i, word in enumerate(words):
            for noun in MEME_NOUNS:
                if noun.lower() in word.lower():
                    replacement = random.choice([n for n in MEME_NOUNS if n != noun])
                    words[i] = word.lower().replace(noun.lower(), replacement)
                    return clean_name(" ".join(words))
        adj = random.choice(MEME_ADJECTIVES)
        return clean_name(f"{adj} {source_name}")

    def _riff(self, source_name: str) -> str:
        adj = random.choice(MEME_ADJECTIVES)
        noun = random.choice(MEME_NOUNS)
        first_word = source_name.split()[0] if source_name.split() else ""
        is_meme_noun = any(n.lower() in first_word.lower() for n in MEME_NOUNS)
        if is_meme_noun:
            return clean_name(f"{adj} {first_word} {random.choice(SUFFIXES)}")
        return clean_name(f"{first_word} {noun}")

    def _generate_symbol_legacy(self, source_symbol: str) -> str:
        candidate = f"{source_symbol[:6]}{random.randint(2, 99)}"
        if len(candidate) <= 10:
            return clean_symbol(candidate)
        if len(source_symbol) >= 2:
            return clean_symbol(source_symbol[:4] + "X")
        return "".join(random.choices(string.ascii_uppercase, k=5))

    def _generate_description_legacy(self, source_name: str, new_name: str) -> str:
        return (
            f"[EDUCATIONAL TEST TOKEN — DEVNET ONLY] "
            f"'{new_name}' is an automated educational clone of '{source_name}', "
            f"created to study Solana token deployment mechanics. "
            f"This token has NO monetary value and should NOT be traded. "
            f"Generated by the Solana Meme Coin Educational Research System."
        )

    def _wrap_description(self, mimicry_description: str, source_name: str) -> str:
        """
        For mimicry clones, the description comes from MimicryEngine (a variation
        of the original). We always append a visible educational disclaimer so
        the token is never mistaken for a real asset.
        """
        disclaimer = (
            " [EDUCATIONAL DEVNET TOKEN — No monetary value. "
            f"Research clone of '{source_name}'. DO NOT TRADE.]"
        )
        return mimicry_description + disclaimer

    # ------------------------------------------------------------------
    # Metadata JSON
    # ------------------------------------------------------------------

    def _build_metadata_json(
        self,
        name: str,
        symbol: str,
        description: str,
        source_image_uri: str = "",
    ) -> dict:
        """
        Build a Metaplex-compatible metadata JSON.
        Uses source image URI initially (will be replaced after re-upload).
        """
        image = source_image_uri or self._placeholder_image_url(symbol)
        return {
            "name": name,
            "symbol": symbol,
            "description": description,
            "image": image,
            "external_url": "",
            "attributes": [
                {"trait_type": "Type", "value": "Educational"},
                {"trait_type": "Network", "value": "Devnet"},
                {"trait_type": "Purpose", "value": "Research & Testing"},
            ],
            "properties": {
                "files": [{"uri": image, "type": "image/png"}],
                "category": "image",
            },
        }

    def _placeholder_image_url(self, symbol: str) -> str:
        initials = symbol[:2].upper()
        colors = ["FF6B6B", "4ECDC4", "45B7D1", "96CEB4", "FFEAA7", "DDA0DD"]
        bg = random.choice(colors)
        return (
            f"https://ui-avatars.com/api/?name={initials}"
            f"&background={bg}&color=fff&size=256&bold=true&format=png"
        )

    def _predict_metadata_uri(self, symbol: str) -> str:
        if self._meta_provider == "local_uri":
            return f"http://localhost:{self._local_port}/metadata/{symbol.lower()}.json"
        return ""

    # ------------------------------------------------------------------
    # Metadata hosting helpers
    # ------------------------------------------------------------------

    def save_metadata_locally(self, spec: GeneratedTokenSpec) -> str:
        data_dir = Path("data/metadata")
        data_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{spec.new_symbol.lower()}.json"
        filepath = data_dir / filename
        with open(filepath, "w") as f:
            json.dump(spec.metadata_json, f, indent=2)
        logger.info(f"Metadata saved locally: {filepath}")
        return str(filepath)

    async def upload_to_nft_storage(self, spec: GeneratedTokenSpec) -> str:
        """Upload metadata JSON to NFT.storage (free IPFS). Returns IPFS URI."""
        import aiohttp as _aiohttp

        api_key = os.getenv("NFT_STORAGE_API_KEY")
        if not api_key:
            logger.warning("NFT_STORAGE_API_KEY not set — using local URI")
            self.save_metadata_locally(spec)
            return spec.metadata_uri

        try:
            async with _aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.nft.storage/upload",
                    data=json.dumps(spec.metadata_json).encode(),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                ) as resp:
                    result = await resp.json()
                    cid = result["value"]["cid"]
                    uri = f"https://nftstorage.link/ipfs/{cid}"
                    logger.info(f"Metadata uploaded to NFT.storage: {uri}")
                    return uri
        except Exception as exc:
            logger.error(f"NFT.storage upload failed: {exc}")
            self.save_metadata_locally(spec)
            return spec.metadata_uri

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def persist(self, spec: GeneratedTokenSpec, candidate_db_id: int) -> int:
        """Save the generated token spec to the database. Returns db_id."""
        db_id = insert_generated_token(
            candidate_id=candidate_db_id,
            source_address=spec.source_address,
            new_name=spec.new_name,
            new_symbol=spec.new_symbol,
            new_supply=spec.new_supply,
            source_name=spec.source_name,
            source_symbol=spec.source_symbol,
            new_description=spec.new_description,
            new_decimals=spec.new_decimals,
            metadata_uri=spec.metadata_uri,
            image_uri=spec.image_uri or spec.metadata_json.get("image", ""),
            # v2 mimicry fields
            source_description=spec.source_description,
            source_image_uri=spec.source_image_uri,
            source_metadata_uri=spec.source_metadata_uri,
            variation_rule=spec.variation_rule,
            name_diff=spec.name_diff,
            symbol_diff=spec.symbol_diff,
            description_diff=spec.description_diff,
            mimicry_score=spec.mimicry_score,
            uploaded_image_uri=spec.uploaded_image_uri,
            metadata_provider=self._meta_provider,
        )
        spec.db_id = db_id

        # Log variation rules to audit table
        if db_id and spec.mimicry_score > 0:
            try:
                log_variation(
                    generated_id=db_id,
                    field="name",
                    rule_name=spec.variation_rule,
                    original_value=spec.source_name,
                    modified_value=spec.new_name,
                    similarity_pct=spec.mimicry_score,
                )
            except Exception as exc:
                logger.debug(f"variation log error: {exc}")

        return db_id
