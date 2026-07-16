"""Shared SQL parsing helpers (Module 2).

sqlglot (dialect tsql) does the heavy lifting; everything here exists to
feed it clean batches and to provide the regex fallback layer for the
T-SQL constructs it can't parse (cursors, WHILE blocks, etc.).
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from coop_data_doc.graph.model import normalize_identifier

GO_RE = re.compile(r"^\s*GO\s*;?\s*$", re.IGNORECASE | re.MULTILINE)

# A (possibly multi-part) SQL identifier: each part is either a bracketed
# segment — which may contain spaces, e.g. `[Order Details]` — or a bare
# word, joined by dots. Using one pattern everywhere keeps the regex fallback
# from truncating a spaced, bracketed name at its first space (which used to
# leak a phantom table like `[my` -> `my`).
_IDENT = r"(?:\[[^\]]+\]|[#@\w]+)(?:\.(?:\[[^\]]+\]|[#@\w]+))*"

# Use the bracket-aware _IDENT so a spaced bracketed proc name like
# `[dbo].[My Proc]` is captured whole, not truncated at the first space.
PROC_HEADER_RE = re.compile(rf"\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+({_IDENT})", re.IGNORECASE)
DYNAMIC_SQL_RE = re.compile(r"\bsp_executesql\b|\bEXEC(?:UTE)?\s*\(", re.IGNORECASE)

# Capture the callee after EXEC, tolerating an optional `@var =` return-status
# assignment (EXEC @rc = dbo.Proc) so `@rc` isn't mistaken for the callee.
EXEC_RE = re.compile(rf"^\s*EXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?({_IDENT})", re.IGNORECASE)

# Same callee capture, but for an EXEC that starts a statement AFTER a leading
# conditional/block keyword ON THE SAME LINE — the very common single-line forms
# `IF @debug = 1 EXEC dbo.log`, `ELSE EXEC dbo.fallback`, `WHILE ... EXEC x`, and
# `IF ... BEGIN EXEC x END`. EXEC_RE (anchored to line start) misses these, so the
# references edge to the callee was silently lost. The prefix is one of: start /
# `;` / `ELSE` / `BEGIN` / an `IF`/`WHILE` condition (consumed non-greedily up to
# EXEC). `EXECUTE AS <ctx>` is NOT special-cased here — it captures the bare
# keyword `AS`, which the caller rejects via _EXEC_AS_KEYWORDS, same as EXEC_RE.
# `EXEC(...)` / sp_executesql (dynamic SQL) never reach this: the caller diverts
# those to a dynamic_sql warning first (DYNAMIC_SQL_RE), so requiring whitespace
# after EXEC(UTE) here is enough to keep the `EXEC(` form out.
EXEC_CALL_RE = re.compile(
    rf"(?:^|;|\bELSE\b|\bBEGIN\b|\b(?:IF|WHILE)\b[^\n;]*?)\s*\bEXEC(?:UTE)?\s+(?:@\w+\s*=\s*)?({_IDENT})",
    re.IGNORECASE,
)

# lines that only drive cursor mechanics; their identifiers are cursors,
# not tables, so they must never reach the regex table extractor
_CURSOR_LINE_RE = re.compile(r"^\s*(?:FETCH|OPEN|CLOSE|DEALLOCATE)\b.*$", re.IGNORECASE | re.MULTILINE)
_BEGIN_END_LINE_RE = re.compile(r"^\s*(?:BEGIN|END)\s*;?\s*$", re.IGNORECASE | re.MULTILINE)

_WRITE_RX = [
    re.compile(rf"\bINSERT\s+INTO\s+({_IDENT})", re.IGNORECASE),
    re.compile(rf"\bMERGE\s+(?:INTO\s+)?({_IDENT})", re.IGNORECASE),
    re.compile(rf"\bUPDATE\s+({_IDENT})", re.IGNORECASE),
    re.compile(rf"\bINTO\s+({_IDENT})", re.IGNORECASE),
    re.compile(rf"\bDELETE\s+FROM\s+({_IDENT})", re.IGNORECASE),
    re.compile(rf"\bTRUNCATE\s+TABLE\s+({_IDENT})", re.IGNORECASE),
]
_READ_RX = [
    re.compile(rf"\bFROM\s+({_IDENT})", re.IGNORECASE),
    re.compile(rf"\bJOIN\s+({_IDENT})", re.IGNORECASE),
    re.compile(rf"\bUSING\s+({_IDENT})", re.IGNORECASE),
    # Fabric DW zero-copy clone: `CREATE TABLE tgt AS CLONE OF src` — sqlglot degrades
    # it to an opaque Command, so the clone's real table->table source lineage would be
    # silently dropped without this. The target is captured by the CREATE TABLE fallback.
    re.compile(rf"\bCLONE\s+OF\s+({_IDENT})", re.IGNORECASE),
]
# A CTE alias: the name after WITH or a following comma, optionally bracketed
# (so it may contain spaces) and optionally followed by an explicit column list
# `(a, b)` before AS. Both alternatives are needed because a WITH block can
# define several CTEs (`WITH a AS (...), b AS (...)`).
_CTE_NAME = r"(\[[^\]]+\]|[\w]+)\s*(?:\([^)]*\))?"
_CTE_RX = re.compile(rf"\bWITH\s+{_CTE_NAME}\s+AS\s*\(|,\s*{_CTE_NAME}\s+AS\s*\(", re.IGNORECASE)
_ALIAS_RX = re.compile(rf"\b(?:FROM|JOIN)\s+({_IDENT})\s+(?:AS\s+)?(\[[^\]]+\]|[\w]+)", re.IGNORECASE)
_KEYWORDS = frozenset(
    "on where inner left right full outer cross join group order set when as with select".split()
)


def strip_bom(text: str) -> str:
    """Drop a UTF-8 BOM if present."""
    return text.lstrip("﻿")


def decode_text(data: bytes) -> str:
    """Decode file bytes tolerantly, BOM-aware, newlines normalized to LF.

    UTF-16/UTF-32 BOMs (SSMS's 'Save with Encoding' Unicode default is UTF-16)
    are honored; everything else decodes as utf-8-sig with replacement, so a
    file never raises here. Callers should treat NULs surviving in the result
    (BOM-less UTF-16, binary) as unreadable and warn — never stay silent.

    CRLF/lone-CR are normalized to LF: git's autocrlf checks sources out as
    CRLF on Windows, and carrying CRLF into `source_code` embeds it in
    rendered pages — breaking the cross-OS byte-identity guarantee (docs
    built on Windows read as stale on Linux CI, and vice versa).
    """
    # UTF-32-LE's BOM starts with UTF-16-LE's, so check the wider one first.
    if data.startswith(codecs.BOM_UTF32_LE) or data.startswith(codecs.BOM_UTF32_BE):
        text = data.decode("utf-32", errors="replace")
    elif data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_batches(sql_text: str) -> list[str]:
    """Split a script on GO separators into trimmed, non-empty batches."""
    return [batch.strip() for batch in GO_RE.split(strip_bom(sql_text)) if batch.strip()]


def parse_batch(
    batch: str,
    dialect: str = "tsql",
    *,
    error_level: sqlglot.ErrorLevel = sqlglot.ErrorLevel.IGNORE,
) -> list[exp.Expression]:
    """sqlglot.parse that returns [] instead of raising.

    ``error_level`` defaults to IGNORE (best-effort partial parse). Pass
    ``sqlglot.ErrorLevel.RAISE`` when a partial parse would be worse than no
    parse — e.g. a semicolon-less multi-statement proc chunk, which IGNORE
    silently mangles into one statement; RAISE turns it into [] so the caller
    can fall back to the (honestly flagged) regex layer instead.
    """
    try:
        parsed = sqlglot.parse(batch, read=dialect, error_level=error_level)
    except Exception:
        return []
    return [expression for expression in parsed if expression is not None]


def _block_comment_end(sql: str, start: int) -> int:
    """Index just past the `*/` that closes the block comment opening at
    ``start`` (which must point at its `/*`), or ``len(sql)`` if unterminated.

    T-SQL block comments NEST: ``/* outer /* inner */ still outer */`` is ONE
    comment through the FINAL ``*/``. A flat ``sql.find('*/')`` stops at the
    first ``*/`` and un-hides the code archived after it (a nested-comment
    INSERT/CREATE PROCEDURE leaking a phantom object — hard rule 4). Track a
    depth counter instead: +1 on each ``/*``, -1 on each ``*/``, done at depth
    0. Per documented T-SQL server semantics, quotes/`--` inside a block comment
    have no special meaning, so this scans characters without quote-awareness.
    """
    depth = 0
    i, n = start, len(sql)
    while i < n:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
        elif sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def scrub(sql: str, *, strip_strings: bool) -> str:
    """Remove comments (and optionally string-literal contents) via a scanner."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "[":
            # T-SQL bracket-quoted identifier (e.g. [Owner's Name]): a `'` inside
            # is part of the name, not a string-literal start, so copy through to
            # the matching ']' verbatim and never treat its contents as a string.
            j = sql.find("]", i + 1)
            end = n if j == -1 else j + 1
            out.append(sql[i:end])
            i = end
        elif ch == "'":
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
            # nesting-aware: skip through the FINAL matching */ (see _block_comment_end)
            i = _block_comment_end(sql, i)
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def blank_comments_and_strings(sql: str) -> str:
    """Length-preserving scrub: comments and string literals are overwritten
    with spaces (newlines kept), so every offset maps 1:1 back into the raw
    text. Used where a *position* found on clean text must be applied to the
    original — e.g. locating a proc body's top-level AS without parens inside
    comments/string defaults corrupting the depth count.
    """
    chars = list(sql)
    n = len(sql)

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if chars[k] != "\n":
                chars[k] = " "

    i = 0
    while i < n:
        ch = sql[i]
        if ch == "[":
            # bracket-quoted identifier: copy through (a `'` inside is part of
            # the name, not a string start), same as scrub/split_statements
            j = sql.find("]", i + 1)
            i = n if j == -1 else j + 1
        elif ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            blank(i, end)
            i = end
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            end = n if j == -1 else j
            blank(i, end)
            i = end
        elif sql.startswith("/*", i):
            # nesting-aware: blank through the FINAL matching */ (see _block_comment_end)
            end = _block_comment_end(sql, i)
            blank(i, end)
            i = end
        else:
            i += 1
    return "".join(chars)


def split_statements(sql: str) -> list[str]:
    """Split on ';' outside string literals and comments."""
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "[":
            # T-SQL bracket-quoted identifier (e.g. [Owner's Name]): a `'` inside
            # is part of the name, not a string-literal start. Copy through to the
            # matching ']' so a real `;` terminator after it is not swallowed.
            j = sql.find("]", i + 1)
            end = n if j == -1 else j + 1
            buf.append(sql[i:end])
            i = end
        elif ch == "'":
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
            # nesting-aware: preserve the WHOLE comment (through the final */) so a
            # `;` inside a nested comment can't split the statement (see
            # _block_comment_end)
            j = _block_comment_end(sql, i)
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


_QUOTE_JUNK = re.compile(r'[\[\]"`]')

# One identifier part: a [..]-bracketed segment, a ".."-double-quoted segment,
# or a bare run of non-dot characters. Splitting on this (rather than a plain
# str.split(".")) keeps a literal dot *inside* brackets/quotes in its own part,
# so `[my.proc]` stays one segment instead of becoming ('my', 'proc').
_IDENT_PART = re.compile(r'\[[^\]]*\]|"[^"]*"|[^.]+')


def _ident_parts(raw: str) -> list[str]:
    """Split a (possibly multi-part) identifier into its dot-separated parts,
    treating a dot inside brackets/double-quotes as part of the segment, not a
    separator. Quote/bracket characters are stripped from each returned part.
    """
    parts = [_QUOTE_JUNK.sub("", m.group(0)).strip() for m in _IDENT_PART.finditer(str(raw))]
    return [p for p in parts if p]


def original_name(raw: str) -> str:
    """The object's name with original case, brackets/quotes stripped.

    Unlike normalize_identifier (which lowercases for stable ids), this keeps
    the source casing for display, e.g. '[dim].[Practice]' -> 'Practice'.
    A dot inside brackets/quotes (`[my.weird.table]`) stays in the name.
    """
    parts = _ident_parts(raw)
    return parts[-1] if parts else ""


def qualify(raw: str) -> tuple[str, str]:
    """'[dbo].[Foo]' -> ('dbo', 'foo'); unqualified names default to dbo.

    Respects bracket/quote structure before splitting on '.', so a bracketed
    name containing a literal dot (`[my.proc]`, `[dbo].[my.weird.table]`) keeps
    that dot in the object name rather than being split apart.

    Cross-database qualifiers are preserved, not collapsed: a 3-part
    'OtherDb.dbo.t' becomes ('otherdb.dbo', 't') and a 4-part linked-server
    'SRV.OtherDb.dbo.t' becomes ('srv.otherdb.dbo', 't'), so an object in
    another database can never share a node id with (and silently steal the
    lineage of) the local schema.name object. table_parts applies the same
    policy to sqlglot Tables.
    """
    parts = [p.lower() for p in _ident_parts(raw)]
    if not parts:
        return ("dbo", "")
    name = parts[-1]
    schema = ".".join(parts[:-1]) or "dbo"
    return (schema, name)


def table_parts(table: exp.Table) -> tuple[str, str]:
    """(schema, name) for a sqlglot Table, lowercased, defaulting to dbo.

    Every qualifier (linked server, database, schema) is kept in the schema
    part — same policy as qualify() — so cross-database reads get their own
    node ids instead of being conflated with local objects.
    """
    parts = [normalize_identifier(part.name) for part in table.parts]
    parts = [p for p in parts if p]
    if not parts:
        return ("dbo", "")
    name = parts[-1]
    schema = ".".join(parts[:-1]) or "dbo"
    return (schema, name)


def cte_names(expression: exp.Expression) -> set[str]:
    """Normalized aliases of every CTE under an expression."""
    return {normalize_identifier(cte.alias_or_name) for cte in expression.find_all(exp.CTE)}


# OPENROWSET(BULK ...) named-option keywords. sqlglot's tsql grammar can't parse the
# named-option arg list, so under IGNORE recovery an option name (FORMAT / DATA_SOURCE /
# ...) is left behind as a phantom *unqualified* exp.Table. When an OPENROWSET call is in
# scope these are parse artifacts, not real sources — treating one as a table invents a
# bogus reads-edge to e.g. `dbo.format` (hard rule 4: never guess lineage). The external
# file itself isn't a warehouse object, so like COPY INTO it contributes no source node.
_OPENROWSET_OPTION_NAMES = frozenset({"bulk", "format", "data_source", "parser_version", "rowset_options"})


def _has_openrowset(expression: exp.Expression) -> bool:
    return any(
        isinstance(fn, exp.Anonymous) and fn.name.upper() == "OPENROWSET"
        for fn in expression.find_all(exp.Anonymous)
    )


def collect_source_tables(expression: exp.Expression) -> set[tuple[str, str]]:
    """All real tables under an expression, excluding CTE aliases, temp
    tables, table variables, table-valued functions, and OPENROWSET option-keyword
    phantoms (see :data:`_OPENROWSET_OPTION_NAMES`)."""
    ctes = cte_names(expression)
    has_openrowset = _has_openrowset(expression)
    found: set[tuple[str, str]] = set()
    for table in expression.find_all(exp.Table):
        if is_temp_table(table):
            continue
        if isinstance(table.this, exp.Func):
            # FROM <schema>.<fn>(...): a schema-QUALIFIED table-valued function
            # is a real user object (built-ins are never schema-qualified), so
            # keep it as a source — callers of an inline TVF get lineage, and
            # resolve_stub_references folds the stub into the TVF's view node
            # (issue #31). Unqualified calls (STRING_SPLIT, OPENJSON,
            # OPENROWSET…) stay dropped — never guess lineage.
            if table.text("db"):
                found.add(table_parts(table))
            continue
        schema, name = table_parts(table)
        if not table.text("db") and name in ctes:
            continue
        # An unqualified OPENROWSET option keyword is a recovery artifact, not a source;
        # a genuinely-joined real table (schema-qualified, or any other name) is kept.
        if has_openrowset and not table.text("db") and name in _OPENROWSET_OPTION_NAMES:
            continue
        found.add((schema, name))
    return found


@dataclass
class StatementLineage:
    """Tables one statement writes to and reads from."""

    writes: set[tuple[str, str]] = field(default_factory=set)
    reads: set[tuple[str, str]] = field(default_factory=set)
    dml_column_lineage: dict[tuple[str, str], dict[str, list[str]]] = field(default_factory=dict)


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
            # A CTE/alias only shadows a *bare* name; a schema-qualified table
            # (dbo.tgt) is a real object even when a CTE happens to share its
            # name (`WITH tgt AS (...) ... INSERT INTO dbo.tgt`).
            qualified = "." in normalize_identifier(raw)
            if not qualified:
                if name in ctes:
                    continue
                if name in aliases:
                    schema, name = aliases[name]
            lineage.writes.add((schema, name))
    for rx in _READ_RX:
        for match in rx.finditer(text):
            raw = match.group(1)
            if is_temp(normalize_identifier(raw)) or is_temp(raw):
                continue
            schema, name = qualify(raw)
            # same rule on the read side: drop a CTE/alias only when unqualified,
            # so `FROM dbo.orders` survives even with a CTE also named `orders`.
            if (name in ctes or name in aliases) and "." not in normalize_identifier(raw):
                continue
            lineage.reads.add((schema, name))
    lineage.reads -= lineage.writes
    return lineage


# -- parallel SQL parsing worker (issue #18) -----------------------------------
#
# ``parse_worker_both_passes`` is the ONLY function shipped across a process
# boundary, so it MUST be a picklable module-level function (no closures,
# lambdas, or bound methods) — Windows uses the spawn start-method, which
# re-imports this module and looks the function up by qualified name. Its inputs
# are a FileEntry dump (dict), the already-decoded file text (str), and the
# dialect (str) — all picklable. It parses ONE file into a throwaway graph via
# the same per-file entry parsers a cache MISS uses, so the ParseCacheEntry it
# returns is byte-for-byte what the sequential path would have cached. The
# parent merges every file's entry in sorted entry.path order via the shared
# _replay_entry path, so a --jobs N build is byte-identical to --jobs 1 and to a
# cold serial build by the same argument the parse cache already proves.


def parse_worker_both_passes(
    args: tuple[dict, str, str],
) -> tuple[str, dict | None, dict | None, str | None]:
    """Parse one SQL file's objects + procs passes in an isolated worker process.

    ``args`` is ``(entry_dump, text, dialect)`` where ``entry_dump`` is
    ``FileEntry.model_dump()`` (picklable) and ``text`` is the file's
    already-decoded content (read ONCE in the parent so the worker never touches
    disk and can't see a different encoding). Returns
    ``(path, objects_entry_dump | None, procs_entry_dump | None, error | None)``:

    - On success, the two entry dumps are ``ParseCacheEntry.model_dump()`` for
      the objects and procs passes — exactly what a sequential cache MISS stores.
    - On ANY exception, both entry dumps are ``None`` and ``error`` is the string
      the parent degrades into a ``parse_worker_error`` warning; the file
      contributes nothing but the build never hangs or aborts (HAZARD: a worker
      that raises must never take down the pool).

    All heavy parser imports are lazy (inside the function) to avoid a
    module-import cycle: ``sql_objects``/``sql_procs`` import from this module.
    """
    entry_dump, text, dialect = args
    # Best-effort path for the return tuple / error diagnostic, even if the
    # entry dump is somehow malformed — never let extracting it raise.
    path = entry_dump.get("path", "") if isinstance(entry_dump, dict) else ""
    try:
        from coop_data_doc.crawler import FileEntry
        from coop_data_doc.graph.model import LineageGraph
        from coop_data_doc.parsers.sql_objects import (
            _Contribution,
            _parse_objects_entry,
        )
        from coop_data_doc.parsers.sql_procs import _parse_procs_entry

        entry = FileEntry.model_validate(entry_dump)
        # A throwaway graph constructed INSIDE the worker (never shipped from the
        # parent): the contributions are recorded against it, but only the
        # recorders' entries are returned. Warnings are captured on the
        # contribution too (record_warning), so the discarded list is fine.
        graph = LineageGraph()
        discard: list = []

        objects_contribution = _Contribution()
        _parse_objects_entry(entry, text, graph, dialect, discard, objects_contribution)

        procs_contribution = _Contribution()
        _parse_procs_entry(entry, text, graph, dialect, discard, procs_contribution)

        return (
            entry.path,
            objects_contribution.to_entry().model_dump(),
            procs_contribution.to_entry().model_dump(),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the pool
        return (path, None, None, f"{type(exc).__name__}: {exc}")

def extract_column_lineage(select: exp.Select, dialect: str) -> dict[str, list[str]]:
    from sqlglot.lineage import lineage
    col_lineage = {}
    for item in select.expressions:
        if isinstance(item, exp.Star):
            return {}
        name = normalize_identifier(item.alias_or_name or "")
        if not name or name == "*":
            continue
        try:
            node = lineage(name, select, dialect=dialect)
        except Exception:
            continue
            
        sources = set()
        for d in node.downstream:
            if isinstance(d.source, exp.Table):
                schema, table_name = table_parts(d.source)
                col_name = d.name.split('.')[-1]
                schema = normalize_identifier(schema) if schema else ""
                table_name = normalize_identifier(table_name)
                col_name = normalize_identifier(col_name)
                prefix = f"{schema}.{table_name}" if schema else table_name
                sources.add(f"{prefix}.{col_name}")
        if sources:
            col_lineage[name] = sorted(sources)
    return col_lineage
