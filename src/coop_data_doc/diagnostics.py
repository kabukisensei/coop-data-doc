"""Run diagnostics — one place that classifies every warning/unresolved
reference by severity and renders it three ways: a console summary, a
machine-readable ``diagnostics.json``, and a human ``diagnostics.md`` page
in the HTML portal.

Severity is advisory — nothing here is fatal to a build:
- error : a parse genuinely failed / data is missing
- warning: lineage is degraded or heuristic (worth a look)
- info  : expected/cosmetic (no action usually needed)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from coop_data_doc.config import ParseWarning

_SEVERITY: dict[str, str] = {
    # parse failures / missing data
    "tmdl_parse": "error",
    "bim_parse": "error",
    "pbir_parse": "error",
    "report_json_parse": "error",
    "pbix_unreadable": "error",
    "pbix_layout_parse": "error",
    "cache_invalid": "error",
    # degraded / heuristic lineage
    "dynamic_sql": "warning",
    "regex_fallback": "warning",
    "unresolved_partition_source": "warning",
    "unresolved_reference": "warning",
    "fuzzy_auto": "warning",
    "cache_pruned": "warning",
    "pbix_opaque_model": "warning",
    "symlink_escape": "warning",
    "file_too_large": "warning",
    # expected / cosmetic
    "layer_unclassified": "info",
    "select_star_view": "info",
    "unclassified_file": "info",
}
_DEFAULT_SEVERITY = "warning"
_SEVERITY_ORDER = ("error", "warning", "info")


def severity_of(category: str) -> str:
    return _SEVERITY.get(category, _DEFAULT_SEVERITY)


@dataclass
class Diagnostics:
    """Aggregated, severity-classified issues from one pipeline run."""

    warnings: list[ParseWarning] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def items(self) -> list[tuple[str, str, str, str]]:
        """Unified, sorted (severity, category, file, message) rows."""
        rows: list[tuple[str, str, str, str]] = []
        for w in self.warnings:
            rows.append((severity_of(w.category), w.category, w.file, w.message))
        for node_id in self.unresolved:
            rows.append(
                (
                    severity_of("unresolved_reference"),
                    "unresolved_reference",
                    node_id,
                    f"{node_id} could not be matched to a SQL object",
                )
            )
        rows.sort(key=lambda r: (_SEVERITY_ORDER.index(r[0]), r[1], r[2], r[3]))
        return rows

    def category_counts(self) -> Counter:
        counts: Counter = Counter()
        for severity, category, _file, _msg in self.items():
            counts[(severity, category)] += 1
        return counts

    def severity_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in _SEVERITY_ORDER}
        for severity, _c, _f, _m in self.items():
            counts[severity] += 1
        return counts

    @property
    def total(self) -> int:
        return len(self.items())

    # -- renderers ----------------------------------------------------------

    def console_lines(self) -> list[str]:
        """Lines for the terminal summary (grouped by severity)."""
        rows = self.items()
        if not rows:
            return ["No issues found."]
        sev = self.severity_counts()
        head = ", ".join(f"{sev[s]} {s}{'s' if sev[s] != 1 else ''}" for s in _SEVERITY_ORDER if sev[s])
        lines = [f"Diagnostics: {head}"]
        by_cat = self.category_counts()
        for severity in _SEVERITY_ORDER:
            cats = sorted(c for (s, c) in by_cat if s == severity)
            for category in cats:
                lines.append(f"  [{severity}] {category:30} {by_cat[(severity, category)]}")
        # top offending files (deterministic: by count desc, then filename)
        file_counts = Counter(f for _s, _c, f, _m in rows)
        top = sorted(file_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        if top:
            lines.append("  most affected:")
            lines.extend(f"    {f} ({n})" for f, n in top)
        return lines

    def to_json(self) -> dict:
        return {
            "severity_counts": self.severity_counts(),
            "issues": [
                {"severity": s, "category": c, "file": f, "message": m} for s, c, f, m in self.items()
            ],
        }

    def to_markdown(self, project_name: str) -> str:
        rows = self.items()
        out = [f"# Diagnostics — {project_name}", ""]
        if not rows:
            out.append("✅ No issues found.")
            out.append("")
            return "\n".join(out)
        sev = self.severity_counts()
        out.append("| Severity | Count |")
        out.append("| --- | --- |")
        for s in _SEVERITY_ORDER:
            if sev[s]:
                out.append(f"| {s} | {sev[s]} |")
        out.append("")
        out.append(
            "_error = a parse failed or data is missing · warning = lineage degraded or "
            "heuristic · info = expected/cosmetic._"
        )
        by_cat = self.category_counts()
        for severity in _SEVERITY_ORDER:
            cats = sorted(c for (s, c) in by_cat if s == severity)
            if not cats:
                continue
            out.append("")
            out.append(f"## {severity.capitalize()}")
            for category in cats:
                out.append("")
                out.append(f"### `{category}` ({by_cat[(severity, category)]})")
                out.append("")
                out.append("| File / Object | Detail |")
                out.append("| --- | --- |")
                for s, c, f, m in rows:
                    if s == severity and c == category:
                        out.append(f"| {f} | {m} |")
        out.append("")
        return "\n".join(out)
