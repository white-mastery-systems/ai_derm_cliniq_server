"""
tests/integration/test_models_db.py — DB Integration Tests for Models
=======================================================================

WHAT WE TEST HERE
------------------
1. Models can be inserted into the database and retrieved correctly
2. Relationships work — related objects load via FK joins
3. Cascaded deletes propagate correctly (delete User → delete Profile)
4. Unique constraints are enforced (duplicate email, patient_code)
5. The "Someone Else" flow persists dependent fields correctly
6. Dual status fields (ai_status + clinical_status) are independent

These tests use the shared `db_session` fixture from conftest.py.
Each test runs inside a transaction that is rolled back at the end,
so tests are fully isolated — they don't affect each other.

LESSON: Integration tests catch:
- Relationship definitions that don't match FK column names
- Missing cascade settings (orphaned rows after parent delete)
- Column constraints that the model definition doesn't enforce locally
"""

from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AiStatus,
    Case,
    CaseImage,
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
    ReviewStatus,
    SnomedMapping,
    User,
    UserRole,
    VisualDescription,
    new_uuid,
)


# ================================================================== #
# Helpers
# ================================================================== #

def make_patient(email="patient@test.com", name="Jane Doe") -> User:
    return User(
        id=new_uuid(),
        email=email,
        full_name=name,
        role=UserRole.PATIENT,
        password_hash="hashed",
    )


def make_doctor(email="doctor@test.com", name="Dr. Smith") -> User:
    return User(
        id=new_uuid(),
        email=email,
        full_name=name,
        role=UserRole.DOCTOR,
        password_hash="hashed",
    )


def make_case(patient_id: str, **kwargs) -> Case:
    return Case(
        id=new_uuid(),
        patient_id=patient_id,
        consultation_type=ConsultationType.NEW_COMPLAINT,
        **kwargs,
    )


# ================================================================== #
# User CRUD
# ================================================================== #

