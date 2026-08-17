# coop-data-doc: MAP
Last updated: 2026-08-17 | Branch: `main` | Last commit: 2026-08-10 | ✅ Clean
## Destination
Data documentation tool for Fabric/DW estates — build lineage graphs, impact analysis, CI gates. Published to PyPI.
## Notes
Published to PyPI. CLI tool for Fabric/DW documentation. ~490 tests.
## Status
🟢 **Stable**
## Decisions so far
- CLI-first, offline pipeline; no live Fabric auth required for core tool.
- `build --strict` exits 2 on problems; `check --lenient` is the CI gate.
- `unmatched_visual_entity` is advisory, never gating.
## Frontier (active)
*Nothing actively in progress.*
## Fog of war
- Whether to add richer multi-hop lineage UI beyond current graph export
- What the next PyPI minor feature should be after v1.0.0
- What's next on the roadmap after current stable state
## Out of scope
- Full Azure DevOps estate builds on the VPS (interactive auth required; see AGENTS.md)
