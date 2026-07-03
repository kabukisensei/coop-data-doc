# Builder-Agent Task Files

Each `module-N.md` is a self-contained brief for a coding LLM (Claude Sonnet, Kimi K2.7, etc.).

> **Status: every module (M0–M7) is built and merged.** These briefs remain as
> interface documentation (see the module map in `CONTRIBUTING.md`). The
> handoff process below applies **only** if a new `module-N.md` brief is added;
> for changes to existing code, follow `CONTRIBUTING.md` instead — do not
> re-implement a module from its brief. Where a brief and the code disagree,
> the code wins.

## How to hand off a task
1. Paste **`_shared-context.md`** first (always).
2. Paste the source of `src/coop_data_doc/graph/model.py` (the interface every module codes
   against — already implemented and tested).
3. Paste the `module-N.md` brief.
4. For M4/M5/M6, also paste the output interfaces named in the brief's "Inputs you can rely on"
   section (copy the relevant function signatures from the merged code of earlier modules).

## Build order
| Wave | Modules | Parallel? |
|---|---|---|
| 1 | ~~M0 core graph~~ ✅ done · ~~M1 config + crawler~~ ✅ done | — |
| 2 | ~~M2 SQL parser~~ ✅ done · ~~M3 Power BI extractor~~ ✅ done | — |
| 3 | ~~M4 linker + interactive cache~~ ✅ done | — |
| 4 | ~~M5 renderers~~ ✅ done · ~~M6 CLI~~ ✅ done | — |
| 5 | ~~M7 packaging/CI hardening~~ ✅ done | — |

## Review checklist for every returned module
- [ ] `.venv/bin/python -m pytest -q` green (expect `~310 passed`, zero failures), including new fixtures
- [ ] No prints/exits outside cli.py, wizard.py, progress.py, and linker/interactive.py; warnings returned as data
- [ ] Determinism: two builds on the same inputs are byte-identical — `tests/test_determinism.py` (part of the suite above) enforces this; sorted iteration everywhere
- [ ] No new dependencies beyond the allowed list (`[project.dependencies]` in `pyproject.toml`; mirrored in `_shared-context.md`)
- [ ] Type hints + module docstring present
