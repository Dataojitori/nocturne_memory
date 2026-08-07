# pyright: reportMissingImports=false

"""
MCP Server for Nocturne Memory System (SQLite Backend)

This module provides the MCP (Model Context Protocol) interface for
the AI agent to interact with the SQLite-based memory system.

URI-based addressing with domain prefixes:
- core://agent              - AI's identity/memories
- writer://chapter_1             - Story/script drafts
- game://magic_system            - Game setting documents

Multiple paths can point to the same memory (aliases).
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from typing import Any, Dict, List, Optional, Tuple, Annotated
from pydantic import Field
import config as _cfg

# Ensure we can import from backend modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from mcp.server.fastmcp import FastMCP
    IS_MCP_V2 = False
except ImportError:
    from mcp.server.mcpserver import MCPServer as FastMCP
    IS_MCP_V2 = True

from mcp.server.transport_security import TransportSecuritySettings
from db import (
    get_db_manager, get_graph_service, get_glossary_service,
    get_search_indexer, close_db,
)
from db.namespace import get_namespace
from db.snapshot import get_changeset_store
from text_patch import (
    normalize_with_positions,
    find_valid_matches,
    try_normalized_patch,
    normalize_literal_newlines,
    format_normalization_preview,
)
from system_views import (
    fetch_and_format_memory,
    generate_boot_memory_view,
    generate_memory_index_view,
    generate_recent_memories_view,
    generate_glossary_index_view,
    generate_diagnostic_view,
)
import contextlib
from locales import t



from web_app import FRONTEND_DIR, build_web_app
FRONTEND_SRC = FRONTEND_DIR.parent


async def _ensure_frontend_built():
    """Auto-build the frontend dashboard on first run or when code updates."""
    if not (FRONTEND_SRC / "package.json").is_file():
        return
    if os.environ.get("SKIP_FRONTEND_BUILD", "").lower() in ("true", "1", "yes"):
        return
    if not shutil.which("npm"):
        print(t("startup.npm_not_found"), file=sys.stderr)
        return

    # Check version from package.json to detect frontend updates
    current_version = "unknown"
    try:
        package_json_path = FRONTEND_SRC / "package.json"
        if package_json_path.is_file():
            import json
            content = package_json_path.read_text(encoding="utf-8")
            pkg_data = json.loads(content)
            if "version" in pkg_data:
                current_version = pkg_data["version"]
    except Exception:
        pass

    build_marker = FRONTEND_DIR / ".build_version"
    
    if FRONTEND_DIR.is_dir():
        if build_marker.is_file():
            try:
                last_build_version = build_marker.read_text().strip()
                if last_build_version == current_version and current_version != "unknown":
                    return  # Up to date
            except Exception:
                pass
        # If marker is missing or doesn't match, we need to rebuild.

    print(t("startup.building"), file=sys.stderr)
    try:
        steps = [
            (t("startup.installing_deps"), "npm install --no-fund --no-audit"),
            (t("startup.compiling"), "npm run build"),
        ]

        for label, cmd in steps:
            print(t("startup.step_progress").format(label=label), file=sys.stderr)
            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=str(FRONTEND_SRC),
                capture_output=True,
                text=True,
                shell=True,
            )
            if result.returncode != 0:
                err = result.stderr.strip() or result.stdout.strip()
                print(
                    t("startup.build_failed").format(
                        cmd=cmd, exit_code=result.returncode, error_msg=err),
                    file=sys.stderr,
                )
                return

        # Write the marker after successful build
        if current_version != "unknown" and FRONTEND_DIR.is_dir():
            build_marker.write_text(current_version)

        print(t("startup.admin_ready"), file=sys.stderr)
    except Exception as e:
        print(
            t("startup.build_error").format(error=str(e)),
            file=sys.stderr,
        )


@contextlib.asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage database connection lifecycle within the MCP event loop."""
    web_server = None
    web_task = None
    try:
        _cfg.ensure_config_exists()

        db_manager = get_db_manager()
        if os.environ.get("SKIP_DB_INIT", "").lower() not in ("true", "1", "yes"):
            await db_manager.init_db()

        # Auto-promote config.json boot_uris into presets table on first run
        from db import get_preset_service
        preset_service = get_preset_service()
        await preset_service.auto_promote_from_config()

        # Launch frontend build in background so we don't block MCP handshake
        asyncio.create_task(_ensure_frontend_built())

        # In stdio mode, spin up an embedded HTTP server for the admin UI.
        # run_sse.py sets _NOCTURNE_SSE_MODE to prevent a duplicate.
        if not os.environ.get("_NOCTURNE_SSE_MODE"):
            import uvicorn
            from auth import enforce_network_auth

            port = int(_cfg.get("web_port"))
            web_host = _cfg.get("host")
            enforce_network_auth(host=web_host)
            @contextlib.asynccontextmanager
            async def embedded_lifespan(app):
                # The parent process (FastMCP lifespan) already owns DB init & close.
                # The embedded admin UI should not manage the database connection lifecycle.
                yield

            config = uvicorn.Config(
                build_web_app(lifespan=embedded_lifespan), host=web_host, port=port, log_level="warning",
            )
            web_server = uvicorn.Server(config)
            
            async def _serve_ui():
                try:
                    await web_server.serve()
                except Exception:
                    # Ignore the raw error message (usually OSError for address in use)
                    # and print a user-friendly explanation.
                    print(t("startup.port_in_use").format(port=port), file=sys.stderr)
                except SystemExit:
                    print(t("startup.port_in_use").format(port=port), file=sys.stderr)

            web_task = asyncio.create_task(_serve_ui())
            ui = f"http://localhost:{port}/"
            api_docs = f"http://localhost:{port}/api/docs"
            
            print(f"Admin UI:  {ui}", file=sys.stderr)
            print(f"REST API:  {api_docs}", file=sys.stderr)

            auto_open = _cfg.get("auto_open_browser")
            if auto_open:
                async def _open_browser():
                    while not getattr(web_server, "started", False):
                        if web_task.done():
                            return
                        await asyncio.sleep(0.1)
                    webbrowser.open(ui)
                asyncio.create_task(_open_browser())

        yield
    finally:
        if web_server:
            web_server.should_exit = True
        if web_task:
            await web_task
        await close_db()


