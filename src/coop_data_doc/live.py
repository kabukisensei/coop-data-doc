"""Snapshot a live database's object definitions into a source folder.

The second, explicit exception to the offline pipeline (the first is
``upgrade.py``): ``coop-data-doc snapshot`` connects to each database listed
under the config's ``live_sources``, reads its catalog, and writes one ``.sql``
file per object into that source's repo folder. ``scan`` / ``build`` then read
those files exactly as they read a checked-out repo, so the pipeline itself
stays offline and deterministic. The pipeline never imports this module.

What is read (SELECT on catalog views only; the connection asks for read-only
intent and no statement here changes anything):

* ``sys.objects`` / ``sys.schemas``: the tables, views, procedures, functions
  and triggers that exist;
* ``sys.sql_modules``: each module's definition, written as the database
  stores it;
* ``sys.columns`` / ``sys.types``: each table's columns, written as a plain
  ``CREATE TABLE`` (names, types, nullability; no keys, indexes or defaults,
  which carry no lineage);
* ``sys.sql_expression_dependencies``: the database's own dependency list,
  written as ``Dependencies.json`` in the shape ``catalog_deps`` checks the
  parsed lineage against. A platform without that view only loses the check.

A snapshot is all or nothing: the whole catalog is read first, and a failed
read leaves the previous snapshot as it was. Only files a previous snapshot
wrote (named in its manifest) are ever deleted, and a folder that holds
anything else is refused, so pointing a live source at a real repo by mistake
cannot overwrite it. No rows of data are read, and no password is accepted:
sign-in is the machine's Entra ID sign-in (``az login``) or Windows sign-in.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coop_data_doc import __version__
from coop_data_doc.atomic import atomic_write_text
from coop_data_doc.config import Config, LiveSource

MANIFEST = ".coop-data-doc-snapshot.json"
DEPENDENCIES_FILE = "Dependencies.json"
MANIFEST_VERSION = 1
SNAPSHOT_ENVIRONMENTS = frozenset({"dev", "development"})

SQL_COPT_SS_ACCESS_TOKEN = 1256
TOKEN_SCOPE = "https://database.windows.net/.default"
DEFAULT_CONNECT_DEADLINE = 90
QUERY_TIMEOUT_SECONDS = 300

# Schemas the platform owns (Fabric's query insights and result-set cache).
_PLATFORM_SCHEMAS = frozenset({"sys", "information_schema", "queryinsights", "_rsc"})

# sys.objects type code -> (folder, word used in Dependencies.json)
_TYPES: dict[str, tuple[str, str]] = {
    "U": ("Tables", "table"),
    "V": ("Views", "view"),
    "P": ("Stored Procedures", "procedure"),
    "FN": ("Functions", "function"),
    "IF": ("Functions", "function"),
    "TF": ("Functions", "function"),
    "TR": ("Triggers", "trigger"),
}
_DEPENDENCY_TYPES = {
    **{code: word for code, (_, word) in _TYPES.items()},
    "FS": "function",
    "FT": "function",
    "SN": "synonym",
    "SO": "sequence",
}

OBJECTS_SQL = """
SELECT o.object_id, s.name, o.name, RTRIM(o.type)
FROM sys.objects AS o
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
WHERE o.is_ms_shipped = 0
  AND o.type IN ('U', 'V', 'P', 'FN', 'IF', 'TF', 'TR')
"""

MODULES_SQL = """
SELECT m.object_id, m.definition
FROM sys.sql_modules AS m
JOIN sys.objects AS o ON o.object_id = m.object_id
WHERE o.is_ms_shipped = 0
"""

COLUMNS_SQL = """
SELECT c.object_id, c.column_id, c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable
FROM sys.columns AS c
JOIN sys.objects AS o ON o.object_id = c.object_id
JOIN sys.types AS t ON t.user_type_id = c.user_type_id
WHERE o.is_ms_shipped = 0 AND o.type = 'U'
"""

# Same rows `sqlsc dependencies` reads, so the file means the same thing.
DEPENDENCIES_SQL = """
SELECT DISTINCT
    rs.name, ro.name, RTRIM(ro.type),
    COALESCE(d.referenced_schema_name, ts.name), d.referenced_entity_name, RTRIM(t.type),
    d.referenced_database_name, d.referenced_server_name
