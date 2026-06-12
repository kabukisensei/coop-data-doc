# Builder-Agent Task Files

Each `module-N.md` is a self-contained brief for a coding LLM (Claude Sonnet, Kimi K2.7, etc.).

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
- [ ] `pytest` green, including new fixtures
- [ ] No prints/exits outside cli.py; warnings returned as data
- [ ] Determinism: run twice on fixtures → identical output (sorted iteration everywhere)
- [ ] No new dependencies beyond the allowed list
- [ ] Type hints + module docstring present
