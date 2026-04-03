# AiDerm Cliniq — FastAPI Backend: Final Architecture Plan

> **Migration target:** `D:\@White Mastery Systems\Derm AI\ai_derm_cliniq_server`
> **Source:** `D:\@White Mastery Systems\Derm AI\Dermchatbot2` (Streamlit app)
> **Date:** 2026-03-31
> **Status:** In Development

---

## 1. Overview

AiDerm Cliniq is an AI-powered dermatology diagnostic platform. This document defines the complete
architecture of the production FastAPI backend that replaces the old Streamlit monolith.

**Flutter mobile app** → calls this FastAPI server → PostgreSQL + GCS + Celery + Redis

---

## 2. Tech Stack

| Concern | Technology |
|---|---|
| Framework | FastAPI (Python 3.11+) |
| Database | PostgreSQL via SQLAlchemy (async) + Alembic |
| Auth | JWT (python-jose) + bcrypt (passlib) + Google OAuth |
| File Storage | Google Cloud Storage (GCS) |
| AI (LLM) | Google Gemini Flash, OpenAI GPT-4o, Perplexity, DeepSeek |
| Task Queue | Celery + Redis |
| Real-time Chat | WebSockets (patient ↔ doctor) |
| AI Streaming | Server-Sent Events (SSE) |
| Validation | Pydantic v2 |
| Rate Limiting | slowapi |
| PDF Generation | WeasyPrint |
| Logging | structlog (structured JSON logs) |
| Testing | pytest + httpx AsyncClient |
| Containerization | Docker + docker-compose |

---

## 3. Project Structure

