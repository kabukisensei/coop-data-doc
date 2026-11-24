"""`coop-data-doc snapshot`: a live dev database's catalog written as source files.

The database is a fake connection that answers the four catalog queries, so the
suite stays offline. What is proven here: the files a snapshot writes are read by
the unchanged pipeline (nodes, columns, edges), a snapshot is all or nothing, it
only ever deletes its own files, it refuses anything that is not a dev source or
not its own folder, and the pipeline never loads the database code."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_cli import run

from coop_data_doc import live
from coop_data_doc.cli import run_pipeline
from coop_data_doc.config import Config, ConfigError

NOW = datetime(2026, 11, 24, 6, 0, 0, tzinfo=timezone.utc)

OBJECTS = [
    (1, "silver", "customers", "U "),
    (2, "sales", "dim_customer", "V "),
    (3, "dbo", "usp_load_customers", "P "),
    (4, "dbo", "fn_region", "IF"),
    (5, "queryinsights", "exec_requests_history", "V "),  # the platform's: never written
    (8, "sqlsc", "ObjectLock", "U "),  # coop-sql-source-control's bookkeeping: never written
]
MODULES = [
    (
        2,
        (
            "CREATE VIEW [sales].[dim_customer] AS\r\n"
            "SELECT c.customer_id, c.customer_name FROM silver.customers AS c;"
        ),
    ),
    (
        3,
        (
            "CREATE PROCEDURE dbo.usp_load_customers AS\n"
            "INSERT INTO silver.customers (customer_id, customer_name)\n"
            "SELECT customer_id, customer_name FROM staging.customers_raw;"
        ),
    ),
    (4, "CREATE FUNCTION dbo.fn_region() RETURNS TABLE AS RETURN (SELECT region FROM silver.customers);"),
    (5, "CREATE VIEW queryinsights.exec_requests_history AS SELECT 1 AS x;"),
]
COLUMNS = [
    (1, 2, "customer_name", "varchar", 100, 0, 0, True),
    (1, 1, "customer_id", "int", 4, 10, 0, False),
    (1, 3, "region", "nvarchar", 40, 0, 0, True),
    (1, 4, "balance", "decimal", 9, 18, 2, True),
    (8, 1, "object_name", "varchar", 200, 0, 0, False),
]
DEPENDENCIES = [
    ("sales", "dim_customer", "V ", "silver", "customers", "U ", None, None),
    ("dbo", "usp_load_customers", "P ", "staging", "customers_raw", None, None, None),
    ("dbo", "fn_region", "IF", "silver", "customers", "U ", "OtherDb", None),
    ("queryinsights", "exec_requests_history", "V ", "sys", "objects", "V ", None, None),
]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def execute(self, sql):
        self.conn.executed.append(sql)
        table = self.conn.tables
        for key, query in (
            ("objects", live.OBJECTS_SQL),
            ("modules", live.MODULES_SQL),
            ("columns", live.COLUMNS_SQL),
            ("dependencies", live.DEPENDENCIES_SQL),
        ):
            if sql == query:
                answer = table[key]
                if isinstance(answer, Exception):
                    raise answer
                self.rows = list(answer)
                return
        raise AssertionError(f"unexpected statement: {sql}")

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, **tables):
        self.tables = {
            "objects": OBJECTS,
            "modules": MODULES,
            "columns": COLUMNS,
            "dependencies": DEPENDENCIES,
            **tables,
        }
        self.executed = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def workspace(tmp_path: Path, environment: str = "dev") -> Path:
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        f"""\
project_name: Live Estate
repos:
  warehouse:
    path: ./live/warehouse
    include: ["**/*.sql"]
live_sources:
  dev_wh:
    server: example.datawarehouse.fabric.microsoft.com
    database: Dev Warehouse
    repo: warehouse
    environment: {environment}
output:
  dir: ./data-docs
  site_dir: ./site
