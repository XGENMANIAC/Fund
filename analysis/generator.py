"""
Token name, symbol, and metadata generator for educational "clone" tokens.

DISCLAIMER: Educational system for Solana devnet only.
Generated tokens are purely for simulating the mechanics of token creation.
Do NOT deploy to mainnet. Do NOT promote generated tokens.

This module takes a source token (the one we detected and want to learn from)
and generates a plausible-looking "inspired by" token with:
  - A new name (suffix/riff strategy)
  - A new unique symbol
  - A description explaining it's an educational test clone
  - A metadata JSON conforming to the Metaplex NFT standard
  - A simple placeholder image URI

In a real (non-educational) system, this is where the "copy-cat" creativity
lives. In ours, we always include an educational disclaimer in the description.
"""

import json
import os
import random
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from analysis.scorer import ScoredCandidate
from storage.database import insert_generated_token
from utils.logger import get_logger
from utils.helpers import clean_name, clean_symbol, load_config, utcnow_iso

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Name/symbol generation strategies
# ---------------------------------------------------------------------------

SUFFIXES = [
    "2.0", "Reloaded", "Classic", "OG", "Genesis", "Pro",
    "Ultra", "Mega", "Plus", "V2", "Reborn", "Legacy",
    "X", "Max", "Core", "Prime", "Elite",
]

# Meme-coin-style adjectives for generating riff names
MEME_ADJECTIVES = [
    "Baby", "Super", "Mega", "Ultra", "Degen", "Turbo",
    "Gigachad", "Bonk", "Pepe", "Wojak", "Moon", "Based",
]

# Common meme coin animal references
MEME_NOUNS = [
    "Doge", "Cat", "Frog", "Ape", "Bull", "Bear", "Shib",
    "Floki", "Inu", "Wif", "Bonk", "Pepe",
]


@dataclass
class GeneratedTokenSpec:
    """Complete specification for a new educational token."""

    # Source (what we're inspired by)
    source_address: str = ""
    source_name: str = ""
    source_symbol: str = ""

    # New token identity
    new_name: str = ""
    new_symbol: str = ""
    new_description: str = ""
    new_supply: int = 1_000_000_000
    new_decimals: int = 6

    # Allocation
    pool_tokens: int = 0     # Goes to Raydium pool
    retained_tokens: int = 0  # Kept by deployer (simulation)

    # Metadata
    metadata_uri: str = ""
    image_uri: str = ""
    metadata_json: dict = field(default_factory=dict)

    # DB id after persistence
    db_id: Optional[int] = None

    generated_at: str = field(default_factory=utcnow_iso)


