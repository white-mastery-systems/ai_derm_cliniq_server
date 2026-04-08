"""
core/email.py — Email Sender
=============================

Low-level email utility used by Celery tasks.
Sends HTML emails via Gmail SMTP using Python's built-in smtplib.

WHY smtplib (not a third-party library)?
-----------------------------------------
smtplib is part of the Python standard library — no extra dependency.
We're already using it in the old app (email_utils.py). Jinja2 (already
in requirements.txt) handles the HTML template.

GRACEFUL DEGRADATION
---------------------
If GMAIL_USER or GMAIL_APP_PASSWORD is not set, send_email() logs a
warning and returns without raising. This means:
- Development / test environments work without email credentials
- The QR generation flow succeeds even if email is misconfigured
- No patient-facing error from a non-critical background step

HOW TO GET A GMAIL APP PASSWORD
---------------------------------
1. Go to myaccount.google.com → Security → 2-Step Verification (enable it)
2. Search "App passwords" → generate one for "Mail"
3. Set GMAIL_USER=aidermcliniq@gmail.com and GMAIL_APP_PASSWORD=<16-char code>

USAGE (from Celery tasks only)
--------------------------------
    from src.core.email import send_email, render_visit_email

    html = render_visit_email(patient_name, case_id, patient_url, qr_bytes)
    send_email(
        to_email="patient@example.com",
        subject="Your AiDerm Cliniq Visit Summary",
        html_body=html,
        attachments=[("visit_qr.png", qr_bytes, "image/png")],
    )
"""

import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import Template

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# HTML Email Template
# ------------------------------------------------------------------ #

