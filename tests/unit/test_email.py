"""
tests/unit/test_email.py — Email Utility & Task Tests
======================================================

Tests for:
    src/core/email.py            — render_visit_email, send_email
    src/workers/tasks/email.py   — send_visit_email_task

APPROACH
--------
- send_email: mock smtplib.SMTP_SSL to avoid real network calls
- send_visit_email_task: mock send_email + _load_case_and_patient
- render_visit_email: assert HTML contains expected strings
- QR generation: assert task is correctly skipped when no credentials
"""

from unittest.mock import MagicMock, patch


# ================================================================== #
# render_visit_email
# ================================================================== #

def test_render_visit_email_contains_patient_name():
    from src.core.email import render_visit_email
    html = render_visit_email(
        patient_name="Jane Smith",
        display_id="AI-9001",
        patient_url="https://example.com/case/abc123",
    )
    assert "Jane Smith" in html


def test_render_visit_email_contains_case_id_prefix():
    from src.core.email import render_visit_email
    html = render_visit_email(
        patient_name="Patient X",
        display_id="AI-9001",
        patient_url="https://example.com/case/abcdef123456",
    )
    assert "AI-9001" in html


def test_render_visit_email_contains_patient_url():
    from src.core.email import render_visit_email
    url = "https://example.com/case/test-id"
    html = render_visit_email(
        patient_name="Test Patient",
        display_id="AI-9002",
        patient_url=url,
    )
    assert url in html


def test_render_visit_email_contains_qr_instruction():
    from src.core.email import render_visit_email
    html = render_visit_email("P", "AI-9003", "http://x.com")
    assert "QR" in html or "qr" in html.lower()


# ================================================================== #
# send_email — credential checks
# ================================================================== #

def test_send_email_skips_when_no_credentials():
    """Returns False immediately when GMAIL credentials are not set."""
    from src.core.email import send_email
    with patch("src.core.email.settings") as mock_settings:
        mock_settings.GMAIL_USER = ""
        mock_settings.GMAIL_APP_PASSWORD = ""
        result = send_email("to@example.com", "Subject", "<p>body</p>")
    assert result is False


def test_send_email_success():
    """Returns True when SMTP connection and send succeed."""
    from src.core.email import send_email

    mock_server = MagicMock()
    mock_server.__enter__ = MagicMock(return_value=mock_server)
    mock_server.__exit__ = MagicMock(return_value=False)

    with patch("src.core.email.settings") as mock_settings, \
         patch("src.core.email.smtplib.SMTP_SSL", return_value=mock_server):
        mock_settings.GMAIL_USER = "test@gmail.com"
        mock_settings.GMAIL_APP_PASSWORD = "app-password"
        result = send_email("patient@example.com", "Visit Summary", "<p>hello</p>")

    assert result is True
    mock_server.login.assert_called_once_with("test@gmail.com", "app-password")
    mock_server.sendmail.assert_called_once()


def test_send_email_with_attachment():
    """Attachment bytes are added to the MIME message."""
    import smtplib
    from src.core.email import send_email

    mock_server = MagicMock()
    mock_server.__enter__ = MagicMock(return_value=mock_server)
    mock_server.__exit__ = MagicMock(return_value=False)

    with patch("src.core.email.settings") as mock_settings, \
         patch("src.core.email.smtplib.SMTP_SSL", return_value=mock_server):
        mock_settings.GMAIL_USER = "test@gmail.com"
        mock_settings.GMAIL_APP_PASSWORD = "app-password"
        result = send_email(
            to_email="patient@example.com",
            subject="Test",
            html_body="<p>body</p>",
            attachments=[("qr.png", b"\x89PNG...", "image/png")],
        )

    assert result is True
    # sendmail was called with a message that includes the attachment
    call_args = mock_server.sendmail.call_args
    raw_message = call_args[0][2]   # third positional arg to sendmail
    assert "qr.png" in raw_message


def test_send_email_auth_failure_returns_false():
    """SMTP auth failure returns False without raising."""
    import smtplib
    from src.core.email import send_email

    mock_server = MagicMock()
    mock_server.__enter__ = MagicMock(return_value=mock_server)
    mock_server.__exit__ = MagicMock(return_value=False)
    mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")

    with patch("src.core.email.settings") as mock_settings, \
         patch("src.core.email.smtplib.SMTP_SSL", return_value=mock_server):
        mock_settings.GMAIL_USER = "test@gmail.com"
        mock_settings.GMAIL_APP_PASSWORD = "wrong-password"
        result = send_email("to@example.com", "Subject", "<p>body</p>")

    assert result is False