```
ai_derm_cliniq_server/
│
├── src/
│   ├── main.py                          # FastAPI app factory + lifespan
│   ├── api.py                           # Central router registration
│   ├── config.py                        # Pydantic BaseSettings (all env vars)
│   ├── logger.py                        # Structured JSON logging (structlog)
│   ├── exceptions.py                    # Custom exceptions + global error handlers
│   ├── rate_limiting.py                 # slowapi rate limiter setup
│   │
│   ├── core/                            # Infrastructure: security + DI
│   │   ├── security.py                  # JWT create/verify, bcrypt hashing
│   │   └── dependencies.py             # get_db, get_current_user, require_role
│   │
│   ├── database/
│   │   └── core.py                      # Async SQLAlchemy engine + session factory
│   │
│   ├── models/                          # SQLAlchemy ORM models (PostgreSQL)
│   │   ├── base.py                      # DeclarativeBase + UUID + Timestamp mixins
│   │   ├── user.py                      # User, DoctorProfile, PatientProfile
│   │   ├── case.py                      # Case (lifecycle, status, consent)
│   │   ├── case_image.py               # CaseImage (GCS path, image type)
│   │   ├── conversation.py             # Message (role, content, metadata)
│   │   ├── visual_description.py       # VisualDescription (AI JSON output)
│   │   ├── differential_diagnosis.py   # DifferentialDiagnosis (AI JSON output)
│   │   ├── doctor_review.py            # DoctorReview + Q&A history
│   │   ├── case_report.py              # CaseReport (final diagnosis + treatment)
│   │   ├── snomed_mapping.py           # SNOMED CT term mapping
│   │   ├── qr_token.py                 # QR tokens with expiry
│   │   └── refresh_token.py            # JWT refresh token store
│   │
│   ├── schemas/                         # Pydantic v2 request/response schemas
│   │   ├── common.py                    # PaginatedResponse, ErrorResponse, TaskStatus
│   │   ├── auth.py                      # Register, Login, Token, Refresh
│   │   ├── user.py                      # UserOut, ProfileUpdate
│   │   ├── case.py                      # CaseCreate, CaseOut, CaseUpdate
│   │   ├── image.py                     # ImageUpload, ImageOut, SignedUrl
│   │   ├── conversation.py             # MessageCreate, MessageOut
│   │   ├── ai_analysis.py              # VisualDescriptionOut, DifferentialOut
│   │   ├── doctor_review.py            # ReviewCreate, ReviewOut, DoctorQuestionOut
│   │   ├── case_report.py              # ReportCreate, ReportOut, PDFDownload
│   │   ├── qr.py                        # QRGenerate, QRScanResult
│   │   └── stats.py                     # DoctorStats, AdminStats
│   │
│   ├── auth/                            # Authentication module
│   │   ├── model.py                     # (re-exports from models/)
│   │   ├── service.py                   # register, login, refresh, google_oauth
│   │   └── controller.py               # /api/v1/auth/* routes
│   │
│   ├── users/                           # Users module
│   │   ├── model.py
│   │   ├── service.py                   # get_profile, update_profile
│   │   └── controller.py               # /api/v1/users/* routes
│   │
│   ├── cases/                           # Cases module
│   │   ├── model.py
│   │   ├── service.py                   # case lifecycle management
│   │   └── controller.py               # /api/v1/cases/* routes
│   │
│   ├── images/                          # Images module
│   │   ├── model.py
│   │   ├── service.py                   # upload, validate, signed URLs
│   │   └── controller.py               # /api/v1/cases/{id}/images/* routes
│   │
│   ├── conversations/                   # Chat module
│   │   ├── model.py
│   │   ├── service.py                   # message persistence
│   │   └── controller.py               # /api/v1/cases/{id}/chat/* + WS
│   │
│   ├── ai/                              # AI/ML service layer
│   │   ├── llm_client.py               # Multi-provider: Gemini, OpenAI, Perplexity, DeepSeek
│   │   ├── image_analyzer.py           # Dermatological image → visual_description JSON
│   │   ├── patient_ai_service.py       # Patient consultation flow (from main4.py)
│   │   ├── doctor_ai_service.py        # Doctor review flow (from doctor_processor.py)
│   │   ├── snomed_service.py           # Free-text → SNOMED CT mapping
│   │   ├── visual_description_editor.py # JSON patch with cascade (from tools_derma.py)
│   │   ├── context_service.py          # Aggregate past visits into context
│   │   ├── prompt_templates.py         # All LLM prompt templates
│   │   └── controller.py               # /api/v1/cases/{id}/ai/* routes (SSE)
│   │
│   ├── doctor_review/                   # Doctor review module
│   │   ├── model.py
│   │   ├── service.py
│   │   └── controller.py               # /api/v1/cases/{id}/review/* routes
│   │
│   ├── reports/                         # Report + PDF module
│   │   ├── model.py
│   │   ├── service.py
│   │   └── controller.py               # /api/v1/cases/{id}/report/* routes
│   │
│   ├── qr/                              # QR code module
│   │   ├── service.py
│   │   └── controller.py               # /api/v1/qr/* routes
│   │
│   ├── admin/                           # Admin module
│   │   ├── service.py
│   │   └── controller.py               # /api/v1/admin/* routes
│   │
│   ├── workers/                         # Celery async task workers
│   │   ├── celery_app.py               # Celery factory + Redis config
│   │   ├── beat_schedule.py            # Periodic cleanup tasks
│   │   └── tasks/
│   │       ├── image_tasks.py          # analyze_images_task
│   │       ├── ai_tasks.py             # differential, questions, summary tasks
│   │       ├── doctor_tasks.py         # doctor_questions, snomed_mapping tasks
│   │       ├── report_tasks.py         # pdf_generation task
│   │       └── notification_tasks.py  # email + push notification tasks
│   │
│   ├── storage/
│   │   ├── base.py                      # Abstract StorageBackend interface
│   │   └── gcs.py                       # Google Cloud Storage (from gcputils.py)
│   │
│   └── utils/
│       ├── pdf.py                       # PDF generation (WeasyPrint)
│       ├── qrcode_gen.py               # QR code PNG generation
│       ├── email_templates.py          # Jinja2 HTML email templates
│       └── pagination.py               # Cursor + offset pagination
│
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│
├── tests/
│   ├── conftest.py                      # Fixtures: test DB, auth tokens, mock GCS
│   ├── unit/
│   │   ├── test_config.py
│   │   ├── test_security.py
│   │   ├── test_auth_service.py
│   │   ├── test_case_service.py
│   │   ├── test_image_service.py
│   │   ├── test_ai_service.py
│   │   └── test_snomed_service.py
│   └── integration/
│       ├── test_auth_endpoints.py
│       ├── test_cases_endpoints.py
│       ├── test_images_endpoints.py
│       ├── test_ai_endpoints.py
│       └── test_websocket.py
│
├── scripts/
│   ├── migrate_gcs_to_postgres.py
│   ├── backfill_snomed.py
│   └── seed_db.py
│
├── docker/
│   ├── Dockerfile
│   ├── Dockerfile.worker
│   └── docker-compose.yml
│
├── Final Flow.md                        # This file
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── alembic.ini
└── README.md
```

