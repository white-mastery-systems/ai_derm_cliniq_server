"""
tests/unit/test_models.py — Unit Tests for All DB Models
=========================================================

WHAT WE TEST HERE
------------------
1. Enum values are correct strings (what gets stored in the DB)
2. Models can be instantiated with required fields only
3. Default values are applied correctly
4. String representations (__repr__) work without crashing
5. UUID generation produces unique values

WHAT WE DO NOT TEST HERE
--------------------------
- DB persistence (INSERT/SELECT) — that's in test_models_db.py
- Foreign key constraints — need a real DB session
- Relationships traversal — need DB session

LESSON: Unit tests for models are fast (no DB needed) and catch:
- Typos in enum values (e.g. "follow_up_availble" vs "follow_up_available")
- Wrong default values
- Missing __repr__ methods that crash on unexpected attribute access
"""

import uuid
from datetime import date, datetime, timezone

from src.models import (
    AiStatus,
    Case,
    CaseImage,
    CaseReport,
    ClinicalStatus,
    ConsultationType,
    DifferentialDiagnosis,
    DoctorProfile,
    DoctorReview,
    ImageType,
    Message,
    MessageRole,
    PatientProfile,
    QRToken,
    RefreshToken,
    ReportType,
    ReviewStatus,
    SnomedMapping,
    User,
    UserRole,
    VisualDescription,
    new_uuid,
)


# ================================================================== #
# Enum Tests
# ================================================================== #

class TestUserRoleEnum:
    """UserRole enum values must match exactly what Flutter expects."""

    def test_patient_value(self):
        assert UserRole.PATIENT == "patient"

    def test_doctor_value(self):
        assert UserRole.DOCTOR == "doctor"

    def test_admin_value(self):
        assert UserRole.ADMIN == "admin"

    def test_all_roles_are_strings(self):
        for role in UserRole:
            assert isinstance(role.value, str)


class TestConsultationTypeEnum:
    def test_new_complaint_value(self):
        assert ConsultationType.NEW_COMPLAINT == "new_complaint"

    def test_follow_up_value(self):
        assert ConsultationType.FOLLOW_UP == "follow_up"


class TestAiStatusEnum:
    """AI pipeline statuses — set by Celery workers only."""

    def test_pending(self):
        assert AiStatus.PENDING == "pending"

    def test_processing(self):
        assert AiStatus.PROCESSING == "processing"

    def test_completed(self):
        assert AiStatus.COMPLETED == "completed"

    def test_failed(self):
        assert AiStatus.FAILED == "failed"


class TestClinicalStatusEnum:
    """Clinical statuses — set by doctors, shown as badges in patient History."""

    def test_active(self):
        assert ClinicalStatus.ACTIVE == "active"

    def test_follow_up_available(self):
        # CRITICAL: exact string used as badge label
        assert ClinicalStatus.FOLLOW_UP_AVAILABLE == "follow_up_available"

    def test_monitoring(self):
        assert ClinicalStatus.MONITORING == "monitoring"

    def test_resolved(self):
        assert ClinicalStatus.RESOLVED == "resolved"

    def test_ai_status_and_clinical_status_are_separate_enums(self):
        """
        Guard against ISS-001: these must NEVER be merged into one field.
        Verify they are distinct enum types.
        """
        assert AiStatus is not ClinicalStatus
        assert set(AiStatus) != set(ClinicalStatus)


class TestImageTypeEnum:
    def test_skin(self):
        assert ImageType.SKIN == "skin"

    def test_prescription(self):
        assert ImageType.PRESCRIPTION == "prescription"

    def test_dermoscopy(self):
        assert ImageType.DERMOSCOPY == "dermoscopy"


class TestMessageRoleEnum:
    def test_ai(self):
        assert MessageRole.AI == "ai"

    def test_patient(self):
        assert MessageRole.PATIENT == "patient"

    def test_doctor(self):
        assert MessageRole.DOCTOR == "doctor"


class TestReviewStatusEnum:
    def test_pending(self):
        assert ReviewStatus.PENDING == "pending"

    def test_in_progress(self):
        assert ReviewStatus.IN_PROGRESS == "in_progress"

    def test_completed(self):
        assert ReviewStatus.COMPLETED == "completed"


class TestReportTypeEnum:
    def test_doctor(self):
        assert ReportType.DOCTOR == "doctor"

    def test_patient(self):
        assert ReportType.PATIENT == "patient"