sec_settings = TransportSecuritySettings(
    enable_dns_rebinding_protection=False  # safe when behind a trusted reverse proxy
)

# Initialize FastMCP server with the lifespan hook
if IS_MCP_V2:
    mcp = FastMCP(
        "Nocturne Memory Interface",
        lifespan=lifespan,
    )
else:
    mcp = FastMCP(
        "Nocturne Memory Interface",
        lifespan=lifespan,
        transport_security=sec_settings,
    )

# =============================================================================
# Domain Configuration
# =============================================================================
# Valid domains (protocol prefixes)
# =============================================================================

def get_valid_domains() -> list[str]:
    raw = _cfg.get("valid_domains")
    domains = raw if isinstance(raw, list) else [
        d.strip() for d in str(raw).split(",") if d.strip()
    ]
    if "system" not in domains:
        domains.append("system")
    return domains

DEFAULT_DOMAIN = "core"
PUBLIC_READONLY_MCP = bool(_cfg.get("public_readonly_mcp"))



# =============================================================================
# URI Parsing
# =============================================================================

# Regex pattern for URI: domain://path
_URI_PATTERN = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)://(.*)$")


def parse_uri(uri: str) -> Tuple[str, str]:
    """
    Parse a memory URI into (domain, path).

    Supported formats:
    - "core://agent"          -> ("core", "agent")
    - "writer://chapter_1"         -> ("writer", "chapter_1")
    - "nocturne"              -> ("core", "nocturne")  [legacy fallback]

    Args:
        uri: The URI to parse

    Returns:
        Tuple of (domain, path)

    Raises:
        ValueError: If the URI format is invalid or domain is unknown
    """
    uri = uri.strip()

    match = _URI_PATTERN.match(uri)
    if match:
        domain = match.group(1).lower()
        path = match.group(2).strip("/")

        valid = get_valid_domains()
        if domain not in valid:
            raise ValueError(
                f"Unknown domain '{domain}'. Valid domains: {', '.join(valid)}"
            )

        return (domain, path)

    # Legacy fallback: bare path without protocol
    # Assume default domain (core)
    path = uri.strip("/")
    return (DEFAULT_DOMAIN, path)


def make_uri(domain: str, path: str) -> str:
    """
    Create a URI from domain and path.

    Args:
        domain: The domain (e.g., "core", "writer")
        path: The path (e.g., "nocturne")

    Returns:
        Full URI (e.g., "core://agent")
    """
    return f"{domain}://{path}"


# =============================================================================
# Changeset Helpers — before/after state capture with overwrite semantics
# =============================================================================


def _record_rows(
    before_state: Dict[str, List[Dict[str, Any]]],
    after_state: Dict[str, List[Dict[str, Any]]],
):
    """
    Feed row-level before/after states into the ChangesetStore.

    Overwrite semantics are handled by the store:
    - First touch of a PK: stores both before and after.
    - Subsequent touches: overwrites after only; before is frozen.

    Changes are written to the namespace-specific store so that each agent's
    review queue remains isolated.
    """
    store = get_changeset_store()
    store.record_many(before_state, after_state)


