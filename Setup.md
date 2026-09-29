# AiDerm Cliniq — Environment Setup Guide

This guide walks you through setting up every component referenced in the `.env` file.  
**The `.env` file must NEVER be committed to Git.** It is already in `.gitignore`.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Copy the .env File](#2-copy-the-env-file)
3. [SECRET_KEY — JWT Signing Secret](#3-secret_key--jwt-signing-secret)
4. [PostgreSQL — Database](#4-postgresql--database)
5. [Redis — Task Queue and Rate Limiting](#5-redis--task-queue-and-rate-limiting)
6. [Google Cloud Storage (GCS) — File Storage](#6-google-cloud-storage-gcs--file-storage)
7. [Google OAuth 2.0 — Social Login](#7-google-oauth-20--social-login)
8. [AI Provider API Keys](#8-ai-provider-api-keys)
9. [Gmail SMTP — Email Notifications](#9-gmail-smtp--email-notifications)
10. [Firebase — Push Notifications](#10-firebase--push-notifications)
11. [Admin Bootstrap](#11-admin-bootstrap)
12. [Run the Server](#12-run-the-server)
13. [Environment Variable Reference](#13-environment-variable-reference)

---

## 1. Prerequisites

Install these before starting:

| Tool | Version | Purpose |
| --- | --- | --- |
| Python | 3.11+ | Runtime |
| PostgreSQL | 15+ | Primary database |
| Redis | 7+ | Celery task queue + rate limiter |
| Git | Any | Version control |

**Install Python dependencies:**

```bash
cd ai_derm_cliniq_server
pip install -r requirements.txt
```

---

## 2. Copy the .env File

```bash
cp .env.example .env
```

Open `.env` and fill in each section as described below.

---

## 3. SECRET_KEY — JWT Signing Secret

**What it is:** A random string used to sign and verify JWT access tokens. If this is compromised, attackers can forge tokens for any user.

**How to generate:**

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

This produces a 64-character hex string. Copy and paste it:

```env
SECRET_KEY=a3f9e2b1c8d7...  # your generated value
```

**Rules:**
- Minimum 32 bytes (64 hex chars) — shorter keys are cryptographically weak
- Must be different in development, staging, and production
- Never share this key or commit it to Git
- If compromised: change the key — all existing tokens immediately become invalid

**Other token settings (defaults are fine):**

```env
ALGORITHM=HS256                   # JWT signing algorithm — do not change
ACCESS_TOKEN_EXPIRE_MINUTES=60    # How long access tokens last (1 hour)
REFRESH_TOKEN_EXPIRE_DAYS=30      # How long refresh tokens last (30 days)
```

---

## 4. PostgreSQL — Database

### Option A: Local Installation (Development)

**Windows:**
1. Download the PostgreSQL installer from the official PostgreSQL website
2. Run the installer — note the password you set for the `postgres` user
3. Default port: `5432`

**Create the database:**

```bash
# Open psql (PostgreSQL terminal)
psql -U postgres

# Inside psql:
CREATE DATABASE aiderm_cliniq;
\q
```

**Set in .env:**

```env
DATABASE_URL=postgresql+asyncpg://postgres:YOUR_PASSWORD@localhost:5432/aiderm_cliniq
```

Replace `YOUR_PASSWORD` with the postgres user password you set during installation.

### Option B: Docker (Recommended for Development)

```bash
docker run --name aiderm-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=aiderm_cliniq \
  -p 5432:5432 \
  -d postgres:15
```

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/aiderm_cliniq
```

### Run Alembic Migrations

After setting `DATABASE_URL`, run all migrations to create the tables:

```bash
alembic upgrade head
```

### Test the connection

```bash
python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def test():
    engine = create_async_engine('postgresql+asyncpg://postgres:postgres@localhost:5432/aiderm_cliniq')
    async with engine.connect() as conn:
        result = await conn.execute(text('SELECT 1'))
        print('PostgreSQL connected:', result.scalar())
    await engine.dispose()

asyncio.run(test())
"
```

---

## 5. Redis — Task Queue and Rate Limiting

Redis is used for two things:
- **Celery broker:** queues background AI pipeline tasks
- **Rate limiter:** slowapi uses Redis to track request counts per IP
- **Prompt registry:** admin-overridden AI prompts are stored here

### Option A: Local Installation

**Windows:**
Redis does not have an official Windows build. Use one of these approaches:

**Approach 1 — WSL2 (Recommended):**
```bash
# Inside WSL2
sudo apt-get update
sudo apt-get install redis-server
sudo service redis-server start
redis-cli ping  # should print: PONG
```

**Approach 2 — Windows port (Memurai):**
Download Memurai (Redis-compatible for Windows) and install it. It runs as a Windows service.

### Option B: Docker (Recommended)

```bash
docker run --name aiderm-redis \
  -p 6379:6379 \
  -d redis:7
```

**Test:**
```bash
redis-cli ping
# PONG
```

**Set in .env:**

```env
REDIS_URL=redis://localhost:6379/0
```

The `/0` is the Redis database index (0–15). Use `/0` for the main app, `/1` for tests.

---

## 6. Google Cloud Storage (GCS) — File Storage

GCS stores all binary files: skin images, prescription photos, generated PDF reports, and profile avatars.

### Step 1 — Create a Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click **New Project** (top bar)
3. Name it: `aiderm-cliniq` (or any name)
4. Note the **Project ID**

### Step 2 — Enable the Cloud Storage API

1. In your project, go to **APIs & Services → Library**
2. Search for **Cloud Storage**
3. Click **Enable**

### Step 3 — Create a Storage Bucket

1. Go to **Cloud Storage → Buckets**
2. Click **Create**
3. Bucket name: `aiderm-cliniq-storage` (must be globally unique — add your project ID if needed)
4. Region: choose the closest region to your users
5. Storage class: **Standard**
6. Access control: **Uniform** (recommended)
7. Click **Create**

**Set in .env:**

```env
GCS_BUCKET_NAME=aiderm-cliniq-storage
```

### Step 4 — Create a Service Account

A service account gives the backend permission to read/write to GCS without using a personal Google account.

1. Go to **IAM & Admin → Service Accounts**
2. Click **Create Service Account**
3. Name: `aiderm-backend`
4. Click **Create and Continue**
5. Role: **Storage Object Admin** (allows read + write to bucket objects)
6. Click **Done**

### Step 5 — Download the Service Account Key

1. Click the service account you just created
2. Go to **Keys** tab
3. Click **Add Key → Create new key**
4. Choose **JSON**
5. Download the file
6. Rename it to `service-account.json` and place it at:

   ```text
   ai_derm_cliniq_server/credentials/service-account.json
   ```

7. **Never commit this file to Git** — it is in `.gitignore`

**Set in .env:**

```env
GOOGLE_APPLICATION_CREDENTIALS=credentials/service-account.json
```

This is a relative path from the project root — works in both local dev and Docker.

### Step 6 — Configure CORS on the Bucket (required for Flutter Web)

Flutter Web loads images directly from GCS signed URLs. Without a CORS policy, the browser blocks them. Run this once after creating the bucket:

```bash
cd ai_derm_cliniq_server
python -c "
from google.cloud import storage
from google.oauth2 import service_account

creds = service_account.Credentials.from_service_account_file('credentials/service-account.json')
client = storage.Client(credentials=creds)
bucket = client.bucket('aiderm-cliniq-storage')

bucket.cors = [
    {
        'origin': ['*'],
        'method': ['GET', 'HEAD', 'OPTIONS'],
        'responseHeader': ['Content-Type', 'Authorization', 'Access-Control-Allow-Origin'],
        'maxAgeSeconds': 3600,
    }
]
bucket.patch()
print('CORS configured:', bucket.cors)
"
```

> In production, replace `'*'` with your actual web app domain (e.g. `'https://app.aidermcliniq.com'`).

---

## 7. Google OAuth 2.0 — Social Login

This enables the **Sign in with Google** button in the Flutter app.

### Step 1 — Configure OAuth Consent Screen

1. Go to **APIs & Services → OAuth consent screen**
2. User type: **External** (for patients/doctors outside your org)
3. Fill in:
   - App name: `AiDerm Cliniq`
   - User support email: your email
   - Developer contact: your email
4. Scopes: add `email` and `profile`
5. Test users: add your email for testing
6. Save

### Step 2 — Create OAuth 2.0 Credentials

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Web application** (the Flutter app sends the token to this backend)
4. Name: `AiDerm Cliniq Backend`
5. Authorized redirect URIs: `http://localhost:8000/api/v1/auth/google/callback`
6. Click **Create**
7. Copy the **Client ID** and **Client Secret**

**Set in .env:**

```env
GOOGLE_CLIENT_ID=123456789-abcde.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-your-secret
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback
```

In production, add your production domain to the Authorized redirect URIs and update `GOOGLE_REDIRECT_URI`.

### How it works in the app

The Flutter app uses the Google Sign-In SDK to get an **ID token** from Google. It sends this token to `POST /api/v1/auth/google`. The backend calls Google's `tokeninfo` endpoint to verify the token, then creates or finds the user account.

---

## 8. AI Provider API Keys

The backend supports 4 AI providers. At minimum you need one — Gemini is the default.

### Gemini (Default — Recommended)

1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Sign in with your Google account
3. Click **Get API Key → Create API Key**
4. Choose your Google Cloud project (or create a new one)
5. Copy the key

```env
GEMINI_API_KEY=AIzaSy...
GEMINI_MODEL=gemini-2.5-flash
DEFAULT_LLM_PROVIDER=gemini
```

### OpenAI (Optional — also used for Whisper voice transcription)

1. Go to [platform.openai.com](https://platform.openai.com)
2. Go to **API Keys** in your account
3. Click **Create new secret key**
4. Copy the key (shown only once)

```env
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-4o
```

> **Note:** Even if `DEFAULT_LLM_PROVIDER=gemini`, the OpenAI key is required for the doctor voice note transcription feature (Whisper).

### Perplexity (Optional)

1. Go to [perplexity.ai](https://www.perplexity.ai)
2. Go to **Settings → API**
3. Click **Generate** under API Keys
4. Copy the key

```env
PERPLEXITY_API_KEY=pplx-...
```

### DeepSeek (Optional)

1. Go to [platform.deepseek.com](https://platform.deepseek.com)
2. Go to **API Keys**
3. Click **Create API Key**
4. Copy the key

```env
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat
```

### Switching Providers

Change this setting at any time — or switch via the admin panel at runtime:

```env
DEFAULT_LLM_PROVIDER=gemini   # or: openai, perplexity, deepseek
```

---

## 9. Gmail SMTP — Email Notifications

Used for: OTP verification emails, password reset codes, doctor approval/rejection emails, red flag alerts, and patient visit emails.

### Step 1 — Enable 2-Step Verification

Google App Passwords require 2FA on your account.

1. Go to your Google Account → **Security**
2. Under "How you sign in to Google", click **2-Step Verification**
3. Follow the setup steps

### Step 2 — Create an App Password

1. Go to **Security → 2-Step Verification → App passwords**  
   (Direct URL: myaccount.google.com/apppasswords)
2. App: **Mail**
3. Device: **Other** → type `AiDerm Cliniq Backend`
4. Click **Generate**
5. Copy the **16-character password** (shown only once, spaces don't matter)

**Set in .env:**

```env
GMAIL_USER=aidermcliniq@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop   # your 16-char app password
FRONTEND_URL=https://your-app-domain.com  # embedded in patient visit emails
```

---

## 10. Firebase — Push Notifications

Firebase Cloud Messaging (FCM) sends push notifications to patients and doctors (e.g. "Doctor assigned", "Report ready").

### Step 1 — Create a Firebase Project

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Add project**
3. Name it: `AiDerm Cliniq` (can be the same Google Cloud project)
4. Enable Google Analytics if desired → **Create project**

### Step 2 — Download the Service Account Key

1. In your Firebase project, go to **Project Settings** (gear icon)
2. Click the **Service accounts** tab
3. Click **Generate new private key**
4. Download the JSON file
5. Rename it to `firebase_service_account.json` and place it at:

   ```text
   ai_derm_cliniq_server/credentials/firebase_service_account.json
   ```

6. **Never commit this file to Git** — it is in `.gitignore`

**Set in .env:**

```env
FIREBASE_CREDENTIALS_PATH=credentials/firebase_service_account.json
```

### Step 3 — Add Firebase to the Flutter App

1. Install the Firebase CLI: `npm install -g firebase-tools`
2. Run `flutterfire configure` in the Flutter project root
3. Select your Firebase project
4. This generates `lib/firebase_options.dart` automatically

---

## 11. Admin Bootstrap

The server can auto-create the first admin account on startup. This saves you from manually inserting a row into the database.

**Set in .env:**

```env
ADMIN_EMAIL=admin@aidermcliniq.com
ADMIN_PASSWORD=YourStrongPassword123!
```

**How it works:**

- On every server start, the app checks if a user with `ADMIN_EMAIL` exists
- If not → creates a `User` (role: admin) + a `DoctorProfile` (admins can also review cases)
- If already exists → skips creation (idempotent — safe to restart many times)
- If `ADMIN_EMAIL` or `ADMIN_PASSWORD` are empty → bootstrap is skipped entirely

**Security notes:**

- The bootstrap admin cannot be deleted or have their role changed via the admin panel
- Use a strong password — this account has full access to all data
- Leave both fields empty if you prefer to create the admin manually

---

## 12. Run the Server

Once all required variables are set:

### Required variables checklist

Before starting, confirm these are set in `.env`:

- [ ] `SECRET_KEY` — a real random key, not the placeholder
- [ ] `DATABASE_URL` — PostgreSQL is running and the database exists
- [ ] `REDIS_URL` — Redis is running
- [ ] At least one AI key (`GEMINI_API_KEY` if using the default provider)

GCS, Google OAuth, Gmail, and Firebase are only required for their specific features — they don't block server startup.

### Run database migrations

```bash
alembic upgrade head
```

### Start the API server

```bash
cd ai_derm_cliniq_server
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

**Check the health endpoint:**

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
    "status": "healthy",
    "app": "AiDerm Cliniq API",
    "version": "1.0.0",
    "environment": "development",
    "database": "ok",
    "redis": "ok"
}
```

If database or Redis is unreachable, `status` will be `"degraded"` and the affected service will show `"unreachable"`.

**View auto-generated API docs:**

Open in your browser: `http://localhost:8000/docs`

This shows all endpoints with interactive testing capability. Docs are hidden in production (`APP_ENV=production`).

### Start the Celery worker (for AI pipeline tasks)

In a second terminal:

```bash
celery -A src.workers.celery_app worker --loglevel=info
```

The Celery worker handles all background tasks: AI image analysis, PDF report generation, email sending, and push notifications.

---

## 13. Environment Variable Reference

Complete list of all variables with descriptions.

### App Settings

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `APP_ENV` | `development` | No | `development`, `staging`, or `production` |
| `APP_NAME` | `AiDerm Cliniq API` | No | Display name in logs and Swagger UI |
| `APP_VERSION` | `1.0.0` | No | API version shown in /health |
| `DEBUG` | `true` | No | `true` prints SQL queries and error details to console |

### Security / JWT

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `SECRET_KEY` | — | **YES** | Random 64-char hex string for JWT signing |
| `ALGORITHM` | `HS256` | No | JWT signing algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | No | Access token lifetime in minutes |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | No | Refresh token lifetime in days |

### Database

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `DATABASE_URL` | — | **YES** | `postgresql+asyncpg://USER:PASS@HOST:PORT/DBNAME` |

### Redis

| Variable    | Default | Required  | Description                                          |
|-------------|---------|-----------|------------------------------------------------------|
| `REDIS_URL` | —       | **YES**   | Redis connection URL e.g. `redis://localhost:6379/0` |

### Google Cloud Storage

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `GOOGLE_APPLICATION_CREDENTIALS` | `""` | For image upload | Relative path to service account JSON: `credentials/service-account.json` |
| `GCS_BUCKET_NAME` | `aiderm-cliniq-storage` | For image upload | GCS bucket name |

### Google OAuth

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `GOOGLE_CLIENT_ID` | `""` | For Google login | OAuth 2.0 client ID from Google Console |
| `GOOGLE_CLIENT_SECRET` | `""` | For Google login | OAuth 2.0 client secret |
| `GOOGLE_REDIRECT_URI` | `http://localhost:8000/api/v1/auth/google/callback` | For Google login | Must match what you registered in Google Console |

### AI Providers

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | `""` | If using Gemini | Google AI Studio API key |
| `GEMINI_MODEL` | `gemini-2.5-flash` | No | Gemini model name (overridable via admin panel) |
| `OPENAI_API_KEY` | `""` | For voice transcription | OpenAI API key — also needed for Whisper voice notes |
| `OPENAI_MODEL` | `gpt-4o` | No | OpenAI model name (overridable via admin panel) |
| `PERPLEXITY_API_KEY` | `""` | If using Perplexity | Perplexity API key |
| `DEEPSEEK_API_KEY` | `""` | If using DeepSeek | DeepSeek platform API key |
| `DEEPSEEK_MODEL` | `deepseek-chat` | No | DeepSeek model name (overridable via admin panel) |
| `DEFAULT_LLM_PROVIDER` | `gemini` | No | `gemini`, `openai`, `perplexity`, or `deepseek` |

### Email

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `GMAIL_USER` | `""` | For email sending | Gmail address to send from |
| `GMAIL_APP_PASSWORD` | `""` | For email sending | 16-char Gmail App Password (not your account password) |
| `FRONTEND_URL` | `https://aidermcliniq.com` | No | Base URL embedded in patient visit emails |

### CORS

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `CORS_ORIGINS` | `["*"]` | No | List of allowed origins. Use `["*"]` for dev, restrict in production |

### Rate Limiting

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `RATE_LIMIT_ENABLED` | `false` | No | Set to `true` to enable rate limiting |
| `RATE_LIMIT_PER_MINUTE` | `60` | No | Max requests per IP per minute |

### File Upload

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `MAX_IMAGE_SIZE_MB` | `10` | No | Maximum size of a single uploaded image in MB |
| `MAX_IMAGES_PER_CASE` | `10` | No | Maximum number of images per case |

### QR Tokens

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `QR_TOKEN_EXPIRE_HOURS` | `24` | No | How long a QR code is valid after generation |

### Firebase / Push Notifications

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `FIREBASE_CREDENTIALS_PATH` | `credentials/firebase_service_account.json` | For push notifications | Relative path to Firebase service account JSON |

### Admin Bootstrap

| Variable | Default | Required | Description |
| --- | --- | --- | --- |
| `ADMIN_EMAIL` | `""` | No | Email for auto-created admin account. Leave blank to disable. |
| `ADMIN_PASSWORD` | `""` | No | Password for auto-created admin account. |

---

## Security Reminders

- **Never commit `.env` to Git** — it is already in `.gitignore`
- **Never commit `credentials/`** — service account JSON files are in `.gitignore`
- **Rotate API keys immediately** if they are ever accidentally exposed (pushed to GitHub, pasted in a chat, visible in a screenshot)
- **Use a different `SECRET_KEY`** in every environment — if production keys are compromised, development keys should not give access
- **In production**, set `DEBUG=false` and `APP_ENV=production`
- **In production**, restrict `CORS_ORIGINS` to your actual app domains
- **In production**, use a strong PostgreSQL password, not `postgres:postgres`
- **In production**, set `RATE_LIMIT_ENABLED=true`
