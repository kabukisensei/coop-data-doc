"""User-facing configuration (Module 1).

Loads and validates ``coop-data-doc.yml`` — the single file users edit to
point the tool at their SQL and Power BI repos. Also defines ParseWarning,
the structured warning type that every parser returns instead of printing.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator

VALID_LAYERS = ("bronze", "silver", "gold")


class ParseWarning(BaseModel):
    """A structured, printable warning returned (never printed) by parsers."""

    file: str
    message: str
    category: str


class ConfigError(Exception):
    """A user-facing configuration problem; the message is printable as-is."""


class RepoConfig(BaseModel):
    """One crawl root: path plus include/exclude globs (exclude wins)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=list)


class SchemaMapping(BaseModel):
    """A hint linking a SQL view schema to the semantic model it feeds."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: str = Field(alias="schema")
    model: str


class OutputConfig(BaseModel):
    """Where generated docs land: markdown dir and HTML site dir."""

    model_config = ConfigDict(extra="forbid")

    dir: str = "./data-docs"
    site_dir: str = "./data-docs-site"


class LayerRule(BaseModel):
    """Which objects belong to a medallion layer.

    A table or view is assigned this layer if its schema is in ``schemas``
    OR its source-file path matches one of the ``paths`` globs. Both lists
    are optional; an omitted layer simply isn't declared (so bronze/silver
    can each be skipped).
    """

    model_config = ConfigDict(extra="forbid")

    schemas: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class Config(BaseModel):
    """Validated coop-data-doc.yml. Relative paths resolve against the
    config file's directory, not the current working directory.
    """

    model_config = ConfigDict(extra="forbid")

    project_name: str = "Data Estate"
    repos: dict[str, RepoConfig]
    schema_mappings: list[SchemaMapping] = Field(default_factory=list)
    layers: dict[str, LayerRule] = Field(default_factory=dict)
    output: OutputConfig = Field(default_factory=OutputConfig)
    sql_dialect: str = "tsql"

    @field_validator("layers")
    @classmethod
    def _check_layer_names(cls, value: dict[str, LayerRule]) -> dict[str, LayerRule]:
        bad = sorted(set(value) - set(VALID_LAYERS))
        if bad:
            raise ValueError(f"unknown layer(s) {bad}; valid layers are {list(VALID_LAYERS)}")
        return value

    # directory of the loaded config file; relative paths resolve against it
    _base_dir: Path = PrivateAttr(default=Path("."))

    @property
    def base_dir(self) -> Path:
        """Directory containing the loaded config file."""
        return self._base_dir

    def repo_root(self, repo_key: str) -> Path:
        """Absolute root of a configured repo."""
        return (self._base_dir / self.repos[repo_key].path).resolve()

    def output_dir(self) -> Path:
        """Absolute markdown output directory."""
        return (self._base_dir / self.output.dir).resolve()

    def site_dir(self) -> Path:
        """Absolute HTML site output directory."""
        return (self._base_dir / self.output.site_dir).resolve()

    @classmethod
    def load(cls, path: Path | str) -> "Config":
        """Load and validate a config file; raises ConfigError with a
        user-printable message naming the offending key or path.
        """
        path = Path(path)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}. Run `coop-data-doc init` to create one.")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark is not None else ""
            raise ConfigError(f"Invalid YAML in {path}{where}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
        try:
            config = cls.model_validate(data)
        except ValidationError as exc:
            issues = "; ".join(
                f"'{'.'.join(str(part) for part in err['loc'])}': {err['msg']}" for err in exc.errors()
            )
            raise ConfigError(f"Invalid config in {path}: {issues}") from exc
        config._base_dir = path.resolve().parent
        for repo_key in sorted(config.repos):
            root = config.repo_root(repo_key)
            if not root.is_dir():
                raise ConfigError(f"Repo '{repo_key}' path does not exist: {root} (configured in {path})")
        return config

    @staticmethod
    def scaffold(path: Path | str) -> None:
        """Write a fully commented starter config. Refuses to overwrite."""
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"{path} already exists")
        path.write_text(
            render_config_yaml(
                project_name="Coop BI Estate",
                sql_path="../sql-repo",
                pbi_path="../pbi-repo",
                mappings=[("salespm", "Sales and Project Management")],
                layers={
                    "bronze": {"schemas": ["d365po", "d365fo"], "paths": []},
                    "silver": {"schemas": ["stg"], "paths": []},
                    "gold": {"schemas": ["dwm", "common"], "paths": ["**/dim/**", "**/fact/**"]},
                },
            ),
            encoding="utf-8",
            newline="\n",
        )


DEFAULT_SQL_INCLUDE = ["**/*.sql"]
DEFAULT_SQL_EXCLUDE = ["**/archive/**"]
DEFAULT_PBI_INCLUDE = [
    "**/*.tmdl",
    "**/*.bim",
    "**/report.json",
    "**/visual.json",
    "**/page.json",
    "**/*.pbix",
]

_CONFIG_TEMPLATE = """\
# coop-data-doc configuration
# Point the tool at your repos, then run `coop-data-doc build`.
# All relative paths resolve against the folder containing THIS file.
# Re-run `coop-data-doc setup` anytime to update this file interactively.

