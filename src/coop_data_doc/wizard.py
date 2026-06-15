"""Interactive configuration wizard (`coop-data-doc setup`).

Walks the user through every config value, prefilling defaults from an
existing coop-data-doc.yml when present (so re-running setup edits rather
than starts over), writes the file, and reloads it through Config.load to
validate. Nothing is written until the very end, so Ctrl-C is always safe.

Terminal I/O is allowed here (like cli.py and linker/interactive.py);
everything else in the codebase stays pure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import questionary
import yaml

from coop_data_doc.config import (
    Config,
    ConfigError,
    output_dirs_conflict,
    render_config_yaml,
    DEFAULT_PBI_INCLUDE,
    DEFAULT_SQL_INCLUDE,
    VALID_LAYERS,
)


def _sibling_site(output_dir: str) -> str:
    """A sensible HTML-site default that sits NEXT TO the markdown dir, never
    inside it (mkdocs refuses a site_dir nested in docs_dir)."""
    trimmed = output_dir.rstrip("/\\") or "./data-docs"
    return f"{trimmed}-site"


def _ask(prompt) -> object:
    """Run a questionary prompt; Ctrl-C/EOF becomes KeyboardInterrupt.

    questionary returns None on EOF and may also raise; normalize both to
    KeyboardInterrupt so the CLI shows the correct 'cancelled' message.
    """
    try:
        answer = prompt.ask()
    except EOFError as exc:
        raise KeyboardInterrupt from exc
    if answer is None:
        raise KeyboardInterrupt
    return answer


def _ask_csv(message: str, default: list[str]) -> list[str]:
    """Prompt for a comma-separated list; blank returns []."""
    raw = str(_ask(questionary.text(message, default=", ".join(default)))).strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _ask_repo_path(label: str, default: str, base_dir: Path) -> str:
    """Prompt for a repo path until it exists (or the user opts to keep it)."""
    while True:
        raw = str(_ask(questionary.path(f"{label}:", default=default, only_directories=True))).strip()
        if not raw:
            continue
        resolved = (base_dir / Path(raw).expanduser()).resolve()
        if resolved.is_dir():
            return raw
        keep = _ask(
            questionary.confirm(
                f"'{resolved}' doesn't exist (yet). Use it anyway?",
                default=False,
                auto_enter=False,
            )
        )
        if keep:
            return raw


def _existing_config(config_path: Path) -> Config | None:
    """Previous config for prefilling, loaded leniently.

    Strict loading fails when a repo path doesn't exist — which is exactly
    the 'saved but not runnable yet' state setup itself creates — so fall
    back to validating the YAML without the path-existence check rather
    than discarding the user's saved answers.
    """
    if not config_path.is_file():
        return None
    try:
        return Config.load(config_path)
    except ConfigError as exc:
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            lenient = Config.model_validate(data)
        except Exception:
            print(
                f"note: existing config could not be read ({exc}); starting fresh",
                file=sys.stderr,
            )
            return None
        print(
            f"note: existing config isn't runnable yet ({exc}) — your saved values are prefilled anyway",
            file=sys.stderr,
        )
        return lenient


def _repo_default(existing: Config | None, key: str, fallback: str) -> str:
    if existing is not None and key in existing.repos:
        return existing.repos[key].path
    return fallback


def run_setup(config_path: Path) -> Config | None:
    """Run the wizard, write the config, and return the validated result.

    Returns None when the file was saved but doesn't validate yet (e.g. the
    user pointed at a repo they haven't cloned and chose 'use it anyway').
    """
    config_path = Path(config_path).resolve()
    base_dir = config_path.parent
    existing = _existing_config(config_path)
    if existing is not None:
        print(f"Updating {config_path} (current values shown as defaults)", file=sys.stderr)

    project_name = (
        str(
            _ask(
                questionary.text(
                    "Project name (shown as the docs site title):",
                    default=existing.project_name if existing else "Coop BI Estate",
                )
            )
        ).strip()
        or "Coop BI Estate"
    )

    sql_path = _ask_repo_path(
        "SQL repo path (procs, tables, views)",
        _repo_default(existing, "sql", "../sql-repo"),
        base_dir,
    )
    pbi_path = _ask_repo_path(
        "Power BI repo path (semantic models, reports)",
        _repo_default(existing, "powerbi", "../pbi-repo"),
        base_dir,
    )

    output_dir = (
        str(
            _ask(
                questionary.text(
                    "Markdown output folder:",
                    default=existing.output.dir if existing else "./data-docs",
                )
            )
        ).strip()
        or "./data-docs"
    )
    # The HTML site is rebuilt by wiping its folder, so it must sit beside the
    # markdown dir, never inside it. Default to a sibling and reject a conflict.
    site_default = existing.output.site_dir if existing else _sibling_site(output_dir)
    while True:
        site_dir = (
            str(
                _ask(
                    questionary.text(
                        "HTML site output folder (must be separate from the markdown folder):",
                        default=site_default,
                    )
                )
            ).strip()
            or site_default
        )
        out_abs = (base_dir / Path(output_dir).expanduser()).resolve()
        site_abs = (base_dir / Path(site_dir).expanduser()).resolve()
        if not output_dirs_conflict(out_abs, site_abs):
            break
        print(
            "  ✗ The HTML site folder can't be the same as — or inside — the markdown\n"
            "    folder. Each build wipes the site folder, which would clobber your\n"
            f"    markdown. Try a sibling like '{_sibling_site(output_dir)}'.",
            file=sys.stderr,
        )
        site_default = _sibling_site(output_dir)

    # --- what to document (include) and skip (exclude), per repo ---
    sql_repo = existing.repos.get("sql") if existing else None
    pbi_repo = existing.repos.get("powerbi") if existing else None
    print("\n── What to document ──", file=sys.stderr)
    print(
        "  INCLUDE = the files to document (press Enter to keep the sensible default).\n"
        "  SKIP    = optional folders to leave out (e.g. backups). Blank keeps everything.",
        file=sys.stderr,
    )
    sql_include = _ask_csv(
        "SQL — files/patterns to INCLUDE (comma-separated globs):",
        sql_repo.include if sql_repo else DEFAULT_SQL_INCLUDE,
    )
    sql_exclude = _ask_csv(
        "SQL — folders to SKIP (optional, e.g. **/archive/**, **/Deployment/** — blank for none):",
        sql_repo.exclude if sql_repo else [],
    )
    pbi_include = _ask_csv(
        "Power BI — files/patterns to INCLUDE (comma-separated globs):",
        pbi_repo.include if pbi_repo else DEFAULT_PBI_INCLUDE,
    )
    pbi_exclude = _ask_csv(
        "Power BI — folders to SKIP (optional, e.g. **/BACKUP/**, **/Documentation/**, "
        "**/Editor and Theme Files/** — blank for none):",
        pbi_repo.exclude if pbi_repo else [],
    )

    # --- medallion layers: assign by SCHEMA (the common case) ---
    print("\n── Medallion layers ──", file=sys.stderr)
    print(
        "  Assign each layer by SCHEMA name. In a Fabric/SQL warehouse the schema IS\n"
        "  the folder, so schemas alone are all you need. Leave a layer blank to skip it.",
        file=sys.stderr,
    )
    layer_schemas: dict[str, list[str]] = {}
    for layer in VALID_LAYERS:  # bronze, silver, gold
        existing_rule = existing.layers.get(layer) if existing else None
        layer_schemas[layer] = _ask_csv(
            f"{layer.capitalize()} layer — schemas (comma-separated, e.g. "
            + (
                "erp_orders, erp_finance"
                if layer == "bronze"
                else "mart, common, silver"
                if layer == "gold"
                else "stg"
            )
            + ", or blank to skip):",
            existing_rule.schemas if existing_rule else [],
        )

    # Folder-based layering is an advanced fallback for repos where a layer is
    # a directory rather than a schema. Most repos don't need it, so it's off
    # by default — but re-running setup keeps any folder rules you already had.
    had_paths = bool(existing and any(rule.paths for rule in existing.layers.values()))
    layer_paths: dict[str, list[str]] = {}
    if _ask(
        questionary.confirm(
            "Advanced: does any layer map to a FOLDER instead of a schema?",
            default=had_paths,
            auto_enter=False,
        )
    ):
        for layer in VALID_LAYERS:
            existing_rule = existing.layers.get(layer) if existing else None
            layer_paths[layer] = _ask_csv(
                f"{layer.capitalize()} layer — folder globs (comma-separated, e.g. "
                + ("**/dim/**, **/fact/**" if layer == "gold" else "**/Bronze/**")
                + ", or blank):",
                existing_rule.paths if existing_rule else [],
            )

    layers: dict[str, dict[str, list[str]]] = {}
    for layer in VALID_LAYERS:
        schemas, paths = layer_schemas.get(layer, []), layer_paths.get(layer, [])
        if schemas or paths:
            layers[layer] = {"schemas": schemas, "paths": paths}

    ignore_schemas = _ask_csv(
        "Schemas to IGNORE entirely / skip (comma-separated, e.g. staging, sm — blank "
        "for none; system schemas like sys/information_schema are always dropped):",
        existing.ignore_schemas if existing else [],
    )

    # --- optional company branding for the HTML site ---
    existing_brand = existing.branding if existing else None
    print("\n── Branding (optional — blank to skip) ──", file=sys.stderr)
    branding: dict[str, str] = {}
    logo = str(
        _ask(
            questionary.text(
                "Logo image path (shown in the site header; relative to this config):",
                default=(existing_brand.logo if existing_brand and existing_brand.logo else ""),
            )
        )
    ).strip()
    if logo:
        branding["logo"] = logo
    primary = str(
        _ask(
            questionary.text(
                "Primary brand color (hex, e.g. #004060):",
                default=(
                    existing_brand.primary_color if existing_brand and existing_brand.primary_color else ""
                ),
            )
        )
    ).strip()
    if primary:
        branding["primary_color"] = primary
    accent = str(
        _ask(
            questionary.text(
                "Accent color (hex, e.g. #e04020):",
                default=(
                    existing_brand.accent_color if existing_brand and existing_brand.accent_color else ""
                ),
            )
        )
    ).strip()
    if accent:
        branding["accent_color"] = accent
    # carry an existing favicon through unchanged (not prompted separately)
    if existing_brand and existing_brand.favicon:
        branding["favicon"] = existing_brand.favicon

    # --- schema → semantic-model hints ---
    print("\n── Power BI: which view schema feeds which model ──", file=sys.stderr)
    mappings: list[tuple[str, str]] = []
    if existing is not None and existing.schema_mappings:
        current = ", ".join(f"{m.schema_name} → {m.model}" for m in existing.schema_mappings)
        keep = _ask(
            questionary.confirm(f"Keep existing schema mappings ({current})?", default=True, auto_enter=False)
        )
        if keep:
            mappings = [(m.schema_name, m.model) for m in existing.schema_mappings]

    while _ask(
        questionary.confirm(
            "Add a view-schema → semantic-model mapping?", default=not mappings, auto_enter=False
        )
    ):
        schema = str(_ask(questionary.text("View schema (e.g. sales):"))).strip()
        model = str(_ask(questionary.text("Semantic model it feeds (e.g. Sales Analytics):"))).strip()
        if schema and model:
            mappings.append((schema, model))

    rendered = render_config_yaml(
        project_name=project_name,
        sql_path=sql_path,
        pbi_path=pbi_path,
        mappings=mappings,
        layers=layers,
        ignore_schemas=ignore_schemas,
        branding=branding,
        sql_include=sql_include,
        sql_exclude=sql_exclude,
        pbi_include=pbi_include,
        pbi_exclude=pbi_exclude,
        output_dir=output_dir,
        site_dir=site_dir,
        sql_dialect=existing.sql_dialect if existing else "tsql",
    )
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(rendered, encoding="utf-8", newline="\n")
    except OSError as exc:
        # distinct from questionary's no-TTY OSError: report as a config problem
        raise ConfigError(f"could not write {config_path}: {exc}") from exc
    try:
        return Config.load(config_path)  # refresh: reload + validate what was written
    except ConfigError as exc:
        print(f"Saved, but not runnable yet: {exc}", file=sys.stderr)
        return None