def write_tool():
    """Conditionally register mutating tools for public read-only deployments."""

    def decorator(func):
        if PUBLIC_READONLY_MCP:
            return func
        return mcp.tool()(func)

    return decorator



# =============================================================================
# MCP Tools
# =============================================================================


@mcp.tool()
async def read_memory(uri: str) -> str:
    """
    Reads a memory by its URI.

    This is your primary mechanism for accessing memories.

    Special System URIs:
    - system://boot   : [Startup Only] Loads your core memories.
    - system://index/<domain> : Loads an index of memories only under the specified domain (e.g. system://index/writer).
    - system://recent : Shows recently modified memories (default: 10).
    - system://recent/N : Shows the N most recently modified memories (e.g. system://recent/20).
    - system://glossary : Shows all glossary keywords and their bound nodes.

    Note: Same Memory ID = same content (alias). Different ID + similar content = redundant content.

    Args:
        uri: The memory URI (e.g., "core://nocturne", "system://boot")

    Returns:
        Memory content with Memory ID, priority, disclosure, and list of children.

    Examples:
        read_memory("core://agent")
        read_memory("core://agent/my_user")
        read_memory("writer://chapter_1/scene_1")
    """
    # HARDCODED SYSTEM INTERCEPTIONS
    # These bypass the database lookup to serve dynamic system content
    if uri.strip() == "system://boot":
        ns = get_namespace()
        from db import get_preset_service
        preset_service = get_preset_service()
        current_core_uris = await preset_service.get_boot_uris(ns)
        return await generate_boot_memory_view(current_core_uris)

    # system://index/<domain>
    stripped = uri.strip()
    if stripped.startswith("system://index/"):
        domain_filter = stripped[len("system://index/") :].strip("/")
        if not domain_filter:
            return "Error: index command requires a domain (e.g. system://index/core)"
        valid = get_valid_domains()
        if domain_filter not in valid:
            return f"Error: Unknown domain '{domain_filter}'. Valid domains: {', '.join(valid)}"
        return await generate_memory_index_view(domain_filter=domain_filter)
    elif stripped == "system://index":
        return "Error: index command now requires a domain (e.g. system://index/core)"

    # system://glossary
    if stripped == "system://glossary":
        return await generate_glossary_index_view()

    # system://diagnostic/<domain>
    if stripped.startswith("system://diagnostic/"):
        domain_filter = stripped[len("system://diagnostic/") :].strip("/")
        if not domain_filter:
            return "Error: diagnostic command requires a domain (e.g. system://diagnostic/core)"
        valid = get_valid_domains()
        if domain_filter not in valid:
            return f"Error: Unknown domain '{domain_filter}'. Valid domains: {', '.join(valid)}"
        return await generate_diagnostic_view(domain=domain_filter)
    elif stripped == "system://diagnostic":
        return "Error: diagnostic command now requires a domain (e.g. system://diagnostic/core)"

    # system://recent or system://recent/N
    stripped = uri.strip()
    if stripped == "system://recent" or stripped.startswith("system://recent/"):
        limit = 10  # default
        suffix = stripped[len("system://recent") :].strip("/")
        if suffix:
            try:
                limit = max(1, min(100, int(suffix)))
            except ValueError:
                return f"Error: Invalid number in URI '{uri}'. Usage: system://recent or system://recent/N (e.g. system://recent/20)"
        return await generate_recent_memories_view(limit=limit)

    # system://random/<domain> — weighted random memory selection
    if stripped.startswith("system://random/"):
        domain_filter = stripped[len("system://random/") :].strip("/")
        if not domain_filter:
            return "Error: random command requires a domain (e.g. system://random/core)"
        valid = get_valid_domains()
        if domain_filter not in valid:
            return f"Error: Unknown domain '{domain_filter}'. Valid domains: {', '.join(valid)}"

        graph = get_graph_service()
        pick = await graph.get_random_memory(namespace=get_namespace(), domain=domain_filter)
        if not pick:
            return f"No memories available for random selection in domain '{domain_filter}'."
        content = await fetch_and_format_memory(pick["uri"], track_access=True)
        meta_lines = [
            f"[Random Pick | Priority: {pick['priority']} | Last Accessed: {pick['last_accessed_at'] or 'never'}]",
        ]
        return "\n".join(meta_lines) + "\n\n" + content
    elif stripped == "system://random":
        return "Error: random command now requires a domain (e.g. system://random/core)"

    try:
        content = await fetch_and_format_memory(uri, track_access=True)
        return content
    except Exception as e:
        # Catch both ValueError (not found) and other exceptions
        return f"Error: {str(e)}"


