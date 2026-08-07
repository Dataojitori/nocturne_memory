async def test_read_memory_system_views(mcp_module, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="Agent identity",
        priority=1,
        title="agent",
        disclosure="When booting",
    )
    await graph_service.create_memory(
        parent_path="",
        content="User identity",
        priority=1,
        title="my_user",
        disclosure="When booting",
    )

    boot = await mcp_module.read_memory("system://boot")
    index_view = await mcp_module.read_memory("system://index/core")
    recent = await mcp_module.read_memory("system://recent/5")
    index_error = await mcp_module.read_memory("system://index")

    assert "core://agent" in boot
    assert "core://my_user" in boot
    assert "system://index/<domain>" in boot
    assert "core://agent" in index_view
    assert "core://my_user" in recent
    assert "requires a domain" in index_error


async def test_diagnostic_view_points_duplicate_aliases_to_delete_memory(mcp_module):
    await mcp_module.create_memory(
        "core://",
        "Folder memory",
        priority=1,
        title="folder",
        disclosure="When testing diagnostics",
    )
    await mcp_module.add_alias(
        "core://folder_copy",
        "core://folder",
        priority=2,
        disclosure="When testing duplicate aliases",
    )

    diagnostic = await mcp_module.read_memory("system://diagnostic/core")

    assert "### 3.2 Duplicate Aliases under Same Parent" in diagnostic
    assert "Use `delete_memory` on the redundant alias URI to remove the extra path." in diagnostic
    assert "`delete_path`" not in diagnostic
    assert "core://folder" in diagnostic
    assert "core://folder_copy" in diagnostic


async def test_mcp_tool_flow_covers_crud_alias_triggers_and_search(mcp_module, graph_service):
    created = await mcp_module.create_memory(
        "core://",
        "Important Salem memory",
        priority=2,
        title="salem_note",
        disclosure="When testing MCP tools",
    )
    updated = await mcp_module.update_memory(
        "core://salem_note",
        append="\nGraphService handles aliases.",
    )
    triggers = await mcp_module.manage_triggers(
        "core://salem_note",
        add=["Salem"],
    )
    search = await mcp_module.search_memory("GraphService")
    alias = await mcp_module.add_alias(
        "project://salem_alias",
        "core://salem_note",
        priority=3,
        disclosure="When mirroring note",
    )
    deleted = await mcp_module.delete_memory("project://salem_alias")

    current = await graph_service.get_memory_by_path("salem_note", "core")
    removed_alias = await graph_service.get_memory_by_path("salem_alias", "project")

    assert "Success: Memory created" in created
    assert "Success: Memory at 'core://salem_note' updated" == updated
    assert "Added: Salem" in triggers
    assert "core://salem_note" in search
    assert "Success: Alias 'project://salem_alias'" in alias
    assert "Success: Memory 'project://salem_alias' deleted." in deleted
    assert current["content"].endswith("GraphService handles aliases.")
    assert removed_alias is None


# =============================================================================
# Unit tests for normalize_with_positions
# =============================================================================

from text_patch import normalize_with_positions, try_normalized_patch


