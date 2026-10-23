"""
Knowledge Graph Visualization API Router.
Provides structured nodes and edges from Neo4j for interactive graph exploration,
hierarchical scoping, node neighborhood expansion, and graph search.
"""

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.db.neo4j_client import neo4j_client

logger = logging.getLogger("autokt.graph")

router = APIRouter(prefix="/graph", tags=["Knowledge Graph"])

# Entity styling tokens aligned with AutoKT Design System
NODE_STYLE_CONFIG = {
    "Repository": {"color": "#17201d", "val": 26, "badge": "Repo"},
    "Module": {"color": "#5579e8", "val": 18, "badge": "Module"},
    "File": {"color": "#44833f", "val": 12, "badge": "File"},
    "Function": {"color": "#ed7c4a", "val": 8, "badge": "Function"},
    "Class": {"color": "#8d6aca", "val": 10, "badge": "Class"},
    "Person": {"color": "#c85b52", "val": 11, "badge": "Owner"},
    "Document": {"color": "#0d9488", "val": 14, "badge": "Doc"},
    "Package": {"color": "#eab308", "val": 9, "badge": "Package"},
}


class GraphNode(BaseModel):
    id: str = Field(..., description="Unique node identifier or tenant key")
    label: str = Field(..., description="Primary Neo4j entity label")
    name: str = Field(..., description="Human-readable node title/name")
    path: Optional[str] = Field(None, description="Relative file/module path if applicable")
    val: float = Field(10.0, description="Visual scale/radius weight for physics simulation")
    color: str = Field("#5579e8", description="Hex color token")
    badge: Optional[str] = Field(None, description="Short entity badge text")
    properties: Dict[str, Any] = Field(default_factory=dict, description="Raw metadata and metrics")


class GraphLink(BaseModel):
    source: str = Field(..., description="Source node id")
    target: str = Field(..., description="Target node id")
    type: str = Field(..., description="Relationship type e.g. PART_OF, CALLS, OWNS")
    label: str = Field(..., description="Human-readable relationship label")
    properties: Dict[str, Any] = Field(default_factory=dict)


class GraphTopologyResponse(BaseModel):
    nodes: List[GraphNode]
    links: List[GraphLink]
    node_count: int
    link_count: int
    repository_id: Optional[str] = None
    module_id: Optional[str] = None


class GraphSearchItem(BaseModel):
    id: str
    label: str
    name: str
    path: Optional[str] = None
    badge: Optional[str] = None
    color: str


class GraphSearchResponse(BaseModel):
    results: List[GraphSearchItem]
    total: int


def _extract_node_dict(neo_node: Any, default_label: str = "Node") -> Optional[GraphNode]:
    """Helper to convert a Neo4j node object or map into a formatted GraphNode."""
    if not neo_node:
        return None

    # Handle Neo4j Node objects
    labels = list(getattr(neo_node, "labels", []))
    props = dict(getattr(neo_node, "_properties", neo_node if isinstance(neo_node, dict) else {}))

    label = labels[0] if labels else default_label
    # fallback to label from properties if available
    if label == "Node" and "label" in props:
        label = props["label"]

    style = NODE_STYLE_CONFIG.get(label, {"color": "#64748b", "val": 9, "badge": label})

    node_id = str(props.get("tenant_key") or props.get("id") or props.get("path") or props.get("name") or "")
    if not node_id:
        return None

    name = props.get("name") or props.get("title") or props.get("path") or node_id
    # Clean display name for files: use basename
    if label == "File" and "path" in props:
        name = props["path"].split("/")[-1].split("\\")[-1]

    # Dynamic sizing based on LOC or complexity if available
    val = style["val"]
    if "loc" in props and isinstance(props["loc"], (int, float)):
        val = min(30, max(8, style["val"] + (props["loc"] / 150)))
    elif "complexity" in props and isinstance(props["complexity"], (int, float)):
        val = min(25, max(8, style["val"] + (props["complexity"] / 4)))

    return GraphNode(
        id=node_id,
        label=label,
        name=name,
        path=props.get("path") or props.get("relative_path"),
        val=round(val, 1),
        color=style["color"],
        badge=style.get("badge", label),
        properties={k: v for k, v in props.items() if k not in ("embedding", "vector", "content") and not isinstance(v, (bytes, bytearray))},
    )