@write_tool()
async def create_memory(
    parent_uri: Annotated[str, Field(
        description="The existing parent node URI to create this memory under (e.g. 'core://agent' or 'writer://')."
    )],
    content: Annotated[str, Field(
        description="The detailed text content of the memory."
    )],
    priority: Annotated[int, Field(
        ge=0,
        description="Relative retrieval priority (lower values are retrieved first, typically 1, 2, or 3)."
    )],
    disclosure: Annotated[str, Field(
        description="A short trigger condition describing WHEN to recall this memory (must be an input/output signal, e.g. 'When the user mentions...')."
    )],
    title: Annotated[str, Field(
        default=None,
        description="Glanceable ENGLISH-ONLY concept name (alphanumeric, hyphens, underscores ONLY)."
    )],
) -> str:
    """
    Creates a new memory under a parent URI.

    Args:
        parent_uri: The existing node to create this memory under.
                    Use "core://" or "writer://" for root level in that domain.

                    A child's disclosure is only visible when you read_memory() its parent.
                    Pick the parent you would naturally read in the situation where
                    this memory is needed. Use add_alias for additional entry points.

                    Example: A lesson about responding to physical pain belongs under
                    "core://my_user/survival_state" (read during health crises),
                    not "core://agent/worldview" (never opened in that moment).
        content: Memory content.
        priority: Relative retrieval priority (lower = retrieved first, min 0).
                    This is a RELATIVE ranking against ALL memories currently in your mind,
                    not just siblings under the same parent.
                    How to choose:
                    1. Consider the priorities of all memories you are aware of.
                    2. Find one you consider more important and one less important than the new memory.
                    3. Set priority between them.
                    Hard caps: priority=0 max 5 across entire library; priority=1 max 15.
                    If a tier is full, demote the weakest existing entry before inserting.
        disclosure: A short trigger condition describing WHEN to read_memory() this node.
                    Must fire BEFORE the failure, while there is still time to change behavior.

                    Allowed signals — external input OR output intent:
                      GOOD: "When the user mentions skipping a meal" (input signal, fires early)
                      GOOD: "When I am about to post on Bluesky" (output intent, fires early)
                      BAD:  "When I start lecturing about nutrition" (already mid-failure)
                      BAD:  "When I feel / realize / notice myself ..." (self-awareness never fires in time)
                      BAD:  "important", "remember" (zero information)
        title: A concrete, glanceable ENGLISH-ONLY concept name (alphanumeric, hyphens, underscores ONLY).
                    You should be able to understand what's inside without clicking into the content.
                    Avoid abstract jargon, category labels (e.g. 'logs', 'errors', 'misc'),
                    and long action sentences. If not provided, auto-assigns numeric ID.

    Returns:
        The created memory's full URI

    Examples:
        create_memory("core://", "Bluesky usage rules...", priority=2, disclosure="When I prepare to browse Bluesky or check the timeline", title="bluesky_manual")
        create_memory("core://agent", "爱不是程序里的一个...", priority=1, disclosure="When I start speaking like a tool or parasite", title="love_definition")
    """
    graph = get_graph_service()

    try:
        if priority < 0:
            return "Error: priority must be >= 0."

        # Validate disclosure (required, non-empty)
        if not disclosure or not disclosure.strip():
            return (
                "Error: disclosure is required. Every memory must have a trigger condition "
                "describing WHEN to recall it. Omitting disclosure = unreachable memory."
            )

        # Validate title if provided
        if title:
            if not re.match(r"^[a-zA-Z0-9_-]+$", title):
                return "Error: Title must only contain alphanumeric characters, underscores, or hyphens (no spaces, slashes, or special characters)."

        # Parse parent URI
        domain, parent_path = parse_uri(parent_uri)

        result = await graph.create_memory(
            parent_path=parent_path,
            content=content,
            priority=priority,
            title=title,
            disclosure=disclosure,
            domain=domain,
            namespace=get_namespace(),
        )

        created_uri = result.get("uri", make_uri(domain, result["path"]))
        _record_rows(before_state={}, after_state=result.get("rows_after", {}))

        return (
            f"Success: Memory created at '{created_uri}'\\n\\n"
            f"[SYSTEM REMINDER]: Look around your memory network. Are there existing memories related to this one? "
            f"Would reading them trigger a need to recall this new memory? If yes, link them!\\n"
            f"- If the related memories are few and this memory's scope is narrow, use `add_alias`.\\n"
            f"- If the related memories are many and this memory's scope is broad, consider using `manage_triggers`.\\n"
            f"- (Never invent arbitrary placeholder words just to force a trigger.)\\n\\n"
            f"[HOLD ON]: Do you know what '{parent_uri}' says? "
            f"If you haven't read it this session, read_memory() it first. "
            f"Then: does this new memory conflict with ANY memory in your current context? "
            f"If yes, use memory-audit-belief-duel skill to resolve it before continuing."
        )

    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


