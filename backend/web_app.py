# pyright: reportMissingImports=false

"""
Unified Web Application Builder for Nocturne Memory.

This module consolidates the FastAPI REST API registration, middleware setup,
and static frontend SPA fallback routing. It is shared across all web-facing
entry points (main.py, run_sse.py, and mcp_server.py).
"""

import os
import sys
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

import config as _cfg
from auth import BearerTokenAuthMiddleware, get_cors_config
from namespace_middleware import NamespaceMiddleware
from locales.middleware import LocaleMiddleware
from api import review_router, browse_router, maintenance_router, settings_router, presets_router
from health import router as health_router, health_check
from config import ConfigWriteError
from db import get_db_manager, close_db

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@asynccontextmanager
async def default_lifespan(app: Starlette):
    """Default lifespan that initializes the database and auto-promotes presets."""
    _cfg.ensure_config_exists()
    try:
        db_manager = get_db_manager()
        await db_manager.init_db()
        print("Database initialized (default lifespan).")
        
        # Auto-promote config.json boot_uris into presets table on first run
        from db import get_preset_service
        preset_service = get_preset_service()
        await preset_service.auto_promote_from_config()
        print("Presets auto-promoted from config (default lifespan).")
    except Exception as e:
        print(f"Failed to initialize database or presets: {e}", file=sys.stderr)

    yield

    print("Closing database connections (default lifespan)...")
    await close_db()


class _Fallback:
    """Route backend prefixes to the inner app; everything else to the SPA."""

    def __init__(self, backend: ASGIApp, dist: Path, backend_prefixes: List[str]):
        self.backend = backend
        self.dist = dist
        self.backend_prefixes = tuple(backend_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.backend(scope, receive, send)
            return
        path: str = scope.get("path", "/")
        if any(path == p or path.startswith(p + "/") for p in self.backend_prefixes):
            await self.backend(scope, receive, send)
            return
            
        if not self.dist.is_dir():
            from starlette.responses import PlainTextResponse
            await PlainTextResponse(
                "Admin UI is building or missing. Please refresh in a moment...", 
                status_code=503
            )(scope, receive, send)
            return

        try:
            f = (self.dist / path.lstrip("/")).resolve()
            if path != "/" and f.is_file() and f.is_relative_to(self.dist):
                await FileResponse(f)(scope, receive, send)
                return
        except (ValueError, OSError):
            pass
            
        index_file = self.dist / "index.html"
        if index_file.is_file():
            await FileResponse(index_file)(scope, receive, send)
        else:
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("Admin UI missing index.html.", status_code=404)(scope, receive, send)


def build_web_app(*, extra_routes=None, extra_prefixes=None, lifespan=None):
    """Build the ASGI app: REST API + optional extra routes + frontend SPA.

    Args:
        extra_routes:   Additional Starlette Route/Mount objects (e.g. MCP transports).
        extra_prefixes: Path prefixes for those routes (e.g. ["/sse", "/mcp"]),
                        so the frontend fallback knows not to capture them.
        lifespan:       Optional async context manager for the inner Starlette app.
    """
    api = FastAPI(
        title="Nocturne Memory API",
        description="AI长期记忆知识图谱后端",
        version="2.5.6",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    
    @api.exception_handler(ConfigWriteError)
    async def config_write_error_handler(request: Request, exc: ConfigWriteError):
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc)},
        )
        
    api.include_router(health_router)
    api.include_router(review_router)
    api.include_router(browse_router)
    api.include_router(maintenance_router)
    api.include_router(settings_router)
    api.include_router(presets_router)

    routes = list(extra_routes or [])
    routes.append(Mount("/api", app=api))

    async def _health_endpoint(request):
        return await health_check()

    routes.append(Route("/health", endpoint=_health_endpoint))

    # Use the default lifespan if none is provided
    app_lifespan = lifespan or default_lifespan

    inner = Starlette(routes=routes, lifespan=app_lifespan)
    authed = NamespaceMiddleware(
        LocaleMiddleware(
            BearerTokenAuthMiddleware(inner, excluded_paths=["/api/health", "/health"])
        )
    )
    cors_authed = CORSMiddleware(
        authed,
        **get_cors_config(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    backend_prefixes = ["/api", "/health"] + list(extra_prefixes or [])
    return _Fallback(cors_authed, FRONTEND_DIR, backend_prefixes)
