"""
tests/unit/test_notifications.py — Push Notification Service Tests
===================================================================

Tests for:
    src/notifications/service.py  — send_push_notification, send_push_multicast
    src/workers/tasks/notifications.py — Celery tasks (mocked Firebase + DB)

MOCK STRATEGY
-------------
Firebase Admin SDK is never initialised in tests. All calls to get_messaging()
are patched so no credentials file or network access is needed.

Celery tasks are called directly as regular Python functions (not via .delay())
with their DB calls patched via _run_async mock.
"""

from unittest.mock import MagicMock, patch


# ================================================================== #
# send_push_notification — single device
# ================================================================== #

class TestSendPushNotification:

    def _make_mock_messaging(self, send_return="projects/test/messages/abc123"):
        """Build a mock firebase_admin.messaging module."""
        msg = MagicMock()
        msg.Message.return_value = MagicMock()
        msg.Notification.return_value = MagicMock()
        msg.AndroidConfig.return_value = MagicMock()
        msg.APNSConfig.return_value = MagicMock()
        msg.APNSPayload.return_value = MagicMock()
        msg.Aps.return_value = MagicMock()
        msg.send.return_value = send_return
        return msg

    def test_returns_true_on_success(self):
        from src.notifications.service import send_push_notification

        mock_msg = self._make_mock_messaging()
        with patch("src.notifications.firebase.get_messaging", return_value=mock_msg):
            result = send_push_notification(
                token="valid-fcm-token",
                title="Test",
                body="Hello",
            )

        assert result is True
        mock_msg.send.assert_called_once()

    def test_passes_title_and_body(self):
        from src.notifications.service import send_push_notification

        mock_msg = self._make_mock_messaging()
        with patch("src.notifications.firebase.get_messaging", return_value=mock_msg):
            send_push_notification(token="tok", title="My Title", body="My Body")

        mock_msg.Notification.assert_called_once_with(title="My Title", body="My Body")

    def test_passes_data_payload(self):
        from src.notifications.service import send_push_notification

        mock_msg = self._make_mock_messaging()
        with patch("src.notifications.firebase.get_messaging", return_value=mock_msg):
            send_push_notification(
                token="tok",
                title="T",
                body="B",
                data={"type": "ai_complete", "case_id": "abc"},
            )

        call_kwargs = mock_msg.Message.call_args[1]
        assert call_kwargs["data"] == {"type": "ai_complete", "case_id": "abc"}

    def test_returns_false_on_firebase_error(self):
        from src.notifications.service import send_push_notification

        mock_msg = self._make_mock_messaging()
        mock_msg.send.side_effect = Exception("FCM quota exceeded")

        with patch("src.notifications.firebase.get_messaging", return_value=mock_msg):
            result = send_push_notification(token="tok", title="T", body="B")

        assert result is False

    def test_empty_token_returns_false_without_calling_firebase(self):
        from src.notifications.service import send_push_notification

        with patch("src.notifications.firebase.get_messaging") as mock_get:
            result = send_push_notification(token="", title="T", body="B")

        assert result is False
        mock_get.assert_not_called()

    def test_whitespace_token_returns_false(self):
        from src.notifications.service import send_push_notification

        with patch("src.notifications.firebase.get_messaging") as mock_get:
            result = send_push_notification(token="   ", title="T", body="B")

        assert result is False
        mock_get.assert_not_called()

    def test_none_token_returns_false(self):
        """None token is handled gracefully — no exception raised."""
        from src.notifications.service import send_push_notification

        with patch("src.notifications.firebase.get_messaging") as mock_get:
            result = send_push_notification(token=None, title="T", body="B")

        assert result is False
        mock_get.assert_not_called()


# ================================================================== #
# send_push_multicast — multiple devices
# ================================================================== #

class TestSendPushMulticast:

    def _make_mock_messaging(self, success=2, failure=0):
        msg = MagicMock()
        msg.MulticastMessage.return_value = MagicMock()
        msg.Notification.return_value = MagicMock()
        msg.AndroidConfig.return_value = MagicMock()
        msg.APNSConfig.return_value = MagicMock()
        msg.APNSPayload.return_value = MagicMock()
        msg.Aps.return_value = MagicMock()
        batch_response = MagicMock()
        batch_response.success_count = success
        batch_response.failure_count = failure
        msg.send_each_for_multicast.return_value = batch_response
        return msg

    def test_returns_success_count(self):
        from src.notifications.service import send_push_multicast

        mock_msg = self._make_mock_messaging(success=3, failure=1)
        with patch("src.notifications.firebase.get_messaging", return_value=mock_msg):
            count = send_push_multicast(
                tokens=["tok1", "tok2", "tok3", "tok4"],
                title="Alert",
                body="Message",
            )

        assert count == 3

    def test_empty_token_list_returns_zero_without_firebase(self):
        from src.notifications.service import send_push_multicast

        with patch("src.notifications.firebase.get_messaging") as mock_get:
            count = send_push_multicast(tokens=[], title="T", body="B")

        assert count == 0
        mock_get.assert_not_called()

    def test_filters_out_empty_tokens(self):
        from src.notifications.service import send_push_multicast

        mock_msg = self._make_mock_messaging(success=2)
        with patch("src.notifications.firebase.get_messaging", return_value=mock_msg):
            count = send_push_multicast(
                tokens=["valid1", "", "valid2", "   "],
                title="T",
                body="B",
            )

        # Only valid tokens are sent; the multicast call is made
        mock_msg.send_each_for_multicast.assert_called_once()
        call_kwargs = mock_msg.MulticastMessage.call_args[1]
        assert call_kwargs["tokens"] == ["valid1", "valid2"]

    def test_returns_zero_on_firebase_error(self):
        from src.notifications.service import send_push_multicast

        mock_msg = self._make_mock_messaging()
        mock_msg.send_each_for_multicast.side_effect = Exception("Network error")

        with patch("src.notifications.firebase.get_messaging", return_value=mock_msg):
            count = send_push_multicast(tokens=["tok1"], title="T", body="B")

        assert count == 0


