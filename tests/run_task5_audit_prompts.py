"""Task 5 — Prompt Versioning + Case Access Audit Log + Blur Check"""
import requests, time, json, os

BASE_URL    = "http://localhost:8880/api/v1"
OUT         = r"d:\@White Mastery Systems\Derm AI\ai_derm_cliniq_server\Results\Result 1705\05_Audit_Prompt_Versioning.txt"
ADMIN_EMAIL = "dev@bdcode.in"
ADMIN_PASS  = "@Dev_bdcode.in"

ts        = int(time.time())
P_EMAIL   = f"qa_audit_{ts}@example.com"
P_PASS    = "TestPass@99"

S = requests.Session()

admin_token = pat_token = pat_user_id = case_id = None
lines   = []
summary = []


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


def block(sid, desc, method, url, status, resp, result,
          role=None, body=None, note=None):
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
    summary.append(f"  {sid:<10} {tag}  {desc}")
    flush()


# ── HEADER ──────────────────────────────────────────────────────────────────
lines += [
    "TASK 5 — PROMPT VERSIONING + CASE ACCESS AUDIT LOG + BLUR CHECK",
    "Tested On : 2026-05-17",
    f"Server    : {BASE_URL}",
    "",
    "TEST STRATEGY",
    "─" * 69,
    "1. Prompt versioning (Issue #199):",
    "   Set v1 override → set v2 override → verify history has v1",
    "   → rollback → verify current restored to v1 → verify history empty",
    "   → verify 400 on rollback with empty history",
    "   → verify 403 for non-admin on history and rollback endpoints",
    "2. Case access audit log (Issue #195):",
    "   Create patient + case → admin views case detail",
    "   → admin reads audit log → verify CASE_ACCESSED entry present",
    "3. Blur check (Issue #223):",
    "   Verify quality.py blur check is active (code inspection result)",
    "",
    "ISSUES COVERED",
    "─" * 69,
    "  #199  [MH15] Prompt versioning allows rollback",
    "  #195  [MH14] Audit logs capture case access",
    "  #223  [MH?]  Blur/quality check active on image upload",
    "",
]
flush()

