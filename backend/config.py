"""
Configuration management via config.json.

First run (no config.json):
  1. Legacy .env with app-level keys (DATABASE_URL etc.)? → migrate to config.json
  2. Environment variables exist (Docker)? → generate config.json from them
  3. Nothing? → create config.json with defaults

After config.json exists: it is the sole source of truth. Period.
All settings can be changed via the Dashboard Settings UI or by editing config.json directly.

IMPORTANT: config.py NEVER writes to .env. The .env → config.json migration is
read-only and one-directional. .env files containing only Docker Compose vars
(POSTGRES_USER/PASSWORD/DB) are ignored to prevent generating default configs.
"""

import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from locales import t

_BACKEND_DIR = Path(__file__).resolve().parent
# 兼容 Docker 部署：Dockerfile 把 backend/* 复制到 WORKDIR，所以容器内
# _BACKEND_DIR 本身就是根目录；本地开发则是 backend/，根目录在上一级。
_IN_DOCKER = Path("/.dockerenv").exists()
ROOT_DIR = _BACKEND_DIR if _IN_DOCKER else _BACKEND_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.json"

_DEMO_DB = "demo.db"
_USER_DB = "nocturne_data.db"


def _default_database_url() -> str:
    db_path = (ROOT_DIR / _DEMO_DB).resolve()
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


DEFAULTS: dict[str, Any] = {
    "database_url": _default_database_url(),
    # PostgreSQL connection pool sizing (ignored for SQLite).
    # Sized for the typical single-user MCP workload; raise for
    # high-concurrency deployments. Takes effect after restart.
    "db_pool_size": 5,
    "db_max_overflow": 5,
    "valid_domains": ["core", "writer", "game", "notes", "narrative"],
    "boot_uris": {"": ["core://agent", "core://my_user", "core://agent/my_user"]},
    "host": "127.0.0.1",
    "web_port": 8233,
    "auto_open_browser": True,
    "api_token": None,
    "cors_origins": None,
    "public_readonly_mcp": False,
    "locale": None,
    "bloat_min_bytes": 2048,
}

_ENV_MAP: dict[str, str] = {
    "database_url": "DATABASE_URL",
    "valid_domains": "VALID_DOMAINS",
    "host": "HOST",
    "web_port": "WEB_PORT",
    "auto_open_browser": "AUTO_OPEN_BROWSER",
    "api_token": "API_TOKEN",
    "cors_origins": "CORS_ORIGINS",
    "public_readonly_mcp": "PUBLIC_READONLY_MCP",
    "locale": "LOCALE",
    "bloat_min_bytes": "BLOAT_MIN_BYTES",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class ConfigWriteError(Exception):
    """Raised when config.json cannot be written due to permissions."""
    pass


def _docker_setup_hint() -> str:
    return t("config.docker_hint")


def _save_file(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except PermissionError as e:
        import sys
        print(t("config.permission_denied").format(filename=CONFIG_PATH.name), file=sys.stderr)
        msg = t("config.permission_denied").format(filename=CONFIG_PATH.name) + " "
        if _IN_DOCKER:
            msg += t("config.permission_docker_hint")
        else:
            msg += t("config.permission_local_hint")
        raise ConfigWriteError(msg) from e


def _coerce(key: str, raw: str) -> Any:
    if key == "valid_domains":
        return [d.strip() for d in raw.split(",") if d.strip()]
    if key in ("web_port", "bloat_min_bytes", "db_pool_size", "db_max_overflow"):
        return int(raw)
    if key in ("auto_open_browser", "public_readonly_mcp"):
        return raw.lower() not in ("false", "0", "no")
    return raw


def _db_path_from_url(url: str) -> Optional[Path]:
    """Extract the file path from a sqlite database_url, or None if not sqlite."""
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix):]
    return Path(raw) if raw else None


def _make_db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _unique_db_path(directory: Path, base_name: str) -> Path:
    """Return a non-colliding path under *directory*. Tries base_name first,
    then appends random hex suffixes until a free slot is found."""
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix
    candidate = directory / base_name
    if not candidate.exists():
        return candidate
    for _ in range(100):
        rand = secrets.token_hex(3)
        candidate = directory / f"{stem}_{rand}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{secrets.token_hex(8)}{suffix}"


def _migrate_away_from_demo(cfg: dict) -> bool:
    """If database_url points to demo.db, copy it to a user-owned file that is
    safe from ``git pull`` overwrites.  Returns True if the config was changed."""
    url = cfg.get("database_url", "")
    db_path = _db_path_from_url(url)
    if db_path is None:
        return False
    if db_path.name != _DEMO_DB:
        return False

    target = _unique_db_path(db_path.parent, _USER_DB)

    if db_path.exists():
        shutil.copy2(str(db_path), str(target))
        print(
            t("config.demo_copied").format(demo_db=_DEMO_DB, target=target.name),
            file=sys.stderr,
        )
    else:
        print(t("config.using_db").format(name=target.name), file=sys.stderr)

    cfg["database_url"] = _make_db_url(target)
    return True


def _extract_boot_uris(source: dict) -> dict[str, list[str]]:
    """Extract boot URI config from a flat key-value dict (os.environ)."""
    boot: dict[str, list[str]] = {}
    if "CORE_MEMORY_URIS" in source:
        base = source["CORE_MEMORY_URIS"] or ""
        boot[""] = [u.strip() for u in base.split(",") if u.strip()]
    for key, val in source.items():
        if key.startswith("CORE_MEMORY_URIS__"):
            ns = key[len("CORE_MEMORY_URIS__"):]
            val_str = val or ""
            boot[ns] = [u.strip() for u in val_str.split(",") if u.strip()]
    return boot