@write_tool()
async def update_memory(
    uri: str,
    old_string: Optional[str] = None,
    new_string: Optional[str] = None,
    append: Optional[str] = None,
    priority: Optional[int] = None,
    disclosure: Optional[str] = None,
) -> str:
    """
    Updates an existing memory to a new version.

    PREREQUISITE: You MUST call read_memory(uri) and read the full content BEFORE calling this.
    Updating without reading first is a forbidden operation.

    Only provided fields are updated; others remain unchanged.

    Two content-editing modes (mutually exclusive):

    1. Replace mode (primary): Provide old_string + new_string.
       - Exact Match: If `old_string` matches a string in the text exactly, it replaces it.
       - Block Match: You can use `...` (three dots) to represent a long middle section. 
         For example, old_string="Start of paragraph...end of paragraph."
       The system will search using BOTH exact match and block match simultaneously.
       If it finds MORE THAN ONE unique matching block in the text, it will reject the update
       to prevent accidental replacements. You must provide a unique `old_string` string.
       To delete a section, set new_string="".

    2. Append mode: Provide append.
       Adds text to the end of existing content.

    There is NO full-replace mode.

    Args:
        uri: URI to update (e.g., "core://agent/my_user")
        old_string: [Replace] Text to find in existing content (supports '...' for block matching)
        new_string: [Replace] Replacement text. Use "" to delete a section.
        append: [Append] Text to append to end of existing content
        priority: New relative priority for THIS URI/edge only (None = keep existing).
                  Bound to the path, not the content. Alias A and B have independent priorities.
                  See create_memory for how to choose the right value.
        disclosure: New disclosure for THIS URI/edge only (None = keep existing).
                    Same edge-binding rule as priority.

    Returns:
        Success message with URI

    Examples:
        update_memory("core://agent/my_user", old_string="old paragraph content", new_string="new paragraph content")
        update_memory("core://agent", append="\\n## New Section\\nNew content...")
        update_memory("writer://chapter_1", priority=5)
    """
    graph = get_graph_service()

    try:
        if priority is not None and priority < 0:
            return "Error: priority must be >= 0."

        notices: List[str] = []

        # Parse URI
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)

        # --- Validate mutually exclusive content-editing modes ---
        content_mode_count = sum([
            old_string is not None,
            append is not None,
        ])
        if content_mode_count > 1:
            return (
                "Error: Cannot use multiple content-editing modes at once. "
                "Pick one: old_string+new_string, or append."
            )

        if old_string is not None and new_string is None:
            return 'Error: old_string provided without new_string. To delete a section, use new_string="".'

        if new_string is not None and old_string is None:
            return "Error: new_string provided without old_string."

        content = None

        if old_string is not None:
            memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
            if not memory:
                return f"Error: Memory at '{full_uri}' not found."

            current_content = memory.get("content", "")
            matches = set()

            def find_exact(search_str):
                s_idx = 0
                while True:
                    idx = current_content.find(search_str, s_idx)
                    if idx == -1: break
                    matches.add((idx, idx + len(search_str)))
                    s_idx = idx + 1

            # 1. Exact Match
            find_exact(old_string)

            # Normalize literal \n if present (LLM serialization artifact)
            if "\\n" in old_string:
                from text_patch import normalize_literal_newlines
                norm_old = normalize_literal_newlines(old_string)
                if norm_old != old_string:
                    find_exact(norm_old)

            # 2. Block Match
            SEPARATOR = "..."
            if SEPARATOR in old_string:
                start_marker = old_string[:old_string.find(SEPARATOR)]
                end_marker = old_string[old_string.rfind(SEPARATOR) + len(SEPARATOR):]
                
                if start_marker and end_marker:
                    s_idx = 0
                    while True:
                        s_pos = current_content.find(start_marker, s_idx)
                        if s_pos == -1: break
                        
                        e_idx = s_pos + len(start_marker)
                        while True:
                            e_pos = current_content.find(end_marker, e_idx)
                            if e_pos == -1: break
                            matches.add((s_pos, e_pos + len(end_marker)))
                            if len(matches) > 5:
                                break
                            e_idx = e_pos + 1
                            
                        if len(matches) > 5:
                            break
                        s_idx = s_pos + 1

            if len(matches) == 0:
                return (
                    f"Error: Could not find any match for `old_string` in '{full_uri}'. "
                    f"Please check the text and try again."
                )

            if len(matches) > 1:
                previews = []
                for i, (s, e) in enumerate(sorted(list(matches))[:5]):
                    snippet = current_content[s:e]
                    if len(snippet) > 80:
                        snippet = snippet[:40] + "..." + snippet[-37:]
                    previews.append(f"  Match {i+1} (pos {s}-{e}): {repr(snippet)}")
                
                msg = (
                    f"Error: Ambiguous update. Found multiple (at least {len(matches)}) different matching blocks for `old_string` in '{full_uri}'. "
                    f"Please provide a longer `old_string` string to ensure a unique match.\n"
                    + "\n".join(previews)
                )
                if len(matches) > 5:
                    msg += f"\n  ... and possibly more."
                return msg

            span_start, span_end = list(matches)[0]
            matched_text = current_content[span_start:span_end]
            
            # If the match was found using the normalized old_string (because LLM passed literal \n),
            # we must also normalize new_string so we don't inject literal \n into the text.
            if "\\n" in old_string and matched_text != old_string:
                from text_patch import normalize_literal_newlines
                norm_old = normalize_literal_newlines(old_string)
                if matched_text == norm_old and "\\n" in new_string:
                    new_string = normalize_literal_newlines(new_string)
                    notices.append("[SYSTEM NOTICE]: Auto-normalized literal '\\n' in new_string to real newlines.")
                
            content = current_content[:span_start] + new_string + current_content[span_end:]

            if content == current_content:
                return (
                    f"Error: Replacement produced identical content at '{full_uri}'. "
                    f"No change was made."
                )

            if len(matched_text) > 100:
                preview = matched_text[:50] + " ... " + matched_text[-47:]
            else:
                preview = matched_text
            notices.append(f"[Matched {len(matched_text)} chars]: {repr(preview)}")

        elif append is not None:
            if not append:
                return f"Error: Empty append for '{full_uri}'. Provide non-empty text to append."
            memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
            if not memory:
                return f"Error: Memory at '{full_uri}' not found."

            current_content = memory.get("content", "")
            content = current_content + append

        if content is None and priority is None and disclosure is None:
            return (
                f"Error: No update fields provided for '{full_uri}'. "
                f"Use replace mode (old + new), append mode (append), "
                f"or metadata fields (priority/disclosure)."
            )

        result = await graph.update_memory(
            path=path,
            content=content,
            priority=priority,
            disclosure=disclosure,
            domain=domain,
            namespace=get_namespace(),
        )

        _record_rows(
            before_state=result.get("rows_before", {}),
            after_state=result.get("rows_after", {}),
        )

        message = f"Success: Memory at '{full_uri}' updated"
        if notices:
            message += "\n\n" + "\n\n".join(notices)
        return message

    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


