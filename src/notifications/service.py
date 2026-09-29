"""
notifications/service.py — Push Notification Helpers
=====================================================

send_push_notification  — send to one FCM token (sync, for use in Celery tasks)
send_push_multicast     — send to multiple FCM tokens at once

Both functions are synchronous (firebase-admin SDK is sync).
They are always called from Celery tasks, never directly from async FastAPI handlers.

SILENT FAILURE POLICY
---------------------
FCM errors (invalid token, quota exceeded, network) are logged but never
raise. A failed push notification must never break the business operation
that triggered it (e.g. a doctor approval must still succeed even if FCM
is temporarily unavailable).

DATA PAYLOAD
------------
Every notification includes a `data` dict (string→string) alongside the
visible title/body. The Flutter app reads `data["type"]` to decide how to
navigate:
    "doctor_pending"  → admin navigates to pending doctors list
    "account_approved"→ doctor navigates to home
    "account_rejected"→ doctor navigates to re-register
    "ai_complete"     → patient navigates to case screen
    "review_complete" → patient navigates to review screen
    "status_update"   → patient navigates to case screen
    "red_flag"        → admin navigates to case detail
"""

from src.logger import get_logger

logger = get_logger(__name__)


def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> bool:
    """
    Send a push notification to a single FCM device token.

    Returns True on success, False on any error (never raises).
    """
    if not token or not token.strip():
        return False

    try:
        from src.notifications.firebase import get_messaging
        msg = get_messaging()

        message = msg.Message(
            notification=msg.Notification(title=title, body=body),
            data=data or {},
            token=token,
            android=msg.AndroidConfig(priority="high"),
            apns=msg.APNSConfig(
                payload=msg.APNSPayload(
                    aps=msg.Aps(sound="default"),
                ),
            ),
        )
        response = msg.send(message)
        logger.info("push_sent", message_id=response, title=title)
        return True

    except Exception as exc:
        logger.warning("push_failed", error=str(exc), title=title)
        return False


def send_push_multicast(
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> int:
    """
    Send the same push notification to multiple FCM device tokens.

    Returns the count of successful sends. Never raises.
    """
    valid = [t for t in tokens if t and t.strip()]
    if not valid:
        return 0

    try:
        from src.notifications.firebase import get_messaging
        msg = get_messaging()

        message = msg.MulticastMessage(
            notification=msg.Notification(title=title, body=body),
            data=data or {},
            tokens=valid,
            android=msg.AndroidConfig(priority="high"),
            apns=msg.APNSConfig(
                payload=msg.APNSPayload(
                    aps=msg.Aps(sound="default"),
                ),
            ),
        )
        response = msg.send_each_for_multicast(message)
        success = response.success_count
        failure = response.failure_count
        logger.info(
            "push_multicast_sent",
            title=title,
            total=len(valid),
            success=success,
            failure=failure,
        )
        return success

    except Exception as exc:
        logger.warning("push_multicast_failed", error=str(exc), title=title)
        return 0