def _build_cfg_from_kvs(kvs: dict) -> dict:
    """Build a config dict from flat key-value pairs (.env or env vars)."""
    cfg = dict(DEFAULTS)
    for cfg_key, env_key in _ENV_MAP.items():
        val = kvs.get(env_key)
        # 统一标准：只认 WEB_PORT。PORT 仅作为历史遗留的 fallback。
        if cfg_key == "web_port" and not val:
            val = kvs.get("PORT")
        if val:
            cfg[cfg_key] = _coerce(cfg_key, val)
    boot = _extract_boot_uris(kvs)
    if boot:
        cfg["boot_uris"] = boot
    return cfg



def _migrate_from_dotenv() -> Optional[dict]:
    """One-time migration: read legacy .env and build a config dict.
    Only triggers if .env contains app-level keys (DATABASE_URL, API_TOKEN, etc.),
    not just Docker Compose vars (POSTGRES_USER/PASSWORD/DB)."""
    dotenv_path = ROOT_DIR / ".env"
    if not dotenv_path.exists():
        return None
    try:
        from dotenv import dotenv_values
        env = dotenv_values(dotenv_path)
    except ImportError:
        return None
    if not env:
        return None
    app_keys = set(_ENV_MAP.values()) | {"CORE_MEMORY_URIS"}
    if not any(k in app_keys or k.startswith("CORE_MEMORY_URIS__") for k in env):
        return None
    print(t("config.migrating_dotenv"), file=sys.stderr)
    return _build_cfg_from_kvs(env)


def _migrate_from_env_vars() -> Optional[dict]:
    """Build config from os.environ (Docker first boot). Returns None if nothing relevant found."""
    # Only trigger on Nocturne-specific vars to avoid false positives from
    # common env vars like PORT that exist in many environments.
    strong_signals = {"DATABASE_URL", "API_TOKEN", "VALID_DOMAINS", "CORE_MEMORY_URIS"}
    if not any(k in strong_signals or k.startswith("CORE_MEMORY_URIS__") for k in os.environ):
        return None
    print(t("config.generating_env"), file=sys.stderr)
    return _build_cfg_from_kvs(dict(os.environ))


# ---------------------------------------------------------------------------
# Config loading (cached per-process)
# ---------------------------------------------------------------------------

_cache: Optional[dict] = None

def _load() -> dict:
    global _cache

    if CONFIG_PATH.exists():
        if CONFIG_PATH.is_dir():
            if _IN_DOCKER:
                raise RuntimeError(_docker_setup_hint())
            raise RuntimeError(
                f"{CONFIG_PATH} is a directory, but Nocturne expects a JSON file."
            )

        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except json.JSONDecodeError as e:
            if _IN_DOCKER:
                raise RuntimeError(
                    f"Failed to parse config.json: {e}\n\n{_docker_setup_hint()}"
                ) from e
            raise
            
        if "bloat_min_bytes" not in _cache:
            _cache["bloat_min_bytes"] = DEFAULTS["bloat_min_bytes"]

        if _migrate_away_from_demo(_cache):
            try:
                _save_file(_cache)
            except ConfigWriteError as e:
                raise RuntimeError(
                    t("config.db_migrated_not_writable").format(demo_db=_DEMO_DB)
                ) from e

        return _cache

    cfg = _migrate_from_dotenv()
    if cfg is None:
        cfg = _migrate_from_env_vars()
    if cfg is None:
        if _IN_DOCKER:
            raise RuntimeError(_docker_setup_hint())
        cfg = dict(DEFAULTS)

    migrated = _migrate_away_from_demo(cfg)
    try:
        _save_file(cfg)
    except ConfigWriteError as e:
        if migrated:
            raise RuntimeError(
                t("config.db_migrated_not_writable").format(demo_db=_DEMO_DB)
            ) from e

    _cache = cfg
    return _cache


def _invalidate():
    global _cache
    _cache = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get(key: str) -> Any:
    """Get a config value. Reads only from config.json."""
    return _load().get(key, DEFAULTS.get(key))


def get_locale() -> str:
    """Get the current locale code, falling back to 'en'."""
    return get("locale") or "en"


def get_boot_uris(namespace: str = "") -> list[str]:
    """Get boot URIs for a namespace."""
    boot = _load().get("boot_uris", {})
    if namespace in boot:
        return boot[namespace]
    if "" in boot:
        return boot[""]
    return []


def get_all_boot_uris() -> dict[str, list[str]]:
    """Get the full boot_uris dict (all namespaces)."""
    return dict(_load().get("boot_uris", {}))


def set_boot_uris(uris: list[str], namespace: str = "") -> None:
    cfg = _load()
    if "boot_uris" not in cfg:
        cfg["boot_uris"] = {}
    cfg["boot_uris"][namespace] = uris
    _save_file(cfg)
    _invalidate()


def delete_boot_uris(namespace: str) -> bool:
    """Remove a namespace override. Returns True if it existed."""
    cfg = _load()
    boot = cfg.get("boot_uris", {})
    if namespace not in boot:
        return False
    del boot[namespace]
    _save_file(cfg)
    _invalidate()
    return True


def set_value(key: str, value: Any) -> None:
    cfg = _load()
    cfg[key] = value
    _save_file(cfg)
    _invalidate()


def get_all() -> dict:
    """Get all settings for the UI."""
    return dict(_load())


def ensure_config_exists() -> None:
    _load()