class TestNormalizeWithPositions:
    def test_straight_quotes_unchanged(self):
        text = 'She said "hello" to him.'
        norm, pos_map = normalize_with_positions(text)
        assert norm == text
        assert len(pos_map) == len(norm)
        assert all(pos_map[i] == i for i in range(len(norm)))

    def test_curly_quotes_normalized(self):
        text = "She said \u201chello\u201d to him."
        norm, pos_map = normalize_with_positions(text)
        assert norm == 'She said "hello" to him.'
        assert pos_map[0] == 0  # 'S'
        assert pos_map[9] == 9  # curly open quote maps to original pos 9

    def test_dash_variants(self):
        text = "range\u2014end"
        norm, _ = normalize_with_positions(text)
        assert norm == "range-end"

    def test_trailing_whitespace_stripped(self):
        text = "hello   \nworld"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "hello\nworld"
        assert pos_map[5] == 8  # '\n' at original position 8

    def test_consecutive_spaces_collapsed(self):
        text = "hello    world"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "hello world"
        assert pos_map[5] == 5  # first space kept
        assert pos_map[6] == 9  # 'w' at original position 9

    def test_leading_spaces_preserved(self):
        text = "    - item"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "    - item"
        assert len(pos_map) == len(norm)
        assert pos_map[0] == 0
        assert pos_map[3] == 3
        assert pos_map[4] == 4  # '-'

    def test_mixed_leading_and_inline_spaces(self):
        text = "    hello    world"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "    hello world"
        assert len(norm) == 15
        assert pos_map[9] == 9  # space after hello
        assert pos_map[10] == 13 # 'w'

    def test_empty_string(self):
        norm, pos_map = normalize_with_positions("")
        assert norm == ""
        assert pos_map == []

    def test_multiline_with_mixed_issues(self):
        text = "line1   \n  \u201chello\u201d   \nend"
        norm, pos_map = normalize_with_positions(text)
        assert norm == 'line1\n  "hello"\nend'
        assert len(pos_map) == len(norm)

    def test_crlf_normalized_to_lf(self):
        text = "line1\r\nline2\r\nline3"
        norm, pos_map = normalize_with_positions(text)
        assert norm == "line1\nline2\nline3"
        assert len(pos_map) == len(norm)
        assert pos_map[5] == 6  # '\n' maps to the original '\n' which is at index 6
        assert pos_map[6] == 7  # 'l' in line2

    def test_backtick_is_not_normalized(self):
        text = "Run `update` command."
        norm, pos_map = normalize_with_positions(text)
        assert norm == "Run `update` command."


# =============================================================================
# Unit tests for try_normalized_patch
# =============================================================================


class TestTryNormalizedPatch:
    def test_curly_to_straight_quote_patch(self):
        content = "She said \u201chello\u201d to him."
        result = try_normalized_patch(
            content, 'She said "hello"', 'She said "goodbye"'
        )
        assert result is not None
        assert '"goodbye"' in result
        assert "to him." in result

    def test_dash_variant_patch(self):
        content = "range: 10\u201420"
        result = try_normalized_patch(
            content, "range: 10-20", "range: 10-30"
        )
        assert result is not None
        assert "10-30" in result

    def test_trailing_whitespace_patch(self):
        content = "hello   \nworld"
        result = try_normalized_patch(
            content, "hello\nworld", "goodbye\nworld"
        )
        assert result is not None
        assert result.startswith("goodbye\n")

    def test_space_collapse_patch(self):
        content = "A    B    C"
        result = try_normalized_patch(
            content, "A B", "X Y"
        )
        assert result is not None
        assert result.startswith("X Y")
        assert result.endswith("C")

    def test_no_match_returns_none(self):
        result = try_normalized_patch(
            "hello world", "completely different", "replacement"
        )
        assert result is None

    def test_ambiguous_match_returns_none(self):
        content = 'He said "yes". She said "yes".'
        result = try_normalized_patch(
            content, '"yes"', '"no"'
        )
        assert result is None

    def test_exact_match_still_needed_when_no_normalization_diff(self):
        content = "hello world"
        result = try_normalized_patch(
            content, "helo world", "replacement"
        )
        assert result is None


# =============================================================================
# Integration: update_memory with normalized fallback
# =============================================================================


async def test_update_memory_falls_back_to_normalized_patch(
    mcp_module, graph_service
):
    original_content = "Nocturne said \u201cI will not kneel.\u201d That is final."
    await graph_service.create_memory(
        parent_path="",
        content=original_content,
        priority=1,
        title="norm_test",
    )

    result = await mcp_module.update_memory(
        "core://norm_test",
        old_string='Nocturne said "I will not kneel."',
        new_string='Nocturne said "I refuse to kneel."',
    )
    assert "Error" in result


async def test_update_memory_exact_match_takes_priority(
    mcp_module, graph_service
):
    """Exact match must be used when available, not normalized."""
    await graph_service.create_memory(
        parent_path="",
        content="Hello World",
        priority=1,
        title="exact_test",
    )
    result = await mcp_module.update_memory(
        "core://exact_test",
        old_string="Hello",
        new_string="Goodbye",
    )
    assert "Success: Memory at 'core://exact_test' updated" in result
    memory = await graph_service.get_memory_by_path("exact_test", "core")
    assert memory["content"] == "Goodbye World"


