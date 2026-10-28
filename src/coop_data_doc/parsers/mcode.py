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
    raw_kind: str  # "sql_database" | "native_query" | "lakehouse" | "as_model" | "static" | "fallback"


# The first argument can itself be a call containing top-level commas, e.g.
# Sql.Database("srv", "db"). Match the first argument as a run of either
# non-comma/paren chars or whole `( ... )` parenthesized groups (which may hold
# their own commas), so the `,` we stop on is the top-level one before the
# quoted query — never the comma inside Sql.Database(...). Otherwise the DB name
# would be mis-extracted as the SQL and all referenced tables dropped.
_NATIVE_QUERY_RE = re.compile(r'Value\.NativeQuery\s*\((?:[^(),]|\([^()]*\))+,\s*"((?:[^"]|"")*)"', re.DOTALL)
# The OTHER standard native-SQL shape (what Desktop's "SQL statement" import box
# generates): an options record on the connector itself —
# Sql.Database("srv", "db", [Query="SELECT …", CommandTimeout=…]). The call span
# is walked with a string-aware paren scanner (_call_args) so parens inside the
# SQL never truncate it; the Query value may be a quoted literal ("" escapes
# honored) or a let-bound identifier resolved via _BINDING_RE (issue #36).
_SQL_DATABASE_CALL_RE = re.compile(r"\bSql\.Databases?\s*\(")
_QUERY_OPTION_RE = re.compile(r'\bQuery\s*=\s*(?:"((?:[^"]|"")*)"|([A-Za-z_]\w*))', re.DOTALL)
_LAKEHOUSE_RE = re.compile(r"Lakehouse\.Contents\s*\(|Fabric\.|\.Warehouse\s*\(")
# A composite/DirectQuery reference to another Power BI semantic model:
# AnalysisServices.Database("powerbi://…", "ModelName", …) — the 2nd arg is the
# upstream model (a.k.a. AS database) this table chains to.
_AS_DATABASE_RE = re.compile(r'AnalysisServices\.Database\s*\(\s*"[^"]*"\s*,\s*"([^"]+)"')
_NAME_NAV_RE = re.compile(r'\b(?:Name|Id)\s*=\s*"([^"]+)"')

# `let` variable bindings: IDENT = "literal" — used to resolve indirected
# navigation like  Source{[Schema=LocalSchema, Item=LocalTable]}[Data]
_BINDING_RE = re.compile(r'\b([A-Za-z_]\w*)\s*=\s*"([^"]*)"')
# a Schema=/Item= navigation token whose value is either a quoted literal
# or an identifier (resolved against the bindings above)
_NAV_SCHEMA_RE = re.compile(r'\bSchema\s*=\s*(?:"([^"]+)"|([A-Za-z_]\w*))')
_NAV_ITEM_RE = re.compile(r'\bItem\s*=\s*(?:"([^"]+)"|([A-Za-z_]\w*))')
# a `{[ ... ]}` record-navigation segment (where Schema=/Item= really live)
_NAV_RECORD_RE = re.compile(r"\{\[(.*?)\]\}", re.DOTALL)
# inline/static tables (calculation, parameter, hand-built) have no DB source
_STATIC_RE = re.compile(r"Table\.FromRows|#table\b|Json\.Document")