FROM sys.sql_expression_dependencies AS d
JOIN sys.objects AS ro ON ro.object_id = d.referencing_id
JOIN sys.schemas AS rs ON rs.schema_id = ro.schema_id
LEFT JOIN sys.objects AS t ON t.object_id = d.referenced_id
LEFT JOIN sys.schemas AS ts ON ts.schema_id = t.schema_id
WHERE d.referencing_class = 1
  AND d.referenced_class = 1
  AND ro.is_ms_shipped = 0
"""


class LiveError(Exception):
    """A user-facing snapshot problem; the message is printable as-is."""


@dataclass
class Catalog:
    """Everything one snapshot reads, held in memory before any file is written."""

    # (object_id, schema, name, type code)
    objects: list[tuple[int, str, str, str]] = field(default_factory=list)
    definitions: dict[int, str | None] = field(default_factory=dict)
    # object_id -> [(column_id, name, type, max_length, precision, scale, nullable)]
    columns: dict[int, list[tuple[int, str, str, int, int, int, bool]]] = field(default_factory=dict)
    # None when the platform could not list them; the reason is in dependencies_error
    dependencies: list[dict[str, Any]] | None = None
    dependencies_error: str | None = None


@dataclass
class SnapshotResult:
    """What one source's snapshot did, in the shape the CLI prints as JSON."""

    source: str
    path: str
    skipped: bool = False  # fresh enough (--if-older-than): nothing was read
    taken_at: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    written: int = 0
    removed: int = 0
    unread: list[dict[str, str]] = field(default_factory=list)
    renamed: list[dict[str, str]] = field(default_factory=list)
    dependencies: int | None = None
    dependencies_error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "skipped": self.skipped,
            "taken_at": self.taken_at,
            "counts": dict(sorted(self.counts.items())),
            "written": self.written,
            "removed": self.removed,
            "unread": self.unread,
            "renamed": self.renamed,
            "dependencies": self.dependencies,
            "dependencies_error": self.dependencies_error,
        }


# --------------------------------------------------------------------------
# connecting
# --------------------------------------------------------------------------


def pick_driver(drivers: list[str]) -> str | None:
    """The newest installed ``ODBC Driver N for SQL Server`` with N >= 18."""
    found = []
    for driver in drivers:
        match = re.match(r"^ODBC Driver (\d+) for SQL Server$", driver)
        if match and int(match.group(1)) >= 18:
            found.append((int(match.group(1)), driver))
    return max(found)[1] if found else None