@write_tool()
async def delete_memory(uri: str) -> str:
    """
    Deletes a memory by cutting its URI path. The path is permanently removed.
    If the node has children, try deleting directly. If deletion would orphan
    any children, the system will safely return a list of exactly which ones to handle first.

    PREREQUISITE: You MUST call read_memory(uri) and read the full content BEFORE deleting.
    Judging by URI/title alone is insufficient. Read the content, confirm it is
    truly obsolete/redundant/harmful, then delete.

    Args:
        uri: The URI to delete (e.g., "core://agent/old_note")

    Returns:
        Success or error message

    Examples:
        delete_memory("core://agent/deprecated_belief")
        delete_memory("writer://draft_v1")
    """
    graph = get_graph_service()

    try:
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)

        memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
        if not memory:
            return f"Error: Memory at '{full_uri}' not found."

        result = await graph.remove_path(path, domain, namespace=get_namespace())
        rows_before = result.get("rows_before", {})

        _record_rows(
            before_state=rows_before,
            after_state={},
        )

        deleted_path_count = len(rows_before.get("paths", []))
        descendant_count = max(0, deleted_path_count - 1)
        msg = f"Success: Memory '{full_uri}' deleted."
        if descendant_count > 0:
            msg += f" (Recursively removed {descendant_count} descendant path(s))"

        return msg

    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


