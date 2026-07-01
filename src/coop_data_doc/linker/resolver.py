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
from coop_data_doc.linker.cache import LineageCache

FUZZY_AUTO_ACCEPT = 0.92
FUZZY_AMBIGUOUS = 0.6

# Node types a partition source can resolve against. Order matters: it is the
# precedence _exact_match tries when several objects share schema.name, so
# BRONZE_TABLE stays LAST (a curated view/gold/silver object wins over a raw
# landing table). assign_layers retypes user-declared bronze schemas before
# link_graph runs, so bronze must be here or those tables can never be linked.
_SQL_TYPES = (NodeType.VIEW, NodeType.GOLD_TABLE, NodeType.SILVER_TABLE, NodeType.BRONZE_TABLE)


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
    return sorted(node_id for node_id, node in graph.nodes.items() if node.node_type in _SQL_TYPES)


def _qualified(node_id: str) -> str:
    return node_id.split(":", 1)[1]


def _exact_match(graph: LineageGraph, schema: str, name: str) -> str | None:
    for node_type in _SQL_TYPES:
        node_id = Node.make_id(node_type, schema, name)
        if node_id in graph.nodes:
            return node_id
    return None


def _config_rule_match(graph: LineageGraph, config: Config, model_key: str, name: str) -> str | None:
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


def _fuzzy_candidates(candidates: list[str], schema: str, name: str) -> list[tuple[str, float]]:
    needle = f"{schema}.{name}" if schema else name
    scored = [
        (candidate, SequenceMatcher(None, needle, _qualified(candidate)).ratio()) for candidate in candidates
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def _apply(graph: LineageGraph, item: _Item, target_id: str, method: str, result: ResolutionResult) -> None:
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
    pending_out: list | None = None,
) -> tuple[ResolutionResult, list[ParseWarning]]:
    """Run the resolution ladder (cache -> exact -> config rule -> fuzzy ->
    interactive) for every Power BI table source; create feeds edges and
    return (result, warnings). Deterministic: items processed in sorted order.

    When ``pending_out`` is a list, the interactive step does NOT prompt: each
    ambiguous item (with its candidates) is appended to ``pending_out`` and left
    unresolved. That's how `coop-data-doc resolve` exposes the choices so the agent
    can present them and feed decisions back via `resolve-apply`.
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
        runner_up = scored[1][1] if len(scored) > 1 else 0.0
        # Auto-accept only an unambiguous winner: a dead tie with the runner-up
        # is positive proof the source can't distinguish the candidates, and
        # picking one alphabetically would be guessed lineage (hard rule 4).
        # Ties fall through to the ambiguous/interactive band below.
        if best_score >= FUZZY_AUTO_ACCEPT and runner_up < best_score:
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

    if pending_interactive and pending_out is not None:
        # Collect mode (the `resolve` command): record each ambiguous item + its
        # candidates for the agent to map, leave them unresolved, don't prompt.
        for item, scored in pending_interactive:
            node = graph.nodes[item.node_id]
            source = f"{item.schema_name}.{item.object_name}" if item.schema_name else item.object_name
            pending_out.append(
                {
                    "cache_key": item.cache_key,
                    "pbi_table": node.qualified_display,
                    "model": node.schema_name,
                    "source": source,
                    "candidates": [
                        {
                            "target": cid,
                            "name": graph.nodes[cid].qualified_display if cid in graph.nodes else cid,
                            "score": round(score, 4),
                        }
                        for cid, score in scored[:10]
                    ],
                }
            )
            node.metadata["unresolved"] = True
            result.unresolved.append(item.cache_key)
        result.unresolved.sort()
        return result, warnings

    if pending_interactive:
        by_model: dict[str, list[tuple[_Item, list[tuple[str, float]]]]] = {}
        for item, scored in pending_interactive:
            model = graph.nodes[item.node_id].schema_name
            by_model.setdefault(model, []).append((item, scored))
        handled: set[str] = set()
        try:
            for model in sorted(by_model):
                group = by_model[model]
                interactive.print_group_header(model, len(group))
                for item, scored in group:
                    node = graph.nodes[item.node_id]
                    entry = interactive.prompt_resolution(
                        node, f"{item.schema_name}.{item.object_name}", scored
                    )
                    handled.add(item.cache_key)
                    cache.put(item.cache_key, entry)  # immediately — crash-safe
                    if entry.target is not None:
                        _apply(graph, item, entry.target, "interactive", result)
                    elif entry.method == "external":
                        node.metadata["external_source"] = True
                        result.count("external")
                    else:
                        node.metadata["unresolved"] = True
                        result.unresolved.append(item.cache_key)
        except interactive.TerminalUnavailable as exc:
            # No usable terminal (e.g. launched by the coop agent / another program).
            # Don't crash the build: leave every still-pending ambiguous link
            # unresolved and warn, so the docs still generate and a human can map
            # them later by re-running in a terminal.
            remaining = 0
            for item, _scored in pending_interactive:
                if item.cache_key in handled:
                    continue
                graph.nodes[item.node_id].metadata["unresolved"] = True
                result.unresolved.append(item.cache_key)
                remaining += 1
            warnings.append(
                ParseWarning(
                    file="<interactive>",
                    message=(
                        f"no interactive terminal ({exc}); {remaining} ambiguous "
                        "cross-repo link(s) left unresolved — run `coop-data-doc setup` "
                        "or `coop-data-doc build` in a terminal to map them"
                    ),
                    category="interactive_unavailable",
                )
            )

    result.unresolved.sort()
    return result, warnings