def _brace(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def connection_string(source: LiveSource, driver: str) -> str:
    """The ODBC connection string: encrypted, read-only intent, never a password."""
    server = source.server
    if source.auth == "entra" and "," not in server and not server.lower().startswith("tcp:"):
        server = f"tcp:{server},1433"
    parts = [
        f"Driver={_brace(driver)}",
        f"Server={_brace(server)}",
        f"Database={_brace(source.database)}",
        "Encrypt=yes",
        "TrustServerCertificate=no",
        "ApplicationIntent=ReadOnly",
        "Connection Timeout=60",
        "APP=coop-data-doc",
    ]
    if source.auth == "windows":
        parts.append("Trusted_Connection=yes")
    return ";".join(parts) + ";"


def _entra_token(source: LiveSource) -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise LiveError(_MISSING_EXTRA) from exc
    import logging

    # azure-identity logs every credential it tried as a WARNING with a traceback
    logging.getLogger("azure.identity").setLevel(logging.ERROR)
    kwargs: dict[str, Any] = {"exclude_interactive_browser_credential": True}
    if source.tenant_id:
        kwargs["additionally_allowed_tenants"] = [source.tenant_id]
    try:
        credential = DefaultAzureCredential(**kwargs)
        return credential.get_token(TOKEN_SCOPE, tenant_id=source.tenant_id or None).token
    except Exception as exc:  # azure-identity raises several unrelated types
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        raise LiveError(
            "Could not get an Entra ID token. Run `az login` (add --tenant <id> for another "
            f"tenant) and try again. Details: {first[:300]}"
        ) from exc


def pack_token(token: str) -> bytes:
    """An access token in the layout the ODBC driver's SQL_COPT_SS_ACCESS_TOKEN takes."""
    encoded = token.encode("utf-16-le")
    return struct.pack("<I", len(encoded)) + encoded


_MISSING_EXTRA = (
    "Reading a live database needs the optional `live` extra (pyodbc, azure-identity) and the "
    "ODBC Driver 18 for SQL Server. Install with: pipx install 'coop-data-doc[live]' --force "
    "(or pipx inject coop-data-doc pyodbc azure-identity); on Windows the driver is "
    "`winget install --id Microsoft.msodbcsql.18 -e`."
)


def _connect_deadline() -> int:
    raw = os.environ.get("COOP_DATA_DOC_CONNECT_TIMEOUT", "").strip()
    try:
        return max(5, int(raw)) if raw else DEFAULT_CONNECT_DEADLINE
    except ValueError:
        return DEFAULT_CONNECT_DEADLINE


def _within(connect: Callable[[], Any], seconds: int) -> Any:
    """Run connect() on a daemon thread and give up after ``seconds``: a login a Fabric
    endpoint accepts and never answers cannot be interrupted, but must not hang the run."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["conn"] = connect()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError
    if "error" in box:
        raise box["error"]
    return box["conn"]


def open_connection(source: LiveSource) -> Any:
    """A read-only-intent pyodbc connection to ``source``; LiveError says what to fix."""
    try:
        import pyodbc
    except ImportError as exc:
        raise LiveError(_MISSING_EXTRA) from exc
    pyodbc.pooling = False  # one short process; must be set before the first ODBC call
    driver = pick_driver(list(pyodbc.drivers()))
    if driver is None:
        raise LiveError(
            "ODBC Driver 18 for SQL Server is not installed. "
            "On Windows: winget install --id Microsoft.msodbcsql.18 -e"
        )
    conn_str = connection_string(source, driver)
    attrs: dict[int, Any] = {}
    if source.auth == "entra":
        attrs[SQL_COPT_SS_ACCESS_TOKEN] = pack_token(_entra_token(source))

    def attempt() -> Any:
        return pyodbc.connect(conn_str, attrs_before=attrs or None, autocommit=True)

    deadline = _connect_deadline()
    try:
        try:
            conn = _within(attempt, deadline // 2)
        except TimeoutError:
            conn = _within(attempt, deadline - deadline // 2)  # a stalled login usually works next time
    except TimeoutError as exc:
        raise LiveError(
            f"Could not connect to {source.server} / {source.database}: no answer within {deadline} "
            "seconds, twice. A paused Fabric capacity can take minutes to wake; try again, or raise "
            "COOP_DATA_DOC_CONNECT_TIMEOUT."
        ) from exc
    except pyodbc.Error as exc:
        raise LiveError(
            f"Could not connect to {source.server} / {source.database}: {_first_line(exc)}"
        ) from exc
    conn.timeout = QUERY_TIMEOUT_SECONDS
    return conn


def _first_line(exc: BaseException) -> str:
    text = " ".join(str(part) for part in exc.args) if exc.args else str(exc)
    return (text.strip().splitlines() or [type(exc).__name__])[0][:400]


# --------------------------------------------------------------------------
# reading the catalog
# --------------------------------------------------------------------------


def _rows(conn: Any, sql: str) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def read_catalog(conn: Any) -> Catalog:
    """Read every catalog list. The object, module and column reads must all succeed
    (their error propagates, and nothing is written); the dependency list is optional."""
    catalog = Catalog()
    for object_id, schema, name, code in _rows(conn, OBJECTS_SQL):
        if str(schema).lower() in _PLATFORM_SCHEMAS:
            continue
        catalog.objects.append((int(object_id), str(schema), str(name), str(code).strip()))
    for object_id, definition in _rows(conn, MODULES_SQL):
        catalog.definitions[int(object_id)] = definition
    for object_id, column_id, name, type_name, max_length, precision, scale, nullable in _rows(
        conn, COLUMNS_SQL
    ):
        catalog.columns.setdefault(int(object_id), []).append(
            (
                int(column_id),
                str(name),
                str(type_name),
                int(max_length or 0),
                int(precision or 0),
                int(scale or 0),
                bool(nullable),
            )
        )
    try:
        rows = _rows(conn, DEPENDENCIES_SQL)
    except Exception as exc:  # noqa: BLE001 - the driver's error: this platform has no such view
        catalog.dependencies_error = _first_line(exc)
        return catalog
    found: dict[tuple, dict[str, Any]] = {}
    for schema, name, code, ref_schema, ref_name, ref_code, ref_db, ref_server in rows:
        if not ref_name or str(schema).lower() in _PLATFORM_SCHEMAS:
            continue
        reference: dict[str, Any] = {
            "schema": ref_schema or None,
            "name": ref_name,
            "type": _DEPENDENCY_TYPES.get(str(ref_code or "").strip()) if ref_code else None,
        }
        if ref_db:
            reference["database"] = ref_db
        if ref_server:
            reference["server"] = ref_server
        key = (
            str(schema).lower(),
            str(name).lower(),
            str(ref_server or "").lower(),
            str(ref_db or "").lower(),
            str(ref_schema or "").lower(),
            str(ref_name).lower(),
        )
        found[key] = {
            "object": {
                "schema": schema,
                "name": name,
                "type": _DEPENDENCY_TYPES.get(str(code or "").strip(), "object"),
            },
            "references": reference,
        }
    catalog.dependencies = [found[key] for key in sorted(found)]
    return catalog


# --------------------------------------------------------------------------
# rendering files
# --------------------------------------------------------------------------

# Characters Windows refuses in a file name, plus '%' (the escape itself).
_UNSAFE = frozenset('<>:"/\\|?*%') | {chr(c) for c in range(32)}


def _encode(part: str, *, dot: bool) -> str:
    """sqlsc's file-name escape (%XX per UTF-8 byte), so both tools name files alike."""
    out = []
    for ch in part:
        if ch in _UNSAFE or (dot and ch == "."):
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)
    encoded = "".join(out)
    if encoded.endswith((" ", ".")):  # Windows refuses names ending in a space or dot
        encoded = encoded[:-1] + f"%{ord(encoded[-1]):02X}"
    return encoded