# ════════════════════════════════════════════════════════════════════════════
# SETUP — Admin login
# ════════════════════════════════════════════════════════════════════════════
r = S.post(f"{BASE_URL}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
admin_token = jparse(r).get("access_token")
block("SETUP", "Admin login", "POST", f"{BASE_URL}/auth/login",
      f"{r.status_code}", jdump(r),
      "PASS — admin token obtained" if r.status_code == 200 else f"FAIL — {r.status_code}",
      body={"email": ADMIN_EMAIL, "password": "***"})

# ════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Prompt Versioning (Issue #199)
# ════════════════════════════════════════════════════════════════════════════
lines.append(""); lines.append("━━━ SECTION 1 — Prompt Versioning ━━━"); lines.append("")
flush()

# 1A — List prompts, pick a key to use for testing
r = S.get(f"{BASE_URL}/admin/prompts", headers=h(admin_token))
prompt_key = None
if r.status_code == 200:
    prompts = jparse(r).get("prompts", [])
    if prompts:
        prompt_key = prompts[0]["key"]  # use first available key
block("1A", "GET /admin/prompts — list all prompts", "GET",
      f"{BASE_URL}/admin/prompts", f"{r.status_code}", jdump(r),
      f"PASS — {len(jparse(r).get('prompts', []))} keys returned, using key='{prompt_key}'"
      if r.status_code == 200 else f"FAIL — {r.status_code}",
      role="admin_token",
      note=f"Test key selected: {prompt_key}")

# 1B — Reset first (clean state), then set v1
if prompt_key and admin_token:
    S.delete(f"{BASE_URL}/admin/prompts/{prompt_key}", headers=h(admin_token))  # clean

    body = {"value": "VERSION_1_TEST_PROMPT — Set by Task 5 test run"}
    r = S.patch(f"{BASE_URL}/admin/prompts/{prompt_key}", headers=h(admin_token), json=body)
    block("1B", f"PATCH /admin/prompts/{{key}} — set v1 override", "PATCH",
          f"{BASE_URL}/admin/prompts/{prompt_key}", f"{r.status_code}", jdump(r),
          "PASS — v1 override set" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token", body=body)
else:
    block("1B", "PATCH /admin/prompts/{key} — set v1 override", "PATCH",
          f"{BASE_URL}/admin/prompts/<key>", "SKIP", "N/A",
          "SKIP — no prompt key or admin token", note="SETUP failed")

# 1C — Set v2 (this pushes v1 into history)
if prompt_key and admin_token:
    body = {"value": "VERSION_2_TEST_PROMPT — Set by Task 5 test run (overwrites v1)"}
    r = S.patch(f"{BASE_URL}/admin/prompts/{prompt_key}", headers=h(admin_token), json=body)
    block("1C", "PATCH /admin/prompts/{key} — set v2 (v1 → history)", "PATCH",
          f"{BASE_URL}/admin/prompts/{prompt_key}", f"{r.status_code}", jdump(r),
          "PASS — v2 set, v1 should now be in history" if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token", body=body)
else:
    block("1C", "PATCH /admin/prompts/{key} — set v2", "PATCH",
          f"{BASE_URL}/admin/prompts/<key>", "SKIP", "N/A", "SKIP — no prompt key")

# 1D — GET history — expect 1 entry (v1)
if prompt_key and admin_token:
    r = S.get(f"{BASE_URL}/admin/prompts/{prompt_key}/history", headers=h(admin_token))
    d = jparse(r)
    history = d.get("history", [])
    has_v1 = any("VERSION_1" in e.get("value", "") for e in history)
    block("1D", "GET /admin/prompts/{key}/history — expect 1 entry (v1)", "GET",
          f"{BASE_URL}/admin/prompts/{prompt_key}/history", f"{r.status_code}", jdump(r),
          f"PASS — {len(history)} history entry, v1 present"
          if r.status_code == 200 and len(history) == 1 and has_v1
          else f"FAIL — status={r.status_code} history_count={len(history)} v1_present={has_v1}",
          role="admin_token")
else:
    block("1D", "GET /admin/prompts/{key}/history", "GET",
          f"{BASE_URL}/admin/prompts/<key>/history", "SKIP", "N/A", "SKIP — no prompt key")

# 1E — POST rollback — should restore v1
if prompt_key and admin_token:
    r = S.post(f"{BASE_URL}/admin/prompts/{prompt_key}/rollback", headers=h(admin_token))
    d = jparse(r)
    restored_val = d.get("value", "")
    block("1E", "POST /admin/prompts/{key}/rollback — restore v1", "POST",
          f"{BASE_URL}/admin/prompts/{prompt_key}/rollback", f"{r.status_code}", jdump(r),
          "PASS — rollback succeeded, v1 restored"
          if r.status_code == 200 and "VERSION_1" in restored_val
          else f"FAIL — status={r.status_code} value='{restored_val[:60]}'",
          role="admin_token")
else:
    block("1E", "POST /admin/prompts/{key}/rollback", "POST",
          f"{BASE_URL}/admin/prompts/<key>/rollback", "SKIP", "N/A", "SKIP — no prompt key")

# 1F — GET prompts — verify current value = v1
if prompt_key and admin_token:
    r = S.get(f"{BASE_URL}/admin/prompts", headers=h(admin_token))
    d = jparse(r)
    current = next((p for p in d.get("prompts", []) if p["key"] == prompt_key), {})
    current_val = current.get("value", "")
    block("1F", "GET /admin/prompts — verify current value is v1", "GET",
          f"{BASE_URL}/admin/prompts", f"{r.status_code}", jdump(r),
          "PASS — current value is v1 after rollback"
          if r.status_code == 200 and "VERSION_1" in current_val
          else f"FAIL — current_value='{current_val[:60]}'",
          role="admin_token")
else:
    block("1F", "GET /admin/prompts — verify current value", "GET",
          f"{BASE_URL}/admin/prompts", "SKIP", "N/A", "SKIP — no prompt key")

# 1G — GET history after rollback — expect empty
if prompt_key and admin_token:
    r = S.get(f"{BASE_URL}/admin/prompts/{prompt_key}/history", headers=h(admin_token))
    d = jparse(r)
    history = d.get("history", [])
    block("1G", "GET /admin/prompts/{key}/history — expect empty after rollback", "GET",
          f"{BASE_URL}/admin/prompts/{prompt_key}/history", f"{r.status_code}", jdump(r),
          f"PASS — history is empty after rollback"
          if r.status_code == 200 and len(history) == 0
          else f"FAIL — status={r.status_code} history_count={len(history)}",
          role="admin_token")
else:
    block("1G", "GET /admin/prompts/{key}/history — expect empty", "GET",
          f"{BASE_URL}/admin/prompts/<key>/history", "SKIP", "N/A", "SKIP — no prompt key")

# ROLL_EMPTY — Rollback with empty history → 400
if prompt_key and admin_token:
    r = S.post(f"{BASE_URL}/admin/prompts/{prompt_key}/rollback", headers=h(admin_token))
    block("ROLL_EMPTY", "POST rollback with no history → 400", "POST",
          f"{BASE_URL}/admin/prompts/{prompt_key}/rollback", f"{r.status_code}", jdump(r),
          "PASS — 400 returned when no history"
          if r.status_code == 400
          else f"FAIL — expected 400, got {r.status_code}",
          role="admin_token",
          note="No history remaining after 1G, rollback should return 400")
else:
    block("ROLL_EMPTY", "POST rollback with no history → 400", "POST",
          f"{BASE_URL}/admin/prompts/<key>/rollback", "SKIP", "N/A", "SKIP — no prompt key")

# AUTH_H — Non-admin patient cannot access history
if prompt_key:
    r_reg = S.post(f"{BASE_URL}/auth/register/patient", json={
        "full_name": f"Audit Test Patient {ts}",
        "email": P_EMAIL,
        "password": P_PASS,
    })
    r_login = S.post(f"{BASE_URL}/auth/login", json={"email": P_EMAIL, "password": P_PASS})
    pat_token = jparse(r_login).get("access_token")
    pat_user_id = jparse(r_reg).get("user", {}).get("id") or jparse(r_reg).get("id")

    r = S.get(f"{BASE_URL}/admin/prompts/{prompt_key}/history", headers=h(pat_token))
    block("AUTH_H", "GET /admin/prompts/{key}/history as patient → 403", "GET",
          f"{BASE_URL}/admin/prompts/{prompt_key}/history", f"{r.status_code}", jdump(r),
          "PASS — 403 INSUFFICIENT_ROLE" if r.status_code == 403 else f"FAIL — expected 403, got {r.status_code}",
          role="patient_token")
else:
    block("AUTH_H", "GET history as patient → 403", "GET",
          f"{BASE_URL}/admin/prompts/<key>/history", "SKIP", "N/A", "SKIP — no prompt key")

# AUTH_R — Non-admin patient cannot rollback
if prompt_key and pat_token:
    r = S.post(f"{BASE_URL}/admin/prompts/{prompt_key}/rollback", headers=h(pat_token))
    block("AUTH_R", "POST /admin/prompts/{key}/rollback as patient → 403", "POST",
          f"{BASE_URL}/admin/prompts/{prompt_key}/rollback", f"{r.status_code}", jdump(r),
          "PASS — 403 INSUFFICIENT_ROLE" if r.status_code == 403 else f"FAIL — expected 403, got {r.status_code}",
          role="patient_token")
else:
    block("AUTH_R", "POST rollback as patient → 403", "POST",
          f"{BASE_URL}/admin/prompts/<key>/rollback", "SKIP", "N/A", "SKIP — no prompt key or patient token")

# ════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Case Access Audit Log (Issue #195)
# ════════════════════════════════════════════════════════════════════════════
lines.append(""); lines.append("━━━ SECTION 2 — Case Access Audit Log ━━━"); lines.append("")
flush()

# 2A — Patient already registered above; update profile (DOB + gender required for case creation)
if pat_token:
    profile_body = {"date_of_birth": "1990-05-15", "gender": "male"}
    r_profile = S.patch(f"{BASE_URL}/users/me", headers=h(pat_token), json=profile_body)
    block("2A_PROFILE", "PATCH /users/me — set DOB + gender (required for case creation)", "PATCH",
          f"{BASE_URL}/users/me", f"{r_profile.status_code}", jdump(r_profile),
          "PASS — profile updated" if r_profile.status_code == 200 else f"FAIL — {r_profile.status_code}",
          role="patient_token", body=profile_body)

# 2A — Create case
if pat_token:
    body = {
        "consultation_type": "new_complaint",
        "has_visible_lesion": False,
        "is_for_self": True,
        "consent_ai_analysis": True,
        "consent_research": False,
        "body_location": "Arm",
        "presenting_complaint": "Audit log test case — dry patch on arm",
    }
    r = S.post(f"{BASE_URL}/cases", headers=h(pat_token), json=body)
    case_id = jparse(r).get("id")
    block("2A", "POST /cases — create case for audit test", "POST",
          f"{BASE_URL}/cases", f"{r.status_code}", jdump(r),
          f"PASS — case created id={case_id}" if r.status_code == 201 else f"FAIL — {r.status_code}",
          role="patient_token", body=body)
else:
    block("2A", "POST /cases — create case", "POST",
          f"{BASE_URL}/cases", "SKIP", "N/A",
          "SKIP — no patient token", note="Patient registration failed")

# 2B — Admin views case detail (triggers CASE_ACCESSED audit entry)
if case_id and admin_token:
    r = S.get(f"{BASE_URL}/admin/cases/{case_id}", headers=h(admin_token))
    block("2B", "GET /admin/cases/{case_id} — admin view (triggers CASE_ACCESSED)", "GET",
          f"{BASE_URL}/admin/cases/{case_id}", f"{r.status_code}", jdump(r),
          "PASS — case detail returned (CASE_ACCESSED should be logged)"
          if r.status_code == 200 else f"FAIL — {r.status_code}",
          role="admin_token",
          note="This call should write a CASE_ACCESSED entry to case_audit_logs")
else:
    block("2B", "GET /admin/cases/{case_id} — admin view", "GET",
          f"{BASE_URL}/admin/cases/<case_id>", "SKIP", "N/A",
          "SKIP — no case_id or admin token")

# 2C — Admin reads audit log — verify CASE_ACCESSED entry
if case_id and admin_token:
    time.sleep(0.5)  # allow commit to complete
    r = S.get(f"{BASE_URL}/admin/cases/{case_id}/audit-log", headers=h(admin_token))
    d = jparse(r)
    items = d.get("items", [])
    has_case_accessed = any(e.get("event_type") == "case_accessed" for e in items)
    block("2C", "GET /admin/cases/{case_id}/audit-log — verify CASE_ACCESSED entry", "GET",
          f"{BASE_URL}/admin/cases/{case_id}/audit-log", f"{r.status_code}", jdump(r),
          f"PASS — CASE_ACCESSED entry found in audit log ({len(items)} total events)"
          if r.status_code == 200 and has_case_accessed
          else f"FAIL — status={r.status_code} has_case_accessed={has_case_accessed} total_items={len(items)}",
          role="admin_token",
          note="event_type=case_accessed should appear from the 2B admin view call")
else:
    block("2C", "GET /admin/cases/{case_id}/audit-log — verify CASE_ACCESSED", "GET",
          f"{BASE_URL}/admin/cases/<case_id>/audit-log", "SKIP", "N/A",
          "SKIP — no case_id or admin token")

# 2D — Second admin view — audit log should accumulate (2 CASE_ACCESSED entries)
if case_id and admin_token:
    S.get(f"{BASE_URL}/admin/cases/{case_id}", headers=h(admin_token))  # second view
    time.sleep(0.5)
    r = S.get(f"{BASE_URL}/admin/cases/{case_id}/audit-log", headers=h(admin_token))
    d = jparse(r)
    items = d.get("items", [])
    case_accessed_count = sum(1 for e in items if e.get("event_type") == "case_accessed")
    # Note: 2D itself also triggers CASE_ACCESSED, so expect ≥2
    block("2D", "Second admin view — audit log accumulates", "GET",
          f"{BASE_URL}/admin/cases/{case_id}/audit-log", f"{r.status_code}", jdump(r),
          f"PASS — {case_accessed_count} CASE_ACCESSED entries (accumulates per view)"
          if r.status_code == 200 and case_accessed_count >= 2
          else f"FAIL — case_accessed_count={case_accessed_count}",
          role="admin_token",
          note="Each GET /admin/cases/{id} call adds one CASE_ACCESSED entry")
else:
    block("2D", "Second admin view — audit log accumulates", "GET",
          f"{BASE_URL}/admin/cases/<case_id>/audit-log", "SKIP", "N/A", "SKIP — no case_id")

# ════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Blur Check Verification (Issue #223)
# ════════════════════════════════════════════════════════════════════════════
lines.append(""); lines.append("━━━ SECTION 3 — Blur Check Verification ━━━"); lines.append("")
flush()

QUALITY_FILE = r"d:\@White Mastery Systems\Derm AI\ai_derm_cliniq_server\src\images\quality.py"
sep()
lines.append("[BLUR | Verify blur check is active in quality.py]")
lines.append(f"REQUEST  : code inspection of {QUALITY_FILE}")
try:
    with open(QUALITY_FILE, "r") as f:
        content = f.read()
    # Check that the raise line is NOT commented out
    check_active = (
        "raise ImageQualityException" in content
        and "too_blurry" in content
        and "# if laplacian_variance" not in content  # key: not commented
    )
    result = (
        "PASS — blur check is active (raise ImageQualityException uncommented)"
        if check_active
        else "FAIL — blur check appears commented out — re-enable in quality.py"
    )
    lines.append(f"STATUS   : CODE_READ")
    lines.append(f"RESPONSE : check_active={check_active}")
    lines.append(f"RESULT   : {result}")
except Exception as e:
    lines.append(f"STATUS   : ERROR")
    lines.append(f"RESPONSE : {e}")
    lines.append(f"RESULT   : FAIL — could not read quality.py")
lines.append("")
tag = "PASS" if "PASS" in lines[-2] else "FAIL"
summary.append(f"  {'BLUR':<10} {tag}  Blur check active in quality.py")
flush()

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
sep()
lines.append("SUMMARY")
lines.append("─" * 69)
lines.append("")
lines.append("Prompt Versioning (#199):")
for s in summary:
    if any(s.strip().startswith(k) for k in ["SETUP", "1A", "1B", "1C", "1D", "1E", "1F", "1G", "ROLL_EMPTY", "AUTH_H", "AUTH_R"]):
        lines.append(s)
lines.append("")
lines.append("Case Access Audit Log (#195):")
for s in summary:
    if any(s.strip().startswith(k) for k in ["2A_PROFILE", "2A", "2B", "2C", "2D"]):
        lines.append(s)
lines.append("")
lines.append("Blur Check (#223):")
for s in summary:
    if s.strip().startswith("BLUR"):
        lines.append(s)

pass_count = sum(1 for s in summary if "  PASS  " in s)
fail_count = sum(1 for s in summary if "  FAIL  " in s)
skip_count = sum(1 for s in summary if "  SKIP  " in s)
lines.append("")
lines.append(f"Total: {pass_count} PASS  {fail_count} FAIL  {skip_count} SKIP")
sep()
flush()

print(f"Done. Results written to:\n{OUT}")
print(f"Total: {pass_count} PASS  {fail_count} FAIL  {skip_count} SKIP")