""",
        encoding="utf-8",
    )
    return config


def load(config_path: Path) -> Config:
    """The config as the snapshot command loads it (the folder may not exist yet)."""
    from coop_data_doc.cli import _load_config_lenient

    config = _load_config_lenient(config_path)
    config._base_dir = config_path.parent
    return config


def take(config_path: Path, conn=None, **kwargs):
    conn = conn or FakeConnection()
    return live.snapshot_source(
        load(config_path), "dev_wh", connect=lambda source: conn, now=kwargs.pop("now", NOW), **kwargs
    )


def tree(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def test_snapshot_writes_one_file_per_object_and_the_pipeline_reads_them(tmp_path: Path):
    config_path = workspace(tmp_path)
    conn = FakeConnection()
    result = take(config_path, conn)
    folder = tmp_path / "live" / "warehouse"

    assert conn.closed
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in conn.executed)
    assert sorted(tree(folder)) == [
        ".coop-data-doc-snapshot.json",
        "Dependencies.json",
        "Functions/dbo.fn_region.sql",
        "Stored Procedures/dbo.usp_load_customers.sql",
        "Tables/silver.customers.sql",
        "Views/sales.dim_customer.sql",
    ]
    assert (folder / "Tables" / "silver.customers.sql").read_text(encoding="utf-8") == (
        "CREATE TABLE [silver].[customers] (\n"
        "    [customer_id] int NOT NULL,\n"
        "    [customer_name] varchar(100) NULL,\n"
        "    [region] nvarchar(20) NULL,\n"
        "    [balance] decimal(18, 2) NULL\n"
        ");\n"
    )
    # stored text, LF line endings, one trailing newline
    assert (folder / "Views" / "sales.dim_customer.sql").read_bytes() == (
        b"CREATE VIEW [sales].[dim_customer] AS\n"
        b"SELECT c.customer_id, c.customer_name FROM silver.customers AS c;\n"
    )
    assert result.counts == {"function": 1, "procedure": 1, "table": 1, "view": 1}
    assert (result.written, result.removed, result.unread, result.renamed) == (5, 0, [], [])
    assert result.dependencies == 3 and result.taken_at == "2026-11-24T06:00:00Z"

    graph, _, warnings = run_pipeline(Config.load(config_path), interactive=False)
    by_name = {(n.schema_name, n.name): node_id for node_id, n in graph.nodes.items()}
    table, view = by_name[("silver", "customers")], by_name[("sales", "dim_customer")]
    proc = by_name[("dbo", "usp_load_customers")]
    assert [c.name for c in graph.nodes[table].columns] == [
        "customer_id",
        "customer_name",
        "region",
        "balance",
    ]
    flows = {(e.flow()[0], e.flow()[1]) for e in graph.edges}
    assert (table, view) in flows  # the view reads the table
    assert (proc, table) in flows  # the procedure writes it
    # the snapshot's own dependency list is checked without being configured:
    # the cross-database reference the parse does not link is reported
    missed = [w.message for w in warnings if w.category == "catalog_dependency_unparsed"]
    assert any("otherdb" in m.lower() for m in missed), missed
    assert not [w for w in warnings if w.category == "catalog_dependencies_unreadable"]


def test_dependencies_file_has_the_sqlsc_shape(tmp_path: Path):
    take(workspace(tmp_path))
    document = json.loads((tmp_path / "live" / "warehouse" / "Dependencies.json").read_text(encoding="utf-8"))
    assert document["version"] == 1 and document["source"] == "sys.sql_expression_dependencies"
    assert document["dependencies"] == [
        {
            "object": {"schema": "dbo", "name": "fn_region", "type": "function"},
            "references": {"schema": "silver", "name": "customers", "type": "table", "database": "OtherDb"},
        },
        {
            "object": {"schema": "dbo", "name": "usp_load_customers", "type": "procedure"},
            "references": {"schema": "staging", "name": "customers_raw", "type": None},
        },
        {
            "object": {"schema": "sales", "name": "dim_customer", "type": "view"},
            "references": {"schema": "silver", "name": "customers", "type": "table"},
        },
    ]


def test_second_snapshot_of_an_unchanged_database_rewrites_nothing(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)
    folder = tmp_path / "live" / "warehouse"
    before = {k: v for k, v in tree(folder).items() if k != live.MANIFEST}
    result = take(config_path, now=NOW + timedelta(days=7))
    assert (result.written, result.removed) == (0, 0)
    assert {k: v for k, v in tree(folder).items() if k != live.MANIFEST} == before
    assert live.read_manifest(folder)["taken_at"] == "2026-12-01T06:00:00Z"


def test_dropped_object_loses_its_file_and_changed_object_is_rewritten(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)
    changed = [(2, "CREATE VIEW sales.dim_customer AS SELECT customer_id FROM silver.customers;")]
    conn = FakeConnection(objects=OBJECTS[:2], modules=changed, dependencies=DEPENDENCIES[:1])
    result = take(config_path, conn)
    folder = tmp_path / "live" / "warehouse"
    assert (result.written, result.removed) == (2, 2)  # the view + Dependencies.json; proc + function gone
    assert sorted(tree(folder)) == [
        ".coop-data-doc-snapshot.json",
        "Dependencies.json",
        "Tables/silver.customers.sql",
        "Views/sales.dim_customer.sql",
    ]


def test_a_folder_that_is_not_a_snapshot_is_refused_untouched(tmp_path: Path):
    config_path = workspace(tmp_path)
    folder = tmp_path / "live" / "warehouse"
    (folder / "Views").mkdir(parents=True)
    (folder / "Views" / "sales.dim_customer.sql").write_text("-- a person's file\n", encoding="utf-8")
    before = tree(folder)
    with pytest.raises(live.LiveError, match="not an empty folder or an earlier snapshot"):
        take(config_path)
    assert tree(folder) == before


def test_a_file_someone_added_to_a_snapshot_folder_stops_the_next_snapshot(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)
    folder = tmp_path / "live" / "warehouse"
    (folder / "notes.md").write_text("mine\n", encoding="utf-8")
    before = tree(folder)
    with pytest.raises(live.LiveError, match="notes.md"):
        take(config_path, FakeConnection(objects=[]))
    assert tree(folder) == before


def test_manifest_cannot_point_a_removal_outside_the_folder(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)
    folder = tmp_path / "live" / "warehouse"
    outside = tmp_path / "live" / "keep.sql"
    outside.write_text("keep\n", encoding="utf-8")
    manifest = live.read_manifest(folder)
    manifest["files"] += ["../keep.sql", str(outside)]
    (folder / live.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    take(config_path)
    assert outside.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize("environment", ["prod", "production", "test", '""'])
def test_only_a_dev_source_is_read(tmp_path: Path, environment: str):
    config_path = workspace(tmp_path, environment)

    def never(source):
        raise AssertionError("connected to a source that is not dev")

    with pytest.raises(live.LiveError, match="development database only"):
        live.snapshot_source(load(config_path), "dev_wh", connect=never, now=NOW)
    assert not (tmp_path / "live").exists()


@pytest.mark.parametrize("failing", ["objects", "modules", "columns"])
def test_a_failed_catalog_read_leaves_the_previous_snapshot_as_it_was(tmp_path: Path, failing: str):
    config_path = workspace(tmp_path)
    take(config_path)
    folder = tmp_path / "live" / "warehouse"
    before = tree(folder)
    conn = FakeConnection(**{failing: RuntimeError("[42000] permission denied\nmore detail")})
    with pytest.raises(live.LiveError, match="Could not read the catalog.*permission denied"):
        take(config_path, conn, now=NOW + timedelta(days=1))
    assert conn.closed
    assert tree(folder) == before


def test_a_platform_without_the_dependency_view_only_loses_the_check(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)
    folder = tmp_path / "live" / "warehouse"
    conn = FakeConnection(dependencies=RuntimeError("Invalid object name 'sys.sql_expression_dependencies'."))
    result = take(config_path, conn)
    assert result.dependencies is None and "Invalid object name" in result.dependencies_error
    assert not (folder / "Dependencies.json").exists()  # last week's list would mislead
    assert result.counts == {"function": 1, "procedure": 1, "table": 1, "view": 1}
    _, _, warnings = run_pipeline(Config.load(config_path), interactive=False)
    assert not [w for w in warnings if w.category.startswith("catalog_")]


def test_objects_this_sign_in_cannot_read_and_renamed_modules_are_reported(tmp_path: Path):
    objects = [*OBJECTS[:4], (6, "dbo", "v_new_name", "V "), (7, "dbo", "empty_table", "U ")]
    modules = [
        (2, None),  # no VIEW DEFINITION permission, or encrypted
        MODULES[1],
        MODULES[2],
        (6, "/* CREATE VIEW dbo.commented */\nCREATE   OR ALTER VIEW [dbo].[v_old_name] AS SELECT 1 AS x;"),
    ]
    result = take(workspace(tmp_path), FakeConnection(objects=objects, modules=modules))
    assert result.unread == [
        {"schema": "dbo", "name": "empty_table", "type": "table", "why": "no columns visible"},
        {"schema": "sales", "name": "dim_customer", "type": "view", "why": "no definition visible"},
    ]
    assert result.renamed == [
        {"schema": "dbo", "name": "v_new_name", "type": "view", "declared": "v_old_name"}
    ]
    folder = tmp_path / "live" / "warehouse"
    assert not (folder / "Views" / "sales.dim_customer.sql").exists()
    manifest = live.read_manifest(folder)
    assert manifest["unread"] == result.unread and manifest["renamed"] == result.renamed


def test_if_older_than_skips_a_fresh_snapshot_without_connecting(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)

    def never(source):
        raise AssertionError("connected although the snapshot is fresh")

    config = load(config_path)
    fresh = live.snapshot_source(
        config, "dev_wh", connect=never, now=NOW + timedelta(days=6, hours=23), if_older_than=7
    )
    assert fresh.skipped and fresh.taken_at == "2026-11-24T06:00:00Z"
    stale = take(config_path, now=NOW + timedelta(days=7, minutes=1), if_older_than=7)
    assert not stale.skipped and stale.taken_at == "2026-12-01T06:01:00Z"


def test_an_interrupted_snapshot_is_retried_not_trusted(tmp_path: Path, monkeypatch):
    config_path = workspace(tmp_path)
    take(config_path)
    folder = tmp_path / "live" / "warehouse"
    real = live.atomic_write_text
    calls = {"n": 0}

    def fail_on_third(path, text):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(live, "atomic_write_text", fail_on_third)
    changed = [(object_id, (text or "") + "\n-- changed") for object_id, text in MODULES]
    with pytest.raises(live.LiveError, match="disk full"):
        take(config_path, FakeConnection(modules=changed), now=NOW + timedelta(days=1))
    monkeypatch.setattr(live, "atomic_write_text", real)
    # the folder is still a snapshot folder, but it does not count as taken
    assert live.age_days(folder, NOW + timedelta(days=1)) is None
    result = take(config_path, now=NOW + timedelta(days=1), if_older_than=7)
    assert not result.skipped
    assert "-- changed" not in (folder / "Views" / "sales.dim_customer.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "max_length", "precision", "scale", "expected"),
    [
        ("varchar", 50, 0, 0, "varchar(50)"),
        ("varchar", -1, 0, 0, "varchar(max)"),
        ("nvarchar", 100, 0, 0, "nvarchar(50)"),
        ("nvarchar", -1, 0, 0, "nvarchar(max)"),
        ("varbinary", 16, 0, 0, "varbinary(16)"),
        ("decimal", 9, 18, 4, "decimal(18, 4)"),
        ("datetime2", 8, 27, 6, "datetime2(6)"),
        ("INT", 4, 10, 0, "int"),
        ("uniqueidentifier", 16, 0, 0, "uniqueidentifier"),
        ("My Type", 4, 0, 0, "[My Type]"),
    ],
)
def test_type_text(name, max_length, precision, scale, expected):
    assert live.type_text(name, max_length, precision, scale) == expected


def test_file_names_escape_what_windows_refuses():
    assert live.relative_path("my.schema", "a/b:c", "V") == "Views/my%2Eschema.a%2Fb%3Ac.sql"
    assert live.relative_path("dbo", "ends.", "U") == "Tables/dbo.ends%2E.sql"
    assert live.relative_path("dbo", "naïve %", "P") == "Stored Procedures/dbo.naïve %25.sql"


def test_names_that_differ_only_by_case_do_not_overwrite_each_other(tmp_path: Path):
    objects = [(1, "dbo", "Orders", "U "), (2, "dbo", "orders", "U ")]
    columns = [(1, 1, "a", "int", 4, 10, 0, False), (2, 1, "b", "int", 4, 10, 0, False)]
    result = take(workspace(tmp_path), FakeConnection(objects=objects, modules=[], columns=columns))
    assert result.counts == {"table": 1}
    assert [u["why"] for u in result.unread] == ["file name collides"]


def test_connection_string_is_read_only_and_holds_no_password():
    source = load_source()
    text = live.connection_string(source, "ODBC Driver 18 for SQL Server")
    assert "ApplicationIntent=ReadOnly" in text and "Encrypt=yes" in text
    assert "Server={tcp:example.datawarehouse.fabric.microsoft.com,1433}" in text
    assert "Database={Dev Warehouse}" in text
    assert "PWD" not in text.upper() and "UID" not in text.upper()
    windows = source.model_copy(update={"auth": "windows", "server": "localhost"})
    assert "Trusted_Connection=yes" in live.connection_string(windows, "ODBC Driver 18 for SQL Server")


def load_source():
    from coop_data_doc.config import LiveSource

    return LiveSource(
        server="example.datawarehouse.fabric.microsoft.com", database="Dev Warehouse", repo="warehouse"
    )


def test_pick_driver_wants_18_or_newer():
    assert live.pick_driver(["SQL Server", "ODBC Driver 17 for SQL Server"]) is None
    assert (
        live.pick_driver(["ODBC Driver 18 for SQL Server", "ODBC Driver 19 for SQL Server"])
        == "ODBC Driver 19 for SQL Server"
    )


def test_config_rejects_a_password_an_unknown_repo_and_a_shared_folder(tmp_path: Path):
    config_path = workspace(tmp_path)
    (tmp_path / "live" / "warehouse").mkdir(parents=True)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("    repo: warehouse\n", "    repo: warehouse\n    password: x\n"), "utf-8"
    )
    with pytest.raises(ConfigError, match="live_sources.dev_wh.password"):
        Config.load(config_path)
    config_path.write_text(text.replace("repo: warehouse", "repo: nowhere"), "utf-8")
    with pytest.raises(ConfigError, match="not under `repos`"):
        Config.load(config_path)
    second = text.replace(
        "output:",
        "  other:\n    server: s\n    database: d\n    repo: warehouse\n    environment: dev\noutput:",
    )
    config_path.write_text(second, "utf-8")
    with pytest.raises(ConfigError, match="both write to repo 'warehouse'"):
        Config.load(config_path)


def test_cli_snapshot_json_and_build(tmp_path: Path, monkeypatch):
    workspace(tmp_path)
    monkeypatch.setattr(live, "open_connection", lambda source: FakeConnection())
    result = run(["snapshot", "--json", "--build", "--if-older-than", "7"], tmp_path)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "snapshot"
    assert payload["sources"][0]["counts"] == {"function": 1, "procedure": 1, "table": 1, "view": 1}
    assert payload["sources"][0]["skipped"] is False
    assert (tmp_path / "data-docs" / "graph.json").is_file()  # --build ran
    assert (tmp_path / "site" / "index.html").is_file()

    def never(source):
        raise AssertionError("connected although the snapshot is fresh")

    monkeypatch.setattr(live, "open_connection", never)
    again = run(["snapshot", "--json", "--if-older-than", "7"], tmp_path)
    assert again.exit_code == 0 and json.loads(again.stdout)["sources"][0]["skipped"] is True


def test_the_docs_say_which_day_dev_was_read_and_count_no_power_bi_links(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)
    built = run(["build", "--non-interactive"], tmp_path)
    assert built.exit_code == 0, built.output
    # no Power BI table in the docs: nothing to link, so no "0 cross-repo links"
    assert "lineage edges" in built.output and "cross-repo links" not in built.output
    graph = json.loads((tmp_path / "data-docs" / "graph.json").read_text(encoding="utf-8"))
    assert graph["coverage"]["sources"]["warehouse"]["live_snapshot"] == "2026-11-24"
    assert "live_snapshot" not in graph["coverage"]["sources"]["sql"]
    index = (tmp_path / "data-docs" / "index.md").read_text(encoding="utf-8")
    assert "read from the dev database on 2026-11-24" in index
    first = (tmp_path / "data-docs" / "graph.json").read_bytes()

    # a second read on the same day leaves the docs byte-identical
    take(config_path, now=NOW.replace(hour=23))
    assert run(["build", "--non-interactive"], tmp_path).exit_code == 0
    assert (tmp_path / "data-docs" / "graph.json").read_bytes() == first

    # a manifest that cannot be read shows no date at all
    (tmp_path / "live" / "warehouse" / ".coop-data-doc-snapshot.json").write_text("{", encoding="utf-8")
    assert run(["build", "--non-interactive"], tmp_path).exit_code == 0
    assert "read from the dev database" not in (tmp_path / "data-docs" / "index.md").read_text(
        encoding="utf-8"
    )


def test_cli_reports_a_missing_source_and_no_sources(tmp_path: Path):
    config_path = workspace(tmp_path)
    result = run(["snapshot", "nope"], tmp_path)
    assert result.exit_code == 1 and "No live source named 'nope'" in result.output
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text[: text.index("live_sources:")] + text[text.index("output:") :], "utf-8")
    (tmp_path / "live" / "warehouse").mkdir(parents=True)
    result = run(["snapshot"], tmp_path)
    assert result.exit_code == 1 and "No live_sources in the config" in result.output


def test_missing_driver_packages_give_one_actionable_line(tmp_path: Path, monkeypatch):
    workspace(tmp_path)
    monkeypatch.setitem(sys.modules, "pyodbc", None)  # import pyodbc -> ImportError
    result = run(["snapshot"], tmp_path)
    assert result.exit_code == 1
    assert "coop-data-doc[live]" in result.output and "Traceback" not in result.output


def test_config_set_and_show_config_keep_live_sources(tmp_path: Path):
    config_path = workspace(tmp_path)
    (tmp_path / "live" / "warehouse").mkdir(parents=True)
    result = run(["config-set", "--from-json", "-"], tmp_path, input='{"project_name": "Renamed"}')
    assert result.exit_code == 0, result.output
    config = Config.load(config_path)
    assert config.project_name == "Renamed"
    assert config.live_sources["dev_wh"].model_dump() == {
        "server": "example.datawarehouse.fabric.microsoft.com",
        "database": "Dev Warehouse",
        "repo": "warehouse",
        "environment": "dev",
        "auth": "entra",
        "tenant_id": None,
    }
    shown = json.loads(run(["show-config"], tmp_path).stdout)
    assert shown["live_sources"]["dev_wh"]["database"] == "Dev Warehouse"
    patch = {
        "live_sources": {"lake": {"server": "s", "database": "d", "repo": "warehouse", "environment": "dev"}}
    }
    result = run(["config-set", "--from-json", "-"], tmp_path, input=json.dumps(patch))
    assert result.exit_code == 0, result.output
    assert sorted(Config.load(config_path).live_sources) == ["lake"]


def test_the_pipeline_never_loads_the_database_code(tmp_path: Path):
    config_path = workspace(tmp_path)
    take(config_path)
    script = (
        "import sys\n"
        "from coop_data_doc.cli import run_pipeline\n"
        "from coop_data_doc.config import Config\n"
        f"run_pipeline(Config.load({str(config_path)!r}), interactive=False)\n"
        "bad = [m for m in ('coop_data_doc.live', 'pyodbc', 'azure.identity') if m in sys.modules]\n"
        "sys.exit(repr(bad) if bad else 0)\n"
    )
    src = str(Path(__file__).resolve().parent.parent / "src")
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
        # the whole environment: Windows cannot load its socket provider without SystemRoot
        env={**os.environ, "PYTHONPATH": src, "COOP_DATA_DOC_JOBS": "1"},
    )
    assert done.returncode == 0, done.stderr


class _Done:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_without_azure_identity_the_token_comes_from_the_azure_cli(monkeypatch):
    import shutil

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _Done(0, stdout="tok-123\n")

    monkeypatch.setitem(sys.modules, "azure.identity", None)  # import -> ImportError
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/az" if name == "az" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    source = load_source()
    source.tenant_id = "11111111-2222-3333-4444-555555555555"

    assert live._entra_token(source) == "tok-123"
    assert calls == [
        [
            "/bin/az",
            "account",
            "get-access-token",
            "--resource",
            "https://database.windows.net/",
            "--query",
            "accessToken",
            "--output",
            "tsv",
            "--tenant",
            "11111111-2222-3333-4444-555555555555",
        ]
    ]


def test_azure_cli_failures_are_one_line_and_never_show_a_token(monkeypatch):
    import shutil

    monkeypatch.setitem(sys.modules, "azure.identity", None)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(live.LiveError, match="Azure CLI"):
        live._entra_token(load_source())

    monkeypatch.setattr(shutil, "which", lambda name: "/bin/az")
    monkeypatch.setattr(
        subprocess, "run", lambda command, **kw: _Done(1, stderr="ERROR: Please run 'az login'.\nmore")
    )
    with pytest.raises(live.LiveError) as caught:
        live._entra_token(load_source())
    assert "az login" in str(caught.value) and "more" not in str(caught.value)


def test_a_token_handed_over_in_the_environment_is_used_as_is(monkeypatch):
    monkeypatch.setenv(live.TOKEN_ENV, "  handed-token\n")
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("no sign-in when a token is handed over")
    )
    assert live._entra_token(load_source()) == "handed-token"
