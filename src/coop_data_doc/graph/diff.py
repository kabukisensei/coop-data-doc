from dataclasses import dataclass, field

from coop_data_doc.graph.model import Edge, LineageGraph, Node


@dataclass
class GraphDiff:
    added_nodes: list[Node] = field(default_factory=list)
    removed_nodes: list[Node] = field(default_factory=list)
    changed_nodes: list[Node] = field(default_factory=list)
    added_edges: list[Edge] = field(default_factory=list)
    removed_edges: list[Edge] = field(default_factory=list)


def _nodes_equal(a: Node, b: Node) -> bool:
    if a.name != b.name or a.node_type != b.node_type or a.schema_name != b.schema_name:
        return False
    if a.source_file != b.source_file:
        return False
    if a.metadata.get("trust") != b.metadata.get("trust"):
        return False

    # compare columns
    if len(a.columns) != len(b.columns):
        return False

    for c_a, c_b in zip(a.columns, b.columns):
        if (
            c_a.name != c_b.name
            or c_a.data_type != c_b.data_type
            or c_a.nullable != c_b.nullable
            or set(c_a.constraints) != set(c_b.constraints)
        ):
            return False

    return True


def impact_seeds(
    old: LineageGraph,
    new: LineageGraph,
    files: tuple[str, ...] = (),
) -> tuple[set[str], set[str]]:
    """Return ``(current, baseline)`` seeds for a graph impact report.

    Node additions and changes are traversed in the current graph, while
    removals and removed edges are traversed in the baseline graph.  Edge
    endpoints are selected in data-flow order, rather than authored order.
    Explicit source-file seeds retain their historical current-graph behavior;
    a path found only in the baseline is a deletion and is traversed there.
    """
    diff = diff_graphs(old, new)
    current_seeds: set[str] = set()
    baseline_seeds: set[str] = set()

    if files:
        requested = set(files)
        current_by_file: dict[str, set[str]] = {}
        baseline_by_file: dict[str, set[str]] = {}
        for node in new.nodes.values():
            if node.source_file in requested:
                current_by_file.setdefault(node.source_file, set()).add(node.id)
        for node in old.nodes.values():
            if node.source_file in requested:
                baseline_by_file.setdefault(node.source_file, set()).add(node.id)
        for path in requested:
            current_seeds.update(current_by_file.get(path, set()))
            for node_id in baseline_by_file.get(path, set()):
                if node_id in new.nodes:
                    # The object survived (possibly under a new source path),
                    # so use its current topology and metadata.
                    current_seeds.add(node_id)
                else:
                    # A file may still exist while one of several objects it
                    # used to define was removed. Preserve that deletion's
                    # former blast radius instead of treating the whole file
                    # as current-only.
                    baseline_seeds.add(node_id)
        return current_seeds, baseline_seeds

    current_seeds.update(node.id for node in diff.added_nodes)
    current_seeds.update(node.id for node in diff.changed_nodes)
    baseline_seeds.update(node.id for node in diff.removed_nodes)
    current_seeds.update(edge.flow()[0] for edge in diff.added_edges)
    baseline_seeds.update(edge.flow()[0] for edge in diff.removed_edges)
    return current_seeds, baseline_seeds


def impact_map(
    old: LineageGraph,
    new: LineageGraph,
    files: tuple[str, ...] = (),
) -> tuple[dict[str, list[str]], dict[str, LineageGraph]]:
    """Compute deterministic downstream impact and the graph used per seed.

    A seed present in multiple diff categories gets the union of all relevant
    traversals.  The second return value lets renderers resolve removed and
    baseline-only nodes without assuming every id exists in the current graph.
    """
    current_seeds, baseline_seeds = impact_seeds(old, new, files)
    impacts: dict[str, set[str]] = {}
    seed_graphs: dict[str, LineageGraph] = {}
    for node_id in current_seeds:
        impacts.setdefault(node_id, set()).update(new.downstream(node_id))
        seed_graphs[node_id] = new
    for node_id in baseline_seeds:
        impacts.setdefault(node_id, set()).update(old.downstream(node_id))
        # Prefer the current graph when an id is present in both graphs. This
        # keeps changed/edge-added seeds rendered using current metadata.
        seed_graphs.setdefault(node_id, old)
    return (
        {node_id: sorted(downstream) for node_id, downstream in sorted(impacts.items())},
        seed_graphs,
    )


def diff_graphs(old: LineageGraph, new: LineageGraph) -> GraphDiff:
    diff = GraphDiff()

    # Node diffs
    old_nodes = set(old.nodes.keys())
    new_nodes = set(new.nodes.keys())

    for nid in new_nodes - old_nodes:
        diff.added_nodes.append(new.nodes[nid])

    for nid in old_nodes - new_nodes:
        diff.removed_nodes.append(old.nodes[nid])

    for nid in old_nodes & new_nodes:
        if not _nodes_equal(old.nodes[nid], new.nodes[nid]):
            diff.changed_nodes.append(new.nodes[nid])

    # Edge diffs
    old_edges = {e.key(): e for e in old.edges}
    new_edges = {e.key(): e for e in new.edges}

    for ekey in set(new_edges.keys()) - set(old_edges.keys()):
        diff.added_edges.append(new_edges[ekey])

    for ekey in set(old_edges.keys()) - set(new_edges.keys()):
        diff.removed_edges.append(old_edges[ekey])

    # Sort for determinism
    diff.added_nodes.sort(key=lambda n: n.id)
    diff.removed_nodes.sort(key=lambda n: n.id)
    diff.changed_nodes.sort(key=lambda n: n.id)
    diff.added_edges.sort(key=lambda e: e.key())
    diff.removed_edges.sort(key=lambda e: e.key())

    return diff
