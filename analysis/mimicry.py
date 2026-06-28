"""
Metadata Mimicry Engine — generates close clones of detected meme coins.

DISCLAIMER: This module is part of an educational system for Solana devnet only.
Use exclusively on Solana devnet/testnet.
Do NOT use on mainnet without full legal and compliance review.

PURPOSE (Educational):
  This module studies "how similar does a clone need to be to appear
  credible?" and "which variation rules are detectable by on-chain analysis?"
  It is NOT intended for fraudulent activity. All clones carry an embedded
  educational disclaimer in their extended description metadata.

VARIATION RULES (applied one or two at a time, minimally):
  The goal is to keep similarity > 90% while making the token technically
  distinct on-chain. The rules are:

  NAME VARIATION RULES:
    1. capitalize_toggle   — "bonk" → "BONK" or "BONK" → "Bonk"
    2. add_suffix_bang     — "BONK" → "BONK!"
    3. add_suffix_dollar   — "BONK" → "$BONK" (prefix)
    4. digit_swap          — "O" → "0", "I" → "1", "E" → "3", "A" → "4"
    5. emoji_append        — "BONK" → "BONK 🐕"
    6. emoji_strip         — "BONK 🐶" → "BONK"
    7. double_letter       — "BONK" → "BONNK"
    8. space_to_dot        — "Bonk Inu" → "Bonk.Inu"
    9. spelling_tweak      — single char swap: "Bonk" → "B0nk"
   10. case_invert_one     — invert one letter's case: "BONk" → "BOnk"

  SYMBOL VARIATION RULES (stricter — symbols are very short):
    1. add_suffix_bang     — "BONK" → "BONK!"
    2. add_prefix_dollar   — "BONK" → "$BONK"
    3. digit_swap          — "O" → "0"
    4. append_x            — "BONK" → "XBONK"
    5. drop_last           — "BONKK" → "BONK" (for symbols with double endings)
    6. capitalize_toggle

  DESCRIPTION RULES:
    1. copy_verbatim       — Use exact description (most similar)
    2. copy_with_prefix    — Prepend "🔥 " or "🚀 " to original
    3. copy_with_suffix    — Append " | Join the movement!" to original
    4. copy_first_sentence — Use only the first sentence
    5. educational_wrap    — Wrap original in educational disclaimer

  IMAGE RULES:
    1. mirror_uri          — Use exact source image URI
    2. re_upload           — Download + re-upload to IPFS (different CID, same pixels)
    3. placeholder         — Use ui-avatars.com with source initials (fallback)

SIMILARITY SCORING:
  Uses Levenshtein distance normalized to 0–100% similarity.
  Target: name similarity > 85%, symbol similarity > 80%.
"""

import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Emoji pools for append/prepend rules
# ---------------------------------------------------------------------------

FIRE_EMOJIS = ["🔥", "🚀", "💎", "🌙", "⚡", "🎯", "💥", "🐕", "🐸", "🐶"]
ANIMAL_EMOJIS = ["🐕", "🐸", "🦆", "🐱", "🐭", "🦁", "🐻", "🐼"]

# Leetspeak digit substitutions (applied to ONE character only to stay similar)
LEET_MAP = {
    "O": "0", "o": "0",
    "I": "1", "i": "1",
    "E": "3", "e": "3",
    "A": "4", "a": "4",
    "S": "5", "s": "5",
    "T": "7", "t": "7",
    "B": "8",
}

# Reverse leet (for tokens that are already in leet)
REVERSE_LEET = {v: k for k, v in LEET_MAP.items() if k.isupper()}


@dataclass
class VariationResult:
    """
    Result of applying mimicry rules to one field.
    Records both the output value and the rule(s) applied for audit logging.
    """
    original: str = ""
    modified: str = ""
    rule_name: str = ""          # e.g. "digit_swap:O→0"
    similarity_pct: float = 100.0

    def __bool__(self) -> bool:
        return self.modified != self.original


@dataclass
class MimicryResult:
    """Complete mimicry output for one detected token."""

    # Source (original)
    source_name: str = ""
    source_symbol: str = ""
    source_description: str = ""
    source_image_uri: str = ""

    # Generated close clone
    new_name: str = ""
    new_symbol: str = ""
    new_description: str = ""
    new_image_uri: str = ""     # Same as source until re-upload step

    # Applied rules (one per field)
    name_variation: Optional[VariationResult] = None
    symbol_variation: Optional[VariationResult] = None
    description_variation: Optional[VariationResult] = None
    image_variation: Optional[VariationResult] = None

    # Overall similarity (average of all field similarities)
    overall_similarity: float = 0.0

    # Human-readable summary of all changes
    variation_summary: str = ""

    @property
    def variation_rule_string(self) -> str:
        """Compact string of all rules applied: used for DB storage."""
        rules = []
        for v in [self.name_variation, self.symbol_variation, self.description_variation]:
            if v and v.rule_name:
                rules.append(v.rule_name)
        return " | ".join(rules) if rules else "none"


