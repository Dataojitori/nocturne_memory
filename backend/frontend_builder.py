"""
Frontend Build and Diagnostic Manager for Nocturne Memory.

Manages the lifecycle of the frontend admin UI build, automatic dependency
installation, mirror fallback for network failures, live status tracking,
and the diagnostic landing page when the UI is building or encountered errors.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

from locales import t

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
FRONTEND_SRC = FRONTEND_DIR.parent


class FrontendBuildManager:
    """Manages frontend build state, auto-build, retry with mirror fallback, and diagnostics."""

    def __init__(self, src_dir: Path = FRONTEND_SRC, dist_dir: Path = FRONTEND_DIR):
        self.src_dir = src_dir
        self.dist_dir = dist_dir
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

        self.state: str = "idle"  # idle, checking, installing, building, ready, failed, missing_node, skipped
        self.step: str = "就绪"
        self.progress: int = 0
        self.error_type: str | None = None
        self.error_summary: str | None = None
        self.error_details: str | None = None
        self.suggested_command: str | None = None
        self.using_mirror: bool = False
        self.last_updated: float = time.time()

        # Check initial state immediately if dist already exists
        if self.has_dist():
            self.state = "ready"
            self.step = "前端 Dashboard 已就绪"
            self.progress = 100

    def has_dist(self) -> bool:
        """Check if frontend dist/index.html exists on disk."""
        return (self.dist_dir / "index.html").is_file()

    def is_ready(self) -> bool:
        """Check if frontend is ready to serve.
        Only True if state is 'ready' or 'skipped' (when dist exists), and dist/index.html is on disk."""
        return self.state in ("ready", "skipped") and self.has_dist()

    def get_status(self, include_sensitive: bool = True) -> dict[str, Any]:
        """Return a snapshot dictionary of the current build status.

        Args:
            include_sensitive: If False (unauthenticated), sanitize/hide error_details.
        """
        ready = self.is_ready()
        details = self.error_details if include_sensitive else None

        return {
            "state": self.state,
            "step": self.step,
            "progress": self.progress,
            "is_ready": ready,
            "has_dist": self.has_dist(),
            "error_type": self.error_type,
            "error_summary": self.error_summary,
            "error_details": details,
            "requires_auth_for_details": bool(self.error_details and not include_sensitive),
            "suggested_command": self.suggested_command,
            "using_mirror": self.using_mirror,
            "last_updated": self.last_updated,
        }

    def _set_status(
        self,
        state: str,
        step: str,
        progress: int,
        error_type: str | None = None,
        error_summary: str | None = None,
        error_details: str | None = None,
        suggested_command: str | None = None,
        using_mirror: bool = False,
    ):
        self.state = state
        self.step = step
        self.progress = progress
        self.error_type = error_type
        self.error_summary = error_summary
        self.error_details = error_details
        self.suggested_command = suggested_command
        self.using_mirror = using_mirror
        self.last_updated = time.time()

    def ensure_built_background(self, force: bool = False) -> asyncio.Task:
        """Trigger background build task if not already building."""
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.ensure_built(force=force))
        return self._task

    async def retry_build(self) -> dict[str, Any]:
        """Manually trigger a re-build (e.g. from UI retry button)."""
        if self._task and not self._task.done():
            return {"status": "already_running", "build": self.get_status()}
        self._task = asyncio.create_task(self.ensure_built(force=True))
        return {"status": "started", "build": self.get_status()}

    async def ensure_built(self, force: bool = False) -> bool:
        """Ensure the frontend is built. Handles node detection, mirror fallback and compilation."""
        async with self._lock:
            try:
                package_json_path = self.src_dir / "package.json"
                if not package_json_path.is_file():
                    self._set_status(
                        state="skipped",
                        step="前端源码 package.json 不存在，跳过自动构建",
                        progress=100,
                        error_summary="未找到 frontend/package.json，已跳过前端构建。若需使用 Admin UI 请确保前端源码完整并执行手动构建。",
                        suggested_command="cd frontend && npm install && npm run build",
                    )
                    return self.has_dist()

                if os.environ.get("SKIP_FRONTEND_BUILD", "").lower() in ("true", "1", "yes"):
                    self._set_status(
                        state="skipped",
                        step="环境变量 SKIP_FRONTEND_BUILD=true，跳过自动构建",
                        progress=100,
                        error_summary="检测到环境变量 SKIP_FRONTEND_BUILD=true，自动构建已被禁用。请手动构建前端或取消该环境变量。",
                        suggested_command="cd frontend && npm install && npm run build",
                    )
                    return self.has_dist()

                # Check if current build version is already up-to-date
                current_version = "unknown"
                try:
                    content = package_json_path.read_text(encoding="utf-8")
                    pkg_data = json.loads(content)
                    current_version = pkg_data.get("version", "unknown")
                except (OSError, json.JSONDecodeError):
                    pass

                build_marker = self.dist_dir / ".build_version"
                if not force and self.has_dist() and build_marker.is_file():
                    try:
                        last_build_version = build_marker.read_text(encoding="utf-8").strip()
                        if last_build_version == current_version and current_version != "unknown":
                            self._set_status(
                                state="ready",
                                step="前端 Dashboard 已就绪 (Up to date)",
                                progress=100,
                            )
                            return True
                    except OSError:
                        pass

                # A matching pre-built dist does not require Node.js at runtime.
                # Only require npm after determining that a rebuild is necessary.
                npm_path = shutil.which("npm")
                if not npm_path:
                    print(t("startup.npm_not_found"), file=sys.stderr)
                    self._set_status(
                        state="missing_node",
                        step="未检测到 Node.js / npm 环境 (Node.js not found in PATH)",
                        progress=0,
                        error_type="missing_node",
                        error_summary="系统环境变量 PATH 中未找到 npm。Admin Dashboard 首次运行需要 Node.js (推荐 v18+) 打包静态页面。",
                        suggested_command="cd frontend && npm install && npm run build",
                    )
                    return False

                # Step 1: Install dependencies
                print(t("startup.building"), file=sys.stderr)
                self._set_status(
                    state="installing",
                    step="[1/2] 正在安装前端依赖 (npm install)...",
                    progress=25,
                )

                # A rebuild must synchronize the complete dependency tree. The
                # presence of one package (for example vite) is not sufficient.
                print(t("startup.step_progress").format(label=t("startup.installing_deps")), file=sys.stderr)
                install_cmd = "npm install --no-fund --no-audit"
                res = await asyncio.to_thread(
                    subprocess.run,
                    install_cmd,
                    cwd=str(self.src_dir),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    shell=True,
                )

                if res.returncode != 0:
                    err_output = (res.stderr or res.stdout or "").strip()
                    # Check if error might be network-related and try npmmirror
                    print(f"[Nocturne] npm install failed (exit {res.returncode}). Retrying with npmmirror...", file=sys.stderr)
                    self._set_status(
                        state="installing",
                        step="[1/2] 官方源连接失败，正在切换国内镜像源重试 (npmmirror)...",
                        progress=40,
                        using_mirror=True,
                    )

                    mirror_cmd = "npm install --no-fund --no-audit --registry=https://registry.npmmirror.com"
                    res_mirror = await asyncio.to_thread(
                        subprocess.run,
                        mirror_cmd,
                        cwd=str(self.src_dir),
                        capture_output=True,
                        text=True,
                        errors="replace",
                        shell=True,
                    )

                    if res_mirror.returncode != 0:
                        mirror_err = (res_mirror.stderr or res_mirror.stdout or err_output).strip()
                        print(
                            t("startup.build_failed").format(
                                cmd=mirror_cmd, exit_code=res_mirror.returncode, error_msg=mirror_err
                            ),
                            file=sys.stderr,
                        )
                        self._set_status(
                            state="failed",
                            step="[1/2] 依赖安装失败 (npm install failed)",
                            progress=40,
                            error_type="install_failed",
                            error_summary=f"前端依赖安装失败 (exit code {res_mirror.returncode})。可能是网络超时或权限问题。",
                            error_details=mirror_err,
                            suggested_command="cd frontend && npm config set registry https://registry.npmmirror.com && npm install && npm run build",
                            using_mirror=True,
                        )
                        return False

                # Step 2: Compile frontend
                self._set_status(
                    state="building",
                    step="[2/2] 正在编译前端代码 (npm run build)...",
                    progress=75,
                )
                print(t("startup.step_progress").format(label=t("startup.compiling")), file=sys.stderr)
                build_cmd = "npm run build"
                res_build = await asyncio.to_thread(
                    subprocess.run,
                    build_cmd,
                    cwd=str(self.src_dir),
                    capture_output=True,
                    text=True,
                    errors="replace",
                    shell=True,
                )

                if res_build.returncode != 0:
                    build_err = (res_build.stderr or res_build.stdout or "").strip()
                    print(
                        t("startup.build_failed").format(
                            cmd=build_cmd, exit_code=res_build.returncode, error_msg=build_err
                        ),
                        file=sys.stderr,
                    )
                    self._set_status(
                        state="failed",
                        step="[2/2] 前端代码编译失败 (npm run build failed)",
                        progress=75,
                        error_type="build_failed",
                        error_summary=f"前端代码编译失败 (exit code {res_build.returncode})。",
                        error_details=build_err,
                        suggested_command="cd frontend && npm run build",
                    )
                    return False

                if not self.has_dist():
                    expected_index = self.dist_dir / "index.html"
                    self._set_status(
                        state="failed",
                        step="[2/2] 前端构建未生成入口文件",
                        progress=90,
                        error_type="missing_build_output",
                        error_summary="前端构建命令已完成，但未找到 Dashboard 入口文件。",
                        error_details=f"Expected build output was not found: {expected_index}",
                        suggested_command="cd frontend && npm run build",
                    )
                    return False

                # Success
                try:
                    if current_version != "unknown" and self.dist_dir.is_dir():
                        build_marker.write_text(current_version, encoding="utf-8")
                except OSError:
                    pass

                self._set_status(
                    state="ready",
                    step="前端 Dashboard 已就绪",
                    progress=100,
                )
                print(t("startup.admin_ready"), file=sys.stderr)
                return True
            except Exception as e:
                err_msg = str(e)
                print(f"[Nocturne] Unexpected error during frontend build: {err_msg}", file=sys.stderr)
                self._set_status(
                    state="failed",
                    step="构建过程中发生未预期的异常 (Unexpected Build Error)",
                    progress=0,
                    error_type="unexpected_exception",
                    error_summary="构建过程异常终止，请查看详细错误日志。",
                    error_details=f"Exception: {type(e).__name__}: {err_msg}",
                    suggested_command="cd frontend && npm install && npm run build",
                )
                return False

    def render_diagnostic_html(self, is_authenticated: bool = True, token_configured: bool = False) -> str:
        """Render self-contained, live-refreshing HTML diagnostic page."""
        status = self.get_status(include_sensitive=is_authenticated)
        state = status["state"]
        step = html.escape(status["step"] or "")
        progress = status["progress"]
        error_summary = html.escape(status["error_summary"] or "")
        error_details = html.escape(status["error_details"] or "")
        suggested_cmd = html.escape(status["suggested_command"] or "cd frontend && npm install && npm run build")
        requires_auth_details = status.get("requires_auth_for_details", False)

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nocturne Memory - Admin UI Status</title>
  <style>
    :root {{
      --bg: #0b0f19;
      --card-bg: #141b2d;
      --card-border: rgba(255, 255, 255, 0.08);
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --accent: #6366f1;
      --accent-glow: rgba(99, 102, 241, 0.25);
      --success: #10b981;
      --error: #ef4444;
      --warning: #f59e0b;
      --code-bg: #070a13;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }}
    .container {{
      width: 100%;
      max-width: 680px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 2.25rem;
      box-shadow: 0 20px 40px -15px rgba(0, 0, 0, 0.5), 0 0 50px -10px var(--accent-glow);
    }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 1.5rem;
      padding-bottom: 1.25rem;
      border-bottom: 1px solid var(--card-border);
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }}
    .logo-icon {{
      width: 36px;
      height: 36px;
      background: linear-gradient(135deg, #6366f1, #a855f7);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: bold;
      font-size: 1.1rem;
      color: #fff;
    }}
    .brand h1 {{
      font-size: 1.2rem;
      font-weight: 700;
      letter-spacing: 0.5px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      font-size: 0.75rem;
      font-weight: 600;
      padding: 0.35rem 0.75rem;
      border-radius: 9999px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .badge-building {{
      background: rgba(99, 102, 241, 0.15);
      color: #818cf8;
      border: 1px solid rgba(99, 102, 241, 0.3);
    }}
    .badge-error {{
      background: rgba(239, 68, 68, 0.15);
      color: #f87171;
      border: 1px solid rgba(239, 68, 68, 0.3);
    }}
    .badge-ready {{
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }}
    .badge-skipped {{
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }}
    .pulse-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      animation: pulse 1.5s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.4; transform: scale(0.85); }}
    }}
    .status-section {{
      margin: 1.5rem 0;
    }}
    .status-title {{
      font-size: 1.1rem;
      font-weight: 600;
      margin-bottom: 0.5rem;
    }}
    .status-desc {{
      font-size: 0.9rem;
      color: var(--text-muted);
      line-height: 1.5;
    }}
    .progress-bar-container {{
      width: 100%;
      height: 8px;
      background: rgba(255, 255, 255, 0.06);
      border-radius: 9999px;
      overflow: hidden;
      margin: 1.25rem 0;
    }}
    .progress-bar {{
      height: 100%;
      background: linear-gradient(90deg, #6366f1, #a855f7);
      border-radius: 9999px;
      transition: width 0.4s ease;
      width: {progress}%;
    }}
    .code-box {{
      background: var(--code-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 1rem;
      margin: 1.25rem 0;
      position: relative;
    }}
    .code-box pre {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.85rem;
      color: #cbd5e1;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }}
    .code-box-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.5rem;
      font-size: 0.75rem;
      color: var(--text-muted);
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      padding: 0.65rem 1.25rem;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      transition: all 0.2s ease;
      border: none;
    }}
    .btn-primary {{
      background: #6366f1;
      color: white;
    }}
    .btn-primary:hover {{
      background: #4f46e5;
    }}
    .btn-secondary {{
      background: rgba(255, 255, 255, 0.08);
      color: var(--text);
      border: 1px solid var(--card-border);
    }}
    .btn-secondary:hover {{
      background: rgba(255, 255, 255, 0.12);
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1.5rem;
    }}
    details {{
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 0.75rem 1rem;
      margin-top: 1rem;
    }}
    summary {{
      cursor: pointer;
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-muted);
      user-select: none;
    }}
    summary:hover {{ color: var(--text); }}
    .auth-box {{
      margin-top: 1rem;
      padding: 1rem;
      background: rgba(99, 102, 241, 0.08);
      border: 1px solid rgba(99, 102, 241, 0.2);
      border-radius: 8px;
    }}
    .auth-input {{
      flex: 1;
      padding: 0.5rem 0.75rem;
      background: #070a13;
      border: 1px solid var(--card-border);
      border-radius: 6px;
      color: #fff;
      font-size: 0.85rem;
      outline: none;
    }}
    .toast {{
      position: fixed;
      bottom: 2rem;
      left: 50%;
      transform: translateX(-50%);
      background: #1e293b;
      color: #fff;
      padding: 0.5rem 1rem;
      border-radius: 8px;
      font-size: 0.85rem;
      border: 1px solid rgba(255, 255, 255, 0.1);
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
      z-index: 999;
    }}
    .toast.show {{ opacity: 1; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="brand">
        <div class="logo-icon">N</div>
        <div>
          <h1>Nocturne Memory</h1>
          <p style="font-size: 0.75rem; color: var(--text-muted);">Admin Dashboard Initialization</p>
        </div>
      </div>
      <div id="status-badge" class="badge {'badge-ready' if state == 'ready' else 'badge-skipped' if state == 'skipped' else 'badge-error' if state in ('failed', 'missing_node') else 'badge-building'}">
        <div class="pulse-dot"></div>
        <span id="badge-text">{'READY' if state == 'ready' else 'SKIPPED' if state == 'skipped' else 'FAILED' if state == 'failed' else 'MISSING NODE' if state == 'missing_node' else 'BUILDING'}</span>
      </div>
    </div>

    <div class="status-section">
      <div class="status-title" id="status-step">{step}</div>
      <p class="status-desc" id="status-desc">
        {error_summary if error_summary else '环境变量 SKIP_FRONTEND_BUILD=true 或 package.json 不存在，已跳过前端自动构建。' if state == 'skipped' else '首次启动需要自动安装依赖并构建管理面板。构建完成后本页面将自动刷新进入系统。' if state in ('idle', 'checking', 'installing', 'building') else '请根据下方说明排查问题。'}
      </p>

      <div class="progress-bar-container" id="progress-container" style="display: {'none' if state in ('failed', 'missing_node', 'skipped') else 'block'};">
        <div class="progress-bar" id="progress-bar" style="width: {progress}%;"></div>
      </div>
    </div>

    <div id="guidance-section">
      <div class="code-box">
        <div class="code-box-header">
          <span>手动执行构建命令 (Manual Build Command)</span>
          <button class="btn btn-secondary" style="padding: 0.25rem 0.5rem; font-size: 0.75rem;" onclick="copyCommand()">复制</button>
        </div>
        <pre><code id="cmd-text">{suggested_cmd}</code></pre>
      </div>

      <div class="actions">
        {'<a href="https://nodejs.org/" target="_blank" class="btn btn-secondary">👉 下载 Node.js (LTS)</a>' if state == 'missing_node' else ''}
        <button id="retry-btn" class="btn btn-primary" onclick="triggerRetry()">🔄 重新检测并重试构建</button>
        <button class="btn btn-secondary" onclick="window.location.reload()">刷新页面</button>
      </div>

      <div id="auth-section" class="auth-box" style="display: {'block' if token_configured else 'none'};">
        <div style="font-size: 0.85rem; font-weight: 600; margin-bottom: 0.4rem; color: #a5b4fc;">
          🔒 管理员 Token 认证 (Admin Token Authentication)
        </div>
        <p style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.6rem;">
          当前服务已配置 API_TOKEN 保护。请输入 Token 以解锁完整错误日志或触发重试构建。
        </p>
        <div style="display: flex; gap: 0.5rem;">
          <input id="api-token-input" class="auth-input" type="password" placeholder="输入 API_TOKEN (Bearer Token)..." />
          <button class="btn btn-primary" style="padding: 0.5rem 1rem; font-size: 0.85rem;" onclick="saveTokenAndVerify()">验证 Token</button>
        </div>
      </div>

      <div id="error-details-box" style="display: {'block' if error_details or requires_auth_details else 'none'}; margin-top: 1rem;">
        <details id="error-details-tag" {'open' if error_details else ''}>
          <summary id="error-summary-label">
            {'🔒 详细错误日志（需要管理员 Token 验证）' if requires_auth_details else '查看详细错误终端日志 (Terminal Output)'}
          </summary>
          <div class="code-box" style="margin-top: 0.5rem; max-height: 240px; overflow-y: auto;">
            <pre><code id="error-log">{error_details if error_details else '请在上方输入 API_TOKEN 后点击【验证 Token】以查看完整终端报错日志。'}</code></pre>
          </div>
        </details>
      </div>
    </div>
  </div>

  <div id="toast" class="toast">已复制到剪贴板</div>

  <script>
    let isRetrying = false;
    const TOKEN_KEY = 'nocturne_admin_api_token';

    function getSavedToken() {{
      return sessionStorage.getItem(TOKEN_KEY) || '';
    }}

    function setSavedToken(token) {{
      if (token) {{
        sessionStorage.setItem(TOKEN_KEY, token.trim());
      }} else {{
        sessionStorage.removeItem(TOKEN_KEY);
      }}
    }}

    function showToast(msg) {{
      const toast = document.getElementById('toast');
      toast.innerText = msg;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 2000);
    }}

    function copyCommand() {{
      const cmd = document.getElementById('cmd-text').innerText;
      navigator.clipboard.writeText(cmd).then(() => showToast('命令已复制到剪贴板'));
    }}

    function getAuthHeaders() {{
      const token = getSavedToken();
      return token ? {{ 'Authorization': 'Bearer ' + token }} : {{}};
    }}

    async function saveTokenAndVerify() {{
      const input = document.getElementById('api-token-input');
      const token = input ? input.value.trim() : '';
      if (!token) {{
        showToast('请输入有效的 API Token');
        return;
      }}
      setSavedToken(token);
      showToast('正在验证 Token...');
      await pollStatus();
    }}

    async function pollStatus() {{
      try {{
        const res = await fetch('/api/build-status', {{
          headers: getAuthHeaders()
        }});
        if (!res.ok) return;
        const data = await res.json();

        // Update UI
        document.getElementById('status-step').innerText = data.step || '';
        document.getElementById('progress-bar').style.width = (data.progress || 0) + '%';

        const badge = document.getElementById('status-badge');
        const badgeText = document.getElementById('badge-text');
        const progressContainer = document.getElementById('progress-container');
        const desc = document.getElementById('status-desc');

        if (data.is_ready) {{
          badge.className = 'badge badge-ready';
          badgeText.innerText = 'READY';
          document.getElementById('status-step').innerText = '前端构建成功！正在进入管理面板...';
          desc.innerText = '正在为您跳转...';
          setTimeout(() => window.location.reload(), 800);
          return;
        }} else if (data.state === 'skipped') {{
          badge.className = 'badge badge-skipped';
          badgeText.innerText = 'SKIPPED';
          progressContainer.style.display = 'none';
          desc.innerText = data.error_summary || data.step || '已跳过自动构建。';
        }} else if (data.state === 'failed' || data.state === 'missing_node') {{
          badge.className = 'badge badge-error';
          badgeText.innerText = data.state === 'missing_node' ? 'MISSING NODE' : 'FAILED';
          progressContainer.style.display = 'none';
          desc.innerText = data.error_summary || '构建遇到错误，请参考下方说明。';
        }} else {{
          badge.className = 'badge badge-building';
          badgeText.innerText = 'BUILDING';
          progressContainer.style.display = 'block';
          desc.innerText = '首次启动需要自动安装依赖并构建管理面板。构建完成后本页面将自动刷新进入系统。';
        }}

        if (data.suggested_command) {{
          document.getElementById('cmd-text').innerText = data.suggested_command;
        }}

        const errBox = document.getElementById('error-details-box');
        const summaryLabel = document.getElementById('error-summary-label');
        const errorLog = document.getElementById('error-log');

        if (data.error_details) {{
          errBox.style.display = 'block';
          summaryLabel.innerText = '查看详细错误终端日志 (Terminal Output)';
          errorLog.innerText = data.error_details;
          if (getSavedToken()) {{
            showToast('Token 验证成功');
          }}
        }} else if (data.requires_auth_for_details) {{
          errBox.style.display = 'block';
          summaryLabel.innerText = '🔒 详细错误日志（需要管理员 Token 验证）';
          errorLog.innerText = '请在上方输入 API_TOKEN 后点击【验证 Token】以查看完整终端报错日志。';
        }}
      }} catch (e) {{
        console.warn('Status poll failed:', e);
      }}

      if (!isRetrying) {{
        setTimeout(pollStatus, 1500);
      }}
    }}

    async function triggerRetry() {{
      const btn = document.getElementById('retry-btn');
      if (btn) {{
        btn.disabled = true;
        btn.innerText = '⏳ 正在重新构建...';
      }}
      isRetrying = true;
      document.getElementById('status-step').innerText = '正在准备重试构建...';
      document.getElementById('progress-container').style.display = 'block';
      document.getElementById('progress-bar').style.width = '10%';

      try {{
        const res = await fetch('/api/build-retry', {{
          method: 'POST',
          headers: getAuthHeaders()
        }});
        if (res.status === 401) {{
          showToast('❌ 重试失败：需要管理员 Token 认证');
          const authInput = document.getElementById('api-token-input');
          if (authInput) authInput.focus();
        }} else if (res.ok) {{
          showToast('已开始重新构建');
        }}
      }} catch (e) {{
        console.error('Retry error:', e);
      }}

      setTimeout(() => {{
        isRetrying = false;
        if (btn) {{
          btn.disabled = false;
          btn.innerText = '🔄 重新检测并重试构建';
        }}
        pollStatus();
      }}, 1000);
    }}

    // Initialize token input if saved
    window.addEventListener('DOMContentLoaded', () => {{
      const saved = getSavedToken();
      const input = document.getElementById('api-token-input');
      if (saved && input) {{
        input.value = saved;
      }}
    }});

    // Start polling
    setTimeout(pollStatus, 1000);
  </script>
</body>
</html>
"""


_manager: FrontendBuildManager | None = None


def get_frontend_builder() -> FrontendBuildManager:
    """Get the global singleton FrontendBuildManager."""
    global _manager
    if _manager is None:
        _manager = FrontendBuildManager()
    return _manager
