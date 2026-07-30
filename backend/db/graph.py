# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false, reportGeneralTypeIssues=false, reportOperatorIssue=false, reportReturnType=false

"""
Graph Service for Nocturne Memory System

Graph-based memory storage with:
- Node: a conceptual entity (UUID), version-independent
- Memory: a content version of a node
- Edge: parent→child relationship between nodes, carrying metadata
- Path: materialized URI cache (domain://path → edge)

All infrastructure (engine, session, migrations) lives in database.py.
This module contains only graph-domain business logic.
"""

import uuid as uuid_lib
import unicodedata
from collections import defaultdict
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from sqlalchemy import (
    select,
    update,
    delete,
    func,
    and_,
    or_,
    not_,
)
from sqlalchemy.ext.asyncio import AsyncSession
from .models import (
    ROOT_NODE_UUID,
    Node,
    Memory,
    Edge,
    Path,
    GlossaryKeyword,
    MemoryAccessLog,
    ChangeCollector,
    escape_like_literal,
    serialize_row,
    serialize_memory_ref,
)

if TYPE_CHECKING:
    from .database import DatabaseManager
    from .search import SearchIndexer


class GraphService:
    """
    Graph-domain service for memory operations.

    Owns all graph traversal, memory CRUD, path management, and
    orphan/deprecated memory handling.  Receives a DatabaseManager
    for session access and a SearchIndexer for post-mutation index
    refreshes.

    Core operations:
    - read: Get memory by path (Path → Edge → Memory via node_uuid)
    - create: New node + memory + edge + path
    - update: New memory version on same node; update edge metadata
    - add_path: Create alias (new Path, maybe new Edge)
    - remove_path: Delete paths; refuse if children would become unreachable
    """

    def __init__(self, db: "DatabaseManager", search: "SearchIndexer"):
        self.session = db.session
        self._optional_session = db._optional_session
        self._search = search

    # =========================================================================
    # Read Operations
    # =========================================================================

    async def log_access(self, node_uuid: str, namespace: str, context: Optional[str] = None) -> None:
        """
        Asynchronously log an access event and update the node's last_accessed_at timestamp.
        """
        from datetime import datetime
        async with self.session() as session:
            await session.execute(
                update(Node)
                .where(Node.uuid == node_uuid)
                .values(last_accessed_at=datetime.now())
            )
            
            log_entry = MemoryAccessLog(
                node_uuid=node_uuid,
                namespace=namespace,
                context=context
            )
            session.add(log_entry)

    async def get_memory_by_path(
        self, path: str, domain: str = "core", namespace: str = ""
    ) -> Optional[Dict[str, Any]]:
        """
        Get a memory by its path.

        Returns:
            Memory dict with id, node_uuid, content, priority, disclosure,
            created_at, domain, path — or None if not found.
        """
        async with self.session() as session:
            result = await session.execute(
                select(Memory, Edge, Path)
                .select_from(Path)
                .join(Edge, Path.edge_id == Edge.id)
                .join(
                    Memory,
                    and_(
                        Memory.node_uuid == Edge.child_uuid,
                        Memory.deprecated == False,
                    ),
                )
                .where(Path.namespace == namespace)
                .where(Path.domain == domain)
                .where(Path.path == path)
                .order_by(Memory.created_at.desc())
                .limit(1)
            )
            row = result.first()

            if not row:
                if path == "":
                    return {
                        "id": 0,
                        "node_uuid": ROOT_NODE_UUID,
                        "content": f"Root node for domain '{domain}'.",
                        "priority": 0,
                        "disclosure": None,
                        "deprecated": False,
                        "created_at": None,
                        "domain": domain,
                        "path": "",
                        "alias_count": 0,
                    }
                return None

            memory, edge, path_obj = row

            # Count total paths (aliases) for this node within namespace
            total_paths = await self._count_incoming_paths(session, edge.child_uuid, namespace=namespace)
            alias_count = max(0, total_paths - 1)

            return {
                "id": memory.id,
                "node_uuid": edge.child_uuid,
                "content": memory.content,
                "priority": edge.priority,
                "disclosure": edge.disclosure,
                "deprecated": memory.deprecated,
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "domain": path_obj.domain,
                "path": path_obj.path,
                "alias_count": alias_count,
            }

    async def get_paths_for_node(
        self,
        node_uuid: str,
        namespace: str = "",
        search_all_namespaces: bool = False,
        session: Optional[AsyncSession] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all paths pointing to a specific node.
        
        Args:
            node_uuid: The node UUID to query.
            namespace: If provided, filters to paths in this namespace. 
            search_all_namespaces: If True, returns paths across all namespaces.
            session: Optional existing AsyncSession to use.
                       
        Returns:
            A list of dicts, each containing 'domain', 'path', 'namespace', and 'uri'.
        """
        async def _query(db_session: AsyncSession):
            stmt = (
                select(Path.domain, Path.path, Path.namespace)
                .select_from(Path)
                .where(Path.node_uuid == node_uuid)
            )
            if not search_all_namespaces:
                stmt = stmt.where(Path.namespace == namespace)

            result = await db_session.execute(stmt)
            paths = []
            for r in result.all():
                domain, path_str, ns = r[0], r[1], r[2]
                uri = f"{domain}://{path_str}"
                paths.append({
                    "domain": domain,
                    "path": path_str,
                    "namespace": ns,
                    "uri": uri
                })
            return paths

        if session:
            return await _query(session)
        else:
            async with self.session() as new_session:
                return await _query(new_session)

    async def get_memory_by_node_uuid(
        self, 
        node_uuid: str, 
        namespace: str = "",
        search_all_namespaces: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the current active (non-deprecated) memory for a node.
        
        Args:
            node_uuid: The node UUID to query.
            namespace: If provided, filters the returned paths to this namespace. 
            search_all_namespaces: If True, returns paths across all namespaces.
        """
        async with self.session() as session:
            result = await session.execute(
                select(Memory)
                .where(Memory.node_uuid == node_uuid, Memory.deprecated == False)
                .order_by(Memory.created_at.desc())
                .limit(1)
            )
            memory = result.scalar_one_or_none()

            if not memory:
                return None

            paths_data = await self.get_paths_for_node(
                node_uuid, namespace=namespace, search_all_namespaces=search_all_namespaces, session=session
            )
            paths = [p["uri"] for p in paths_data]

            return {
                "id": memory.id,
                "node_uuid": node_uuid,
                "content": memory.content,
                "deprecated": memory.deprecated,
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "paths": paths,
            }

    async def get_children(
        self,
        node_uuid: str = ROOT_NODE_UUID,
        context_domain: Optional[str] = None,
        context_path: Optional[str] = None,
        namespace: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Get direct children of a node via the edges table.

        When *context_domain* / *context_path* are supplied the returned
        ``path`` for each child is chosen with affinity:
          1. Same domain AND path starts with ``context_path/``
          2. Same domain (any path)
          3. Any path at all
        This ensures the browse UI shows paths that match the caller's
        current navigation context rather than an arbitrary alias.
        """
        async with self.session() as session:
            stmt = (
                select(Edge, Memory)
                .join(
                    Memory,
                    and_(
                        Memory.node_uuid == Edge.child_uuid,
                        Memory.deprecated == False,
                    ),
                )
                .where(Edge.parent_uuid == node_uuid)
                .order_by(Edge.priority.asc(), Edge.name)
            )
            result = await session.execute(stmt)
            rows = result.all()

            prefix = f"{context_path}/" if context_path else None

            child_uuids = {edge.child_uuid for edge, _ in rows}
            edge_ids = {edge.id for edge, _ in rows}
            approx_children_count_map: Dict[str, int] = {}
            paths_by_edge_id: Dict[int, List[Path]] = defaultdict(list)
            if child_uuids:
                count_result = await session.execute(
                    select(Edge.parent_uuid, func.count(Edge.id.distinct()))
                    .join(Path, Path.edge_id == Edge.id)
                    .where(
                        Edge.parent_uuid.in_(child_uuids),
                        Path.namespace == namespace,
                    )
                    .group_by(Edge.parent_uuid)
                )
                approx_children_count_map = {
                    parent_uuid: count for parent_uuid, count in count_result.all()
                }

            if edge_ids:
                path_result = await session.execute(
                    select(Path).where(
                        Path.namespace == namespace,
                        Path.edge_id.in_(edge_ids),
                    )
                )
                for p in path_result.scalars().all():
                    paths_by_edge_id[p.edge_id].append(p)

            children = []
            seen = set()
            for edge, memory in rows:
                if edge.child_uuid in seen:
                    continue
                seen.add(edge.child_uuid)

                all_paths = paths_by_edge_id[edge.id]

                if node_uuid == ROOT_NODE_UUID and context_domain:
                    has_domain_path = any(p.domain == context_domain for p in all_paths)
                    if not has_domain_path:
                        continue

                path_obj = self._pick_best_path(all_paths, context_domain, prefix)

                # If this edge has no Path in the current namespace, the child
                # node belongs to a different namespace.  Skip it rather than
                # falling back to edge.name, which would expose cross-namespace
                # nodes to the caller.
                # Single-namespace deployments are unaffected: every edge always
                # has at least one Path in the (only) namespace.
                if path_obj is None:
                    continue

                approx_children_count = approx_children_count_map.get(
                    edge.child_uuid, 0
                )

                children.append(
                    {
                        "node_uuid": edge.child_uuid,
                        "edge_id": edge.id,
                        "name": edge.name,
                        "domain": path_obj.domain,
                        "path": path_obj.path,
                        "content_snippet": memory.content[:100] + "..."
                        if len(memory.content) > 100
                        else memory.content,
                        "priority": edge.priority,
                        "disclosure": edge.disclosure,
                        "approx_children_count": approx_children_count,
                    }
                )

            return children

    @staticmethod
    def _pick_best_path(
        paths: List[Path],
        context_domain: Optional[str],
        prefix: Optional[str],
    ) -> Optional[Path]:
        """Pick the most contextually relevant path from a list of aliases."""
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]

        # Tier 1: same domain + path is under the caller's current prefix
        if context_domain and prefix:
            for p in paths:
                if p.domain == context_domain and p.path.startswith(prefix):
                    return p

        # Tier 2: same domain, any path
        if context_domain:
            for p in paths:
                if p.domain == context_domain:
                    return p

        # Tier 3: whatever is available
        return paths[0]

    async def get_all_paths(self, domain: Optional[str] = None, namespace: str = "", search_all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """
        Get all paths with their node/edge info.
        """
        async with self.session() as session:
            stmt = (
                select(Path, Edge, Memory)
                .select_from(Path)
                .join(Edge, Path.edge_id == Edge.id)
                .join(
                    Memory,
                    and_(
                        Memory.node_uuid == Edge.child_uuid,
                        Memory.deprecated == False,
                    ),
                )
            )

            if not search_all_namespaces:
                stmt = stmt.where(Path.namespace == namespace)

            if domain is not None:
                stmt = stmt.where(Path.domain == domain)

            stmt = stmt.order_by(Path.domain, Path.path)
            result = await session.execute(stmt)

            paths = []
            seen = set()
            for path_obj, edge, memory in result.all():
                key = (path_obj.namespace, path_obj.domain, path_obj.path)
                if key in seen:
                    continue
                seen.add(key)
                paths.append(
                    {
                        "namespace": path_obj.namespace,
                        "domain": path_obj.domain,
                        "path": path_obj.path,
                        "uri": f"{path_obj.domain}://{path_obj.path}",
                        "name": path_obj.path.rsplit("/", 1)[-1],
                        "priority": edge.priority,
                        "memory_id": memory.id,
                        "node_uuid": path_obj.node_uuid,
                    }
                )

            return paths

    async def get_diagnostics(self, namespace: str = "", days_stale: int = 30, max_children: int = 10, priority_thresholds: Dict[int, int] = None, domain: str = None) -> Dict[str, Any]:
        """
        Get a diagnostic report of the memory graph for a given namespace.
        Returns stale nodes and crowded parent nodes.
        """
        from datetime import datetime, timedelta
            
        async with self.session() as session:
            # First find when we actually started tracking last_accessed_at
            tracking_stmt = select(func.min(Node.last_accessed_at)).where(Node.last_accessed_at != None)
            tracking_result = await session.execute(tracking_stmt)
            tracking_start_date = tracking_result.scalar_one_or_none()

            # If we don't have any access logs yet, default to today
            if not tracking_start_date:
                tracking_start_date = datetime.now()

            # We query nodes where their *effective* last access time is < cutoff_date.
            # For nodes with last_accessed_at != null, effective time is last_accessed_at
            # For nodes with last_accessed_at == null, effective time is MAX(created_at, tracking_start_date)
            # Since tracking_start_date is a constant, we can do this logic in Python or SQL.
            # We'll fetch all nodes and filter in Python to handle the MAX logic easily.
            all_nodes_stmt = (
                select(Path, Node, Edge, Memory)
                .select_from(Path)
                .join(Edge, Path.edge_id == Edge.id)
                .join(Node, Node.uuid == Edge.child_uuid)
                .join(Memory, and_(Memory.node_uuid == Node.uuid, Memory.deprecated == False))
                .where(Path.namespace == namespace)
            )
            if domain:
                all_nodes_stmt = all_nodes_stmt.where(Path.domain == domain)
            
            all_nodes_result = await session.execute(all_nodes_stmt)

            stale_nodes = {}
            for path_obj, node, edge, memory in all_nodes_result.all():
                if node.last_accessed_at:
                    effective_date = node.last_accessed_at
                else:
                    node_created = node.created_at or datetime.now()
                    effective_date = max(node_created, tracking_start_date)

                # Determine the threshold based on priority
                prio = edge.priority if edge.priority is not None else 999
                if priority_thresholds and prio in priority_thresholds:
                    threshold_days = priority_thresholds[prio]
                elif prio != 999:
                    # Automatically calculate: priority 0 -> 3, priority 1 -> 7, priority 2 -> 14, priority 3 -> 28, etc.
                    # Cap priority between 0 and 20 to prevent OverflowError and negative zero-threshold bugs
                    safe_prio = max(0, min(prio, 20))
                    threshold_days = 3 if safe_prio == 0 else int(3.5 * (2 ** safe_prio))
                    # Cap at 36500 days (~100 years) to keep UI and logic sane
                    threshold_days = min(threshold_days, 36500)
                else:
                    threshold_days = days_stale
                    
                cutoff_date = datetime.now() - timedelta(days=threshold_days)

                if effective_date < cutoff_date:
                    key = node.uuid
                    
                    # Check if this node is already in the dict. If it is, only overwrite 
                    # if the current path has a higher priority (smaller number).
                    existing_prio = 999
                    if key in stale_nodes:
                        existing_val = stale_nodes[key].get("priority")
                        existing_prio = existing_val if existing_val is not None else 999
                        
                    if key not in stale_nodes or prio < existing_prio:
                        # Calculate exactly how many days stale this is
                        stale_days_calc = round((datetime.now() - effective_date).total_seconds() / 86400.0, 1)
                        
                        stale_nodes[key] = {
                            "uuid": node.uuid,
                            "uri": f"{path_obj.domain}://{path_obj.path}",
                            "created_at": node.created_at.isoformat() if node.created_at else None,
                            "last_accessed_at": node.last_accessed_at.isoformat() if node.last_accessed_at else None,
                            "effective_date": effective_date.isoformat(),
                            "stale_days": stale_days_calc,
                            "threshold_days": threshold_days,
                            "priority": edge.priority,
                            "title": path_obj.path.rsplit("/", 1)[-1],
                            "memory_id": memory.id
                        }

            # 2. Crowded Nodes (parents with > max_children)
            crowded_stmt = (
                select(Edge.parent_uuid, func.count(Edge.child_uuid.distinct()).label("child_count"))
                .join(Path, Path.edge_id == Edge.id)
                .where(Path.namespace == namespace)
            )
            if domain:
                crowded_stmt = crowded_stmt.where(Path.domain == domain)
            
            crowded_stmt = (
                crowded_stmt.group_by(Edge.parent_uuid)
                .having(func.count(Edge.child_uuid.distinct()) > max_children)
            )
            crowded_result = await session.execute(crowded_stmt)

            crowded_parents = {}
            for parent_uuid, count in crowded_result.all():
                if parent_uuid == ROOT_NODE_UUID:
                    # special case for root
                    crowded_parents[parent_uuid] = {
                        "uuid": parent_uuid,
                        "uri": f"{domain if domain else 'core'}://",
                        "title": "(root)",
                        "child_count": count
                    }
                else:
                    path_stmt = (
                        select(Path)
                        .where(Path.node_uuid == parent_uuid, Path.namespace == namespace)
                    )
                    if domain:
                        path_stmt = path_stmt.where(Path.domain == domain)
                        
                    path_stmt = path_stmt.limit(1)
                    
                    path_res = await session.execute(path_stmt)
                    path_obj = path_res.scalar_one_or_none()
                    if path_obj:
                        crowded_parents[parent_uuid] = {
                            "uuid": parent_uuid,
                            "uri": f"{path_obj.domain}://{path_obj.path}",
                            "title": path_obj.path.rsplit("/", 1)[-1],
                            "child_count": count
                        }

            # 3. Orphaned Paths (paths where the parent path does not exist)
            all_paths_stmt = (
                select(Path.domain, Path.path, Node, Memory)
                .join(Node, Node.uuid == Path.node_uuid)
                .outerjoin(Memory, and_(Memory.node_uuid == Node.uuid, Memory.deprecated == False))
                .where(Path.namespace == namespace)
            )
            if domain:
                all_paths_stmt = all_paths_stmt.where(Path.domain == domain)
            
            all_paths_result = await session.execute(all_paths_stmt)
            paths_dict = {}
            for domain, path_str, node, memory in all_paths_result.all():
                paths_dict[(domain, path_str)] = (node, memory)

            parent_node_groups: Dict[tuple, List[str]] = defaultdict(list)

            orphaned_nodes = []
            for (domain, path_str), (node, memory) in paths_dict.items():
                if "/" in path_str:
                    parent_path_str = path_str.rsplit("/", 1)[0]
                    parent_node_groups[(domain, parent_path_str, node.uuid)].append(path_str)
                    if (domain, parent_path_str) not in paths_dict:
                        snippet = ""
                        memory_id = None
                        if memory:
                            memory_id = memory.id
                            if memory.content:
                                snippet = memory.content[:50].replace('\n', ' ') + "..." if len(memory.content) > 50 else memory.content.replace('\n', ' ')

                        orphaned_nodes.append({
                            "uuid": node.uuid,
                            "uri": f"{domain}://{path_str}",
                            "created_at": node.created_at.isoformat() if node.created_at else None,
                            "last_accessed_at": node.last_accessed_at.isoformat() if node.last_accessed_at else None,
                            "memory_id": memory_id,
                            "snippet": snippet
                        })
                else:
                    parent_node_groups[(domain, "", node.uuid)].append(path_str)

            duplicate_aliases = []
            for (domain, parent_path_str, node_uuid), paths_list in parent_node_groups.items():
                if len(paths_list) > 1:
                    _, memory = paths_dict[(domain, paths_list[0])]
                    duplicate_aliases.append({
                        "uuid": node_uuid,
                        "memory_id": memory.id if memory else None,
                        "domain": domain,
                        "parent_path": parent_path_str,
                        "paths": paths_list,
                        "count": len(paths_list)
                    })

            return {
                "stale_nodes": sorted(list(stale_nodes.values()), key=lambda x: x.get("last_accessed_at") or x.get("created_at") or ""),
                "crowded_nodes": sorted(list(crowded_parents.values()), key=lambda x: x["child_count"], reverse=True),
                "orphaned_nodes": sorted(orphaned_nodes, key=lambda x: x.get("created_at") or ""),
                "duplicate_aliases": sorted(duplicate_aliases, key=lambda x: x["count"], reverse=True)
            }

    async def get_longest_memories(
        self,
        namespace: str = "",
        domain: Optional[str] = None,
        limit: int = 10,
        min_bytes: int = 2048,
    ) -> List[Dict[str, Any]]:
        """Return the top *limit* active memories by content size.

        Uses SQL to calculate exact byte length across dialects (SQLite/PG)
        and limits the result set in the database to minimize memory overhead.
        """
        from sqlalchemy import cast, LargeBinary
        async with self.session() as session:
            # 1. Use window function to pick exactly one representative alias (highest priority) per memory
            rn = func.row_number().over(
                partition_by=Memory.id,
                order_by=Edge.priority.asc()
            ).label("rn")

            subq = (
                select(
                    Path.domain,
                    Path.path,
                    Memory.id.label("memory_id"),
                    Memory.content,
                    Edge.priority,
                    rn
                )
                .select_from(Path)
                .join(Edge, Path.edge_id == Edge.id)
                .join(Node, Node.uuid == Edge.child_uuid)
                .join(
                    Memory,
                    and_(
                        Memory.node_uuid == Node.uuid,
                        Memory.deprecated == False,
                    ),
                )
                .where(Path.namespace == namespace)
            )
            if domain:
                subq = subq.where(Path.domain == domain)
            
            subq = subq.subquery()

            # 2. Calculate byte length in SQL and filter/sort/limit directly
            dialect_name = session.bind.dialect.name
            if dialect_name == "postgresql":
                byte_length_expr = func.octet_length(subq.c.content)
            else:
                byte_length_expr = func.length(cast(subq.c.content, LargeBinary))

            stmt = (
                select(
                    subq.c.domain,
                    subq.c.path,
                    subq.c.memory_id,
                    subq.c.content,
                    subq.c.priority,
                    byte_length_expr.label("byte_length"),
                )
                .where(subq.c.rn == 1)
                .where(byte_length_expr >= min_bytes)
                .order_by(byte_length_expr.desc())
                .limit(limit)
            )

            result = await session.execute(stmt)

            entries: List[Dict[str, Any]] = []
            for domain_val, path_str, mem_id, content, priority, b_len in result.all():
                content = content or ""
                entries.append(
                    {
                        "uri": f"{domain_val}://{path_str}",
                        "memory_id": mem_id,
                        "char_count": len(content),
                        "byte_size": b_len,
                        "priority": priority,
                    }
                )

            return entries

    async def get_random_memory(self, namespace: str = "", domain: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Pick a weighted-random memory node. Weight = staleness * priority_multiplier.
        Older and higher-priority memories are more likely to surface.
        Returns dict with node_uuid, uri, priority, last_accessed_at, or None if empty.
        """
        import random
        from datetime import datetime

        async with self.session() as session:
            # 1. 数据库层面直接过滤和聚合，只拉取计算权重所需的标量字段，大幅降低内存和传输开销
            stmt = (
                select(
                    Node.uuid,
                    Node.last_accessed_at,
                    Node.created_at,
                    func.min(func.coalesce(Edge.priority, 2)).label("best_priority")
                )
                .select_from(Path)
                .join(Edge, Path.edge_id == Edge.id)
                .join(Node, Node.uuid == Edge.child_uuid)
                .where(Path.namespace == namespace)
                .where(Node.uuid != ROOT_NODE_UUID)
                .group_by(Node.uuid, Node.last_accessed_at, Node.created_at)
            )
            
            if domain:
                stmt = stmt.where(Path.domain == domain)
            else:
                stmt = stmt.where(Path.domain != "system")
            
            result = await session.execute(stmt)
            rows = result.all()

            if not rows:
                return None

            # 2. 在 Python 中计算权重并进行加权随机选择
            now = datetime.now()
            weights = []
            for row in rows:
                last = row.last_accessed_at or row.created_at or now
                staleness_days = max((now - last).total_seconds() / 86400.0, 0.5)
                # 优先级 0 -> 1x, 1 -> 0.5x, 2 -> 0.25x, 等等
                priority = row.best_priority if row.best_priority is not None else 2
                mult = max(0.5 ** max(0, priority), 1e-12)
                weights.append(staleness_days * mult)

            chosen = random.choices(rows, weights=weights, k=1)[0]

            # 3. 针对选中的 node，查出它在当前 namespace 下的一个随机 URI
            uri_stmt = (
                select(Path.domain, Path.path)
                .join(Edge, Path.edge_id == Edge.id)
                .where(Edge.child_uuid == chosen.uuid)
                .where(Path.namespace == namespace)
            )
            
            if domain:
                uri_stmt = uri_stmt.where(Path.domain == domain)
            else:
                uri_stmt = uri_stmt.where(Path.domain != "system")
                
            uri_result = await session.execute(uri_stmt)
            uri_rows = uri_result.all()
            
            if not uri_rows:
                return None
                
            chosen_domain, chosen_path = random.choice(uri_rows)
            chosen_uri = f"{chosen_domain}://{chosen_path}"

            return {
                "node_uuid": chosen.uuid,
                "uri": chosen_uri,
                "priority": chosen.best_priority,
                "last_accessed_at": chosen.last_accessed_at.isoformat() if chosen.last_accessed_at else None,
            }

    # =========================================================================
    # Layer 0: Row-Level Primitives
    # Single-row / single-table. Takes session, never opens own transaction.
    # =========================================================================

    async def _ensure_node(self, session: AsyncSession, node_uuid: str) -> Node:
        """Create a node if it doesn't exist; return it either way."""
        result = await session.execute(select(Node).where(Node.uuid == node_uuid))
        node = result.scalar_one_or_none()
        if node:
            return node
        node = Node(uuid=node_uuid)
        session.add(node)
        await session.flush()
        return node

    async def _insert_memory(
        self,
        session: AsyncSession,
        node_uuid: str,
        content: str,
        *,
        deprecated: bool = False,
    ) -> Memory:
        """Insert a new memory row and flush to obtain its ID."""
        # Canonicalize content to NFC at the write boundary so all stored
        # strings are consistently normalized.  This is an intentional
        # whole-content policy: the system has no byte-level version diff or
        # rollback, so converting historical NFD data to NFC on write is
        # the desired migration path, not a side-effect.
        content = unicodedata.normalize("NFC", content)
        
        memory = Memory(
            content=content,
            node_uuid=node_uuid,
            deprecated=deprecated,
        )
        session.add(memory)
        await session.flush()
        return memory

    async def _get_or_create_edge(
        self,
        session: AsyncSession,
        parent_uuid: str,
        child_uuid: str,
        name: str,
        priority: int = 0,
        disclosure: Optional[str] = None,
    ) -> tuple:
        """Get an existing edge or create a new one.

        Returns (edge, created: bool).
        """
        result = await session.execute(
            select(Edge).where(
                Edge.parent_uuid == parent_uuid,
                Edge.child_uuid == child_uuid,
            )
        )
        edge = result.scalar_one_or_none()
        if edge:
            return edge, False

        edge = Edge(
            parent_uuid=parent_uuid,
            child_uuid=child_uuid,
            name=name,
            priority=priority,
            disclosure=disclosure,
        )
        session.add(edge)
        await session.flush()
        return edge, True

    async def _insert_path(
        self, session: AsyncSession, domain: str, path: str, edge_id: int, node_uuid: str, namespace: str = ""
    ) -> Path:
        """Insert a new path row with the given namespace."""
        path_obj = Path(namespace=namespace, domain=domain, path=path, edge_id=edge_id, node_uuid=node_uuid)
        session.add(path_obj)
        return path_obj

    async def _resolve_path(
        self, session: AsyncSession, path: str, domain: str = "core", namespace: str = ""
    ) -> Optional[tuple[Path, Edge, str]]:
        """Resolve domain+path to (Path, Edge, node_uuid). Returns None if not found."""
        result = await session.execute(
            select(Path, Edge)
            .join(Edge, Path.edge_id == Edge.id)
            .where(Path.namespace == namespace, Path.domain == domain, Path.path == path)
        )
        row = result.first()
        if not row:
            return None
        path_obj, edge = row
        return path_obj, edge, path_obj.node_uuid

    async def _count_paths_for_edge(self, session: AsyncSession, edge_id: int) -> int:
        """Count how many path rows reference a given edge."""
        result = await session.execute(
            select(func.count()).select_from(Path).where(Path.edge_id == edge_id)
        )
        return result.scalar()

    async def _count_incoming_paths(
        self,
        session: AsyncSession,
        node_uuid: str,
        *,
        namespace: str = "",
        search_all_namespaces: bool = False,
        exclude_domain: Optional[str] = None,
        exclude_path_prefix: Optional[str] = None,
        exclude_namespace: Optional[str] = None,
    ) -> int:
        """Count paths pointing TO this node (via Path.node_uuid).

        When search_all_namespaces is True, counts across ALL namespaces (GC callers use this).
        Otherwise only counts paths in the specified namespace.
        """
        stmt = (
            select(func.count())
            .select_from(Path)
            .where(Path.node_uuid == node_uuid)
        )

        if not search_all_namespaces:
            stmt = stmt.where(Path.namespace == namespace)

        if exclude_domain and exclude_path_prefix:
            safe_prefix = escape_like_literal(exclude_path_prefix)
            exclude_cond = and_(
                Path.domain == exclude_domain,
                or_(
                    Path.path == exclude_path_prefix,
                    Path.path.like(f"{safe_prefix}/%", escape="\\"),
                )
            )
            if exclude_namespace is not None:
                exclude_cond = and_(exclude_cond, Path.namespace == exclude_namespace)
            
            stmt = stmt.where(not_(exclude_cond))

        result = await session.execute(stmt)
        return result.scalar()

    async def _count_memories_for_node(
        self, session: AsyncSession, node_uuid: str
    ) -> int:
        """Count all memory rows (including deprecated) for a node."""
        result = await session.execute(
            select(func.count())
            .select_from(Memory)
            .where(Memory.node_uuid == node_uuid)
        )
        return result.scalar()

    async def _get_next_child_number(
        self, session: AsyncSession, parent_uuid: str
    ) -> int:
        """Get the next numeric name for auto-naming under a parent node."""
        result = await session.execute(
            select(Edge.name).where(Edge.parent_uuid == parent_uuid)
        )
        max_num = 0
        for (name,) in result.all():
            try:
                num = int(name)
                max_num = max(max_num, num)
            except ValueError:
                pass
        return max_num + 1

    async def _would_create_cycle(
        self,
        session: AsyncSession,
        parent_uuid: str,
        child_uuid: str,
    ) -> bool:
        """Check if adding edge parent_uuid->child_uuid would create a cycle.

        Returns True if child_uuid can already reach parent_uuid by
        following existing edges downward (parent->child direction),
        or if the two UUIDs are identical (self-loop).
        """
        if parent_uuid == ROOT_NODE_UUID:
            return False
        if parent_uuid == child_uuid:
            return True

        visited = {child_uuid}
        queue = [child_uuid]
        while queue:
            current = queue.pop(0)
            result = await session.execute(
                select(Edge.child_uuid).where(Edge.parent_uuid == current)
            )
            for (next_uuid,) in result.all():
                if next_uuid == parent_uuid:
                    return True
                if next_uuid not in visited:
                    visited.add(next_uuid)
                    queue.append(next_uuid)
        return False

    # =========================================================================
    # Layer 1: Table-Scoped Operations
    # Multi-row ops within a single table or closely related tables.
    # =========================================================================

    async def _deprecate_node_memories(
        self,
        session: AsyncSession,
        node_uuid: str,
        *,
        successor_id: Optional[int] = None,
    ) -> List[int]:
        """Mark active memories for a node as deprecated.

        Args:
            successor_id: Value for migrated_to (None = orphan deprecation).
                When provided, that row is excluded from deprecation.

        Returns list of deprecated memory IDs.
        """
        conditions = [
            Memory.node_uuid == node_uuid,
            Memory.deprecated == False,
        ]
        if successor_id is not None:
            conditions.append(Memory.id != successor_id)

        result = await session.execute(select(Memory.id).where(and_(*conditions)))
        ids = [row[0] for row in result.all()]

        if ids:
            await session.execute(
                update(Memory)
                .where(Memory.id.in_(ids))
                .values(deprecated=True, migrated_to=successor_id)
            )
        return ids

    async def _safely_delete_memory(
        self,
        session: AsyncSession,
        memory_id: int,
        *,
        require_deprecated: bool = False,
    ) -> Dict[str, Any]:
        """Safely delete one memory row with chain repair.

        Steps:
        1) Validate target existence (and deprecated-only requirement if requested).
        2) Repair predecessor pointers: migrated_to == memory_id -> successor_id.
        3) Delete the target memory row.
        """
        target_result = await session.execute(
            select(Memory).where(Memory.id == memory_id)
        )
        target = target_result.scalar_one_or_none()
        if not target:
            raise ValueError(f"Memory ID {memory_id} not found")

        if require_deprecated and not target.deprecated:
            raise PermissionError(
                f"Memory {memory_id} is active (deprecated=False). Deletion aborted."
            )

        successor_id = target.migrated_to
        await session.execute(
            update(Memory)
            .where(Memory.migrated_to == memory_id)
            .values(migrated_to=successor_id)
        )

        result = await session.execute(delete(Memory).where(Memory.id == memory_id))
        if result.rowcount == 0:
            raise ValueError(f"Memory ID {memory_id} not found")

        return {
            "deleted_memory_id": memory_id,
            "chain_repaired_to": successor_id,
            "node_uuid": target.node_uuid,
            "deleted_memory_before": serialize_memory_ref(target),
        }

    async def _get_subtree_path_rows(
        self,
        session: AsyncSession,
        domain: str,
        base_path: str,
        namespace: str = "",
    ) -> List[Dict[str, Any]]:
        """Return serialized path rows for base_path and all descendants."""
        safe = escape_like_literal(base_path)
        result = await session.execute(
            select(Path).where(
                Path.namespace == namespace,
                Path.domain == domain,
                or_(
                    Path.path == base_path,
                    Path.path.like(f"{safe}/%", escape="\\"),
                ),
            )
        )
        return [serialize_row(p) for p in result.scalars().all()]

    async def _cascade_create_paths(
        self,
        session: AsyncSession,
        node_uuid: str,
        domain: str,
        base_path: str,
        _visited: Optional[set] = None,
        namespace: str = "",
    ):
        """Recursively create path entries for all descendants of a node."""
        if _visited is None:
            _visited = set()
        if node_uuid in _visited:
            return
        _visited.add(node_uuid)
        try:
            result = await session.execute(
                select(Edge).where(Edge.parent_uuid == node_uuid)
            )
            child_edges = result.scalars().all()

            for child_edge in child_edges:
                child_path = f"{base_path}/{child_edge.name}"

                existing = await session.execute(
                    select(Path)
                    .where(Path.namespace == namespace)
                    .where(Path.domain == domain)
                    .where(Path.path == child_path)
                )
                if not existing.scalar_one_or_none():
                    session.add(
                        Path(namespace=namespace, domain=domain, path=child_path, edge_id=child_edge.id, node_uuid=child_edge.child_uuid)
                    )

                await self._cascade_create_paths(
                    session, child_edge.child_uuid, domain, child_path, _visited, namespace
                )
        finally:
            _visited.remove(node_uuid)

    # =========================================================================
    # Layer 2: Cross-Table Cascades
    # Deterministic multi-table operations. No condition checks.
    # =========================================================================

    async def _delete_subtree_paths(
        self,
        session: AsyncSession,
        domain: str,
        path: str,
        *,
        namespace: str = "",
        collector: Optional[ChangeCollector] = None,
    ) -> None:
        """Delete a path and all its descendant paths in the given domain/namespace."""
        safe = escape_like_literal(path)
        result = await session.execute(
            select(Path)
            .where(Path.namespace == namespace)
            .where(Path.domain == domain)
            .where(
                or_(
                    Path.path == path,
                    Path.path.like(f"{safe}/%", escape="\\"),
                )
            )
        )
        paths = result.scalars().all()

        for p in paths:
            serialized = serialize_row(p)
            if collector:
                collector.record("paths", serialized)
            await session.delete(p)

    async def _cascade_delete_edge(
        self,
        session: AsyncSession,
        edge: Edge,
        *,
        collector: Optional[ChangeCollector] = None,
    ) -> None:
        """Delete an edge, all its path references, and descendant paths."""
        paths_result = await session.execute(
            select(Path).where(Path.edge_id == edge.id)
        )
        edge_paths = paths_result.scalars().all()

        for p in edge_paths:
            await self._delete_subtree_paths(
                session,
                p.domain,
                p.path,
                namespace=p.namespace,
                collector=collector,
            )

        if collector:
            collector.record("edges", serialize_row(edge))
        await session.delete(edge)

    async def cascade_delete_node(
        self, session: AsyncSession, node_uuid: str
    ) -> Optional[Dict[str, list]]:
        """Hard-delete a node, all its memories, edges, and paths."""
        if node_uuid == ROOT_NODE_UUID:
            return None

        collector = ChangeCollector()

        edges_result = await session.execute(
            select(Edge).where(
                or_(Edge.parent_uuid == node_uuid, Edge.child_uuid == node_uuid)
            )
        )
        for edge in edges_result.scalars().all():
            await self._cascade_delete_edge(
                session,
                edge,
                collector=collector,
            )

        mem_result = await session.execute(
            select(Memory).where(Memory.node_uuid == node_uuid)
        )
        for mem in mem_result.scalars().all():
            collector.record("memories", serialize_row(mem))

        kw_result = await session.execute(
            select(GlossaryKeyword).where(GlossaryKeyword.node_uuid == node_uuid)
        )
        for kw in kw_result.scalars().all():
            collector.record("glossary_keywords", serialize_row(kw))

        await session.execute(
            delete(GlossaryKeyword).where(GlossaryKeyword.node_uuid == node_uuid)
        )

        await session.execute(
            delete(Memory).where(Memory.node_uuid == node_uuid)
        )
        node_row = await session.execute(select(Node).where(Node.uuid == node_uuid))
        node = node_row.scalar_one_or_none()
        if node:
            collector.record("nodes", serialize_row(node))
        await session.execute(delete(Node).where(Node.uuid == node_uuid))

        return collector.to_dict()

    async def _create_edge_with_paths(
        self,
        session: AsyncSession,
        parent_uuid: str,
        child_uuid: str,
        name: str,
        domain: str,
        path: str,
        priority: int = 0,
        disclosure: Optional[str] = None,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """Create (or get) an edge, its path entry, and cascade sub-paths."""
        edge, edge_created = await self._get_or_create_edge(
            session, parent_uuid, child_uuid, name, priority, disclosure
        )
        path_obj = await self._insert_path(session, domain, path, edge.id, child_uuid, namespace)
        await self._cascade_create_paths(session, child_uuid, domain, path, _visited=None, namespace=namespace)
        return {
            "edge": edge,
            "edge_id": edge.id,
            "edge_created": edge_created,
            "path": path_obj,
        }

    # =========================================================================
    # Layer 3: GC / Conditional Logic
    # Check conditions, then delegate to Layer 2/1/0.
    # =========================================================================

    async def _gc_edge_if_pathless(
        self,
        session: AsyncSession,
        edge: Edge,
        *,
        collector: Optional[ChangeCollector] = None,
    ) -> Optional[Dict[str, Any]]:
        """Delete an edge only if it has no remaining path references."""
        if await self._count_paths_for_edge(session, edge.id) > 0:
            return None
        if collector:
            collector.record("edges", serialize_row(edge))
        info = {
            "edge_id": edge.id,
            "parent_uuid": edge.parent_uuid,
            "child_uuid": edge.child_uuid,
            "name": edge.name,
            "priority": edge.priority,
            "disclosure": edge.disclosure,
        }
        await session.delete(edge)
        return info

    async def _gc_node_soft(
        self,
        session: AsyncSession,
        node_uuid: str,
        *,
        collector: Optional[ChangeCollector] = None,
    ) -> None:
        """Soft GC: if a node has no incoming paths, deprecate its memories
        and cascade-delete all edges/paths around it.

        Memories are kept (marked deprecated) so they can be recovered.
        """
        if await self._count_incoming_paths(session, node_uuid, search_all_namespaces=True) > 0:
            return

        # Incoming edges are pathless by definition; delete them.
        incoming = await session.execute(
            select(Edge).where(Edge.child_uuid == node_uuid)
        )
        for edge in incoming.scalars().all():
            await self._gc_edge_if_pathless(session, edge, collector=collector)

        outgoing = await session.execute(
            select(Edge).where(Edge.parent_uuid == node_uuid)
        )
        for edge in outgoing.scalars().all():
            await self._cascade_delete_edge(
                session,
                edge,
                collector=collector,
            )

        # Record pre-deprecation state, then deprecate.
        if collector:
            active_mems = await session.execute(
                select(Memory).where(
                    Memory.node_uuid == node_uuid,
                    Memory.deprecated == False,
                )
            )
            for mem in active_mems.scalars().all():
                collector.record("memories", serialize_row(mem))

            kw_result = await session.execute(
                select(GlossaryKeyword).where(GlossaryKeyword.node_uuid == node_uuid)
            )
            for kw in kw_result.scalars().all():
                collector.record("glossary_keywords", serialize_row(kw))

        await session.execute(
            delete(GlossaryKeyword).where(GlossaryKeyword.node_uuid == node_uuid)
        )

        await self._deprecate_node_memories(session, node_uuid)

    async def _gc_node_if_memoryless(
        self, session: AsyncSession, node_uuid: str
    ) -> Optional[Dict[str, list]]:
        """Hard GC: if a node has zero memory rows, cascade-delete everything."""
        if await self._count_memories_for_node(session, node_uuid) > 0:
            return None
        return await self.cascade_delete_node(session, node_uuid)

    # =========================================================================
    # Public Write API
    # =========================================================================

    async def create_memory(
        self,
        parent_path: str,
        content: str,
        priority: int,
        title: Optional[str] = None,
        disclosure: Optional[str] = None,
        domain: str = "core",
        namespace: str = "",
    ) -> Dict[str, Any]:
        """
        Create a new memory under a parent path.

        Creates: Node -> Memory -> Edge (parent->child) -> Path.
        """
        async with self.session() as session:
            if not parent_path:
                parent_uuid = ROOT_NODE_UUID
            else:
                parent = await self._resolve_path(session, parent_path, domain, namespace=namespace)
                if not parent:
                    raise ValueError(
                        f"Parent '{domain}://{parent_path}' does not exist. "
                        f"Create the parent first, or use '{domain}://' as root."
                    )
                _, _, parent_uuid = parent

            if title:
                final_path = f"{parent_path}/{title}" if parent_path else title
            else:
                next_num = await self._get_next_child_number(session, parent_uuid)
                final_path = (
                    f"{parent_path}/{next_num}" if parent_path else str(next_num)
                )

            existing = await session.execute(
                select(Path).where(
                    Path.namespace == namespace,
                    Path.domain == domain,
                    Path.path == final_path,
                )
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"Path '{domain}://{final_path}' already exists")

            new_uuid = str(uuid_lib.uuid4())
            node = await self._ensure_node(session, new_uuid)
            memory = await self._insert_memory(session, new_uuid, content)

            edge_name = title if title else final_path.rsplit("/", 1)[-1]
            created = await self._create_edge_with_paths(
                session,
                parent_uuid,
                new_uuid,
                edge_name,
                domain,
                final_path,
                priority,
                disclosure,
                namespace,
            )

            await self._search.refresh_search_documents_for_node(new_uuid, session=session, namespace=namespace)

            return {
                "id": memory.id,
                "node_uuid": new_uuid,
                "domain": domain,
                "path": final_path,
                "uri": f"{domain}://{final_path}",
                "priority": priority,
                "rows_after": {
                    "nodes": [serialize_row(node)],
                    "memories": [serialize_memory_ref(memory)],
                    "edges": [serialize_row(created["edge"])],
                    "paths": [serialize_row(created["path"])],
                },
            }

    async def update_memory(
        self,
        path: str,
        content: Optional[str] = None,
        priority: Optional[int] = None,
        disclosure: Optional[str] = None,
        domain: str = "core",
        namespace: str = "",
    ) -> Dict[str, Any]:
        """
        Update a memory.

        Content change -> new Memory row with the same node_uuid.
        Metadata change -> update the Edge directly.
        """
        if content is None and priority is None and disclosure is None:
            raise ValueError(
                f"No update fields provided for '{domain}://{path}'. "
                "At least one of content, priority, or disclosure must be set."
            )

        async with self.session() as session:
            result = await session.execute(
                select(Memory, Edge, Path)
                .select_from(Path)
                .join(Edge, Path.edge_id == Edge.id)
                .join(
                    Memory,
                    and_(
                        Memory.node_uuid == Edge.child_uuid,
                        Memory.deprecated == False,
                    ),
                )
                .where(Path.namespace == namespace)
                .where(Path.domain == domain, Path.path == path)
                .order_by(Memory.created_at.desc())
                .limit(1)
            )
            row = result.first()

            if not row:
                raise ValueError(
                    f"Path '{domain}://{path}' not found or memory is deprecated"
                )

            old_memory, edge, path_obj = row
            old_id = old_memory.id
            node_uuid = edge.child_uuid

            rows_before: Dict[str, list] = {}
            rows_after: Dict[str, list] = {}

            edge_before = serialize_row(edge)

            if priority is not None:
                edge.priority = priority
                session.add(edge)
            if disclosure is not None:
                edge.disclosure = disclosure
                session.add(edge)

            edge_after = serialize_row(edge)
            if edge_before != edge_after:
                rows_before["edges"] = [edge_before]
                rows_after["edges"] = [edge_after]

            new_memory_id = old_id

            if content is not None:
                rows_before["memories"] = [serialize_memory_ref(old_memory)]

                new_memory = await self._insert_memory(
                    session, node_uuid, content, deprecated=True
                )
                new_memory_id = new_memory.id
                await self._deprecate_node_memories(
                    session,
                    node_uuid,
                    successor_id=new_memory_id,
                )
                await session.execute(
                    update(Memory)
                    .where(Memory.id == new_memory_id)
                    .values(deprecated=False, migrated_to=None)
                )

                await session.flush()
                updated = await session.execute(
                    select(Memory).where(Memory.id.in_([old_id, new_memory_id]))
                )
                rows_after["memories"] = [
                    serialize_memory_ref(m) for m in updated.scalars().all()
                ]

            if content is None:
                session.add(path_obj)

            await self._search.refresh_search_documents_for_node(node_uuid, session=session, namespace=namespace, refresh_all_namespaces=True)

            return {
                "domain": domain,
                "path": path,
                "uri": f"{domain}://{path}",
                "old_memory_id": old_id,
                "new_memory_id": new_memory_id,
                "node_uuid": node_uuid,
                "rows_before": rows_before,
                "rows_after": rows_after,
            }

    async def rollback_to_memory(
        self, target_memory_id: int, session: Optional[AsyncSession] = None, namespace: str = ""
    ) -> Dict[str, Any]:
        """Inverse of _deprecate_node_memories: restore a deprecated memory
        as the active version, deprecating whatever is currently active."""
        async with self._optional_session(session) as session:
            target_row = await session.execute(
                select(Memory).where(Memory.id == target_memory_id)
            )
            target_memory = target_row.scalar_one_or_none()
            if not target_memory:
                raise ValueError(f"Memory ID {target_memory_id} not found")

            if not target_memory.deprecated:
                return {
                    "restored_memory_id": target_memory_id,
                    "was_already_active": True,
                }

            await self._deprecate_node_memories(
                session,
                target_memory.node_uuid,
                successor_id=target_memory_id,
            )

            await session.execute(
                update(Memory)
                .where(Memory.id == target_memory_id)
                .values(deprecated=False, migrated_to=None)
            )

            await self._search.refresh_search_documents_for_node(
                target_memory.node_uuid, session=session, namespace=namespace, refresh_all_namespaces=True
            )

            return {
                "restored_memory_id": target_memory_id,
                "node_uuid": target_memory.node_uuid,
            }

    async def add_path(
        self,
        new_path: str,
        target_path: str,
        new_domain: str = "core",
        target_domain: str = "core",
        priority: int = 0,
        disclosure: Optional[str] = None,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """
        Create an alias path pointing to the same node as target_path.

        Also cascades: automatically creates sub-paths for all descendants.
        """
        if new_path == "":
            raise ValueError("Cannot create an alias at the root path.")

        async with self.session() as session:
            target = await self._resolve_path(session, target_path, target_domain, namespace=namespace)
            if not target:
                raise ValueError(
                    f"Target path '{target_domain}://{target_path}' not found"
                )
            _, target_edge, target_node_uuid = target

            # Inherit disclosure from the target if not explicitly provided
            if disclosure is None and target_edge.disclosure:
                disclosure = target_edge.disclosure

            if "/" in new_path:
                parent_path = new_path.rsplit("/", 1)[0]
                parent = await self._resolve_path(session, parent_path, new_domain, namespace=namespace)
                if not parent:
                    raise ValueError(
                        f"Parent '{new_domain}://{parent_path}' does not exist. "
                        f"Create the parent first, or use a shallower alias path."
                    )
                _, _, parent_uuid = parent
            else:
                parent_uuid = ROOT_NODE_UUID

            existing = await session.execute(
                select(Path).where(
                    Path.namespace == namespace,
                    Path.domain == new_domain,
                    Path.path == new_path,
                )
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"Path '{new_domain}://{new_path}' already exists")

            # Snapshot existing rows under this prefix so rows_after only
            # contains rows actually created by this call.
            before_subtree = await self._get_subtree_path_rows(
                session, new_domain, new_path, namespace=namespace
            )
            before_path_keys = {(row["domain"], row["path"]) for row in before_subtree}

            if await self._would_create_cycle(session, parent_uuid, target_node_uuid):
                raise ValueError(
                    f"Cannot create alias '{new_domain}://{new_path}': "
                    f"target node is an ancestor of the destination parent, "
                    f"which would create a cycle in the graph."
                )

            result = await self._create_edge_with_paths(
                session,
                parent_uuid,
                target_node_uuid,
                new_path.rsplit("/", 1)[-1],
                new_domain,
                new_path,
                priority,
                disclosure,
                namespace,
            )
            await session.flush()

            after_subtree = await self._get_subtree_path_rows(
                session, new_domain, new_path, namespace=namespace
            )
            created_paths = [
                row
                for row in after_subtree
                if (row["domain"], row["path"]) not in before_path_keys
            ]

            rows_after: Dict[str, list] = {
                "paths": created_paths,
            }
            if result["edge_created"]:
                rows_after["edges"] = [serialize_row(result["edge"])]

            affected_nodes = await self._search.get_node_uuids_for_prefix(session, new_domain, new_path, namespace=namespace)
            for node_uuid in affected_nodes:
                await self._search.refresh_search_documents_for_node(node_uuid, session=session, namespace=namespace)

            return {
                "new_uri": f"{new_domain}://{new_path}",
                "target_uri": f"{target_domain}://{target_path}",
                "node_uuid": target_node_uuid,
                "edge_id": result["edge_id"],
                "edge_created": result["edge_created"],
                "rows_after": rows_after,
            }

    async def remove_path(
        self, path: str, domain: str = "core", session: Optional[AsyncSession] = None, namespace: str = ""
    ) -> Dict[str, Any]:
        """
        Remove a path and its sub-paths with orphan prevention.

        Pre-flight safety: refuses to proceed if any direct child of the
        target node would become unreachable (no surviving paths outside
        the deletion set).

        If the target node loses its last reachable path, pathless incoming/
        outgoing edges around the target node are pruned so dead graph
        fragments do not linger.  The target node's memory is preserved but
        becomes an orphan (recoverable via the review interface).

        Raises:
            ValueError: If the path does not exist, or if deletion would
                create unreachable child nodes.
        """
        if path == "":
            raise ValueError("Cannot remove the root path.")

        async with self._optional_session(session) as session:
            target = await self._resolve_path(session, path, domain, namespace=namespace)
            if not target:
                raise ValueError(f"Path '{domain}://{path}' not found")
            _, target_edge, target_node_uuid = target

            # Pre-flight orphan check for children
            child_edges_result = await session.execute(
                select(Edge).where(Edge.parent_uuid == target_node_uuid)
            )
            child_edges = child_edges_result.scalars().all()

            would_orphan = []
            repair_tasks = []

            safe_prefix = escape_like_literal(path)
            exclude_cond = and_(
                Path.domain == domain,
                or_(
                    Path.path == path,
                    Path.path.like(f"{safe_prefix}/%", escape="\\"),
                )
            )
            if namespace is not None:
                exclude_cond = and_(exclude_cond, Path.namespace == namespace)

            surviving_query = select(Path).where(
                Path.node_uuid == target_node_uuid,
                ~exclude_cond,
            )
            if namespace is not None:
                surviving_query = surviving_query.where(Path.namespace == namespace)
            
            from sqlalchemy import case
            surviving_query = surviving_query.order_by(
                case(
                    (Path.domain == domain, 0),
                    else_=1
                ),
                Path.path
            )
            
            surviving_paths_result = await session.execute(surviving_query.limit(1))
            surviving_path = surviving_paths_result.scalar_one_or_none()

            for child_edge in child_edges:
                surviving_count = await self._count_incoming_paths(
                    session,
                    child_edge.child_uuid,
                    search_all_namespaces=True,
                    exclude_domain=domain,
                    exclude_path_prefix=path,
                    exclude_namespace=namespace,
                )
                if surviving_count == 0:
                    if surviving_path is None:
                        would_orphan.append(child_edge)
                    else:
                        repair_tasks.append(child_edge)

            if would_orphan:
                details = ", ".join(
                    f"'{e.name}' (node: {e.child_uuid[:8]}...)" for e in would_orphan
                )
                raise ValueError(
                    f"Cannot remove '{domain}://{path}': "
                    f"the following child node(s) would become unreachable: "
                    f"{details}. "
                    f"Create alternative paths for these children first, "
                    f"or remove them explicitly."
                )

            if repair_tasks and surviving_path:
                for child_edge in repair_tasks:
                    base_name = child_edge.name
                    expected_path = f"{surviving_path.path}/{base_name}" if surviving_path.path else base_name

                    suffix_counter = 0
                    max_attempts = 100
                    while True:
                        existing_path_result = await session.execute(
                            select(1).where(
                                Path.namespace == surviving_path.namespace,
                                Path.domain == surviving_path.domain,
                                Path.path == expected_path,
                            )
                        )
                        if existing_path_result.scalar_one_or_none() is None:
                            break

                        if suffix_counter >= max_attempts:
                            raise ValueError(
                                f"Cannot find a free path for child '{base_name}' "
                                f"under '{surviving_path.domain}://{surviving_path.path}' "
                                f"after {max_attempts} attempts"
                            )

                        if suffix_counter == 0:
                            fallback_name = f"{base_name}_{child_edge.child_uuid[:8]}"
                        else:
                            fallback_name = f"{base_name}_{child_edge.child_uuid[:8]}_{suffix_counter}"

                        expected_path = f"{surviving_path.path}/{fallback_name}" if surviving_path.path else fallback_name
                        suffix_counter += 1

                    await self._insert_path(
                        session,
                        surviving_path.domain,
                        expected_path,
                        child_edge.id,
                        child_edge.child_uuid,
                        surviving_path.namespace,
                    )
                    await self._cascade_create_paths(
                        session,
                        child_edge.child_uuid,
                        surviving_path.domain,
                        expected_path,
                        _visited=None,
                        namespace=surviving_path.namespace,
                    )

            collector = ChangeCollector()
            affected_nodes = await self._search.get_node_uuids_for_prefix(session, domain, path, namespace=namespace)
            await self._delete_subtree_paths(session, domain, path, namespace=namespace, collector=collector)
            await session.flush()

            # GC: edge + node cleanup (collector records deleted edges/memories)
            await self._gc_edge_if_pathless(session, target_edge, collector=collector)

            await self._gc_node_soft(session, target_node_uuid, collector=collector)

            for node_uuid in affected_nodes:
                await self._search.refresh_search_documents_for_node(node_uuid, session=session, namespace=namespace)

            return {
                "rows_before": collector.to_dict(),
                "rows_after": {},
            }

    async def restore_path(
        self,
        path: str,
        domain: str,
        node_uuid: str,
        parent_uuid: Optional[str] = None,
        priority: int = 0,
        disclosure: Optional[str] = None,
        session: Optional[AsyncSession] = None,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """
        Restore a path pointing to a node (used for rollback of delete).

        Creates/finds the edge from the parent to the target node,
        creates the path entry, and ensures the node has an active memory.

        Rollback may need to restore a deleted path for a node that still has
        surviving paths in other namespaces. That is allowed here because this
        method reconstructs a previously existing mapping; it is not creating a
        new cross-namespace association from scratch.
        """
        if path == "":
            raise ValueError("Cannot restore the root path.")

        async with self._optional_session(session) as session:
            node_result = await session.execute(
                select(Node).where(Node.uuid == node_uuid)
            )
            if not node_result.scalar_one_or_none():
                raise ValueError(f"Node '{node_uuid}' not found")

            active_mem = await session.execute(
                select(Memory).where(
                    Memory.node_uuid == node_uuid, Memory.deprecated == False
                )
            )
            if not active_mem.scalar_one_or_none():
                latest = await session.execute(
                    select(Memory)
                    .where(Memory.node_uuid == node_uuid)
                    .order_by(Memory.created_at.desc())
                    .limit(1)
                )
                latest_mem = latest.scalar_one_or_none()
                if not latest_mem:
                    raise ValueError(f"Node '{node_uuid}' has no memory versions")
                await session.execute(
                    update(Memory)
                    .where(Memory.id == latest_mem.id)
                    .values(deprecated=False, migrated_to=None)
                )

            existing = await session.execute(
                select(Path).where(
                    Path.namespace == namespace,
                    Path.domain == domain,
                    Path.path == path,
                )
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"Path '{domain}://{path}' already exists")

            if parent_uuid is None:
                if "/" in path:
                    parent_path_str = path.rsplit("/", 1)[0]
                    parent = await self._resolve_path(session, parent_path_str, domain, namespace=namespace)
                    if parent:
                        _, _, parent_uuid = parent
                    else:
                        parent_uuid = ROOT_NODE_UUID
                else:
                    parent_uuid = ROOT_NODE_UUID

            edge_name = path.rsplit("/", 1)[-1]
            edge, _ = await self._get_or_create_edge(
                session, parent_uuid, node_uuid, edge_name, priority, disclosure
            )
            await self._insert_path(session, domain, path, edge.id, node_uuid, namespace=namespace)
            await self._search.refresh_search_documents_for_node(node_uuid, session=session, namespace=namespace)

            return {"uri": f"{domain}://{path}", "node_uuid": node_uuid}

    # =========================================================================
    # Recent Memories
    # =========================================================================

    async def get_recent_memories(self, limit: int = 10, namespace: str = "", search_all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """
        Get the most recently created/updated non-deprecated memories
        that have at least one path.
        """
        async with self.session() as session:
            stmt = (
                select(Memory, Edge, Path)
                .select_from(Path)
                .join(Edge, Path.edge_id == Edge.id)
                .join(
                    Memory,
                    and_(
                        Memory.node_uuid == Edge.child_uuid,
                        Memory.deprecated == False,
                    ),
                )
            )

            if not search_all_namespaces:
                stmt = stmt.where(Path.namespace == namespace)
            
            stmt = stmt.order_by(Memory.created_at.desc())
            
            result = await session.execute(stmt)

            seen = set()
            memories = []

            for memory, edge, path_obj in result.all():
                if memory.id in seen:
                    continue
                seen.add(memory.id)

                memories.append(
                    {
                        "memory_id": memory.id,
                        "uri": f"{path_obj.domain}://{path_obj.path}",
                        "priority": edge.priority,
                        "disclosure": edge.disclosure,
                        "created_at": memory.created_at.isoformat()
                        if memory.created_at
                        else None,
                    }
                )

                if len(memories) >= limit:
                    break

            return memories

    # =========================================================================
    # Deprecated Memory Operations (for human's review)
    # =========================================================================

    async def get_memory_by_id(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a memory by its ID (including deprecated ones).
        """
        async with self.session() as session:
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()

            if not memory:
                return None

            paths = []
            if memory.node_uuid:
                paths_result = await session.execute(
                    select(Path.domain, Path.path)
                    .where(Path.node_uuid == memory.node_uuid)
                )
                paths = list({f"{r[0]}://{r[1]}" for r in paths_result.all()})

            return {
                "memory_id": memory.id,
                "node_uuid": memory.node_uuid,
                "content": memory.content,
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "deprecated": memory.deprecated,
                "migrated_to": memory.migrated_to,
                "paths": paths,
            }

    async def get_deprecated_memories(self) -> List[Dict[str, Any]]:
        """
        Get all deprecated memories for human's review.
        """
        async with self.session() as session:
            result = await session.execute(
                select(Memory)
                .where(Memory.deprecated == True)
                .order_by(Memory.created_at.desc())
            )

            return [
                {
                    "id": m.id,
                    "content_snippet": m.content[:200] + "..."
                    if len(m.content) > 200
                    else m.content,
                    "migrated_to": m.migrated_to,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in result.scalars().all()
            ]

    async def _resolve_migration_chain(
        self, session: AsyncSession, start_id: int, max_hops: int = 50
    ) -> Optional[Dict[str, Any]]:
        """Follow the migrated_to chain to the final target."""
        current_id = start_id
        for _ in range(max_hops):
            result = await session.execute(
                select(Memory).where(Memory.id == current_id)
            )
            memory = result.scalar_one_or_none()
            if not memory:
                return None
            if memory.migrated_to is None:
                paths = []
                if memory.node_uuid:
                    paths_result = await session.execute(
                        select(Path.domain, Path.path)
                        .where(Path.node_uuid == memory.node_uuid)
                    )
                    paths = list({f"{r[0]}://{r[1]}" for r in paths_result.all()})
                return {
                    "id": memory.id,
                    "content": memory.content,
                    "content_snippet": memory.content[:200] + "..."
                    if len(memory.content) > 200
                    else memory.content,
                    "created_at": memory.created_at.isoformat()
                    if memory.created_at
                    else None,
                    "deprecated": memory.deprecated,
                    "paths": paths,
                }
            current_id = memory.migrated_to
        return None

    async def get_all_orphan_memories(self) -> List[Dict[str, Any]]:
        """
        Get all orphan memories (deprecated=True).

        Two sub-categories (distinguished by migrated_to):
        - "deprecated": migrated_to is set — old version replaced by update_memory.
        - "orphaned": migrated_to is NULL — node lost all paths.
        """
        async with self.session() as session:
            orphans = []

            result = await session.execute(
                select(Memory)
                .where(Memory.deprecated == True)
                .order_by(Memory.created_at.desc())
            )

            for memory in result.scalars().all():
                category = "deprecated" if memory.migrated_to else "orphaned"
                item = {
                    "id": memory.id,
                    "node_uuid": memory.node_uuid,
                    "content_snippet": memory.content[:200] + "..."
                    if len(memory.content) > 200
                    else memory.content,
                    "created_at": memory.created_at.isoformat()
                    if memory.created_at
                    else None,
                    "deprecated": True,
                    "migrated_to": memory.migrated_to,
                    "category": category,
                    "migration_target": None,
                }

                if memory.migrated_to:
                    target = await self._resolve_migration_chain(
                        session, memory.migrated_to
                    )
                    if target:
                        item["migration_target"] = {
                            "id": target["id"],
                            "paths": target["paths"],
                            "content_snippet": target["content_snippet"],
                        }

                orphans.append(item)

            return orphans

    async def get_orphan_detail(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """
        Get full detail of an orphan memory for content viewing and diff.
        """
        async with self.session() as session:
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()
            if not memory:
                return None

            if not memory.deprecated:
                category = "active"
            elif memory.migrated_to:
                category = "deprecated"
            else:
                category = "orphaned"

            detail = {
                "id": memory.id,
                "node_uuid": memory.node_uuid,
                "content": memory.content,
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "deprecated": memory.deprecated,
                "migrated_to": memory.migrated_to,
                "category": category,
                "migration_target": None,
            }

            if memory.migrated_to:
                target = await self._resolve_migration_chain(
                    session, memory.migrated_to
                )
                if target:
                    detail["migration_target"] = {
                        "id": target["id"],
                        "content": target["content"],
                        "paths": target["paths"],
                        "created_at": target["created_at"],
                    }

            return detail

    async def permanently_delete_memory(self, memory_id: int) -> Dict[str, Any]:
        """
        Permanently delete a memory version (human only).

        Repairs the version chain, deletes the row.  If this was the
        last memory for the node, hard-GCs the node.
        Refuses to delete an active memory.

        Returns a compact result plus ``rows_before`` aligned with
        other delete flows.
        """
        async with self.session() as session:
            mem_result = await session.execute(select(Memory).where(Memory.id == memory_id))
            mem = mem_result.scalar_one_or_none()
            if not mem:
                raise ValueError(f"Memory {memory_id} not found")

            delete_result = await self._safely_delete_memory(
                session,
                memory_id,
                require_deprecated=True,
            )

            rows_before: Dict[str, list] = {
                "nodes": [],
                "memories": [delete_result["deleted_memory_before"]],
                "edges": [],
                "paths": [],
                "glossary_keywords": [],
            }

            response: Dict[str, Any] = {
                "deleted_memory_id": delete_result["deleted_memory_id"],
                "chain_repaired_to": delete_result["chain_repaired_to"],
            }

            node_uuid = delete_result["node_uuid"]
            if node_uuid:
                gc_snapshot = await self._gc_node_if_memoryless(session, node_uuid)
                if gc_snapshot:
                    for table in ("nodes", "memories", "edges", "paths", "glossary_keywords"):
                        rows_before[table].extend(gc_snapshot.get(table, []))

            response["rows_before"] = rows_before
            response["rows_after"] = {}

            return response

    async def restore_orphan_memory(
        self,
        memory_id: int,
        new_path: str,
        new_domain: str = "core",
        priority: int = 0,
        disclosure: Optional[str] = None,
        namespace: str = "",
    ) -> Dict[str, Any]:
        """
        Restore an orphan or deprecated memory by assigning a new path and
        activating it (setting deprecated=False).
        """
        if not new_path:
            raise ValueError("New path cannot be empty")

        async with self.session() as session:
            # 1. Fetch memory
            result = await session.execute(
                select(Memory).where(Memory.id == memory_id)
            )
            memory = result.scalar_one_or_none()
            if not memory:
                raise ValueError(f"Memory ID {memory_id} not found")

            # 2. Resolve parent node UUID and check validity
            if "/" in new_path:
                parent_path = new_path.rsplit("/", 1)[0]
                parent = await self._resolve_path(session, parent_path, new_domain, namespace=namespace)
                if not parent:
                    raise ValueError(
                        f"Parent '{new_domain}://{parent_path}' does not exist. "
                        f"Create the parent first, or use a shallower path."
                    )
                _, _, parent_uuid = parent
            else:
                parent_uuid = ROOT_NODE_UUID

            # 3. Check for duplicates
            existing = await session.execute(
                select(Path).where(
                    Path.namespace == namespace,
                    Path.domain == new_domain,
                    Path.path == new_path,
                )
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"Path '{new_domain}://{new_path}' already exists")

            # 4. Reject historical deprecated versions of active nodes
            # If memory.migrated_to is not None, it means this is a historical version of a node
            # that has an active version elsewhere in the graph. We do not restore old versions here,
            # we only restore true orphaned nodes that lost all their paths.
            if memory.migrated_to is not None:
                raise ValueError(
                    "Historical deprecated versions of an active node cannot be restored. "
                    "Only true orphaned memories (which lost all paths) can be restored."
                )

            target_node_uuid = memory.node_uuid
            restored_memory_id = memory_id

            # 5. Check for cycles
            if await self._would_create_cycle(session, parent_uuid, target_node_uuid):
                raise ValueError(
                    f"Cannot restore memory to '{new_domain}://{new_path}': "
                    f"this would create a cycle in the graph."
                )

            # 6. Bind the path (creates Edge and Path row(s))
            await self._create_edge_with_paths(
                session,
                parent_uuid,
                target_node_uuid,
                new_path.rsplit("/", 1)[-1],
                new_domain,
                new_path,
                priority=priority,
                disclosure=disclosure,
                namespace=namespace,
            )

            # 7. For true orphans, deprecate other versions (if any) and un-deprecate the target memory
            await self._deprecate_node_memories(
                session,
                target_node_uuid,
                successor_id=restored_memory_id,
            )
            await session.execute(
                update(Memory)
                .where(Memory.id == restored_memory_id)
                .values(deprecated=False, migrated_to=None)
            )

            # 8. Refresh Search Index
            await self._search.refresh_search_documents_for_node(
                target_node_uuid, session=session, namespace=namespace, refresh_all_namespaces=True
            )

            return {
                "success": True,
                "memory_id": restored_memory_id,
                "node_uuid": target_node_uuid,
                "uri": f"{new_domain}://{new_path}"
            }