# ================================================================== #
# Model Instantiation Tests
# ================================================================== #

class TestUserModel:
    """
    Test User model instantiation.

    NOTE ON DEFAULTS
    -----------------
    In SQLAlchemy 2.0 DeclarativeBase style, `mapped_column(default=X)`
    is a SQL-level default — it is applied at INSERT time (in the DB),
    NOT when the Python object is instantiated.

    `user.is_active` is None until the row is flushed to the DB.
    SQL-level defaults are tested in tests/integration/test_models_db.py.

    Unit tests here verify:
    - Values you SET at instantiation are returned correctly
    - Optional fields are None when not set
    - __repr__ works without crashing
    """

    def _make_user(self, **kwargs) -> User:
        defaults = dict(
            email="jane@example.com",
            full_name="Jane Doe",
            role=UserRole.PATIENT,
        )
        defaults.update(kwargs)
        return User(**defaults)

    def test_user_instantiation(self):
        user = self._make_user()
        assert user.email == "jane@example.com"
        assert user.full_name == "Jane Doe"
        assert user.role == UserRole.PATIENT

    def test_is_active_set_explicitly(self):
        """SQL default is True. When set explicitly, it holds the value."""
        user = self._make_user(is_active=True)
        assert user.is_active is True

    def test_is_verified_set_explicitly(self):
        user = self._make_user(is_verified=False)
        assert user.is_verified is False

    def test_password_hash_optional(self):
        user = self._make_user()
        assert user.password_hash is None

    def test_google_id_optional(self):
        user = self._make_user()
        assert user.google_id is None

    def test_doctor_role(self):
        user = self._make_user(role=UserRole.DOCTOR, email="dr@clinic.com")
        assert user.role == UserRole.DOCTOR

    def test_repr_does_not_crash(self):
        user = self._make_user()
        user.id = new_uuid()
        repr_str = repr(user)
        assert "User" in repr_str
        assert "jane@example.com" in repr_str


class TestPatientProfileModel:
    def test_instantiation(self):
        profile = PatientProfile(
            user_id=new_uuid(),
            patient_code="ABC-1234-Z",
        )
        assert profile.patient_code == "ABC-1234-Z"

    def test_optional_fields_default_none(self):
        profile = PatientProfile(user_id=new_uuid(), patient_code="XYZ-0000-A")
        assert profile.date_of_birth is None
        assert profile.gender is None
        assert profile.phone is None
        assert profile.avatar_url is None

    def test_repr(self):
        profile = PatientProfile(user_id="some-uuid", patient_code="ABC-1234-Z")
        assert "ABC-1234-Z" in repr(profile)


class TestDoctorProfileModel:
    def test_instantiation(self):
        profile = DoctorProfile(user_id=new_uuid(), notifications_enabled=True)
        assert profile.notifications_enabled is True

    def test_optional_fields(self):
        profile = DoctorProfile(user_id=new_uuid())
        assert profile.clinic_name is None
        assert profile.clinic_schedule is None
        assert profile.specialization is None

    def test_repr(self):
        profile = DoctorProfile(user_id="some-uuid", clinic_name="City Clinic")
        assert "City Clinic" in repr(profile)


