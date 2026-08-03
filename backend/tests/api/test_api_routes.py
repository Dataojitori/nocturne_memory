from httpx import ASGITransport, AsyncClient


async def _seed_review_change(graph_service, mcp_module):
    await graph_service.create_memory(
        parent_path="",
        content="Original review content",
        priority=2,
        title="review_item",
        disclosure="When reviewing",
    )
    await mcp_module.update_memory("core://review_item", append="\nPending review update")


async def test_health_endpoint_reports_connected_database(api_client):
    response = await api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


async def test_browse_node_round_trip_update(api_client, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="Workspace content",
        priority=2,
        title="workspace",
        disclosure="When browsing workspace",
    )

    response = await api_client.get("/browse/node", params={"domain": "core", "path": "workspace"})
    assert response.status_code == 200
    assert response.json()["node"]["content"] == "Workspace content"

    update_response = await api_client.put(
        "/browse/node",
        params={"domain": "core", "path": "workspace"},
        json={
            "content": "Updated workspace content",
            "priority": 5,
            "disclosure": "When updating workspace",
        },
    )
    assert update_response.status_code == 200

    refreshed = await api_client.get("/browse/node", params={"domain": "core", "path": "workspace"})
    payload = refreshed.json()["node"]

    assert payload["content"] == "Updated workspace content"
    assert payload["priority"] == 5
    assert payload["disclosure"] == "When updating workspace"


