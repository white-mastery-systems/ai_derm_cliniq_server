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

from slowapi import Limiter
from slowapi.util import get_remote_address

from src.config import settings

# The limiter uses the client's IP address as the key.
# In production behind a reverse proxy (nginx, GCP Load Balancer),
# you should use the X-Forwarded-For header instead:
#   key_func=lambda request: request.headers.get("X-Forwarded-For", get_remote_address(request))
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE}/minute"],
    storage_uri=settings.REDIS_URL,   # Use Redis so limits persist across workers
)