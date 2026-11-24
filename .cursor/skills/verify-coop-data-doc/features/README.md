# coop-data-doc verification map

This directory is the maintained source for verifying what a person does with the coop-data-doc CLI. Read this index, then use the matching feature file.

## Baseline preconditions

- `bash scripts/verify.sh launch` has printed `LAUNCH: ready` for this checkout.
- `bash scripts/verify.sh doctor` prints `DOCTOR: ok` with `__version__` and Python 3.10–3.13.
- The temp project is a copy of `.cursor/skills/verify-coop-data-doc/fixture/`. Its config is `coop-data-doc.yml` and its name is `Verify Estate`.
- Drive only the temp project this launch created. A config in the repo root or a parent directory is not it.
- One verification project per checkout. A second launch refuses.

## Driving conventions

- Run commands through `bash scripts/verify.sh drive <feature>`.
- Treat object ids, flags, and fixture names as literal.
- The full gate is `bash scripts/verify.sh`. One feature drive does not cover the others.
- Drive `project`, then `scan`, then `build`, then `export`, then `lineage`, then `lineage-cache`, then `check`.
- Keep the evidence directory after cleanup.

## Proof and skip reporting

- Capture the command, its exit code, and the file or JSON it produced.
- Record the feature id in the `PASS` line.
- `setup`, `set-folders`, and `upgrade` are not features in this map. The skill's feature table says why the harness leaves them alone.
- Do not report a skipped entry point as verified through a different command.

## Feature entry contract

Each feature file has an H1, one paragraph, then four H2 sections: `Sub-features`, `How to get to it (user POV)`, `Driving it with verify.sh`, and `Gotchas`.

## Features

- [Project](./project.md) covers init, config-set, show-config, folders, and status before any docs exist.
- [Generate](./generate.md) covers scan, build, check, and a byte-identical update.
- [Export](./export.md) covers the four CSV files.
- [Lineage](./lineage.md) covers an ambiguous name, one object's neighborhood, and a column trace.
- [Lineage cache](./lineage-cache.md) covers resolve, resolve-apply, the cached rebuild, and impact.
