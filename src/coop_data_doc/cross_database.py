"""Join a cross-database reference to the object a live source documents.

A Warehouse view that reads ``lakehouse_db.dbo.customers`` gets a definition-less
stub ``lakehouse_db.dbo.customers`` from the parser (a three-part name is another
database, see ``parsers.sql_common.qualify``). When the config's ``live_sources``
say that database is snapshotted into one of this estate's repos, and that repo
holds the file that defines ``dbo.customers``, the stub IS that object: its edges
move to the real node and the stub goes.

Nothing is guessed (hard rule 4). The fold needs all of: exactly one live source
with that database name, a two-part stub schema (a linked-server name is left
alone), exactly one documented object with the remaining schema and name, and its
source file present in that live source's repo and in no other live source's
repo. Two live sources that both define the same ``schema.name`` share one node
in this graph, so a reference to either is reported as
``cross_database_ambiguous`` and stays a stub; the collision itself is reported
as ``live_source_name_collision``.

Offline: this reads the config and checks that files exist. It never imports
``coop_data_doc.live`` and never connects.
"""

from __future__ import annotations

from pathlib import Path

from coop_data_doc.config import Config, ParseWarning
from coop_data_doc.graph.model import Edge, LineageGraph, NodeType, normalize_identifier

_SQL_NODE_TYPES = frozenset(
    {
        NodeType.BRONZE_TABLE,
        NodeType.SILVER_TABLE,
        NodeType.GOLD_TABLE,
        NodeType.VIEW,
        NodeType.STORED_PROC,
    }
)


def live_databases(config: Config) -> dict[str, str]:
    """``{normalized database name: live source name}`` for every database that
    exactly one live source names (a name two sources share is left out)."""
    by_db: dict[str, list[str]] = {}
    for name in sorted(config.live_sources):
        database = normalize_identifier(config.live_sources[name].database)
        by_db.setdefault(database, []).append(name)
    return {db: names[0] for db, names in by_db.items() if len(names) == 1}


def local_schema(config: Config, schema: str) -> str | None:
    """The schema inside its own database for a two-part ``database.schema`` whose
    database a live source snapshots; ``None`` for anything else."""
    parts = schema.split(".")
    if len(parts) != 2 or parts[0] not in live_databases(config):
        return None
    return parts[1]


def _live_roots(config: Config) -> dict[str, Path]:
    return {
        name: config.repo_root(source.repo)
        for name, source in sorted(config.live_sources.items())
        if source.repo in config.repos
    }


def link(graph: LineageGraph, config: Config) -> list[ParseWarning]:
    """Fold each provable cross-database stub into the object it names; return the
    warnings for the ones that could not be proven and for name collisions."""
    if not config.live_sources:
        return []
    databases = live_databases(config)
    roots = _live_roots(config)
    warnings: list[ParseWarning] = []

    def holders(source_file: str) -> list[str]:
        return [name for name, root in roots.items() if (root / source_file).is_file()]

    defined: dict[tuple[str, str], list[str]] = {}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if node.node_type not in _SQL_NODE_TYPES or not node.source_file:
            continue
        defined.setdefault((node.schema_name, node.name), []).append(node_id)
        if len(roots) > 1:
            held = holders(node.source_file)
            if len(held) > 1:
                warnings.append(
                    ParseWarning(
                        file=node.source_file,
                        message=(
                            f"{node.schema_name}.{node.name} is defined in more than one live source "
                            f"({', '.join(held)}); the docs show them as one object"
                        ),
                        category="live_source_name_collision",
                    )
                )

    redirect: dict[str, str] = {}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if node.node_type not in _SQL_NODE_TYPES or node.source_file:
            continue
        parts = node.schema_name.split(".")
        if len(parts) != 2 or parts[0] not in databases:
            continue
        source_name = databases[parts[0]]
        if source_name not in roots:
            continue
        candidates = defined.get((parts[1], node.name), [])
        if not candidates:
            continue  # the live source does not document it: it stays a stub
        target = graph.nodes[candidates[0]]
        held = holders(target.source_file) if len(candidates) == 1 else []
        if held != [source_name]:
            warnings.append(
                ParseWarning(
                    file=target.source_file,
                    message=(
                        f"a reference to {node.schema_name}.{node.name} was not joined to "
                        f"{parts[1]}.{node.name}: that name is not defined only in live source "
                        f"'{source_name}'"
                    ),
                    category="cross_database_ambiguous",
                )
            )
            continue
        redirect[node_id] = candidates[0]

    if redirect:
        deduped: dict[tuple[str, str, str], Edge] = {}
        for edge in graph.edges:
            edge.source_id = redirect.get(edge.source_id, edge.source_id)
            edge.target_id = redirect.get(edge.target_id, edge.target_id)
            if edge.source_id == edge.target_id:
                continue
            kept = deduped.get(edge.key())
            if kept is None:
                deduped[edge.key()] = edge
            elif not kept.evidence and edge.evidence:
                kept.evidence = edge.evidence
        graph.replace_edges(list(deduped.values()))
        for node_id in redirect:
            del graph.nodes[node_id]
    return sorted(warnings, key=lambda w: (w.category, w.file, w.message))
