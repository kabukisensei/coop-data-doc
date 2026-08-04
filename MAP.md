# coop-data-doc: MAP

Last updated: 2026-08-03 | Branch: `main` | Last commit: 2026-08-03 | ✅ Clean

## Destination

Data documentation tool for Fabric/DW estates — build lineage graphs, impact analysis, CI gates. Published to PyPI.

## Notes

- See `AGENTS.md` for full agent contract (commands, env, CLI codes, artifacts).
- Python 3.10–3.13 only. Rebuild with `make setup`.
- Headless VPS can run tests/fixtures only; full Azure estates are Aaron's-Mac-only.
- Pushing a `v*` tag auto-publishes to PyPI — never create a tag unless explicitly asked.

## Status

🟢 **Stable** (v1.0.0 released, 525 tests passing)

## Decisions so far

- CLI-first, offline pipeline; no live Fabric auth required for core tool.
- `build --strict` exits 2 on problems; `check --lenient` is the CI gate.
- `unmatched_visual_entity` is advisory, never gating.

## Frontier (active)

- [ ] Monitor v1.0.0 adoption / any bug reports from release
- [ ] Keep test count parity after future parser changes (currently 525)

## Fog of war

- Whether to add richer multi-hop lineage UI beyond current graph export
- What the next PyPI minor feature should be after v1.0.0

## Out of scope

- Full Azure DevOps estate builds on the VPS (interactive auth required; see AGENTS.md)
