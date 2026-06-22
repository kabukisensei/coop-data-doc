# Vendored third-party assets

Shipped inside the wheel so generated sites work fully offline (`file://`,
no CDN). Material for MkDocs falls back to unpkg.com for both of these
when they aren't provided locally.

| File | Version | Source | License |
| --- | --- | --- | --- |
| `mermaid.min.js` | 11.15.0 | https://unpkg.com/mermaid@11.15.0/dist/mermaid.min.js | MIT |
| `iframe-worker-shim.js` | 1.0.4 | https://unpkg.com/iframe-worker@1.0.4/shim/index.js | MIT |

To update: download the new pinned version, update this table, and bump
the package version.

## First-party assets

| File | Purpose |
| --- | --- |
| `custom.css` | Site styling tweaks (nav, tables, mermaid zoom viewport). |
| `mermaid-zoom.js` | Dependency-free pan/zoom for rendered Mermaid diagrams (drag to pan, Ctrl/Cmd+scroll to zoom, control bar). Hand-rolled rather than vendoring svg-pan-zoom to stay within the no-CDN rule. |