project_name: {project_name}

# The repos to crawl.
repos:
  sql:
    path: {sql_path}
    include: {sql_include}
    exclude: {sql_exclude}
  powerbi:
    path: {pbi_path}
    include: {pbi_include}
    exclude: {pbi_exclude}

# Hints linking SQL view schemas to the semantic models they feed.
# Anything still ambiguous is resolved interactively on first run and
# remembered in .lineage-cache.json (commit that file!).
{mappings_block}

# Medallion layers. A table/view is assigned a layer if its schema is in
# 'schemas' OR its file path matches a 'paths' glob (precedence gold >
# silver > bronze). Omit a layer to skip it; anything unmatched falls back
# to a read/write heuristic (read-only source -> silver, else gold).
{layers_block}

output:
  dir: {output_dir}        # markdown docs (for agents)
  site_dir: {site_dir}     # html portal (for humans)

# sqlglot dialect used to parse the SQL repo (tsql covers SQL Server,
# Azure SQL/serverless, and Fabric warehouse).
sql_dialect: {sql_dialect}
"""


def render_config_yaml(
    *,
    project_name: str,
    sql_path: str,
    pbi_path: str,
    mappings: list[tuple[str, str]],
    layers: dict[str, dict[str, list[str]]] | None = None,
    sql_include: list[str] | None = None,
    sql_exclude: list[str] | None = None,
    pbi_include: list[str] | None = None,
    pbi_exclude: list[str] | None = None,
    output_dir: str = "./data-docs",
    site_dir: str = "./data-docs-site",
    sql_dialect: str = "tsql",
) -> str:
    """Render a commented coop-data-doc.yml from values.

    Used by both `init` (defaults) and the `setup` wizard (entered/refreshed
    values). All scalars are JSON-quoted, which is valid YAML.
    """
    if mappings:
        lines = ["schema_mappings:"]
        for schema, model in mappings:
            lines.append(f"  - schema: {json.dumps(schema)}")
            lines.append(f"    model: {json.dumps(model)}")
        mappings_block = "\n".join(lines)
    else:
        mappings_block = "schema_mappings: []"

    declared = {
        name: rule for name, rule in (layers or {}).items() if rule.get("schemas") or rule.get("paths")
    }
    if declared:
        lines = ["layers:"]
        for name in VALID_LAYERS:  # stable order: bronze, silver, gold
            rule = declared.get(name)
            if not rule:
                continue
            lines.append(f"  {name}:")
            lines.append(f"    schemas: {json.dumps(rule.get('schemas', []))}")
            lines.append(f"    paths: {json.dumps(rule.get('paths', []))}")
        layers_block = "\n".join(lines)
    else:
        layers_block = "layers: {}"
    return _CONFIG_TEMPLATE.format(
        project_name=json.dumps(project_name),
        sql_path=json.dumps(sql_path),
        sql_include=json.dumps(sql_include if sql_include is not None else DEFAULT_SQL_INCLUDE),
        sql_exclude=json.dumps(sql_exclude if sql_exclude is not None else DEFAULT_SQL_EXCLUDE),
        pbi_path=json.dumps(pbi_path),
        pbi_include=json.dumps(pbi_include if pbi_include is not None else DEFAULT_PBI_INCLUDE),
        pbi_exclude=json.dumps(pbi_exclude if pbi_exclude is not None else []),
        mappings_block=mappings_block,
        layers_block=layers_block,
        output_dir=json.dumps(output_dir),
        site_dir=json.dumps(site_dir),
        sql_dialect=json.dumps(sql_dialect),
    )