class TestCaseModel:
    """
    SQL-level defaults (ai_status, clinical_status, consent_ai_analysis, etc.) are
    applied at INSERT time — not at Python instantiation. Those are verified
    in integration tests. Here we test values that are set explicitly.
    """

    def _make_case(self, **kwargs) -> Case:
        defaults = dict(
            patient_id=new_uuid(),
            ai_status=AiStatus.PENDING,
            clinical_status=ClinicalStatus.ACTIVE,
            consent_ai_analysis=False,
            is_for_self=True,
            has_visible_lesion=True,
            question_round=0,
            max_question_rounds=5,
            consultation_type=ConsultationType.NEW_COMPLAINT,
        )
        defaults.update(kwargs)
        return Case(**defaults)

    def test_instantiation(self):
        case = self._make_case()
        assert case.patient_id is not None

    def test_ai_status_set(self):
        case = self._make_case(ai_status=AiStatus.PENDING)
        assert case.ai_status == AiStatus.PENDING

    def test_clinical_status_set(self):
        case = self._make_case(clinical_status=ClinicalStatus.ACTIVE)
        assert case.clinical_status == ClinicalStatus.ACTIVE

    def test_consent_given_set(self):
        case = self._make_case(consent_ai_analysis=False)
        assert case.consent_ai_analysis is False

    def test_is_for_self_set(self):
        case = self._make_case(is_for_self=True)
        assert case.is_for_self is True

    def test_has_visible_lesion_set(self):
        case = self._make_case(has_visible_lesion=True)
        assert case.has_visible_lesion is True

    def test_question_round_set(self):
        case = self._make_case(question_round=0)
        assert case.question_round == 0

    def test_max_question_rounds_set(self):
        case = self._make_case(max_question_rounds=5)
        assert case.max_question_rounds == 5

    def test_consultation_type_set(self):
        case = self._make_case(consultation_type=ConsultationType.NEW_COMPLAINT)
        assert case.consultation_type == ConsultationType.NEW_COMPLAINT

    def test_doctor_id_optional(self):
        case = self._make_case()
        assert case.doctor_id is None

    def test_dependent_fields_optional(self):
        """Someone Else flow: all dependent_* fields are optional."""
        case = self._make_case()
        assert case.dependent_name is None
        assert case.dependent_relationship is None
        assert case.dependent_dob is None
        assert case.dependent_gender is None

    def test_someone_else_flow(self):
        """Simulate patient consulting for a family member."""
        case = self._make_case(
            is_for_self=False,
            dependent_name="Lee",
            dependent_relationship="Son",
            dependent_gender="Male",
            dependent_dob=date(2015, 3, 10),
        )
        assert case.is_for_self is False
        assert case.dependent_name == "Lee"
        assert case.dependent_relationship == "Son"

    def test_repr(self):
        case = self._make_case()
        case.id = new_uuid()
        r = repr(case)
        assert "Case" in r
        assert "PENDING" in r


class TestCaseImageModel:
    def test_instantiation(self):
        img = CaseImage(
            case_id=new_uuid(),
            gcs_path="cases/abc/images/img1.jpg",
            mime_type="image/jpeg",
            size_bytes=204800,
            image_type=ImageType.SKIN,
            upload_order=0,
        )
        assert img.image_type == ImageType.SKIN
        assert img.upload_order == 0

    def test_prescription_type(self):
        img = CaseImage(
            case_id=new_uuid(),
            gcs_path="cases/abc/images/rx.jpg",
            mime_type="image/jpeg",
            size_bytes=102400,
            image_type=ImageType.PRESCRIPTION,
        )
        assert img.image_type == ImageType.PRESCRIPTION

    def test_repr(self):
        img = CaseImage(
            case_id="some-case-id",
            gcs_path="path",
            mime_type="image/jpeg",
            size_bytes=100,
        )
        img.id = new_uuid()
        assert "CaseImage" in repr(img)


class TestVisualDescriptionModel:
    def test_instantiation(self):
        vd = VisualDescription(
            case_id=new_uuid(),
            description_json='{"type_of_lesion": "plaque"}',
            round_number=0,
        )
        assert vd.round_number == 0
        assert vd.overall_description is None

    def test_repr(self):
        vd = VisualDescription(case_id="c-id", description_json="{}")
        assert "VisualDescription" in repr(vd)


class TestDifferentialDiagnosisModel:
    def test_instantiation(self):
        dd = DifferentialDiagnosis(
            case_id=new_uuid(),
            diagnosis_json='{"most_probable_diagnosis": {"diagnosis": "Psoriasis"}}',
            most_probable_diagnosis="Psoriasis",
            round_number=0,
            is_final=False,
        )
        assert dd.round_number == 0
        assert dd.is_final is False

    def test_confidence_optional(self):
        dd = DifferentialDiagnosis(case_id=new_uuid(), diagnosis_json="{}")
        assert dd.confidence is None

    def test_repr(self):
        dd = DifferentialDiagnosis(
            case_id="c-id",
            diagnosis_json="{}",
            most_probable_diagnosis="Eczema",
        )
        assert "Eczema" in repr(dd)