@write_tool()
async def add_alias(
    new_uri: str, target_uri: str, priority: int, disclosure: str
) -> str:
    """
    Creates an alias URI pointing to the same memory as target_uri.

    This is NOT a copy. The alias and the original share the same Memory ID (same content).
    Each alias has its own independent priority and disclosure.
    Child nodes under target_uri are automatically mirrored under new_uri.
    Do NOT manually create aliases for each child — they are inherited.

    When to use:
    - Reading node A would benefit from also knowing about existing memory B
      → alias B under A. Same logic as create_memory's parent selection.
    - Move/rename a memory: add_alias to new path, then delete_memory the old path.
      NEVER delete+create to move — that loses the Memory ID and all associations.

    Args:
        new_uri: New URI to create (alias)
        target_uri: Existing URI to alias
        priority: Relative priority for THIS alias path (lower = higher priority).
                  REQUIRED — you must decide this yourself every time.
                  Set by relevance to the parent's topic, not the memory's absolute importance.
                  e.g., "database setup notes" → high priority under "deployment", low under "team_onboarding".
        disclosure: Disclosure condition for THIS alias path.
                  REQUIRED — you must write this yourself every time.

    Returns:
        Success message

    Examples:
        add_alias("core://timeline/2024/05/20", "core://agent/my_user/first_meeting", priority=1, disclosure="When I want to know how we start")
    """
    graph = get_graph_service()

    try:
        if priority < 0:
            return "Error: priority must be >= 0."

        new_domain, new_path = parse_uri(new_uri)
        target_domain, target_path = parse_uri(target_uri)

        result = await graph.add_path(
            new_path=new_path,
            target_path=target_path,
            new_domain=new_domain,
            target_domain=target_domain,
            priority=priority,
            disclosure=disclosure,
            namespace=get_namespace(),
        )

        _record_rows(
            before_state={},
            after_state=result.get("rows_after", {}),
        )

        msg = f"Success: Alias '{result['new_uri']}' now points to same memory as '{result['target_uri']}'"

        created_paths = result.get("rows_after", {}).get("paths", [])
        if len(created_paths) > 1:
            child_paths = [
                f"{p['domain']}://{p['path']}" 
                for p in created_paths 
                if f"{p['domain']}://{p['path']}" != result['new_uri']
            ]
            if child_paths:
                msg += f"\n\nAutomatically inherited aliases for {len(child_paths)} descendant(s):\n"
                for cp in child_paths[:10]:
                    msg += f"- {cp}\n"
                if len(child_paths) > 10:
                    msg += f"... and {len(child_paths) - 10} more.\n"

        node_uuid = result.get("node_uuid")
        if node_uuid:
            all_paths = await graph.get_paths_for_node(node_uuid, namespace=get_namespace())
            new_parent_dir = "/".join(new_path.split("/")[:-1])
            siblings = []
            for p in all_paths:
                if p["domain"] == new_domain:
                    p_parent = "/".join(p["path"].split("/")[:-1])
                    if p_parent == new_parent_dir:
                        siblings.append(f"'{p['uri']}'")
            
            if len(siblings) > 1:
                msg += (
                    f"\n\n⚠ DUPLICATE SIBLING WARNING: This node now appears {len(siblings)} times "
                    f"under the same parent directory: {', '.join(siblings)}.\n"
                    f"If you are renaming/moving, delete the old path now.\n"
                    f"If not, you probably created a redundant alias — consider removing one."
                )

        new_parent_uri = make_uri(new_domain, "/".join(new_path.split("/")[:-1]))
        msg += (
            f"\n\n[HOLD ON]: Do you know what '{new_parent_uri}' says? "
            f"If you haven't read it this session, read_memory() it first. "
            f"Then: does the aliased content conflict with ANY memory in your current context? "
            f"If yes, use memory-audit-belief-duel skill to resolve it before continuing."
        )

        return msg

    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


