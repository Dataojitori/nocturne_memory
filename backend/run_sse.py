import os
import sys
import contextlib
import threading
import time
import webbrowser

# Prevent the MCP lifespan from starting a duplicate embedded web server.
os.environ["_NOCTURNE_SSE_MODE"] = "1"

import uvicorn

# Ensure we can import from backend dir
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config as _cfg
from auth import enforce_network_auth
from mcp_server import mcp, IS_MCP_V2, sec_settings
from web_app import build_web_app, default_lifespan


def main():
    """
    Single-process server: MCP transports + REST API + frontend UI.

    After running `npm run build` in frontend/, the admin UI is accessible
    at the same port — no separate dev server needed.
    """
    _cfg.ensure_config_exists()
    port = int(_cfg.get("web_port"))
    host = _cfg.get("host")

    enforce_network_auth(host=host)

    print("Initializing Nocturne Memory Server...")

    # --- MCP transports ---
    if IS_MCP_V2:
        sse_asgi_app = mcp.sse_app(transport_security=sec_settings)
        streamable_asgi_app = mcp.streamable_http_app(transport_security=sec_settings)
    else:
        sse_asgi_app = mcp.sse_app("/")
        streamable_asgi_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def combined_lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(default_lifespan(app))
            await stack.enter_async_context(sse_asgi_app.router.lifespan_context(app))
            await stack.enter_async_context(streamable_asgi_app.router.lifespan_context(app))
            yield

    extra_routes = list(sse_asgi_app.router.routes) + list(streamable_asgi_app.router.routes)

    final_app = build_web_app(
        extra_routes=extra_routes,
        extra_prefixes=["/sse", "/messages", "/mcp"],
        lifespan=combined_lifespan,
    )

    print(f"Server starting on http://{host}:{port}")
    print(f"  MCP (SSE):   http://{host}:{port}/sse")
    print(f"  MCP (HTTP):  http://{host}:{port}/mcp")
    print(f"  REST API:    http://{host}:{port}/api/docs")
    print(f"  Admin UI:    http://{host}:{port}/")

    if _cfg.get("auto_open_browser"):
        def _open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}/")
        threading.Thread(target=_open_browser, daemon=True).start()

    uvicorn.run(final_app, host=host, port=port)


if __name__ == "__main__":
    main()
