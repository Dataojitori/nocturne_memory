from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_build_status_and_retry_endpoints(api_client, monkeypatch):
    from frontend_builder import get_frontend_builder

    builder = get_frontend_builder()
    retry_build = AsyncMock(
        return_value={"status": "started", "build": builder.get_status()}
    )
    monkeypatch.setattr(builder, "retry_build", retry_build)

    # GET /api/build-status
    response = await api_client.get("/build-status")
    assert response.status_code == 200
    data = response.json()
    assert "state" in data
    assert "is_ready" in data
    assert "progress" in data

    # GET /api/build/status (alias)
    response_alias = await api_client.get("/build/status")
    assert response_alias.status_code == 200
    assert response_alias.json()["state"] == data["state"]

    # POST /api/build-retry without API_TOKEN configured (local dev mode)
    retry_resp = await api_client.post("/build-retry")
    assert retry_resp.status_code == 200
    retry_data = retry_resp.json()
    assert retry_data["status"] in ("started", "already_running")
    retry_build.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_build_api_enforces_auth_and_scrubs_details_when_token_configured(monkeypatch):
    import auth
    from frontend_builder import get_frontend_builder
    from web_app import build_web_app

    valid_token = "secret_token_123456789012345678901234"
    monkeypatch.setattr(auth, "get_api_token", lambda: valid_token)

    # Set mock error on builder
    builder = get_frontend_builder()
    builder._set_status(
        state="failed",
        step="编译失败",
        progress=75,
        error_type="build_failed",
        error_summary="Vite build error",
        error_details="Sensitive path /home/user/code/frontend error line 42",
    )
    retry_build = AsyncMock(
        return_value={"status": "started", "build": builder.get_status()}
    )
    monkeypatch.setattr(builder, "retry_build", retry_build)

    app = build_web_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Unauthenticated status check: allowed, but error_details is scrubbed
        resp_unauthed = await client.get("/api/build-status")
        assert resp_unauthed.status_code == 200
        data_unauthed = resp_unauthed.json()
        assert data_unauthed["state"] == "failed"
        assert data_unauthed["error_summary"] == "Vite build error"
        assert data_unauthed["error_details"] is None
        assert data_unauthed["requires_auth_for_details"] is True

        # 2. Authenticated status check: returns full error_details
        resp_authed = await client.get(
            "/api/build-status",
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp_authed.status_code == 200
        data_authed = resp_authed.json()
        assert data_authed["error_details"] == "Sensitive path /home/user/code/frontend error line 42"
        assert data_authed["requires_auth_for_details"] is False

        # 3. Unauthenticated retry: MUST be rejected with 401 Unauthorized
        resp_retry_unauthed = await client.post("/api/build-retry")
        assert resp_retry_unauthed.status_code == 401

        # 4. Invalid token retry: MUST be rejected with 401 Unauthorized
        resp_retry_invalid = await client.post(
            "/api/build-retry",
            headers={"Authorization": "Bearer wrong_token_123"},
        )
        assert resp_retry_invalid.status_code == 401

        # 5. Authenticated retry: succeeds
        resp_retry_authed = await client.post(
            "/api/build-retry",
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert resp_retry_authed.status_code == 200
        assert resp_retry_authed.json()["status"] in ("started", "already_running")
        retry_build.assert_awaited_once_with()
