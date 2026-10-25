"""Token estimation for limits, metrics and fallback usage accounting.

Two estimators:

- ``UnicodeHeuristicEstimator`` (default): dependency-free character-class
  model calibrated against tiktoken's o200k_base on CJK/latin/mixed corpora
  (CJK ≈ 0.8 token/char, latin words ≈ 4.5 chars/token, digit runs 3/token,
  punctuation 2/token). Within ~±40% on real text — versus the previous flat
  ~4 chars/token heuristic, which under-counted Chinese ~3.6x.
- ``TiktokenEstimator``: exact counts via tiktoken when installed and the
  model encoding is reachable (``pip install aigateway[tokenizer]``).
  Unavailable at build time -> falls back to the heuristic with a warning,
  so an offline node never fails to start.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("aigateway.tokenizers")

#: CJK code points treated at ~0.7 token per character.
_CJK_RANGES = (
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x3040, 0x30FF),  # hiragana + katakana
    (0xAC00, 0xD7AF),  # hangul syllables
    (0xFF66, 0xFF9D),  # halfwidth katakana
)


def _is_cjk(code: int) -> bool:
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _is_letter(code: int) -> bool:
    return code > 127 or ("a" <= chr(code) <= "z") or ("A" <= chr(code) <= "Z")


class TokenEstimator(ABC):
    """Estimates the token count of a text. Deterministic, side-effect free."""

    @abstractmethod
    def estimate(self, text: str) -> int:
        """Token count; at least 1 for non-empty text, 0 for empty."""


class UnicodeHeuristicEstimator(TokenEstimator):
    """Character-class model (see module docstring for the calibration).

    CJK ≈ 0.7 token/char, latin word ≈ max(1, ceil((len-1)/5)), digit runs
    3 chars/token, punctuation/symbols ≈ 0.4 each. Aggregate error vs
    o200k_base ≈ ±20% on ordinary text (short high-frequency phrases can
    overshoot ~2x because BPE merges them — acceptable for prepaid limiting
    and cost estimates, which aggregate over many requests).
    """

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        total = 0.0
        word_letters = 0  # current latin/other-script letter run
        digit_run = 0

        def _flush_word() -> None:
            nonlocal word_letters, total
            if word_letters:
                total += max(1.0, math.ceil((word_letters - 1) / 5))
                word_letters = 0

        def _flush_digits() -> None:
            nonlocal digit_run, total
            if digit_run:
                total += math.ceil(digit_run / 3)
                digit_run = 0

        for char in text:
            code = ord(char)
            if char.isspace():
                _flush_word()
                _flush_digits()
            elif _is_cjk(code):
                _flush_word()
                _flush_digits()
                total += 0.7
            elif char.isdigit():
                _flush_word()
                digit_run += 1
            elif _is_letter(code):
                _flush_digits()
                word_letters += 1
            else:  # punctuation, symbols, emoji
                _flush_word()
                _flush_digits()
                total += 0.4
        _flush_word()
        _flush_digits()
        return max(1, math.ceil(total))


class TiktokenEstimator(TokenEstimator):
    """Exact counts via tiktoken; pick the encoding by model family."""

    def __init__(self, model: str = "gpt-4o-mini"):
        import tiktoken

        family = model.lower()
        if any(tag in family for tag in ("gpt-4o", "o1", "o3", "o4", "chatgpt", "gpt-5")):
            encoding_name = "o200k_base"
        else:
            encoding_name = "cl100k_base"
        self._encoding = tiktoken.get_encoding(encoding_name)

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        return len(self._encoding.encode_ordinary(text))


def build_tokenizer(server_config: dict[str, Any] | None) -> TokenEstimator:
    """Build the configured estimator; failures fall back to the heuristic."""
    server_config = server_config or {}
    kind = str(server_config.get("tokenizer", "unicode"))
    if kind in ("tiktoken", "exact"):
        try:
            return TiktokenEstimator(str(server_config.get("tokenizer_model", "gpt-4o-mini")))
        except Exception as exc:  # noqa: BLE001 - offline installs must still start
            logger.warning("tiktoken unavailable (%s); falling back to unicode heuristic", exc)
            return UnicodeHeuristicEstimator()
    if kind != "unicode":
        logger.warning("unknown tokenizer %r; using unicode heuristic", kind)
    return UnicodeHeuristicEstimator()


#: Process-wide default used by schemas.estimate_tokens and plugin defaults.
DEFAULT_ESTIMATOR: TokenEstimator = UnicodeHeuristicEstimator()


def estimate_default(text: str) -> int:
    return DEFAULT_ESTIMATOR.estimate(text)
