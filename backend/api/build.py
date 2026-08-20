"""
Build API — check frontend build status and trigger re-builds from the diagnostic UI.
"""

from fastapi import APIRouter, Request
from auth import verify_token
from frontend_builder import get_frontend_builder

router = APIRouter(tags=["build"])


@router.get("/build-status")
@router.get("/build/status")
async def get_build_status(request: Request):
    """Return the current frontend build status and progress.

    Sanitizes sensitive error details when unauthenticated.
    """
    auth_err = await verify_token(request)
    is_authenticated = (auth_err is None)

    builder = get_frontend_builder()
    return builder.get_status(include_sensitive=is_authenticated)


@router.post("/build-retry")
@router.post("/build/retry")
async def retry_frontend_build():
    """Trigger a retry of the frontend build.

    Protected by BearerTokenAuthMiddleware when API_TOKEN is configured.
    """
    builder = get_frontend_builder()
    return await builder.retry_build()
