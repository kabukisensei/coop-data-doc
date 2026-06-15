"""Power Query (M) partition-source extraction (Module 3).

Given the M expression of a semantic-model partition, determine which
database object it loads — deterministically, with ordered regex patterns.
Anything unrecognizable is reported as unresolved, never guessed.
"""

from __future__ import annotations

import re

from pydantic import BaseModel


class SourceRef(BaseModel):
    """A recognized partition source: schema, object, and how it was found."""

    schema_name: str
    object_name: str
    raw_kind: str  # "sql_database" | "native_query" | "lakehouse" | "static" | "fallback"


_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_NATIVE_QUERY_RE = re.compile(r'Value\.NativeQuery\s*\([^,]+,\s*"((?:[^"]|"")*)"', re.S)
_LAKEHOUSE_RE = re.compile(r"Lakehouse\.Contents\s*\(|Fabric\.|\.Warehouse\s*\(")
_NAME_NAV_RE = re.compile(r'\b(?:Name|Id)\s*=\s*"([^"]+)"')

# `let` variable bindings: IDENT = "literal" — used to resolve indirected
# navigation like  Source{[Schema=LocalSchema, Item=LocalTable]}[Data]
_BINDING_RE = re.compile(r'\b([A-Za-z_]\w*)\s*=\s*"([^"]*)"')
# a Schema=/Item= navigation token whose value is either a quoted literal
# or an identifier (resolved against the bindings above)
_NAV_SCHEMA_RE = re.compile(r'\bSchema\s*=\s*(?:"([^"]+)"|([A-Za-z_]\w*))')
_NAV_ITEM_RE = re.compile(r'\bItem\s*=\s*(?:"([^"]+)"|([A-Za-z_]\w*))')
# inline/static tables (calculation, parameter, hand-built) have no DB source
_STATIC_RE = re.compile(r"Table\.FromRows|#table\b|Json\.Document")


def strip_m_comments(m_expression: str) -> str:
    # '//' inside string literals (URLs) is also stripped — an accepted
    # v1 edge case; it can only ever hide a source, never invent one
    """Remove // and /* */ comments from M code before pattern matching."""
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub(" ", m_expression))


def _resolve(match: re.Match | None, bindings: dict[str, str]) -> str | None:
    """A Schema=/Item= match resolves to its literal, or via a let binding."""
    if match is None:
        return None
    if match.group(1) is not None:  # quoted literal
        return match.group(1)
    return bindings.get(match.group(2))  # identifier -> bound literal (or None)


def extract_source(m_expression: str) -> tuple[SourceRef | None, list[str]]:
    """Return (source-ref-or-None, raw SQL strings from NativeQuery calls)."""
    text = strip_m_comments(m_expression)

    native_sql = [m.group(1).replace('""', '"') for m in _NATIVE_QUERY_RE.finditer(text)]
    if native_sql:
        return SourceRef(schema_name="", object_name="", raw_kind="native_query"), native_sql

    # Sql.Database navigation — works for quoted literals AND for the common
    # PBIP template that binds the schema/table to `let` variables first.
    bindings = {name: value for name, value in _BINDING_RE.findall(text)}
    schema = _resolve(_NAV_SCHEMA_RE.search(text), bindings)
    item = _resolve(_NAV_ITEM_RE.search(text), bindings)
    if schema and item:
        return (
            SourceRef(schema_name=schema.lower(), object_name=item.lower(), raw_kind="sql_database"),
            [],
        )

    if _LAKEHOUSE_RE.search(text):
        names = _NAME_NAV_RE.findall(text)
        if names:
            return (
                SourceRef(
                    schema_name=names[-2].lower() if len(names) >= 2 else "",
                    object_name=names[-1].lower(),
                    raw_kind="lakehouse",
                ),
                [],
            )

    # inline/static table (e.g. a DAX calculation or parameter table) — it has
    # no database source by design, so it's resolved, not "unresolved".
    if _STATIC_RE.search(text):
        return SourceRef(schema_name="", object_name="", raw_kind="static"), []

    return None, []
