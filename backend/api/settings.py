"""
Settings API — read and update server configuration from the admin UI.
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
from db.namespace import get_namespace
from locales import t

_IN_DOCKER = Path("/.dockerenv").exists()

router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    database_url: str | None = None
    valid_domains: list[str] | None = None
    host: str | None = None
    web_port: int | None = None
    auto_open_browser: bool | None = None
    api_token: str | None = None
    cors_origins: str | None = None
    public_readonly_mcp: bool | None = None
    locale: str | None = None
    bloat_min_bytes: int | None = None


class BootUriUpdate(BaseModel):
    uris: list[str]


class DatabaseCreate(BaseModel):
    path: str


class DatabaseTest(BaseModel):
    database_url: str


# ---------------------------------------------------------------------------
# Settings CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def get_settings():
    """Return all current settings from config.json."""
    result = {
        "settings": config.get_all(),
        "config_path": str(config.CONFIG_PATH),
    }
    if _IN_DOCKER:
        result["locked_fields"] = ["web_port", "host"]
    return result


_URI_RE = re.compile(r"^[a-zA-Z0-9_-]+://[a-zA-Z0-9_/\.-]*$")
_DEFAULT_NS_SENTINEL = "_ns_default_0x7f3a9e"


@router.put("")
async def update_settings(body: SettingsUpdate):
    """Update one or more settings in config.json."""
    updated = []
    needs_restart = False

    fields = body.model_dump(exclude_unset=True)
    
    # Only locale is allowed to be explicitly set to None (to clear it).
    # For all other fields, if they are None, we drop them to preserve partial-update semantics.
    fields = {k: v for k, v in fields.items() if v is not None or k == "locale"}

    _DOCKER_LOCKED = {"web_port", "host"}
    if _IN_DOCKER:
        locked = _DOCKER_LOCKED & fields.keys()
        if locked:
            raise HTTPException(
                status_code=422,
                detail=t("api.settings.docker_locked_fields").format(fields=', '.join(sorted(locked))),
            )

    if "web_port" in fields:
        port = fields["web_port"]
        if not (1 <= port <= 65535):
            raise HTTPException(status_code=422, detail=t("api.settings.invalid_port").format(port=port))

    if "bloat_min_bytes" in fields:
        bloat_bytes = fields["bloat_min_bytes"]
        if bloat_bytes is not None and bloat_bytes < 1:
            raise HTTPException(status_code=422, detail=t("api.settings.invalid_bloat_min_bytes"))

    if "api_token" in fields:
        token = fields["api_token"]
        if token and len(token) < 32:
            raise HTTPException(
                status_code=422,
                detail=t("api.settings.token_too_short").format(len=len(token)),
            )

    if "valid_domains" in fields:
        for d in fields["valid_domains"]:
            if not re.match(r'^[a-z][a-z0-9_]*$', d):
                raise HTTPException(
                    status_code=422,
                    detail=t("api.settings.validation_error"),
                )

    pending_host = fields.get("host", config.get("host"))
    pending_token = fields.get("api_token", config.get("api_token"))
    if pending_host not in ("127.0.0.1", "localhost", "::1") and not pending_token:
        raise HTTPException(
            status_code=422,
            detail=t("api.settings.token_required"),
        )

    for field_name, value in fields.items():
        config.set_value(field_name, value)
        updated.append(field_name)
        if field_name in ("database_url", "host", "web_port", "api_token", "public_readonly_mcp", "cors_origins"):
            needs_restart = True

    return {
        "success": True,
        "updated": updated,
        "needs_restart": needs_restart,
    }


# ---------------------------------------------------------------------------
# Boot URI management (replaces browse.py boot-uris endpoints)
# ---------------------------------------------------------------------------

@router.get("/boot-uris")
async def get_boot_uris():
    """Return boot URIs for the current namespace (reads from active preset)."""
    from db import get_preset_service
    service = get_preset_service()
    uris = await service.get_boot_uris(get_namespace())
    return {"uris": uris}


@router.put("/boot-uris")
async def set_boot_uris(body: BootUriUpdate):
    """Replace the full boot URI list for the current namespace."""
    from db import get_preset_service

    for uri in body.uris:
        if not _URI_RE.match(uri):
            raise HTTPException(status_code=422, detail=t("api.settings.invalid_uri_format").format(uri=uri))

    service = get_preset_service()
    await service.set_boot_uris(get_namespace(), body.uris)
    return {"success": True, "uris": body.uris}


class BootUriToggle(BaseModel):
    uri: str
    enabled: bool


@router.patch("/boot-uris")
async def toggle_boot_uri(body: BootUriToggle):
    """Add or remove a single URI from the boot list."""
    from db import get_preset_service
    ns = get_namespace()
    service = get_preset_service()
    current = await service.get_boot_uris(ns)
    uri = body.uri.strip()
    if not uri:
        raise HTTPException(status_code=422, detail=t("api.settings.uri_empty"))
    if not _URI_RE.match(uri):
        raise HTTPException(status_code=422, detail=t("api.settings.invalid_uri_format_bare"))

    if body.enabled:
        if uri not in current:
            current.append(uri)
    else:
        current = [u for u in current if u != uri]

    await service.set_boot_uris(ns, current)
    return {"success": True, "uris": current}


# --- Multi-namespace boot URI management (used by Settings drawer) ---


def _resolve_ns(namespace: str) -> str:
    """Map the URL-safe sentinel back to the real empty-string key."""
    return "" if namespace == _DEFAULT_NS_SENTINEL else namespace


@router.get("/boot-uris/all")
async def get_all_boot_uris():
    """Return boot URIs for every namespace at once (from active preset)."""
    from db import get_preset_service
    service = get_preset_service()
    return {"boot_uris": await service.get_all_boot_uris()}


@router.put("/boot-uris/ns/{namespace}")
async def set_boot_uris_for_ns(namespace: str, body: BootUriUpdate):
    """Set boot URIs for a specific namespace."""
    from db import get_preset_service
    ns = _resolve_ns(namespace)
    for uri in body.uris:
        if not _URI_RE.match(uri):
            raise HTTPException(status_code=422, detail=t("api.settings.invalid_uri_format").format(uri=uri))

    service = get_preset_service()
    await service.set_boot_uris(ns, body.uris)
    return {"success": True, "namespace": ns, "uris": body.uris}


@router.delete("/boot-uris/ns/{namespace}")
async def delete_boot_uris_for_ns(namespace: str):
    """Remove a namespace override."""
    from db import get_preset_service
    ns = _resolve_ns(namespace)
    if ns == "":
        raise HTTPException(status_code=400, detail=t("api.settings.cannot_delete_default_ns"))

    service = get_preset_service()
    if not await service.delete_boot_uris(ns):
        raise HTTPException(status_code=404, detail=t("api.settings.no_boot_uri_override").format(ns=ns))

    return {"success": True, "namespace": namespace}


# ---------------------------------------------------------------------------
# Database management
# ---------------------------------------------------------------------------

@router.get("/database/status")
async def database_status():
    """Return current DB info: type, path (SQLite), size, etc."""
    url = config.get("database_url") or ""
    info: dict = {"database_url": url, "type": "unknown"}

    if not url:
        return info

    if "sqlite" in url:
        info["type"] = "sqlite"
        match = re.search(r"///(.+)$", url)
        if match:
            db_path = Path(match.group(1))
            info["path"] = str(db_path)
            info["exists"] = db_path.exists()
            if db_path.exists():
                size = db_path.stat().st_size
                info["size_bytes"] = size
                info["size_display"] = _format_size(size)
    elif "postgresql" in url:
        info["type"] = "postgresql"
        info["url_masked"] = _mask_password(url)

    return info


_ALLOWED_DB_SCHEMES = ("sqlite+aiosqlite", "postgresql+asyncpg")


@router.post("/database/test")
async def test_database(body: DatabaseTest):
    """Test if a database URL is connectable."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    url = body.database_url
    if not any(url.startswith(s + "://") for s in _ALLOWED_DB_SCHEMES):
        raise HTTPException(
            status_code=422,
            detail=t("api.settings.unsupported_scheme").format(schemes=', '.join(_ALLOWED_DB_SCHEMES)),
        )

    try:
        engine = create_async_engine(url, echo=False)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return {"success": True, "message": t("api.settings.db_connected")}
    except Exception as e:
        return {"success": False, "message": t("api.settings.db_failed").format(error=e)}


