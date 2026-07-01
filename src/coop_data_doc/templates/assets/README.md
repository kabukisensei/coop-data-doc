# Vendored third-party assets

Shipped inside the wheel so generated sites work fully offline (`file://`,
no CDN). Material for MkDocs falls back to unpkg.com for this when it isn't
provided locally.

| File | Version | Source | License |
| --- | --- | --- | --- |
| `iframe-worker-shim.js` | 1.0.4 | https://unpkg.com/iframe-worker@1.0.4/shim/index.js | MIT |

To update: download the new pinned version, update this table, and bump
the package version.

## First-party assets

| File | Purpose |
| --- | --- |
| `custom.css` | Site styling tweaks (nav, relationship-grid gridlines, collapsible trees). |
| `doc-tree.js` | Dependency-free collapsible "Upstream lineage" trees (each branch starts collapsed; click ▸/▾ to drill down). Degrades to the full Markdown list with no JS. |