class TestMessageModel:
    def test_instantiation(self):
        msg = Message(
            case_id=new_uuid(),
            role=MessageRole.AI,
            content="How long have you had this rash?",
            round_number=1,
        )
        assert msg.role == MessageRole.AI
        assert msg.question_index is None

    def test_patient_reply(self):
        msg = Message(
            case_id=new_uuid(),
            role=MessageRole.PATIENT,
            content="About 2 weeks",
            round_number=1,
            question_index=0,
        )
        assert msg.role == MessageRole.PATIENT

    def test_repr(self):
        msg = Message(
            case_id="c-id",
            role=MessageRole.AI,
            content="Q?",
            round_number=0,
        )
        msg.id = new_uuid()
        assert "Message" in repr(msg)


class TestDoctorReviewModel:
    def test_instantiation(self):
        review = DoctorReview(
            case_id=new_uuid(),
            doctor_id=new_uuid(),
            review_status=ReviewStatus.PENDING,
        )
        assert review.review_status == ReviewStatus.PENDING
        assert review.confirmed_diagnosis is None
        assert review.reviewed_at is None

    def test_repr(self):
        review = DoctorReview(
            case_id="c-id",
            doctor_id="d-id",
            confirmed_diagnosis="Psoriasis",
        )
        assert "Psoriasis" in repr(review)


class TestCaseReportModel:
    def test_instantiation(self):
        now = datetime.now(tz=timezone.utc)
        report = CaseReport(
            case_id=new_uuid(),
            gcs_path="cases/abc/reports/report_doctor.pdf",
            generated_at=now,
            report_type=ReportType.DOCTOR,
            download_count=0,
        )
        assert report.report_type == ReportType.DOCTOR
        assert report.download_count == 0

    def test_patient_report_type(self):
        report = CaseReport(
            case_id=new_uuid(),
            gcs_path="path.pdf",
            generated_at=datetime.now(tz=timezone.utc),
            report_type=ReportType.PATIENT,
        )
        assert report.report_type == ReportType.PATIENT

    def test_repr(self):
        report = CaseReport(
            case_id="c-id",
            gcs_path="path.pdf",
            generated_at=datetime.now(tz=timezone.utc),
        )
        assert "CaseReport" in repr(report)


class TestSnomedMappingModel:
    def test_instantiation(self):
        mapping = SnomedMapping(
            diagnosis_text="Psoriasis",
            snomed_code="9014002",
            snomed_term="Psoriasis (disorder)",
        )
        assert mapping.diagnosis_text == "Psoriasis"
        assert mapping.snomed_code == "9014002"

    def test_optional_snomed_fields(self):
        mapping = SnomedMapping(diagnosis_text="Unknown Condition")
        assert mapping.snomed_code is None
        assert mapping.snomed_term is None

    def test_repr(self):
        mapping = SnomedMapping(diagnosis_text="Eczema", snomed_code="43116000")
        assert "Eczema" in repr(mapping)


class TestQRTokenModel:
    def test_instantiation(self):
        now = datetime.now(tz=timezone.utc)
        token = QRToken(
            case_id=new_uuid(),
            token="abc123urlsafetoken",
            expires_at=now,
            used=False,
        )
        assert token.used is False
        assert token.used_at is None

    def test_repr(self):
        token = QRToken(
            case_id="c-id",
            token="tok",
            expires_at=datetime.now(tz=timezone.utc),
        )
        assert "QRToken" in repr(token)


class TestRefreshTokenModel:
    def test_instantiation(self):
        now = datetime.now(tz=timezone.utc)
        rt = RefreshToken(
            user_id=new_uuid(),
            token_hash="a" * 64,
            expires_at=now,
            revoked=False,
        )
        assert rt.revoked is False
        assert rt.revoked_at is None

    def test_repr(self):
        rt = RefreshToken(
            user_id="u-id",
            token_hash="a" * 64,
            expires_at=datetime.now(tz=timezone.utc),
        )
        assert "RefreshToken" in repr(rt)


# ================================================================== #
# UUID Generation
# ================================================================== #

class TestNewUuid:
    def test_produces_valid_uuid(self):
        result = new_uuid()
        # Should not raise
        parsed = uuid.UUID(result)
        assert parsed.version == 4

    def test_produces_unique_values(self):
        ids = {new_uuid() for _ in range(100)}
        assert len(ids) == 100

    def test_returns_string(self):
        assert isinstance(new_uuid(), str)

    def test_correct_length(self):
        # UUID4 string format: 8-4-4-4-12 = 36 chars with hyphens
        assert len(new_uuid()) == 36
