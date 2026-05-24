"""PPT page filters: perceptual-hash dedup + multi-pattern invalid-page match.

All configurable rules live in ``ppt_dedup_config.py`` — edit that file to
tune patterns without touching this logic code.

Pipeline (both stages run from main.py's ``_fetch_and_ocr_ppts``):

1. ``compute_dhash`` / ``dedup_dhash`` — pre-OCR, drops near-duplicate
   frames using perceptual hashing so the OCR pass runs on fewer images.

2. ``is_invalid_page`` — post-OCR, discards pages whose text matches known
   full-screen noise (desktop wallpaper, e-learning portal, file explorer, etc.)

3. ``clean_ppt_text`` — post-invalidation, strips per-line UI chrome labels
   (PowerPoint ribbon, IDE panels, system dialogs, etc.) from surviving pages.

4. ``dedup_text_subset`` — post-cleaning, removes pages whose text is a
   near-subset of a nearby page (PPT animation reveals, progressive bullet
   disclosure).
"""

from __future__ import annotations

import io
import re
from typing import Iterable

import imagehash
from PIL import Image

from src.ai.ppt_dedup_config import (
    DHASH_THRESHOLD,
    DHASH_WINDOW,
    INVALID_PAGE_PATTERNS,
    PPT_UI_STOPWORDS,
    SUBSET_CONFIG,
    UI_NOISE_LINE_PATTERNS,
)

# ── Compile regexes from config once at import time ─────────────────────────
_UI_NOISE_LINE_RES: list[re.Pattern] = [
    re.compile(p) for p in UI_NOISE_LINE_PATTERNS
]

_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
_NORMALIZE_UI_RE = re.compile(r"[\s　]+")


# ══════════════════════════════════════════════════════════════════════════════
# Stage 1 — PHASH dedup  (pre-OCR)
# ══════════════════════════════════════════════════════════════════════════════


def compute_dhash(image_bytes: bytes) -> str | None:
    """Perceptual hash for an image. Returns 16-hex string or None on error.

    Uses imagehash.dhash (8x8 difference hash). Identical/near-identical
    crops yield identical hashes; visually distinct frames almost always
    differ by more than 4 bits.  Caller must tolerate ``None`` (image
    decode failure, missing PIL, etc.) — those pages are excluded from
    the dedup pass and pass through to OCR untouched.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return str(imagehash.dhash(img))
    except Exception:
        return None


def _hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def dedup_dhash(
    items: list[str | None],
    window: int = DHASH_WINDOW,
    threshold: int = DHASH_THRESHOLD,
) -> list[int]:
    """Sliding-window perceptual dedup. Returns sorted list of dropped indices.

    For each surviving anchor i, scan forward collecting at most ``window``
    *non-dropped* items to compare against.  When the scan finds a match the
    matched index is dropped and the window automatically "fills forward" (the
    dropped slot is replaced by the next non-dropped item beyond the current
    window boundary).  This makes the dedup more aggressive than a fixed-position
    window since dropped items don't reduce the number of actual comparisons.

    Already-dropped images never become anchors — that prevents a chain of "near
    to last-kept" pages from cascading drops onto pages that aren't actually near
    the kept anchor.

    ``items`` may contain ``None`` (compute_dhash failure) — those indices are
    passed through (never dropped, never used as anchor).
    """
    n = len(items)
    dropped: set[int] = set()
    for i in range(n):
        if i in dropped:
            continue
        a = items[i]
        if a is None:
            continue
        cmp_count = 0
        j = i + 1
        while cmp_count < window and j < n:
            if j in dropped:
                j += 1
                continue
            b = items[j]
            if b is None:
                j += 1
                continue
            if _hamming_hex(a, b) <= threshold:
                dropped.add(j)
                # dropped slot is replaced by next non-dropped item
            else:
                cmp_count += 1
            j += 1
    return sorted(dropped)


# ══════════════════════════════════════════════════════════════════════════════
# Stage 2 — Full-page invalidation  (post-OCR)
# ══════════════════════════════════════════════════════════════════════════════


def _normalize_for_match(text: str) -> str:
    """Lowercase + strip whitespace and punctuation. CJK chars are kept."""
    if not text:
        return ""
    return _NORMALIZE_RE.sub("", text).lower()


def is_invalid_page(text: str) -> bool:
    """True if any feature string matches the (normalized) OCR'd text."""
    norm = _normalize_for_match(text)
    if not norm:
        return False
    return any(p in norm for p in INVALID_PAGE_PATTERNS)


