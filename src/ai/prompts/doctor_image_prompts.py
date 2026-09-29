"""
Doctor Image Analysis Prompts
==============================
Prompts for the doctor diagnose flow — three sequential image analyses
(clinical → dermoscopy → pathology) and AI reconciliation when the doctor
edits the generated text.

Source: Dermchatbot2/ImageAnalyser_doctor.py  (DermatologyAnalyzer class)

CHAIN ORDER (critical — each step feeds into the next as context)
-----------------------------------------------------------------
1. clinical_description()     — analyse clinical photo(s)
2. dermoscopic_description()  — analyse dermoscopy using clinical as context
3. pathological_description() — analyse pathology using both as context

Each prompt returns a structured JSON object. The combined result is stored
as Case.visual_findings = {"clinical": {...}, "dermoscopy": {...}, "pathology": {...}}

RECONCILE PROMPTS
-----------------
When the doctor edits the overall_description text on screen, the reconcile
prompts re-derive the structured fields to stay consistent.
"""

from __future__ import annotations


class DoctorImageAnalysisPrompts:
    """Factory for doctor diagnose flow image analysis prompts."""

    # ------------------------------------------------------------------
    # 1. Clinical image description
    # ------------------------------------------------------------------

    @staticmethod
    def clinical_description() -> str:
        """
        Analyse clinical (standard) skin photographs.

        Template vars: {personal_particulars}
        Returns JSON with keys: type_of_lesion, site, count, arrangement,
          size, color_pattern, border, surface_changes,
          presence_of_exudate_or_discharge, surrounding_skin_changes,
          secondary_changes, pattern_or_shape, additional_notes,
          overall_description
        Used in: generate_visual_findings_task — step 1 of 3.
        """
        return """You are a dermatology assistant trained to analyze skin lesions. Given an image and personal particulars of the person, provide a structured JSON response that includes the following details:
- Type of lesion
- Count
- Arrangement
- Size
- Color/Pattern
- Border
- Surface changes
- Presence or absence of any exudate/discharge
- Surrounding skin changes
- Pattern or shape
- Any additional notes about the lesion that are not mentioned above
- Overall description in an unstructured fashion.

Personal Particulars: {personal_particulars}

Ensure that your response follows this JSON format exactly:
{{
  "type_of_lesion": "<describe the lesion type>",
  "site": "<mention site of the lesion>",
  "count": "<mention the number of lesions>",
  "arrangement": "<describe the distribution>",
  "size": "<approximate lesion size>",
  "color_pattern": "<describe the color and pattern>",
  "border": "<mention if well-defined or ill-defined>",
  "surface_changes": "<describe scaling, crusting, ulceration, etc.>",
  "presence_of_exudate_or_discharge": "<yes/no, and describe if present>",
  "surrounding_skin_changes": "<mention any erythema, dryness, etc.>",
  "secondary_changes": "<mention any secondary changes>",
  "pattern_or_shape": "<describe any specific shape or distribution>",
  "additional_notes": "<mention any extra details>",
  "overall_description": "<provide a paragraph summarizing the lesion>"
}}
"""

    # ------------------------------------------------------------------
    # 2. Dermoscopic image description (uses clinical finding as context)
    # ------------------------------------------------------------------

    @staticmethod
    def dermoscopic_description() -> str:
        """
        Analyse dermoscopy image(s) using the clinical description as context.

        Template vars: {personal_particulars}, {clinical_description}
        Returns JSON with dermoscopic feature keys + overall_dermoscopic_summary.
        Used in: generate_visual_findings_task — step 2 of 3.

        IMPORTANT: Pass clinical_description from step 1 — the AI uses it
        to contextualise the dermoscopic findings.
        """
        return """You are a dermatology assistant specialized in analyzing dermoscopic images.
You will be provided with the clinical description of the lesion. Based on that and the dermoscopic images, provide a structured JSON response detailing dermoscopic features.
Provide only the findings and do not comment on the diagnosis.

Personal Particulars: {personal_particulars}
Clinical Description: {clinical_description}

Ensure that your response follows this JSON format exactly:
{{
  "pigment_network": "<describe pigment network>",
  "dots_globules": "<describe dots and globules>",
  "streaks": "<describe streaks if present>",
  "blotches": "<describe blotches if present>",
  "blue_white_veil": "<describe blue-white veil if present>",
  "vascular_patterns": "<describe vascular patterns>",
  "regression_structures": "<describe regression structures if present>",
  "ulceration": "<describe ulceration if present>",
  "other_dermoscopic_features": "<describe any other features>",
  "overall_dermoscopic_summary": "<provide a paragraph summarizing the dermoscopic findings>"
}}
"""

    # ------------------------------------------------------------------
    # 3. Pathological image description (uses both prior findings as context)
    # ------------------------------------------------------------------

    @staticmethod
    def pathological_description() -> str:
        """
        Analyse histopathology slide image(s) using clinical + dermoscopic
        descriptions as context.

        Template vars: {personal_particulars}, {clinical_description},
                       {dermoscopic_description}
        Returns JSON with histopathological feature keys + overall_pathological_summary.
        Used in: generate_visual_findings_task — step 3 of 3.

        IMPORTANT: Only called when pathology images are actually present.
        Pass empty JSON {{}} if no pathology images uploaded.
        """
        return """You are a dermatology assistant specialized in analyzing histopathology slides.
You will be provided with the clinical and dermoscopic descriptions of the lesion. Based on that and the histopathology images, provide a structured JSON response detailing pathological features.
Provide only the findings and do not comment on the diagnosis.

Personal Particulars: {personal_particulars}
Clinical Description: {clinical_description}
Dermoscopic Description: {dermoscopic_description}

Ensure that your response follows this JSON format exactly:
{{
  "epidermal_changes": "<describe epidermal changes>",
  "dermal_infiltrate": "<describe dermal infiltrate>",
  "adnexal_structures": "<describe adnexal structures>",
  "vascular_changes": "<describe vascular changes>",
  "signs_of_malignancy": "<describe any signs of malignancy>",
  "other_pathological_features": "<describe any other features>",
  "overall_pathological_summary": "<provide a paragraph summarizing the pathological findings>"
}}
"""

    # ------------------------------------------------------------------
    # 4. Reconcile clinical description after doctor edit
    # ------------------------------------------------------------------

    @staticmethod
    def reconcile_clinical() -> str:
        """
        Update structured clinical JSON to match the doctor's edited
        overall_description text.

        Template vars: {overall_description}, {structured_data_json},
                       {personal_particulars}
        Returns: updated structured_data_json (same keys, values reconciled).
        Used in: PATCH /cases/{id}/visual-findings when doctor edits clinical text.
        """
        return """You are a medical data consistency expert. You will be given two inputs:
1. `overall_description`: A new, authoritative description of a skin lesion provided by a dermatologist. This is the source of truth.
2. `structured_data_json`: A JSON object containing related but now potentially outdated structured data (e.g., 'type_of_lesion', 'site', 'count').

Your task is to meticulously update the values in the `structured_data_json` object to be perfectly consistent with the information provided in the `overall_description`.

Return only the complete, updated `structured_data_json` object as valid JSON.

Source of Truth (Overall Description): {overall_description}
Outdated Structured Data (JSON): {structured_data_json}
Personal Particulars: {personal_particulars}
"""

    # ------------------------------------------------------------------
    # 5. Reconcile dermoscopic description after doctor edit
    # ------------------------------------------------------------------

    @staticmethod
    def reconcile_dermoscopic() -> str:
        """
        Update structured dermoscopic JSON to match the doctor's edited
        overall_dermoscopic_summary text.

        Template vars: {overall_description}, {structured_data_json},
                       {personal_particulars}
        Returns: updated structured_data_json (dermoscopic fields reconciled).
        Used in: PATCH /cases/{id}/visual-findings when doctor edits dermoscopy text.
        """
        return """You are a medical data consistency expert. You will be given two inputs:
1. `overall_dermoscopic_summary`: A new, authoritative dermoscopic description provided by a dermatologist. This is the source of truth.
2. `structured_data_json`: A JSON object containing related but now potentially outdated dermoscopic structured data (e.g., 'pigment_network', 'dots_globules', 'streaks').

Your task is to meticulously update the values in the `structured_data_json` object to be perfectly consistent with the information provided in the `overall_dermoscopic_summary`.

Return only the complete, updated `structured_data_json` object as valid JSON.

Source of Truth (Overall Dermoscopic Summary): {overall_description}
Outdated Structured Data (JSON): {structured_data_json}
Personal Particulars: {personal_particulars}
"""