---

## 4. All API Endpoints

### Auth — `/api/v1/auth`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/register/doctor` | Public | Register new doctor (email + password) |
| POST | `/login/doctor` | Public | Doctor login → JWT access + refresh token |
| POST | `/session/patient` | Public | Create anonymous patient session → short-lived JWT |
| POST | `/google` | Public | Google OAuth code exchange → JWT |
| POST | `/refresh` | Auth | Rotate refresh token |
| POST | `/logout` | Auth | Revoke refresh token |

### Users — `/api/v1/users`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| GET | `/me` | Any | Get own profile |
| PATCH | `/me` | Any | Update own profile |
| DELETE | `/me` | Any | Soft-delete account |

### Patients — `/api/v1/patients`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| GET | `/me/cases` | Patient | Paginated case history |
| GET | `/me/cases/{case_id}` | Patient | Single case detail |

### Doctors — `/api/v1/doctors`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| GET | `/me` | Doctor | Doctor profile |
| PATCH | `/me` | Doctor | Update profile |
| GET | `/me/stats` | Doctor | today_cases, pending, completed, total |
| GET | `/me/cases` | Doctor | Paginated case list with status filter |

### Cases — `/api/v1/cases`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/` | Patient | Create new case |
| GET | `/` | Doctor/Patient | List cases (role-filtered) |
| GET | `/{case_id}` | Any | Full case detail |
| PATCH | `/{case_id}` | Any | Update status, consent, patient_for |
| DELETE | `/{case_id}` | Any | Soft delete |
| PATCH | `/{case_id}/assign` | Doctor | Doctor claims case after QR scan |

### Images — `/api/v1/cases/{case_id}/images`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/upload` | Patient | Upload 1–10 skin images (multipart) |
| GET | `/` | Any | List images (signed URLs) |
| GET | `/{image_id}` | Any | Single image signed URL |
| DELETE | `/{image_id}` | Patient | Remove image |

### AI Analysis — `/api/v1/cases/{case_id}/ai`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/analyze` | Patient | Trigger Celery image analysis → returns task_id |
| GET | `/stream` | Patient | **SSE**: stream AI question/answer tokens |
| GET | `/suggestions` | Any | Get visual description + differential |
| POST | `/next-question` | Patient | Advance diagnostic conversation loop |
| POST | `/finalize` | Patient | Generate case summary |

### Conversations — `/api/v1/cases/{case_id}/chat`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/` | Patient | Save patient message |
| GET | `/` | Any | Full conversation history |
| **WS** | `/ws/cases/{case_id}/chat` | Any | Real-time patient ↔ doctor |

### Doctor Review — `/api/v1/cases/{case_id}/review`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/` | Doctor | Start doctor review session |
| GET | `/` | Doctor | Get review state |
| PATCH | `/visual-description` | Doctor | JSON patch edit on AI description |
| POST | `/answer` | Doctor | Doctor answers AI-generated doubt |
| POST | `/finalize` | Doctor | Confirm findings, generate SNOMED |

### Reports — `/api/v1/cases/{case_id}/report`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/` | Doctor | Create final case report |
| GET | `/` | Any | Get report |
| POST | `/generate-pdf` | Doctor | Trigger Celery PDF generation |
| GET | `/download` | Any | Signed GCS URL to download PDF |

### QR — `/api/v1/qr`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| POST | `/cases/{case_id}/generate` | Patient | Generate QR token |
| GET | `/cases/{case_id}` | Patient | Get current QR + expiry |
| POST | `/cases/{case_id}/regenerate` | Patient | Revoke + issue new QR |
| GET | `/{qr_token}` | **Public** | Doctor scans → resolves to case |

### Admin — `/api/v1/admin`
| Method | Endpoint | Role | Description |
|--------|----------|------|-------------|
| GET | `/users` | Admin | List all users |
| PATCH | `/users/{user_id}/role` | Admin | Change user role |
| DELETE | `/users/{user_id}` | Admin | Hard delete user |
| GET | `/stats` | Admin | Platform-wide stats |

