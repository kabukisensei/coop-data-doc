import re
from pathlib import Path

import coop_data_doc
from coop_data_doc.config import ParseWarning
from coop_data_doc.diagnostics import _SEVERITY, Diagnostics, severity_of


def test_severity_classification():
    assert severity_of("tmdl_parse") == "error"
    assert severity_of("dynamic_sql") == "warning"
    assert severity_of("layer_unclassified") == "info"
    assert severity_of("totally_unknown_category") == "warning"  # default
    # issue #16: missing-data categories are error (fail the CI gate); newly-classified
    # advisory categories are warning (previously fell to the default).
    assert severity_of("encoding_unreadable") == "error"
    assert severity_of("proc_body_not_found") == "error"
    assert severity_of("ambiguous_visual_binding") == "warning"
    assert severity_of("file_unreadable") == "warning"
    assert severity_of("interactive_unavailable") == "warning"
    # issue #45: an entity that matches no documented table is a heads-up, never
    # a missing edge to a known object — tolerated by --strict (not in STRICT_CATEGORIES)
    assert severity_of("unmatched_visual_entity") == "warning"


def test_every_emitted_category_is_registered():
    """The _SEVERITY map is 'the one place that classifies every warning'. Guard
    it: every ``category="..."`` literal in the source (plus the reviews._warn
    default parameter) must be an EXPLICIT key of _SEVERITY, so a new or typo'd
    category is a test failure instead of a silent fall-through to the warning
    default — which would mask a category that should be error-severity."""
    src = Path(coop_data_doc.__file__).parent
    emitted: set[str] = set()
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        emitted |= set(re.findall(r"""category\s*=\s*["']([a-z_]+)["']""", text))
    # reviews._warn defaults category to reviews_unreadable (not a literal at a call site)
    emitted.add("reviews_unreadable")
    unregistered = sorted(c for c in emitted if c not in _SEVERITY)
    assert unregistered == [], f"unregistered warning categories: {unregistered}"


def test_diagnostics_counts_and_console():
    d = Diagnostics(
        warnings=[
            ParseWarning(file="a.sql", message="dyn", category="dynamic_sql"),
            ParseWarning(file="b.tmdl", message="boom", category="tmdl_parse"),
            ParseWarning(file="c.sql", message="star", category="select_star_view"),
        ],
        unresolved=["pbi_table:m.x"],
    )
    sev = d.severity_counts()
    assert sev == {"error": 1, "warning": 2, "info": 1}  # unresolved_reference=warning
    assert d.total == 4
    lines = "\n".join(d.console_lines())
    assert "1 error" in lines and "2 warnings" in lines and "1 info" in lines
    assert "tmdl_parse" in lines


def test_diagnostics_markdown_and_json():
    d = Diagnostics(
        warnings=[ParseWarning(file="a.sql", message="dyn SQL", category="dynamic_sql")],
        unresolved=[],
    )
    md = d.to_markdown("My Estate")
    assert "# Diagnostics — My Estate" in md
    assert "dynamic_sql" in md and "dyn SQL" in md
    blob = d.to_json()
    assert blob["severity_counts"]["warning"] == 1
    assert blob["issues"][0]["category"] == "dynamic_sql"


def test_markdown_escapes_pipes_in_cells():
    """A literal '|' in a file/message cell (e.g. a Power BI object/page name)
    must be escaped so it can't break the rendered Markdown table row."""
    d = Diagnostics(
        warnings=[
            ParseWarning(
                file="page 'A|B'",
                message="unparseable visual config on page 'A|B'",
                category="pbir_parse",
            )
        ],
        unresolved=[],
    )
    md = d.to_markdown("Estate")
    row = next(line for line in md.splitlines() if line.startswith("| page"))
    # exactly two cells -> three '|' separators after escaping the literals
    assert row.count("|") - row.count("\\|") == 3
    assert "\\|" in row  # the literal pipes were escaped


def test_no_issues():
    d = Diagnostics(warnings=[], unresolved=[])
    assert d.console_lines() == ["No issues found."]
    assert "No issues found." in d.to_markdown("X")
    assert d.to_json()["issues"] == []