def _ensure_neo4j_ready():
    if neo4j_client.driver is None:
        try:
            neo4j_client.connect()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Neo4j database connection unavailable: {exc}",
            )


@router.get("/topology", response_model=GraphTopologyResponse)
async def get_graph_topology(
    repository_id: Optional[str] = Query(None, description="Repository ID to inspect"),
    organization_id: Optional[str] = Query(None, description="Tenant organization ID"),
    module_id: Optional[str] = Query(None, description="Optional module ID to filter"),
    include_functions: bool = Query(False, description="Whether to include fine-grained functions/classes"),
    limit: int = Query(250, ge=10, le=1000, description="Max node density limit"),
):
    """
    Fetch the multi-tenant architecture topology for the graph canvas.
    Retrieves Repositories, Modules, Files, Authors, and Docs, plus connecting edges.
    """
    _ensure_neo4j_ready()

    nodes_map: Dict[str, GraphNode] = {}
    links_list: List[GraphLink] = []
    seen_links = set()

    def add_link(src: str, tgt: str, rel_type: str, props: Optional[dict] = None):
        if not src or not tgt or src == tgt:
            return
        link_key = f"{src}->{rel_type}->{tgt}"
        if link_key not in seen_links:
            seen_links.add(link_key)
            links_list.append(
                GraphLink(
                    source=src,
                    target=tgt,
                    type=rel_type,
                    label=rel_type.replace("_", " "),
                    properties=props or {},
                )
            )

    try:
        with neo4j_client.driver.session() as session:
            # 1. Base Query: Repo & Modules
            repo_match = ""
            where_clauses = []
            params: Dict[str, Any] = {"limit": limit}

            if organization_id and organization_id.strip():
                where_clauses.append("r.organization_id = $organization_id")
                params["organization_id"] = organization_id.strip()

            if repository_id and repository_id.strip():
                where_clauses.append("(r.id = $repo_id OR r.name = $repo_id OR r.tenant_key = $repo_id)")
                params["repo_id"] = repository_id.strip()

            filter_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

            # Fetch Repositories and Modules
            cypher_repos_modules = f"""
            MATCH (r:Repository)
            {filter_str}
            OPTIONAL MATCH (m:Module)-[:PART_OF]->(r)
            RETURN r, m
            LIMIT $limit
            """
            res = session.run(cypher_repos_modules, params)
            for rec in res:
                r_node = rec.get("r")
                m_node = rec.get("m")
                gn_r = _extract_node_dict(r_node, "Repository")
                if gn_r:
                    nodes_map[gn_r.id] = gn_r

                if m_node:
                    gn_m = _extract_node_dict(m_node, "Module")
                    if gn_m:
                        nodes_map[gn_m.id] = gn_m
                        if gn_r:
                            add_link(gn_m.id, gn_r.id, "PART_OF")

            # 2. Fetch Files & Document/Ownership connections
            file_match_filter = []
            file_params: Dict[str, Any] = {"limit": limit}

            if organization_id and organization_id.strip():
                file_match_filter.append("m.organization_id = $organization_id")
                file_params["organization_id"] = organization_id.strip()

            if repository_id and repository_id.strip():
                file_match_filter.append("m.repository_id = $repo_id")
                file_params["repo_id"] = repository_id.strip()

            if module_id and module_id.strip():
                file_match_filter.append("(m.id = $module_id OR m.name = $module_id OR m.tenant_key = $module_id)")
                file_params["module_id"] = module_id.strip()

            where_files = f"WHERE {' AND '.join(file_match_filter)}" if file_match_filter else ""

            cypher_files = f"""
            MATCH (f:File)-[:PART_OF]->(m:Module)
            {where_files}
            OPTIONAL MATCH (p:Person)-[:OWNS]->(f)
            OPTIONAL MATCH (d:Document)-[:DOCUMENTS]->(m)
            OPTIONAL MATCH (d_f:Document)-[:MAPS_TO]->(f)
            RETURN f, m, p, d, d_f
            LIMIT $limit
            """
            res_files = session.run(cypher_files, file_params)
            for rec in res_files:
                f_node = rec.get("f")
                m_node = rec.get("m")
                p_node = rec.get("p")
                d_node = rec.get("d")
                df_node = rec.get("d_f")

                gn_m = _extract_node_dict(m_node, "Module")
                if gn_m and gn_m.id not in nodes_map:
                    nodes_map[gn_m.id] = gn_m

                gn_f = _extract_node_dict(f_node, "File")
                if gn_f:
                    nodes_map[gn_f.id] = gn_f
                    if gn_m:
                        add_link(gn_f.id, gn_m.id, "PART_OF")

                if p_node:
                    gn_p = _extract_node_dict(p_node, "Person")
                    if gn_p:
                        nodes_map[gn_p.id] = gn_p
                        if gn_f:
                            add_link(gn_p.id, gn_f.id, "OWNS")

                if d_node:
                    gn_d = _extract_node_dict(d_node, "Document")
                    if gn_d:
                        nodes_map[gn_d.id] = gn_d
                        if gn_m:
                            add_link(gn_d.id, gn_m.id, "DOCUMENTS")

                if df_node:
                    gn_df = _extract_node_dict(df_node, "Document")
                    if gn_df:
                        nodes_map[gn_df.id] = gn_df
                        if gn_f:
                            add_link(gn_df.id, gn_f.id, "MAPS_TO")

            # 3. Inter-File IMPORTS relationships
            if nodes_map:
                file_ids = [nid for nid, n in nodes_map.items() if n.label == "File"]
                if file_ids:
                    cypher_imports = """
                    MATCH (f1:File)-[r:IMPORTS]->(f2:File)
                    WHERE (f1.tenant_key IN $f_ids OR f1.id IN $f_ids)
                      AND (f2.tenant_key IN $f_ids OR f2.id IN $f_ids)
                    RETURN f1.tenant_key AS src, f2.tenant_key AS tgt
                    LIMIT 150
                    """
                    res_imp = session.run(cypher_imports, {"f_ids": file_ids[:100]})
                    for rec in res_imp:
                        src = rec.get("src")
                        tgt = rec.get("tgt")
                        if src in nodes_map and tgt in nodes_map:
                            add_link(src, tgt, "IMPORTS")

            # 4. Optional Functions & Calls
            if include_functions:
                cypher_funcs = """
                MATCH (fn:Function)-[:PART_OF]->(f:File)
                WHERE f.tenant_key IN $f_ids OR f.id IN $f_ids
                OPTIONAL MATCH (fn)-[:CALLS]->(callee:Function)
                RETURN fn, f, callee
                LIMIT 150
                """
                res_funcs = session.run(cypher_funcs, {"f_ids": file_ids[:50]})
                for rec in res_funcs:
                    fn_node = rec.get("fn")
                    f_node = rec.get("f")
                    callee = rec.get("callee")

                    gn_fn = _extract_node_dict(fn_node, "Function")
                    if gn_fn:
                        nodes_map[gn_fn.id] = gn_fn
                        f_id = f_node.get("tenant_key") if f_node else None
                        if f_id and f_id in nodes_map:
                            add_link(gn_fn.id, f_id, "PART_OF")

                        if callee:
                            gn_callee = _extract_node_dict(callee, "Function")
                            if gn_callee:
                                nodes_map[gn_callee.id] = gn_callee
                                add_link(gn_fn.id, gn_callee.id, "CALLS")

    except Exception as exc:
        logger.error("Error executing topology query: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to query knowledge graph topology: {exc}",
        )

    # Filter links to only those where both source and target exist in nodes_map
    valid_links = [link for link in links_list if link.source in nodes_map and link.target in nodes_map]

    return GraphTopologyResponse(
        nodes=list(nodes_map.values()),
        links=valid_links,
        node_count=len(nodes_map),
        link_count=len(valid_links),
        repository_id=repository_id,
        module_id=module_id,
    )


