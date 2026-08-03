"""User-facing configuration (Module 1).

Loads and validates ``coop-data-doc.yml`` — the single file users edit to
point the tool at their SQL and Power BI repos. Also defines ParseWarning,
the structured warning type that every parser returns instead of printing.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator

DEFAULT_CONFIG = "coop-data-doc.yml"

VALID_LAYERS = ("bronze", "silver", "gold")
# Default site theme = the Cooptimize brand. Applied to every build unless the
# config's branding.* overrides it (the setup wizard prefills these). Any user
# can change them to their own colors.
DEFAULT_PRIMARY_COLOR = "#004060"  # header / nav / links
DEFAULT_ACCENT_COLOR = "#e04020"  # hover / active
# safe CSS color forms for branding (no '{', '}', ';', newlines → no injection)
_COLOR_RE = re.compile(
    r"^(#[0-9A-Fa-f]{3,8}|rgb\([\d,\s.%]+\)|rgba\([\d,\s.%]+\)|hsl\([\d,\s.%]+\)|[A-Za-z]+)$"
)


def _within_or_equal(inner: Path, outer: Path) -> bool:
    """True when ``inner`` is the same path as ``outer`` or sits inside it."""
    try:
        inner.relative_to(outer)
        return True
    except ValueError:
        return False


def output_dirs_conflict(output_dir: Path, site_dir: Path) -> bool:
    """Whether the markdown and HTML output dirs collide.

    mkdocs rebuilds the HTML site by wiping ``site_dir`` and filling it, so it
    must not be the markdown dir nor nested either way — otherwise the build
    clobbers the markdown or copies the build into itself. Expects already
    resolved absolute paths; True when they conflict.
    """
    return _within_or_equal(site_dir, output_dir) or _within_or_equal(output_dir, site_dir)


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


class Branding(BaseModel):
    """Optional company branding for the HTML site: a logo, a favicon, and
    brand colors (hex). All optional; relative paths resolve against the
    config file's folder."""

    model_config = ConfigDict(extra="forbid")

    logo: str | None = None
    favicon: str | None = None
    # default to the Cooptimize brand theme; overridable in the config / wizard
    primary_color: str | None = DEFAULT_PRIMARY_COLOR  # header / nav / links
    accent_color: str | None = DEFAULT_ACCENT_COLOR  # hover / active

    @field_validator("primary_color", "accent_color")
    @classmethod
    def _check_color(cls, value: str | None) -> str | None:
        # only safe color forms — prevents CSS injection into brand.css
        if value and not _COLOR_RE.match(value):
            raise ValueError(
                f"invalid color {value!r}; use hex (#rgb / #rrggbb / #rrggbbaa), "
                "rgb()/rgba()/hsl(), or a CSS color name"
            )
        return value


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
    ignore_schemas: list[str] = Field(default_factory=list)
    # if non-empty, ONLY these schemas are documented (the wizard writes the
    # union of the schemas checked in its layer questions); empty = no
    # restriction. ignore_schemas still wins on conflict.
    include_schemas: list[str] = Field(default_factory=list)
    branding: Branding = Field(default_factory=Branding)
    output: OutputConfig = Field(default_factory=OutputConfig)
    sql_dialect: str = "tsql"
    # review-findings JSON files (coop-sql-review / coop-dax-review
    # `--format json`) composed into the portal at render time (issue #38).
    # Paths resolve against this file's folder; `--reviews` extends the list.
    reviews: list[str] = Field(default_factory=list)

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
    def find(cls, start_dir: Path | str | None = None) -> Path | None:
        """Search for coop-data-doc.yml in start_dir and parent directories.

        Checks (in order):
        1. Environment variable COOP_DATA_DOC_CONFIG
        2. start_dir / DEFAULT_CONFIG (or cwd if start_dir is None)
        3. Walk up parent directories until found or filesystem root

        Returns the absolute path if found, None otherwise.
        """
        env_path = os.environ.get("COOP_DATA_DOC_CONFIG")
        if env_path:
            p = Path(env_path).resolve()
            if p.is_file():
                return p

        start = Path(start_dir or ".").resolve()
        if start.is_file():
            start = start.parent

        for directory in [start, *start.parents]:
            candidate = directory / DEFAULT_CONFIG
            if candidate.is_file():
                return candidate.resolve()
        return None

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
        except UnicodeDecodeError as exc:
            # e.g. PowerShell 5.1's `>` redirect writes UTF-16LE; keep the
            # contract of a user-printable ConfigError, never a raw traceback
            raise ConfigError(
                f"{path} is not UTF-8 text (was it saved as UTF-16, e.g. by a PowerShell "
                "`>` redirect?). Re-save the file as UTF-8 and retry."
            ) from exc
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
        out_dir, site = config.output_dir(), config.site_dir()
        if output_dirs_conflict(out_dir, site):
            raise ConfigError(
                "output.dir and output.site_dir must be separate folders — neither can be "
                "inside the other. mkdocs rebuilds the HTML site by wiping site_dir, which "
                "would clobber or duplicate your markdown.\n"
                f"  dir:      {config.output.dir}  ->  {out_dir}\n"
                f"  site_dir: {config.output.site_dir}  ->  {site}\n"
                "Fix: put them side by side, e.g. dir: ./data-docs and site_dir: "
                f"./data-docs-site (configured in {path})."
            )
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
                mappings=[("sales", "Sales Analytics")],
                layers={
                    "bronze": {"schemas": ["erp_orders", "erp_finance"], "paths": []},
                    "silver": {"schemas": ["stg"], "paths": []},
                    "gold": {"schemas": ["mart", "common"], "paths": ["**/dim/**", "**/fact/**"]},
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
    "**/definition.pbir",
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

# Schemas to drop entirely (never documented). System schemas (sys,
# information_schema, tempdb, guest, db_*) are always dropped automatically.
ignore_schemas: {ignore_schemas}

{include_schemas_block}# Optional company branding for the HTML site (logo/favicon paths relative
# to this file; colors as hex). Leave empty for the default theme.
{branding_block}

{reviews_block}output:
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
    ignore_schemas: list[str] | None = None,
    include_schemas: list[str] | None = None,
    branding: dict[str, str] | None = None,
    sql_include: list[str] | None = None,
    sql_exclude: list[str] | None = None,
    pbi_include: list[str] | None = None,
    pbi_exclude: list[str] | None = None,
    output_dir: str = "./data-docs",
    site_dir: str = "./data-docs-site",
    sql_dialect: str = "tsql",
    reviews: list[str] | None = None,
) -> str:
    """Render a commented coop-data-doc.yml from values.

    Used by both `init` (defaults) and the `setup` wizard (entered/refreshed
    values). All scalars are JSON-quoted, which is valid YAML. ``reviews``
    renders nothing when empty, so configs without review files are
    byte-identical to before the key existed; ``include_schemas`` likewise
    renders only when non-empty.
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

    brand = {k: v for k, v in (branding or {}).items() if v}
    if brand:
        lines = ["branding:"]
        for key in ("logo", "favicon", "primary_color", "accent_color"):
            if brand.get(key):
                lines.append(f"  {key}: {json.dumps(brand[key])}")
        branding_block = "\n".join(lines)
    else:
        branding_block = "branding: {}"

    if include_schemas:
        include_schemas_block = (
            "# Schemas to document. Non-empty = ONLY these schemas appear in the docs\n"
            "# (empty = every schema not dropped by ignore_schemas above).\n"
            f"include_schemas: {json.dumps(include_schemas)}\n\n"
        )
    else:
        include_schemas_block = ""

    if reviews:
        lines = [
            "# Review-findings JSON files (coop-sql-review / coop-dax-review",
            "# `check --format json` output) composed into the portal at build time.",
            "# Paths are relative to this file; `--reviews` on build/check extends the list.",
            "reviews:",
        ]
        lines.extend(f"  - {json.dumps(p)}" for p in reviews)
        reviews_block = "\n".join(lines) + "\n\n"
    else:
        reviews_block = ""

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
        ignore_schemas=json.dumps(ignore_schemas or []),
        include_schemas_block=include_schemas_block,
        branding_block=branding_block,
        output_dir=json.dumps(output_dir),
        site_dir=json.dumps(site_dir),
        sql_dialect=json.dumps(sql_dialect),
        reviews_block=reviews_block,
    )
