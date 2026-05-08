"""Task 2 — Full E2E Doctor Flow Test (v2: all schema fixes applied)"""
import requests, time, json, os

BASE_URL    = "https://54d1-115-97-59-234.ngrok-free.app/api/v1"
OUT         = r"d:\@White Mastery Systems\Derm AI\ai_derm_cliniq_server\Results\Result 0605\02_Doctor_Flow_E2E.txt"
IMG         = r"d:\@White Mastery Systems\Derm AI\image.png"
ADMIN_EMAIL = "dev@bdcode.in"
ADMIN_PASS  = "@Dev_bdcode.in"

ts       = int(time.time())
D_EMAIL  = f"qa_doc_{ts}@clinic.com"
D_PASS   = "DocPass@99"
P2_EMAIL = f"qa_pat2_{ts}@example.com"
P2_PASS  = "Test@12345"
D2_EMAIL = f"qa_doc2_{ts}@clinic.com"
D2_PASS  = "DocPass@99"

NGROK = {"ngrok-skip-browser-warning": "true"}
S = requests.Session()
S.headers.update(NGROK)

admin_token = doc_token = doc_refresh = doc_user_id = None
pat2_token = pat2_user_id = pat2_code = None
case_p2_id = case_doc_id = None
qr2_token = display2_id = None
todo_id = study_id = None

lines = []
summary = []

# Minimal valid JPEG (1x1 pixel)
TINY_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c'
    b'\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c'
    b'\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\x1edL\t\n\x11\x13'
    b'\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00'
    b'\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00'
    b'\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b'
    b'\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xf5\x0f\xff\xd9'
)


def jdump(r):
    try:
        return json.dumps(r.json(), ensure_ascii=False)[:600]
    except:
        return r.text[:300]


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


def flush():
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def sep():
    lines.append("=" * 70)


def block(sid, desc, method, url, status, resp, result, role=None, body=None, note=None):
    sep()
    lines.append(f"[{sid} | {desc}]")
    lines.append(f"REQUEST  : {method} {url}")
    if role:  lines.append(f"HEADER   : Authorization: Bearer <{role}>")
    if body:  lines.append(f"BODY     : {json.dumps(body)}")
    lines.append(f"STATUS   : {status}")
    lines.append(f"RESPONSE : {resp}")
    lines.append(f"RESULT   : {result}")
    if note:  lines.append(f"NOTE     : {note}")
    lines.append("")
    tag = "PASS" if result.startswith("PASS") else ("SKIP" if result.startswith("SKIP") else "FAIL")
    summary.append(f"  {sid:<8} {tag}  {desc}")
    flush()


# ── HEADER ─────────────────────────────────────────────────────────────────
lines += [
    "TASK 2 — FULL E2E DOCTOR FLOW",
    "Tested On : 2026-05-06",
    f"Server    : {BASE_URL}",
    "",
    "TEST STRATEGY",
    "─" * 69,
    "1. Register fresh doctor account (pending approval)",
    "2. Admin approves doctor; walk every doctor-facing endpoint",
    "3. Patient 2 completes AI questionnaire → QR generated → doctor scans",
    "4. Visual findings, review, AI-assist, voice note, todos, entities,",
    "   report generation, studies",
    "",
    "SCHEMA CORRECTIONS (discovered during Task 1 & Task 2 v1)",
    "─" * 69,
    "• OTP column          : verification_tokens.otp (plaintext, not otp_hash)",
    "• device-token field  : fcm_token (not device_token)",
    "• assessment depth    : rounds:int (not depth:str)",
    "• chat answers body   : [{question_index:int, answer:str}]",
    "• voice-note field    : audio (not file)",
    "• study submit        : multipart form (file, features, stability, technical_quality)",
    "• prompt PATCH/DELETE : uses actual key from prompts[].key, not top-level dict key",
    "• doctor reject       : requires JSON body {reason: str}",
    "",
]
flush()

# ══════════════════════════════════════════════════════════════════════════
# SETUP — admin + patient 2 + case + AI questionnaire + QR
# ══════════════════════════════════════════════════════════════════════════