# ================================================================== #
# Celery Tasks — notify_doctor_approved
# ================================================================== #

class TestNotifyDoctorApproved:

    def test_sends_notification_when_token_present(self):
        from src.workers.tasks.notifications import notify_doctor_approved

        with patch("src.workers.tasks.notifications._run_async", return_value="fcm-token-xyz"), \
             patch("src.workers.tasks.notifications.send_push_notification", return_value=True) as mock_send:
            result = notify_doctor_approved("doctor-uuid-123")

        assert result["status"] == "sent"
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["data"]["type"] == "account_approved"

    def test_skips_when_no_token(self):
        from src.workers.tasks.notifications import notify_doctor_approved

        with patch("src.workers.tasks.notifications._run_async", return_value=None):
            result = notify_doctor_approved("doctor-uuid-456")

        assert result["status"] == "skipped"
        assert result["reason"] == "no_fcm_token"

    def test_returns_failed_when_send_fails(self):
        from src.workers.tasks.notifications import notify_doctor_approved

        with patch("src.workers.tasks.notifications._run_async", return_value="token"), \
             patch("src.workers.tasks.notifications.send_push_notification", return_value=False):
            result = notify_doctor_approved("doctor-uuid-789")

        assert result["status"] == "failed"


# ================================================================== #
# Celery Tasks — notify_admins_doctor_registered
# ================================================================== #

class TestNotifyAdminsDoctorRegistered:

    def test_sends_multicast_to_all_admins(self):
        from src.workers.tasks.notifications import notify_admins_doctor_registered

        admin_tokens = ["admin-tok-1", "admin-tok-2"]
        with patch("src.workers.tasks.notifications._run_async", return_value=admin_tokens), \
             patch("src.workers.tasks.notifications.send_push_multicast", return_value=2) as mock_send:
            result = notify_admins_doctor_registered("Dr. Smith", "doctor-id-abc")

        assert result["status"] == "sent"
        assert result["count"] == 2
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        assert "Dr. Smith" in call_kwargs["body"]

    def test_returns_sent_with_zero_when_no_admins(self):
        from src.workers.tasks.notifications import notify_admins_doctor_registered

        with patch("src.workers.tasks.notifications._run_async", return_value=[]), \
             patch("src.workers.tasks.notifications.send_push_multicast", return_value=0):
            result = notify_admins_doctor_registered("Dr. Nobody", "doc-id")

        assert result["status"] == "sent"
        assert result["count"] == 0


# ================================================================== #
# Celery Tasks — notify_patient_ai_complete
# ================================================================== #

class TestNotifyPatientAiComplete:

    def test_sends_to_patient_with_case_data(self):
        from src.workers.tasks.notifications import notify_patient_ai_complete

        with patch("src.workers.tasks.notifications._run_async", return_value="patient-fcm-tok"), \
             patch("src.workers.tasks.notifications.send_push_notification", return_value=True) as mock_send:
            result = notify_patient_ai_complete("patient-id", "case-id", "AI-0042")

        assert result["status"] == "sent"
        call_kwargs = mock_send.call_args[1]
        assert call_kwargs["data"]["type"] == "ai_complete"
        assert call_kwargs["data"]["case_id"] == "case-id"
        assert "AI-0042" in call_kwargs["body"]

    def test_skips_when_patient_has_no_token(self):
        from src.workers.tasks.notifications import notify_patient_ai_complete

        with patch("src.workers.tasks.notifications._run_async", return_value=None):
            result = notify_patient_ai_complete("patient-id", "case-id", "AI-0042")

        assert result["status"] == "skipped"


# ================================================================== #
# Celery Tasks — notify_patient_status_update
# ================================================================== #

class TestNotifyPatientStatusUpdate:

    def test_human_readable_status_label_in_body(self):
        from src.workers.tasks.notifications import notify_patient_status_update

        with patch("src.workers.tasks.notifications._run_async", return_value="tok"), \
             patch("src.workers.tasks.notifications.send_push_notification", return_value=True) as mock_send:
            notify_patient_status_update("p-id", "c-id", "AI-0001", "follow_up_available")

        body = mock_send.call_args[1]["body"]
        assert "Follow-up Available" in body

    def test_unknown_status_is_title_cased(self):
        from src.workers.tasks.notifications import notify_patient_status_update

        with patch("src.workers.tasks.notifications._run_async", return_value="tok"), \
             patch("src.workers.tasks.notifications.send_push_notification", return_value=True) as mock_send:
            notify_patient_status_update("p-id", "c-id", "AI-0001", "some_new_status")

        body = mock_send.call_args[1]["body"]
        assert "Some New Status" in body
