"""Cross-check the parsed lineage against a database's own dependency list.

coop-sql-source-control (`sqlsc dependencies`) commits ``Dependencies.json``
next to the object scripts: what each view, procedure, function and trigger
references, as the database records it in sys.sql_expression_dependencies.
Listed under the config's ``catalog_dependencies``, each file is read here and
every reference the parse did not link becomes a ``catalog_dependency_unparsed``
warning on the referencing object's script, so a gap (a statement the parser
could not read, a scalar function call, a synonym) is visible instead of silent.
Dynamic SQL is invisible to the database's list too, so it stays a
``dynamic_sql`` warning from the parser.

The list says that one object references another, not whether it reads or
writes it or which way data flows, so it only checks lineage: it never adds an
edge (hard rule 4: never guess lineage). The file is a local input read
offline; nothing here touches a database.
"""

from __future__ import annotations

import json
from pathlib import Path

from coop_data_doc.config import Config, ParseWarning
from coop_data_doc.graph.model import LineageGraph, NodeType, normalize_identifier
from coop_data_doc.layering import SYSTEM_SCHEMAS

SQL_NODE_TYPES = frozenset(
    {
        NodeType.BRONZE_TABLE,
        NodeType.SILVER_TABLE,
        NodeType.GOLD_TABLE,
        NodeType.VIEW,
        NodeType.STORED_PROC,
    }
)
# referenced kinds that carry no data lineage (a NEXT VALUE FOR, a type)
_NOT_LINEAGE = frozenset({"sequence"})
SUPPORTED_VERSION = 1


def _key(schema: str | None, name: str, database: str | None = None, server: str | None = None) -> tuple[str, str]:
    """(schema, name) the way the parsers qualify names: lowercase, and a
    cross-database reference keeps its database (and server) in the schema."""
    parts = [p for p in (server, database, schema or "dbo") if p]
    return ".".join(normalize_identifier(p) for p in parts), normalize_identifier(name)


def load(config: Config) -> tuple[list[tuple[str, dict]], list[ParseWarning]]:
    """Every listed file's dependency entries, as ``(display path, entry)`` pairs,
    plus a ``catalog_dependencies_unreadable`` warning for each file that can't be used."""
    entries: list[tuple[str, dict]] = []
    warnings: list[ParseWarning] = []
    for raw in config.catalog_dependencies:
        path = (config.base_dir / Path(raw).expanduser()).resolve()

        def unreadable(why: str, raw: str = raw) -> None:
            warnings.append(
                ParseWarning(
                    file=raw,
                    message=f"catalog dependency list not used: {why}",
                    category="catalog_dependencies_unreadable",
                )
            )

        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            unreadable("file not found")
            continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            unreadable(f"{type(exc).__name__}: {exc}")
            continue
        if not isinstance(document, dict) or not isinstance(document.get("dependencies"), list):
            unreadable("expected a JSON object with a `dependencies` list (sqlsc dependencies)")
            continue
        if document.get("version") != SUPPORTED_VERSION:
            unreadable(f"unsupported version {document.get('version')!r} (expected {SUPPORTED_VERSION})")
            continue
        for entry in document["dependencies"]:
            obj = entry.get("object") if isinstance(entry, dict) else None
            ref = entry.get("references") if isinstance(entry, dict) else None
            if isinstance(obj, dict) and isinstance(ref, dict) and obj.get("name") and ref.get("name"):
                entries.append((raw, entry))
    return entries, warnings


def check(graph: LineageGraph, config: Config, entries: list[tuple[str, dict]]) -> list[ParseWarning]:
    """One warning per listed reference the parsed graph does not link.

    Skipped, never warned: a referencing object that is not documented (out of
    the configured folders or schemas), a referenced object in an ignored or
    not-included schema, and references that carry no lineage (sequences).
    """
    by_key: dict[tuple[str, str], list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for node_id, node in graph.nodes.items():
        if node.node_type in SQL_NODE_TYPES:
            by_key.setdefault((node.schema_name, node.name), []).append(node_id)
            by_name.setdefault(node.name, []).append(node_id)
    linked = {frozenset((edge.source_id, edge.target_id)) for edge in graph.edges}
    ignored = SYSTEM_SCHEMAS | {s.lower() for s in config.ignore_schemas}
    included = {s.lower() for s in config.include_schemas}

    def out_of_scope(schema: str) -> bool:
        segment = schema.rsplit(".", 1)[-1]
        if schema in ignored or segment in ignored or segment.startswith("db_"):
            return True
        return bool(included) and schema not in included and segment not in included

    warnings: list[ParseWarning] = []
    seen: set[tuple[str, tuple[str, str] | str]] = set()
    for display, entry in entries:
        obj, ref = entry["object"], entry["references"]
        if ref.get("type") in _NOT_LINEAGE or str(ref["name"]).startswith(("#", "@")):
            continue  # a sequence, or a #temp table / table variable the module creates
        sources = by_key.get(_key(obj.get("schema"), obj["name"]), [])
        if not sources:
            continue  # the referencing object is not documented here
        if ref.get("schema"):
            ref_key: tuple[str, str] | str = _key(ref["schema"], ref["name"], ref.get("database"), ref.get("server"))
            if out_of_scope(ref_key[0]):
                continue
            targets = by_key.get(ref_key, [])
            label = f"{ref_key[0]}.{ref_key[1]}"
        else:
            # the module names it without a schema and the database could not resolve
            # it: any documented object of that name counts as the link
            ref_key = normalize_identifier(ref["name"])
            targets = by_name.get(ref_key, [])
            label = ref_key
        for source in sorted(sources):
            if (source, ref_key) in seen:
                continue
            seen.add((source, ref_key))
            if any(frozenset((source, target)) in linked for target in targets):
                continue
            node = graph.nodes[source]
            warnings.append(
                ParseWarning(
                    file=node.source_file or display,
                    message=(
                        f"{node.schema_name}.{node.name} references {label} in the database's "
                        f"dependency list ({display}), but the parsed lineage has no link between them"
                    ),
                    category="catalog_dependency_unparsed",
                )
            )
    return sorted(warnings, key=lambda w: (w.file, w.message))