def _fetch_node_neighbors(
    node_id: str,
    organization_id: Optional[str] = None,
    limit: int = 40,
) -> GraphTopologyResponse:
    _ensure_neo4j_ready()

    nodes_map: Dict[str, GraphNode] = {}
    links_list: List[GraphLink] = []
    seen_links = set()

    try:
        with neo4j_client.driver.session() as session:
            cypher = """
            MATCH (center)
            WHERE center.tenant_key = $node_id OR center.id = $node_id OR center.path = $node_id
            MATCH (center)-[r]-(neighbor)
            RETURN center, type(r) AS rel_type, startNode(r) = center AS is_outbound, neighbor
            LIMIT $limit
            """
            params = {"node_id": node_id, "limit": limit}
            res = session.run(cypher, params)

            for rec in res:
                center = rec.get("center")
                neighbor = rec.get("neighbor")
                rel_type = rec.get("rel_type") or "CONNECTED_TO"
                is_outbound = rec.get("is_outbound", True)

                gn_c = _extract_node_dict(center)
                if gn_c:
                    nodes_map[gn_c.id] = gn_c

                gn_n = _extract_node_dict(neighbor)
                if gn_n:
                    nodes_map[gn_n.id] = gn_n

                if gn_c and gn_n:
                    src_id = gn_c.id if is_outbound else gn_n.id
                    tgt_id = gn_n.id if is_outbound else gn_c.id
                    link_key = f"{src_id}->{rel_type}->{tgt_id}"
                    if link_key not in seen_links:
                        seen_links.add(link_key)
                        links_list.append(
                            GraphLink(
                                source=src_id,
                                target=tgt_id,
                                type=rel_type,
                                label=rel_type.replace("_", " "),
                            )
                        )

    except Exception as exc:
        logger.error("Error expanding node neighbors for %s: %s", node_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to expand node neighbors: {exc}",
        )

    valid_links = [l for l in links_list if l.source in nodes_map and l.target in nodes_map]

    return GraphTopologyResponse(
        nodes=list(nodes_map.values()),
        links=valid_links,
        node_count=len(nodes_map),
        link_count=len(valid_links),
    )