_VISIT_EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { font-family: Arial, sans-serif; background-color: #f4f6f9; margin: 0; padding: 0; }
    .wrapper { max-width: 600px; margin: 30px auto; background: #ffffff;
               border-radius: 8px; overflow: hidden;
               box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
    .header { background-color: #1a6b5a; padding: 30px 40px; }
    .header h1 { color: #ffffff; margin: 0; font-size: 22px; font-weight: 600; }
    .header p  { color: #a8d5ca; margin: 4px 0 0; font-size: 14px; }
    .body { padding: 32px 40px; color: #333333; }
    .body p { line-height: 1.6; margin: 0 0 16px; }
    .case-box { background: #f0f9f6; border-left: 4px solid #1a6b5a;
                padding: 16px 20px; border-radius: 4px; margin: 20px 0; }
    .case-box .label { font-size: 12px; color: #888; text-transform: uppercase;
                       letter-spacing: 0.5px; margin-bottom: 4px; }
    .case-box .value { font-size: 20px; font-weight: 700; color: #1a6b5a;
                       letter-spacing: 1px; }
    .btn { display: inline-block; background-color: #1a6b5a; color: #ffffff !important;
           text-decoration: none; padding: 12px 28px; border-radius: 6px;
           font-size: 15px; font-weight: 600; margin: 8px 0; }
    .divider { border: none; border-top: 1px solid #e8ecf0; margin: 24px 0; }
    .qr-section { text-align: center; padding: 20px 0; }
    .qr-section p { color: #555; font-size: 14px; }
    .footer { background-color: #f4f6f9; padding: 20px 40px;
              text-align: center; color: #999; font-size: 12px; }
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <h1>AiDerm Cliniq</h1>
      <p>AI-Powered Dermatology Consultation</p>
    </div>
    <div class="body">
      <p>Dear <strong>{{ patient_name }}</strong>,</p>
      <p>
        Thank you for using AiDerm Cliniq. Your consultation has been submitted
        successfully and our AI has completed its initial analysis.
      </p>

      <div class="case-box">
        <div class="label">Your Case Number</div>
        <div class="value">{{ case_id[:8].upper() }}</div>
      </div>

      <hr class="divider">

      <p><strong>Access Your Case</strong></p>
      <p>
        View your complete case summary — including uploaded images, AI analysis,
        and any doctor notes — by clicking below:
      </p>
      <a href="{{ patient_url }}" class="btn">View My Case</a>
      <p style="font-size:12px; color:#999;">
        Or copy this link: <a href="{{ patient_url }}">{{ patient_url }}</a>
      </p>

      <hr class="divider">

      <div class="qr-section">
        <p><strong>Doctor QR Code</strong></p>
        <p>
          Show the attached QR code to your doctor during your consultation.
          They can scan it to instantly access your case on their device.
        </p>
        <p style="color:#1a6b5a; font-weight:600;">
          QR code is attached to this email as <em>visit_qr.png</em>
        </p>
      </div>

      <hr class="divider">

      <p style="font-size:13px; color:#777;">
        This email was generated automatically. Please do not reply.
        If you have questions, contact us at
        <a href="mailto:support@aidermcliniq.com">support@aidermcliniq.com</a>.
      </p>
    </div>
    <div class="footer">
      &copy; 2026 AiDerm Cliniq &nbsp;|&nbsp; All rights reserved
    </div>
  </div>
</body>
</html>
"""


_PASSWORD_RESET_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{font-family:Arial,sans-serif;background:#f4f6f9;margin:0;padding:0}
  .wrapper{max-width:600px;margin:30px auto;background:#fff;border-radius:8px;
           box-shadow:0 2px 8px rgba(0,0,0,.08);overflow:hidden}
  .header{background:#1a6b5a;padding:28px 40px}
  .header h1{color:#fff;margin:0;font-size:20px}
  .body{padding:32px 40px;color:#333;line-height:1.6}
  .btn{display:inline-block;background:#1a6b5a;color:#fff!important;
       text-decoration:none;padding:12px 28px;border-radius:6px;font-weight:600;margin:12px 0}
  .warning{background:#fff8e1;border-left:4px solid #f59e0b;padding:12px 16px;
           border-radius:4px;font-size:13px;color:#78350f;margin:16px 0}
  .footer{background:#f4f6f9;padding:16px 40px;text-align:center;color:#999;font-size:12px}
</style></head><body>
<div class="wrapper">
  <div class="header"><h1>AiDerm Cliniq — Password Reset</h1></div>
  <div class="body">
    <p>Hi <strong>{{ name }}</strong>,</p>
    <p>We received a request to reset your password. Click the button below to set a new one:</p>
    <a href="{{ reset_url }}" class="btn">Reset My Password</a>
    <p style="font-size:12px;color:#999">Or copy this link: <a href="{{ reset_url }}">{{ reset_url }}</a></p>
    <div class="warning">This link expires in <strong>15 minutes</strong> and can only be used once.
    If you did not request a password reset, you can safely ignore this email.</div>
  </div>
  <div class="footer">&copy; 2026 AiDerm Cliniq</div>
</div></body></html>
"""

_EMAIL_VERIFY_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{font-family:Arial,sans-serif;background:#f4f6f9;margin:0;padding:0}
  .wrapper{max-width:600px;margin:30px auto;background:#fff;border-radius:8px;
           box-shadow:0 2px 8px rgba(0,0,0,.08);overflow:hidden}
  .header{background:#1a6b5a;padding:28px 40px}
  .header h1{color:#fff;margin:0;font-size:20px}
  .body{padding:32px 40px;color:#333;line-height:1.6}
  .btn{display:inline-block;background:#1a6b5a;color:#fff!important;
       text-decoration:none;padding:12px 28px;border-radius:6px;font-weight:600;margin:12px 0}
  .footer{background:#f4f6f9;padding:16px 40px;text-align:center;color:#999;font-size:12px}
</style></head><body>
<div class="wrapper">
  <div class="header"><h1>AiDerm Cliniq — Verify Your Email</h1></div>
  <div class="body">
    <p>Hi <strong>{{ name }}</strong>,</p>
    <p>Please verify your email address to activate your account:</p>
    <a href="{{ verify_url }}" class="btn">Verify Email Address</a>
    <p style="font-size:12px;color:#999">Or copy this link: <a href="{{ verify_url }}">{{ verify_url }}</a></p>
    <p style="font-size:13px;color:#777">This link expires in 24 hours.</p>
  </div>
  <div class="footer">&copy; 2026 AiDerm Cliniq</div>
</div></body></html>
"""


def render_password_reset_email(name: str, reset_url: str) -> str:
    """Render the password reset HTML email."""
    return Template(_PASSWORD_RESET_TEMPLATE).render(name=name, reset_url=reset_url)


def render_email_verify_email(name: str, verify_url: str) -> str:
    """Render the email verification HTML email."""
    return Template(_EMAIL_VERIFY_TEMPLATE).render(name=name, verify_url=verify_url)


def render_visit_email(patient_name: str, case_id: str, patient_url: str) -> str:
    """Render the visit summary HTML email body."""
    return Template(_VISIT_EMAIL_TEMPLATE).render(
        patient_name=patient_name,
        case_id=case_id,
        patient_url=patient_url,
    )


# ------------------------------------------------------------------ #
# SMTP Send
# ------------------------------------------------------------------ #

def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> bool:
    """
    Send an HTML email via Gmail SMTP.

    Parameters
    ----------
    to_email    : recipient address
    subject     : email subject line
    html_body   : rendered HTML string
    attachments : list of (filename, bytes_data, mime_type) tuples

    Returns True on success, False on any failure (never raises).
    """
    if not settings.GMAIL_USER or not settings.GMAIL_APP_PASSWORD:
        logger.warning(
            "email_skipped_no_credentials",
            to=to_email,
            reason="GMAIL_USER or GMAIL_APP_PASSWORD not configured",
        )
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = settings.GMAIL_USER
    msg["To"] = to_email
    msg["Subject"] = subject

    # HTML body
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Attachments
    for filename, data, mime_type in (attachments or []):
        main_type, sub_type = mime_type.split("/", 1)
        part = MIMEBase(main_type, sub_type)
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
            server.login(settings.GMAIL_USER, settings.GMAIL_APP_PASSWORD)
            server.sendmail(settings.GMAIL_USER, [to_email], msg.as_string())

        logger.info("email_sent", to=to_email, subject=subject)
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error("email_auth_failed", to=to_email,
                     hint="Check GMAIL_USER and GMAIL_APP_PASSWORD in .env")
        return False
    except smtplib.SMTPException as exc:
        logger.error("email_smtp_error", to=to_email, error=str(exc))
        return False
    except Exception as exc:
        logger.error("email_unexpected_error", to=to_email, error=str(exc))
        return False