def relative_path(schema: str, name: str, code: str) -> str:
    """``<Type folder>/<schema>.<name>.sql``, POSIX separators."""
    return f"{_TYPES[code][0]}/{_encode(schema, dot=True)}.{_encode(name, dot=False)}.sql"


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


_LENGTH_TYPES = frozenset({"char", "varchar", "binary", "varbinary"})
_WIDE_TYPES = frozenset({"nchar", "nvarchar"})
_PRECISION_TYPES = frozenset({"decimal", "numeric"})
_SCALE_TYPES = frozenset({"datetime2", "time", "datetimeoffset"})
_PLAIN_TYPE = re.compile(r"^[a-z_][a-z0-9_]*$")


def type_text(name: str, max_length: int, precision: int, scale: int) -> str:
    """A column's type as T-SQL writes it (``varchar(50)``, ``decimal(18, 2)``)."""
    lowered = name.lower()
    if lowered in _LENGTH_TYPES:
        return f"{lowered}({'max' if max_length == -1 else max_length})"
    if lowered in _WIDE_TYPES:
        return f"{lowered}({'max' if max_length == -1 else max_length // 2})"
    if lowered in _PRECISION_TYPES:
        return f"{lowered}({precision}, {scale})"
    if lowered in _SCALE_TYPES:
        return f"{lowered}({scale})"
    return lowered if _PLAIN_TYPE.match(lowered) else _quote(name)


def table_text(schema: str, name: str, columns: list[tuple[int, str, str, int, int, int, bool]]) -> str:
    """A plain ``CREATE TABLE`` listing the columns in catalog order."""
    lines = [
        f"    {_quote(col)} {type_text(type_name, length, precision, scale)} {'NULL' if nullable else 'NOT NULL'}"
        for _, col, type_name, length, precision, scale, nullable in sorted(columns)
    ]
    return f"CREATE TABLE {_quote(schema)}.{_quote(name)} (\n" + ",\n".join(lines) + "\n);\n"


def module_text(definition: str) -> str:
    """The stored definition with LF line endings and one trailing newline."""
    text = definition.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"


_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_PART = r"(?:\[(?:[^\]]|\]\])*\]|\"(?:[^\"]|\"\")*\"|[^\s.(;\[\"]+)"
_DECLARATION = re.compile(
    r"\bCREATE\s+(?:OR\s+ALTER\s+)?(?:VIEW|PROC|PROCEDURE|FUNCTION|TRIGGER)\s+"
    rf"(?:{_PART}\s*\.\s*)?({_PART})",
    re.IGNORECASE,
)