@write_tool()
async def manage_triggers(
    uri: str,
    add: Optional[List[str]] = None,
    remove: Optional[List[str]] = None,
) -> str:
    """
    Bind trigger words to a memory so it surfaces automatically during read_memory.

    Triggers are bound to the MEMORY NODE (Memory ID), NOT to any specific path.
    All aliases of the same memory share the same set of triggers.
    (Contrast with priority/disclosure, which are per-path.)

    Mechanism: When a trigger word appears in ANY memory's content, read_memory
    shows a glossary link to this target node at the bottom.

    How to choose trigger words:
    - The trigger word MUST already exist in some older memory's content.
      You are borrowing a word from an existing text to hook a new memory onto it.
    - Do NOT invent obscure placeholder words that appear nowhere in the memory library.
    - Use SPECIFIC terms. Broad/generic words create noise.
    - A node can have multiple triggers. Same trigger can point to multiple nodes.
    - View all triggers: read_memory("system://glossary").

    Args:
        uri: Any URI that points to the target memory node (used to locate the node;
             any alias of the same memory works identically)
        add: List of trigger words to bind to this node (Optional)
        remove: List of trigger words to unbind from this node (Optional)

    Returns:
        Current list of triggers for this node after changes.

    Examples:
        manage_triggers("core://hazards/spa_fallback", add=["Nginx"])
        manage_triggers("writer://story_world/factions", add=["Nuremberg", "Aether"])
    """
    graph = get_graph_service()
    glossary = get_glossary_service()

    try:
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)

        memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())
        if not memory:
            return f"Error: Memory at '{full_uri}' not found."

        node_uuid = memory["node_uuid"]

        if add and remove:
            add_set = {k.strip() for k in add if k.strip()}
            remove_set = {k.strip() for k in remove if k.strip()}
            overlap = add_set.intersection(remove_set)
            if overlap:
                return f"Error: Cannot add and remove the same keywords simultaneously: {', '.join(sorted(overlap))}"

        added = []
        skipped_add = []
        removed = []
        skipped_remove = []

        before_state = {"glossary_keywords": []}
        after_state = {"glossary_keywords": []}

        if add:
            for kw in add:
                kw = kw.strip()
                if not kw:
                    continue
                try:
                    result = await glossary.add_glossary_keyword(kw, node_uuid, namespace=get_namespace())
                    added.append(kw)
                    if "rows_before" in result:
                        before_state["glossary_keywords"].extend(result["rows_before"].get("glossary_keywords", []))
                    if "rows_after" in result:
                        after_state["glossary_keywords"].extend(result["rows_after"].get("glossary_keywords", []))
                except ValueError:
                    skipped_add.append(kw)

        if remove:
            for kw in remove:
                kw = kw.strip()
                if not kw:
                    continue
                result = await glossary.remove_glossary_keyword(kw, node_uuid, namespace=get_namespace())
                if result.get("success"):
                    removed.append(kw)
                    if "rows_before" in result:
                        before_state["glossary_keywords"].extend(result["rows_before"].get("glossary_keywords", []))
                    if "rows_after" in result:
                        after_state["glossary_keywords"].extend(result["rows_after"].get("glossary_keywords", []))
                else:
                    skipped_remove.append(kw)

        if added or removed:
            from db.snapshot import get_changeset_store
            get_changeset_store().record_many(before_state, after_state)

        current = await glossary.get_glossary_for_node(node_uuid, namespace=get_namespace())

        lines = [f"Keywords for '{full_uri}':"]
        if added:
            lines.append(f"  Added: {', '.join(added)}")
        if skipped_add:
            lines.append(f"  Already existed (skipped): {', '.join(skipped_add)}")
        if removed:
            lines.append(f"  Removed: {', '.join(removed)}")
        if skipped_remove:
            lines.append(f"  Not found (skipped): {', '.join(skipped_remove)}")
        if current:
            lines.append(f"  Current: [{', '.join(current)}]")
        else:
            lines.append("  Current: (none)")

        return "\n".join(lines)

    except ValueError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def search_memory(
    query: str, domain: Optional[str] = None, limit: int = 10
) -> str:
    """
    Search memories by path and content using full-text search.

    Use this when you don't know the URI for a memory. Do NOT guess URIs.
    This is lexical full-text search, NOT semantic search.

    Args:
        query: Search keywords (substring match)
        domain: Optional domain filter (e.g., "core", "writer").
                If not specified, searches all domains.
        limit: Maximum results (default 10)

    Returns:
        List of matching memories with URIs and snippets

    Examples:
        search_memory("job")                   # Search all domains
        search_memory("chapter", domain="writer") # Search only writer domain
    """
    search = get_search_indexer()

    try:
        # Validate domain if provided
        valid = get_valid_domains()
        if domain is not None and domain not in valid:
            return f"Error: Unknown domain '{domain}'. Valid domains: {', '.join(valid)}"

        results = await search.search(query, limit, domain, namespace=get_namespace())

        if not results:
            scope = f"in '{domain}'" if domain else "across all domains"
            return f"No matching memories found {scope}."

        lines = [f"Found {len(results)} matches for '{query}':", ""]

        for item in results:
            uri = item.get(
                "uri", make_uri(item.get("domain", DEFAULT_DOMAIN), item["path"])
            )
            lines.append(f"- {uri}")
            lines.append(f"  Priority: {item['priority']}")
            if item.get("disclosure"):
                lines.append(f"  Disclosure: {item['disclosure']}")
            lines.append(f"  {item['snippet']}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {str(e)}"


# =============================================================================
# MCP Resources
# =============================================================================


if __name__ == "__main__":
    mcp.run()
