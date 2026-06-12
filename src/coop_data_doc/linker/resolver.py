"""Cross-repo identity resolution (Module 4).

Joins the SQL half of the graph to the Power BI half. Each pbi_table's
partition source runs through a ladder — cache, exact, config rule, fuzzy,
interactive — stopping at the first hit; the method is recorded in the
created edge's evidence. View schemas and semantic-model names are similar
but not identical in this estate, which is exactly what the config rules
and the prompt exist for.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from coop_data_doc.config import Config, ParseWarning
from coop_data_doc.graph.model import (
    Edge,
    EdgeType,
    LineageGraph,
    Node,
    NodeType,
    normalize_identifier,
)
from coop_data_doc.linker import interactive
from coop_data_doc.linker.cache import CacheEntry, LineageCache

FUZZY_AUTO_ACCEPT = 0.92
FUZZY_AMBIGUOUS = 0.6

_SQL_TYPES = (NodeType.VIEW, NodeType.GOLD_TABLE, NodeType.SILVER_TABLE)


class ResolutionResult(BaseModel):
    """Outcome summary: totals per resolution method plus unresolved keys."""
    resolved: int = 0
    unresolved: list[str] = Field(default_factory=list)
    methods: dict[str, int] = Field(default_factory=dict)

    def count(self, method: str) -> None:
        """Record one successful resolution under the given method."""
        self.resolved += 1
        self.methods[method] = self.methods.get(method, 0) + 1


class _Item(BaseModel):
    cache_key: str
    node_id: str
    schema_name: str
    object_name: str
    raw_kind: str


def _collect_items(graph: LineageGraph) -> list[_Item]:
    items: list[_Item] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if node.node_type is not NodeType.PBI_TABLE:
            continue
        source = node.metadata.get("partition_source")
        native_tables = node.metadata.get("native_query_tables") or []
        if len(native_tables) > 1:
            # one item per table read by the native query
            for qualified in native_tables:
                schema, _, name = qualified.partition(".")
                items.append(
                    _Item(
                        cache_key=f"{node.id}#{qualified}",
                        node_id=node.id,
                        schema_name=schema,
                        object_name=name,
                        raw_kind="native_query",
                    )
                )
        elif source:
            items.append(
                _Item(
                    cache_key=node.id,
                    node_id=node.id,
                    schema_name=source.get("schema", ""),
                    object_name=source.get("object", ""),
                    raw_kind=source.get("raw_kind", ""),
                )
            )
    return items


def _candidate_ids(graph: LineageGraph) -> list[str]:
    return sorted(
        node_id
        for node_id, node in graph.nodes.items()
        if node.node_type in _SQL_TYPES
    )


def _qualified(node_id: str) -> str:
    return node_id.split(":", 1)[1]


def _exact_match(graph: LineageGraph, schema: str, name: str) -> str | None:
    for node_type in _SQL_TYPES:
        node_id = Node.make_id(node_type, schema, name)
        if node_id in graph.nodes:
            return node_id
    return None


def _config_rule_match(
    graph: LineageGraph, config: Config, model_key: str, name: str
) -> str | None:
    schemas = [
        mapping.schema_name
        for mapping in config.schema_mappings
        if normalize_identifier(mapping.model) == model_key
    ]
    for schema in schemas:
        match = _exact_match(graph, schema, name)
        if match is not None:
            return match
    return None


def _fuzzy_candidates(
    candidates: list[str], schema: str, name: str
) -> list[tuple[str, float]]:
    needle = f"{schema}.{name}" if schema else name
    scored = [
        (candidate, SequenceMatcher(None, needle, _qualified(candidate)).ratio())
        for candidate in candidates
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def _apply(
    graph: LineageGraph, item: _Item, target_id: str, method: str, result: ResolutionResult
) -> None:
    graph.add_edge(
        Edge(
            source_id=target_id,
            target_id=item.node_id,
            edge_type=EdgeType.FEEDS,
            evidence=f"linker: {method} ({item.schema_name}.{item.object_name})",
        )
    )
    result.count(method)


def link_graph(
    graph: LineageGraph,
    config: Config,
    cache: LineageCache,
    interactive_mode: bool,
) -> tuple[ResolutionResult, list[ParseWarning]]:
    """Run the resolution ladder (cache -> exact -> config rule -> fuzzy ->
    interactive) for every Power BI table source; create feeds edges and
    return (result, warnings). Deterministic: items processed in sorted order.
    """
    result = ResolutionResult()
    warnings: list[ParseWarning] = list(cache.warnings)

    dropped = cache.prune_invalid(graph)
    for key in dropped:
        warnings.append(
            ParseWarning(
                file=str(cache.path),
                message=f"cache entry {key!r} pointed at a vanished node; will re-resolve",
                category="cache_pruned",
            )
        )

    candidates = _candidate_ids(graph)
    items = _collect_items(graph)
    pending_interactive: list[tuple[_Item, list[tuple[str, float]]]] = []

    for item in items:
        node = graph.nodes[item.node_id]

        cached = cache.get(item.cache_key)
        if cached is not None:
            if cached.target is not None:
                _apply(graph, item, cached.target, "cache", result)
            else:
                node.metadata["external_source" if cached.method == "external" else "skipped"] = True
                result.count("cache")
            continue

        exact = _exact_match(graph, item.schema_name, item.object_name)
        if exact is not None:
            _apply(graph, item, exact, "exact", result)
            continue

        rule = _config_rule_match(graph, config, node.schema_name, item.object_name)
        if rule is not None:
            _apply(graph, item, rule, "config_rule", result)
            continue

        scored = _fuzzy_candidates(candidates, item.schema_name, item.object_name)
        best_score = scored[0][1] if scored else 0.0
        if best_score >= FUZZY_AUTO_ACCEPT:
            _apply(graph, item, scored[0][0], "fuzzy", result)
            warnings.append(
                ParseWarning(
                    file=node.source_file,
                    message=(
                        f"{item.schema_name}.{item.object_name} fuzzy-matched to "
                        f"{scored[0][0]} ({best_score:.2f})"
                    ),
                    category="fuzzy_auto",
                )
            )
            continue

        if interactive_mode and best_score >= FUZZY_AMBIGUOUS:
            pending_interactive.append((item, scored))
            continue

        node.metadata["unresolved"] = True
        result.unresolved.append(item.cache_key)

    if pending_interactive:
        by_model: dict[str, list[tuple[_Item, list[tuple[str, float]]]]] = {}
        for item, scored in pending_interactive:
            model = graph.nodes[item.node_id].schema_name
            by_model.setdefault(model, []).append((item, scored))
        for model in sorted(by_model):
            group = by_model[model]
            interactive.print_group_header(model, len(group))
            for item, scored in group:
                node = graph.nodes[item.node_id]
                entry = interactive.prompt_resolution(
                    node, f"{item.schema_name}.{item.object_name}", scored
                )
                cache.put(item.cache_key, entry)  # immediately — crash-safe
                if entry.target is not None:
                    _apply(graph, item, entry.target, "interactive", result)
                elif entry.method == "external":
                    node.metadata["external_source"] = True
                    result.count("external")
                else:
                    node.metadata["unresolved"] = True
                    result.unresolved.append(item.cache_key)

    result.unresolved.sort()
    return result, warnings