def declared_name(definition: str) -> str | None:
    """The object name a module's CREATE declares, or None when it can't be read.

    A module renamed with ``sp_rename`` keeps its old name in the stored text, so the
    docs would show the old name; the snapshot reports each one instead of guessing.
    """
    match = _DECLARATION.search(_COMMENTS.sub(" ", definition))
    if not match:
        return None
    part = match.group(1)
    if part.startswith("["):
        return part[1:-1].replace("]]", "]")
    if part.startswith('"'):
        return part[1:-1].replace('""', '"')
    return part


def render(catalog: Catalog) -> tuple[dict[str, str], dict[str, Any]]:
    """``{relative path: text}`` for every readable object, plus the report:
    ``counts`` by kind, ``unread`` (no definition or no columns visible to this
    sign-in) and ``renamed`` (stored CREATE names another object)."""
    files: dict[str, str] = {}
    taken: set[str] = set()
    counts: dict[str, int] = {}
    unread: list[dict[str, str]] = []
    renamed: list[dict[str, str]] = []
    for object_id, schema, name, code in sorted(
        catalog.objects, key=lambda o: (o[1].lower(), o[2].lower(), o)
    ):
        kind = _TYPES[code][1]
        if code == "U":
            columns = catalog.columns.get(object_id)
            if not columns:
                unread.append({"schema": schema, "name": name, "type": kind, "why": "no columns visible"})
                continue
            text = table_text(schema, name, columns)
        else:
            definition = catalog.definitions.get(object_id)
            if not definition or not definition.strip():
                unread.append({"schema": schema, "name": name, "type": kind, "why": "no definition visible"})
                continue
            text = module_text(definition)
            declared = declared_name(definition)
            if declared is not None and declared.lower() != name.lower():
                renamed.append({"schema": schema, "name": name, "type": kind, "declared": declared})
        path = relative_path(schema, name, code)
        if path.lower() in taken:
            # two names that differ only by case cannot both be files on Windows
            unread.append({"schema": schema, "name": name, "type": kind, "why": "file name collides"})
            continue
        taken.add(path.lower())
        files[path] = text
        counts[kind] = counts.get(kind, 0) + 1
    return files, {"counts": counts, "unread": unread, "renamed": renamed}


def dependencies_text(dependencies: list[dict[str, Any]]) -> str:
    document = {"version": 1, "source": "sys.sql_expression_dependencies", "dependencies": dependencies}
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# the snapshot folder
# --------------------------------------------------------------------------


def read_manifest(folder: Path) -> dict[str, Any] | None:
    """The previous snapshot's manifest, or None when there is none or it can't be read."""
    try:
        document = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) and isinstance(document.get("files"), list) else None


