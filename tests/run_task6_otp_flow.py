"""Task 6 — OTP Flow End-to-End Verification (#184)

Covers:
  Section 1 — Patient email verification OTP
  Section 2 — Password reset OTP
  Section 3 — Doctor email verification OTP
  Section 4 — Edge cases (enumeration-safe, replay-safe)

OTP RECOVERY STRATEGY
---------------------
OTPs are SHA-256 hashed before DB storage — the raw value is never persisted.
OTPs are 6-digit zero-padded (000000–999999, 1M possibilities).
For each flow we read the token_hash from the DB, then brute-force the match.
SHA-256 over 1M inputs completes in ~1–2 s on modern hardware.
"""

import hashlib
import json
import os
import time

import psycopg2
import requests

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #
BASE_URL    = "http://localhost:8880/api/v1"
OUT         = r"d:\@White Mastery Systems\Derm AI\ai_derm_cliniq_server\Results\Result 1705\06_OTP_Flow_E2E.txt"
ADMIN_EMAIL = "dev@bdcode.in"
ADMIN_PASS  = "@Dev_bdcode.in"

DB = dict(host="127.0.0.1", port=5455, user="postgres", password="postgres", dbname="aiderm_cliniq")

ts         = int(time.time())
P_EMAIL    = f"qa_otp_pat_{ts}@example.com"
P_PASS     = "TestPass@99"
P_NEW_PASS = "TestPass@NewOTP99"
D_EMAIL    = f"qa_otp_doc_{ts}@example.com"
D_PASS     = "DoctorPass@99"

S = requests.Session()

lines   = []
summary = []


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def jdump(r):
    try:
        return json.dumps(r.json(), ensure_ascii=False)[:600]
    except Exception:
        return r.text[:300]


def jparse(r):
    try:
        return r.json()
    except Exception:
        return {}


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


def flush():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def sep():
    lines.append("=" * 70)


def block(sid, desc, method, url, status_code, resp, result,
          role=None, body=None, note=None):
    sep()
    lines.append(f"[{sid} | {desc}]")
    lines.append(f"REQUEST  : {method} {url}")
    if role:  lines.append(f"HEADER   : Authorization: Bearer <{role}>")
    if body:  lines.append(f"BODY     : {json.dumps(body, ensure_ascii=False)}")
    lines.append(f"STATUS   : {status_code}")
    lines.append(f"RESPONSE : {resp}")
    lines.append(f"RESULT   : {result}")
    if note:  lines.append(f"NOTE     : {note}")
    lines.append("")
    tag = "PASS" if result.startswith("PASS") else ("SKIP" if result.startswith("SKIP") else "FAIL")
    summary.append((sid, tag, desc))
    flush()


def info_block(sid, desc, detail, result, note=None):
    sep()
    lines.append(f"[{sid} | {desc}]")
    lines.append(f"ACTION   : {detail}")
    lines.append(f"RESULT   : {result}")
    if note:  lines.append(f"NOTE     : {note}")
    lines.append("")
    tag = "PASS" if result.startswith("PASS") else ("SKIP" if result.startswith("SKIP") else "FAIL")
    summary.append((sid, tag, desc))
    flush()


def header(title):
    lines.append("")
    lines.append(f"━━━ {title} ━━━")
    lines.append("")
    flush()


# ------------------------------------------------------------------ #
# OTP recovery
# ------------------------------------------------------------------ #
def crack_otp(target_hash: str) -> str | None:
    """Brute-force a 6-digit OTP from its SHA-256 hex digest."""
    for n in range(1_000_000):
        candidate = f"{n:06d}"
        if hashlib.sha256(candidate.encode()).hexdigest() == target_hash:
            return candidate
    return None