class TokenGenerator:
    """
    Generates a new educational token spec inspired by a detected meme coin.

    The generated name always:
      1. Avoids exact duplication of the source name/symbol.
      2. Includes an educational disclaimer in the description.
      3. Produces valid Metaplex-compatible metadata JSON.
    """

    def __init__(self):
        cfg = load_config()
        gen_cfg = cfg.get("token_generation", {})
        self._name_strategy = gen_cfg.get("name_strategy", "append_suffix")
        self._custom_suffixes = gen_cfg.get("suffixes", SUFFIXES)
        self._default_supply = gen_cfg.get("default_supply", 1_000_000_000)
        self._decimals = gen_cfg.get("decimals", 6)
        self._pool_pct = gen_cfg.get("pool_supply_pct", 0.80)
        self._retained_pct = gen_cfg.get("retained_pct", 0.20)

        meta_cfg = cfg.get("metadata", {})
        self._meta_provider = meta_cfg.get("provider", "local_uri")
        self._local_port = meta_cfg.get("local_server_port", 8765)

    def generate(self, candidate: ScoredCandidate) -> GeneratedTokenSpec:
        """
        Generate a complete token spec from a scored candidate.
        Returns a GeneratedTokenSpec ready for deployment.
        """
        det = candidate.detection
        source_name = det.token_name or det.token_symbol or "Unknown"
        source_symbol = det.token_symbol or "???"

        # Generate new name and symbol
        new_name = self._generate_name(source_name)
        new_symbol = self._generate_symbol(source_symbol)

        # Generate description
        description = self._generate_description(source_name, new_name)

        # Supply allocation
        supply = self._default_supply
        pool_tokens = int(supply * self._pool_pct)
        retained_tokens = supply - pool_tokens

        # Build metadata
        metadata_json = self._build_metadata_json(new_name, new_symbol, description)

        spec = GeneratedTokenSpec(
            source_address=det.token_address,
            source_name=source_name,
            source_symbol=source_symbol,
            new_name=new_name,
            new_symbol=new_symbol,
            new_description=description,
            new_supply=supply,
            new_decimals=self._decimals,
            pool_tokens=pool_tokens,
            retained_tokens=retained_tokens,
            metadata_json=metadata_json,
        )

        # Assign metadata URI (set after upload in deploy step)
        spec.metadata_uri = self._predict_metadata_uri(new_symbol)

        logger.info(
            f"Generated token spec: {new_name} ({new_symbol}) "
            f"supply={supply:,} pool={pool_tokens:,}"
        )
        return spec

    # ------------------------------------------------------------------
    # Name strategies
    # ------------------------------------------------------------------

    def _generate_name(self, source_name: str) -> str:
        """Generate a new token name based on the configured strategy."""
        if self._name_strategy == "append_suffix":
            return self._append_suffix(source_name)
        elif self._name_strategy == "synonym_swap":
            return self._synonym_swap(source_name)
        elif self._name_strategy == "ai_riff":
            return self._riff(source_name)
        else:
            return self._append_suffix(source_name)

    def _append_suffix(self, source_name: str) -> str:
        """Append a random suffix to the source name."""
        suffix = random.choice(self._custom_suffixes)
        candidate = f"{source_name} {suffix}"
        return clean_name(candidate)

    def _synonym_swap(self, source_name: str) -> str:
        """
        Swap a word in the name with a meme synonym.
        Simple word replacement — not NLP-based.
        """
        words = source_name.split()
        if not words:
            return self._append_suffix(source_name)

        # Replace the first recognizable meme word with an alternative
        for i, word in enumerate(words):
            for noun in MEME_NOUNS:
                if noun.lower() in word.lower():
                    replacement = random.choice([n for n in MEME_NOUNS if n != noun])
                    words[i] = word.lower().replace(noun.lower(), replacement)
                    return clean_name(" ".join(words))

        # Fallback: prepend an adjective
        adj = random.choice(MEME_ADJECTIVES)
        return clean_name(f"{adj} {source_name}")

    def _riff(self, source_name: str) -> str:
        """
        Generate a creative meme-style riff on the name.
        Combines random adjective + source word fragments.
        """
        adj = random.choice(MEME_ADJECTIVES)
        noun = random.choice(MEME_NOUNS)
        # Use first word of source name if it's not a known meme noun
        first_word = source_name.split()[0] if source_name.split() else ""
        is_meme_noun = any(
            n.lower() in first_word.lower() for n in MEME_NOUNS
        )
        if is_meme_noun:
            return clean_name(f"{adj} {first_word} {random.choice(SUFFIXES)}")
        return clean_name(f"{first_word} {noun}")

    # ------------------------------------------------------------------
    # Symbol generation
    # ------------------------------------------------------------------

    def _generate_symbol(self, source_symbol: str) -> str:
        """
        Generate a new unique symbol based on the source.
        Strategies: append digit, use source prefix, random 4-letter combo.
        """
        # Strategy 1: source + random number
        candidate = f"{source_symbol[:6]}{random.randint(2, 99)}"
        if len(candidate) <= 10:
            return clean_symbol(candidate)

        # Strategy 2: first 4 chars of source + 'X'
        if len(source_symbol) >= 2:
            return clean_symbol(source_symbol[:4] + "X")

        # Strategy 3: random 5-letter uppercase
        return "".join(random.choices(string.ascii_uppercase, k=5))

    # ------------------------------------------------------------------
    # Description
    # ------------------------------------------------------------------

    def _generate_description(self, source_name: str, new_name: str) -> str:
        """
        Always includes an educational disclaimer.
        In a real copy-cat system, this would be a fake hype description.
        Here we're transparent about the educational purpose.
        """
        return (
            f"[EDUCATIONAL TEST TOKEN — DEVNET ONLY] "
            f"'{new_name}' is an automated educational clone of '{source_name}', "
            f"created to study Solana token deployment mechanics. "
            f"This token has NO monetary value and should NOT be traded. "
            f"Generated by the Solana Meme Coin Educational Research System."
        )

    # ------------------------------------------------------------------
    # Metadata JSON (Metaplex Token Metadata standard)
    # ------------------------------------------------------------------

    def _build_metadata_json(
        self, name: str, symbol: str, description: str
    ) -> dict:
        """
        Build a Metaplex-compatible metadata JSON.
        See: https://docs.metaplex.com/programs/token-metadata/token-standard
        """
        return {
            "name": name,
            "symbol": symbol,
            "description": description,
            "image": self._placeholder_image_url(symbol),
            "external_url": "",
            "attributes": [
                {"trait_type": "Type", "value": "Educational"},
                {"trait_type": "Network", "value": "Devnet"},
                {"trait_type": "Purpose", "value": "Research & Testing"},
            ],
            "properties": {
                "files": [
                    {
                        "uri": self._placeholder_image_url(symbol),
                        "type": "image/png",
                    }
                ],
                "category": "image",
            },
        }

    def _placeholder_image_url(self, symbol: str) -> str:
        """
        Generate a placeholder image URL using a free service.
        ui-avatars.com generates simple letter-based avatars (no signup).
        """
        initials = symbol[:2].upper()
        # Random bright background color
        colors = ["FF6B6B", "4ECDC4", "45B7D1", "96CEB4", "FFEAA7", "DDA0DD"]
        bg = random.choice(colors)
        return (
            f"https://ui-avatars.com/api/?name={initials}"
            f"&background={bg}&color=fff&size=256&bold=true&format=png"
        )

    def _predict_metadata_uri(self, symbol: str) -> str:
        """Return the expected URI where metadata will be hosted after upload."""
        if self._meta_provider == "local_uri":
            return f"http://localhost:{self._local_port}/metadata/{symbol.lower()}.json"
        return ""

    # ------------------------------------------------------------------
    # Metadata hosting helpers
    # ------------------------------------------------------------------

    def save_metadata_locally(self, spec: GeneratedTokenSpec) -> str:
        """
        Save the metadata JSON to the local data/ directory.
        Returns the local file path.
        Used when provider = "local_uri" — only accessible from localhost.
        """
        data_dir = Path("data/metadata")
        data_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{spec.new_symbol.lower()}.json"
        filepath = data_dir / filename

        with open(filepath, "w") as f:
            json.dump(spec.metadata_json, f, indent=2)

        logger.info(f"Metadata saved locally: {filepath}")
        return str(filepath)

    async def upload_to_nft_storage(self, spec: GeneratedTokenSpec) -> str:
        """
        Upload metadata to NFT.storage (free, decentralized IPFS pinning).
        Requires NFT_STORAGE_API_KEY in environment.
        Returns the IPFS URI.

        NFT.storage free tier: unlimited storage for NFT data.
        Sign up at: https://nft.storage
        """
        import aiohttp

        api_key = os.getenv("NFT_STORAGE_API_KEY")
        if not api_key:
            logger.warning("NFT_STORAGE_API_KEY not set — using local URI")
            self.save_metadata_locally(spec)
            return spec.metadata_uri

        try:
            async with aiohttp.ClientSession() as session:
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
            image_uri=spec.metadata_json.get("image", ""),
        )
        spec.db_id = db_id
        return db_id
