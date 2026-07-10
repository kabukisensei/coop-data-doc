"""Review-findings composition (issue #38).

Loads ``coop-sql-review`` / ``coop-dax-review`` ``--format json`` envelopes
(explicit build inputs — the offline hard rule is untouched) and joins their
findings onto the documented estate by the same ``normalize_identifier``
identity the graph uses. Everything here is a **renderer-layer** concern:
``graph.json`` / ``manifest.json`` never change, findings never affect exit
codes, and a run without review files is byte-identical to one before this
module existed.

Tolerant by design, silent about nothing (hard rule 4's spirit):

- an unreadable / non-JSON / wrong-shape file degrades to a
  ``reviews_unreadable`` warning and is skipped — never a crash;
- an unknown-but-integer ``schema_version`` gets a ``reviews_schema_mismatch``
  warning and the join is still attempted (the consumed fields are stable);
- findings that match no documented object are never dropped — they land in
  the Findings page's "Not matched to a documented object" section.

Pure module: no print/exit; problems are returned as ``ParseWarning`` values.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from coop_data_doc.config import ParseWarning
from coop_data_doc.graph.model import LineageGraph, Node, NodeType, normalize_identifier

SQL_REVIEW_TOOL = "coop-sql-review"
DAX_REVIEW_TOOL = "coop-dax-review"
# The envelope schema_version each tool currently emits. A file with a
# different (integer) version still joins — the fields consumed here
# (rule_id / severity / message / file / line / object / model) are the stable
# core — but it is flagged so a human knows to eyeball the result.
KNOWN_SCHEMA_VERSIONS = {SQL_REVIEW_TOOL: 4, DAX_REVIEW_TOOL: 3}

# SQL findings key objects as "schema.name"; they may only ever match nodes of
# the SQL types (never pbi_table/measure nodes that happen to share a name).
_SQL_NODE_TYPES = frozenset(
    {
        NodeType.BRONZE_TABLE,
        NodeType.SILVER_TABLE,
        NodeType.GOLD_TABLE,
        NodeType.VIEW,
        NodeType.STORED_PROC,
    }
)

_SEVERITY_ORDER = ("error", "warning", "info")


def severity_rank(severity: str) -> int:
    """Sort rank for a finding severity: error < warning < info < anything else."""
    try:
        return _SEVERITY_ORDER.index(severity)
    except ValueError:
        return len(_SEVERITY_ORDER)


@dataclass(frozen=True)
class ReviewFinding:
    """One linter finding, reduced to the minimal pinned fields the join and
    the renderer consume. Additive linter-side fields are ignored, so an
    upstream release can't break the join."""

    tool: str
    rule_id: str
    severity: str
    message: str
    file: str
    line: int
    object: str
    model: str = ""  # coop-dax-review only

    @property
    def location(self) -> str:
        """``file:line`` for display; just ``file`` for file-level findings (line 0)."""
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file


@dataclass
class ReviewEnvelope:
    """One successfully-loaded review file: its tool, findings, and the
    (deliberately unjoined) agent-review item count."""

    tool: str
    path: str  # the path as the user supplied it (for messages)
    findings: list[ReviewFinding] = field(default_factory=list)
    agent_review_count: int = 0


