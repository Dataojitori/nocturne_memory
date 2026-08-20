from unittest.mock import MagicMock

import pytest
from frontend_builder import FrontendBuildManager


def test_frontend_build_manager_initial_state(tmp_path):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    src_dir.mkdir(parents=True)
    dist_dir.mkdir(parents=True)

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    assert manager.is_ready() is False
    assert manager.has_dist() is False
    status = manager.get_status()
    assert status["is_ready"] is False
    assert status["state"] == "idle"

    # When index.html exists
    (dist_dir / "index.html").write_text("<html></html>")
    manager_with_dist = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    assert manager_with_dist.is_ready() is True
    assert manager_with_dist.has_dist() is True
    status = manager_with_dist.get_status()
    assert status["is_ready"] is True
    assert status["state"] == "ready"
    assert status["progress"] == 100


@pytest.mark.asyncio
async def test_ensure_built_missing_node(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    src_dir.mkdir(parents=True)
    (src_dir / "package.json").write_text('{"version": "1.0.0"}')

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)

    # Mock shutil.which to simulate missing npm
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    success = await manager.ensure_built()
    assert success is False
    status = manager.get_status()
    assert status["state"] == "missing_node"
    assert status["error_type"] == "missing_node"
    assert "PATH" in status["error_summary"]
    assert "npm install" in status["suggested_command"]

    html_out = manager.render_diagnostic_html()
    assert "下载 Node.js" in html_out
    assert 'id="retry-btn"' in html_out


@pytest.mark.asyncio
async def test_up_to_date_dist_does_not_require_node(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    dist_dir.mkdir(parents=True)
    (src_dir / "package.json").write_text('{"version": "1.0.0"}')
    (dist_dir / "index.html").write_text("<html></html>")
    (dist_dir / ".build_version").write_text("1.0.0")

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    success = await manager.ensure_built()

    assert success is True
    assert manager.state == "ready"
    assert manager.is_ready() is True


@pytest.mark.asyncio
async def test_rebuild_installs_dependencies_even_when_vite_exists(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    (src_dir / "node_modules" / "vite").mkdir(parents=True)
    (src_dir / "package.json").write_text('{"version": "2.0.0"}')
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/npm")

    commands = []

    def mock_subprocess_run(cmd, *args, **kwargs):
        commands.append(cmd)
        result = MagicMock(returncode=0, stderr="", stdout="")
        if cmd == "npm run build":
            dist_dir.mkdir(parents=True)
            (dist_dir / "index.html").write_text("<html></html>")
        return result

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)
    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)

    success = await manager.ensure_built()

    assert success is True
    assert commands == ["npm install --no-fund --no-audit", "npm run build"]