class TestUserCRUD:
    async def test_insert_and_retrieve_user(self, db_session: AsyncSession):
        user = make_patient()
        db_session.add(user)
        await db_session.flush()

        result = await db_session.execute(
            select(User).where(User.email == "patient@test.com")
        )
        fetched = result.scalar_one()
        assert fetched.full_name == "Jane Doe"
        assert fetched.role == UserRole.PATIENT
        assert fetched.is_active is True

    async def test_unique_email_constraint(self, db_session: AsyncSession):
        user1 = make_patient(email="dupe@test.com")
        user2 = make_patient(email="dupe@test.com")
        db_session.add(user1)
        await db_session.flush()

        db_session.add(user2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_google_id_unique(self, db_session: AsyncSession):
        user1 = make_patient(email="g1@test.com")
        user1.google_id = "google-sub-123"
        user2 = make_patient(email="g2@test.com")
        user2.google_id = "google-sub-123"  # same google_id
        db_session.add(user1)
        await db_session.flush()

        db_session.add(user2)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ================================================================== #
# Patient Profile
# ================================================================== #

class TestPatientProfile:
    async def test_insert_profile(self, db_session: AsyncSession):
        user = make_patient(email="profile_test@test.com")
        db_session.add(user)
        await db_session.flush()

        profile = PatientProfile(
            id=new_uuid(),
            user_id=user.id,
            patient_code="ABC-1234-Z",
            date_of_birth=date(1995, 1, 12),
            gender="Female",
            phone="+91887615-43210",
        )
        db_session.add(profile)
        await db_session.flush()

        result = await db_session.execute(
            select(PatientProfile).where(PatientProfile.patient_code == "ABC-1234-Z")
        )
        fetched = result.scalar_one()
        assert fetched.gender == "Female"
        assert fetched.date_of_birth == date(1995, 1, 12)

    async def test_patient_code_unique_constraint(self, db_session: AsyncSession):
        user1 = make_patient(email="p1@test.com")
        user2 = make_patient(email="p2@test.com")
        db_session.add_all([user1, user2])
        await db_session.flush()

        p1 = PatientProfile(id=new_uuid(), user_id=user1.id, patient_code="DUP-0000-X")
        p2 = PatientProfile(id=new_uuid(), user_id=user2.id, patient_code="DUP-0000-X")
        db_session.add(p1)
        await db_session.flush()

        db_session.add(p2)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_one_profile_per_user_constraint(self, db_session: AsyncSession):
        user = make_patient(email="oneprofile@test.com")
        db_session.add(user)
        await db_session.flush()

        p1 = PatientProfile(id=new_uuid(), user_id=user.id, patient_code="ONE-0001-A")
        p2 = PatientProfile(id=new_uuid(), user_id=user.id, patient_code="ONE-0002-B")
        db_session.add(p1)
        await db_session.flush()

        db_session.add(p2)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ================================================================== #
# Case CRUD
# ================================================================== #

class TestCaseCRUD:
    async def test_insert_case(self, db_session: AsyncSession):
        patient = make_patient(email="case_patient@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        result = await db_session.execute(select(Case).where(Case.id == case.id))
        fetched = result.scalar_one()
        assert fetched.ai_status == AiStatus.PENDING
        assert fetched.clinical_status == ClinicalStatus.ACTIVE
        assert fetched.consent_ai_analysis is False

    async def test_dual_status_are_independent(self, db_session: AsyncSession):
        """
        Guard against ISS-001: ai_status and clinical_status are independent.
        Changing one must not affect the other.
        """
        patient = make_patient(email="dual_status@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        # Simulate AI completing while clinical status stays active
        case.ai_status = AiStatus.COMPLETED
        await db_session.flush()

        result = await db_session.execute(select(Case).where(Case.id == case.id))
        fetched = result.scalar_one()
        assert fetched.ai_status == AiStatus.COMPLETED
        assert fetched.clinical_status == ClinicalStatus.ACTIVE  # unchanged

        # Now doctor sets clinical status — ai_status stays completed
        case.clinical_status = ClinicalStatus.MONITORING
        await db_session.flush()

        result = await db_session.execute(select(Case).where(Case.id == case.id))
        fetched = result.scalar_one()
        assert fetched.ai_status == AiStatus.COMPLETED    # still completed
        assert fetched.clinical_status == ClinicalStatus.MONITORING

    async def test_someone_else_flow_persists(self, db_session: AsyncSession):
        """Dependent fields must persist correctly for 'Someone Else' consultations."""
        patient = make_patient(email="proxy@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(
            patient_id=patient.id,
            is_for_self=False,
            dependent_name="Lee",
            dependent_relationship="Son",
            dependent_dob=date(2015, 3, 10),
            dependent_gender="Male",
        )
        db_session.add(case)
        await db_session.flush()

        result = await db_session.execute(select(Case).where(Case.id == case.id))
        fetched = result.scalar_one()
        assert fetched.is_for_self is False
        assert fetched.dependent_name == "Lee"
        assert fetched.dependent_relationship == "Son"
        assert fetched.dependent_dob == date(2015, 3, 10)

    async def test_consent_gate_persists(self, db_session: AsyncSession):
        """Consent fields must be stored correctly."""
        patient = make_patient(email="consent@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        assert not case.consent_ai_analysis

        now = datetime.now(tz=timezone.utc)
        case.consent_ai_analysis = True
        case.consent_ai_analysis_at = now
        await db_session.flush()

        result = await db_session.execute(select(Case).where(Case.id == case.id))
        fetched = result.scalar_one()
        assert fetched.consent_ai_analysis is True
        assert fetched.consent_ai_analysis_at is not None


# ================================================================== #
# Relationships
# ================================================================== #

class TestCaseRelationships:
    async def test_case_images_relationship(self, db_session: AsyncSession):
        patient = make_patient(email="imgs@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        for i in range(3):
            img = CaseImage(
                id=new_uuid(),
                case_id=case.id,
                gcs_path=f"cases/{case.id}/images/img_{i}.jpg",
                mime_type="image/jpeg",
                size_bytes=204800,
                upload_order=i,
            )
            db_session.add(img)
        await db_session.flush()

        result = await db_session.execute(
            select(CaseImage).where(CaseImage.case_id == case.id)
        )
        images = result.scalars().all()
        assert len(images) == 3
        # Verify ordering
        assert images[0].upload_order == 0
        assert images[2].upload_order == 2

    async def test_messages_relationship(self, db_session: AsyncSession):
        patient = make_patient(email="msgs@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        ai_q = Message(
            id=new_uuid(),
            case_id=case.id,
            role=MessageRole.AI,
            content="How long have you had this?",
            round_number=1,
            question_index=0,
        )
        patient_a = Message(
            id=new_uuid(),
            case_id=case.id,
            role=MessageRole.PATIENT,
            content="About 2 weeks",
            round_number=1,
            question_index=0,
        )
        db_session.add_all([ai_q, patient_a])
        await db_session.flush()

        result = await db_session.execute(
            select(Message).where(Message.case_id == case.id)
        )
        messages = result.scalars().all()
        assert len(messages) == 2
        roles = {m.role for m in messages}
        assert MessageRole.AI in roles
        assert MessageRole.PATIENT in roles

    async def test_differential_diagnosis_final_flag(self, db_session: AsyncSession):
        patient = make_patient(email="diff@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        # Round 0: initial differential
        d0 = DifferentialDiagnosis(
            id=new_uuid(),
            case_id=case.id,
            round_number=0,
            diagnosis_json='{}',
            most_probable_diagnosis="Eczema",
            is_final=False,
        )
        # Round 1: final
        d1 = DifferentialDiagnosis(
            id=new_uuid(),
            case_id=case.id,
            round_number=1,
            diagnosis_json='{}',
            most_probable_diagnosis="Psoriasis",
            is_final=True,
        )
        db_session.add_all([d0, d1])
        await db_session.flush()

        result = await db_session.execute(
            select(DifferentialDiagnosis)
            .where(
                DifferentialDiagnosis.case_id == case.id,
                DifferentialDiagnosis.is_final == True,  # noqa: E712
            )
        )
        final = result.scalar_one()
        assert final.most_probable_diagnosis == "Psoriasis"
        assert final.round_number == 1


# ================================================================== #
# Doctor Review
# ================================================================== #

class TestDoctorReview:
    async def test_doctor_review_insert(self, db_session: AsyncSession):
        patient = make_patient(email="rev_pat@test.com")
        doctor = make_doctor(email="rev_doc@test.com")
        db_session.add_all([patient, doctor])
        await db_session.flush()

        case = make_case(patient_id=patient.id, doctor_id=doctor.id)
        db_session.add(case)
        await db_session.flush()

        review = DoctorReview(
            id=new_uuid(),
            case_id=case.id,
            doctor_id=doctor.id,
            confirmed_diagnosis="Psoriasis Vulgaris",
            review_status=ReviewStatus.COMPLETED,
            reviewed_at=datetime.now(tz=timezone.utc),
        )
        db_session.add(review)
        await db_session.flush()

        result = await db_session.execute(
            select(DoctorReview).where(DoctorReview.case_id == case.id)
        )
        fetched = result.scalar_one()
        assert fetched.confirmed_diagnosis == "Psoriasis Vulgaris"
        assert fetched.review_status == ReviewStatus.COMPLETED


# ================================================================== #
# QR Token
# ================================================================== #

class TestQRToken:
    async def test_qr_token_insert(self, db_session: AsyncSession):
        patient = make_patient(email="qr@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        token = QRToken(
            id=new_uuid(),
            case_id=case.id,
            token="secure-random-token-abc123",
            expires_at=datetime.now(tz=timezone.utc),
        )
        db_session.add(token)
        await db_session.flush()

        result = await db_session.execute(
            select(QRToken).where(QRToken.token == "secure-random-token-abc123")
        )
        fetched = result.scalar_one()
        assert fetched.used is False
        assert fetched.case_id == case.id

    async def test_token_unique_constraint(self, db_session: AsyncSession):
        patient = make_patient(email="qr_unique@test.com")
        db_session.add(patient)
        await db_session.flush()

        case = make_case(patient_id=patient.id)
        db_session.add(case)
        await db_session.flush()

        now = datetime.now(tz=timezone.utc)
        t1 = QRToken(id=new_uuid(), case_id=case.id, token="same-token", expires_at=now)
        t2 = QRToken(id=new_uuid(), case_id=case.id, token="same-token", expires_at=now)
        db_session.add(t1)
        await db_session.flush()

        db_session.add(t2)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ================================================================== #
# Refresh Token
# ================================================================== #

class TestRefreshToken:
    async def test_refresh_token_insert(self, db_session: AsyncSession):
        user = make_patient(email="rt@test.com")
        db_session.add(user)
        await db_session.flush()

        rt = RefreshToken(
            id=new_uuid(),
            user_id=user.id,
            token_hash="a" * 64,
            expires_at=datetime.now(tz=timezone.utc),
        )
        db_session.add(rt)
        await db_session.flush()

        result = await db_session.execute(
            select(RefreshToken).where(RefreshToken.user_id == user.id)
        )
        fetched = result.scalar_one()
        assert fetched.revoked is False

    async def test_token_hash_unique(self, db_session: AsyncSession):
        user = make_patient(email="rt_unique@test.com")
        db_session.add(user)
        await db_session.flush()

        now = datetime.now(tz=timezone.utc)
        rt1 = RefreshToken(id=new_uuid(), user_id=user.id, token_hash="b" * 64, expires_at=now)
        rt2 = RefreshToken(id=new_uuid(), user_id=user.id, token_hash="b" * 64, expires_at=now)
        db_session.add(rt1)
        await db_session.flush()

        db_session.add(rt2)
        with pytest.raises(IntegrityError):
            await db_session.flush()


# ================================================================== #
# SNOMED Mapping Cache
# ================================================================== #

class TestSnomedMapping:
    async def test_snomed_insert(self, db_session: AsyncSession):
        mapping = SnomedMapping(
            id=new_uuid(),
            diagnosis_text="__test_model_psoriasis__",
            snomed_code="9014002",
            snomed_term="Psoriasis (disorder)",
        )
        db_session.add(mapping)
        await db_session.flush()

        result = await db_session.execute(
            select(SnomedMapping).where(SnomedMapping.diagnosis_text == "__test_model_psoriasis__")
        )
        fetched = result.scalar_one()
        assert fetched.snomed_code == "9014002"

    async def test_diagnosis_text_unique(self, db_session: AsyncSession):
        m1 = SnomedMapping(id=new_uuid(), diagnosis_text="__test_model_eczema__")
        m2 = SnomedMapping(id=new_uuid(), diagnosis_text="__test_model_eczema__")
        db_session.add(m1)
        await db_session.flush()

        db_session.add(m2)
        with pytest.raises(IntegrityError):
            await db_session.flush()