async def test_review_group_diff_and_rollback(api_client, graph_service, mcp_module):
    await _seed_review_change(graph_service, mcp_module)

    groups = await api_client.get("/review/groups")
    group = groups.json()[0]

    diff = await api_client.get(f"/review/groups/{group['node_uuid']}/diff")
    assert diff.status_code == 200
    assert diff.json()["before_content"] == "Original review content"
    assert "Pending review update" in diff.json()["current_content"]

    rollback = await api_client.post(f"/review/groups/{group['node_uuid']}/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["success"] is True

    groups_after = await api_client.get("/review/groups")
    current = await graph_service.get_memory_by_path("review_item", "core")

    assert groups_after.json() == []
    assert current["content"] == "Original review content"


async def test_review_existing_memory_consecutive_updates_are_modified_and_rollback(
    api_client, graph_service, mcp_module
):
    created = await mcp_module.create_memory(
        "core://",
        "Original version",
        2,
        title="review_consecutive_update",
        disclosure="When first reviewing",
    )
    assert "Success" in created

    initial_groups = await api_client.get("/review/groups")
    assert initial_groups.status_code == 200
    initial_group = initial_groups.json()[0]
    assert initial_group["top_level_table"] == "nodes"
    assert initial_group["action"] == "created"

    approved = await api_client.delete(f"/review/groups/{initial_group['node_uuid']}")
    assert approved.status_code == 200
    assert (await api_client.get("/review/groups")).json() == []

    first = await mcp_module.update_memory(
        "core://review_consecutive_update",
        old_string="Original version",
        new_string="Middle version",
        disclosure="When reviewing middle",
    )
    assert "Success" in first

    second = await mcp_module.update_memory(
        "core://review_consecutive_update",
        old_string="Middle version",
        new_string="Final version",
        disclosure="When reviewing final",
    )
    assert "Success" in second

    groups = await api_client.get("/review/groups")
    assert groups.status_code == 200
    group = groups.json()[0]

    assert group["top_level_table"] == "memories"
    assert group["action"] == "modified"

    diff = await api_client.get(f"/review/groups/{group['node_uuid']}/diff")
    assert diff.status_code == 200
    payload = diff.json()

    assert payload["action"] == "modified"
    assert payload["before_content"] == "Original version"
    assert payload["current_content"] == "Final version"
    assert payload["before_meta"]["disclosure"] == "When first reviewing"
    assert payload["current_meta"]["disclosure"] == "When reviewing final"

    rollback = await api_client.post(f"/review/groups/{group['node_uuid']}/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["success"] is True

    current = await graph_service.get_memory_by_path("review_consecutive_update", "core")
    groups_after = await api_client.get("/review/groups")

    assert current["content"] == "Original version"
    assert current["disclosure"] == "When first reviewing"
    assert groups_after.json() == []


async def test_review_rollback_fails_if_previous_memory_version_was_purged(
    api_client, graph_service, mcp_module
):
    created = await graph_service.create_memory(
        parent_path="",
        content="Rollback source version",
        priority=2,
        title="review_missing_old_version",
        disclosure="When old version exists",
    )

    updated = await mcp_module.update_memory(
        "core://review_missing_old_version",
        old_string="Rollback source version",
        new_string="Updated version",
    )
    assert "Success" in updated

    await graph_service.permanently_delete_memory(created["id"])

    groups = await api_client.get("/review/groups")
    assert groups.status_code == 200
    group = groups.json()[0]

    rollback = await api_client.post(f"/review/groups/{group['node_uuid']}/rollback")
    payload = rollback.json()

    current = await graph_service.get_memory_by_path("review_missing_old_version", "core")
    groups_after = await api_client.get("/review/groups")

    assert rollback.status_code == 200
    assert payload["success"] is False
    assert "Cannot restore previous memory content" in payload["message"]
    assert current["content"] == "Updated version"
    assert groups_after.json() != []


async def test_review_rollback_restores_path_even_if_node_is_live_in_other_namespace(
    api_client, graph_service, mcp_module
):
    from sqlalchemy import select

    from db import get_db_manager
    from db.models import Path
    created = await graph_service.create_memory(
        parent_path="",
        content="Shared across namespaces",
        priority=2,
        title="shared_review_item",
        disclosure="When reviewing shared node rollback",
        namespace="ns_a",
    )

    db = get_db_manager()
    async with db.session() as session:
        path_row = (
            await session.execute(
                select(Path).where(
                    Path.namespace == "ns_a",
                    Path.domain == "core",
                    Path.path == "shared_review_item",
                )
            )
        ).scalar_one()
        session.add(
            Path(
                namespace="ns_b",
                domain="core",
                path="shared_review_item",
                edge_id=path_row.edge_id,
            )
        )

    from db.namespace import set_namespace

    set_namespace("ns_a")
    deleted = await mcp_module.delete_memory("core://shared_review_item")
    assert "Success" in deleted

    groups = await api_client.get("/review/groups")
    group = next(
        g for g in groups.json() if g["node_uuid"] == created["node_uuid"]
    )

    rollback = await api_client.post(f"/review/groups/{group['node_uuid']}/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["success"] is True

    restored = await graph_service.get_memory_by_path(
        "shared_review_item", "core", namespace="ns_a"
    )
    still_live_elsewhere = await graph_service.get_memory_by_path(
        "shared_review_item", "core", namespace="ns_b"
    )

    assert restored is not None
    assert restored["node_uuid"] == created["node_uuid"]
    assert still_live_elsewhere is not None
    assert still_live_elsewhere["node_uuid"] == created["node_uuid"]


async def test_review_diff_includes_path_and_glossary_changes(api_client, graph_service, mcp_module):
    await graph_service.create_memory(
        parent_path="",
        content="Searchable linked memory",
        priority=2,
        title="linked_item",
        disclosure="When diffing linked memory",
    )

    await mcp_module.add_alias(
        "project://linked_alias",
        "core://linked_item",
        priority=3,
        disclosure="When mirroring linked memory",
    )
    await mcp_module.manage_triggers("core://linked_item", add=["GraphService"])

    groups = await api_client.get("/review/groups")
    payload = groups.json()
    linked_group = payload[0]

    diff = await api_client.get(f"/review/groups/{linked_group['node_uuid']}/diff")
    payload = diff.json()

    assert any(change["action"] == "created" for change in payload["path_changes"])
    assert any(change["keyword"] == "GraphService" for change in payload["glossary_changes"])


async def test_maintenance_lists_deprecated_and_orphaned_memories(api_client, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="Deprecated source",
        priority=2,
        title="deprecated_item",
        disclosure="When testing maintenance",
    )
    await graph_service.update_memory("deprecated_item", content="Active replacement")

    await graph_service.create_memory(
        parent_path="",
        content="Orphan me",
        priority=2,
        title="orphan_leaf",
        disclosure="When testing maintenance",
    )
    await graph_service.remove_path("orphan_leaf", "core")

    response = await api_client.get("/maintenance/orphans")
    categories = {item["category"] for item in response.json()}

    assert response.status_code == 200
    assert {"deprecated", "orphaned"}.issubset(categories)


async def test_api_requires_bearer_token_when_configured(reload_module, monkeypatch):
    import config
    config.set_value("api_token", "secret-token-that-is-at-least-32-chars-long")

    from db import get_db_manager

    main = reload_module("main")
    await get_db_manager().init_db()

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://testserver/api") as client:
        unauthorized = await client.get("/browse/domains")
        authorized = await client.get(
            "/browse/domains",
            headers={"Authorization": "Bearer secret-token-that-is-at-least-32-chars-long"},
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_config_backfills_bloat_min_bytes(tmp_path, monkeypatch):
    import json
    import config

    test_config_path = tmp_path / "config.json"
    test_config_path.write_text(json.dumps({"host": "127.0.0.1", "web_port": 8233}))

    monkeypatch.setattr(config, "CONFIG_PATH", test_config_path)
    config._invalidate()

    # 内存补齐生效：config.get() 和 config.get_all() 均可获取默认值 2400
    val = config.get("bloat_min_bytes")
    assert val == 2400
    assert config.get_all().get("bloat_min_bytes") == 2400

    # 旧的配置文件在磁盘上保持原样，未进行物理写入，避免只读文件系统等问题
    saved_data = json.loads(test_config_path.read_text())
    assert "bloat_min_bytes" not in saved_data


async def test_settings_api_bloat_min_bytes(api_client, monkeypatch, tmp_path):
    import config

    test_config_path = tmp_path / "config.json"
    test_config_path.write_text('{"host": "127.0.0.1", "web_port": 8233}')
    monkeypatch.setattr(config, "CONFIG_PATH", test_config_path)
    config._invalidate()

    get_res = await api_client.get("/settings")
    assert get_res.status_code == 200
    assert get_res.json()["settings"]["bloat_min_bytes"] == 2400

    invalid_res = await api_client.put("/settings", json={"bloat_min_bytes": 0})
    assert invalid_res.status_code == 422

    update_res = await api_client.put("/settings", json={"bloat_min_bytes": 4096})
    assert update_res.status_code == 200
    assert "bloat_min_bytes" in update_res.json()["updated"]
    assert config.get("bloat_min_bytes") == 4096