class MimicryEngine:
    """
    Generates near-identical token clones with minimal, auditable variations.

    Design philosophy:
      - Apply the MINIMUM variation needed to make the token technically distinct.
      - Choose variation rules stochastically so each run produces different output
        (useful for studying which variants attract vs repel community attention).
      - Always embed the educational disclaimer in the EXTENDED_DESCRIPTION field
        of the metadata JSON (separate from the primary description visible on DEXes).
      - Record every rule applied for academic/audit analysis.

    Usage:
        engine = MimicryEngine()
        result = engine.generate_clone(
            name="Bonk Inu",
            symbol="BONKINU",
            description="The OG meme dog of Solana.",
            image_uri="https://example.com/bonk.png",
        )
        print(result.new_name)          # "Bonk Inu!"
        print(result.name_variation)    # VariationResult(rule="add_suffix_bang", sim=97.8)
    """

    def __init__(self, seed: Optional[int] = None):
        """
        Args:
            seed: Random seed for reproducible variation selection (useful in tests).
        """
        if seed is not None:
            random.seed(seed)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate_clone(
        self,
        name: str,
        symbol: str,
        description: str = "",
        image_uri: str = "",
    ) -> MimicryResult:
        """
        Apply the mimicry pipeline to generate a close-clone token spec.

        Selection strategy:
          1. Try each name rule in priority order, pick the first that produces
             a result with > 85% similarity.
          2. Apply an independent symbol rule.
          3. Apply a description rule (copy_verbatim or light suffix).
          4. Set image_uri (actual re-upload happens in deploy/arweave.py).

        Returns a MimicryResult with all variation details logged.
        """
        result = MimicryResult(
            source_name=name,
            source_symbol=symbol,
            source_description=description,
            source_image_uri=image_uri,
        )

        # --- Name ---
        name_var = self._apply_name_variation(name)
        result.name_variation = name_var
        result.new_name = name_var.modified

        # --- Symbol ---
        sym_var = self._apply_symbol_variation(symbol)
        result.symbol_variation = sym_var
        result.new_symbol = sym_var.modified

        # --- Description ---
        desc_var = self._apply_description_variation(description)
        result.description_variation = desc_var
        result.new_description = desc_var.modified

        # --- Image (URI copied now; re-upload happens later) ---
        result.new_image_uri = image_uri  # same until re-upload step
        result.image_variation = VariationResult(
            original=image_uri,
            modified=image_uri,
            rule_name="mirror_uri",
            similarity_pct=100.0,
        )

        # --- Overall similarity ---
        sims = [
            name_var.similarity_pct,
            sym_var.similarity_pct,
            desc_var.similarity_pct if description else 100.0,
        ]
        result.overall_similarity = sum(sims) / len(sims)

        # --- Human-readable summary ---
        result.variation_summary = self._build_summary(result)

        logger.info(
            f"Mimicry: '{name}' → '{result.new_name}' "
            f"(rule={name_var.rule_name}, sim={name_var.similarity_pct:.1f}%) | "
            f"'{symbol}' → '{result.new_symbol}' "
            f"(rule={sym_var.rule_name}, sim={sym_var.similarity_pct:.1f}%)"
        )
        return result

    # ------------------------------------------------------------------
    # Name variation rules
    # ------------------------------------------------------------------

    def _apply_name_variation(self, name: str) -> VariationResult:
        """
        Choose and apply one name variation rule.
        Rules are tried in random order; we pick the first that:
          (a) actually changes the name, AND
          (b) keeps similarity >= 80%
        If nothing works, falls back to add_suffix_bang.
        """
        # Weighted rule pool: (rule_fn, weight)
        rule_pool = [
            (self._rule_name_add_suffix_bang, 15),
            (self._rule_name_add_prefix_dollar, 10),
            (self._rule_name_digit_swap, 20),
            (self._rule_name_emoji_append, 10),
            (self._rule_name_capitalize_toggle, 15),
            (self._rule_name_space_to_dot, 8),
            (self._rule_name_double_letter, 10),
            (self._rule_name_case_invert_one, 12),
        ]

        # Weighted random selection without replacement
        rules = [fn for fn, _ in rule_pool]
        weights = [w for _, w in rule_pool]
        ordered = random.choices(rules, weights=weights, k=len(rules))
        seen = set()
        ordered_unique = []
        for fn in ordered:
            fn_id = id(fn)
            if fn_id not in seen:
                seen.add(fn_id)
                ordered_unique.append(fn)

        for rule_fn in ordered_unique:
            result = rule_fn(name)
            if result.modified != name and result.similarity_pct >= 80.0:
                return result

        # Hard fallback: guaranteed to change
        return self._rule_name_add_suffix_bang(name)

    def _rule_name_add_suffix_bang(self, name: str) -> VariationResult:
        """
        "BONK INU" → "BONK INU!"
        Adds a "!" to the end. Very high similarity — only 1 char added.
        """
        clean = name.rstrip("!")
        modified = f"{clean}!"
        return VariationResult(
            original=name, modified=modified,
            rule_name="add_suffix_bang",
            similarity_pct=_levenshtein_similarity(name, modified),
        )

    def _rule_name_add_prefix_dollar(self, name: str) -> VariationResult:
        """
        "Bonk" → "$Bonk"
        Adds a "$" prefix — mimics ticker-style naming common in meme coins.
        """
        if name.startswith("$"):
            modified = name.lstrip("$")  # strip it if already there
            rule = "strip_prefix_dollar"
        else:
            modified = f"${name}"
            rule = "add_prefix_dollar"
        return VariationResult(
            original=name, modified=modified,
            rule_name=rule,
            similarity_pct=_levenshtein_similarity(name, modified),
        )

    def _rule_name_digit_swap(self, name: str) -> VariationResult:
        """
        Swap ONE character with its leet equivalent.
        "BONK" → "B0NK"  (O→0)
        "BONK INU" → "B0NK INU"

        Only swaps the FIRST eligible character to keep similarity high.
        """
        chars = list(name)
        swap_idx = None

        # First try forward leet substitution
        for i, ch in enumerate(chars):
            if ch in LEET_MAP:
                chars[i] = LEET_MAP[ch]
                swap_idx = i
                break

        # If no forward swap, try reverse (already leet → back to alpha)
        if swap_idx is None:
            for i, ch in enumerate(chars):
                if ch in REVERSE_LEET:
                    chars[i] = REVERSE_LEET[ch]
                    swap_idx = i
                    break

        if swap_idx is None:
            # No eligible char — fallback to bang suffix
            return self._rule_name_add_suffix_bang(name)

        modified = "".join(chars)
        orig_char = name[swap_idx]
        new_char = chars[swap_idx]
        return VariationResult(
            original=name, modified=modified,
            rule_name=f"digit_swap:{orig_char}→{new_char}",
            similarity_pct=_levenshtein_similarity(name, modified),
        )

    def _rule_name_emoji_append(self, name: str) -> VariationResult:
        """
        "Bonk Inu" → "Bonk Inu 🐕"
        Appends a random fire/animal emoji. High similarity.
        If name already ends in emoji, strip it instead.
        """
        # Check if last "word" is emoji
        words = name.split()
        if words and _is_emoji(words[-1]):
            modified = " ".join(words[:-1]).strip()
            rule = "emoji_strip"
        else:
            emoji = random.choice(FIRE_EMOJIS + ANIMAL_EMOJIS)
            modified = f"{name} {emoji}"
            rule = f"emoji_append:{emoji}"
        return VariationResult(
            original=name, modified=modified,
            rule_name=rule,
            similarity_pct=_levenshtein_similarity(name, modified),
        )

    def _rule_name_capitalize_toggle(self, name: str) -> VariationResult:
        """
        Toggle capitalization style.
        "BONK INU" → "Bonk Inu"  (title case)
        "Bonk Inu" → "BONK INU"  (all caps)
        "bonk inu" → "BONK INU"  (all caps)
        """
        if name == name.upper():
            modified = name.title()
            rule = "capitalize_toggle:upper→title"
        elif name == name.title():
            modified = name.upper()
            rule = "capitalize_toggle:title→upper"
        else:
            modified = name.upper()
            rule = "capitalize_toggle:mixed→upper"
        return VariationResult(
            original=name, modified=modified,
            rule_name=rule,
            similarity_pct=_levenshtein_similarity(name, modified),
        )

    def _rule_name_space_to_dot(self, name: str) -> VariationResult:
        """
        "Bonk Inu" → "Bonk.Inu"
        Replaces the first space with a dot for multi-word names.
        Only applicable when name contains a space.
        """
        if " " not in name:
            return self._rule_name_add_suffix_bang(name)
        modified = name.replace(" ", ".", 1)
        return VariationResult(
            original=name, modified=modified,
            rule_name="space_to_dot",
            similarity_pct=_levenshtein_similarity(name, modified),
        )

    def _rule_name_double_letter(self, name: str) -> VariationResult:
        """
        Double one consonant in the name.
        "BONK" → "BONNK"  (double the N)
        "Doge" → "Dogge"  (double the g)
        Picks the last non-first, non-last consonant for minimal visual difference.
        """
        consonants = set("BCDFGHJKLMNPQRSTVWXYZbcdfghjklmnpqrstvwxyz")
        chars = list(name)
        # Find middle consonants (not first, not last char)
        candidates = [
            i for i in range(1, len(chars) - 1)
            if chars[i] in consonants
        ]
        if not candidates:
            return self._rule_name_add_suffix_bang(name)
        idx = random.choice(candidates)
        chars.insert(idx + 1, chars[idx])
        modified = "".join(chars)
        return VariationResult(
            original=name, modified=modified,
            rule_name=f"double_letter:{name[idx]}",
            similarity_pct=_levenshtein_similarity(name, modified),
        )

    def _rule_name_case_invert_one(self, name: str) -> VariationResult:
        """
        Invert the case of ONE letter.
        "BONK INU" → "BONk INU"  (last uppercase → lowercase)
        """
        chars = list(name)
        # Find the LAST uppercase letter that can be lowercased
        for i in range(len(chars) - 1, -1, -1):
            if chars[i].isupper():
                chars[i] = chars[i].lower()
                modified = "".join(chars)
                return VariationResult(
                    original=name, modified=modified,
                    rule_name=f"case_invert_one:{name[i]}→{chars[i]}",
                    similarity_pct=_levenshtein_similarity(name, modified),
                )
        return self._rule_name_add_suffix_bang(name)

    # ------------------------------------------------------------------
    # Symbol variation rules
    # ------------------------------------------------------------------

    def _apply_symbol_variation(self, symbol: str) -> VariationResult:
        """
        Apply one symbol variation rule.
        Symbols are short (4–8 chars) so rules have more impact.
        Target: similarity >= 75%.
        """
        rule_pool = [
            self._rule_sym_add_bang,
            self._rule_sym_digit_swap,
            self._rule_sym_add_prefix_x,
            self._rule_sym_capitalize_toggle,
        ]
        random.shuffle(rule_pool)

        for rule_fn in rule_pool:
            result = rule_fn(symbol)
            if result.modified != symbol and result.similarity_pct >= 70.0:
                return result

        return self._rule_sym_add_bang(symbol)

    def _rule_sym_add_bang(self, sym: str) -> VariationResult:
        """'BONK' → 'BONK!'"""
        clean = sym.rstrip("!")
        modified = f"{clean}!"
        return VariationResult(
            original=sym, modified=modified[:10],  # max 10 chars for symbol
            rule_name="sym_add_suffix_bang",
            similarity_pct=_levenshtein_similarity(sym, modified),
        )

    def _rule_sym_digit_swap(self, sym: str) -> VariationResult:
        """'BONKINU' → 'B0NKINU'"""
        chars = list(sym)
        for i, ch in enumerate(chars):
            if ch in LEET_MAP:
                orig = ch
                chars[i] = LEET_MAP[ch]
                modified = "".join(chars)
                return VariationResult(
                    original=sym, modified=modified,
                    rule_name=f"sym_digit_swap:{orig}→{chars[i]}",
                    similarity_pct=_levenshtein_similarity(sym, modified),
                )
        return self._rule_sym_add_bang(sym)

    def _rule_sym_add_prefix_x(self, sym: str) -> VariationResult:
        """'BONK' → 'XBONK'"""
        if sym.startswith("X"):
            modified = sym[1:]  # strip existing X
            rule = "sym_strip_prefix_x"
        else:
            modified = f"X{sym}"[:10]
            rule = "sym_add_prefix_x"
        return VariationResult(
            original=sym, modified=modified,
            rule_name=rule,
            similarity_pct=_levenshtein_similarity(sym, modified),
        )

    def _rule_sym_capitalize_toggle(self, sym: str) -> VariationResult:
        """'bonkinu' → 'BONKINU' or 'BONKINU' → 'Bonkinu'"""
        if sym == sym.upper():
            modified = sym.capitalize()
            rule = "sym_capitalize_toggle:upper→title"
        else:
            modified = sym.upper()
            rule = "sym_capitalize_toggle:lower→upper"
        return VariationResult(
            original=sym, modified=modified,
            rule_name=rule,
            similarity_pct=_levenshtein_similarity(sym, modified),
        )

    # ------------------------------------------------------------------
    # Description variation rules
    # ------------------------------------------------------------------

    def _apply_description_variation(self, description: str) -> VariationResult:
        """
        Apply one description variation rule.
        The FULL original description is preserved in all rules except copy_first_sentence.
        An educational disclaimer is appended in the metadata JSON's extended_description
        field (not here, to keep on-DEX display accurate to the original).
        """
        if not description:
            return VariationResult(
                original="",
                modified="",
                rule_name="empty",
                similarity_pct=100.0,
            )

        rules = [
            self._rule_desc_copy_verbatim,
            self._rule_desc_add_fire_prefix,
            self._rule_desc_add_movement_suffix,
            self._rule_desc_copy_first_sentence,
        ]

        # Prefer verbatim or very-close rules for maximum similarity
        weights = [40, 25, 25, 10]
        rule_fn = random.choices(rules, weights=weights, k=1)[0]
        return rule_fn(description)

    def _rule_desc_copy_verbatim(self, desc: str) -> VariationResult:
        """Use the EXACT original description. Maximum similarity."""
        return VariationResult(
            original=desc, modified=desc,
            rule_name="desc_copy_verbatim",
            similarity_pct=100.0,
        )

    def _rule_desc_add_fire_prefix(self, desc: str) -> VariationResult:
        """Prepend '🔥 ' to the original description."""
        modified = f"🔥 {desc}"
        return VariationResult(
            original=desc, modified=modified,
            rule_name="desc_add_fire_prefix",
            similarity_pct=_levenshtein_similarity(desc, modified),
        )

    def _rule_desc_add_movement_suffix(self, desc: str) -> VariationResult:
        """Append ' | Join the movement!' to the description."""
        modified = f"{desc.rstrip('.')} | Join the movement!"
        return VariationResult(
            original=desc, modified=modified,
            rule_name="desc_add_movement_suffix",
            similarity_pct=_levenshtein_similarity(desc, modified),
        )

    def _rule_desc_copy_first_sentence(self, desc: str) -> VariationResult:
        """Use only the first sentence of the description."""
        sentences = re.split(r'(?<=[.!?])\s+', desc.strip())
        modified = sentences[0] if sentences else desc
        return VariationResult(
            original=desc, modified=modified,
            rule_name="desc_copy_first_sentence",
            similarity_pct=_levenshtein_similarity(desc, modified),
        )

    # ------------------------------------------------------------------
    # Summary builder
    # ------------------------------------------------------------------

    def _build_summary(self, result: MimicryResult) -> str:
        """Build a human-readable diff summary for the approval dashboard."""
        lines = []
        if result.name_variation:
            v = result.name_variation
            lines.append(
                f"Name:   '{v.original}' → '{v.modified}' "
                f"[{v.rule_name}, {v.similarity_pct:.1f}% similar]"
            )
        if result.symbol_variation:
            v = result.symbol_variation
            lines.append(
                f"Symbol: '{v.original}' → '{v.modified}' "
                f"[{v.rule_name}, {v.similarity_pct:.1f}% similar]"
            )
        if result.description_variation and result.source_description:
            v = result.description_variation
            lines.append(f"Desc:   rule={v.rule_name}")
        lines.append(f"Overall similarity: {result.overall_similarity:.1f}%")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Levenshtein similarity (no external dependencies)
# ---------------------------------------------------------------------------

def _levenshtein_distance(a: str, b: str) -> int:
    """Standard Levenshtein edit distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    rows = len(a) + 1
    cols = len(b) + 1
    prev = list(range(cols))
    for i in range(1, rows):
        curr = [i] + [0] * (cols - 1)
        for j in range(1, cols):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _levenshtein_similarity(a: str, b: str) -> float:
    """Return similarity as 0–100 float (100 = identical)."""
    if not a and not b:
        return 100.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 100.0
    dist = _levenshtein_distance(a, b)
    return round((1 - dist / max_len) * 100, 2)


def _is_emoji(text: str) -> bool:
    """Return True if text consists entirely of emoji characters."""
    if not text:
        return False
    for ch in text:
        cat = unicodedata.category(ch)
        # Emoji are in So (Symbol, Other) or have high code points
        if cat not in ("So", "Mn") and ord(ch) < 0x1F300:
            return False
    return True


def compute_similarity(a: str, b: str) -> float:
    """Public similarity function — used by copy_scorer and dashboard."""
    return _levenshtein_similarity(a, b)
