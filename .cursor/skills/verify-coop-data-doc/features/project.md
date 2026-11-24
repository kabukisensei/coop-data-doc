# Project

Project commands tell a person whether a config exists, which folders it documents, and whether docs have been built yet.

## Sub-features

- `init-write` scaffolds `coop-data-doc.yml` named `Coop BI Estate` in an empty directory.
- `init-refuse` leaves that file in place when `init` is run again without `--force`.
- `config-set` applies a JSON patch and reloads the file.
- `show-config` prints the fixture config as JSON.
- `folders` lists documented top-level folders for both repos.
- `status-empty` reports missing docs, a missing lineage cache, and one unresolved reference.

## How to get to it (user POV)

- Run `coop-data-doc init` in a directory with no config.
- Run `coop-data-doc init` again in that directory.
- Run `coop-data-doc config-set --from-json <file>` against the scaffold.
- Run `coop-data-doc show-config` in the fixture project.
- Run `coop-data-doc folders` in the fixture project.
- Run `coop-data-doc status` in the fixture project before `build`.

## Driving it with verify.sh

Preconditions:

- `bash scripts/verify.sh doctor` prints `DOCTOR: ok`.
- The temp project does not yet have `data-docs/` or `.lineage-cache.json`.

- **Scaffold.** Run `bash scripts/verify.sh drive project`. In the temp scratch directory, `init` exits 0 and writes `project_name: "Coop BI Estate"`.
- **Refuse overwrite.** The same command runs `init` again. Exit code is 1 and the transcript contains `already exists`. The scaffold is still `Coop BI Estate`.
- **Patch.** The same command runs `config-set` with `{"project_name": "Scratch Estate"}`. Exit code is 0. Stdout contains `saved, not runnable yet` because the starter paths `../sql-repo` and `../pbi-repo` are not on disk. `show-config` on that file returns `Scratch Estate` and `exists: true`.
- **Fixture config.** The same command runs `show-config` in the fixture project. `project_name` is `Verify Estate`, `exists` is true, and `path` is that project's `coop-data-doc.yml`.
- **Folders.** The same command runs `folders`. The `sql` repo is `allowlist` and `tables`, `views`, and `procs` are documented. The `powerbi` repo exists, mode `legacy`, and both `Sales.SemanticModel` and `Sales.Report` are documented.
- **Empty status.** The same command runs `status`. Exit code is 0. Stdout contains `freshness: no docs yet`, `(missing)` for the lineage cache, and `unresolved: 1`.
- **Proof.** The evidence directory holds `init.txt`, `init-again.txt`, `config-set.txt`, `show-config.txt`, `folders.txt`, and `status-before.txt`.

## Gotchas

- `init` in the fixture project would refuse or, with `--force`, replace the fixture. The harness scaffolds only in the scratch directory.
- The starter config is valid YAML whose repo paths do not exist yet. `config-set` still saves the patch and says it is not runnable. That is the starter, not a failed patch.
- `status` runs the pipeline. On this fixture that is what produces `unresolved: 1` before anyone answers the alias.
- `setup` is the interactive wizard. This drive does not open it.
- `set-folders` rewrites include globs. This drive only reads `folders`.
