from dataclasses import dataclass, field
from coop_data_doc.graph.model import LineageGraph, Node, Edge

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
        if (c_a.name != c_b.name or c_a.data_type != c_b.data_type or 
            c_a.nullable != c_b.nullable or set(c_a.constraints) != set(c_b.constraints)):
            return False
            
    return True

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
