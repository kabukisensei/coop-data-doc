# coop-data-doc developer targets.
# No venv activation anywhere — every target calls .venv/bin/* directly.
# Run `make setup` once first. Python 3.10-3.13 only; NEVER 3.14 (it breaks
# the editable-install .pth / console-script imports).

VENV   := .venv
PY     := $(VENV)/bin/python
PYTHON ?= python3.13
INIT   := src/coop_data_doc/__init__.py

.PHONY: setup test lint release-check

# Create .venv with python3.13 and editable-install with dev deps.
# Verification step: the console entry point must import and print a version.
setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -e ".[dev]"
	$(PY) -m coop_data_doc --version

# Full unit suite. Expected: ~300 passed in < 2 s.
test: | $(PY)
	$(PY) -m pytest -q

# Same two checks CI enforces (.github/workflows/ci.yml).
lint: | $(PY)
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests

# Local mirror of the tag gate in .github/workflows/publish.yml:
# the pushed tag v<X.Y.Z> must equal __version__ in $(INIT) (single source
# of truth; pyproject.toml derives its version from it via hatchling).
# Also fails if the tag already exists — PyPI never reuses a version.
# Checks LOCAL tags only (stays offline): run `git fetch --tags` first if
# this clone may be behind origin.
# Run this BEFORE `git tag vX.Y.Z && git push origin vX.Y.Z`.
release-check: | $(PY)
	@ver="$$($(PY) -c "import re,pathlib; print(re.search(r'__version__ = \"([^\"]+)\"', pathlib.Path('$(INIT)').read_text()).group(1))")"; \
	echo "__version__ = $$ver  ($(INIT))"; \
	echo "$$ver" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "FAIL: '$$ver' is not semver X.Y.Z"; exit 1; }; \
	if git rev-parse -q --verify "refs/tags/v$$ver" >/dev/null; then \
		echo "FAIL: tag v$$ver already exists — bump __version__ in $(INIT) (PyPI versions are never reused)"; exit 1; \
	fi; \
	echo "OK: next release tag must be exactly v$$ver (publish.yml aborts on mismatch)"; \
	echo "    (local tags only — 'git fetch --tags' first if this clone may be behind)"; \
	echo "    release with: git tag v$$ver && git push origin v$$ver"

# Guard: everything except `setup` needs the venv to exist already.
$(PY):
	@echo "ERROR: $(VENV) missing — run 'make setup' first."; exit 1
