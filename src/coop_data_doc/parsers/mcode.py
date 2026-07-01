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
_NATIVE_QUERY_RE = re.compile(r'Value\.NativeQuery\s*\((?:[^(),]|\([^()]*\))+,\s*"((?:[^"]|"")*)"', re.S)
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
_NAV_RECORD_RE = re.compile(r"\{\[(.*?)\]\}", re.S)
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

    # composite/DirectQuery chain to another semantic model. The string-aware
    # comment strip keeps the `powerbi://…` connection URL intact, so matching
    # `text` works — and a commented-out AnalysisServices.Database line can no
    # longer be mistaken for the live source.
    as_match = _AS_DATABASE_RE.search(text)
    if as_match:
        return SourceRef(schema_name="", object_name=as_match.group(1), raw_kind="as_model"), []

    # Sql.Database navigation — works for quoted literals AND for the common
    # PBIP template that binds the schema/table to `let` variables first.
    # Anchor Schema=/Item= to an actual `{[ ... ]}` navigation record so a
    # `let` step named Schema/Item can't poison the result.
    bindings = {name: value for name, value in _BINDING_RE.findall(text)}
    for record in _NAV_RECORD_RE.findall(text):
        schema = _resolve(_NAV_SCHEMA_RE.search(record), bindings)
        item = _resolve(_NAV_ITEM_RE.search(record), bindings)
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