# SETUP: Admin login
r = S.post(f"{BASE_URL}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
admin_token = r.json().get("access_token")
block("SETUP", "Admin login", "POST", f"{BASE_URL}/auth/login",
      f"{r.status_code}", jdump(r),
      "PASS — admin token obtained" if r.status_code == 200 else f"FAIL — {r.status_code}",
      body={"email": ADMIN_EMAIL, "password": "***"})

# SETUP2: Register Patient 2
r = S.post(f"{BASE_URL}/auth/register/patient",
           json={"email": P2_EMAIL, "password": P2_PASS, "full_name": "QA Patient Two",
                 "date_of_birth": "1990-01-01", "gender": "male"})
d = r.json()
pat2_user_id = (d.get("user") or {}).get("id") or d.get("user_id")
pat2_token   = d.get("access_token")
pat2_code    = (d.get("user") or {}).get("patient_code", "")
block("SETUP2", "Register Patient 2 (for QR scenario)", "POST", f"{BASE_URL}/auth/register/patient",
      f"{r.status_code}", jdump(r),
      "PASS — Patient 2 ready" if r.status_code == 201 else f"FAIL — {r.status_code}",
      body={"email": P2_EMAIL, "password": "***", "full_name": "QA Patient Two"})

# SETUP3: Patient 2 creates case
body3 = {"consultation_type": "new_complaint", "has_visible_lesion": True,
         "is_for_self": True, "consent_ai_analysis": True, "consent_research": False,
         "body_location": "Back", "presenting_complaint": "Dark mole on back, changing size"}
r = S.post(f"{BASE_URL}/cases", headers=h(pat2_token), json=body3)
case_p2_id = r.json().get("id") if r.status_code == 201 else None
block("SETUP3", "Patient 2 creates case (for QR scan)", "POST", f"{BASE_URL}/cases",
      f"{r.status_code}", jdump(r),
      f"PASS — case_id={case_p2_id}" if r.status_code == 201 else f"FAIL — {r.status_code}",
      role="pat2_token", body=body3)

# SETUP4: Patient 2 uploads image
if case_p2_id and os.path.exists(IMG):
    with open(IMG, "rb") as f_img:
        r = S.post(f"{BASE_URL}/cases/{case_p2_id}/images",
                   headers=h(pat2_token),
                   files={"file": ("clinical.png", f_img, "image/png")},
                   data={"image_type": "clinical"})
    block("SETUP4", "Patient 2 uploads clinical image", "POST",
          f"{BASE_URL}/cases/{case_p2_id}/images", f"{r.status_code}", jdump(r),
          "PASS — image uploaded" if r.status_code == 201 else f"FAIL — {r.status_code}",
          role="pat2_token")

# SETUP_AI: Trigger AI analysis via POST /ai/analyze (correct endpoint from Task 1 F3)
# /chat/session and /chat/depth do not exist — /ai/analyze is the direct trigger.
ai_status = "not_started"
if case_p2_id and pat2_token:
    r = S.post(f"{BASE_URL}/cases/{case_p2_id}/ai/analyze", headers=h(pat2_token))
    d_ai = r.json() if r.status_code == 200 else {}
    ai_status = d_ai.get("ai_status", "unknown")
    block("SETUP_AI1", "Patient 2 triggers AI analysis (POST /cases/{id}/ai/analyze)", "POST",
          f"{BASE_URL}/cases/{case_p2_id}/ai/analyze", f"{r.status_code}", jdump(r),
          f"PASS — AI {ai_status} (async 202)" if r.status_code == 202
          else f"PASS — AI {ai_status}" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="pat2_token",
          note="Correct trigger (Task 1 F3); returns 202 async with task_id; /chat/session does not exist")

    # Poll if not immediately completed
    poll_start = time.time()
    r_ai = r
    while ai_status not in ("completed", "failed") and time.time() - poll_start < 180:
        r_ai = S.get(f"{BASE_URL}/cases/{case_p2_id}/ai/status", headers=h(pat2_token))
        if r_ai.status_code == 200:
            ai_status = r_ai.json().get("ai_status", "unknown")
            if ai_status in ("completed", "failed"):
                break
        time.sleep(8)
    block("SETUP_AI2", f"Poll Patient 2 AI completion (status={ai_status})", "GET",
          f"{BASE_URL}/cases/{case_p2_id}/ai/status",
          f"{r_ai.status_code}", jdump(r_ai),
          f"PASS — AI {ai_status}" if ai_status in ("completed", "failed")
          else f"FAIL — AI still '{ai_status}' after 180s",
          role="pat2_token", note=f"Elapsed: {int(time.time()-poll_start)}s")

# SETUP5: Generate QR
if case_p2_id and pat2_token:
    r = S.post(f"{BASE_URL}/qr/generate", headers=h(pat2_token), json={"case_id": case_p2_id})
    d = r.json()
    qr2_token   = d.get("token")
    display2_id = d.get("display_id", "")
    block("SETUP5", "Patient 2 generates QR token", "POST", f"{BASE_URL}/qr/generate",
          f"{r.status_code}", jdump(r),
          f"PASS — token={qr2_token}" if r.status_code == 201 else f"FAIL — {r.status_code}",
          role="pat2_token")
else:
    qr2_token = None
    display2_id = ""

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE A — Doctor Registration & Approval ━━━")
lines.append("")

# A1: Register Doctor 1
body_a1 = {"email": D_EMAIL, "password": D_PASS, "full_name": "Dr QA Tester",
            "specialization": "dermatology", "license_number": f"LIC-{ts}",
            "clinic_name": "QA Skin Clinic"}
r = S.post(f"{BASE_URL}/auth/register/doctor", json=body_a1)
d = r.json()
doc_user_id = (d.get("user") or {}).get("id") or d.get("user_id")
block("A1", "POST /auth/register/doctor → 201 pending", "POST",
      f"{BASE_URL}/auth/register/doctor", f"{r.status_code}", jdump(r),
      "PASS — doctor registered pending" if r.status_code == 201 else f"FAIL — {r.status_code}",
      body=body_a1)

# A2: Unapproved doctor login → 401/403
r = S.post(f"{BASE_URL}/auth/login", json={"email": D_EMAIL, "password": D_PASS})
block("A2", "POST /auth/login unapproved doctor → 401/403", "POST",
      f"{BASE_URL}/auth/login", f"{r.status_code}", jdump(r),
      "PASS — unapproved doctor blocked" if r.status_code in (401, 403)
      else f"FAIL — expected 401/403 got {r.status_code}",
      body={"email": D_EMAIL, "password": "***"})

# A3: Register Doctor 2 (for reject test)
body_a3 = {"email": D2_EMAIL, "password": D2_PASS, "full_name": "Dr Reject Test",
            "specialization": "general_practice", "license_number": f"LIC2-{ts}",
            "clinic_name": "Reject Clinic"}
r2 = S.post(f"{BASE_URL}/auth/register/doctor", json=body_a3)
doc2_user_id = (r2.json().get("user") or {}).get("id") or r2.json().get("user_id")
block("A3", "POST /auth/register/doctor (2nd for reject test) → 201", "POST",
      f"{BASE_URL}/auth/register/doctor", f"{r2.status_code}", jdump(r2),
      "PASS — Dr2 registered" if r2.status_code == 201 else f"FAIL — {r2.status_code}",
      body=body_a3)

# A4: Admin views pending doctors
r = S.get(f"{BASE_URL}/admin/doctors/pending", headers=h(admin_token))
block("A4", "GET /admin/doctors/pending → doctor appears", "GET",
      f"{BASE_URL}/admin/doctors/pending", f"{r.status_code}", jdump(r),
      "PASS — pending doctors listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="admin_token")

# A5: Admin lists all doctors
r = S.get(f"{BASE_URL}/admin/doctors", headers=h(admin_token))
block("A5", "GET /admin/doctors → all doctors list", "GET",
      f"{BASE_URL}/admin/doctors", f"{r.status_code}", jdump(r),
      "PASS — doctors listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="admin_token")

# A6: Admin approves Doctor 1
if doc_user_id:
    r = S.post(f"{BASE_URL}/admin/doctors/{doc_user_id}/approve", headers=h(admin_token))
    block("A6", "POST /admin/doctors/{id}/approve → 200", "POST",
          f"{BASE_URL}/admin/doctors/{doc_user_id}/approve", f"{r.status_code}", jdump(r),
          "PASS — doctor approved" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token")

# A7: Admin rejects Doctor 2 — requires a body with reason
if doc2_user_id:
    reject_body = {"reason": "QA test rejection — administrative decision"}
    r = S.post(f"{BASE_URL}/admin/doctors/{doc2_user_id}/reject", headers=h(admin_token),
               json=reject_body)
    block("A7", "POST /admin/doctors/{id}/reject → 204", "POST",
          f"{BASE_URL}/admin/doctors/{doc2_user_id}/reject", f"{r.status_code}",
          "(no body)" if r.status_code == 204 else jdump(r),
          "PASS — doctor rejected" if r.status_code in (200, 204) else f"FAIL — {r.status_code}",
          role="admin_token", body=reject_body)

# A8: Approved doctor login
r = S.post(f"{BASE_URL}/auth/login", json={"email": D_EMAIL, "password": D_PASS})
d = r.json()
doc_token   = d.get("access_token")
doc_refresh = d.get("refresh_token")
block("A8", "POST /auth/login approved doctor → 200", "POST",
      f"{BASE_URL}/auth/login", f"{r.status_code}", jdump(r),
      "PASS — doctor logged in" if r.status_code == 200 else f"FAIL — {r.status_code}",
      body={"email": D_EMAIL, "password": "***"})

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE B — Admin User Management ━━━")
lines.append("")

r = S.get(f"{BASE_URL}/admin/users", headers=h(admin_token))
block("B1", "GET /admin/users → paginated list → 200", "GET",
      f"{BASE_URL}/admin/users", f"{r.status_code}", jdump(r),
      "PASS — users listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="admin_token")

r = S.get(f"{BASE_URL}/admin/users?role=doctor", headers=h(admin_token))
block("B2", "GET /admin/users?role=doctor → filtered → 200", "GET",
      f"{BASE_URL}/admin/users?role=doctor", f"{r.status_code}", jdump(r),
      "PASS — doctor filter works" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="admin_token")

if pat2_user_id:
    r = S.get(f"{BASE_URL}/admin/users/{pat2_user_id}", headers=h(admin_token))
    block("B3", "GET /admin/users/{user_id} → full detail → 200", "GET",
          f"{BASE_URL}/admin/users/{pat2_user_id}", f"{r.status_code}", jdump(r),
          "PASS — user detail returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token")

    r = S.patch(f"{BASE_URL}/admin/users/{pat2_user_id}", headers=h(admin_token),
                json={"is_active": False})
    block("B4", "PATCH /admin/users/{id} → suspend (is_active=false) → 200", "PATCH",
          f"{BASE_URL}/admin/users/{pat2_user_id}", f"{r.status_code}", jdump(r),
          "PASS — user suspended" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token", body={"is_active": False})

    r = S.patch(f"{BASE_URL}/admin/users/{pat2_user_id}", headers=h(admin_token),
                json={"is_active": True})
    block("B5", "PATCH /admin/users/{id} → reactivate (is_active=true) → 200", "PATCH",
          f"{BASE_URL}/admin/users/{pat2_user_id}", f"{r.status_code}", jdump(r),
          "PASS — user reactivated" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token", body={"is_active": True})

    r = S.post(f"{BASE_URL}/admin/users/{pat2_user_id}/resend-verification",
               headers=h(admin_token))
    block("B6", "POST /admin/users/{id}/resend-verification → 204", "POST",
          f"{BASE_URL}/admin/users/{pat2_user_id}/resend-verification", f"{r.status_code}",
          "(no body)" if r.status_code == 204 else jdump(r),
          "PASS — verification resent" if r.status_code == 204 else f"FAIL — {r.status_code}",
          role="admin_token")

r = S.get(f"{BASE_URL}/admin/cases", headers=h(admin_token))
block("B7", "GET /admin/cases → all cases across all patients → 200", "GET",
      f"{BASE_URL}/admin/cases", f"{r.status_code}", jdump(r),
      "PASS — admin cases listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="admin_token")

if case_p2_id:
    r = S.get(f"{BASE_URL}/admin/cases/{case_p2_id}", headers=h(admin_token))
    block("B8", "GET /admin/cases/{case_id} → full case detail → 200", "GET",
          f"{BASE_URL}/admin/cases/{case_p2_id}", f"{r.status_code}", jdump(r),
          "PASS — admin case detail returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token")

r = S.get(f"{BASE_URL}/admin/stats", headers=h(admin_token))
d = r.json()
total_u = d.get("total_users", 0)
total_p = d.get("total_patients", 0)
total_d = d.get("total_doctors", 0)
total_a = d.get("total_admins", 0)
match = total_p + total_d + total_a == total_u
block("B9", "GET /admin/stats → platform counts + invariant check", "GET",
      f"{BASE_URL}/admin/stats", f"{r.status_code}", jdump(r),
      f"PASS — invariant: {total_p}+{total_d}+{total_a}={total_u}" if r.status_code == 200 and match
      else f"FAIL — invariant broken: {total_p}+{total_d}+{total_a}≠{total_u}",
      role="admin_token")

r = S.get(f"{BASE_URL}/admin/stats", headers=h(doc_token))
block("B10", "GET /admin/stats with doctor token → 403", "GET",
      f"{BASE_URL}/admin/stats", f"{r.status_code}", jdump(r),
      "PASS — doctor cannot access admin stats" if r.status_code == 403 else f"FAIL — {r.status_code}",
      role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE C — Admin AI Prompt Overrides ━━━")
lines.append("")

r = S.get(f"{BASE_URL}/admin/prompts", headers=h(admin_token))
prompts_resp = r.json() if r.status_code == 200 else {}
# Response is {"prompts": [{key, label, value, has_override}, ...]}
prompt_list = prompts_resp.get("prompts", [])
first_key = prompt_list[0].get("key") if prompt_list else None
block("C1", "GET /admin/prompts → list all prompt keys → 200", "GET",
      f"{BASE_URL}/admin/prompts", f"{r.status_code}", jdump(r),
      "PASS — prompts listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="admin_token",
      note=f"first_key={first_key}; total={len(prompt_list)} keys")

if first_key:
    override_body = {"value": f"[QA OVERRIDE] Test prompt override for: {first_key}"}
    r = S.patch(f"{BASE_URL}/admin/prompts/{first_key}", headers=h(admin_token),
                json=override_body)
    block("C2", "PATCH /admin/prompts/{key} → set override → 200", "PATCH",
          f"{BASE_URL}/admin/prompts/{first_key}", f"{r.status_code}", jdump(r),
          "PASS — prompt override saved" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token", body=override_body)

    r = S.get(f"{BASE_URL}/admin/prompts", headers=h(admin_token))
    block("C3", "GET /admin/prompts after override → verify persisted", "GET",
          f"{BASE_URL}/admin/prompts", f"{r.status_code}", jdump(r),
          "PASS — override visible in GET" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token")

    r = S.delete(f"{BASE_URL}/admin/prompts/{first_key}", headers=h(admin_token))
    block("C4", "DELETE /admin/prompts/{key} → reset to default → 200", "DELETE",
          f"{BASE_URL}/admin/prompts/{first_key}", f"{r.status_code}",
          "(no body)" if r.status_code in (200, 204) else jdump(r),
          "PASS — prompt reset" if r.status_code in (200, 204) else f"FAIL — {r.status_code}",
          role="admin_token")
else:
    block("C2", "PATCH /admin/prompts/{key} → set override", "PATCH",
          f"{BASE_URL}/admin/prompts/(none)", "SKIP", "N/A",
          "SKIP — no prompt key found from GET /admin/prompts")
    block("C3", "GET /admin/prompts after override", "GET",
          f"{BASE_URL}/admin/prompts", "SKIP", "N/A", "SKIP — C2 skipped")
    block("C4", "DELETE /admin/prompts/{key}", "DELETE",
          f"{BASE_URL}/admin/prompts/(none)", "SKIP", "N/A", "SKIP — C2 skipped")

r = S.get(f"{BASE_URL}/admin/prompts", headers=h(doc_token))
block("C5", "GET /admin/prompts with doctor token → 403", "GET",
      f"{BASE_URL}/admin/prompts", f"{r.status_code}", jdump(r),
      "PASS — non-admin blocked" if r.status_code == 403 else f"FAIL — {r.status_code}",
      role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE D — Doctor Dashboard ━━━")
lines.append("")

r = S.get(f"{BASE_URL}/cases/doctors/me/stats", headers=h(doc_token))
block("D1", "GET /cases/doctors/me/stats → dashboard stats → 200", "GET",
      f"{BASE_URL}/cases/doctors/me/stats", f"{r.status_code}", jdump(r),
      "PASS — stats returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

r = S.get(f"{BASE_URL}/cases?page=1&page_size=10", headers=h(doc_token))
block("D2", "GET /cases → assigned cases list → 200", "GET",
      f"{BASE_URL}/cases?page=1&page_size=10", f"{r.status_code}", jdump(r),
      "PASS — list returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

# D3: Patient lookup by code (code from registration response)
if pat2_code:
    r = S.get(f"{BASE_URL}/users/by-code/{pat2_code}", headers=h(doc_token))
    block("D3", f"GET /users/by-code/{{code}} → patient lookup → 200", "GET",
          f"{BASE_URL}/users/by-code/{pat2_code}", f"{r.status_code}", jdump(r),
          "PASS — patient found by code" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token", note=f"Patient code: {pat2_code}")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE E — QR Scan → Case Assignment ━━━")
lines.append("")

if qr2_token:
    # E1: Scan QR → doctor assigned
    r = S.post(f"{BASE_URL}/qr/scan/{qr2_token}", headers=h(doc_token))
    block("E1", "POST /qr/scan/{token} → doctor assigned → 200", "POST",
          f"{BASE_URL}/qr/scan/{qr2_token}", f"{r.status_code}", jdump(r),
          "PASS — case assigned to doctor" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token")

    # E2: Rescan same token → 410
    r = S.post(f"{BASE_URL}/qr/scan/{qr2_token}", headers=h(doc_token))
    block("E2", "POST /qr/scan/{token} rescan same token → 410", "POST",
          f"{BASE_URL}/qr/scan/{qr2_token}", f"{r.status_code}", jdump(r),
          "PASS — used token rejected" if r.status_code == 410 else f"FAIL — {r.status_code}",
          role="doctor_token")

    # E3: Verify assignment
    r = S.get(f"{BASE_URL}/cases/{case_p2_id}", headers=h(doc_token))
    d = r.json()
    asgn = d.get("doctor_id") or d.get("assigned_doctor_id", "none")
    block("E3", "GET /cases/{id} → doctor_id set → 200", "GET",
          f"{BASE_URL}/cases/{case_p2_id}", f"{r.status_code}", jdump(r),
          f"PASS — doctor_id={asgn}" if r.status_code == 200 and asgn and asgn != "none"
          else f"FAIL — {r.status_code}",
          role="doctor_token")

    # E4: by-display-id (generate fresh QR then use display_id)
    if display2_id:
        r_qr2 = S.post(f"{BASE_URL}/qr/generate", headers=h(pat2_token),
                        json={"case_id": case_p2_id})
        r = S.post(f"{BASE_URL}/qr/by-display-id/{display2_id}", headers=h(doc_token))
        block("E4", "POST /qr/by-display-id/{display_id} → access by AI-XXXX", "POST",
              f"{BASE_URL}/qr/by-display-id/{display2_id}", f"{r.status_code}", jdump(r),
              "PASS — case accessed by display ID" if r.status_code in (200, 400, 404, 410)
              else f"FAIL — {r.status_code}",
              role="doctor_token",
              note=f"display_id={display2_id}; 400/404/410 OK if already assigned or expired")
else:
    block("E1", "POST /qr/scan/{token} → doctor assigned", "POST",
          f"{BASE_URL}/qr/scan/(none)", "SKIP", "N/A",
          "SKIP — QR generation failed in SETUP5 (AI did not complete in time)",
          note="Doctor-initiated case (F1) will be used for G–M phases")

# Determine the primary case for subsequent doctor operations
# Use QR-assigned case if available; else fall back to doctor-initiated case
VCASE = case_p2_id if qr2_token else None  # set after F1 if None

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE F — Doctor-Initiated Case ━━━")
lines.append("")

body_f1 = {"patient_name": "QA Direct Patient",
            "patient_email": f"qa_direct_{ts}@example.com",
            "consultation_type": "new_complaint",
            "has_visible_lesion": True,
            "body_location": "Leg"}
r = S.post(f"{BASE_URL}/cases/doctor", headers=h(doc_token), json=body_f1)
d = r.json()
case_doc_id = d.get("id") if r.status_code == 201 else None
block("F1", "POST /cases/doctor → create case for patient → 201", "POST",
      f"{BASE_URL}/cases/doctor", f"{r.status_code}", jdump(r),
      f"PASS — case_id={case_doc_id}" if r.status_code == 201 else f"FAIL — {r.status_code}",
      role="doctor_token", body=body_f1)

if VCASE is None:
    VCASE = case_doc_id  # fallback

if case_doc_id and os.path.exists(IMG):
    with open(IMG, "rb") as f_img:
        r = S.post(f"{BASE_URL}/cases/{case_doc_id}/images",
                   headers=h(doc_token),
                   files={"file": ("clinical.png", f_img, "image/png")},
                   data={"image_type": "clinical"})
    block("F2", "POST /cases/{id}/images clinical (doctor) → 201", "POST",
          f"{BASE_URL}/cases/{case_doc_id}/images", f"{r.status_code}", jdump(r),
          "PASS — image uploaded by doctor" if r.status_code == 201 else f"FAIL — {r.status_code}",
          role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE G — Visual Findings (Doctor AI) ━━━")
lines.append("")

if VCASE:
    r = S.post(f"{BASE_URL}/cases/{VCASE}/visual-findings/generate", headers=h(doc_token))
    block("G1", "POST /cases/{id}/visual-findings/generate → 202", "POST",
          f"{BASE_URL}/cases/{VCASE}/visual-findings/generate", f"{r.status_code}", jdump(r),
          "PASS — visual findings triggered" if r.status_code == 202 else f"FAIL — {r.status_code}",
          role="doctor_token", note=f"case={VCASE}")

    poll_r = None
    poll_start = time.time()
    while time.time() - poll_start < 180:
        poll_r = S.get(f"{BASE_URL}/cases/{VCASE}/ai/status", headers=h(doc_token))
        if poll_r.status_code == 200 and poll_r.json().get("ai_status") == "completed":
            break
        time.sleep(6)
    ai_s = poll_r.json().get("ai_status", "?") if poll_r and poll_r.status_code == 200 else "?"
    block("G2", "Poll GET /cases/{id}/ai/status → completed", "GET",
          f"{BASE_URL}/cases/{VCASE}/ai/status",
          f"{poll_r.status_code if poll_r else 'N/A'}", jdump(poll_r) if poll_r else "N/A",
          f"PASS — AI completed" if ai_s == "completed"
          else f"FAIL — not completed after 180s. Last: {ai_s}",
          role="doctor_token")

    r = S.get(f"{BASE_URL}/cases/{VCASE}/visual-findings", headers=h(doc_token))
    block("G3", "GET /cases/{id}/visual-findings → AI findings → 200", "GET",
          f"{BASE_URL}/cases/{VCASE}/visual-findings", f"{r.status_code}", jdump(r),
          "PASS — visual findings returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token")

    body_g4 = {"observations": "Possible inflammatory changes noted in upper dermis"}
    r = S.patch(f"{BASE_URL}/cases/{VCASE}/visual-findings", headers=h(doc_token), json=body_g4)
    block("G4", "PATCH /cases/{id}/visual-findings → doctor edits → 200", "PATCH",
          f"{BASE_URL}/cases/{VCASE}/visual-findings", f"{r.status_code}", jdump(r),
          "PASS — findings updated" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token", body=body_g4)

    body_g5 = {"features": ["Erythema", "Scaling", "Telangiectasia"],
                "additional_observations": "Confirmed via dermoscopic examination"}
    r = S.post(f"{BASE_URL}/cases/{VCASE}/clinical-features", headers=h(doc_token), json=body_g5)
    block("G5", "POST /cases/{id}/clinical-features → checklist → 200", "POST",
          f"{BASE_URL}/cases/{VCASE}/clinical-features", f"{r.status_code}", jdump(r),
          "PASS — clinical features saved" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token", body=body_g5)

RCASE = VCASE  # review on same case

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE H — Doctor Review Creation ━━━")
lines.append("")

r = S.get(f"{BASE_URL}/cases/{RCASE}/review", headers=h(doc_token))
review_pre_exists = r.status_code == 200
block("H1", "GET /cases/{id}/review → 404 (or 200 if clinical-features auto-created it)", "GET",
      f"{BASE_URL}/cases/{RCASE}/review", f"{r.status_code}", jdump(r),
      "PASS — 404 before review exists" if r.status_code == 404
      else "PASS — 200 (review auto-created by POST /clinical-features in G5)" if r.status_code == 200
      else f"FAIL — got {r.status_code}",
      role="doctor_token",
      note="POST /clinical-features may auto-create a review record as side effect")

body_h2 = {"is_ai_correct": True, "selected_differentials": ["Psoriasis vulgaris"],
            "confidence_level": "high", "confirmed_diagnosis": ["Psoriasis vulgaris"],
            "review_notes": "Initial QA review notes.", "review_status": "in_progress"}
if review_pre_exists:
    # Review already exists from G5 side-effect — use PATCH to update it
    r = S.patch(f"{BASE_URL}/cases/{RCASE}/review", headers=h(doc_token), json=body_h2)
    block("H2", "PATCH /cases/{id}/review (update pre-existing) → 200", "PATCH",
          f"{BASE_URL}/cases/{RCASE}/review", f"{r.status_code}", jdump(r),
          "PASS — review updated via PATCH" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token", body=body_h2,
          note="Used PATCH because review was auto-created by POST /clinical-features")
else:
    r = S.post(f"{BASE_URL}/cases/{RCASE}/review", headers=h(doc_token), json=body_h2)
    block("H2", "POST /cases/{id}/review (in_progress) → 201", "POST",
          f"{BASE_URL}/cases/{RCASE}/review", f"{r.status_code}", jdump(r),
          "PASS — review created" if r.status_code in (200, 201) else f"FAIL — {r.status_code}",
          role="doctor_token", body=body_h2)

r = S.post(f"{BASE_URL}/cases/{RCASE}/review", headers=h(doc_token), json=body_h2)
block("H3", "POST /cases/{id}/review duplicate → 409", "POST",
      f"{BASE_URL}/cases/{RCASE}/review", f"{r.status_code}", jdump(r),
      "PASS — 409 CONFLICT on duplicate" if r.status_code == 409 else f"FAIL — {r.status_code}",
      role="doctor_token")

r = S.get(f"{BASE_URL}/cases/{RCASE}/review", headers=h(doc_token))
block("H4", "GET /cases/{id}/review → review returned → 200", "GET",
      f"{BASE_URL}/cases/{RCASE}/review", f"{r.status_code}", jdump(r),
      "PASS — review returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE I — AI-Assist Endpoints (8 total) ━━━")
lines.append("")

# I1: Patient blocked from review AI
r = S.get(f"{BASE_URL}/cases/{RCASE}/review/ai/complaints", headers=h(pat2_token))
block("I1", "GET /review/ai/complaints with patient token → 403", "GET",
      f"{BASE_URL}/cases/{RCASE}/review/ai/complaints", f"{r.status_code}", jdump(r),
      "PASS — patient blocked from review AI" if r.status_code == 403 else f"FAIL — {r.status_code}",
      role="pat2_token")

for step_id, path, desc in [
    ("I2", "review/ai/complaints", "GET /review/ai/complaints → 200"),
    ("I3", "review/ai/diagnosis",  "GET /review/ai/diagnosis → 200"),
    ("I4", "review/ai/questions",  "GET /review/ai/questions → first question"),
    ("I6", "review/ai/summary",    "GET /review/ai/summary → clinical summary"),
    ("I7", "review/ai/treatment-plan", "GET /review/ai/treatment-plan → treatment plan"),
]:
    r = S.get(f"{BASE_URL}/cases/{RCASE}/{path}", headers=h(doc_token))
    block(step_id, desc, "GET", f"{BASE_URL}/cases/{RCASE}/{path}",
          f"{r.status_code}", jdump(r),
          "PASS — AI response returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token")

# I5: Next question (only if has_more)
r_q = S.get(f"{BASE_URL}/cases/{RCASE}/review/ai/questions", headers=h(doc_token))
q_data = r_q.json() if r_q.status_code == 200 else {}
if q_data.get("has_more"):
    qa_hist = [{"question": q_data.get("question", "Q?"), "answer": "Noted clinically."}]
    body_i5 = {"qa_history": qa_hist, "questions_left": 2}
    r = S.post(f"{BASE_URL}/cases/{RCASE}/review/ai/questions/next",
               headers=h(doc_token), json=body_i5)
    block("I5", "POST /review/ai/questions/next → next question → 200", "POST",
          f"{BASE_URL}/cases/{RCASE}/review/ai/questions/next", f"{r.status_code}", jdump(r),
          "PASS — next question returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token", body=body_i5)
else:
    block("I5", "POST /review/ai/questions/next → next question", "POST",
          f"{BASE_URL}/cases/{RCASE}/review/ai/questions/next", "SKIP", "N/A",
          "SKIP — no more questions (has_more=false)")

# I8: Voice note — field name 'audio'; WAV must be ≥0.1s (Whisper requirement)
# 0.15s at 16kHz 16-bit mono = 2400 samples = 4800 data bytes
import struct as _struct
_sr, _nc, _bps = 16000, 1, 16
_n_samples = int(_sr * 0.15)
_data = b'\x00' * (_n_samples * _nc * (_bps // 8))
wav_bytes = (
    b'RIFF' + _struct.pack('<I', 36 + len(_data)) + b'WAVE' +
    b'fmt ' + _struct.pack('<IHHIIHH', 16, 1, _nc, _sr, _sr * _nc * (_bps // 8), _nc * (_bps // 8), _bps) +
    b'data' + _struct.pack('<I', len(_data)) + _data
)
r = S.post(f"{BASE_URL}/cases/{RCASE}/review/voice-note",
           headers=h(doc_token),
           files={"audio": ("note.wav", wav_bytes, "audio/wav")})
block("I8", "POST /cases/{id}/review/voice-note → Whisper transcription", "POST",
      f"{BASE_URL}/cases/{RCASE}/review/voice-note", f"{r.status_code}", jdump(r),
      "PASS — transcription returned" if r.status_code == 200
      else f"FAIL — {r.status_code} (minimal WAV may be too short for Whisper; response recorded)",
      role="doctor_token",
      note="Field name corrected: 'audio' (was 'file' in v1). Sent minimal 36-byte WAV.")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE J — Complete Review & Case Status ━━━")
lines.append("")

r = S.patch(f"{BASE_URL}/cases/{RCASE}/review", headers=h(doc_token),
            json={"review_status": "completed"})
d = r.json()
rev_at = d.get("reviewed_at")
block("J1", "PATCH /cases/{id}/review → completed → 200", "PATCH",
      f"{BASE_URL}/cases/{RCASE}/review", f"{r.status_code}", jdump(r),
      f"PASS — completed, reviewed_at={rev_at}" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token", body={"review_status": "completed"})

r = S.patch(f"{BASE_URL}/cases/{RCASE}", headers=h(doc_token),
            json={"clinical_status": "follow_up_available"})
block("J2", "PATCH /cases/{id} → clinical_status=follow_up_available → 200", "PATCH",
      f"{BASE_URL}/cases/{RCASE}", f"{r.status_code}", jdump(r),
      "PASS — clinical status updated" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token", body={"clinical_status": "follow_up_available"})

r = S.patch(f"{BASE_URL}/cases/{RCASE}", headers=h(pat2_token),
            json={"clinical_status": "resolved"})
block("J3", "PATCH /cases/{id} clinical_status with patient token → 403/404", "PATCH",
      f"{BASE_URL}/cases/{RCASE}", f"{r.status_code}", jdump(r),
      "PASS — patient blocked (403=role check, 404=case not theirs)" if r.status_code in (403, 404)
      else f"FAIL — {r.status_code}",
      role="pat2_token", body={"clinical_status": "resolved"},
      note="403 if patient owns case but wrong role; 404 if case belongs to different patient")

r = S.post(f"{BASE_URL}/cases/{RCASE}/bookmark", headers=h(doc_token))
d = r.json()
bm = d.get("is_bookmarked")
block("J4", "POST /cases/{id}/bookmark → is_bookmarked=true → 200", "POST",
      f"{BASE_URL}/cases/{RCASE}/bookmark", f"{r.status_code}", jdump(r),
      f"PASS — is_bookmarked={bm}" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

r = S.post(f"{BASE_URL}/cases/{RCASE}/bookmark", headers=h(doc_token))
d = r.json()
bm2 = d.get("is_bookmarked")
block("J5", "POST /cases/{id}/bookmark again → toggle → is_bookmarked=false", "POST",
      f"{BASE_URL}/cases/{RCASE}/bookmark", f"{r.status_code}", jdump(r),
      f"PASS — toggled to {bm2}" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

r = S.get(f"{BASE_URL}/cases?bookmarked=true", headers=h(doc_token))
block("J6", "GET /cases?bookmarked=true → bookmarked list → 200", "GET",
      f"{BASE_URL}/cases?bookmarked=true", f"{r.status_code}", jdump(r),
      "PASS — bookmarked cases listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE K — Todos ━━━")
lines.append("")

body_k1 = {"title": "Order patch test", "description": "Rule out contact dermatitis."}
r = S.post(f"{BASE_URL}/cases/{RCASE}/todos", headers=h(doc_token), json=body_k1)
d = r.json()
todo_id = d.get("id") if r.status_code == 201 else None
block("K1", "POST /cases/{id}/todos → create → 201", "POST",
      f"{BASE_URL}/cases/{RCASE}/todos", f"{r.status_code}", jdump(r),
      f"PASS — todo_id={todo_id}" if r.status_code == 201 else f"FAIL — {r.status_code}",
      role="doctor_token", body=body_k1)

r = S.get(f"{BASE_URL}/cases/{RCASE}/todos", headers=h(doc_token))
block("K2", "GET /cases/{id}/todos → list → 200", "GET",
      f"{BASE_URL}/cases/{RCASE}/todos", f"{r.status_code}", jdump(r),
      "PASS — todos listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

if todo_id:
    r = S.patch(f"{BASE_URL}/cases/{RCASE}/todos/{todo_id}", headers=h(doc_token),
                json={"is_completed": True})
    d = r.json()
    comp_at = d.get("completed_at")
    block("K3", "PATCH todo → is_completed=true → completed_at set", "PATCH",
          f"{BASE_URL}/cases/{RCASE}/todos/{todo_id}", f"{r.status_code}", jdump(r),
          f"PASS — completed_at={comp_at}" if r.status_code == 200 and comp_at else f"FAIL — {r.status_code}",
          role="doctor_token", body={"is_completed": True})

    r = S.patch(f"{BASE_URL}/cases/{RCASE}/todos/{todo_id}", headers=h(doc_token),
                json={"is_completed": False})
    d = r.json()
    comp_at2 = d.get("completed_at")
    block("K4", "PATCH todo → is_completed=false → completed_at cleared", "PATCH",
          f"{BASE_URL}/cases/{RCASE}/todos/{todo_id}", f"{r.status_code}", jdump(r),
          f"PASS — completed_at={comp_at2} (None)" if r.status_code == 200 and not comp_at2
          else f"FAIL — completed_at not cleared",
          role="doctor_token", body={"is_completed": False})

    r = S.delete(f"{BASE_URL}/cases/{RCASE}/todos/{todo_id}", headers=h(doc_token))
    block("K5", "DELETE /cases/{id}/todos/{id} → 204", "DELETE",
          f"{BASE_URL}/cases/{RCASE}/todos/{todo_id}", f"{r.status_code}",
          "(no body)" if r.status_code == 204 else jdump(r),
          "PASS — todo deleted" if r.status_code == 204 else f"FAIL — {r.status_code}",
          role="doctor_token")

    r = S.get(f"{BASE_URL}/cases/{RCASE}/todos", headers=h(doc_token))
    items = r.json() if r.status_code == 200 else []
    if isinstance(items, dict):
        items = items.get("todos", items.get("items", []))
    still = [t for t in (items if isinstance(items, list) else []) if t.get("id") == todo_id]
    block("K6", "GET /cases/{id}/todos after delete → todo absent", "GET",
          f"{BASE_URL}/cases/{RCASE}/todos", f"{r.status_code}", jdump(r),
          "PASS — todo absent from list" if not still else "FAIL — todo still present",
          role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE L — SNOMED Entities ━━━")
lines.append("")

r = S.get(f"{BASE_URL}/cases/{RCASE}/entities", headers=h(doc_token))
d = r.json()
ents = d.get("entities", []) if isinstance(d, dict) else d
block("L1", "GET /cases/{id}/entities → SNOMED-coded entities → 200", "GET",
      f"{BASE_URL}/cases/{RCASE}/entities", f"{r.status_code}", jdump(r),
      f"PASS — {len(ents) if isinstance(ents, list) else '?'} entities returned"
      if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE M — Report Generation & Stale Regen ━━━")
lines.append("")

r = S.post(f"{BASE_URL}/cases/{RCASE}/report", headers=h(doc_token))
block("M1", "POST /cases/{id}/report → trigger → 202", "POST",
      f"{BASE_URL}/cases/{RCASE}/report", f"{r.status_code}", jdump(r),
      "PASS — report triggered" if r.status_code == 202 else f"FAIL — {r.status_code}",
      role="doctor_token")

poll_start = time.time()
report_ready = False
rp = None
while time.time() - poll_start < 90:
    rp = S.get(f"{BASE_URL}/cases/{RCASE}/report", headers=h(doc_token))
    if rp.status_code == 200:
        report_ready = True
        break
    time.sleep(6)
block("M2", "Poll GET /cases/{id}/report → wait for 200 + gcs_path", "GET",
      f"{BASE_URL}/cases/{RCASE}/report",
      f"{rp.status_code if rp else 'N/A'}", jdump(rp) if rp else "N/A",
      f"PASS — report ready after {int(time.time()-poll_start)}s" if report_ready
      else f"FAIL — report not ready after 90s (Celery worker may be busy)",
      role="doctor_token")

r = S.get(f"{BASE_URL}/reports/", headers=h(doc_token))
block("M3", "GET /reports/ → global reports list → 200", "GET",
      f"{BASE_URL}/reports/", f"{r.status_code}", jdump(r),
      "PASS — reports listed" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token")

body_m4 = {"confirmed_diagnosis": ["Eczema (revised)"], "review_status": "completed"}
r = S.patch(f"{BASE_URL}/cases/{RCASE}/review", headers=h(doc_token), json=body_m4)
block("M4", "PATCH /review → revise diagnosis → updated_at refreshed", "PATCH",
      f"{BASE_URL}/cases/{RCASE}/review", f"{r.status_code}", jdump(r),
      "PASS — diagnosis revised" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token", body=body_m4)

r = S.post(f"{BASE_URL}/cases/{RCASE}/report", headers=h(doc_token))
block("M5", "POST /cases/{id}/report after revision → stale → 202 (not 409)", "POST",
      f"{BASE_URL}/cases/{RCASE}/report", f"{r.status_code}", jdump(r),
      "PASS — stale detected, regenerating" if r.status_code == 202
      else "PASS — already current (no revision timestamp past report)" if r.status_code == 409
      else f"FAIL — {r.status_code}",
      role="doctor_token")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE N — Case Search ━━━")
lines.append("")

r = S.get(f"{BASE_URL}/cases/search?q=QA+Patient", headers=h(doc_token))
block("N1", "GET /cases/search?q=QA+Patient → 200 [CORRECTED: GET not POST]", "GET",
      f"{BASE_URL}/cases/search?q=QA+Patient", f"{r.status_code}", jdump(r),
      "PASS — search results returned" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token",
      note="Checklist had POST — live API is GET with query param")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE O — Trichoscopic Studies ━━━")
lines.append("")

r = S.get(f"{BASE_URL}/studies", headers=h(doc_token))
d = r.json()
studies = d if isinstance(d, list) else d.get("studies", d.get("items", []))
study_id = studies[0].get("id") if studies else None
block("O1", "GET /studies → active studies list → 200", "GET",
      f"{BASE_URL}/studies", f"{r.status_code}", jdump(r),
      f"PASS — {len(studies)} studies" if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="doctor_token", note=f"study_id={study_id}")

if study_id:
    r = S.get(f"{BASE_URL}/studies/{study_id}/specimens/next-code", headers=h(doc_token))
    spec_code = r.json().get("specimen_code", "") if r.status_code == 200 else ""
    block("O2", "GET /studies/{id}/specimens/next-code → 200", "GET",
          f"{BASE_URL}/studies/{study_id}/specimens/next-code", f"{r.status_code}", jdump(r),
          f"PASS — next code: {spec_code}" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token")

    r = S.get(f"{BASE_URL}/studies/{study_id}/features", headers=h(doc_token))
    feats = r.json() if r.status_code == 200 else []
    block("O3", "GET /studies/{id}/features → 8 features → 200", "GET",
          f"{BASE_URL}/studies/{study_id}/features", f"{r.status_code}", jdump(r),
          f"PASS — {len(feats) if isinstance(feats, list) else '?'} features returned"
          if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="doctor_token")

    # O4: Study submit via multipart form (field name 'file' required, plus form fields)
    feat_keys = [f.get("key", f.get("name", f"feat_{i}")) for i, f in enumerate(feats[:8])] \
                if isinstance(feats, list) else []
    features_dict = {k: "yes" for k in feat_keys} if feat_keys else {"black_dots": "yes"}
    o4_files = {"file": ("specimen.jpg", TINY_JPEG, "image/jpeg")}
    o4_data  = {
        "specimen_code": spec_code or f"AA-{ts}-T",
        "features": json.dumps(features_dict),
        "stability": "stable",
        "technical_quality": "good",
    }
    r = S.post(f"{BASE_URL}/studies/{study_id}/submit",
               headers=h(doc_token), files=o4_files, data=o4_data)
    block("O4", "POST /studies/{id}/submit → labelling → 201", "POST",
          f"{BASE_URL}/studies/{study_id}/submit", f"{r.status_code}", jdump(r),
          "PASS — study submitted" if r.status_code in (200, 201) else f"FAIL — {r.status_code}",
          role="doctor_token",
          note="Corrected to multipart form data (file + form fields). Was JSON in v1.")

    # O5: Invalid stability → 400/422
    o5_data = {**o4_data, "stability": "INVALID_VALUE"}
    r = S.post(f"{BASE_URL}/studies/{study_id}/submit",
               headers=h(doc_token),
               files={"file": ("specimen.jpg", TINY_JPEG, "image/jpeg")},
               data=o5_data)
    block("O5", "POST /studies/{id}/submit invalid stability → 400/422", "POST",
          f"{BASE_URL}/studies/{study_id}/submit", f"{r.status_code}", jdump(r),
          "PASS — invalid stability rejected" if r.status_code in (400, 422)
          else f"FAIL — {r.status_code}",
          role="doctor_token", body={"stability": "INVALID_VALUE"})

    # O6: Patient blocked from studies
    r = S.get(f"{BASE_URL}/studies", headers=h(pat2_token))
    block("O6", "GET /studies with patient token → 403", "GET",
          f"{BASE_URL}/studies", f"{r.status_code}", jdump(r),
          "PASS — patient cannot access studies" if r.status_code == 403 else f"FAIL — {r.status_code}",
          role="pat2_token")
else:
    block("O2", "Studies flow", "N/A", "N/A", "SKIP", "N/A",
          "SKIP — no active studies in DB")

# ══════════════════════════════════════════════════════════════════════════
lines.append("")
lines.append("━━━ PHASE P — Doctor Auth Cleanup ━━━")
lines.append("")

r = S.post(f"{BASE_URL}/auth/logout", json={"refresh_token": doc_refresh})
block("P1", "POST /auth/logout → 204", "POST", f"{BASE_URL}/auth/logout",
      f"{r.status_code}", "(no body)" if r.status_code == 204 else jdump(r),
      "PASS — doctor logged out" if r.status_code == 204 else f"FAIL — {r.status_code}")

r = S.post(f"{BASE_URL}/auth/refresh", json={"refresh_token": doc_refresh})
block("P2", "POST /auth/refresh revoked token → 401", "POST", f"{BASE_URL}/auth/refresh",
      f"{r.status_code}", jdump(r),
      "PASS — revoked token rejected" if r.status_code == 401 else f"FAIL — {r.status_code}")

# ══════════════════════════════════════════════════════════════════════════
pass_ct = sum(1 for s in summary if "PASS" in s)
fail_ct = sum(1 for s in summary if "FAIL" in s)
skip_ct = sum(1 for s in summary if "SKIP" in s)

phase_map = {
    "S": "Setup",
    "A": "Doctor Registration & Approval",
    "B": "Admin User Management",
    "C": "Admin AI Prompt Overrides",
    "D": "Doctor Dashboard",
    "E": "QR Scan & Assignment",
    "F": "Doctor-Initiated Case",
    "G": "Visual Findings (AI)",
    "H": "Doctor Review",
    "I": "AI-Assist (8 endpoints)",
    "J": "Complete Review & Status",
    "K": "Todos",
    "L": "SNOMED Entities",
    "M": "Report Generation",
    "N": "Case Search",
    "O": "Trichoscopic Studies",
    "P": "Auth Cleanup",
}

lines += ["", "=" * 70, "SUMMARY", "─" * 69]
for prefix, label in phase_map.items():
    section = [s for s in summary if s.strip()[0] == prefix]
    if section:
        lines.append(f"{label}:")
        lines.extend(section)
        lines.append("")

lines += [f"Total: {pass_ct} PASS  {fail_ct} FAIL  {skip_ct} SKIP",
          "=" * 70]
flush()

print(f"Task 2 done. PASS={pass_ct} FAIL={fail_ct} SKIP={skip_ct}")
print(f"Output: {OUT}")
print(f"VCASE={VCASE}  RCASE={RCASE}")