def test_send_email_smtp_exception_returns_false():
    """Generic SMTP error returns False without raising."""
    import smtplib
    from src.core.email import send_email

    mock_server = MagicMock()
    mock_server.__enter__ = MagicMock(return_value=mock_server)
    mock_server.__exit__ = MagicMock(return_value=False)
    mock_server.sendmail.side_effect = smtplib.SMTPException("Connection lost")

    with patch("src.core.email.settings") as mock_settings, \
         patch("src.core.email.smtplib.SMTP_SSL", return_value=mock_server):
        mock_settings.GMAIL_USER = "test@gmail.com"
        mock_settings.GMAIL_APP_PASSWORD = "app-password"
        result = send_email("to@example.com", "Subject", "<p>body</p>")

    assert result is False


# ================================================================== #
# _generate_qr_png
# ================================================================== #

def test_generate_qr_png_returns_png_bytes():
    """QR generation returns valid PNG bytes (starts with PNG header)."""
    from src.workers.tasks.email import _generate_qr_png
    result = _generate_qr_png("https://example.com/api/v1/qr/scan/test-token")
    assert isinstance(result, bytes)
    assert len(result) > 100
    # PNG magic bytes
    assert result[:4] == b"\x89PNG"


# ================================================================== #
# send_visit_email_task
# ================================================================== #

def _mock_run_async(return_value):
    """
    Helper: patches _run_async so it returns return_value AND properly
    closes the coroutine passed to it (avoids 'coroutine never awaited' warning).
    """
    def _side_effect(coro):
        coro.close()   # close the coroutine to silence the ResourceWarning
        return return_value
    return _side_effect


def test_email_task_sends_on_success():
    """Task calls send_email and returns status=sent."""
    from src.workers.tasks.email import send_visit_email_task

    with patch("src.workers.tasks.email._run_async", side_effect=_mock_run_async(("patient@example.com", "Jane Smith", "AI-9001"))), \
         patch("src.workers.tasks.email._generate_qr_png", return_value=b"\x89PNG..."), \
         patch("src.workers.tasks.email.send_email", return_value=True) as mock_send, \
         patch("src.workers.tasks.email.settings") as mock_settings:

        mock_settings.FRONTEND_URL = "https://example.com"
        result = send_visit_email_task("case-uuid-123", "token-abc")

    assert result["status"] == "sent"
    assert result["to"] == "patient@example.com"
    mock_send.assert_called_once()


def test_email_task_returns_failed_when_case_not_found():
    """Task returns failed if case cannot be loaded."""
    from src.workers.tasks.email import send_visit_email_task

    with patch("src.workers.tasks.email._run_async", side_effect=_mock_run_async(None)):
        result = send_visit_email_task("nonexistent-case", "token-xyz")

    assert result["status"] == "failed"
    assert result["reason"] == "case_not_found"


def test_email_task_returns_failed_when_send_fails():
    """Task returns failed if SMTP send fails."""
    from src.workers.tasks.email import send_visit_email_task

    with patch("src.workers.tasks.email._run_async", side_effect=_mock_run_async(("patient@example.com", "Jane Smith", "AI-9002"))), \
         patch("src.workers.tasks.email._generate_qr_png", return_value=b"\x89PNG..."), \
         patch("src.workers.tasks.email.send_email", return_value=False), \
         patch("src.workers.tasks.email.settings") as mock_settings:

        mock_settings.FRONTEND_URL = "https://example.com"
        result = send_visit_email_task("case-uuid-456", "token-def")

    assert result["status"] == "failed"
    assert result["reason"] == "smtp_error"


def test_email_task_sends_without_qr_on_qr_failure():
    """If QR generation fails, email is still sent (without attachment)."""
    from src.workers.tasks.email import send_visit_email_task

    with patch("src.workers.tasks.email._run_async", side_effect=_mock_run_async(("patient@example.com", "Jane Smith", "AI-9003"))), \
         patch("src.workers.tasks.email._generate_qr_png", side_effect=Exception("qr lib error")), \
         patch("src.workers.tasks.email.send_email", return_value=True) as mock_send, \
         patch("src.workers.tasks.email.settings") as mock_settings:

        mock_settings.FRONTEND_URL = "https://example.com"
        result = send_visit_email_task("case-uuid-789", "token-ghi")

    assert result["status"] == "sent"
    call_kwargs = mock_send.call_args
    assert call_kwargs[1]["attachments"] == []