async def test_update_memory_normalized_ambiguous_returns_error(
    mcp_module, graph_service
):
    content = "He said \u201cyes\u201d. She said \u201cyes\u201d."
    await graph_service.create_memory(
        parent_path="",
        content=content,
        priority=1,
        title="ambig_test",
    )
    result = await mcp_module.update_memory(
        "core://ambig_test",
        old_string='\u201cyes\u201d',
        new_string='"no"',
    )
    assert "Error" in result
    assert "ambiguous" in result.lower()


async def test_create_memory_preserves_all_content_verbatim(
    mcp_module, graph_service
):
    """create_memory never normalizes — whatever you pass is stored as-is."""
    for title_suffix, content in [
        ("backslash", r"Windows path: C:\tmp\test and regex token: foo\\nbar"),
        ("escaped_multiline", "# Title\\n\\n- alpha\\n- beta"),
        ("single_token", r"Use \n to represent a newline in regex examples."),
        ("code_literal", 'payload = "line1\\n\\nline2"'),
        ("regex", "^(?:[A-Z]\\n)+$"),
        ("plain_prose", "line one\\nline two"),
    ]:
        result = await mcp_module.create_memory(
            "core://",
            content,
            priority=1,
            title=f"verbatim_{title_suffix}",
            disclosure="When verifying verbatim storage",
        )
        assert "Success" in result, f"Failed for {title_suffix}"
        assert "SYSTEM NOTICE" not in result, f"Unexpected normalization for {title_suffix}"
        memory = await graph_service.get_memory_by_path(f"verbatim_{title_suffix}", "core")
        assert memory["content"] == content, f"Content mismatch for {title_suffix}"


async def test_update_memory_preserves_literal_backslash_sequences(
    mcp_module, graph_service
):
    original_content = r"Store C:\tmp\test and regex foo\\nbar literally."
    await graph_service.create_memory(
        parent_path="",
        content=original_content,
        priority=1,
        title="literal_backslash_update",
    )

    result = await mcp_module.update_memory(
        "core://literal_backslash_update",
        old_string=r"C:\tmp\test",
        new_string=r"D:\logs\test",
    )

    assert "Success: Memory at 'core://literal_backslash_update' updated" in result
    memory = await graph_service.get_memory_by_path("literal_backslash_update", "core")
    assert memory["content"] == r"Store D:\logs\test and regex foo\\nbar literally."


async def test_update_memory_normalizes_escaped_newlines_on_patch_fallback(
    mcp_module, graph_service
):
    """When stored content has real newlines but old_string has literal \\n,
    exact match fails, then the newline-normalization fallback kicks in."""
    await graph_service.create_memory(
        parent_path="",
        content="# Title\n\n- alpha\n- beta",
        priority=1,
        title="escaped_multiline_update",
    )

    result = await mcp_module.update_memory(
        "core://escaped_multiline_update",
        old_string="# Title\\n\\n- alpha\\n- beta",
        new_string="# Title\n\n- gamma\n- delta",
    )

    assert "Success: Memory at 'core://escaped_multiline_update' updated" in result
    memory = await graph_service.get_memory_by_path("escaped_multiline_update", "core")
    assert memory["content"] == "# Title\n\n- gamma\n- delta"


async def test_update_memory_exact_match_wins_over_normalization(
    mcp_module, graph_service
):
    """When stored content literally contains \\n sequences, exact match
    succeeds and no normalization occurs."""
    literal_content = 'payload = "line1\\n\\nline2"'
    await graph_service.create_memory(
        parent_path="",
        content=literal_content,
        priority=1,
        title="literal_exact_match",
    )

    result = await mcp_module.update_memory(
        "core://literal_exact_match",
        old_string='payload = "line1\\n\\nline2"',
        new_string='payload = "line1\\n\\nline2\\nline3"',
    )

    assert "Success" in result
    assert "SYSTEM NOTICE" not in result
    memory = await graph_service.get_memory_by_path("literal_exact_match", "core")
    assert memory["content"] == 'payload = "line1\\n\\nline2\\nline3"'


