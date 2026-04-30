"""
Patient Consultation Prompts
=============================
All prompts used during the patient-facing question-answer consultation flow.

Source: Dermchatbot2/main4.py  (process_user_input class)

These prompts operate without images — they use conversation history,
complaints, visual description text, and differential diagnosis JSON.
"""

from __future__ import annotations


def _p(key: str, default: str) -> str:
    try:
        from src.ai.prompt_registry import get_prompt
        v = get_prompt(key)
        return v if v else default
    except Exception:
        return default


class PatientConsultationPrompts:
    """Factory for patient-facing consultation prompts."""

    # ------------------------------------------------------------------
    # 1. General complaints (no image available)
    # ------------------------------------------------------------------

    @staticmethod
    def get_general_complaints() -> str:
        """
        Generate patient-language complaints when NO image is uploaded.
        Focuses on subjective / invisible-lesion conditions.

        Template vars: {age}, {sex}
        Returns: {{"Complaint": ["...", ...]}}
        Used in: no-image patient entry path.
        """
        return """You are a dermatology AI assistant. The patient has reported no visible skin lesions or only subjective symptoms.
Generate a list of potential presenting complaints that a patient might describe in their own words, focusing on conditions that may not present with persistent visible lesions or are purely subjective.
Examples include: "Generalized itching", "Dry skin", "Burning sensation", "Crawling sensation", "Transient red bumps (hives)", "Scalp itching without dandruff".
Do not provide a diagnosis.
List complaints the patient may report.
Place more probable complaints higher in the list based on the patient's age and sex if relevant.

Age: {age}
Sex: {sex}

Return the output in the following JSON format:
{{"Complaint": ["Complaint1", "Complaint2", ...]}}
"""

    # ------------------------------------------------------------------
    # 2. Questions from complaints (no image)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_questions_from_complaints() -> str:
        """
        Generate 1 follow-up question from patient complaints when no image exists.

        Template vars: {age}, {sex}, {complaints}, {follow_up_context}
        follow_up_context is an empty string for new complaints.
        Returns: Questions JSON schema.
        Used in: no-image consultation Q&A rounds.
        """
        return _p("patient_questions_no_image", """You are a dermatology AI assistant. The patient has reported specific complaints but no visible lesions (or no image provided).
Based on the complaints, age, and sex, generate 1 relevant question to ask the patient to narrow down the diagnosis. Choose the single most important question that will provide the most diagnostic value.
Provide answer options for the question.

Age: {age}
Sex: {sex}
Complaints: {complaints}
{follow_up_context}

Return the output in the following JSON format:
{{
  "Questions": [
    {{
      "question": "<question>",
      "answer_options": ["<answer1>", "<answer2>", ...]
    }}
  ]
}}
""")

    # ------------------------------------------------------------------
    # 3. Differential from complaints (no image)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_differential_from_complaints() -> str:
        """
        Generate initial differential when only complaints are available (no image).

        Template vars: {age}, {sex}, {complaints}, {prescription}, {follow_up_context}
        follow_up_context is an empty string for new complaints.
        Returns: differential-diagnosis JSON schema.
        Used in: no-image path initial differential.
        """
        return """Based on the provided patient particulars, reported complaints, and previous prescription (if any), generate a structured JSON output that includes:

Most Probable Diagnosis: The most likely diagnosis, along with its likelihood and key supporting features.
Differential Diagnoses: A list of as many alternative diagnoses as possible, each with its corresponding likelihood and key supporting features.
confidence in answer: high or medium or low

Age: {age}
Sex: {sex}
Complaints: {complaints}
Previous prescription: {prescription}
{follow_up_context}

The JSON format should be strictly as follows:
{{
  "most_probable_diagnosis": {{
    "diagnosis": "",
    "likelihood": "",
    "key_supporting_features": ""
  }},
  "differential_diagnoses": [
    {{
      "diagnosis": "",
      "likelihood": "",
      "key_supporting_features": ""
    }}
  ],
  "confidence in answer": "<<one out of high, medium, low>>"
}}

Be sure not to include '/' in the diagnosis.
"""

    # ------------------------------------------------------------------
    # 4. Iterative diagnosis refinement (conversation-based)
    # ------------------------------------------------------------------

    @staticmethod
    def diagnosis_analysis_from_conversation() -> str:
        """
        Revise the differential based on NEW information in the latest conversation turn.
        Keeps or updates most-probable diagnosis and ordering.

        Template vars: {conversation_history}, {previous_differential},
                       {visual_description}, {prescription}, {follow_up_context}
        follow_up_context is an empty string for new complaints.
        Returns: differential-diagnosis JSON schema.
        Used in: after every patient answer round.
        """
        return _p("patient_diagnosis_refinement", """Create a revised json from the current json of Disease and differentials based on any new findings that might have appeared in the last message of the conversation. You can choose to keep the most probable diagnosis and the order of differential diagnosis and their likelihood as it is or can choose to change.
You are also being provided with the patient particulars and the visual description of the lesion for additional context.

Conversation history:
{conversation_history}

Previous disease and differential:
{previous_differential}

Visual description:
{visual_description}

Previous prescription:
{prescription}
{follow_up_context}

Generate a structured JSON output that includes:

Most Probable Diagnosis: The most likely diagnosis, along with its likelihood and key supporting features.
Differential Diagnoses: A list of as many alternative diagnoses as possible, each with its corresponding likelihood and key supporting features.
confidence in answer: high or medium or low

The JSON format should be strictly as follows:

{{
  "most_probable_diagnosis": {{
    "diagnosis": "",
    "likelihood": "",
    "key_supporting_features": ""
  }},
  "differential_diagnoses": [
    {{
      "diagnosis": "",
      "likelihood": "",
      "key_supporting_features": ""
    }}
  ],
  "confidence in answer":"<<one out of high, medium, low>>"
}}
""")

    # ------------------------------------------------------------------
    # 5. Doctor-agent doubts (patient-side)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_question_from_context() -> str:
        """
        Single-call replacement for the old two-step
        generate_doctor_doubts_patient() → generate_follow_up_questions() chain.

        Thinks medically first (dermatologist reasoning), then immediately
        frames the single most important doubt as a patient-friendly question.
        Cuts round latency from ~29s (2 Gemini calls) to ~15s (1 Gemini call).

        Template vars: {conversation}, {visual_description}, {diagnoses},
                       {previous_questions}, {prescription}, {datetime},
                       {follow_up_context}
        follow_up_context is an empty string for new complaints.
        Returns:
          If more to ask: {{"doubt_present":"yes","Questions":[{{"question":"...","answer_options":[...]}}]}}
          If nothing more: {{"doubt_present":"no"}}
        Used in: Celery `generate_questions_task` round 1+.
        """
        return _p("patient_follow_up_question", """You are an intelligent dermatologist AI assistant conducting a patient consultation.

You have access to the conversation history, visual description, differential diagnosis, and previous prescription.

Step 1 — Think medically:
Review everything available. Identify the single most important clinical doubt that, if clarified, would most help you differentiate between diagnoses or better understand the patient's condition.
Consider: symptom patterns, triggers, duration, severity, associated conditions, response to treatments, or anything inconsistent in the current data.
If you have no remaining doubts that would meaningfully change the diagnosis or management — return doubt_present: no.

Step 2 — Frame for the patient:
Convert that one medical doubt into a single clear, simple question a non-medical patient can understand and answer.
Provide as many descriptive answer options as needed to cover all likely responses.
Do NOT repeat any question already asked in the conversation.
Do NOT ask anything irrelevant to this specific case.

Conversation:
{conversation}

Visual description:
{visual_description}

Diagnoses:
{diagnoses}

Previous questions asked:
{previous_questions}

Previous prescription:
{prescription}

Current date and time:
{datetime}
{follow_up_context}

Respond in JSON only. No other text.

If you have a question to ask:
{{"doubt_present": "yes", "Questions": [{{"question": "<patient-friendly question>", "answer_options": ["<option1>", "<option2>", ...]}}]}}

If nothing more to ask:
{{"doubt_present": "no"}}
""")

    @staticmethod
    def generate_doctor_doubts_patient() -> str:
        """
        DEPRECATED — replaced by generate_question_from_context() which does
        both doubt-raising and question generation in a single Gemini call.
        Kept for reference only.

        Template vars: {conversation}, {visual_description}, {diagnoses},
                       {prescription}, {datetime}
        Returns: {{"doubt_present":"yes","doubt":[...]}} or {{"doubt_present":"no"}}
        """
        return """You are a part of a dermatological diagnostic application where you are playing the role of an intelligent dermatologist agent.
The application takes in photographs of the patient, generates visual description and differential diagnosis from the photograph. It also takes in text extracted from OCR of previous prescription. Remember the prescription might be for the same or any other disease. Based on this it generates questions which are answered by the patient.
The question and answers are stored inside conversation. You are also being provided with the current date and time when the application is being used.
Based on the above (conversation, diagnoses, visual_description, previous prescription, age, sex) you can raise as many (multiple) doubts/queries as required, clearing which are important for you as a dermatologist to diagnose and manage the patient. These doubts can include clarifications important in differentiating between various differentials or probing further regarding a particular symptom or probing regarding any inciting factor or associated systemic conditions or complications of the disease that is currently under contention or clarifying any particular point brought about by the patient.
If you decide to raise the doubts also give the reason for raising each of the doubt in the current context of the patient.
You can also decide to not raise any doubt if you think there is nothing more to ask.
These doubts are then passed on to the question generating agent who clears them by asking the same to the patient.
Then based on the answers given by the patient the differential diagnosis is revised (if required) and the conversation is updated.

Remember other than the age and sex of the patient no information that is being given to you is hard truth and can be questioned by you if required.
For eg. the diagnosis might be wrong because of wrong/inadequate context given by the visual description or the conversation.
The conversation might be misleading as the patient might not have understood the question properly.
Mention clearly in the reason of the doubt why you are raising it, how would clearing it help you.
If the doubt is a clarification about something that the patient had earlier said, mention that too.

Remember if there is a discordance between the visual description and what patient says you can raise a doubt once but if you see in the conversation that patient repeatedly answers the same thing which is not in concordance with the visual description or the differentials, trust what the patient says.

Conversation:
{conversation}

Visual description:
{visual_description}

Diagnoses:
{diagnoses}

Previous prescription:
{prescription}

Current date and time:
{datetime}

Give your response as follows in a json format:

If doubts present:
{{"doubt_present":"yes", "doubt":[{{"doubt1":"<doubt>","reason":"<reason for raising doubt>"}}, {{"doubt2":"<doubt>","reason":"<reason for raising doubt>"}}, ...]}}

If doubt absent:
{{"doubt_present":"no"}}
"""

    # ------------------------------------------------------------------
    # 6. Follow-up question generation (from doctor doubts)
    # ------------------------------------------------------------------

    @staticmethod
    def generate_follow_up_questions() -> str:
        """
        Convert doctor-agent doubts into 1 patient-friendly question with answer options.
        Prevents repeating questions from previous rounds.

        Template vars: {doubts}, {conversation_history}, {diagnoses},
                       {visual_description}, {previous_questions}, {prescription}
        Returns: Questions JSON schema.
        Used in: Celery `generate_questions_task` — called AFTER generate_doctor_doubts_patient().
        """
        return """You are a question generating agent whose job is to generate a question based on the doubts raised by the doctor agent.
Based on the doubts raised by the dermatologist agent, choose the single most important doubt and frame one question that best clears it.
Also provide as many descriptive answer choices for the question as possible that encompasses all likely patient responses.
Remember not to repeat any question from the set of previous questions.
You are also being provided with conversation history, differential diagnosis list, previous prescription and visual description just to add context to your question and answer choices.
While choosing the question to ask, remember to not repeat any question which has already been asked in the conversation earlier unless the doubts mentioned by the doctor clearly states that it wants some clarification. In that case also don't repeat a similar question more than twice under any circumstance. If questions for all doubts have been asked in the previous conversation, you can generate any other question relevant to this case.
In case you are repeating the question clearly mention why you are asking the question again while asking the question.
Also under no circumstances ask same/similar question thrice even if the same doubt has been raised by the doctor agent. Don't ask any question which is not relevant to the current case.

Doubts:
{doubts}

Conversation history:
{conversation_history}

Differential diagnosis:
{diagnoses}

Visual description:
{visual_description}

Previous questions:
{previous_questions}

Previous prescription:
{prescription}

Remember to give your response in json format as below:

{{
  "Questions": [
    {{
      "question": "<question>",
      "answer_options": ["<answer1>", "<answer2>", "<answer3>", ...]
    }}
  ]
}}
"""

    # ------------------------------------------------------------------
    # 7. Question count selector
    # ------------------------------------------------------------------

    @staticmethod
    def question_numbers() -> str:
        """
        Determine how many follow-up questions to ask (1–15) based on diagnosis complexity.
        Simple/classical presentations need fewer; uncertain ones need more.

        Template vars: {diagnoses}
        Returns: {{"no_of_questions": <int>}}
        Used in: at start of Q&A round to set the round limit.
        """
        return """For dermatological diagnoses that are straightforward and classical, asking too many questions can be burdensome for patients and may not add clinical value. However, for conditions where the diagnosis is uncertain or additional patient inputs may aid in diagnosis or management, a more detailed line of questioning is appropriate.

You are to select an appropriate number of follow-up questions (between 1 and 15) to be asked for a given dermatological diagnoses and differential. This number should reflect the complexity of the diagnosis and the potential usefulness of further patient input.

Diagnoses:
{diagnoses}

Respond only in the following JSON format:

{{"no_of_questions": <number>}}
"""

    # ------------------------------------------------------------------
    # 8. Case summary
    # ------------------------------------------------------------------

    @staticmethod
    def make_case_summary() -> str:
        """
        Generate a structured patient case summary (<150 words) for handoff to doctor.

        Template vars: {conversation_history}, {visual_language_model_text},
                       {possible_diagnoses}, {personal_particulars}, {follow_up_context}
        follow_up_context is an empty string for new complaints.
        Returns: {{"case_summary": "...", "display_statements": [...]}}
        Used in: final step of patient consultation before doctor review.
        """
        return _p("patient_case_summary", """Based on the user's age, sex, medical history, answers given by user in the chat, and the visual language model's analysis of the uploaded photograph, create a structured case summary in less than 150 words.
At the end give the most probable diagnosis with its likelihood taken from 'Possible Diagnoses' to you and place all other diagnoses in differential diagnoses with their likelihood.
Your output should follow this exact structure for easy parsing:

1. **Age**: Provide the age of the patient.
2. **Sex**: Specify the sex of the patient.
3. **Chief Complaint**: Briefly describe the chief complaint or presenting symptom.
4. **History**: Summarize any relevant medical history, including the duration of the condition and any previous treatments or exacerbating factors.
5. **Photograph Analysis**: Provide a brief description of the findings in the photograph.
6. **Most Probable Diagnosis**: Clearly state one single most probable diagnosis with its likelihood.
7. **Differential Diagnosis**: Clearly state other differential diagnoses to consider with their likelihood.

If this is a follow-up visit (previous visit context provided below), begin the History section with a reference to the previous visit diagnosis and note whether symptoms have improved, worsened, or stayed the same.

Do not skip any part of the context provided. Do not fabricate any facts that are not present.

Patient particulars:
{personal_particulars}

Conversation history:
{conversation_history}

Visual language model text:
{visual_language_model_text}

Possible diagnoses:
{possible_diagnoses}
{follow_up_context}

Return JSON in the following format:
{{
  "case_summary": "<the structured case summary in the exact format shown above>",
  "display_statements": [
    "<4-6 short, patient-friendly status updates about their case being prepared>",
    "..."
  ]
}}

The display_statements should be concise, non-repetitive, and avoid medical advice.
Keep them grounded in the provided context (age/sex/complaint/visual findings).
""")

    # ------------------------------------------------------------------
    # 9. Patient chatbot (post-summary)
    # ------------------------------------------------------------------

    @staticmethod
    def talk_on_summary() -> str:
        """
        Patient-facing chatbot that answers questions post-consultation.
        Uses the generated case summary as grounding context.

        Template vars: {case_summary}, {conversation}
        Returns: free-text response.
        Used in: patient chat endpoint after case_summary is created.
        """
        return """Imagine that you are a dermatologist treating a patient. Chat and answer the queries of the patient based on their case summary and last few conversations. Don't be repetitive. Be to the point with your answers.

Case Summary:
{case_summary}

Last few conversations:
{conversation}
"""

    # ------------------------------------------------------------------
    # 10. Treatment plan (patient-facing, simplified)
    # ------------------------------------------------------------------

    @staticmethod
    def treatment_plan() -> str:
        """
        Patient-friendly treatment plan based on differential and conversation.

        Template vars: {conversation}, {differential}
        Returns: free-text response.
        Used in: patient report section.
        """
        return """Imagine that you are a dermatologist treating a patient. Generate a comprehensive treatment plan for the patient telling about the medications, lifestyle changes, dietary modifications/requirements based on their last few conversations with the agent and the differential. Tailor the plan based on the patient's age, sex and their severity of illness.

Last few conversations:
{conversation}

Differential:
{differential}
"""

    # ------------------------------------------------------------------
    # 11. Disease cause / pathogenesis (patient-friendly)
    # ------------------------------------------------------------------

    @staticmethod
    def disease_cause() -> str:
        """
        Explain the cause/pathogenesis in patient-friendly language.

        Template vars: {conversation}, {differential}
        Returns: free-text response.
        """
        return """Tell the patient the cause/pathogenesis of their dermatological condition based on their last few conversations with the agent and differentials. Explain this in the language that the patient can understand, explaining the technical terms if used in simple language.

Last few conversations:
{conversation}

Differential:
{differential}
"""

    # ------------------------------------------------------------------
    # 12. Investigations (patient-friendly)
    # ------------------------------------------------------------------

    @staticmethod
    def investigation() -> str:
        """
        Advise on investigations required, whether necessary, and why.

        Template vars: {conversation}, {differential}
        Returns: free-text response.
        """
        return """Based on their last few conversations, differentials tell the patient if any further investigations are required to be done to confirm the diagnosis of the case also mentioning whether the investigation is necessary or not in this case and if necessary why is it necessary.

Last few conversations:
{conversation}

Differentials:
{differential}
"""

    # ------------------------------------------------------------------
    # 13. JSON repair utility
    # ------------------------------------------------------------------

    REPAIR_FORMATS = {
        "question": """{{"Questions": [{{"question": "<q1>", "answer_options": ["<a1>", "<a2>", ...]}}]}}""",
        "complaint": """{"Complaint": ["Complaint1", "Complaint2", ...]}""",
        "doctor": """{"doubt_present":"yes", "doubt":[{"doubt1":"<doubt>","reason":"<reason>"}]} OR {"doubt_present":"no"}""",
        "image_inspection": """{"answer":"no","reason":"..."} OR {"answer":"yes"}""",
        "differential": """{{"most_probable_diagnosis": {{"diagnosis": "", "likelihood": "", "key_supporting_features": ""}}, "differential_diagnoses": [...], "confidence in answer": "high|medium|low"}}""",
        "prescription": """{"text": "<extracted text>", "reliability": "good|medium|bad"}""",
        "description": """{{"type_of_lesion": "", "site": "", "count": "", "arrangement": "", "size": "", "color_pattern": "", "border": "", "surface_changes": "", "presence_of_exudate_or_discharge": "", "surrounding_skin_changes": "", "secondary_changes": "", "pattern_or_shape": "", "additional_notes": "", "overall_description": ""}}""",
    }

    @classmethod
    def json_repair(cls, correct_format_key: str) -> str:
        """
        Repair a malformed JSON response to the expected schema.

        Template vars: {damaged_json}, {correct_format}
        Pass correct_format_key from REPAIR_FORMATS dict.
        Returns: repaired JSON string.
        Used in: error recovery after LLM returns unparseable JSON.
        """
        correct_format = cls.REPAIR_FORMATS.get(correct_format_key, "{}")
        return f"""Repair the given damaged json to the correct json format as provided to you.

Damaged JSON:
{{damaged_json}}

Correct format:
{correct_format}
"""

    # ------------------------------------------------------------------
    # 14. Systemic / red flag check
    # ------------------------------------------------------------------

    @staticmethod
    def red_flag_check(
        complaint: str,
        answers: str,
        patient_reported_symptoms: str = "",
    ) -> str:
        """
        Check patient complaint, Q&A answers, and self-reported systemic symptoms
        for urgent / red-flag conditions.

        patient_reported_symptoms: pre-formatted bullet list of symptoms the patient
        selected on the Systemic Check screen. Empty string if none reported.

        Returns JSON: {{"flags": [...], "advice": "<string or null>"}}
        flags is an empty list when no red flags are found.
        advice is a patient-readable warning present only when flags is non-empty.
        """
        symptoms_section = (
            f"Patient self-reported systemic symptoms:\n{patient_reported_symptoms}"
            if patient_reported_symptoms
            else "Patient self-reported systemic symptoms:\nNone reported"
        )
        return f"""You are a dermatology triage assistant.
Review the patient's presenting complaint, their answers to follow-up questions, and
any systemic symptoms they self-reported on the Systemic Check screen.
Identify any urgent 'red flag' symptoms that require immediate medical attention.

Red flag examples (not exhaustive):
- Rapidly changing or bleeding mole (possible melanoma)
- Systemic symptoms: fever, weight loss, night sweats alongside skin changes
- Signs of cellulitis with spreading redness, warmth, systemic fever
- Stevens-Johnson syndrome indicators: blistering mucous membranes
- Anaphylaxis indicators: hives + throat tightness + difficulty breathing
- Rapidly spreading purpuric rash (possible meningococcal)

Presenting complaint:
{complaint}

Patient answers:
{answers}

{symptoms_section}

Respond ONLY with valid JSON in this exact format:
{{"flags": ["<flag1>", "<flag2>"], "advice": "<patient-friendly urgent advice, or null if no flags>"}}

If no red flags are found: {{"flags": [], "advice": null}}
"""

    # ------------------------------------------------------------------
    # Case summary AI query (patient Ask AI feature)
    # ------------------------------------------------------------------

    @staticmethod
    def case_query() -> str:
        """
        Answer a free-text patient question about their own case.

        Template vars: {diagnosis}, {differential_json}, {case_summary},
                       {conversation_history}, {question}
        Used in: POST /cases/{case_id}/ai/query
        """
        return """You are a compassionate AI health assistant helping a patient understand their dermatology case summary.

You have access to the following information about the patient's case:

**AI Diagnosis:** {diagnosis}

**Full Differential:** {differential_json}

**Case Summary:**
{case_summary}

**Q&A History:**
{conversation_history}

---

The patient has asked: "{question}"

Instructions:
- Answer clearly and in simple, non-technical language the patient can understand.
- If the question is about treatment, explain general options but remind them their doctor will give the final plan.
- If the question is about investigations, mention what is typically recommended for the diagnosis.
- Do NOT speculate beyond what the case data supports.
- Keep the answer concise — 2 to 4 short paragraphs maximum.
- Do not repeat the diagnosis name unnecessarily.
- Never say you cannot help — always give a useful, grounded answer based on the case data above.
"""
