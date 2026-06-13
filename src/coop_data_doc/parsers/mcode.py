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
    raw_kind: str  # "sql_database" | "native_query" | "lakehouse" | "fallback"


_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_NATIVE_QUERY_RE = re.compile(r'Value\.NativeQuery\s*\([^,]+,\s*"((?:[^"]|"")*)"', re.S)
_SQL_DATABASE_RE = re.compile(r"Sql\.Databases?\s*\(")
_SCHEMA_ITEM_RE = re.compile(r'Schema\s*=\s*"([^"]+)"\s*,\s*Item\s*=\s*"([^"]+)"')
_LAKEHOUSE_RE = re.compile(r"Lakehouse\.Contents\s*\(|Fabric\.|\.Warehouse\s*\(")
_NAME_NAV_RE = re.compile(r'\b(?:Name|Id)\s*=\s*"([^"]+)"')
_FALLBACK_SCHEMA_RE = re.compile(r'Schema\s*=\s*"([^"]+)"')
_FALLBACK_ITEM_RE = re.compile(r'Item\s*=\s*"([^"]+)"')


def strip_m_comments(m_expression: str) -> str:
    # '//' inside string literals (URLs) is also stripped — an accepted
    # v1 edge case; it can only ever hide a source, never invent one
    """Remove // and /* */ comments from M code before pattern matching."""
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub(" ", m_expression))


def extract_source(m_expression: str) -> tuple[SourceRef | None, list[str]]:
    """Return (source-ref-or-None, raw SQL strings from NativeQuery calls)."""
    text = strip_m_comments(m_expression)

    native_sql = [m.group(1).replace('""', '"') for m in _NATIVE_QUERY_RE.finditer(text)]
    if native_sql:
        return SourceRef(schema_name="", object_name="", raw_kind="native_query"), native_sql

    if _SQL_DATABASE_RE.search(text):
        nav = _SCHEMA_ITEM_RE.search(text)
        if nav:
            return (
                SourceRef(
                    schema_name=nav.group(1).lower(),
                    object_name=nav.group(2).lower(),
                    raw_kind="sql_database",
                ),
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

    schema = _FALLBACK_SCHEMA_RE.search(text)
    item = _FALLBACK_ITEM_RE.search(text)
    if schema and item:
        return (
            SourceRef(
                schema_name=schema.group(1).lower(),
                object_name=item.group(1).lower(),
                raw_kind="fallback",
            ),
            [],
        )

    return None, []