@dataclass
class ReviewJoin:
    """Findings joined onto graph nodes, plus everything that didn't match."""

    # node id -> its findings, sorted severity-rank then rule then line
    by_node: dict[str, list[ReviewFinding]] = field(default_factory=dict)
    # findings that matched no documented object — listed, never dropped
    unmatched: list[ReviewFinding] = field(default_factory=list)
    # tool -> severity -> count over ALL loaded findings (each finding once,
    # even when attached to several colliding nodes)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    # tool -> number of agent_review items (counted only; not joined in v1)
    agent_review_counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total loaded findings (matched + unmatched, each counted once)."""
        return sum(n for by_sev in self.counts.values() for n in by_sev.values())


def _warn(path: str, message: str, category: str = "reviews_unreadable") -> ParseWarning:
    return ParseWarning(file=path, message=message, category=category)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def load_review_file(path: Path, display: str) -> tuple[ReviewEnvelope | None, list[ParseWarning]]:
    """Load one review envelope tolerantly.

    Returns ``(envelope, warnings)``; ``envelope`` is ``None`` when the file
    couldn't be used at all (missing / non-JSON / unknown tool / wrong shape),
    in which case a ``reviews_unreadable`` warning names the file. A readable
    file with an unexpected integer ``schema_version`` loads anyway with a
    ``reviews_schema_mismatch`` warning.
    """
    warnings: list[ParseWarning] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [_warn(display, f"could not read review file: {exc}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [_warn(display, f"not valid JSON ({exc}) — is this a --format json report?")]
    if not isinstance(data, dict):
        return None, [_warn(display, "not a JSON object — expected a coop-*-review envelope")]
    tool = data.get("tool")
    if tool not in KNOWN_SCHEMA_VERSIONS:
        return None, [
            _warn(
                display,
                f"unknown tool {tool!r} — expected a {SQL_REVIEW_TOOL} or "
                f"{DAX_REVIEW_TOOL} --format json envelope",
            )
        ]
    schema_version = data.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        return None, [_warn(display, f"schema_version missing or not an integer ({schema_version!r})")]
    if schema_version != KNOWN_SCHEMA_VERSIONS[tool]:
        warnings.append(
            _warn(
                display,
                f"{tool} schema_version {schema_version} (this coop-data-doc knows "
                f"{KNOWN_SCHEMA_VERSIONS[tool]}); attempting the join anyway",
                category="reviews_schema_mismatch",
            )
        )
    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        return None, warnings + [_warn(display, "no findings list in the envelope")]
    findings: list[ReviewFinding] = []
    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            # skipped, never silently (hard rule 4)
            warnings.append(_warn(display, f"finding #{index} is not a JSON object — skipped"))
            continue
        findings.append(
            ReviewFinding(
                tool=tool,
                rule_id=_str(item.get("rule_id")),
                severity=_str(item.get("severity")),
                message=_str(item.get("message")),
                file=_str(item.get("file")),
                line=_int(item.get("line")),
                object=_str(item.get("object")),
                model=_str(item.get("model")),
            )
        )
    agent_review = data.get("agent_review")
    agent_count = len(agent_review) if isinstance(agent_review, list) else 0
    return ReviewEnvelope(
        tool=tool, path=display, findings=findings, agent_review_count=agent_count
    ), warnings


def load_review_files(
    inputs: list[tuple[Path, str]],
) -> tuple[list[ReviewEnvelope], list[ParseWarning]]:
    """Load every ``(path, display)`` input in order, collecting warnings.
    Unusable files are skipped (with a warning); the rest still load."""
    envelopes: list[ReviewEnvelope] = []
    warnings: list[ParseWarning] = []
    for path, display in inputs:
        envelope, file_warnings = load_review_file(path, display)
        warnings += file_warnings
        if envelope is not None:
            envelopes.append(envelope)
    return envelopes, warnings


def _finding_sort_key(finding: ReviewFinding) -> tuple:
    return (
        severity_rank(finding.severity),
        finding.severity,
        finding.rule_id,
        finding.line,
        finding.message,
        finding.file,
        finding.tool,
    )


def _sql_targets(graph: LineageGraph, index: dict[str, list[str]], finding: ReviewFinding) -> list[str]:
    """Node ids a SQL finding attaches to: every SQL-typed node whose
    ``schema.name`` normalizes equal to the finding's ``object`` — ALL of them
    when several types collide (deterministic, never guess one)."""
    key = normalize_identifier(finding.object)
    if not key:
        return []
    return index.get(key, [])


def _dax_target(graph: LineageGraph, finding: ReviewFinding) -> str | None:
    """The node id a DAX finding attaches to, or None.

    ``measure:model.object`` is tried first, then ``pbi_table:model.object``;
    a model-level finding (``object`` empty or equal to the model) attaches to
    ``semantic_model:model``.
    """
    model_key = normalize_identifier(finding.model)
    if not model_key:
        return None
    object_key = normalize_identifier(finding.object)
    if not object_key or object_key == model_key:
        model_id = Node.make_id(NodeType.SEMANTIC_MODEL, "", finding.model)
        return model_id if model_id in graph.nodes else None
    for node_type in (NodeType.MEASURE, NodeType.PBI_TABLE):
        candidate = Node.make_id(node_type, finding.model, finding.object)
        if candidate in graph.nodes:
            return candidate
    return None


def join_reviews(graph: LineageGraph, envelopes: list[ReviewEnvelope]) -> ReviewJoin:
    """Join every loaded finding onto the graph. Deterministic: node iteration,
    per-node finding lists, and the unmatched list are all sorted."""
    # schema.name -> sorted SQL-typed node ids (the identity the linker uses)
    sql_index: dict[str, list[str]] = {}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if node.node_type not in _SQL_NODE_TYPES:
            continue
        qualified = f"{node.schema_name}.{node.name}" if node.schema_name else node.name
        sql_index.setdefault(normalize_identifier(qualified), []).append(node_id)

    join = ReviewJoin()
    for envelope in envelopes:
        counts = join.counts.setdefault(envelope.tool, {})
        join.agent_review_counts[envelope.tool] = (
            join.agent_review_counts.get(envelope.tool, 0) + envelope.agent_review_count
        )
        for finding in envelope.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
            if envelope.tool == SQL_REVIEW_TOOL:
                targets = _sql_targets(graph, sql_index, finding)
            else:
                target = _dax_target(graph, finding)
                targets = [target] if target is not None else []
            if not targets:
                join.unmatched.append(finding)
                continue
            for node_id in targets:
                join.by_node.setdefault(node_id, []).append(finding)

    for node_id in join.by_node:
        join.by_node[node_id].sort(key=_finding_sort_key)
    join.unmatched.sort(key=lambda f: (f.tool, *_finding_sort_key(f), f.object))
    return join
