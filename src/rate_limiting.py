"""
rate_limiting.py — API Rate Limiter (slowapi)
==============================================

WHY RATE LIMITING?
------------------
Without rate limiting, a single client could:
- Flood the AI endpoints and run up our Gemini/OpenAI bill.
- Perform brute-force attacks on the /auth/login endpoint.
- DoS the server by exhausting the DB connection pool.

We use `slowapi`, which is the FastAPI-native port of Flask-Limiter.
It uses Redis to track request counts per client (by IP address).

HOW IT WORKS
------------
1. A `Limiter` instance is created with a key function (get_remote_address).
2. The limiter is attached to the FastAPI app via middleware.
3. Individual routes are decorated with @limiter.limit("N/minute").
4. If a client exceeds the limit, slowapi raises a 429 response automatically.

USAGE IN ROUTES
---------------
    from src.rate_limiting import limiter
    from fastapi import Request

    @router.post("/login/doctor")
    @limiter.limit("10/minute")   ← stricter limit on auth endpoints
    async def login(request: Request, ...):
        ...

    @router.get("/cases")
    @limiter.limit("60/minute")   ← standard limit
    async def list_cases(request: Request, ...):
        ...

NOTE: The `request: Request` parameter is required by slowapi even if
the route handler does not use it directly.
"""

from starlette.requests import Request

from slowapi import Limiter
from slowapi.util import get_remote_address

from src.config import settings


def _get_client_ip(request: Request) -> str:
    """
    Return the real client IP, honouring X-Forwarded-For from a reverse proxy.

    Behind nginx / GCP Load Balancer, the direct peer is the proxy — not the
    browser. X-Forwarded-For is a comma-separated list; the leftmost entry is
    the original client (added by the first trusted proxy). We take that first
    value so the rate-limit key is the actual client, not the proxy IP.

    Falls back to the direct peer address when the header is absent (i.e. in
    development without a proxy in front).
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(
    key_func=_get_client_ip,
    default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE}/minute"],
    storage_uri=settings.REDIS_URL,   # Use Redis so limits persist across workers
    swallow_errors=True,              # Prevent Redis errors from crashing requests
    enabled=settings.RATE_LIMIT_ENABLED,
)