@pytest.mark.asyncio
async def test_ensure_built_skipped_when_env_set(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    src_dir.mkdir(parents=True)
    (src_dir / "package.json").write_text('{"version": "1.0.0"}')

    monkeypatch.setenv("SKIP_FRONTEND_BUILD", "true")

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    success = await manager.ensure_built()
    assert success is False  # dist does not exist
    status = manager.get_status()
    assert status["state"] == "skipped"
    assert "SKIP_FRONTEND_BUILD" in status["error_summary"]

    html_out = manager.render_diagnostic_html()
    assert "SKIPPED" in html_out
    assert "badge-skipped" in html_out
    assert "display: none" in html_out


@pytest.mark.asyncio
async def test_ensure_built_npm_install_failure_and_mirror_retry(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    src_dir.mkdir(parents=True)
    (src_dir / "package.json").write_text('{"version": "1.0.0"}')

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/npm")

    # Simulate first npm install failing, second npm install (mirror) succeeding, and npm run build succeeding
    call_count = 0

    def mock_subprocess_run(cmd, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        res = MagicMock()
        if "npm install --no-fund --no-audit" == cmd:
            res.returncode = 1
            res.stderr = "ETIMEDOUT connection to registry.npmjs.org"
            res.stdout = ""
        elif "npmmirror.com" in cmd:
            res.returncode = 0
            res.stderr = ""
            res.stdout = "Installed via mirror"
            dist_dir.mkdir(parents=True, exist_ok=True)
        elif "npm run build" in cmd:
            res.returncode = 0
            res.stderr = ""
            res.stdout = "Built successfully"
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "index.html").write_text("<html></html>")
        return res

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    success = await manager.ensure_built(force=True)
    assert success is True
    assert call_count == 3
    status = manager.get_status()
    assert status["state"] == "ready"
    assert status["is_ready"] is True


@pytest.mark.asyncio
async def test_failed_build_not_masked_by_existing_dist(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<html>Old Build</html>")
    (src_dir / "package.json").write_text('{"version": "2.0.0"}')

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/npm")

    # Simulate npm build failing
    def mock_subprocess_run(cmd, *args, **kwargs):
        res = MagicMock()
        if "npm run build" in cmd:
            res.returncode = 1
            res.stderr = "SyntaxError: Unexpected token in App.tsx"
            res.stdout = ""
        else:
            res.returncode = 0
            res.stderr = ""
            res.stdout = ""
        return res

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    success = await manager.ensure_built(force=True)
    assert success is False
    assert manager.state == "failed"

    # Crucial test: get_status() must NOT flip state back to ready even though index.html exists!
    status = manager.get_status()
    assert status["state"] == "failed"
    assert status["is_ready"] is False
    assert status["has_dist"] is True
    assert status["error_type"] == "build_failed"


@pytest.mark.asyncio
async def test_successful_build_command_without_index_is_reported_as_failure(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    src_dir.mkdir(parents=True)
    (src_dir / "package.json").write_text('{"version": "2.0.0"}')

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/npm")

    def mock_subprocess_run(cmd, *args, **kwargs):
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    success = await manager.ensure_built(force=True)

    assert success is False
    status = manager.get_status()
    assert status["state"] == "failed"
    assert status["is_ready"] is False
    assert status["error_type"] == "missing_build_output"
    assert "index.html" in status["error_details"]


@pytest.mark.asyncio
async def test_ensure_built_catches_top_level_exception(tmp_path, monkeypatch):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    dist_dir.mkdir(parents=True)
    (src_dir / "package.json").write_text('{"version": "1.0.0"}')

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/npm")

    def mock_subprocess_run(cmd, *args, **kwargs):
        raise OSError("Permission denied / disk write failed")

    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    success = await manager.ensure_built(force=True)
    assert success is False
    assert manager.state == "failed"
    status = manager.get_status()
    assert status["error_type"] == "unexpected_exception"
    assert status["error_summary"] == "构建过程异常终止，请查看详细错误日志。"
    assert "Permission denied" not in status["error_summary"]
    assert "Permission denied" in status["error_details"]

    public_status = manager.get_status(include_sensitive=False)
    assert public_status["error_details"] is None
    assert "Permission denied" not in public_status["error_summary"]
    assert public_status["requires_auth_for_details"] is True


def test_render_diagnostic_html_sanitizes_unauthenticated_view(tmp_path):
    src_dir = tmp_path / "frontend"
    dist_dir = src_dir / "dist"
    src_dir.mkdir(parents=True)

    manager = FrontendBuildManager(src_dir=src_dir, dist_dir=dist_dir)
    manager._set_status(
        state="failed",
        step="[1/2] 依赖安装失败",
        progress=40,
        error_type="install_failed",
        error_summary="Network timed out connecting to npm registry",
        error_details="npm ERR! ETIMEDOUT 104.16.25.35 /home/secret_user/project",
        suggested_command="cd frontend && npm install",
    )

    # When unauthenticated and token is configured: error_details must NOT appear in HTML
    html_unauthed = manager.render_diagnostic_html(is_authenticated=False, token_configured=True)
    assert "Nocturne Memory" in html_unauthed
    assert "Network timed out" in html_unauthed
    assert "/home/secret_user/project" not in html_unauthed
    assert "需要管理员 Token 验证" in html_unauthed

    # When authenticated: error_details MUST appear in HTML
    html_authed = manager.render_diagnostic_html(is_authenticated=True, token_configured=True)
    assert "/home/secret_user/project" in html_authed
    assert "查看详细错误终端日志" in html_authed
