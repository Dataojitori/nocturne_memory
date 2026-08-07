import pytest

async def test_update_memory_block_nested_ambiguity(mcp_module, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="START\nparagraph 1\nEND\nparagraph 2\nEND",
        priority=1,
        title="block_nested",
        disclosure="test"
    )

    result = await mcp_module.update_memory(
        "core://block_nested",
        old_string="START\n...\nEND",
        new_string="replacement",
    )

    assert "Error" in result
    assert "Ambiguous update" in result
