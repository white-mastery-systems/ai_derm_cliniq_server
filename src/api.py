"""
api.py — Central Router Registration
======================================

WHY A CENTRAL ROUTER FILE?
---------------------------
Without this, main.py would need to import every single router.
As we add more modules (cases, images, AI, reports...), main.py
would grow into a mess of imports.

Instead:
- Each module owns its own router (e.g., src/auth/controller.py)
- This file imports all of them and registers them on the app
- main.py only calls `include_all_routers(app)` — one line

PREFIX STRATEGY
---------------
All API routes are versioned under /api/v1/.
This means if we ever need breaking changes, we can add /api/v2/
without removing /api/v1/ — existing Flutter clients keep working.

TAGS
----
The `tags` parameter groups endpoints in the auto-generated Swagger UI
at http://localhost:8000/docs. Each tag creates a collapsible section.

AS WE BUILD MORE MODULES, ADD THEIR ROUTERS HERE.
"""

from fastapi import FastAPI


def include_all_routers(app: FastAPI) -> None:
    """
    Register all module routers on the FastAPI application.
    Import routers lazily inside the function to avoid circular imports
    at module load time.
    """
    # Auth — /api/v1/auth
    from src.auth.controller import router as auth_router
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["Auth"])

    # Users — /api/v1/users
    from src.users.controller import router as users_router
    app.include_router(users_router, prefix="/api/v1/users", tags=["Users"])

    # ------------------------------------------------------------------ #
    # The routers below will be uncommented as each layer is built.
    # ------------------------------------------------------------------ #

    # Cases — /api/v1/cases
    from src.cases.controller import router as cases_router
    app.include_router(cases_router, prefix="/api/v1/cases", tags=["Cases"])

    # Images — /api/v1/cases/{case_id}/images
    from src.images.controller import router as images_router
    app.include_router(images_router, prefix="/api/v1/cases/{case_id}/images", tags=["Images"])

    # Conversations — /api/v1/cases/{case_id}/chat
    from src.conversations.controller import router as conv_router
    app.include_router(conv_router, prefix="/api/v1/cases/{case_id}/chat", tags=["Conversations"])

    # AI Analysis — /api/v1/cases/{case_id}/ai
    from src.ai.controller import router as ai_router
    app.include_router(ai_router, prefix="/api/v1/cases/{case_id}/ai", tags=["AI Analysis"])

    # Doctor Review — /api/v1/cases/{case_id}/review
    from src.doctor_review.controller import router as review_router
    app.include_router(review_router, prefix="/api/v1/cases/{case_id}", tags=["Doctor Review"])

    # QR Codes — /api/v1/qr
    from src.qr.controller import router as qr_router
    app.include_router(qr_router, prefix="/api/v1/qr", tags=["QR Codes"])

    # Reports — /api/v1/cases/{case_id}/report
    from src.reports.controller import router as reports_router
    app.include_router(reports_router, prefix="/api/v1/cases/{case_id}", tags=["Reports"])

    # Admin — /api/v1/admin
    # from src.admin.controller import router as admin_router
    # app.include_router(admin_router, prefix="/api/v1/admin", tags=["Admin"])