def strip_m_comments(m_expression: str) -> str:
    """Remove // and /* */ comments from M code before pattern matching.

    A character scanner, not regexes: string literals (including `""` escapes
    and `#"quoted identifiers"`) are consumed first and copied through intact,
    so a '//' inside a URL literal can never truncate the string, unbalance
    the quotes, or hide the real source expression.
    """
    out: list[str] = []
    i, n = 0, len(m_expression)
    while i < n:
        ch = m_expression[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if m_expression[j] == '"':
                    if j + 1 < n and m_expression[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            out.append(m_expression[i:end])
            i = end
        elif m_expression.startswith("//", i):
            j = m_expression.find("\n", i)
            i = n if j == -1 else j
        elif m_expression.startswith("/*", i):
            j = m_expression.find("*/", i)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _call_args(text: str, open_paren: int) -> str:
    """The argument text of a call: from just after ``open_paren`` to its
    matching ``)``. String literals (with ``""`` escapes) are skipped whole so
    parens inside a SQL string never unbalance the depth count."""
    depth = 1
    i = open_paren + 1
    n = len(text)
    start = i
    while i < n and depth:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            i = min(j + 1, n)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return text[start : i - 1] if depth == 0 else text[start:]


def _resolve(match: re.Match | None, bindings: dict[str, str]) -> str | None:
    """A Schema=/Item= match resolves to its literal, or via a let binding."""
    if match is None:
        return None
    if match.group(1) is not None:  # quoted literal
        return match.group(1)
    return bindings.get(match.group(2))  # identifier -> bound literal (or None)


_IDENT_RE = re.compile(r'#"((?:[^"]|"")*)"|\b([A-Za-z_][\w.]*)')


def _let_steps(text: str) -> tuple[dict[str, str], str] | None:
    """A ``let … in …`` expression as ({step name: step expression}, the final
    expression), or None when the text is not one (or cannot be split). Steps
    are cut at top-level commas and the final expression at the top-level
    ``in``; string literals and ``#"quoted"`` names are skipped whole so a comma
    or an ``in`` inside them never cuts."""
    stripped = text.strip()
    head = re.match(r"let\b", stripped)
    if head is None:
        return None
    body = stripped[head.end() :]
    depth, i, n, start = 0, 0, len(body), 0
    parts: list[str] = []
    final: str | None = None
    while i < n:
        ch = body[i]
        if ch == '"' or body.startswith('#"', i):
            j = i + (2 if ch == "#" else 1)
            while j < n:
                if body[j] == '"':
                    if j + 1 < n and body[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            i = min(j + 1, n)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == ",":
            parts.append(body[start:i])
            start = i + 1
        elif (
            depth == 0
            and body.startswith("in", i)
            and (i == 0 or not (body[i - 1].isalnum() or body[i - 1] in "_."))
            and (i + 2 >= n or not (body[i + 2].isalnum() or body[i + 2] in "_."))
        ):
            parts.append(body[start:i])
            final = body[i + 2 :]
            break
        i += 1
    if final is None or depth != 0:
        return None
    steps: dict[str, str] = {}
    for part in parts:
        item = part.strip()
        if not item:
            continue
        if item.startswith('#"'):
            close = _IDENT_RE.match(item)
            if close is None:
                return None
            name, rest = close.group(1).replace('""', '"'), item[close.end() :]
        else:
            name, _eq, rest = item.partition("=")
            name, rest = name.strip(), "=" + rest
            if not re.fullmatch(r"[A-Za-z_][\w.]*", name):
                return None
        rest = rest.lstrip()
        if not rest.startswith("="):
            return None
        steps[name] = rest[1:]
    return steps, final


def _identifiers(expression: str) -> list[str]:
    """Every identifier token in an M expression, outside its string literals."""
    found: list[str] = []
    i, n = 0, len(expression)
    while i < n:
        if expression[i] == '"':
            j = i + 1
            while j < n:
                if expression[j] == '"':
                    if j + 1 < n and expression[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            i = min(j + 1, n)
            continue
        match = _IDENT_RE.match(expression, i)
        if match is None:
            i += 1
            continue
        found.append(match.group(1).replace('""', '"') if match.group(1) is not None else match.group(2))
        i = match.end()
    return found


def _reachable_text(text: str) -> str | None:
    """The step expressions the final ``in`` expression depends on, joined in
    step order, or None when the text is not a ``let`` the splitter reads. A
    step the result never uses (a leftover navigation, a renamed source) is
    not a dependency of the table, so its records are left out (#88)."""
    parsed = _let_steps(text)
    if parsed is None:
        return None
    steps, final = parsed
    wanted: set[str] = set()
    pending = [name for name in _identifiers(final) if name in steps]
    while pending:
        name = pending.pop()
        if name in wanted:
            continue
        wanted.add(name)
        pending.extend(ref for ref in _identifiers(steps[name]) if ref in steps and ref not in wanted)
    return "\n".join(expression for name, expression in steps.items() if name in wanted)


def extract_sources(m_expression: str) -> tuple[list[SourceRef], list[str]]:
    """Every source the M expression loads: (source refs, raw SQL strings from
    native queries). A navigation source is one ``Source{[Schema=…, Item=…]}``
    record the result depends on; a join or append of two database tables
    gives two refs (#88). ``static`` and ``as_model`` are one ref each."""
    text = strip_m_comments(m_expression)

    native_sql = [m.group(1).replace('""', '"') for m in _NATIVE_QUERY_RE.finditer(text)]

    # `let` variable bindings: shared by the Query= option below (a let-bound
    # query string) and the navigation records further down.
    bindings = {name: value for name, value in _BINDING_RE.findall(text)}

    # Sql.Database(server, db, [Query="…"]) — native SQL in the connector's
    # options record (issue #36). Same downstream handling as Value.NativeQuery.
    for call in _SQL_DATABASE_CALL_RE.finditer(text):
        args = _call_args(text, call.end() - 1)
        query = _QUERY_OPTION_RE.search(args)
        if query is None:
            continue
        sql = (
            query.group(1).replace('""', '"') if query.group(1) is not None else bindings.get(query.group(2))
        )
        if sql:
            native_sql.append(sql)

    # composite/DirectQuery chain to another semantic model. The string-aware
    # comment strip keeps the `powerbi://…` connection URL intact, so matching
    # `text` works — and a commented-out AnalysisServices.Database line can no
    # longer be mistaken for the live source.
    as_match = _AS_DATABASE_RE.search(text)
    if as_match:
        return [SourceRef(schema_name="", object_name=as_match.group(1), raw_kind="as_model")], native_sql

    # Sql.Database navigation — works for quoted literals AND for the common
    # PBIP template that binds the schema/table to `let` variables first.
    # Anchor Schema=/Item= to an actual `{[ ... ]}` navigation record so a
    # `let` step named Schema/Item can't poison the result. Only the records
    # the final expression depends on count; outside a readable `let`, every
    # record in the text does (the pre-#88 reading, first record included).
    reachable = _reachable_text(text)
    refs: list[SourceRef] = []
    for record in _NAV_RECORD_RE.findall(text if reachable is None else reachable):
        schema = _resolve(_NAV_SCHEMA_RE.search(record), bindings)
        item = _resolve(_NAV_ITEM_RE.search(record), bindings)
        if schema and item:
            ref = SourceRef(schema_name=schema.lower(), object_name=item.lower(), raw_kind="sql_database")
            if ref not in refs:
                refs.append(ref)
    if refs:
        return refs, native_sql
    if native_sql:
        return [], native_sql

    if _LAKEHOUSE_RE.search(text):
        names = _NAME_NAV_RE.findall(text)
        if names:
            return [
                SourceRef(
                    schema_name=names[-2].lower() if len(names) >= 2 else "",
                    object_name=names[-1].lower(),
                    raw_kind="lakehouse",
                )
            ], []

    # inline/static table (e.g. a DAX calculation or parameter table) — it has
    # no database source by design, so it's resolved, not "unresolved".
    if _STATIC_RE.search(text):
        return [SourceRef(schema_name="", object_name="", raw_kind="static")], []

    return [], []


def extract_source(m_expression: str) -> tuple[SourceRef | None, list[str]]:
    """Return (source-ref-or-None, raw SQL strings from NativeQuery calls): the
    one-source view of extract_sources. Native SQL wins (its tables are read
    from the SQL); else the first navigation source, as before #88."""
    refs, native_sql = extract_sources(m_expression)
    if native_sql:
        return SourceRef(schema_name="", object_name="", raw_kind="native_query"), native_sql
    return (refs[0] if refs else None), []