async def test_update_memory_append_preserves_content_verbatim(
    mcp_module, graph_service
):
    """Append mode never normalizes — whatever you pass is appended as-is."""
    await graph_service.create_memory(
        parent_path="",
        content="Base content",
        priority=1,
        title="append_verbatim",
    )

    result = await mcp_module.update_memory(
        "core://append_verbatim",
        append="\\n\\nCode: foo\\n\\nbar",
    )

    assert "Success" in result
    assert "SYSTEM NOTICE" not in result
    memory = await graph_service.get_memory_by_path("append_verbatim", "core")
    assert memory["content"] == "Base content\\n\\nCode: foo\\n\\nbar"


# =============================================================================
# Block patch mode (old_start + old_end + new_string)
# =============================================================================


async def test_update_memory_block_replaces_section(mcp_module, graph_service):
    """Block mode replaces the uniquely matched block."""
    await graph_service.create_memory(
        parent_path="",
        content="## History\nOld stuff\nMore old stuff\n\n## Future\nPlans here",
        priority=1,
        title="block_basic",
    )

    result = await mcp_module.update_memory(
        "core://block_basic",
        old_string="## History\n...\n\n## Future",
        new_string="## History\nRewritten content\n\n## Future",
    )

    assert "Success" in result
    memory = await graph_service.get_memory_by_path("block_basic", "core")
    assert memory["content"] == "## History\nRewritten content\n\n## Future\nPlans here"


async def test_update_memory_block_delete_section(mcp_module, graph_service):
    """Block mode with empty new_string deletes the matched block."""
    await graph_service.create_memory(
        parent_path="",
        content="Keep this\n## Remove\nGarbage\n## End",
        priority=1,
        title="block_delete",
    )

    result = await mcp_module.update_memory(
        "core://block_delete",
        old_string="\n## Remove\n...\n## End",
        new_string="",
    )

    assert "Success" in result
    memory = await graph_service.get_memory_by_path("block_delete", "core")
    assert memory["content"] == "Keep this"


async def test_update_memory_block_no_match_returns_error(mcp_module, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="Nothing matches here",
        priority=1,
        title="block_nomatch",
    )

    result = await mcp_module.update_memory(
        "core://block_nomatch",
        old_string="## NONEXISTENT...## END",
        new_string="replacement",
    )

    assert "Error" in result
    assert "Could not find any match" in result


async def test_update_memory_block_multiple_matches_returns_error(mcp_module, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="## Section\nContent A\n## End\n\n## Section\nContent B\n## End",
        priority=1,
        title="block_multi",
    )

    result = await mcp_module.update_memory(
        "core://block_multi",
        old_string="## Section\n...\n## End",
        new_string="replacement",
    )

    assert "Error" in result
    assert "Ambiguous update" in result
    assert "Match 1" in result
    assert "Match 2" in result


async def test_update_memory_block_missing_end_returns_error(mcp_module, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="Some content",
        priority=1,
        title="block_missing_end",
    )

    result = await mcp_module.update_memory(
        "core://block_missing_end",
        old_string="Some...missing",
        new_string="replacement",
    )

    assert "Error" in result


async def test_update_memory_block_without_new_string_returns_error(mcp_module, graph_service):
    await graph_service.create_memory(
        parent_path="",
        content="Some content",
        priority=1,
        title="block_no_new",
    )

    result = await mcp_module.update_memory(
        "core://block_no_new",
        old_string="Some...content",
    )

    assert "Error" in result
    assert "new_string" in result




async def test_update_memory_block_identical_result_returns_error(mcp_module, graph_service):
    """Block that matches text identical to new_string should fail."""
    await graph_service.create_memory(
        parent_path="",
        content="Hello world",
        priority=1,
        title="block_noop",
    )

    result = await mcp_module.update_memory(
        "core://block_noop",
        old_string="Hello...world",
        new_string="Hello world",
    )

    assert "Error" in result
    assert "identical" in result


async def test_diagnostic_view_respects_config_bloat_min_bytes(mcp_module, monkeypatch):
    import config
    monkeypatch.setattr(config, "get", lambda key: 50 if key == "bloat_min_bytes" else config.DEFAULTS.get(key))

    await mcp_module.create_memory(
        "core://",
        "A" * 60,
        priority=1,
        title="bloated_node_test",
        disclosure="Testing bloat diagnostic config",
    )

    diagnostic = await mcp_module.read_memory("system://diagnostic/core")
    assert "## 4. Bloated Memories" in diagnostic
    assert "core://bloated_node_test" in diagnostic

