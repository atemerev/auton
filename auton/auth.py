"""API key authentication middleware.

Supports two modes:
- If AUTON_API_KEY is set (comma-separated for multiple keys), all
  non-public endpoints require a valid key via X-API-Key header or
  Authorization: Bearer <key>.
- If AUTON_API_KEY is not set, authentication is disabled (dev mode).
"""

import logging
import os

from fastapi import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Paths that never require authentication
PUBLIC_PATHS = frozenset({"/api/health", "/health", "/docs", "/openapi.json", "/redoc"})


def get_valid_keys() -> set[str]:
    """Load API keys from AUTON_API_KEY env var (comma-separated)."""
    raw = os.environ.get("AUTON_API_KEY", "")
    if not raw:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


def _extract_key(request: Request) -> str | None:
    """Extract API key from request headers."""
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


async def auth_middleware(request: Request, call_next):
    """Verify API key on all non-public endpoints.

    Skips authentication for OPTIONS requests (CORS preflight),
    public paths (/health, /docs), and when no keys are configured.
    """
    # Always allow preflight and public paths
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    valid_keys = get_valid_keys()
    if not valid_keys:
        return await call_next(request)

    key = _extract_key(request)
    if not key or key not in valid_keys:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key. Provide X-API-Key header or Authorization: Bearer <key>."},
        )

    return await call_next(request)


def log_auth_status() -> None:
    """Log authentication configuration on startup."""
    keys = get_valid_keys()
    if keys:
        logger.info(f"Authentication enabled ({len(keys)} API key(s) configured)")
    else:
        logger.warning(
            "Authentication DISABLED — no AUTON_API_KEY set. "
            "Set AUTON_API_KEY env var to require API keys."
        )
