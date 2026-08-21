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
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:  # annotation-only; runtime import stays lazy inside the function
    from coop_data_doc.graph.model import LineageGraph
    from coop_data_doc.linker.resolver import ResolutionResult

from coop_data_doc.config import (
    DEFAULT_ACCENT_COLOR,
    DEFAULT_PBI_INCLUDE,
    DEFAULT_PRIMARY_COLOR,
    DEFAULT_SQL_INCLUDE,
    VALID_LAYERS,
    Config,
    ConfigError,
    output_dirs_conflict,
    render_config_yaml,
)
from coop_data_doc.folders import (
    base_patterns_from_includes,
    folder_scoped_includes,
    includes_for_folders,
    split_excludes,
)
from coop_data_doc.folders import (
    top_level_folders as _top_level_folders,
)
from coop_data_doc.wizard_io import Choice as WizardChoice
from coop_data_doc.wizard_io import WizardIO


def _sibling_site(output_dir: str) -> str:
    """A sensible HTML-site default that sits NEXT to the markdown dir, never
    inside it (mkdocs refuses a site_dir nested in docs_dir)."""
    trimmed = output_dir.rstrip("/\\") or "./data-docs"
    return f"{trimmed}-site"


def _ask_csv(io: WizardIO, message: str, default: list[str]) -> list[str]:
    """Prompt for a comma-separated list; blank returns []."""
    raw = io.text("csv", message, default=", ".join(default)).strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _ask_folders_to_document(
    io: WizardIO,
    repo_label: str,
    repo_rel_path: str,
    base_dir: Path,
    existing_include: list[str] | None,
    base_patterns: list[str],
    csv_message: str,
) -> list[str]:
    """Pick which top-level folders to document via a checkbox, returning the
    folder-scoped include globs for the checked ones.

    Folder selection is an ALLOWLIST: nothing starts checked, and checking a
    folder writes ``Folder/<base-pattern>`` include globs for it (base patterns
    are the repo's ``**/``-rooted file-type templates, e.g. ``**/*.sql`` →
    ``Foo/**/*.sql``). Re-running pre-checks the folders already scoped in the
    config's include list; a legacy ``**/Name/**`` exclude simply reads as
    unchecked. An empty selection is rejected and re-asked (an unchecked
    checkbox silently meaning "document everything" would be surprising — the
    opposite of what unchecking implies). Falls back to the comma-separated
    text prompt when the repo isn't on disk yet or has no subfolders.
    """
    repo_abs = (base_dir / Path(repo_rel_path).expanduser()).resolve()
    folders = _top_level_folders(repo_abs)
    if not folders:
        return _ask_csv(io, csv_message, existing_include or base_patterns)

    # re-run prefill: only folders already scoped in the include list are
    # checked — first runs (and legacy denylist configs) start fully unchecked
    prechecked = folder_scoped_includes(existing_include or [])
    choices = [WizardChoice(label=name, value=name, checked=name in prechecked) for name in folders]
    while True:
        selected = io.checkbox(
            f"{repo_label}_folders",
            f"{repo_label} — pick the folders to document "
            "(nothing is checked to start; SPACE checks a folder, ENTER confirms):",
            choices,
        )
        if selected:
            return includes_for_folders(list(selected), base_patterns)
        io.notice("  Select at least one folder (or press Ctrl-C to cancel).")


def _ask_repo_path(io: WizardIO, label: str, default: str, base_dir: Path) -> str:
    """Prompt for a repo path until it exists (or the user opts to keep it)."""
    while True:
        raw = io.path(f"{label}_path", f"{label}:", default)
        if not raw:
            continue
        resolved = (base_dir / Path(raw).expanduser()).resolve()
        if resolved.is_dir():
            return raw
        keep = io.confirm(
            f"{label}_missing",
            f"'{resolved}' doesn't exist (yet). Use it anyway?",
            default=False,
        )
        if keep:
            return raw


def _existing_config(config_path: Path, io: WizardIO) -> Config | None:
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
        except Exception:  # noqa: BLE001 — an unreadable existing config means "start fresh"
            io.notice(
                f"note: existing config could not be read ({exc}); starting fresh",
            )
            return None
        io.notice(
            f"note: existing config isn't runnable yet ({exc}) — your saved values are prefilled anyway",
        )
        return lenient