def age_days(folder: Path, now: datetime) -> float | None:
    """Days since the folder's snapshot was taken; None when there is none."""
    manifest = read_manifest(folder)
    try:
        taken = datetime.fromisoformat(str((manifest or {})["taken_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    return (now - taken).total_seconds() / 86400


def _foreign_entries(folder: Path, manifest: dict[str, Any] | None) -> list[str]:
    """Top-level entries a snapshot did not put there (so the folder is not ours)."""
    if not folder.exists():
        return []
    ours = {MANIFEST.lower(), DEPENDENCIES_FILE.lower()} | {
        folder_name.lower() for folder_name, _ in _TYPES.values()
    }
    if manifest is None:
        return sorted(entry.name for entry in folder.iterdir())
    return sorted(entry.name for entry in folder.iterdir() if entry.name.lower() not in ours)


def _safe_target(folder: Path, relative: str) -> Path | None:
    """``folder / relative`` when that stays inside ``folder``; else None."""
    if not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        return None
    target = folder / relative
    try:
        target.resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return None
    return target


def write_snapshot(
    name: str, source: LiveSource, folder: Path, catalog: Catalog, now: datetime
) -> SnapshotResult:
    """Replace the folder's snapshot with ``catalog``.

    A manifest with no ``taken_at`` goes down first, naming every file of the previous
    and the new snapshot: an interrupted run then leaves a folder that is still ours,
    counts as never taken (so it is retried, not trusted), and still knows which old
    files to remove. Files follow, then removals, then the finished manifest."""
    previous = read_manifest(folder)
    foreign = _foreign_entries(folder, previous)
    if foreign:
        raise LiveError(
            f"{folder} is not an empty folder or an earlier snapshot (found: {', '.join(foreign[:5])}"
            f"{', ...' if len(foreign) > 5 else ''}). A snapshot only writes into a folder of its own; "
            f"point repos.{source.repo}.path at a new folder."
        )
    files, report = render(catalog)
    if catalog.dependencies is not None:
        files[DEPENDENCIES_FILE] = dependencies_text(catalog.dependencies)
    old_files = [f for f in (previous or {}).get("files", []) if isinstance(f, str)]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        pending = {"version": MANIFEST_VERSION, "taken_at": None, "files": sorted({*old_files, *files})}
        atomic_write_text(folder / MANIFEST, json.dumps(pending, indent=2, ensure_ascii=False) + "\n")
        written = 0
        for relative in sorted(files):
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                same = target.read_text(encoding="utf-8") == files[relative]
            except (OSError, UnicodeDecodeError):
                same = False
            if not same:
                atomic_write_text(target, files[relative])
                written += 1
        keep = {relative.lower() for relative in files}
        removed = 0
        for relative in old_files:
            if relative.lower() in keep:
                continue
            target = _safe_target(folder, relative)
            if target is not None and target.is_file():
                target.unlink()
                removed += 1
        taken_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "version": MANIFEST_VERSION,
            "tool_version": __version__,
            "source": name,
            "server": source.server,
            "database": source.database,
            "environment": source.environment,
            "taken_at": taken_at,
            "counts": dict(sorted(report["counts"].items())),
            "unread": report["unread"],
            "renamed": report["renamed"],
            "dependencies": None if catalog.dependencies is None else len(catalog.dependencies),
            "dependencies_error": catalog.dependencies_error,
            "files": sorted(files),
        }
        atomic_write_text(folder / MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise LiveError(f"Could not write the snapshot to {folder}: {exc}") from exc
    return SnapshotResult(
        source=name,
        path=str(folder),
        taken_at=taken_at,
        counts=report["counts"],
        written=written,
        removed=removed,
        unread=report["unread"],
        renamed=report["renamed"],
        dependencies=manifest["dependencies"],
        dependencies_error=catalog.dependencies_error,
    )


def snapshot_source(
    config: Config,
    name: str,
    *,
    if_older_than: float | None = None,
    connect: Callable[[LiveSource], Any] | None = None,
    now: datetime | None = None,
) -> SnapshotResult:
    """Snapshot one configured live source into its repo folder.

    ``if_older_than`` (days) skips a source whose snapshot is younger, without
    connecting. ``connect`` and ``now`` are seams for tests.
    """
    source = config.live_sources.get(name)
    if source is None:
        known = ", ".join(sorted(config.live_sources)) or "none configured"
        raise LiveError(f"No live source named '{name}' (live_sources: {known}).")
    if source.environment.strip().lower() not in SNAPSHOT_ENVIRONMENTS:
        raise LiveError(
            f"live_sources.{name}.environment is {source.environment!r}. A snapshot reads a "
            "development database only; set `environment: dev` when that is what this one is."
        )
    if source.repo not in config.repos:
        raise LiveError(f"live_sources.{name}.repo names '{source.repo}', which is not under `repos`.")
    folder = config.repo_root(source.repo)
    moment = now or datetime.now(timezone.utc)
    if if_older_than is not None:
        age = age_days(folder, moment)
        if age is not None and 0 <= age < if_older_than:
            manifest = read_manifest(folder) or {}
            return SnapshotResult(
                source=name, path=str(folder), skipped=True, taken_at=manifest.get("taken_at")
            )
    conn = (connect or open_connection)(source)
    try:
        try:
            catalog = read_catalog(conn)
        except LiveError:
            raise
        except Exception as exc:  # the driver's error, reported as one line
            raise LiveError(
                f"Could not read the catalog of {source.server} / {source.database}: {_first_line(exc)}"
            ) from exc
    finally:
        # closing a dead connection is not the failure to report
        with contextlib.suppress(Exception):
            conn.close()
    return write_snapshot(name, source, folder, catalog, moment)