@router.post("/database/create")
async def create_database(body: DatabaseCreate):
    """Create a new empty SQLite database at the given path."""
    db_path = Path(body.path).resolve()

    if db_path.exists():
        raise HTTPException(status_code=409, detail=t("api.settings.file_exists"))

    db_path.parent.mkdir(parents=True, exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    try:
        from db.database import DatabaseManager
        mgr = DatabaseManager(url)
        await mgr.init_db()
        await mgr.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=t("api.settings.db_create_failed").format(error=e))

    return {
        "success": True,
        "database_url": url,
        "path": str(db_path),
    }


@router.post("/database/open-folder")
async def open_database_folder():
    """Open the current SQLite DB's containing folder in the OS file manager."""
    import os
    import platform
    import subprocess

    if Path("/.dockerenv").exists():
        raise HTTPException(
            status_code=501,
            detail=t("api.settings.open_folder_docker"),
        )

    url = config.get("database_url") or ""
    if "sqlite" not in url:
        raise HTTPException(status_code=400, detail=t("api.settings.sqlite_only"))

    match = re.search(r"///(.+)$", url)
    if not match:
        raise HTTPException(status_code=400, detail=t("api.settings.parse_path_failed"))

    db_path = Path(match.group(1)).resolve()
    folder = db_path.parent if db_path.is_file() else db_path

    if not folder.exists():
        raise HTTPException(status_code=404, detail=t("api.settings.folder_not_found").format(path=folder))

    system = platform.system()
    if system == "Windows":
        os.startfile(str(folder))
    elif system == "Darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _mask_password(url: str) -> str:
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", url)