def _repo_default(existing: Config | None, key: str, fallback: str) -> str:
    if existing is not None and key in existing.repos:
        return existing.repos[key].path
    return fallback


_MODEL_FOLDER_RE = re.compile(r"(?:\*\*/)?(?P<name>[^/*]+\.SemanticModel)/", re.IGNORECASE)

# report/pbix file-type templates the model picker adds on top of the chosen
# .SemanticModel globs — folder-scoped to the user's folder pick when folder
# selection is active (mirrors DEFAULT_PBI_INCLUDE minus the model globs)
_PBI_REPORT_BASES = ["**/report.json", "**/visual.json", "**/page.json", "**/definition.pbir", "**/*.pbix"]


def _discover_semantic_models(pbi_abs: Path) -> list[str]:
    """Sorted names of every ``*.SemanticModel`` folder under the PBI repo."""
    if not pbi_abs.is_dir():
        return []
    return sorted({p.name for p in pbi_abs.rglob("*.SemanticModel") if p.is_dir()}, key=str.lower)


def _semantic_model_includes(model_folders: list[str], report_globs: list[str] | None = None) -> list[str]:
    """Include globs scoped to the chosen ``.SemanticModel`` folders, plus the
    report/pbix globs — ``report_globs`` when given (folder-scoped by the
    folder pick, so stray ``.Report``/``.pbix`` copies in unselected folders
    can't leak in), otherwise the global ``_PBI_REPORT_BASES`` (which match the
    shipped ``DEFAULT_PBI_INCLUDE`` so a wizard-scoped config documents the
    same pbix-only reports a default ``init`` config would — the pbix parser is
    best-effort/warning-driven, so an unreadable one degrades to a diagnostic,
    not noise). Only ``.pbip`` and other loose files are left out. fnmatch's
    ``*`` crosses ``/``, so ``**/<name>.SemanticModel/**/*.tmdl`` matches
    however deep the folder sits."""
    globs: list[str] = []
    for folder in model_folders:
        globs.append(f"**/{folder}/**/*.tmdl")
        globs.append(f"**/{folder}/**/*.bim")
    globs += report_globs if report_globs is not None else list(_PBI_REPORT_BASES)
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


def _ask_semantic_models(
    io: WizardIO, pbi_abs: Path, existing_includes: list[str] | None
) -> list[str] | None:
    """Let the user pick which ``.SemanticModel`` folders to document. Returns the
    selected folder names, or None when none are found on disk (the repo isn't
    cloned yet, or it has no TMDL models) so the caller falls back to manual
    include globs."""
    found = _discover_semantic_models(pbi_abs)
    if not found:
        return None
    io.notice(
        f"\nFound {len(found)} semantic model folder(s) in the Power BI repo. Pick which to\n"
        "document — only the selected *.SemanticModel folders are crawled (reports and\n"
        ".pbix files inside the folders you pick next are included; .pbip / other loose\n"
        "files are left out)."
    )
    prev = _previously_selected_models(existing_includes)
    while True:
        choices = [
            WizardChoice(label=name, value=name, checked=(name in prev) if prev else True) for name in found
        ]
        selected = io.checkbox(
            "semantic_models",
            "Semantic models to include (Space toggles, Enter confirms):",
            choices,
        )
        if selected:
            return list(selected)
        io.notice("  Select at least one semantic model (or press Ctrl-C to cancel).")


def _ask_manual_schema(io: WizardIO, default: str) -> str | None:
    raw = io.text("manual_schema", "SQL schema this model reads from:", default)
    return raw or None


