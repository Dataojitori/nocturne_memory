"""
System view generators for special ``system://`` URIs.

Each function produces a formatted text view from the memory graph
(boot, index, recent, glossary, diagnostic).  They are called by
``read_memory`` in ``mcp_server`` when a ``system://`` URI is requested.

Imports from ``mcp_server`` (parse_uri, make_uri, config constants) are
done inside function bodies to avoid circular imports at module level.
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from db import get_graph_service, get_glossary_service
from db.namespace import get_namespace
from locales import t


async def fetch_and_format_memory(uri: str, track_access: bool = False) -> str:
    """
    Fetch memory data and return a formatted string.
    Used by read_memory tool and boot view.
    """
    import config as _cfg
    from mcp_server import parse_uri, make_uri, DEFAULT_DOMAIN

    graph = get_graph_service()
    glossary = get_glossary_service()
    domain, path = parse_uri(uri)

    memory = await graph.get_memory_by_path(path, domain, namespace=get_namespace())

    if not memory:
        raise ValueError(t("system.uri_not_found").format(
            uri=make_uri(domain, path)))

    if track_access and memory.get("node_uuid"):
        asyncio.create_task(
            graph.log_access(
                memory["node_uuid"],
                namespace=get_namespace(),
                context="mcp_read"
            )
        )

    children = await graph.get_children(
        memory["node_uuid"],
        context_domain=domain,
        context_path=path,
        namespace=get_namespace(),
    )

    lines = []

    disp_domain = memory.get("domain", DEFAULT_DOMAIN)
    disp_path = memory.get("path", "unknown")
    disp_uri = make_uri(disp_domain, disp_path)

    lines.append("=" * 60)
    lines.append("")
    lines.append(f"MEMORY: {disp_uri}")
    lines.append(f"Memory ID: {memory.get('id')}")
    lines.append(f"Other Aliases: {memory.get('alias_count', 0)}")
    lines.append(f"Priority: {memory.get('priority', 0)}")
    
    created_at = memory.get("created_at")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at)
            lines.append(f"Last Modified: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            lines.append(f"Last Modified: {created_at}")
    else:
        lines.append("Last Modified: (unknown)")

    disclosure = memory.get("disclosure")
    if disclosure:
        lines.append(f"Disclosure: {disclosure}")
    else:
        lines.append("Disclosure: (not set)")

    node_keywords = await glossary.get_glossary_for_node(memory["node_uuid"], namespace=get_namespace())
    if node_keywords:
        lines.append(f"Keywords: [{', '.join(node_keywords)}]")
    else:
        lines.append("Keywords: (none)")

    content = memory.get("content", "(empty)")
    bloat_min = _cfg.get("bloat_min_bytes")
    if bloat_min is not None and bloat_min > 0:
        content_bytes = len(content.encode("utf-8"))
        if content_bytes >= bloat_min:
            lines.append(f"Content Size: {content_bytes / 1024:.1f} KB")

    lines.append("")
    lines.append("=" * 60)
    lines.append("")
    lines.append(content)
    lines.append("")

    try:
        glossary_matches = await glossary.find_glossary_in_content(content, namespace=get_namespace())
        if glossary_matches:
            current_node_uuid = memory["node_uuid"]

            uri_to_keywords: Dict[str, List[str]] = {}
            for kw, nodes in glossary_matches.items():
                for n in nodes:
                    if n["node_uuid"] == current_node_uuid or n["uri"].startswith("unlinked://"):
                        continue
                    target_uri = n["uri"]
                    if target_uri not in uri_to_keywords:
                        uri_to_keywords[target_uri] = []
                    if kw not in uri_to_keywords[target_uri]:
                        uri_to_keywords[target_uri].append(kw)

            lines_to_add: List[str] = []
            if uri_to_keywords:
                for target_uri, kws in sorted(uri_to_keywords.items(), key=lambda x: (-len(x[1]), x[0])):
                    sorted_kws = sorted(kws)
                    kw_str = ", ".join(f"@{k}" for k in sorted_kws)
                    lines_to_add.append(f"- {kw_str} -> {target_uri}")

            if lines_to_add:
                lines.append("=" * 60)
                lines.append("")
                lines.append("GLOSSARY (keywords detected in this content)")
                lines.append("")
                lines.extend(lines_to_add)
                lines.append("")
    except Exception:
        pass

    if children:
        lines.append("=" * 60)
        lines.append("")
        lines.append("CHILD MEMORIES (Use 'read_memory' with URI to access)")
        lines.append("")
        lines.append("=" * 60)
        lines.append("")

        for child in children:
            child_domain = child.get("domain", disp_domain)
            child_path = child.get("path", "")
            child_uri = make_uri(child_domain, child_path)

            child_disclosure = child.get("disclosure")
            snippet = child.get("content_snippet", "")

            lines.append(f"- URI: {child_uri}  ")
            lines.append(f"  Priority: {child.get('priority', 0)}  ")

            if child_disclosure:
                lines.append(f"  When to recall: {child_disclosure}  ")
            else:
                lines.append("  When to recall: (not set)  ")
                lines.append(f"  Snippet: {snippet}  ")

            lines.append("")

    return "\n".join(lines)


async def generate_boot_memory_view(core_memory_uris: List[str]) -> str:
    """Generate the system boot memory view (system://boot)."""

    results = []
    loaded = 0
    failed = []

    for uri in core_memory_uris:
        try:
            content = await fetch_and_format_memory(uri, track_access=True)
            results.append(content)
            loaded += 1
        except Exception as e:
            failed.append(f"- {uri}: {str(e)}")

    output_parts = []

    output_parts.append("# Core Memories")
    output_parts.append(f"# Loaded: {loaded}/{len(core_memory_uris)} memories")
    output_parts.append("")

    if failed:
        output_parts.append("## Failed to load:")
        output_parts.extend(failed)
        output_parts.append("")

    if results:
        output_parts.append("## Contents:")
        output_parts.append("")
        output_parts.append(
            "For a memory index, use: system://index/<domain> (e.g. system://index/core)"
        )
        output_parts.append("For recent memories, use: system://recent")
        output_parts.extend(results)
    else:
        output_parts.append("(No core memories loaded. Run migration first.)")

    try:
        recent_view = await generate_recent_memories_view(limit=5)
        output_parts.append("")
        output_parts.append("---")
        output_parts.append("")
        output_parts.append(recent_view)
    except Exception:
        pass

    return "\n".join(output_parts)


async def generate_memory_index_view(domain_filter: Optional[str] = None) -> str:
    """
    Generate a memory index view.

    Public callers should use `system://index/<domain>`. Passing `None`
    keeps the helper usable for internal all-domain views.

    Node-centric: each conceptual entity (node_uuid) appears once per domain,
    with aliases within the same domain folded underneath its primary path.
    """
    from mcp_server import DEFAULT_DOMAIN, make_uri

    graph = get_graph_service()

    try:
        paths = await graph.get_all_paths(namespace=get_namespace())

        node_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for item in paths:
            domain = item.get("domain", DEFAULT_DOMAIN)
            if domain_filter and domain != domain_filter:
                continue
            nid = item.get("node_uuid", "")
            node_groups.setdefault((domain, nid), []).append(item)

        entries = []
        for _key, items in node_groups.items():
            items.sort(
                key=lambda x: (
                    x["path"].count("/"),
                    x.get("priority", 0),
                    len(x["path"]),
                    x.get("uri", ""),
                )
            )
            entries.append(items[0])

        domains: Dict[str, Dict[str, list]] = {}
        for primary in entries:
            domain = primary.get("domain", DEFAULT_DOMAIN)
            domains.setdefault(domain, {})
            top_level = primary["path"].split("/")[0] if primary["path"] else "(root)"
            domains[domain].setdefault(top_level, []).append(primary)

        unique_nodes_count = len(set(nid for _, nid in node_groups.keys()))
        lines = [
            "# Memory Index",
            f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"# Domain Filter: {domain_filter}"
            if domain_filter
            else "# Domain Filter: None (All Domains)",
            f"# Total: {unique_nodes_count} unique nodes",
            "#",
            "# \u26a0\ufe0f ATTENTION (LLM):",
            "# This index ONLY shows ONE primary path per memory node.",
            "# Other aliases, triggers, and child nodes located under those hidden aliases ARE NOT SHOWN HERE.",
            "# DO NOT assume the children shown here are the ONLY children of a node.",
            "# To see ALL paths, aliases, and triggers for a specific memory, you MUST use `read_memory()` on its URI.",
            "#",
            "# Legend: [#ID] = Memory ID, [\u2605N] = priority (lower = higher)",
            "",
        ]

        for domain_name in sorted(domains.keys()):
            if domain_filter and domain_name != domain_filter:
                continue
            lines.append("# " + "\u2550" * 38)
            lines.append(f"# DOMAIN: {domain_name}://")
            lines.append("# " + "\u2550" * 38)
            lines.append("")

            for group_name in sorted(domains[domain_name].keys()):
                lines.append(f"## {group_name}")
                for primary in sorted(
                    domains[domain_name][group_name],
                    key=lambda x: x["path"],
                ):
                    uri = primary.get("uri", make_uri(domain_name, primary["path"]))
                    priority = primary.get("priority", 0)
                    memory_id = primary.get("memory_id", "?")
                    imp_str = f" [\u2605{priority}]"
                    lines.append(f"  - {uri} [#{memory_id}]{imp_str}")
                lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return t("system.error_index").format(error=str(e))


async def generate_recent_memories_view(limit: int = 10) -> str:
    """
    Generate a view of recently modified memories (system://recent).

    Queries non-deprecated memories ordered by created_at DESC,
    only including those that have at least one URI in the paths table.
    """
    graph = get_graph_service()

    try:
        results = await graph.get_recent_memories(limit=limit, namespace=get_namespace())

        lines = []
        lines.append("# Recently Modified Memories")
        lines.append(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(
            f"# Showing: {len(results)} most recent entries (requested: {limit})"
        )
        lines.append("")

        if not results:
            lines.append("(No memories found.)")
            return "\n".join(lines)

        for i, item in enumerate(results, 1):
            uri = item["uri"]
            priority = item.get("priority", 0)
            disclosure = item.get("disclosure")
            raw_ts = item.get("created_at", "")

            if raw_ts and len(raw_ts) >= 16:
                modified = raw_ts[:10] + " " + raw_ts[11:16]
            else:
                modified = raw_ts or "unknown"

            imp_str = f"\u2605{priority}"

            lines.append(f"{i}. {uri}  [{imp_str}]  modified: {modified}")
            if disclosure:
                lines.append(f"   disclosure: {disclosure}")
            else:
                lines.append("   disclosure: (NOT SET \u2014 consider adding one)")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return t("system.error_recent").format(error=str(e))


async def generate_glossary_index_view() -> str:
    """Generate a view of all glossary keywords and their bound nodes (system://glossary)."""
    glossary = get_glossary_service()

    try:
        raw_entries = await glossary.get_all_glossary(namespace=get_namespace())

        entries = []
        for entry in raw_entries:
            valid_nodes = [
                node for node in entry.get("nodes", [])
                if not node.get("uri", "").startswith("unlinked://")
            ]
            if valid_nodes:
                entries.append({
                    "keyword": entry["keyword"],
                    "nodes": valid_nodes
                })

        lines = [
            "# Glossary Index",
            f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"# Total: {len(entries)} keywords",
            "",
        ]

        if not entries:
            lines.append("(No glossary keywords defined yet.)")
            lines.append("")
            lines.append(
                "Use manage_triggers(uri, add=[...]) to bind trigger words to memory nodes."
            )
            return "\n".join(lines)

        for entry in entries:
            kw = entry["keyword"]
            nodes = entry["nodes"]
            lines.append(f"- {kw}")
            for node in nodes:
                lines.append(f"  -> {node['uri']}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return t("system.error_glossary").format(error=str(e))


async def generate_diagnostic_view(
    domain: str, days_stale: int = 30, max_children: int = 10, bloat_min_bytes: int | None = None
) -> str:
    """Generate a diagnostic report of the memory graph (system://diagnostic/<domain>)."""
    import config

    if bloat_min_bytes is None:
        bloat_min_bytes = config.get("bloat_min_bytes") or 2400

    graph = get_graph_service()

    try:
        ns = get_namespace()
        diagnostics = await graph.get_diagnostics(
            namespace=ns, days_stale=days_stale, max_children=max_children, domain=domain
        )
        bloated_nodes = await graph.get_longest_memories(
            namespace=ns, domain=domain, limit=10, min_bytes=bloat_min_bytes
        )

        stale_nodes = diagnostics.get("stale_nodes", [])
        crowded_nodes = diagnostics.get("crowded_nodes", [])
        orphaned_nodes = diagnostics.get("orphaned_nodes", [])
        duplicate_aliases = diagnostics.get("duplicate_aliases", [])

        if not stale_nodes and not crowded_nodes and not orphaned_nodes and not duplicate_aliases and not bloated_nodes:
            return "No issues found. Memory system is healthy."

        lines = [
            f"# Memory System Diagnostics: {domain}",
            f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]

        if stale_nodes:
            lines.extend([
                "## 1. Stale Memories",
                "Nodes not accessed within their priority threshold.",
                "Thresholds double for each priority level: Priority 0 (<3 days), Priority 1 (<7 days), Priority 2 (<14 days), Priority 3 (<28 days), etc.",
                ""
            ])

            sorted_stale = sorted(
                stale_nodes,
                key=lambda x: (x.get('priority') if x.get('priority') is not None else 999, -x.get('stale_days', 0))
            )

            for i, node in enumerate(sorted_stale, 1):
                last_acc = node.get("last_accessed_at")
                stale_days = node.get("stale_days")
                threshold = node.get("threshold_days", days_stale)

                if last_acc:
                    date_str = f"Last Accessed: {last_acc[:10]}"
                else:
                    date_str = "Never accessed (since tracking began)"

                lines.append(f"{i}. {node['uri']}")
                lines.append(f"   Priority: {node['priority']} | Stale for: ~{stale_days} days (Threshold: {threshold} days) | {date_str}")
            lines.append("")

        if crowded_nodes:
            lines.extend([
                "## 2. Crowded Parent Nodes",
                f"Nodes with more than {max_children} children.",
                ""
            ])
            for i, node in enumerate(crowded_nodes, 1):
                lines.append(f"{i}. {node['uri']} ({node['child_count']} children)")
            lines.append("")

        if orphaned_nodes or duplicate_aliases:
            lines.extend([
                "## 3. Anomaly Diagnostics",
                ""
            ])

            if orphaned_nodes:
                lines.extend([
                    "### 3.1 Orphaned Nodes",
                    "Nodes whose parent path no longer exists (broken path chain).",
                    "Use `read_memory` with the URI to inspect, then `add_alias` to re-parent or `delete_memory` to remove.",
                    ""
                ])
                for i, node in enumerate(orphaned_nodes, 1):
                    memory_id_str = f"Memory ID: {node['memory_id']}" if node['memory_id'] else "No active memory"
                    lines.append(f"{i}. {node['uri']}")
                    lines.append(f"   {memory_id_str} | Created: {node['created_at'][:10] if node['created_at'] else 'Unknown'}")
                    if node.get("snippet"):
                        lines.append(f"   Snippet: {node['snippet']}")
                    lines.append("")

            if duplicate_aliases:
                lines.extend([
                    "### 3.2 Duplicate Aliases under Same Parent",
                    "A single node has multiple alias paths under the same parent node.",
                    "Usually caused by accidentally inserting another alias when one already exists.",
                    "Use `delete_memory` on the redundant alias URI to remove the extra path.",
                    ""
                ])
                for i, item in enumerate(duplicate_aliases, 1):
                    parent_uri = f"{item['domain']}://{item['parent_path']}" if item['parent_path'] else f"{item['domain']}://"
                    paths_list = [f"{item['domain']}://{p}" for p in item['paths']]
                    memory_id_str = item['memory_id'] if item['memory_id'] else "No active memory"
                    lines.append(f"{i}. Memory ID: {memory_id_str}")
                    lines.append(f"   Parent: {parent_uri}")
                    lines.append(f"   Duplicate Paths ({item['count']}):")
                    for p in paths_list:
                        lines.append(f"     - {p}")
                    lines.append("")

        if bloated_nodes:
            lines.extend([
                "## 4. Bloated Memories",
                f"Top active memories exceeding {bloat_min_bytes / 1024:.1f} KB (ranked by UTF-8 byte size).",
                ""
            ])
            for i, node in enumerate(bloated_nodes, 1):
                kb_size = node["byte_size"] / 1024
                lines.append(f"{i}. {node['uri']}")
                lines.append(f"   Priority: {node['priority']} | {kb_size:.1f} KB | {node['char_count']} chars")
            lines.append("")

        return "\n".join(lines).strip()

    except Exception as e:
        return t("system.error_diagnostic").format(error=str(e))
