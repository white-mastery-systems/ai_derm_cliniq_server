from pydantic import BaseModel


class StudyResponse(BaseModel):
    id: str
    title: str
    description: str | None
    is_active: bool
    submission_count: int


class NextSpecimenCodeResponse(BaseModel):
    specimen_code: str


class FeatureResponse(BaseModel):
    key: str
    label: str
    reference_image_url: str | None


class SubmitStudyResponse(BaseModel):
    submission_id: str
    specimen_code: str | None
    report_url: str | None
    pdf_generated: bool