---

## 5. Database Schema (PostgreSQL)

### users
```
id            UUID PK
email         TEXT UNIQUE NOT NULL
password_hash TEXT NULLABLE (null for Google-only accounts)
role          ENUM(patient, doctor, admin)
full_name     TEXT NOT NULL
phone         TEXT
is_active     BOOL DEFAULT true
google_sub    TEXT UNIQUE NULLABLE
created_at    TIMESTAMPTZ
updated_at    TIMESTAMPTZ
```

### doctor_profiles
```
id               UUID PK
user_id          UUID FK → users
specialization   TEXT
clinic_name      TEXT
license_number   TEXT
bio              TEXT
```

### patient_profiles
```
id             UUID PK
user_id        UUID FK → users
date_of_birth  DATE
sex            ENUM(male, female, other)
```

### cases
```
id                  UUID PK
patient_id          UUID FK → users
doctor_id           UUID FK → users NULLABLE
consultation_type   ENUM(new_complaint, follow_up)
patient_for         ENUM(self, dependent)
status              ENUM(draft, pending_review, in_review, completed, cancelled)
consent_given       BOOL DEFAULT false
red_flag_triggered  BOOL DEFAULT false
is_deleted          BOOL DEFAULT false
created_at          TIMESTAMPTZ
updated_at          TIMESTAMPTZ
```

### case_images
```
id           UUID PK
case_id      UUID FK → cases
gcs_path     TEXT NOT NULL
image_type   ENUM(clinical, dermoscopy, pathology, other)
uploaded_at  TIMESTAMPTZ
```

### messages (conversations)
```
id          UUID PK
case_id     UUID FK → cases
role        ENUM(patient, doctor, ai)
content     TEXT NOT NULL
metadata    JSONB
created_at  TIMESTAMPTZ
```

### visual_descriptions
```
id           UUID PK
case_id      UUID FK → cases UNIQUE
clinical     JSONB
dermoscopy   JSONB
pathology    JSONB
raw_response JSONB
created_at   TIMESTAMPTZ
updated_at   TIMESTAMPTZ
```

### differential_diagnoses
```
id          UUID PK
case_id     UUID FK → cases
diagnoses   JSONB   (array of {name, likelihood, features})
confidence  TEXT    (high/medium/low)
version     INT DEFAULT 1
created_at  TIMESTAMPTZ
```

### doctor_reviews
```
id                    UUID PK
case_id               UUID FK → cases UNIQUE
doctor_id             UUID FK → users
technical_complaints  JSONB
qa_history            JSONB
status                ENUM(pending, in_progress, completed)
created_at            TIMESTAMPTZ
updated_at            TIMESTAMPTZ
```

### case_reports
```
id                   UUID PK
case_id              UUID FK → cases UNIQUE
doctor_id            UUID FK → users
confirmed_diagnosis  TEXT
clinical_notes       TEXT
treatment_plan       TEXT
severity             ENUM(mild, moderate, severe)
pdf_gcs_path         TEXT
generated_at         TIMESTAMPTZ
```

### snomed_mappings
```
id               UUID PK
case_report_id   UUID FK → case_reports
snomed_term      TEXT
snomed_code      TEXT
match_confidence FLOAT
created_at       TIMESTAMPTZ
```

### qr_tokens
```
id          UUID PK
case_id     UUID FK → cases
token       TEXT UNIQUE NOT NULL
expires_at  TIMESTAMPTZ
used        BOOL DEFAULT false
created_at  TIMESTAMPTZ
```

### refresh_tokens
```
id          UUID PK
user_id     UUID FK → users
token_hash  TEXT UNIQUE NOT NULL
expires_at  TIMESTAMPTZ
revoked     BOOL DEFAULT false
created_at  TIMESTAMPTZ
```

---

## 6. Celery Task Map

