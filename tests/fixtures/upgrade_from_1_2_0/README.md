# Estates saved by coop-data-doc v1.2.0

Used by `tests/test_legacy_decisions_upgrade.py` (plan row DD3). Everything
here except `sql/` and `pbi/` was written by the real v1.2.0 release
(`git worktree add ../dd-120 v1.2.0`, editable install, Python 3.13), so do
not regenerate it with a newer version.

- `mixed/`: `build --non-interactive`, then `resolve-apply` with six
  decisions (two targets, two external, one skip, and one table-level answer
  for `orders`, which has two partitions), then `build` again. Business
  Intent was then authored on the `orders` and `v_customers` pages.
- `collision/`: the same estate plus `pbi/Archive/Sales.SemanticModel`, a
  second model also named Sales, which v1.2.0 merged into one
  `semantic_model:sales`; one more decision for `legacy_customer`.

Two files are renamed so the repo's `.gitignore` keeps them: `published/` is
the v1.2.0 `data-docs/` folder (HTML assets dropped) and `lineage-cache.json`
is `.lineage-cache.json`. The test copies them back to their real names.
