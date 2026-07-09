"""Interactive configuration wizard (`coop-data-doc setup`).

Walks the user through every config value, prefilling defaults from an
existing coop-data-doc.yml when present (so re-running setup edits rather
than starts over), writes the file, and reloads it through Config.load to
validate. Nothing is written until the very end, so Ctrl-C is always safe.

Terminal I/O is allowed here (like cli.py and linker/interactive.py);
everything else in the codebase stays pure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import questionary
import yaml

if TYPE_CHECKING:  # annotation-only; runtime import stays lazy inside the function
    from coop_data_doc.graph.model import LineageGraph
    from coop_data_doc.linker.resolver import ResolutionResult

from coop_data_doc.config import (
    Config,
    ConfigError,
    output_dirs_conflict,
    render_config_yaml,
    DEFAULT_ACCENT_COLOR,
    DEFAULT_PBI_INCLUDE,
    DEFAULT_PRIMARY_COLOR,
    DEFAULT_SQL_INCLUDE,
    VALID_LAYERS,
)
from coop_data_doc.folders import (
    excludes_for_skips,
    split_excludes,
    top_level_folders as _top_level_folders,
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


def _ask_folders_to_skip(
    repo_label: str,
    repo_rel_path: str,
    base_dir: Path,
    existing_exclude: list[str],
    csv_message: str,
) -> list[str]:
    """Pick which top-level folders to document via a checkbox, returning the
    exclude globs for the ones left unchecked.

    Everything starts checked (= documented); unchecking a folder writes a
    ``**/Name/**`` skip glob for it. Hand-written excludes that don't map to a
    detected top-level folder (e.g. nested ``**/BACKUP/**``) are preserved
    untouched. Falls back to the comma-separated text prompt when the repo
    isn't on disk yet or has no subfolders, so behavior is unchanged there.
    """
    repo_abs = (base_dir / Path(repo_rel_path).expanduser()).resolve()
    folders = _top_level_folders(repo_abs)
    if not folders:
        return _ask_csv(csv_message, existing_exclude)

    # simple folder-globs become unchecked boxes; everything else is a custom pattern
    excluded_names, custom = split_excludes(folders, existing_exclude)

    choices = [
        questionary.Choice(title=name, value=name, checked=name not in excluded_names) for name in folders
    ]
    selected = _ask(
        questionary.checkbox(
            f"{repo_label} — pick the folders to document "
            "(everything is checked; SPACE to uncheck a folder to skip, ENTER to confirm):",
            choices=choices,
        )
    )
    skipped = {name for name in folders if name not in set(selected)}
    return excludes_for_skips(folders, skipped, custom)


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


_MODEL_FOLDER_RE = re.compile(r"(?:\*\*/)?(?P<name>[^/*]+\.SemanticModel)/", re.IGNORECASE)


def _discover_semantic_models(pbi_abs: Path) -> list[str]:
    """Sorted names of every ``*.SemanticModel`` folder under the PBI repo."""
    if not pbi_abs.is_dir():
        return []
    return sorted({p.name for p in pbi_abs.rglob("*.SemanticModel") if p.is_dir()}, key=str.lower)


def _semantic_model_includes(model_folders: list[str]) -> list[str]:
    """Include globs scoped to the chosen ``.SemanticModel`` folders, plus all
    reports (which nest under whichever model they draw from) AND ``.pbix``
    files — the latter matching the shipped ``DEFAULT_PBI_INCLUDE`` so a
    wizard-scoped config documents the same pbix-only reports a default ``init``
    config would (the pbix parser is best-effort/warning-driven, so an
    unreadable one degrades to a diagnostic, not noise). Only ``.pbip`` and
    other loose files are left out. fnmatch's ``*`` crosses ``/``, so
    ``**/<name>.SemanticModel/**/*.tmdl`` matches however deep the folder sits."""
    globs: list[str] = []
    for folder in model_folders:
        globs.append(f"**/{folder}/**/*.tmdl")
        globs.append(f"**/{folder}/**/*.bim")
    globs += ["**/report.json", "**/visual.json", "**/page.json", "**/definition.pbir", "**/*.pbix"]
    return globs


def _previously_selected_models(includes: list[str] | None) -> set[str]:
    """The ``.SemanticModel`` folder names an existing config's include globs
    already scope to, so re-running setup pre-checks the same models."""
    found: set[str] = set()
    for glob in includes or []:
        match = _MODEL_FOLDER_RE.match(glob)
        if match:
            found.add(match.group("name"))
    return found


def _ask_semantic_models(pbi_abs: Path, existing_includes: list[str] | None) -> list[str] | None:
    """Let the user pick which ``.SemanticModel`` folders to document. Returns the
    selected folder names, or None when none are found on disk (the repo isn't
    cloned yet, or it has no TMDL models) so the caller falls back to manual
    include globs."""
    found = _discover_semantic_models(pbi_abs)
    if not found:
        return None
    print(
        f"\nFound {len(found)} semantic model folder(s) in the Power BI repo. Pick which to\n"
        "document — only the selected *.SemanticModel folders are crawled (reports and\n"
        ".pbix files are still included; .pbip / other loose files are left out).",
        file=sys.stderr,
    )
    prev = _previously_selected_models(existing_includes)
    while True:
        choices = [questionary.Choice(name, checked=(name in prev) if prev else True) for name in found]
        selected = _ask(
            questionary.checkbox(
                "Semantic models to include (Space toggles, Enter confirms):",
                choices=choices,
            )
        )
        if selected:
            return list(selected)
        # An empty selection silently meaning "document everything" is
        # surprising (the opposite of what unchecking implies) — re-ask instead.
        # _ask still raises KeyboardInterrupt on Ctrl-C, so this stays escapable.
        print("  Select at least one semantic model (or press Ctrl-C to cancel).", file=sys.stderr)


def _ask_manual_schema(default: str) -> str | None:
    raw = str(_ask(questionary.text("SQL schema this model reads from:", default=default))).strip()
    return raw or None


def _autosuggest_mappings(
    *,
    base_dir: Path,
    project_name: str,
    sql_path: str,
    pbi_path: str,
    sql_include: list[str],
    sql_exclude: list[str],
    pbi_include: list[str],
    pbi_exclude: list[str],
    layers: dict[str, dict[str, list[str]]],
    ignore_schemas: list[str],
    sql_dialect: str,
    mappings: list[tuple[str, str]],
) -> list[tuple[str, str]] | None:
    """Dry-run the pipeline and propose the schema_mappings actually needed to
    link Power BI tables to their SQL sources, deriving each from where the
    unresolved tables' object names really live (confirm, don't type). Returns
    the final ``[(schema, model)]`` list, or ``None`` when the repos aren't on
    disk yet so the caller falls back to manual entry. ``mappings`` seeds the
    working set (prefilled/kept rows).
    """
    import logging
    from collections import Counter

    from coop_data_doc.config import Config
    from coop_data_doc.graph.model import NodeType, normalize_identifier

    sql_abs = (base_dir / Path(sql_path).expanduser()).resolve()
    pbi_abs = (base_dir / Path(pbi_path).expanduser()).resolve()
    if not (sql_abs.is_dir() and pbi_abs.is_dir()):
        return None  # nothing to scan; caller does manual entry

    from coop_data_doc.cli import build_graph, resolve_graph  # lazy: avoid the wizard<->cli cycle
    from coop_data_doc.progress import Progress, should_enable

    # Drive the dry-run's own crawl/parse/link progress on stderr so the scan
    # shows a spinner and bars instead of sitting silent after the message below
    # (disabled automatically when stderr isn't a TTY, e.g. under tests/CI).
    scan_progress = Progress(should_enable(quiet=False))

    def resolve(working: list[tuple[str, str]]) -> tuple[LineageGraph, ResolutionResult]:
        """Resolve the parsed estate against one mapping set, on a throwaway copy
        so ``parsed`` stays pristine for the next call. Only the (cheap) link
        step re-runs — the crawl/parse happens once, above."""
        graph = parsed.model_copy(deep=True)
        result, _ = resolve_graph(graph, build_config(working), interactive=False, progress=scan_progress)
        return graph, result

    def build_config(working: list[tuple[str, str]]) -> Config:
        rendered = render_config_yaml(
            project_name=project_name,
            sql_path=sql_path,
            pbi_path=pbi_path,
            mappings=working,
            layers=layers,
            ignore_schemas=ignore_schemas,
            branding={},
            sql_include=sql_include,
            sql_exclude=sql_exclude,
            pbi_include=pbi_include,
            pbi_exclude=pbi_exclude,
            sql_dialect=sql_dialect,
        )
        config = Config.model_validate(yaml.safe_load(rendered))
        config._base_dir = base_dir
        return config

    logging.getLogger("sqlglot").setLevel(logging.ERROR)  # the dry-run shouldn't spam parse notes
    print(
        "\nScanning your repos to connect Power BI tables to their SQL sources (read-only, a few seconds)…",
        file=sys.stderr,
    )
    # Parse the estate ONCE; both the suggest pass here and the verify pass below
    # re-link this same parsed graph rather than re-crawling and re-parsing.
    parsed, _ = build_graph(build_config(mappings), progress=scan_progress)
    graph, result = resolve(mappings)

    # index: normalized SQL object name -> set of schemas that contain it
    obj_schemas: dict[str, set[str]] = {}
    model_label: dict[str, str] = {}
    for node in graph.nodes.values():
        if node.node_type in (NodeType.VIEW, NodeType.GOLD_TABLE, NodeType.SILVER_TABLE):
            obj_schemas.setdefault(node.name, set()).add(node.schema_name)
        elif node.node_type is NodeType.SEMANTIC_MODEL:
            model_label[node.name] = node.display

    # unresolved Power BI tables, grouped by model -> the object names they read
    by_model: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        if node.node_type is not NodeType.PBI_TABLE or not node.metadata.get("unresolved"):
            continue
        source = node.metadata.get("partition_source") or {}
        objects = (
            [source["object"]]
            if source.get("object")
            else [q.split(".", 1)[-1] for q in (node.metadata.get("native_query_tables") or [])]
        )
        for obj in objects:
            if obj:
                by_model.setdefault(node.schema_name, []).append(normalize_identifier(obj))

    linked = sum(result.methods.get(m, 0) for m in ("exact", "config_rule", "cache", "fuzzy"))
    print(f"  ✓ {linked} Power BI table(s) linked to SQL automatically.", file=sys.stderr)
    if not by_model:
        print("  Nothing else needs a schema mapping.", file=sys.stderr)
        return list(mappings)

    pending = sum(len(set(v)) for v in by_model.values())
    print(
        f"  {pending} table(s) in {len(by_model)} model(s) didn't match a SQL object — let's connect them:",
        file=sys.stderr,
    )

    def candidates(objects: list[str]) -> list[tuple[str, int]]:
        votes: Counter[str] = Counter()
        for obj in set(objects):
            for schema in obj_schemas.get(obj, ()):
                votes[schema] += 1
        return sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))

    for model_key in sorted(by_model):
        label = model_label.get(model_key, model_key)
        remaining = sorted(set(by_model[model_key]))
        while remaining:
            ranked = candidates(remaining)
            if not ranked:
                print(
                    f"  • {label}: {len(remaining)} table(s) match no SQL object by name "
                    "— likely external or renamed; left unresolved.",
                    file=sys.stderr,
                )
                break
            total = len(remaining)
            top_schema = ranked[0][0]
            single = len(ranked) == 1 or ranked[0][1] > ranked[1][1]
            chosen: str | None
            if single:
                if _ask(
                    questionary.confirm(
                        f"Model '{label}' has {total} unresolved table(s); their names live in "
                        f"SQL schema '{top_schema}'. Map {label} → {top_schema}?",
                        default=True,
                        auto_enter=False,
                    )
                ):
                    chosen = top_schema
                else:
                    chosen = _ask_manual_schema(top_schema)
            else:
                options = [questionary.Choice(f"{s} — covers {c}/{total}", value=s) for s, c in ranked]
                options.append(questionary.Choice("Type a schema name myself", value="__manual__"))
                options.append(questionary.Choice("Skip this model", value="__skip__"))
                picked = str(
                    _ask(
                        questionary.select(
                            f"Model '{label}' has {total} unresolved tables across schemas — "
                            "pick which to map:",
                            choices=options,
                        )
                    )
                )
                chosen = (
                    None
                    if picked == "__skip__"
                    else _ask_manual_schema(top_schema)
                    if picked == "__manual__"
                    else picked
                )
            if not chosen:
                break
            new_remaining = [obj for obj in remaining if chosen not in obj_schemas.get(obj, ())]
            if len(new_remaining) == len(remaining):
                # A manually-typed schema that covers none of the remaining
                # tables makes no progress: re-prompting would loop forever and
                # appending it would pollute the config with a bogus mapping.
                # Warn and stop rather than spin.
                print(
                    f"  • {label}: schema '{chosen}' covers none of the remaining "
                    f"{len(remaining)} table(s); left unresolved.",
                    file=sys.stderr,
                )
                break
            mappings.append((chosen, label))
            remaining = new_remaining

    # verify re-scan: re-link (no re-parse) with the new mappings so the user
    # knows they took — reuses the graph parsed once at the top.
    print("\n  Re-checking with your mappings…", file=sys.stderr)
    _, verified = resolve(mappings)
    left = len(verified.unresolved)
    if left == 0:
        print("  ✓ All Power BI tables now link to a SQL object.", file=sys.stderr)
    else:
        print(
            f"  {left} table(s) still unresolved (external/renamed, or no schema rule fits) — "
            "map them per-table on build, or mark them external.",
            file=sys.stderr,
        )
    return list(mappings)


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

    # These two prompts are the step users most often click past without
    # understanding — spell out that a *folder path* is expected, with examples.
    print(
        "\nNext, point the tool at your two source folders (the repos cloned on\n"
        "this computer). For each, enter the path to the folder — for example\n"
        "  /Users/you/code/sql-warehouse   or   C:\\Users\\you\\code\\PowerBI\n"
        "Relative paths are resolved against the config file's folder.\n",
        file=sys.stderr,
    )
    sql_path = _ask_repo_path(
        "SQL repo path — the folder with your procs, tables, views",
        _repo_default(existing, "sql", "../sql-repo"),
        base_dir,
    )
    pbi_path = _ask_repo_path(
        "Power BI repo path — the folder with your semantic models and reports",
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
        "  INCLUDE = the file types to document (press Enter to keep the sensible default).\n"
        "  SKIP    = uncheck any top-level folders to leave out (e.g. backups). When the repo\n"
        "            isn't on disk yet, you type skip globs instead. Keeping everything is fine.",
        file=sys.stderr,
    )
    sql_include = _ask_csv(
        "SQL — files/patterns to INCLUDE (comma-separated globs):",
        sql_repo.include if sql_repo else DEFAULT_SQL_INCLUDE,
    )
    sql_exclude = _ask_folders_to_skip(
        "SQL",
        sql_path,
        base_dir,
        sql_repo.exclude if sql_repo else [],
        "SQL — folders to SKIP (optional, e.g. **/archive/**, **/Deployment/** — blank for none):",
    )
    # If the PBI repo is on disk, let the user pick which .SemanticModel folders
    # to document (scoping the include globs to those + reports); otherwise fall
    # back to manual include globs.
    pbi_abs = (base_dir / Path(pbi_path).expanduser()).resolve()
    selected_models = _ask_semantic_models(pbi_abs, pbi_repo.include if pbi_repo else None)
    if selected_models is not None:
        # The model picker IS the "what to document" decision for an on-disk PBI
        # repo, so DON'T also show the top-level-folder skip checkbox (issue #22):
        # to a user it reads as the same question asked twice, and because exclude
        # beats include, unchecking the folder the models live in would silently
        # drop the models just picked. Preserve any hand-written excludes from a
        # loaded config untouched.
        pbi_include = _semantic_model_includes(selected_models)
        pbi_exclude = list(pbi_repo.exclude) if pbi_repo else []
    else:
        # Fallback (repo not cloned yet / no .SemanticModel folders): the folder
        # checkbox is the only scoping mechanism here, so keep it.
        pbi_include = _ask_csv(
            "Power BI — files/patterns to INCLUDE (comma-separated globs):",
            pbi_repo.include if pbi_repo else DEFAULT_PBI_INCLUDE,
        )
        pbi_exclude = _ask_folders_to_skip(
            "Power BI",
            pbi_path,
            base_dir,
            pbi_repo.exclude if pbi_repo else [],
            "Power BI — folders to SKIP (optional, e.g. **/BACKUP/**, **/Documentation/**, "
            "**/Editor and Theme Files/** — blank for none):",
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

    print("\n── Schemas to drop ──", file=sys.stderr)
    print(
        "  Every object in the included files is documented. The layer schemas above only\n"
        "  group/colour objects — they don't restrict what's shown — so this is the one place\n"
        "  to drop schemas you never want (noise like staging/temp). Blank keeps everything.",
        file=sys.stderr,
    )
    ignore_schemas = _ask_csv(
        "Schemas to DROP from the docs (comma-separated, e.g. staging, tmp — blank "
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
    # colors default to the Cooptimize brand theme; press Enter to keep it
    primary = str(
        _ask(
            questionary.text(
                "Primary brand color (hex; default = Cooptimize navy):",
                default=(
                    existing_brand.primary_color
                    if existing_brand and existing_brand.primary_color
                    else DEFAULT_PRIMARY_COLOR
                ),
            )
        )
    ).strip()
    if primary:
        branding["primary_color"] = primary
    accent = str(
        _ask(
            questionary.text(
                "Accent color (hex; default = Cooptimize red-orange):",
                default=(
                    existing_brand.accent_color
                    if existing_brand and existing_brand.accent_color
                    else DEFAULT_ACCENT_COLOR
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
    print("\n── Power BI: connecting tables to their SQL sources ──", file=sys.stderr)
    sql_dialect = existing.sql_dialect if existing else "tsql"
    mappings: list[tuple[str, str]] = []
    if existing is not None and existing.schema_mappings:
        current = ", ".join(f"{m.schema_name} → {m.model}" for m in existing.schema_mappings)
        keep = _ask(
            questionary.confirm(f"Keep existing schema mappings ({current})?", default=True, auto_enter=False)
        )
        if keep:
            mappings = [(m.schema_name, m.model) for m in existing.schema_mappings]

    # When the repos are on disk, dry-run the pipeline and auto-derive the schema
    # mappings that are actually needed (confirm, don't type). Falls back to
    # manual entry when the repos aren't cloned yet.
    auto = _autosuggest_mappings(
        base_dir=base_dir,
        project_name=project_name,
        sql_path=sql_path,
        pbi_path=pbi_path,
        sql_include=sql_include,
        sql_exclude=sql_exclude,
        pbi_include=pbi_include,
        pbi_exclude=pbi_exclude,
        layers=layers,
        ignore_schemas=ignore_schemas,
        sql_dialect=sql_dialect,
        mappings=mappings,
    )
    if auto is not None:
        mappings = auto
    else:
        while _ask(
            questionary.confirm(
                "Add a view-schema → semantic-model mapping?", default=not mappings, auto_enter=False
            )
        ):
            schema = str(_ask(questionary.text("SQL schema (e.g. mart):"))).strip()
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
        sql_dialect=sql_dialect,
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