| Task | Trigger | What it does |
|------|---------|-------------|
| `analyze_images_task` | POST /ai/analyze | Sends images to Gemini Vision → stores visual_description |
| `generate_differential_task` | After image analysis | Generates initial differential diagnosis |
| `generate_questions_task` | After differential | Generates first patient questions |
| `update_differential_task` | After each patient answer | Refines differential with new info |
| `generate_summary_task` | POST /ai/finalize | Produces case summary text |
| `generate_doctor_questions_task` | POST /review | Generates AI doubts for doctor |
| `map_snomed_task` | POST /review/finalize | Maps free-text diagnosis to SNOMED CT |
| `generate_pdf_task` | POST /report/generate-pdf | Renders PDF, uploads to GCS |
| `send_email_task` | After case finalized | Sends case summary email to patient |

---

## 7. Old Code → New Service Mapping

| Old File (Streamlit) | New Location (FastAPI) |
|---|---|
| `APIcaller.py` | `src/ai/llm_client.py` |
| `ImageAnalyser4.py` + `ImageAnalyser_doctor.py` | `src/ai/image_analyzer.py` |
| `main4.py` (patient LLM engine) | `src/ai/patient_ai_service.py` |
| `doctor_processor.py` | `src/ai/doctor_ai_service.py` |
| `snomed_prompt_chain_test.py` | `src/ai/snomed_service.py` |
| `Custom_prompt_template.py` | `src/ai/prompt_templates.py` |
| `tools_derma.py` (JSON patch) | `src/ai/visual_description_editor.py` |
| `context_generator.py` | `src/ai/context_service.py` |
| `memory.py` | Redis-backed in `src/conversations/service.py` |
| `gcputils.py` | `src/storage/gcs.py` |
| `email_utils.py` | `src/workers/tasks/notification_tasks.py` + `src/utils/email_templates.py` |
| `async_utility.py` (threading) | Celery tasks in `src/workers/tasks/` |
| QR code logic | `src/qr/service.py` + `src/utils/qrcode_gen.py` |
| `acne_scar_rct/` | Separate `acne_rct_service/` microservice |
| `deprecated/` | **Excluded** |
| `hair_diameter_processing.py` + UNet | **Excluded** (not confirmed production) |

---

## 8. Build Order (Layer by Layer)

| Layer | Modules | Status |
|-------|---------|--------|
| **1** | Foundation: config, database, logger, exceptions, main.py | 🔄 In Progress |
| **2** | Database Models + Alembic migrations | ⏳ Pending |
| **3** | Auth: JWT, bcrypt, Google OAuth, refresh tokens | ⏳ Pending |
| **4** | Cases + Images routers and services | ⏳ Pending |
| **5** | AI services: LLM client, image analyzer, patient/doctor flows | ⏳ Pending |
| **6** | Celery workers for async AI tasks | ⏳ Pending |
| **7** | WebSocket + SSE real-time features | ⏳ Pending |
| **8** | Reports, PDF generation, QR codes | ⏳ Pending |
| **9** | RCT microservice | ⏳ Pending |

---

## 9. Security Rules

- All endpoints except `/auth/*` and `/qr/{token}` require `Authorization: Bearer <token>`
- Patients can only access their own cases
- Doctors can only access cases assigned to them (or unassigned via QR)
- Images served via signed GCS URLs (30-min expiry) — never public
- QR tokens expire 24 hours after generation
- Refresh tokens stored as bcrypt hashes in DB
- Rate limit: 60 req/min per IP (slowapi)
- File uploads: JPEG/PNG/HEIC only, max 10MB per image, max 10 per case
- All passwords hashed with bcrypt (cost factor 12)
- Google OAuth `client_secret` only on server, never in client

---

## 10. Environment Variables

```env
# App
APP_ENV=development
SECRET_KEY=your-super-secret-jwt-key-change-this
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=30

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/aiderm_cliniq

# Redis
REDIS_URL=redis://localhost:6379/0

# Google Cloud
GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json
GCS_BUCKET_NAME=aiderm-cliniq-storage

# Google OAuth
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret

# AI Providers
GEMINI_API_KEY=AIza...
OPENAI_API_KEY=sk-...
PERPLEXITY_API_KEY=pplx-...
DEEPSEEK_API_KEY=sk-...

# Email (SMTP)
GMAIL_USER=aidermcliniq@gmail.com
GMAIL_APP_PASSWORD=your-app-password

# CORS
CORS_ORIGINS=["*"]
```

---

*This document is the single source of truth for the AiDerm Cliniq backend architecture.*
