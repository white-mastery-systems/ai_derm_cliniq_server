"""
notifications/firebase.py — Firebase Admin SDK Initialisation
=============================================================

Initialises the Firebase Admin app once per process (lazy singleton).
All other notification code imports `get_messaging()` from here.

WHY LAZY INIT?
--------------
Calling firebase_admin.initialize_app() at import time would fail in
tests and environments where the credentials file does not exist.
Lazy init means the SDK is only wired up the first time a push is
actually sent — tests that mock the task never touch this module.

CREDENTIALS
-----------
The service account JSON lives at FIREBASE_CREDENTIALS_PATH (config.py).
Default: credentials/firebase_service_account.json
"""

import threading

import firebase_admin
from firebase_admin import credentials, messaging

from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
_initialised = False


def _ensure_initialised() -> None:
    global _initialised
    if _initialised:
        return
    with _lock:
        if _initialised:
            return
        try:
            cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
            firebase_admin.initialize_app(cred)
            _initialised = True
            logger.info("firebase_admin_initialised", path=settings.FIREBASE_CREDENTIALS_PATH)
        except Exception as exc:
            logger.error("firebase_admin_init_failed", error=str(exc))
            raise


def get_messaging() -> messaging:
    """Return the firebase_admin.messaging module, initialising the app if needed."""
    _ensure_initialised()
    return messaging
