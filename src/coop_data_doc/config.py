"""User-facing configuration (Module 1).

Loads and validates ``coop-data-doc.yml`` — the single file users edit to
point the tool at their SQL and Power BI repos. Also defines ParseWarning,
the structured warning type that every parser returns instead of printing.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError


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


class Config(BaseModel):
    """Validated coop-data-doc.yml. Relative paths resolve against the
    config file's directory, not the current working directory.
    """
    model_config = ConfigDict(extra="forbid")

    project_name: str = "Data Estate"
    repos: dict[str, RepoConfig]
    schema_mappings: list[SchemaMapping] = Field(default_factory=list)
    output: OutputConfig = Field(default_factory=OutputConfig)
    sql_dialect: str = "tsql"

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
            raise ConfigError(
                f"Config file not found: {path}. Run `coop-data-doc init` to create one."
            )
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
                f"'{'.'.join(str(part) for part in err['loc'])}': {err['msg']}"
                for err in exc.errors()
            )
            raise ConfigError(f"Invalid config in {path}: {issues}") from exc
        config._base_dir = path.resolve().parent
        for repo_key in sorted(config.repos):
            root = config.repo_root(repo_key)
            if not root.is_dir():
                raise ConfigError(
                    f"Repo '{repo_key}' path does not exist: {root} (configured in {path})"
                )
        return config

    @staticmethod
    def scaffold(path: Path | str) -> None:
        """Write a fully commented starter config. Refuses to overwrite."""
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"{path} already exists")
        path.write_text(SCAFFOLD_TEMPLATE, encoding="utf-8")


SCAFFOLD_TEMPLATE = """\
# coop-data-doc configuration
# Point the tool at your repos, then run `coop-data-doc build`.
# All relative paths resolve against the folder containing THIS file.

project_name: Coop BI Estate

# The repos to crawl.
repos:
  sql:
    path: ../sql-repo
    include: ["**/*.sql"]
    exclude: ["**/archive/**"]
  powerbi:
    path: ../pbi-repo
    include: ["**/*.tmdl", "**/*.bim", "**/report.json", "**/visual.json", "**/*.pbix"]
    exclude: []

# Hints linking SQL view schemas to the semantic models they feed.
# Anything still ambiguous is resolved interactively on first run and
# remembered in .lineage-cache.json (commit that file!).
schema_mappings:
  - schema: salespm
    model: "Sales and Project Management"

output:
  dir: ./data-docs            # markdown docs (for agents)
  site_dir: ./data-docs-site  # html portal (for humans)

# sqlglot dialect used to parse the SQL repo (tsql covers SQL Server,
# Azure SQL/serverless, and Fabric warehouse).
sql_dialect: tsql
"""
