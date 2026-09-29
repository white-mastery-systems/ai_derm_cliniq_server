"""
entities/snomed.py — SNOMED CT Lookup Utility
==============================================

Loads the dermatology SNOMED subset CSV into memory and provides
fast case-insensitive lookup by diagnosis text.

CSV FORMAT (snomed_dermatology_subset.csv)
-------------------------------------------
conceptId, fsn, synonyms
"9014002","Psoriasis (disorder)","Psoriasis | Psoriasis vulgaris"

MATCHING STRATEGY (in priority order)
--------------------------------------
1. Exact normalized match against any synonym
2. Prefix match against any synonym
3. Contains match against fsn (full specified name)
4. No match → returns (None, None)

Normalization: lowercase + strip whitespace.

PERFORMANCE
-----------
CSV (~31K rows) is loaded once into a list of tuples at first access
via lazy initialization. Subsequent calls reuse the in-memory index.
Lookup is O(n) — acceptable for a few diagnoses per case.
If performance becomes an issue, build a dict from lowercased terms.

USAGE
-----
    from src.entities.snomed import lookup_snomed
    code, term = lookup_snomed("Psoriasis")
    # code = "9014002", term = "Psoriasis (disorder)"
"""

import csv
import re
from functools import lru_cache
from pathlib import Path

from src.logger import get_logger

logger = get_logger(__name__)

_CSV_PATH = Path(__file__).parent / "data" / "snomed_dermatology_subset.csv"


@lru_cache(maxsize=1)
def _load_entries() -> list[tuple[str, str, list[str]]]:
    """
    Load and parse the CSV once. Returns list of (concept_id, fsn, [synonyms]).
    lru_cache ensures this runs exactly once per process.
    """
    entries: list[tuple[str, str, list[str]]] = []
    try:
        with open(_CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                concept_id = row.get("conceptId", "").strip().strip('"')
                fsn = row.get("fsn", "").strip().strip('"')
                synonyms_raw = row.get("synonyms", "").strip().strip('"')
                synonyms = [s.strip() for s in synonyms_raw.split("|") if s.strip()]
                if concept_id and fsn:
                    entries.append((concept_id, fsn, synonyms))
        logger.info("snomed_csv_loaded", total_entries=len(entries))
    except FileNotFoundError:
        logger.error("snomed_csv_not_found", path=str(_CSV_PATH))
    except Exception as exc:
        logger.error("snomed_csv_load_error", error=str(exc))
    return entries


def _normalize(text: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


def lookup_snomed(diagnosis_text: str) -> tuple[str | None, str | None]:
    """
    Look up a SNOMED CT code for the given diagnosis text.

    Parameters
    ----------
    diagnosis_text : str
        Free-text diagnosis as produced by the AI
        (e.g. "Psoriasis", "Atopic eczema", "Contact dermatitis")

    Returns
    -------
    (snomed_code, snomed_term) — both None if no match found
    """
    if not diagnosis_text or not diagnosis_text.strip():
        return None, None

    needle = _normalize(diagnosis_text)
    entries = _load_entries()

    # Pass 1: exact match against synonyms
    for concept_id, fsn, synonyms in entries:
        for syn in synonyms:
            if _normalize(syn) == needle:
                return concept_id, fsn

    # Pass 2: exact match against fsn (strip the "(disorder)" suffix for comparison)
    for concept_id, fsn, synonyms in entries:
        fsn_clean = re.sub(r"\s*\([^)]*\)\s*$", "", fsn).strip()
        if _normalize(fsn_clean) == needle:
            return concept_id, fsn

    # Pass 3: needle is a prefix of any synonym
    for concept_id, fsn, synonyms in entries:
        for syn in synonyms:
            if _normalize(syn).startswith(needle):
                return concept_id, fsn

    # Pass 4: any synonym contains the needle
    for concept_id, fsn, synonyms in entries:
        for syn in synonyms:
            if needle in _normalize(syn):
                return concept_id, fsn

    return None, None