def normalize_for_match(text: str) -> str:  # noqa: D401
    """Public alias for tests / debugging."""
    return _normalize_for_match(text)


# ══════════════════════════════════════════════════════════════════════════════
# Stage 3 — Per-line UI chrome stripping  (post-invalidation)
# ══════════════════════════════════════════════════════════════════════════════


def clean_ppt_text(text: str) -> str:
    """Remove window-chrome noise from OCR'd slide text.

    Operates per-line so a slide mixing real content and UI labels keeps the
    former while stripping the latter.  Returns the cleaned text (may be empty
    for a fully-noise page).
    """
    if not text:
        return ""
    kept: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        # ≤2 char lines are virtually always UI chrome (ribbon icons, IME
        # indicators, single letters from Alt-key shortcuts, etc.).
        if len(s) <= 2:
            continue
        # Normalise away the full-width ideographic space (U+3000) that
        # PowerPoint uses in its ribbon layout, and repeated spaces.
        norm = _NORMALIZE_UI_RE.sub("", s).strip()
        if not norm:
            continue
        # Exact stopword match (case-insensitive for ASCII labels).
        if norm in PPT_UI_STOPWORDS:
            continue
        if norm.lower() in PPT_UI_STOPWORDS:
            continue
        # Regex patterns — match against the normalised form.
        if any(p.fullmatch(norm) for p in _UI_NOISE_LINE_RES):
            continue
        kept.append(s)
    return "\n".join(kept)


# ══════════════════════════════════════════════════════════════════════════════
# Stage 4 — Text subset dedup  (post-cleaning, pre-prompt)
# ══════════════════════════════════════════════════════════════════════════════


def _normalize_subset(s: str) -> str:
    """Light normalisation: lowercase, fullwidth→halfwidth, collapse space."""
    s = s.lower()
    s = s.replace("　", " ").replace("�", "").replace("\xa0", " ")
    return " ".join(s.split())


def _ngrams(t: str, n: int = 3) -> set[str]:
    return {t[i:i+n] for i in range(max(1, len(t)-n+1))}


def dedup_text_subset(pages: list[dict]) -> list[dict]:
    """Sliding-window text subset dedup for PPT OCR pages.

    Uses directional 3-gram containment to detect pages whose text is a
    near-subset of a nearby page (common with PPT animation reveals).
    No line-level heuristics — OCR line breaks are unreliable.

    ``pages``: list of dicts, each with at least a ``text`` key.
    Returns filtered list with near-subset pages removed.
    """
    cfg = SUBSET_CONFIG

    texts = [_normalize_subset(p.get("text") or "") for p in pages]
    ng_sets = [_ngrams(t, cfg["ngram_n"]) for t in texts]
    lengths = [len(t) for t in texts]

    keep = [True] * len(pages)

    for idx in range(1, len(pages)):
        window_start = max(0, idx - cfg["window"])

        for old_idx in range(window_start, idx):
            if not keep[old_idx]:
                continue

            if lengths[idx] < lengths[old_idx]:
                short, long = idx, old_idx
            else:
                short, long = old_idx, idx

            effective_threshold = cfg["containment_threshold"]
            if lengths[short] < cfg["protect_min_chars"]:
                effective_threshold = 0.95

            if not ng_sets[short]:
                continue
            containment = len(ng_sets[short] & ng_sets[long]) / len(ng_sets[short])
            length_ratio = lengths[long] / max(lengths[short], 1)

            if containment < effective_threshold or length_ratio < cfg["min_length_ratio"]:
                continue

            if short == idx:
                keep[idx] = False
            else:
                keep[old_idx] = False
            break

    return [p for p, k in zip(pages, keep) if k]
