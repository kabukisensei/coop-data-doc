"""Medallion layer assignment — bronze / silver / gold (Module 4½).

Object *type* (table / view / stored proc) is detected from the SQL itself;
the *layer* cannot be (a ``CREATE TABLE`` doesn't say "I'm silver"), so it is
assigned here from user-declared rules in ``config.layers`` — by schema and/or
source-file path. Tables get retyped to the layer's node type; views and procs
carry their layer in ``metadata["layer"]``.

Anything no rule matches falls back to a read/write heuristic: a table with no
defining evidence in the repos — no WRITES/DEFINES edge *and* no ``source_file``
(repo DDL) — is treated as a silver source, otherwise gold. Bronze is only ever
produced by an explicit rule, so skipping bronze (or silver) is just a matter of
not declaring it.
"""

from __future__ import annotations

import fnmatch

from coop_data_doc.config import Config, ParseWarning
from coop_data_doc.graph.model import EdgeType, LineageGraph, NodeType

# precedence when several layers match: the most-refined wins
_PRECEDENCE = ("gold", "silver", "bronze")
_LAYER_TABLE_TYPE = {
    "bronze": NodeType.BRONZE_TABLE,
    "silver": NodeType.SILVER_TABLE,
    "gold": NodeType.GOLD_TABLE,
}
_TABLE_TYPES = (NodeType.BRONZE_TABLE, NodeType.SILVER_TABLE, NodeType.GOLD_TABLE)

# SQL node types whose schema is a real database schema (prunable). PBI
# nodes use the model/report name as schema_name and are never pruned here.
_SQL_NODE_TYPES = (
    NodeType.BRONZE_TABLE,
    NodeType.SILVER_TABLE,
    NodeType.GOLD_TABLE,
    NodeType.VIEW,
    NodeType.STORED_PROC,
)

# system catalog schemas — never user data lineage, always dropped
SYSTEM_SCHEMAS = frozenset({"sys", "information_schema", "tempdb", "guest"})


def prune_schemas(graph: LineageGraph, ignore_schemas: list[str]) -> int:
    """Remove SQL nodes whose schema is a system schema or in the user's
    ignore list (and any edges touching them). Returns the count dropped.

    System schemas (sys, information_schema, tempdb, guest, and any db_*
    fixed database role) are phantom nodes from procs that query the catalog —
    never real lineage — so they go automatically (the first four live in
    SYSTEM_SCHEMAS; db_* is matched by a startswith check); ignore_schemas drops
    client-specific noise on top.
    """
    ignored = SYSTEM_SCHEMAS | {s.lower() for s in ignore_schemas}

    def is_ignored(schema: str) -> bool:
        # Cross-db reads carry multi-part schemas ("otherdb.sys",
        # "linkedsrv.otherdb.staging"): test the full string (so a user can
        # ignore one database's schema exactly) AND the final segment — the
        # actual schema — so OtherDb.sys catalog noise and ignored schema
        # names are dropped wherever the database lives.
        schema = (schema or "").lower()
        segment = schema.rsplit(".", 1)[-1]
        return schema in ignored or segment in ignored or segment.startswith("db_")

    drop = {
        node_id
        for node_id, node in graph.nodes.items()
        if node.node_type in _SQL_NODE_TYPES and is_ignored(node.schema_name)
    }
    for node_id in drop:
        del graph.nodes[node_id]
    if drop:
        graph.replace_edges(
            [edge for edge in graph.edges if edge.source_id not in drop and edge.target_id not in drop]
        )
    return len(drop)


def _path_matches(globs: list[str], source_file: str) -> bool:
    # Same deterministic cross-OS policy as crawler._matches: case-INSENSITIVE
    # on every platform (fnmatch.fnmatch normcases only on Windows, which
    # would assign the same repo different layers per OS).
    posix = source_file.replace("\\", "/").casefold()
    for pattern in globs:
        folded = pattern.casefold()
        if fnmatch.fnmatchcase(posix, folded):
            return True
        if folded.startswith("**/") and fnmatch.fnmatchcase(posix, folded[3:]):
            return True
    return False


def _rule_layer(config: Config, schema: str, source_file: str) -> str | None:
    """The configured layer for a node, or None if no rule matches."""
    schema = (schema or "").lower()
    for layer in _PRECEDENCE:
        rule = config.layers.get(layer)
        if rule is None:
            continue
        if schema and schema in {s.lower() for s in rule.schemas}:
            return layer
        if _path_matches(rule.paths, source_file):
            return layer
    return None


def assign_layers(graph: LineageGraph, config: Config) -> list[ParseWarning]:
    """Assign bronze/silver/gold to every table/view/proc, rules first then
    a read/write heuristic. Returns warnings listing nodes that fell back to
    the heuristic (so the user can refine their rules)."""
    warnings: list[ParseWarning] = []

    # pass 1 — explicit rules (collect table retypes, then apply, so we don't
    # mutate node ids mid-iteration)
    retypes: list[tuple[str, NodeType]] = []
    explicit_tables: set[str] = set()
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        layer = _rule_layer(config, node.schema_name, node.source_file)
        if layer is None:
            continue
        if node.node_type in _TABLE_TYPES:
            retypes.append((node_id, _LAYER_TABLE_TYPE[layer]))
            explicit_tables.add(node_id)
        elif node.node_type in (NodeType.VIEW, NodeType.STORED_PROC):
            node.metadata["layer"] = layer
    for node_id, new_type in retypes:
        new_id = graph.retype_node(node_id, new_type)
        explicit_tables.discard(node_id)
        explicit_tables.add(new_id)

    # pass 2 — heuristic fallback for tables no rule covered
    written = {
        edge.target_id for edge in graph.edges if edge.edge_type in (EdgeType.WRITES, EdgeType.DEFINES)
    }
    for node_id in sorted(graph.nodes):
        node = graph.nodes.get(node_id)
        if node is None or node.node_type not in _TABLE_TYPES or node_id in explicit_tables:
            continue
        # a table with no defining evidence at all is an external source: no
        # WRITES/DEFINES edge AND no source_file. A node with a source_file
        # was parsed from repo DDL (a standalone CREATE TABLE emits no edge),
        # so it is repo-defined by construction and keeps gold, silently —
        # the same treatment as a written table (issue #29).
        if node.node_type is NodeType.GOLD_TABLE and node_id not in written and not node.source_file:
            graph.retype_node(node_id, NodeType.SILVER_TABLE)
            if config.layers:
                warnings.append(
                    ParseWarning(
                        file=node.source_file or node.name,
                        message=(
                            f"{node.name}: no layer rule matched; defaulted to silver "
                            "(read-only source: never created or written in these repos). "
                            "Add a rule to classify it explicitly."
                        ),
                        category="layer_unclassified",
                    )
                )

    # tag every table node's layer in metadata too, for uniform downstream use
    for node in graph.nodes.values():
        if node.node_type in _TABLE_TYPES:
            node.metadata["layer"] = node.node_type.value.split("_", 1)[0]
    return warnings