def get_latest_otp_hash(user_id: str, purpose: str) -> str | None:
    """Return the most-recently-created unused token_hash for a user+purpose."""
    conn = psycopg2.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT token_hash
            FROM   verification_tokens
            WHERE  user_id = %s
              AND  purpose  = %s
              AND  used     = FALSE
            ORDER  BY created_at DESC
            LIMIT  1
            """,
            (user_id, purpose),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #
def run():
    lines.append("TASK 6 — OTP FLOW END-TO-END (#184)")
    lines.append(f"Tested On : {time.strftime('%Y-%m-%d')}")
    lines.append(f"Server    : {BASE_URL}")
    lines.append("")
    lines.append("TEST STRATEGY")
    lines.append("─" * 69)
    lines.append("1. Patient email verification OTP (authenticated flow)")
    lines.append("2. Password reset OTP")
    lines.append("3. Doctor email verification OTP (unauthenticated flow)")
    lines.append("4. Edge cases: no enumeration, OTP replay blocked")
    lines.append("")
    lines.append("ISSUES COVERED")
    lines.append("─" * 69)
    lines.append("  #184  [MH11] Verify OTP flow works end-to-end without errors")
    lines.append("")

    pat_token = pat_user_id = None
    doc_user_id = None

    # ================================================================ #
    # SECTION 1 — Patient Email Verification OTP
    # ================================================================ #
    header("SECTION 1 — Patient Email Verification OTP")

    # 1A: Register patient
    body = {"email": P_EMAIL, "password": P_PASS, "full_name": f"OTP Test Patient {ts}"}
    r = S.post(f"{BASE_URL}/auth/register/patient", json=body)
    d = jparse(r)
    if r.status_code == 201 and "access_token" in d:
        pat_token = d["access_token"]
        pat_user_id = d.get("user", {}).get("id") or d.get("user_id")
        # Some responses nest user.id
        if not pat_user_id:
            pat_user_id = d.get("user", {}).get("id")
        result = f"PASS — patient registered id={pat_user_id}"
    else:
        result = f"FAIL — status={r.status_code}"
    block("1A", "POST /auth/register/patient", "POST",
          f"{BASE_URL}/auth/register/patient", r.status_code, jdump(r), result, body=body)
    if not pat_token:
        lines.append("ABORT: cannot continue without patient token")
        flush()
        return

    # 1B: Request email verification OTP
    r = S.post(f"{BASE_URL}/auth/verify-email", headers=h(pat_token))
    result = (
        f"PASS — OTP requested, email sent (or silently failed in test env)"
        if r.status_code == 200
        else f"FAIL — status={r.status_code}"
    )
    block("1B", "POST /auth/verify-email — request OTP", "POST",
          f"{BASE_URL}/auth/verify-email", r.status_code, jdump(r), result, role="patient_token",
          note="OTP is SHA-256 hashed; raw value only emailed. Recovery via DB brute-force next.")

    # 1C: Recover OTP from DB
    otp_hash = get_latest_otp_hash(pat_user_id, "email_verify")
    if otp_hash:
        otp = crack_otp(otp_hash)
        if otp:
            result = f"PASS — OTP recovered: {otp}"
            note = f"Brute-forced SHA-256 of 000000–999999; hash={otp_hash[:16]}..."
        else:
            result = "FAIL — OTP not found in 0–999999 space"
            note = None
    else:
        otp = None
        result = "FAIL — no unused email_verify token in DB"
        note = None
    info_block("1C", "DB: recover OTP from token_hash",
               f"SELECT token_hash FROM verification_tokens WHERE user_id='{pat_user_id}' AND purpose='email_verify' AND used=FALSE",
               result, note=note)

    # 1D: Submit wrong OTP → expect 401/422
    if otp:
        wrong_otp = "000001" if otp != "000001" else "000002"
        body = {"otp": wrong_otp}
        r = S.post(f"{BASE_URL}/auth/verify-email/confirm", json=body, headers=h(pat_token))
        result = (
            f"PASS — {r.status_code} wrong OTP rejected"
            if r.status_code in (400, 401, 422)
            else f"FAIL — expected 4xx, got {r.status_code}"
        )
        block("1D", "POST /auth/verify-email/confirm — wrong OTP rejected", "POST",
              f"{BASE_URL}/auth/verify-email/confirm", r.status_code, jdump(r), result,
              role="patient_token", body=body,
              note="Wrong OTP must not consume the valid token")

    # 1E: Submit correct OTP → 200
    if otp:
        body = {"otp": otp}
        r = S.post(f"{BASE_URL}/auth/verify-email/confirm", json=body, headers=h(pat_token))
        d = jparse(r)
        result = (
            f"PASS — email verified (200)"
            if r.status_code == 200
            else f"FAIL — status={r.status_code}"
        )
        block("1E", "POST /auth/verify-email/confirm — correct OTP accepted", "POST",
              f"{BASE_URL}/auth/verify-email/confirm", r.status_code, jdump(r), result,
              role="patient_token", body=body)
    else:
        summary.append(("1E", "SKIP", "POST /auth/verify-email/confirm — skipped (no OTP)"))

    # 1F: Verify is_verified=True via /users/me
    r = S.get(f"{BASE_URL}/users/me", headers=h(pat_token))
    d = jparse(r)
    is_verified = d.get("is_verified", False)
    result = (
        f"PASS — is_verified=True confirmed"
        if is_verified
        else f"FAIL — is_verified={is_verified}"
    )
    block("1F", "GET /users/me — is_verified=True after OTP confirm", "GET",
          f"{BASE_URL}/users/me", r.status_code, jdump(r), result, role="patient_token")

    # 1G: Replay same OTP → 401 (already used)
    if otp:
        body = {"otp": otp}
        r = S.post(f"{BASE_URL}/auth/verify-email/confirm", json=body, headers=h(pat_token))
        result = (
            f"PASS — {r.status_code} replayed OTP rejected"
            if r.status_code in (400, 401, 422)
            else f"FAIL — expected 4xx, got {r.status_code}"
        )
        block("1G", "POST /auth/verify-email/confirm — OTP replay blocked", "POST",
              f"{BASE_URL}/auth/verify-email/confirm", r.status_code, jdump(r), result,
              role="patient_token", body=body,
              note="Already-used OTP must be rejected")

    # 1H: Request verification again (already verified) → 200 no-op
    r = S.post(f"{BASE_URL}/auth/verify-email", headers=h(pat_token))
    result = (
        f"PASS — 200 no-op when already verified"
        if r.status_code == 200
        else f"FAIL — status={r.status_code}"
    )
    block("1H", "POST /auth/verify-email — no-op if already verified", "POST",
          f"{BASE_URL}/auth/verify-email", r.status_code, jdump(r), result,
          role="patient_token",
          note="service.request_email_verification is a no-op if is_verified=True")

    # ================================================================ #
    # SECTION 2 — Password Reset OTP
    # ================================================================ #
    header("SECTION 2 — Password Reset OTP")

    # 2A: Forgot password
    body = {"email": P_EMAIL}
    r = S.post(f"{BASE_URL}/auth/forgot-password", json=body)
    result = (
        f"PASS — 200 (always, prevents enumeration)"
        if r.status_code == 200
        else f"FAIL — status={r.status_code}"
    )
    block("2A", "POST /auth/forgot-password — trigger reset OTP", "POST",
          f"{BASE_URL}/auth/forgot-password", r.status_code, jdump(r), result, body=body)

    # 2B: Recover OTP
    otp_hash = get_latest_otp_hash(pat_user_id, "password_reset")
    if otp_hash:
        otp_reset = crack_otp(otp_hash)
        result = (
            f"PASS — reset OTP recovered: {otp_reset}"
            if otp_reset
            else "FAIL — OTP not found"
        )
    else:
        otp_reset = None
        result = "FAIL — no unused password_reset token in DB"
    info_block("2B", "DB: recover password-reset OTP",
               f"SELECT token_hash FROM verification_tokens WHERE user_id='{pat_user_id}' AND purpose='password_reset'",
               result)

    # 2C: Wrong OTP → 401
    if otp_reset:
        wrong_otp = "000001" if otp_reset != "000001" else "000002"
        body = {"email": P_EMAIL, "otp": wrong_otp, "new_password": P_NEW_PASS}
        r = S.post(f"{BASE_URL}/auth/reset-password", json=body)
        result = (
            f"PASS — {r.status_code} wrong OTP rejected"
            if r.status_code in (400, 401, 422)
            else f"FAIL — expected 4xx, got {r.status_code}"
        )
        block("2C", "POST /auth/reset-password — wrong OTP rejected", "POST",
              f"{BASE_URL}/auth/reset-password", r.status_code, jdump(r), result, body=body)

    # 2D: Correct OTP → 200
    if otp_reset:
        body = {"email": P_EMAIL, "otp": otp_reset, "new_password": P_NEW_PASS}
        r = S.post(f"{BASE_URL}/auth/reset-password", json=body)
        result = (
            f"PASS — password reset succeeded (200)"
            if r.status_code == 200
            else f"FAIL — status={r.status_code}"
        )
        block("2D", "POST /auth/reset-password — correct OTP accepted", "POST",
              f"{BASE_URL}/auth/reset-password", r.status_code, jdump(r), result, body=body)
    else:
        summary.append(("2D", "SKIP", "POST /auth/reset-password — skipped (no OTP)"))

    # 2E: Login with old password → 401
    body = {"email": P_EMAIL, "password": P_PASS}
    r = S.post(f"{BASE_URL}/auth/login", json=body)
    result = (
        f"PASS — {r.status_code} old password rejected after reset"
        if r.status_code in (401, 403)
        else f"FAIL — expected 401/403, got {r.status_code}"
    )
    block("2E", "POST /auth/login — old password rejected after reset", "POST",
          f"{BASE_URL}/auth/login", r.status_code, jdump(r), result,
          body={"email": P_EMAIL, "password": "***"})

    # 2F: Login with new password → 200
    body = {"email": P_EMAIL, "password": P_NEW_PASS}
    r = S.post(f"{BASE_URL}/auth/login", json=body)
    d = jparse(r)
    result = (
        f"PASS — login with new password succeeded"
        if r.status_code == 200 and "access_token" in d
        else f"FAIL — status={r.status_code}"
    )
    block("2F", "POST /auth/login — new password accepted", "POST",
          f"{BASE_URL}/auth/login", r.status_code, jdump(r), result,
          body={"email": P_EMAIL, "password": "***"})

    # 2G: Replay reset OTP → 401
    if otp_reset:
        body = {"email": P_EMAIL, "otp": otp_reset, "new_password": "AnotherPass@99"}
        r = S.post(f"{BASE_URL}/auth/reset-password", json=body)
        result = (
            f"PASS — {r.status_code} replayed reset OTP rejected"
            if r.status_code in (400, 401, 422)
            else f"FAIL — expected 4xx, got {r.status_code}"
        )
        block("2G", "POST /auth/reset-password — reset OTP replay blocked", "POST",
              f"{BASE_URL}/auth/reset-password", r.status_code, jdump(r), result, body=body,
              note="Used OTP must not be redeemable")

    # 2H: Forgot password — non-existent email (no enumeration)
    body = {"email": f"nonexistent.qa.{ts}@example.com"}
    r = S.post(f"{BASE_URL}/auth/forgot-password", json=body)
    result = (
        f"PASS — 200 for non-existent email (no enumeration)"
        if r.status_code == 200
        else f"FAIL — expected 200, got {r.status_code}"
    )
    block("2H", "POST /auth/forgot-password — non-existent email returns 200", "POST",
          f"{BASE_URL}/auth/forgot-password", r.status_code, jdump(r), result, body=body)

    # ================================================================ #
    # SECTION 3 — Doctor Email Verification OTP
    # ================================================================ #
    header("SECTION 3 — Doctor Email Verification OTP")

    # 3A: Register doctor
    body = {
        "email": D_EMAIL,
        "password": D_PASS,
        "full_name": f"OTP Test Doctor {ts}",
        "specialization": "Dermatology",
        "license_number": f"TEST-{ts}",
        "clinic_name": "OTP Test Clinic",
    }
    r = S.post(f"{BASE_URL}/auth/register/doctor", json=body)
    d = jparse(r)
    if r.status_code == 201:
        doc_user_id = d.get("user", {}).get("id")
        is_active   = d.get("user", {}).get("is_active", True)
        is_verified = d.get("user", {}).get("is_verified", True)
        result = f"PASS — doctor registered id={doc_user_id}, is_active={is_active}, is_verified={is_verified}"
    else:
        result = f"FAIL — status={r.status_code}"
    block("3A", "POST /auth/register/doctor — pending state, no tokens", "POST",
          f"{BASE_URL}/auth/register/doctor", r.status_code, jdump(r), result,
          body={**body, "password": "***"},
          note="Doctor registered with is_active=False, is_verified=False — OTP sent")

    if not doc_user_id:
        lines.append("SKIP remaining Section 3 — no doctor user_id")
        flush()
    else:
        # 3B: Resend doctor verification OTP
        body = {"email": D_EMAIL}
        r = S.post(f"{BASE_URL}/auth/resend-doctor-verification", json=body)
        result = (
            f"PASS — 200 (always, prevents enumeration)"
            if r.status_code == 200
            else f"FAIL — status={r.status_code}"
        )
        block("3B", "POST /auth/resend-doctor-verification — resend OTP", "POST",
              f"{BASE_URL}/auth/resend-doctor-verification", r.status_code, jdump(r), result, body=body,
              note="Previous OTP invalidated; new one generated")

        # 3C: Recover OTP
        otp_hash = get_latest_otp_hash(doc_user_id, "email_verify")
        if otp_hash:
            otp_doc = crack_otp(otp_hash)
            result = (
                f"PASS — doctor email OTP recovered: {otp_doc}"
                if otp_doc
                else "FAIL — OTP not found"
            )
        else:
            otp_doc = None
            result = "FAIL — no unused email_verify token in DB for doctor"
        info_block("3C", "DB: recover doctor email-verify OTP",
                   f"SELECT token_hash FROM verification_tokens WHERE user_id='{doc_user_id}' AND purpose='email_verify'",
                   result)

        # 3D: Wrong OTP → 401
        if otp_doc:
            wrong_otp = "000001" if otp_doc != "000001" else "000002"
            body = {"email": D_EMAIL, "otp": wrong_otp}
            r = S.post(f"{BASE_URL}/auth/verify-doctor-email", json=body)
            result = (
                f"PASS — {r.status_code} wrong OTP rejected"
                if r.status_code in (400, 401, 422)
                else f"FAIL — expected 4xx, got {r.status_code}"
            )
            block("3D", "POST /auth/verify-doctor-email — wrong OTP rejected", "POST",
                  f"{BASE_URL}/auth/verify-doctor-email", r.status_code, jdump(r), result, body=body)

        # 3E: Correct OTP → 200
        if otp_doc:
            body = {"email": D_EMAIL, "otp": otp_doc}
            r = S.post(f"{BASE_URL}/auth/verify-doctor-email", json=body)
            result = (
                f"PASS — doctor email verified (200)"
                if r.status_code == 200
                else f"FAIL — status={r.status_code}"
            )
            block("3E", "POST /auth/verify-doctor-email — correct OTP accepted", "POST",
                  f"{BASE_URL}/auth/verify-doctor-email", r.status_code, jdump(r), result, body=body)

        # 3F: Login as doctor → 403 DOCTOR_PENDING_APPROVAL (not admin-approved yet)
        body = {"email": D_EMAIL, "password": D_PASS}
        r = S.post(f"{BASE_URL}/auth/login", json=body)
        d = jparse(r)
        code = d.get("error", {}).get("code", "")
        result = (
            f"PASS — 403 DOCTOR_PENDING_APPROVAL (email verified, not yet admin-approved)"
            if r.status_code == 403 and code == "DOCTOR_PENDING_APPROVAL"
            else f"FAIL — expected 403 DOCTOR_PENDING_APPROVAL, got {r.status_code} {code}"
        )
        block("3F", "POST /auth/login — doctor login blocked until admin approval", "POST",
              f"{BASE_URL}/auth/login", r.status_code, jdump(r), result,
              body={"email": D_EMAIL, "password": "***"},
              note="is_active=False until admin approves — login must fail")

        # 3G: Replay doctor OTP → 401
        if otp_doc:
            body = {"email": D_EMAIL, "otp": otp_doc}
            r = S.post(f"{BASE_URL}/auth/verify-doctor-email", json=body)
            result = (
                f"PASS — {r.status_code} replayed doctor OTP rejected"
                if r.status_code in (400, 401, 422)
                else f"FAIL — expected 4xx, got {r.status_code}"
            )
            block("3G", "POST /auth/verify-doctor-email — OTP replay blocked", "POST",
                  f"{BASE_URL}/auth/verify-doctor-email", r.status_code, jdump(r), result, body=body)

        # 3H: Resend for non-existent email → 200 (no enumeration)
        body = {"email": f"nobody.qa.{ts}@example.com"}
        r = S.post(f"{BASE_URL}/auth/resend-doctor-verification", json=body)
        result = (
            f"PASS — 200 for non-existent email (no enumeration)"
            if r.status_code == 200
            else f"FAIL — expected 200, got {r.status_code}"
        )
        block("3H", "POST /auth/resend-doctor-verification — non-existent email returns 200", "POST",
              f"{BASE_URL}/auth/resend-doctor-verification", r.status_code, jdump(r), result, body=body)

    # ================================================================ #
    # SUMMARY
    # ================================================================ #
    lines.append("=" * 70)
    lines.append("SUMMARY")
    lines.append("─" * 69)
    lines.append("")

    sections = {
        "Patient Email Verification OTP": [s for s in summary if s[0].startswith("1")],
        "Password Reset OTP":             [s for s in summary if s[0].startswith("2")],
        "Doctor Email Verification OTP":  [s for s in summary if s[0].startswith("3")],
    }
    for title, items in sections.items():
        lines.append(f"{title}:")
        for sid, tag, desc in items:
            lines.append(f"  {sid:<12} {tag:<5} {desc}")
        lines.append("")

    total = len(summary)
    passed = sum(1 for _, t, _ in summary if t == "PASS")
    failed = sum(1 for _, t, _ in summary if t == "FAIL")
    skipped = sum(1 for _, t, _ in summary if t == "SKIP")
    lines.append(f"Total: {passed} PASS  {failed} FAIL  {skipped} SKIP")
    lines.append("=" * 70)
    flush()
    print(f"\nDone — {passed} PASS  {failed} FAIL  {skipped} SKIP")
    print(f"Output: {OUT}")


if __name__ == "__main__":
    run()