@router.get("/node/neighbors", response_model=GraphTopologyResponse)
async def get_node_neighbors_query(
    node_id: str = Query(..., description="Node ID or tenant key to expand"),
    organization_id: Optional[str] = Query(None, description="Tenant organization ID"),
    limit: int = Query(40, ge=5, le=150, description="Max neighbor density"),
):
    """
    On-demand graph expansion by query param: returns 1-hop connected neighbors.
    """
    return _fetch_node_neighbors(node_id=node_id, organization_id=organization_id, limit=limit)


@router.get("/node/{node_id:path}/neighbors", response_model=GraphTopologyResponse)
async def get_node_neighbors_path(
    node_id: str,
    organization_id: Optional[str] = Query(None, description="Tenant organization ID"),
    limit: int = Query(40, ge=5, le=150, description="Max neighbor density"),
):
    """
    On-demand graph expansion by URL path: returns 1-hop connected neighbors.
    """
    return _fetch_node_neighbors(node_id=node_id, organization_id=organization_id, limit=limit)


@router.get("/search", response_model=GraphSearchResponse)
async def search_graph_nodes(
    query: str = Query(..., min_length=1, description="Search term for node name or path"),
    organization_id: Optional[str] = Query(None, description="Tenant organization ID"),
    limit: int = Query(20, ge=1, le=50),
):
    """
    Search for nodes by title, path, or function name to instantly locate them on canvas.
    """
    _ensure_neo4j_ready()
    items: List[GraphSearchItem] = []

    try:
        with neo4j_client.driver.session() as session:
            where_clauses = ["(toLower(n.name) CONTAINS toLower($q) OR toLower(n.path) CONTAINS toLower($q))"]
            params: Dict[str, Any] = {"q": query.strip(), "limit": limit}

            if organization_id and organization_id.strip():
                where_clauses.append("n.organization_id = $organization_id")
                params["organization_id"] = organization_id.strip()

            cypher = f"""
            MATCH (n)
            WHERE {' AND '.join(where_clauses)}
            RETURN n
            LIMIT $limit
            """
            res = session.run(cypher, params)
            for rec in res:
                gn = _extract_node_dict(rec.get("n"))
                if gn:
                    items.append(
                        GraphSearchItem(
                            id=gn.id,
                            label=gn.label,
                            name=gn.name,
                            path=gn.path,
                            badge=gn.badge,
                            color=gn.color,
                        )
                    )

    except Exception as exc:
        logger.error("Error searching graph nodes with query %s: %s", query, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to search graph nodes: {exc}",
        )

    return GraphSearchResponse(results=items, total=len(items))
