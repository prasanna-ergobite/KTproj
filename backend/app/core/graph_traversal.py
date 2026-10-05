"""
Deterministic Knowledge Graph Traversal Engine for AutoKT.
Executes exact Cypher queries over existing Neo4j PART_OF relationships for structural codebase questions.
Bypasses vector similarity truncation and guarantees 100% recall of symbol/file/module relationships.
"""

import logging
from typing import List, Dict, Any, Optional
from app.db.neo4j_client import neo4j_client
from app.core.query_router import QueryIntent, IntentClassificationResult

logger = logging.getLogger("autokt.graph_traversal")


def get_sibling_functions_in_module(org_id: str, function_name: str) -> List[Dict[str, Any]]:
    """
    Find all functions in the same module as target_function across all files in that module.
    """
    cypher = """
    MATCH (target:Function {organization_id: $organization_id})
    WHERE target.name = $fn_name OR target.id ENDS WITH ('::' + $fn_name)
    MATCH (target)-[:PART_OF]->(f:File)-[:PART_OF]->(m:Module)
    MATCH (m)-[:PART_OF]->(r:Repository)
    MATCH (other_f:File {organization_id: $organization_id})-[:PART_OF]->(m)
    MATCH (fn:Function {organization_id: $organization_id})-[:PART_OF]->(other_f)
    WHERE fn.id <> target.id
    OPTIONAL MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(other_f)
    RETURN r { .id, .name, .url } AS repository,
           m { .id, .name, .path } AS module,
           other_f.path AS file_path,
           other_f.id AS file_id,
           fn.name AS function_name,
           fn.id AS function_id,
           fn.start_line AS start_line,
           fn.end_line AS end_line,
           collect(DISTINCT p { .name, .email, commit_count: o.commit_count }) AS owners
    ORDER BY other_f.path ASC, fn.start_line ASC
    """
    return neo4j_client.run_read_query(
        cypher,
        parameters={"fn_name": function_name},
        organization_id=org_id,
    )


def get_sibling_functions_in_file(org_id: str, function_name: str) -> List[Dict[str, Any]]:
    """
    Find all other functions defined in the exact same file as target_function.
    """
    cypher = """
    MATCH (target:Function {organization_id: $organization_id})
    WHERE target.name = $fn_name OR target.id ENDS WITH ('::' + $fn_name)
    MATCH (target)-[:PART_OF]->(f:File)-[:PART_OF]->(m:Module)-[:PART_OF]->(r:Repository)
    MATCH (fn:Function {organization_id: $organization_id})-[:PART_OF]->(f)
    WHERE fn.id <> target.id
    OPTIONAL MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(f)
    RETURN r { .id, .name, .url } AS repository,
           m { .id, .name, .path } AS module,
           f.path AS file_path,
           f.id AS file_id,
           fn.name AS function_name,
           fn.id AS function_id,
           fn.start_line AS start_line,
           fn.end_line AS end_line,
           collect(DISTINCT p { .name, .email, commit_count: o.commit_count }) AS owners
    ORDER BY fn.start_line ASC
    """
    return neo4j_client.run_read_query(
        cypher,
        parameters={"fn_name": function_name},
        organization_id=org_id,
    )


def get_file_symbols(org_id: str, file_path: str) -> List[Dict[str, Any]]:
    """
    Find all functions and classes defined inside a specific file.
    """
    cypher = """
    MATCH (f:File {organization_id: $organization_id})
    WHERE f.path = $file_path OR f.path ENDS WITH ('/' + $file_path) OR f.path ENDS WITH $file_path
    MATCH (f)-[:PART_OF]->(m:Module)-[:PART_OF]->(r:Repository)
    OPTIONAL MATCH (fn:Function {organization_id: $organization_id})-[:PART_OF]->(f)
    OPTIONAL MATCH (cls:Class {organization_id: $organization_id})-[:PART_OF]->(f)
    OPTIONAL MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(f)
    RETURN r { .id, .name, .url } AS repository,
           m { .id, .name, .path } AS module,
           f.path AS file_path,
           f.id AS file_id,
           collect(DISTINCT fn { .name, .start_line, .end_line }) AS functions,
           collect(DISTINCT cls { .name, .start_line, .end_line }) AS classes,
           collect(DISTINCT p { .name, .email, commit_count: o.commit_count }) AS owners
    """
    return neo4j_client.run_read_query(
        cypher,
        parameters={"file_path": file_path},
        organization_id=org_id,
    )


def get_function_containing_file(org_id: str, symbol_name: str) -> List[Dict[str, Any]]:
    """
    Find which file and module contain a target function or class symbol.
    """
    cypher = """
    MATCH (sym {organization_id: $organization_id})
    WHERE (sym:Function OR sym:Class) AND
          (sym.name = $sym_name OR sym.id ENDS WITH ('::' + $sym_name))
    MATCH (sym)-[:PART_OF]->(f:File)-[:PART_OF]->(m:Module)-[:PART_OF]->(r:Repository)
    OPTIONAL MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(f)
    RETURN labels(sym)[0] AS symbol_type,
           sym.name AS symbol_name,
           sym.start_line AS start_line,
           sym.end_line AS end_line,
           f.path AS file_path,
           f.id AS file_id,
           m { .id, .name, .path } AS module,
           r { .id, .name, .url } AS repository,
           collect(DISTINCT p { .name, .email, commit_count: o.commit_count }) AS owners
    """
    return neo4j_client.run_read_query(
        cypher,
        parameters={"sym_name": symbol_name},
        organization_id=org_id,
    )


def get_file_containing_module(org_id: str, file_path: str) -> List[Dict[str, Any]]:
    """
    Find which module and repository contain a target file.
    """
    cypher = """
    MATCH (f:File {organization_id: $organization_id})
    WHERE f.path = $file_path OR f.path ENDS WITH ('/' + $file_path) OR f.path ENDS WITH $file_path
    MATCH (f)-[:PART_OF]->(m:Module)-[:PART_OF]->(r:Repository)
    OPTIONAL MATCH (p:Person {organization_id: $organization_id})-[o:OWNS]->(f)
    RETURN f.path AS file_path,
           f.id AS file_id,
           m { .id, .name, .path } AS module,
           r { .id, .name, .url } AS repository,
           collect(DISTINCT p { .name, .email, commit_count: o.commit_count }) AS owners
    """
    return neo4j_client.run_read_query(
        cypher,
        parameters={"file_path": file_path},
        organization_id=org_id,
    )


def execute_structural_traversal(intent_res: IntentClassificationResult, org_id: str) -> List[Dict[str, Any]]:
    """
    Execute the matching Cypher graph traversal for a structural QueryIntent.
    Returns normalized raw record objects.
    """
    entity = intent_res.entity_name or ""
    intent = intent_res.intent

    if intent == QueryIntent.SAME_MODULE_FUNCTIONS:
        return get_sibling_functions_in_module(org_id, entity)
    elif intent == QueryIntent.SAME_FILE_FUNCTIONS:
        return get_sibling_functions_in_file(org_id, entity)
    elif intent == QueryIntent.FILE_SYMBOLS:
        return get_file_symbols(org_id, entity)
    elif intent == QueryIntent.CONTAINING_FILE:
        return get_function_containing_file(org_id, entity)
    elif intent == QueryIntent.CONTAINING_MODULE:
        return get_file_containing_module(org_id, entity)
    else:
        return []
