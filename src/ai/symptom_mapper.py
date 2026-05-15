"""
ai/symptom_mapper.py — Differential-to-Systemic-Symptom Mapper
================================================================

Maps the AI's differential diagnosis output to a relevant, case-specific
list of systemic symptom options shown to the patient on the Systemic Check
screen.

WHY RULE-BASED INSTEAD OF LLM?
--------------------------------
The differential is already structured JSON produced by the AI. Mapping it
to symptom categories with keyword matching is instant (no extra API call),
deterministic, and sufficient for the finite set of dermatology red-flag
categories that matter clinically.

OUTPUT FORMAT
-------------
Returns a list of dicts: [{"id": "<slug>", "label": "<patient-readable text>"}]
Always ends with {"id": "none", "label": "None of the above"}.
Generic systemic symptoms (fever, night sweats, weight loss, rapid worsening)
are always included as they apply to any skin condition.
"""

from __future__ import annotations

# ------------------------------------------------------------------ #
# Full symptom catalogue
# ------------------------------------------------------------------ #

_CATALOG: dict[str, str] = {
    # Pigmented lesion / melanoma
    "bleeding_mole":      "Bleeding from a mole or lesion",
    "colour_change":      "Rapid change in mole colour, size, or shape",
    "border_irregular":   "Irregular or expanding border of a mole",
    # Infection / cellulitis / necrotising
    "spreading_redness":  "Spreading redness or warmth around skin",
    "skin_numbness":      "Numbness or loss of feeling at the wound site",
    "foul_smell":         "Foul-smelling wound or discharge",
    "skin_blackening":    "Black or dark discolouration of skin",
    # Severe skin reactions (SJS / TEN / bullous)
    "blistering":         "Blistering or large fluid-filled sores on skin",
    "skin_peeling":       "Skin peeling or sloughing off in sheets",
    "mucous_membrane":    "Sores or blistering in mouth, eyes, or genitals",
    # Allergic / anaphylaxis
    "throat_tightness":   "Throat tightness or difficulty swallowing",
    "breathing_difficulty": "Difficulty breathing or shortness of breath",
    "spreading_hives":    "Rapidly spreading hives or swelling (angioedema)",
    # Generic systemic — always shown
    "fever":              "Fever or chills",
    "night_sweats":       "Night sweats",
    "weight_loss":        "Unexplained weight loss",
    "rapid_worsening":    "Rapid worsening of skin condition",
    # Terminal option — always last
    "none":               "None of the above",
}

# ------------------------------------------------------------------ #
# Keyword → symptom IDs mapping
# ------------------------------------------------------------------ #

# Each entry: (tuple of lowercase keywords, list of symptom IDs to add)
# Keywords are matched as substrings against the lowercased diagnosis names.
_RULES: list[tuple[tuple[str, ...], list[str]]] = [
    (
        ("melanoma", "naevus", "naevi", "mole", "lentigo", "lentigines",
         "dysplastic", "pigmented lesion", "acral", "sebaceous"),
        ["bleeding_mole", "colour_change", "border_irregular"],
    ),
    (
        ("cellulitis", "erysipelas", "abscess", "wound infection",
         "necrotising", "necrotizing", "fasciitis", "gangrene",
         "infected wound", "furuncle", "carbuncle"),
        ["spreading_redness", "skin_numbness", "foul_smell", "skin_blackening"],
    ),
    (
        ("stevens-johnson", "sjs", "toxic epidermal necrolysis", "ten",
         "erythema multiforme", "bullous pemphigoid", "pemphigus",
         "blistering", "vesiculobullous"),
        ["blistering", "skin_peeling", "mucous_membrane"],
    ),
    (
        ("anaphylaxis", "urticaria", "angioedema", "allergic reaction",
         "contact dermatitis", "drug reaction", "drug eruption"),
        ["throat_tightness", "breathing_difficulty", "spreading_hives"],
    ),
    (
        ("vasculitis", "purpura", "meningococcal", "henoch-schonlein",
         "leukocytoclastic", "petechiae"),
        ["skin_blackening", "spreading_redness"],
    ),
    (
        ("squamous cell carcinoma", "scc", "basal cell carcinoma", "bcc",
         "merkel", "kaposi"),
        ["bleeding_mole", "border_irregular"],
    ),
]

# IDs always included regardless of diagnosis
_ALWAYS: list[str] = ["fever", "night_sweats", "weight_loss", "rapid_worsening"]


# ------------------------------------------------------------------ #
# Public function
# ------------------------------------------------------------------ #

def map_symptoms_from_differential(
    most_probable: str | None,
    differentials: list[str],
) -> list[dict[str, str]]:
    """
    Build a case-specific systemic symptom option list from the AI differential.

    Args:
        most_probable:  Most probable diagnosis name (string or None).
        differentials:  List of differential diagnosis name strings.

    Returns:
        Ordered list of {"id": ..., "label": ...} dicts, always ending with
        {"id": "none", "label": "None of the above"}.
    """
    # Combine all diagnosis names into one lowercase string for matching
    all_names = " | ".join(
        filter(None, [most_probable] + differentials)
    ).lower()

    selected_ids: list[str] = []
    seen: set[str] = set()

    def _add(ids: list[str]) -> None:
        for sid in ids:
            if sid not in seen:
                seen.add(sid)
                selected_ids.append(sid)

    # Apply keyword rules
    for keywords, symptom_ids in _RULES:
        if any(kw in all_names for kw in keywords):
            _add(symptom_ids)

    # Always-present systemic symptoms
    _add(_ALWAYS)

    # Terminal option
    _add(["none"])

    return [{"id": sid, "label": _CATALOG[sid]} for sid in selected_ids]
