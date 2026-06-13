"""Shared SQL parsing helpers (Module 2).

sqlglot (dialect tsql) does the heavy lifting; everything here exists to
feed it clean batches and to provide the regex fallback layer for the
T-SQL constructs it can't parse (cursors, WHILE blocks, etc.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from coop_data_doc.graph.model import normalize_identifier

GO_RE = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE | re.MULTILINE)
PROC_HEADER_RE = re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+([\w\[\].]+)", re.IGNORECASE)
DYNAMIC_SQL_RE = re.compile(r"\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(", re.IGNORECASE)
EXEC_RE = re.compile(r"^\s*EXEC(?:UTE)?\s+([\w\[\].]+)", re.IGNORECASE)

# lines that only drive cursor mechanics; their identifiers are cursors,
# not tables, so they must never reach the regex table extractor
_CURSOR_LINE_RE = re.compile(r"^\s*(?:FETCH|OPEN|CLOSE|DEALLOCATE)\b.*$", re.IGNORECASE | re.MULTILINE)
_BEGIN_END_LINE_RE = re.compile(r"^\s*(?:BEGIN|END)\s*;?\s*$", re.IGNORECASE | re.MULTILINE)

_WRITE_RX = [
    re.compile(r"\bINSERT\s+INTO\s+([#@\w\[\].]+)", re.IGNORECASE),
    re.compile(r"\bMERGE\s+(?:INTO\s+)?([#@\w\[\].]+)", re.IGNORECASE),
    re.compile(r"\bUPDATE\s+([#@\w\[\].]+)", re.IGNORECASE),
    re.compile(r"\bINTO\s+([#@\w\[\].]+)", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\s+([#@\w\[\].]+)", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\s+([#@\w\[\].]+)", re.IGNORECASE),
]
_READ_RX = [
    re.compile(r"\bFROM\s+([#@\w\[\].]+)", re.IGNORECASE),
    re.compile(r"\bJOIN\s+([#@\w\[\].]+)", re.IGNORECASE),
    re.compile(r"\bUSING\s+([#@\w\[\].]+)", re.IGNORECASE),
]
_CTE_RX = re.compile(r"\bWITH\s+([\w\[\]]+)\s+AS\s*\(|,\s*([\w\[\]]+)\s+AS\s*\(", re.IGNORECASE)
_ALIAS_RX = re.compile(r"\b(?:FROM|JOIN)\s+([\w\[\].]+)\s+(?:AS\s+)?([\w\[\]]+)", re.IGNORECASE)
_KEYWORDS = frozenset(
    "on where inner left right full outer cross join group order set when as with select".split()
)


def strip_bom(text: str) -> str:
    """Drop a UTF-8 BOM if present."""
    return text.lstrip("﻿")


def split_batches(sql_text: str) -> list[str]:
    """Split a script on GO separators into trimmed, non-empty batches."""
    return [batch.strip() for batch in GO_RE.split(strip_bom(sql_text)) if batch.strip()]


def parse_batch(batch: str, dialect: str = "tsql") -> list[exp.Expression]:
    """sqlglot.parse with errors ignored; returns [] instead of raising."""
    try:
        parsed = sqlglot.parse(batch, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return []
    return [expression for expression in parsed if expression is not None]


def scrub(sql: str, *, strip_strings: bool) -> str:
    """Remove comments (and optionally string-literal contents) via a scanner."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            out.append("''" if strip_strings else sql[i:end])
            i = end
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    """Split on ';' outside string literals and comments."""
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            buf.append(sql[i:end])
            i = end
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def is_temp(name: str) -> bool:
    """True for #temp tables and @table variables (by raw name)."""
    return name.startswith("#") or name.startswith("@")


def is_temp_table(table: exp.Table) -> bool:
    """True for #temp tables and @table variables.

    sqlglot's tsql parser strips the '#' prefix and instead flags the
    identifier with temporary=True, so checking the name alone is not enough.
    """
    if is_temp(table.name):
        return True
    ident = table.this
    if isinstance(ident, exp.Identifier) and ident.args.get("temporary"):
        return True
    return False


def qualify(raw: str) -> tuple[str, str]:
    """'[dbo].[Foo]' -> ('dbo', 'foo'); unqualified names default to dbo."""
    cleaned = normalize_identifier(raw)
    if "." in cleaned:
        schema, _, name = cleaned.rpartition(".")
        schema = schema.rpartition(".")[2]  # drop db part of db.schema.name
        return (schema or "dbo", name)
    return ("dbo", cleaned)


def table_parts(table: exp.Table) -> tuple[str, str]:
    """(schema, name) for a sqlglot Table, lowercased, defaulting to dbo."""
    schema = normalize_identifier(table.text("db")) or "dbo"
    return (schema, normalize_identifier(table.name))


def cte_names(expression: exp.Expression) -> set[str]:
    """Normalized aliases of every CTE under an expression."""
    return {normalize_identifier(cte.alias_or_name) for cte in expression.find_all(exp.CTE)}


def collect_source_tables(expression: exp.Expression) -> set[tuple[str, str]]:
    """All real tables under an expression, excluding CTE aliases, temp
    tables, table variables, and table-valued functions."""
    ctes = cte_names(expression)
    found: set[tuple[str, str]] = set()
    for table in expression.find_all(exp.Table):
        if is_temp_table(table):
            continue
        if isinstance(table.this, exp.Func):
            continue
        schema, name = table_parts(table)
        if not table.text("db") and name in ctes:
            continue
        found.add((schema, name))
    return found


@dataclass
class StatementLineage:
    """Tables one statement writes to and reads from."""

    writes: set[tuple[str, str]] = field(default_factory=set)
    reads: set[tuple[str, str]] = field(default_factory=set)


def regex_extract(statement: str) -> StatementLineage:
    """Fallback lineage extraction for statements sqlglot can't parse.

    Operates on comment- and string-stripped text; cursor mechanics lines
    and standalone BEGIN/END lines are dropped first.
    """
    text = scrub(statement, strip_strings=True)
    text = _CURSOR_LINE_RE.sub("", text)
    text = _BEGIN_END_LINE_RE.sub("", text)

    ctes = {normalize_identifier(g) for m in _CTE_RX.finditer(text) for g in m.groups() if g}
    aliases: dict[str, tuple[str, str]] = {}
    for match in _ALIAS_RX.finditer(text):
        alias = normalize_identifier(match.group(2))
        if alias not in _KEYWORDS:
            aliases[alias] = qualify(match.group(1))

    lineage = StatementLineage()
    for rx in _WRITE_RX:
        for match in rx.finditer(text):
            raw = match.group(1)
            if is_temp(normalize_identifier(raw)) or is_temp(raw):
                continue
            schema, name = qualify(raw)
            if name in ctes:
                continue
            if "." not in normalize_identifier(raw) and name in aliases:
                schema, name = aliases[name]
            lineage.writes.add((schema, name))
    for rx in _READ_RX:
        for match in rx.finditer(text):
            raw = match.group(1)
            if is_temp(normalize_identifier(raw)) or is_temp(raw):
                continue
            schema, name = qualify(raw)
            if name in ctes or name in aliases and "." not in normalize_identifier(raw):
                continue
            lineage.reads.add((schema, name))
    lineage.reads -= lineage.writes
    return lineage