def _candidate_config(
    *,
    base_dir: Path,
    project_name: str,
    sql_path: str,
    pbi_path: str,
    sql_include: list[str],
    sql_exclude: list[str],
    pbi_include: list[str],
    pbi_exclude: list[str],
    sql_dialect: str,
    mappings: list[tuple[str, str]],
    layers: dict[str, dict[str, list[str]]],
    ignore_schemas: list[str],
) -> Config:
    """A throwaway Config built from the wizard's in-flight answers (never
    written to disk), for dry-run scanning/linking."""
    rendered = render_config_yaml(
        project_name=project_name,
        sql_path=sql_path,
        pbi_path=pbi_path,
        mappings=mappings,
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


def _scan_estate(
    *,
    io: WizardIO,
    base_dir: Path,
    project_name: str,
    sql_path: str,
    pbi_path: str,
    sql_include: list[str],
    sql_exclude: list[str],
    pbi_include: list[str],
    pbi_exclude: list[str],
    sql_dialect: str,
) -> LineageGraph | None:
    """Parse the estate ONCE, before the layer and mapping questions, so both
    steps confirm discovered facts instead of asking for typed names (issue
    #35). The scan runs with no layer/ignore/include-schema rules — those are
    exactly what it feeds, and discovery must see EVERY schema, including ones
    the user is about to leave unchecked — and ``_autosuggest_mappings``
    applies them to a copy once answered. Returns ``None`` when the repos
    aren't on disk yet (or the scan fails), so callers fall back to the typed
    prompts."""
    import logging

    sql_abs = (base_dir / Path(sql_path).expanduser()).resolve()
    pbi_abs = (base_dir / Path(pbi_path).expanduser()).resolve()
    if not (sql_abs.is_dir() and pbi_abs.is_dir()):
        return None  # nothing to scan; callers use manual entry

    from coop_data_doc.cli import build_graph  # lazy: avoid the wizard<->cli cycle
    from coop_data_doc.progress import Progress, should_enable

    logging.getLogger("sqlglot").setLevel(logging.ERROR)  # the dry-run shouldn't spam parse notes
    io.notice("\nScanning your repos (read-only, a few seconds)…")
    config = _candidate_config(
        base_dir=base_dir,
        project_name=project_name,
        sql_path=sql_path,
        pbi_path=pbi_path,
        sql_include=sql_include,
        sql_exclude=sql_exclude,
        pbi_include=pbi_include,
        pbi_exclude=pbi_exclude,
        sql_dialect=sql_dialect,
        mappings=[],
        layers={},
        ignore_schemas=[],
    )
    try:
        parsed, _ = build_graph(config, progress=Progress(should_enable(quiet=False)))
    except Exception as exc:  # noqa: BLE001 — a failed dry-run degrades to typed prompts
        io.notice(f"  note: could not scan the repos ({exc}); falling back to typed prompts")
        return None
    return parsed


def _sql_schemas(parsed: LineageGraph | None) -> list[str]:
    """Distinct schemas of the scanned estate's SQL-side objects, sorted —
    the checkbox choices for the medallion-layers step."""
    if parsed is None:
        return []
    from coop_data_doc.graph.model import NodeType

    sql_types = (
        NodeType.BRONZE_TABLE,
        NodeType.SILVER_TABLE,
        NodeType.GOLD_TABLE,
        NodeType.VIEW,
        NodeType.STORED_PROC,
    )
    return sorted(
        {
            node.schema_name
            for node in parsed.nodes.values()
            if node.node_type in sql_types and node.schema_name
        }
    )


def _autosuggest_mappings(
    *,
    io: WizardIO,
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
    include_schemas: list[str],
    sql_dialect: str,
    mappings: list[tuple[str, str]],
    parsed: LineageGraph | None,
) -> list[tuple[str, str]] | None:
    """Propose the schema_mappings actually needed to link Power BI tables to
    their SQL sources, deriving each from where the unresolved tables' object
    names really live (confirm, don't type). ``parsed`` is the estate scanned
    once by ``_scan_estate`` (before the layer questions); returns ``None``
    when it is ``None`` (repos not on disk) so the caller falls back to manual
    entry. ``mappings`` seeds the working set (prefilled/kept rows).
    """
    from collections import Counter

    from coop_data_doc.graph.model import NodeType, normalize_identifier

    if parsed is None:
        return None  # nothing was scanned; caller does manual entry

    from coop_data_doc.cli import resolve_graph  # lazy: avoid the wizard<->cli cycle
    from coop_data_doc.layering import assign_layers, prune_schemas
    from coop_data_doc.progress import Progress, should_enable

    # Drive the dry-run's own link progress on stderr so the scan shows bars
    # instead of sitting silent (disabled when stderr isn't a TTY, e.g. tests).
    scan_progress = Progress(should_enable(quiet=False))

    def resolve(working: list[tuple[str, str]]) -> tuple[LineageGraph, ResolutionResult]:
        """Resolve the parsed estate against one mapping set, on a throwaway copy
        so ``parsed`` stays pristine for the next call. Only the (cheap) link
        step re-runs — the crawl/parse happened once, in _scan_estate."""
        graph = parsed.model_copy(deep=True)
        result, _ = resolve_graph(graph, build_config(working), interactive=False, progress=scan_progress)
        return graph, result

    def build_config(working: list[tuple[str, str]]) -> Config:
        return _candidate_config(
            base_dir=base_dir,
            project_name=project_name,
            sql_path=sql_path,
            pbi_path=pbi_path,
            sql_include=sql_include,
            sql_exclude=sql_exclude,
            pbi_include=pbi_include,
            pbi_exclude=pbi_exclude,
            sql_dialect=sql_dialect,
            mappings=working,
            layers=layers,
            ignore_schemas=ignore_schemas,
        )

    io.notice("\nConnecting Power BI tables to their SQL sources (read-only, a few seconds)…")
    # The estate was parsed BEFORE the layer/include answers existed (that scan
    # feeds them) — apply both post-passes to a copy now for parity with a real
    # build (build_graph runs prune_schemas then assign_layers in this order).
    parsed = parsed.model_copy(deep=True)
    prune_schemas(parsed, ignore_schemas, include_schemas)
    assign_layers(parsed, build_config(mappings))
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
    io.notice(f"  ✓ {linked} Power BI table(s) linked to SQL automatically.")
    if not by_model:
        io.notice("  Nothing else needs a schema mapping.")
        return list(mappings)

    pending = sum(len(set(v)) for v in by_model.values())
    io.notice(
        f"  {pending} table(s) in {len(by_model)} model(s) didn't match a SQL object — let's connect them:"
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
                io.notice(
                    f"  • {label}: {len(remaining)} table(s) match no SQL object by name "
                    "— likely external or renamed; left unresolved."
                )
                break
            total = len(remaining)
            top_schema = ranked[0][0]
            single = len(ranked) == 1 or ranked[0][1] > ranked[1][1]
            chosen: str | None
            if single:
                if io.confirm(
                    f"map_{label}_to_{top_schema}",
                    f"Model '{label}' has {total} unresolved table(s); their names live in "
                    f"SQL schema '{top_schema}'. Map {label} → {top_schema}?",
                    default=True,
                ):
                    chosen = top_schema
                else:
                    chosen = _ask_manual_schema(io, top_schema)
            else:
                options = [WizardChoice(label=f"{s} — covers {c}/{total}", value=s) for s, c in ranked]
                options.append(WizardChoice(label="Type a schema name myself", value="__manual__"))
                options.append(WizardChoice(label="Skip this model", value="__skip__"))
                picked = io.select(
                    f"map_{label}",
                    f"Model '{label}' has {total} unresolved tables across schemas — pick which to map:",
                    options,
                )
                chosen = (
                    None
                    if picked == "__skip__"
                    else _ask_manual_schema(io, top_schema)
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
                io.notice(
                    f"  • {label}: schema '{chosen}' covers none of the remaining "
                    f"{len(remaining)} table(s); left unresolved."
                )
                break
            mappings.append((chosen, label))
            remaining = new_remaining

    # verify re-scan: re-link (no re-parse) with the new mappings so the user
    # knows they took — reuses the graph parsed once at the top.
    io.notice("\n  Re-checking with your mappings…")
    _, verified = resolve(mappings)
    left = len(verified.unresolved)
    if left == 0:
        io.notice("  ✓ All Power BI tables now link to a SQL object.")
    else:
        io.notice(
            f"  {left} table(s) still unresolved (external/renamed, or no schema rule fits) — "
            "map them per-table on build, or mark them external."
        )
    return list(mappings)


_LAYER_EXAMPLES = {"bronze": "erp_orders, erp_finance", "silver": "stg", "gold": "mart, common, silver"}


def _layer_csv_prompt(io: WizardIO, layer: str, prior: list[str]) -> list[str]:
    """The classic free-text layer prompt (repos not scanned / nothing found)."""
    return _ask_csv(
        io,
        f"{layer.capitalize()} layer — schemas (comma-separated, e.g. "
        f"{_LAYER_EXAMPLES[layer]}, or blank to skip):",
        prior,
    )


def _schema_union(*lists: list[str]) -> list[str]:
    """Case-insensitive union of schema lists, first-seen order (deterministic)."""
    out: list[str] = []
    seen: set[str] = set()
    for items in lists:
        for item in items:
            if item.lower() not in seen:
                seen.add(item.lower())
                out.append(item)
    return out


def _ask_layer_schemas(
    io: WizardIO,
    existing: Config | None,
    discovered: list[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """Per-layer schema assignment + the schema allowlist, returned as
    ``(layer_schemas, include_schemas)``.

    Checking a schema under a layer both INCLUDES it in the docs and assigns
    the layer — an unchecked schema is excluded entirely (``include_schemas``
    is the union of every checked schema). A final catch-all checkbox offers
    the schemas no layer claimed, so a schema can be documented without a
    forced layer (it gets the read/write heuristic at build time). An empty
    total selection is rejected and re-asked. Without a scan (repos
    absent/empty), the classic free-text prompts remain, prefilled from the
    existing config exactly as before, and the typed schemas form the allowlist.
    """
    layer_schemas: dict[str, list[str]] = {}
    if not discovered:
        for layer in VALID_LAYERS:
            existing_rule = existing.layers.get(layer) if existing else None
            layer_schemas[layer] = _layer_csv_prompt(
                io, layer, existing_rule.schemas if existing_rule else []
            )
        return layer_schemas, _schema_union(*(layer_schemas[layer] for layer in VALID_LAYERS))

    # re-run prefill: a schema saved in a layer rule is checked under that
    # layer; one saved in include_schemas but not layered is checked in the
    # catch-all. First runs start fully unchecked.
    prior_include = {s.lower() for s in existing.include_schemas} if existing else set()
    layered_prior = (
        {s.lower() for rule in existing.layers.values() for s in rule.schemas} if existing else set()
    )
    while True:
        layer_schemas = {}
        remaining = list(discovered)
        for layer in VALID_LAYERS:  # bronze, silver, gold — each schema offered once
            existing_rule = existing.layers.get(layer) if existing else None
            prior = list(existing_rule.schemas) if existing_rule else []
            prior_keys = {schema.lower() for schema in prior}
            remaining_keys = {schema.lower() for schema in remaining}
            # schemas saved in this layer's rule but not (or no longer) discovered
            # stay offered — checked — so a re-run round-trips the config untouched
            extras = [schema for schema in prior if schema.lower() not in remaining_keys]
            names = remaining + extras
            if not names:
                layer_schemas[layer] = _layer_csv_prompt(io, layer, prior)
                continue
            choices = [WizardChoice(name, value=name, checked=name.lower() in prior_keys) for name in names]
            choices.append(WizardChoice("(add schemas by typing them next)", value="__manual__"))
            selected = io.checkbox(
                f"{layer}_layer",
                f"{layer.capitalize()} layer — pick its schemas (a checked schema is "
                "documented AND layered here; unchecked ones are left out):",
                choices,
            )
            chosen = [schema for schema in selected if schema != "__manual__"]
            if "__manual__" in selected:
                taken = {schema.lower() for schema in chosen}
                for extra in _ask_csv(
                    io, f"{layer.capitalize()} layer — additional schemas (comma-separated):", []
                ):
                    if extra.lower() not in taken:
                        chosen.append(extra)
                        taken.add(extra.lower())
            layer_schemas[layer] = chosen
            assigned = {schema.lower() for schema in chosen}
            remaining = [schema for schema in remaining if schema.lower() not in assigned]
        # catch-all: document a schema without forcing a layer on it
        no_layer: list[str] = []
        if remaining:
            choices = [
                WizardChoice(
                    name,
                    value=name,
                    checked=name.lower() in prior_include and name.lower() not in layered_prior,
                )
                for name in remaining
            ]
            no_layer = io.checkbox(
                "no_layer",
                "Include WITHOUT a layer — schemas to document with automatic layer "
                "detection (SPACE toggles, ENTER confirms; none = leave them out):",
                choices,
            )
        include_schemas = _schema_union(*(layer_schemas[layer] for layer in VALID_LAYERS), no_layer)
        if include_schemas:
            return layer_schemas, include_schemas
        # An empty selection silently meaning "document everything" is
        # surprising (the opposite of what unchecking implies) — re-ask instead.
        io.notice("  Select at least one schema to document (or press Ctrl-C to cancel).")


def run_setup(config_path: Path, io: WizardIO | None = None) -> Config | None:
    """Run the wizard, write the config, and return the validated result.

    ``io`` is the UI/transport abstraction; defaults to terminal questionary.
    Returns None when the file was saved but doesn't validate yet (e.g. the
    user pointed at a repo they haven't cloned and chose 'use it anyway').
    """
    from coop_data_doc.wizard_io import QuestionaryWizardIO

    io = io or QuestionaryWizardIO()
    config_path = Path(config_path).resolve()
    base_dir = config_path.parent
    existing = _existing_config(config_path, io)
    if existing is not None:
        io.notice(f"Updating {config_path} (current values shown as defaults)")

    project_name = (
        io.text(
            "project_name",
            "Project name (shown as the docs site title):",
            default=existing.project_name if existing else "Coop BI Estate",
        ).strip()
        or "Coop BI Estate"
    )

    # These two prompts are the step users most often click past without
    # understanding — spell out that a *folder path* is expected, with examples.
    io.notice(
        "\nNext, point the tool at your two source folders (the repos cloned on\n"
        "this computer). For each, enter the path to the folder — for example\n"
        "  /Users/you/code/sql-warehouse   or   C:\\Users\\you\\code\\PowerBI\n"
        "Relative paths are resolved against the config file's folder.\n"
    )
    sql_path = _ask_repo_path(
        io,
        "SQL repo path — the folder with your procs, tables, views",
        _repo_default(existing, "sql", "../sql-repo"),
        base_dir,
    )
    pbi_path = _ask_repo_path(
        io,
        "Power BI repo path — the folder with your semantic models and reports",
        _repo_default(existing, "powerbi", "../pbi-repo"),
        base_dir,
    )

    output_dir = (
        io.text(
            "output_dir",
            "Markdown output folder:",
            default=existing.output.dir if existing else "./data-docs",
        ).strip()
        or "./data-docs"
    )
    # The HTML site is rebuilt by wiping its folder, so it must sit beside the
    # markdown dir, never inside it. Default to a sibling and reject a conflict.
    site_default = existing.output.site_dir if existing else _sibling_site(output_dir)
    while True:
        site_dir = (
            io.text(
                "site_dir",
                "HTML site output folder (must be separate from the markdown folder):",
                default=site_default,
            ).strip()
            or site_default
        )
        out_abs = (base_dir / Path(output_dir).expanduser()).resolve()
        site_abs = (base_dir / Path(site_dir).expanduser()).resolve()
        if not output_dirs_conflict(out_abs, site_abs):
            break
        io.notice(
            "  ✗ The HTML site folder can't be the same as — or inside — the markdown\n"
            "    folder. Each build wipes the site folder, which would clobber your\n"
            f"    markdown. Try a sibling like '{_sibling_site(output_dir)}'."
        )
        site_default = _sibling_site(output_dir)

    # --- what to document (folder allowlist), per repo ---
    sql_repo = existing.repos.get("sql") if existing else None
    pbi_repo = existing.repos.get("powerbi") if existing else None
    io.notice("\n── What to document ──")
    io.notice(
        "  FOLDERS = check the top-level folders to document. NOTHING is checked to\n"
        "            start — only the folders you pick are crawled. When the repo isn't\n"
        "            on disk yet, you type include globs instead."
    )
    sql_abs = (base_dir / Path(sql_path).expanduser()).resolve()
    sql_bases = base_patterns_from_includes(list(sql_repo.include)) if sql_repo else []
    sql_include = _ask_folders_to_document(
        io,
        "SQL",
        sql_path,
        base_dir,
        sql_repo.include if sql_repo else None,
        sql_bases or DEFAULT_SQL_INCLUDE,
        "SQL — files/patterns to INCLUDE (comma-separated globs):",
    )
    # hand-written (non-folder) excludes are preserved; folder excludes are
    # superseded by the allowlist
    _, sql_exclude = split_excludes(_top_level_folders(sql_abs), list(sql_repo.exclude) if sql_repo else [])
    # If the PBI repo is on disk, let the user pick which .SemanticModel folders
    # to document (scoping the model globs to those), then pick the top-level
    # folders the report/pbix globs scope to; otherwise fall back to the folder
    # allowlist over the full PBI file-type set.
    pbi_abs = (base_dir / Path(pbi_path).expanduser()).resolve()
    _, pbi_exclude = split_excludes(_top_level_folders(pbi_abs), list(pbi_repo.exclude) if pbi_repo else [])
    selected_models = _ask_semantic_models(io, pbi_abs, pbi_repo.include if pbi_repo else None)
    if selected_models is not None:
        # The model picker scopes the models; this folder pick scopes the
        # report/pbix globs so stray .Report/.pbix copies in unselected folders
        # (BACKUP, Documentation, data-docs) can't leak in as reports or
        # pbix-derived junk models.
        report_globs = _ask_folders_to_document(
            io,
            "Power BI",
            pbi_path,
            base_dir,
            pbi_repo.include if pbi_repo else None,
            _PBI_REPORT_BASES,
            "Power BI — report/pbix files to INCLUDE (comma-separated globs):",
        )
        pbi_include = _semantic_model_includes(selected_models, report_globs)
    else:
        # Fallback (repo not cloned yet / no .SemanticModel folders): the folder
        # allowlist is the only scoping mechanism here.
        pbi_bases = base_patterns_from_includes(list(pbi_repo.include)) if pbi_repo else []
        pbi_include = _ask_folders_to_document(
            io,
            "Power BI",
            pbi_path,
            base_dir,
            pbi_repo.include if pbi_repo else None,
            pbi_bases or DEFAULT_PBI_INCLUDE,
            "Power BI — files/patterns to INCLUDE (comma-separated globs):",
        )

    # --- scan once (read-only): the layers step below and the schema-mapping
    #     step later both confirm discovered facts instead of asking the user
    #     to type schema names from memory (issue #35) ---
    sql_dialect = existing.sql_dialect if existing else "tsql"
    parsed = _scan_estate(
        io=io,
        base_dir=base_dir,
        project_name=project_name,
        sql_path=sql_path,
        pbi_path=pbi_path,
        sql_include=sql_include,
        sql_exclude=sql_exclude,
        pbi_include=pbi_include,
        pbi_exclude=pbi_exclude,
        sql_dialect=sql_dialect,
    )

    # --- medallion layers: assign by SCHEMA (the common case) ---
    io.notice("\n── Medallion layers ──")
    io.notice(
        "  Check the schemas to document, layer by layer: a checked schema is both\n"
        "  INCLUDED in the docs and assigned that layer — an unchecked schema is left\n"
        "  out entirely. A final catch-all offers anything left over, to document it\n"
        "  with automatic layer detection. In a Fabric/SQL warehouse the schema IS\n"
        "  the folder, so schemas alone are all you need."
    )
    layer_schemas, include_schemas = _ask_layer_schemas(io, existing, _sql_schemas(parsed))

    # Folder-based layering is an advanced fallback for repos where a layer is
    # a directory rather than a schema. Most repos don't need it, so it's off
    # by default — but re-running setup keeps any folder rules you already had.
    had_paths = bool(existing and any(rule.paths for rule in existing.layers.values()))
    layer_paths: dict[str, list[str]] = {}
    if io.confirm(
        "layer_paths",
        "Advanced: does any layer map to a FOLDER instead of a schema?",
        default=had_paths,
    ):
        for layer in VALID_LAYERS:
            existing_rule = existing.layers.get(layer) if existing else None
            layer_paths[layer] = _ask_csv(
                io,
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

    io.notice("\n── Schemas to drop ──")
    io.notice(
        "  Only the schemas checked above are documented, so most estates need nothing\n"
        "  here. This drops objects even from a checked schema (noise like staging/temp);\n"
        "  system schemas (sys, information_schema, …) are always dropped. Blank = drop\n"
        "  nothing extra."
    )
    ignore_schemas = _ask_csv(
        io,
        "Schemas to DROP from the docs (comma-separated, e.g. staging, tmp — blank "
        "for none; system schemas like sys/information_schema are always dropped):",
        existing.ignore_schemas if existing else [],
    )

    # --- optional company branding for the HTML site ---
    existing_brand = existing.branding if existing else None
    io.notice("\n── Branding (optional — blank to skip) ──")
    branding: dict[str, str] = {}
    logo = io.text(
        "logo",
        "Logo image path (shown in the site header; relative to this config):",
        default=(existing_brand.logo if existing_brand and existing_brand.logo else ""),
    ).strip()
    if logo:
        branding["logo"] = logo
    # colors default to the Cooptimize brand theme; press Enter to keep it
    primary = io.text(
        "primary_color",
        "Primary brand color (hex; default = Cooptimize navy):",
        default=(
            existing_brand.primary_color
            if existing_brand and existing_brand.primary_color
            else DEFAULT_PRIMARY_COLOR
        ),
    ).strip()
    if primary:
        branding["primary_color"] = primary
    accent = io.text(
        "accent_color",
        "Accent color (hex; default = Cooptimize red-orange):",
        default=(
            existing_brand.accent_color
            if existing_brand and existing_brand.accent_color
            else DEFAULT_ACCENT_COLOR
        ),
    ).strip()
    if accent:
        branding["accent_color"] = accent
    # carry an existing favicon through unchanged (not prompted separately)
    if existing_brand and existing_brand.favicon:
        branding["favicon"] = existing_brand.favicon

    # --- schema → semantic-model hints ---
    io.notice("\n── Power BI: connecting tables to their SQL sources ──")
    mappings: list[tuple[str, str]] = []
    if existing is not None and existing.schema_mappings:
        current = ", ".join(f"{m.schema_name} → {m.model}" for m in existing.schema_mappings)
        keep = io.confirm(
            "keep_mappings",
            f"Keep existing schema mappings ({current})?",
            default=True,
        )
        if keep:
            mappings = [(m.schema_name, m.model) for m in existing.schema_mappings]

    # When the repos are on disk (= the estate was scanned above), auto-derive
    # the schema mappings that are actually needed (confirm, don't type).
    # Falls back to manual entry when the repos aren't cloned yet.
    auto = _autosuggest_mappings(
        io=io,
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
        include_schemas=include_schemas,
        sql_dialect=sql_dialect,
        mappings=mappings,
        parsed=parsed,
    )
    if auto is not None:
        mappings = auto
    else:
        while io.confirm(
            "add_mapping",
            "Add a view-schema → semantic-model mapping?",
            default=not mappings,
        ):
            schema = io.text("mapping_schema", "SQL schema (e.g. mart):").strip()
            model = io.text("mapping_model", "Semantic model it feeds (e.g. Sales Analytics):").strip()
            if schema and model:
                mappings.append((schema, model))

    rendered = render_config_yaml(
        project_name=project_name,
        sql_path=sql_path,
        pbi_path=pbi_path,
        mappings=mappings,
        layers=layers,
        ignore_schemas=ignore_schemas,
        include_schemas=include_schemas,
        branding=branding,
        sql_include=sql_include,
        sql_exclude=sql_exclude,
        pbi_include=pbi_include,
        pbi_exclude=pbi_exclude,
        output_dir=output_dir,
        site_dir=site_dir,
        sql_dialect=sql_dialect,
        # carry an existing reviews: list through unchanged (not prompted separately)
        reviews=existing.reviews if existing is not None else None,
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
        io.notice(f"Saved, but not runnable yet: {exc}")
        return None
