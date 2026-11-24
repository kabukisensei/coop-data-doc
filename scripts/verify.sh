#!/usr/bin/env bash
# Verify coop-data-doc by installing this checkout into a temporary venv and
# replaying the CLI against the committed fixture. No secrets. The replay
# itself does not use the network. Does not deploy and does not push.
#
#   bash scripts/verify.sh                 # full run; last line is RESULT: PASS or RESULT: FAIL
#   bash scripts/verify.sh launch          # install + copy the fixture into a temp project
#   bash scripts/verify.sh doctor          # read-only: is that install this checkout?
#   bash scripts/verify.sh drive [feature] # one feature, or "all"
#   bash scripts/verify.sh cleanup         # delete the temp venv and project; keep evidence
#
# Features: project, scan, build, export, lineage, lineage-cache, check, all.
# A second launch refuses while the temp project still exists.
# Cleanup never deletes the evidence directory.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE="$ROOT/.cursor/skills/verify-coop-data-doc/fixture"
STATE_DIR="$ROOT/.cache/verify"
INSTANCE="$STATE_DIR/instance.env"

WORK=""
VENV=""
CLI=""
PROJECT=""
SCRATCH=""
EVIDENCE=""
SUMMARY=""
FAIL=0
READY=0

VIEW="view:sales.dim_customer"
PBI_EXACT="pbi_table:sales.dim_customer"
PBI_ALIAS="pbi_table:sales.dim_cust"
CUSTOMERS="gold_table:dbo.customers"
ORDERS="gold_table:dbo.orders"
PROC="stored_proc:dbo.usp_load_orders"
MODEL="semantic_model:sales"
REPORT="report:sales"
MEASURE="measure:sales.customercount"

usage() {
  cat <<'EOF'
Usage: bash scripts/verify.sh [run|launch|doctor|drive|cleanup]
  run (default)  install, doctor, replay every feature, clean up
  launch         temp venv + a copy of the verification fixture
  doctor         read-only: version, interpreter, and help text
  drive [feature]  project | scan | build | export | lineage | lineage-cache | check | all
  cleanup        delete the temp venv and project; keep evidence
EOF
}

pick_python() {
  local candidate version
  for candidate in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  if command -v python3 >/dev/null 2>&1; then
    version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    case "$version" in
      3.10|3.11|3.12|3.13)
        printf '%s\n' python3
        return 0
        ;;
    esac
  fi
  return 1
}

package_version() {
  local py="$1"
  "$py" - "$ROOT/src/coop_data_doc/__init__.py" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
if match is None:
    sys.exit(1)
print(match.group(1))
PY
}

load_instance() {
  WORK=""
  VENV=""
  CLI=""
  PROJECT=""
  SCRATCH=""
  EVIDENCE=""
  READY=0
  if [[ ! -f "$INSTANCE" ]]; then
    return 1
  fi
  # instance.env is written by this script with printf %q.
  # shellcheck disable=SC1090
  . "$INSTANCE"
  return 0
}

write_instance() {
  mkdir -p "$STATE_DIR"
  printf 'WORK=%q\nVENV=%q\nCLI=%q\nPROJECT=%q\nSCRATCH=%q\nEVIDENCE=%q\nREADY=%q\n' \
    "$WORK" "$VENV" "$CLI" "$PROJECT" "$SCRATCH" "$EVIDENCE" "$READY" >"$INSTANCE"
}

remove_work() {
  case "${WORK:-}" in
    /tmp/coop-data-doc-verify.*)
      rm -rf "$WORK"
      ;;
    "")
      ;;
    *)
      echo "CLEANUP: refusing to delete unexpected path $WORK" >&2
      return 1
      ;;
  esac
}

cmd_cleanup() {
  if ! load_instance; then
    echo "CLEANUP: no verification project"
    return 0
  fi
  remove_work
  rm -f "$INSTANCE"
  echo "CLEANUP: removed temp project"
}

note() {
  if [[ -n "$SUMMARY" ]]; then
    printf '%s\n' "$*" | tee -a "$SUMMARY"
  else
    printf '%s\n' "$*"
  fi
}

die() {
  echo "FAIL: $*" >&2
  if [[ -n "$SUMMARY" ]]; then
    printf '%s\n' "FAIL: $*" >>"$SUMMARY"
  fi
  FAIL=1
  exit 1
}

keep() {
  printf '%s\n' "$1" >>"$EVIDENCE/keep.txt"
}

refresh_wheelhouse() {
  local wheelhouse="$STATE_DIR/wheels"
  mkdir -p "$wheelhouse"
  "$VENV/bin/pip" freeze --all | grep -v -i -E '^(coop-data-doc|pip|setuptools|wheel)([= ]|$)' \
    >"$wheelhouse/requirements.txt" || true
  if [[ ! -s "$wheelhouse/requirements.txt" ]]; then
    return 0
  fi
  # Best effort: a failed download leaves the previous wheelhouse in place.
  "$VENV/bin/pip" download --disable-pip-version-check --dest "$wheelhouse" \
    -r "$wheelhouse/requirements.txt" hatchling >>"$EVIDENCE/install.txt" 2>&1 || true
}

run_cli() {
  local name="$1"
  local cwd="$2"
  shift 2
  local roots="$cwd"
  mkdir -p "$EVIDENCE"
  (
    cd "$cwd"
    unset COOP_DATA_DOC_CONFIG
    export COOP_DATA_DOC_DISCOVERY_ROOTS="$roots"
    export COOP_DATA_DOC_JOBS=1
    export PYTHONNOUSERSITE=1
    export PIP_DISABLE_PIP_VERSION_CHECK=1
    "$CLI" "$@"
  ) >"$EVIDENCE/$name.out" 2>"$EVIDENCE/$name.err"
  local ec=$?
  printf '%s\n' "$ec" >"$EVIDENCE/$name.exit"
  {
    printf '$ coop-data-doc'
    local arg
    for arg in "$@"; do
      printf ' %q' "$arg"
    done
    printf '\nexit: %s\n----- stdout -----\n' "$ec"
    cat "$EVIDENCE/$name.out"
    printf '%s\n' '----- stderr -----'
    cat "$EVIDENCE/$name.err"
  } >"$EVIDENCE/$name.txt"
  keep "$name.txt"
  return "$ec"
}

require_ready() {
  load_instance || die "no verification project. Run: bash scripts/verify.sh launch"
  [[ "$READY" == "1" ]] || die "verification project is not ready. Run: bash scripts/verify.sh doctor"
  [[ -x "$CLI" ]] || die "missing CLI at $CLI"
  [[ -f "$PROJECT/coop-data-doc.yml" ]] || die "fixture config missing at $PROJECT"
}

cmd_launch() {
  local preset="${1:-}"
  if [[ -n "$preset" ]]; then
    EVIDENCE="$preset"
  fi
  if load_instance && [[ -x "${CLI:-}" ]]; then
    echo "LAUNCH: a verification project is already installed. Run: bash scripts/verify.sh cleanup" >&2
    return 1
  fi
  if [[ -n "$preset" ]]; then
    EVIDENCE="$preset"
  elif [[ -z "$EVIDENCE" ]]; then
    EVIDENCE="$STATE_DIR/evidence/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  fi
  local py
  py="$(pick_python)" || die "need Python 3.10-3.13 (3.14 cannot import an editable-style install)"
  mkdir -p "$EVIDENCE"
  : >"$EVIDENCE/keep.txt"
  WORK="$(mktemp -d /tmp/coop-data-doc-verify.XXXXXX)"
  VENV="$WORK/venv"
  PROJECT="$WORK/project"
  SCRATCH="$WORK/scratch"
  READY=0
  write_instance
  echo "INSTALL: creating $VENV with $py"
  "$py" -m venv "$VENV"
  local src="$WORK/checkout"
  mkdir -p "$src"
  cp -a "$ROOT/pyproject.toml" "$ROOT/README.md" "$ROOT/LICENSE" "$src/"
  cp -a "$ROOT/src" "$src/src"
  # The CLI replay does not use the network. A wheelhouse under .cache/verify/wheels
  # supplies dependencies with --no-index after the first download.
  local wheelhouse="$STATE_DIR/wheels"
  mkdir -p "$wheelhouse"
  INSTALL_MODE="download"
  if [[ -s "$wheelhouse/requirements.txt" ]] && \
    "$VENV/bin/pip" install --disable-pip-version-check --no-input --no-index \
      --find-links "$wheelhouse" -r "$wheelhouse/requirements.txt" hatchling \
      >"$EVIDENCE/install.txt" 2>&1 && \
    "$VENV/bin/pip" install --disable-pip-version-check --no-input --no-index \
      --no-build-isolation --no-deps "$src" >>"$EVIDENCE/install.txt" 2>&1
  then
    echo "install: wheelhouse" >>"$EVIDENCE/install.txt"
    INSTALL_MODE="wheelhouse"
  else
    echo "---- wheelhouse miss; downloading dependencies ----" >>"$EVIDENCE/install.txt"
    if ! "$VENV/bin/pip" install --disable-pip-version-check --no-input "$src" >>"$EVIDENCE/install.txt" 2>&1; then
      tail -n 40 "$EVIDENCE/install.txt" >&2
      die "pip install of this checkout failed"
    fi
    refresh_wheelhouse
  fi
  keep install.txt
  CLI="$VENV/bin/coop-data-doc"
  [[ -x "$CLI" ]] || die "install did not create $CLI"
  mkdir -p "$PROJECT" "$SCRATCH"
  cp -a "$FIXTURE/." "$PROJECT/"
  READY=1
  write_instance
  echo "LAUNCH: ready cli=$CLI project=$PROJECT evidence=$EVIDENCE"
}

cmd_doctor() {
  load_instance || {
    echo "DOCTOR: no verification project. Run: bash scripts/verify.sh launch" >&2
    return 1
  }
  [[ "$READY" == "1" && -x "$CLI" ]] || {
    echo "DOCTOR: install is not ready" >&2
    return 1
  }
  local expected actual py_version
  expected="$(package_version "$VENV/bin/python")"
  actual="$("$CLI" --version 2>&1 || true)"
  py_version="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  {
    echo "expected: $expected"
    echo "cli: $actual"
    echo "python: $py_version"
  } >"$EVIDENCE/doctor.txt"
  case "$py_version" in
    3.10|3.11|3.12|3.13) ;;
    *)
      echo "DOCTOR: python $py_version is outside 3.10-3.13" >&2
      return 1
      ;;
  esac
  if [[ "$actual" != *"$expected"* ]]; then
    echo "DOCTOR: '$actual' does not contain __version__ $expected" >&2
    return 1
  fi
  local module_version
  module_version="$("$VENV/bin/python" -m coop_data_doc --version 2>&1 || true)"
  echo "module: $module_version" >>"$EVIDENCE/doctor.txt"
  if [[ "$module_version" != *"$expected"* ]]; then
    echo "DOCTOR: python -m coop_data_doc --version is '$module_version'" >&2
    return 1
  fi
  if ! run_cli doctor-help "$PROJECT" help build; then
    echo "DOCTOR: help build failed" >&2
    return 1
  fi
  if ! grep -q "searchable HTML portal" "$EVIDENCE/doctor-help.out"; then
    echo "DOCTOR: help build is not this CLI" >&2
    return 1
  fi
  if [[ ! -f "$PROJECT/coop-data-doc.yml" ]]; then
    echo "DOCTOR: fixture was not copied" >&2
    return 1
  fi
  keep doctor.txt
  echo "DOCTOR: ok version=$expected python=$py_version evidence=$EVIDENCE"
}

copy_artifact() {
  local src="$1"
  local dest="$2"
  mkdir -p "$EVIDENCE/artifacts"
  cp -a "$src" "$EVIDENCE/artifacts/$dest"
  keep "artifacts/$dest"
}

assert_exit() {
  local name="$1"
  local want="$2"
  local got
  got="$(cat "$EVIDENCE/$name.exit")"
  if [[ "$got" != "$want" ]]; then
    echo "----- $name stdout -----" >&2
    cat "$EVIDENCE/$name.out" >&2
    echo "----- $name stderr -----" >&2
    cat "$EVIDENCE/$name.err" >&2
    die "$name exited $got, expected $want"
  fi
}

assert_grep() {
  local file="$1"
  local needle="$2"
  if ! grep -F -q -- "$needle" "$file"; then
    die "missing $(printf '%q' "$needle") in $file"
  fi
}

py() {
  "$VENV/bin/python" - "$@"
}

py_ok() {
  py "$@" || die "assertion failed"
}

drive_project() {
  require_ready
  run_cli init "$SCRATCH" init || true
  assert_exit init 0
  assert_grep "$EVIDENCE/init.out" "Wrote "
  assert_grep "$SCRATCH/coop-data-doc.yml" 'project_name: "Coop BI Estate"'
  run_cli init-again "$SCRATCH" init || true
  assert_exit init-again 1
  assert_grep "$EVIDENCE/init-again.txt" "already exists"
  printf '%s\n' '{"project_name": "Scratch Estate"}' >"$WORK/patch.json"
  run_cli config-set "$SCRATCH" config-set --config "$SCRATCH/coop-data-doc.yml" --from-json "$WORK/patch.json"
  assert_exit config-set 0
  assert_grep "$EVIDENCE/config-set.out" "saved, not runnable yet"
  run_cli show-config-scratch "$SCRATCH" show-config --config "$SCRATCH/coop-data-doc.yml"
  assert_exit show-config-scratch 0
  py_ok "$EVIDENCE/show-config-scratch.out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["project_name"] == "Scratch Estate", data["project_name"]
assert data["exists"] is True
PY
  run_cli show-config "$PROJECT" show-config
  assert_exit show-config 0
  py_ok "$EVIDENCE/show-config.out" "$PROJECT/coop-data-doc.yml" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["project_name"] == "Verify Estate", data["project_name"]
assert data["exists"] is True
assert data["path"] == sys.argv[2]
assert data["repos"]["sql"]["path"] == "./sql"
assert data["output"]["dir"] == "./data-docs"
PY
  run_cli folders "$PROJECT" folders
  assert_exit folders 0
  py_ok "$EVIDENCE/folders.out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
by_repo = {row["repo"]: row for row in data["repos"]}
sql = by_repo["sql"]
assert sql["exists"] is True
assert sql["mode"] == "allowlist"
documented = {item["name"]: item["documented"] for item in sql["folders"]}
assert documented == {"procs": True, "tables": True, "views": True}, documented
pbi = by_repo["powerbi"]
assert pbi["exists"] is True
assert pbi["mode"] == "legacy"
names = {item["name"] for item in pbi["folders"]}
assert names == {"Sales.Report", "Sales.SemanticModel"}, names
assert all(item["documented"] for item in pbi["folders"])
PY
  run_cli status-before "$PROJECT" status || true
  assert_exit status-before 0
  assert_grep "$EVIDENCE/status-before.out" "freshness: no docs yet"
  assert_grep "$EVIDENCE/status-before.out" "(missing)"
  assert_grep "$EVIDENCE/status-before.out" "unresolved: 1"
  note "project: init Coop BI Estate, config-set, show-config, folders, status (no docs, 1 unresolved)"
  echo "PROJECT: PASS"
}

assert_nodes() {
  local graph="$1"
  py_ok "$graph" "$VIEW" "$PBI_EXACT" "$PBI_ALIAS" "$CUSTOMERS" "$ORDERS" "$PROC" "$MODEL" "$REPORT" "$MEASURE" <<'PY'
import json, sys
graph = json.load(open(sys.argv[1], encoding="utf-8"))
expected = set(sys.argv[2:])
found = set(graph["nodes"])
if found != expected:
    sys.stderr.write("nodes:\n" + "\n".join(sorted(found)) + "\n")
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    if missing:
        sys.stderr.write("missing: " + ", ".join(missing) + "\n")
    if extra:
        sys.stderr.write("extra: " + ", ".join(extra) + "\n")
    sys.exit(1)
PY
}

assert_edge() {
  local graph="$1"
  local source="$2"
  local target="$3"
  local kind="$4"
  py_ok "$graph" "$source" "$target" "$kind" <<'PY'
import json, sys
graph = json.load(open(sys.argv[1], encoding="utf-8"))
source, target, kind = sys.argv[2:]
for edge in graph["edges"]:
    if edge["source_id"] == source and edge["target_id"] == target and edge["edge_type"] == kind:
        sys.exit(0)
sys.stderr.write("missing edge %s -%s-> %s\n" % (source, kind, target))
sys.exit(1)
PY
}

assert_no_edge() {
  local graph="$1"
  local source="$2"
  local target="$3"
  local kind="$4"
  if py "$graph" "$source" "$target" "$kind" <<'PY'
import json, sys
graph = json.load(open(sys.argv[1], encoding="utf-8"))
source, target, kind = sys.argv[2:]
for edge in graph["edges"]:
    if edge["source_id"] == source and edge["target_id"] == target and edge["edge_type"] == kind:
        sys.exit(0)
sys.exit(1)
PY
  then
    die "unexpected edge $source -$kind-> $target in $graph"
  fi
}

drive_scan() {
  require_ready
  [[ ! -f "$PROJECT/.lineage-cache.json" ]] || die "scan precondition failed: lineage cache already exists"
  run_cli scan "$PROJECT" scan --non-interactive --jobs 1
  assert_exit scan 0
  [[ -f "$PROJECT/data-docs/graph.json" ]] || die "scan did not write graph.json"
  [[ ! -e "$PROJECT/data-docs-site/index.html" ]] || die "scan wrote the HTML portal"
  [[ ! -e "$PROJECT/data-docs/index.md" ]] || die "scan wrote markdown pages"
  assert_nodes "$PROJECT/data-docs/graph.json"
  assert_edge "$PROJECT/data-docs/graph.json" "$VIEW" "$CUSTOMERS" reads
  assert_edge "$PROJECT/data-docs/graph.json" "$PROC" "$ORDERS" writes
  assert_edge "$PROJECT/data-docs/graph.json" "$VIEW" "$PBI_EXACT" feeds
  assert_no_edge "$PROJECT/data-docs/graph.json" "$VIEW" "$PBI_ALIAS" feeds
  assert_grep "$EVIDENCE/scan.err" "1 unresolved"
  copy_artifact "$PROJECT/data-docs/graph.json" graph-scan.json
  note "scan: graph.json with the exact feeds edge and 1 unresolved alias; no HTML"
  echo "SCAN: PASS"
}

drive_build() {
  require_ready
  [[ -f "$PROJECT/data-docs/graph.json" ]] || die "build precondition failed: run drive scan first"
  run_cli build "$PROJECT" build --non-interactive --jobs 1
  assert_exit build 0
  assert_grep "$EVIDENCE/build.err" "Markdown docs:"
  assert_grep "$EVIDENCE/build.err" "HTML portal:"
  assert_grep "$EVIDENCE/build.err" "1 unresolved"
  [[ -f "$PROJECT/data-docs/index.md" ]] || die "build did not write index.md"
  [[ -f "$PROJECT/data-docs/manifest.json" ]] || die "build did not write manifest.json"
  [[ -f "$PROJECT/data-docs-site/index.html" ]] || die "build did not write index.html"
  [[ -f "$PROJECT/data-docs-site/search/search_index.json" ]] || die "build did not write the offline search index"
  assert_grep "$PROJECT/data-docs/index.md" "# Verify Estate"
  assert_grep "$PROJECT/data-docs-site/index.html" "Verify Estate"
  assert_grep "$PROJECT/data-docs-site/search/search_index.json" "dim_customer"
  local page
  page="$(find "$PROJECT/data-docs/view" -name '*.md' -print)"
  [[ -n "$page" ]] || die "build did not write a view page"
  assert_grep "$page" 'id: "view:sales.dim_customer"'
  assert_grep "$page" "gold_table:dbo.customers"
  cmp -s "$PROJECT/data-docs/graph.json" "$PROJECT/data-docs/manifest.json" || die "graph.json and manifest.json differ"
  assert_edge "$PROJECT/data-docs/graph.json" "$MODEL" "$REPORT" feeds
  copy_artifact "$PROJECT/data-docs/index.md" index.md
  copy_artifact "$PROJECT/data-docs-site/index.html" index.html
  note "build: markdown, manifest, and the offline HTML portal for Verify Estate"
  echo "BUILD: PASS"
}

drive_export() {
  require_ready
  [[ -f "$PROJECT/data-docs/index.md" ]] || die "export precondition failed: run drive build first"
  run_cli export "$PROJECT" export
  assert_exit export 0
  assert_grep "$EVIDENCE/export.out" "Exported objects, columns, measures, and edges"
  for name in objects.csv columns.csv measures.csv edges.csv; do
    [[ -f "$PROJECT/data-docs/$name" ]] || die "export did not write $name"
  done
  assert_grep "$PROJECT/data-docs/objects.csv" "$VIEW"
  assert_grep "$PROJECT/data-docs/objects.csv" "$PBI_ALIAS"
  assert_grep "$PROJECT/data-docs/columns.csv" "customer_id"
  assert_grep "$PROJECT/data-docs/measures.csv" "CustomerCount"
  assert_grep "$PROJECT/data-docs/measures.csv" "COUNTROWS"
  assert_grep "$PROJECT/data-docs/edges.csv" "$PBI_EXACT"
  if grep -F -q -- "$PBI_ALIAS" "$PROJECT/data-docs/edges.csv"; then
    # The alias table feeds its model even before the SQL link exists.
    # The SQL feeds edge is the one that must still be absent.
    if grep -F -q -- "linker: cache" "$PROJECT/data-docs/edges.csv"; then
      die "export wrote the cached SQL link before resolve-apply"
    fi
  fi
  copy_artifact "$PROJECT/data-docs/objects.csv" objects.csv
  copy_artifact "$PROJECT/data-docs/edges.csv" edges-before-cache.csv
  note "export: objects, columns, measures, and edges CSVs from the built graph"
  echo "EXPORT: PASS"
}

drive_lineage() {
  require_ready
  [[ -f "$PROJECT/data-docs/graph.json" ]] || die "lineage precondition failed: no graph.json"
  run_cli lineage-ambiguous "$PROJECT" lineage sales.dim_customer
  assert_exit lineage-ambiguous 0
  py_ok "$EVIDENCE/lineage-ambiguous.out" "$VIEW" "$PBI_EXACT" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["ambiguous"] is True
ids = {item["id"] for item in data["matches"]}
assert ids == {sys.argv[2], sys.argv[3]}, ids
linked = [hit for hit in data["loaded_by"] if hit["table"]["id"] == sys.argv[3]]
assert len(linked) == 1, data["loaded_by"]
assert linked[0]["linked"] is True
assert linked[0]["source"] == "sales.dim_customer"
PY
  run_cli lineage-view "$PROJECT" lineage "$VIEW" --depth 2
  assert_exit lineage-view 0
  py_ok "$EVIDENCE/lineage-view.out" "$VIEW" "$CUSTOMERS" "$PBI_EXACT" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["object"]["id"] == sys.argv[2]
up = {item["id"] for item in data["upstream"]}
down = {item["id"] for item in data["downstream"]}
assert sys.argv[3] in up, sorted(up)
assert sys.argv[4] in down, sorted(down)
assert "evidence" in data
PY
  run_cli lineage-column "$PROJECT" lineage "$VIEW" --column customer_id
  assert_exit lineage-column 0
  py_ok "$EVIDENCE/lineage-column.out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["column"] == "customer_id"
assert "dbo.customers.customer_id" in data["source_columns"], data["source_columns"]
PY
  copy_artifact "$EVIDENCE/lineage-view.out" lineage-view.json
  copy_artifact "$EVIDENCE/lineage-column.out" lineage-column.json
  note "lineage: ambiguous sales.dim_customer, view upstream customers, column customer_id"
  echo "LINEAGE: PASS"
}

drive_lineage_cache() {
  require_ready
  [[ -f "$PROJECT/data-docs/graph.json" ]] || die "lineage-cache precondition failed: no graph.json"
  [[ ! -f "$PROJECT/.lineage-cache.json" ]] || die "lineage-cache precondition failed: cache already exists"
  cp -a "$PROJECT/data-docs/graph.json" "$WORK/baseline-graph.json"
  run_cli resolve "$PROJECT" resolve
  assert_exit resolve 0
  py_ok "$EVIDENCE/resolve.out" "$WORK/decisions.json" "$PBI_ALIAS" "$VIEW" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
items = data["unresolved"]
assert len(items) == 1, items
item = items[0]
assert item["cache_key"] == sys.argv[3], item
targets = {candidate["target"] for candidate in item["candidates"]}
if sys.argv[4] not in targets:
    sys.stderr.write("candidates: %s\n" % sorted(targets))
    sys.exit(1)
json.dump(
    {
        "decisions": [
            {
                "cache_key": item["cache_key"],
                "target": sys.argv[4],
                "source_signature": item["source_signature"],
            }
        ]
    },
    open(sys.argv[2], "w", encoding="utf-8"),
)
PY
  run_cli resolve-apply "$PROJECT" resolve-apply --from-json "$WORK/decisions.json"
  assert_exit resolve-apply 0
  assert_grep "$EVIDENCE/resolve-apply.out" "Applied 1 decision"
  [[ -f "$PROJECT/.lineage-cache.json" ]] || die "resolve-apply did not write .lineage-cache.json"
  find "$PROJECT" -name '.lineage-cache.json.*.tmp' -print | grep -q . && die "lineage cache left a temp file"
  py_ok "$PROJECT/.lineage-cache.json" "$PBI_ALIAS" "$VIEW" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["version"] == 1
entry = data["mappings"][sys.argv[2]]
assert entry["target"] == sys.argv[3]
assert entry["method"] == "interactive"
assert entry["source_signature"]
PY
  cp -a "$PROJECT/.lineage-cache.json" "$WORK/cache-before-build.json"
  run_cli build-cached "$PROJECT" build --non-interactive --strict --jobs 1
  assert_exit build-cached 0
  assert_grep "$EVIDENCE/build-cached.err" "0 unresolved"
  cmp -s "$PROJECT/.lineage-cache.json" "$WORK/cache-before-build.json" || die "build rewrote the lineage cache"
  assert_edge "$PROJECT/data-docs/graph.json" "$VIEW" "$PBI_ALIAS" feeds
  py_ok "$PROJECT/data-docs/graph.json" "$PBI_ALIAS" "$VIEW" <<'PY'
import json, sys
graph = json.load(open(sys.argv[1], encoding="utf-8"))
resolutions = graph["nodes"][sys.argv[2]]["metadata"]["source_resolutions"]
methods = {item["method"] for item in resolutions.values()}
assert methods == {"cache"}, methods
assert any(
    edge["source_id"] == sys.argv[3]
    and edge["target_id"] == sys.argv[2]
    and edge["edge_type"] == "feeds"
    and "linker: cache (sales.dim_cust)" in edge["evidence"]
    for edge in graph["edges"]
)
PY
  run_cli lineage-alias "$PROJECT" lineage "$PBI_ALIAS"
  assert_exit lineage-alias 0
  py_ok "$EVIDENCE/lineage-alias.out" "$PBI_ALIAS" "$VIEW" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["object"]["id"] == sys.argv[2]
up = {item["id"] for item in data["upstream"]}
assert sys.argv[3] in up, sorted(up)
PY
  run_cli export-after "$PROJECT" export
  assert_exit export-after 0
  assert_grep "$PROJECT/data-docs/edges.csv" "linker: cache (sales.dim_cust)"
  copy_artifact "$PROJECT/.lineage-cache.json" lineage-cache.json
  copy_artifact "$PROJECT/data-docs/graph.json" graph-after-cache.json
  copy_artifact "$PROJECT/data-docs/edges.csv" edges-after-cache.csv
  note "lineage-cache: resolve-apply saved one decision; rebuild linked it as cache"
  echo "LINEAGE-CACHE: PASS"
}

drive_check() {
  require_ready
  [[ -f "$PROJECT/.lineage-cache.json" ]] || die "check precondition failed: no lineage cache"
  run_cli check "$PROJECT" check --no-parse-cache
  assert_exit check 0
  assert_grep "$EVIDENCE/check.out" "docs are up to date"
  run_cli findings "$PROJECT" findings --no-parse-cache
  assert_exit findings 0
  py_ok "$EVIDENCE/findings.out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["tool"] == "coop-data-doc"
assert data["schema_version"] == 1
banned = {
    "regex_fallback",
    "dynamic_sql",
    "unresolved_partition_source",
    "ambiguous_visual_binding",
    "unresolved_reference",
}
rules = {item["rule"] for item in data["findings"]}
hit = sorted(rules & banned)
if hit:
    sys.stderr.write("findings: %s\n" % hit)
    sys.exit(1)
if any(item["severity"] == "error" for item in data["findings"]):
    sys.stderr.write(json.dumps(data["findings"], indent=2) + "\n")
    sys.exit(1)
PY
  run_cli impact "$PROJECT" impact --baseline "$WORK/baseline-graph.json"
  assert_exit impact 0
  py_ok "$EVIDENCE/impact.out" "$VIEW" "$PBI_ALIAS" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
down = data.get(sys.argv[2], [])
assert sys.argv[3] in down, data
PY
  # export writes the CSVs into the docs directory. The HTML build copies
  # them into the portal, so publish once more after the second export and
  # only then compare a later update to that tree.
  run_cli build-synced "$PROJECT" build --non-interactive --strict --jobs 1
  assert_exit build-synced 0
  mkdir -p "$WORK/snap"
  cp -a "$PROJECT/data-docs" "$WORK/snap/data-docs"
  cp -a "$PROJECT/data-docs-site" "$WORK/snap/data-docs-site"
  cp -a "$PROJECT/data-docs/graph.json" "$WORK/after-cache-graph.json"
  run_cli update "$PROJECT" update --non-interactive --strict --jobs 1
  assert_exit update 0
  assert_grep "$EVIDENCE/update.err" "alias of build"
  if ! diff -rq "$WORK/snap/data-docs" "$PROJECT/data-docs" >"$EVIDENCE/update-diff.txt"; then
    cat "$EVIDENCE/update-diff.txt" >&2
    die "update changed data-docs"
  fi
  if ! diff -rq "$WORK/snap/data-docs-site" "$PROJECT/data-docs-site" >"$EVIDENCE/update-site-diff.txt"; then
    cat "$EVIDENCE/update-site-diff.txt" >&2
    die "update changed the HTML portal"
  fi
  run_cli impact-same "$PROJECT" impact --baseline "$WORK/after-cache-graph.json"
  assert_exit impact-same 0
  py_ok "$EVIDENCE/impact-same.out" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data == {}, data
PY
  run_cli status-after "$PROJECT" status
  assert_exit status-after 0
  assert_grep "$EVIDENCE/status-after.out" "freshness: up to date"
  assert_grep "$EVIDENCE/status-after.out" "(exists)"
  assert_grep "$EVIDENCE/status-after.out" "unresolved: 0"
  [[ -f "$PROJECT/.coop-data-doc-parse-cache.json" ]] || die "build did not write the parse cache"
  note "check: docs up to date; findings clean; impact shows the cached link; update is byte-identical"
  echo "CHECK: PASS"
}

cmd_drive() {
  local feature="${1:-all}"
  case "$feature" in
    project) drive_project ;;
    scan) drive_scan ;;
    build) drive_build ;;
    export) drive_export ;;
    lineage) drive_lineage ;;
    lineage-cache) drive_lineage_cache ;;
    check) drive_check ;;
    all)
      drive_project
      drive_scan
      drive_build
      drive_export
      drive_lineage
      drive_lineage_cache
      drive_check
      ;;
    *)
      usage >&2
      return 2
      ;;
  esac
}

git_status() {
  git -C "$ROOT" status --porcelain | sort
}

on_exit() {
  local ec=$?
  trap - EXIT
  local saved_work="${WORK:-}"
  if [[ -f "$INSTANCE" ]]; then
    load_instance || true
  fi
  if [[ -n "${WORK:-}" ]]; then
    remove_work || true
  fi
  rm -f "$INSTANCE"
  if [[ -z "${EVIDENCE:-}" || ! -d "$EVIDENCE" ]]; then
    echo "RESULT: FAIL"
    exit 1
  fi
  local missing=0 line
  if [[ -f "$EVIDENCE/keep.txt" ]]; then
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      if [[ ! -f "$EVIDENCE/$line" ]]; then
        printf '%s\n' "evidence missing after cleanup: $line" >>"$SUMMARY"
        missing=1
      fi
    done <"$EVIDENCE/keep.txt"
  else
    printf '%s\n' "evidence missing after cleanup: keep.txt" >>"$SUMMARY"
    missing=1
  fi
  if [[ -n "$saved_work" && -d "$saved_work" ]]; then
    printf '%s\n' "cleanup: temp project still present at $saved_work" >>"$SUMMARY"
    missing=1
  fi
  printf '%s\n' "evidence: $EVIDENCE" >>"$SUMMARY"
  if [[ "$FAIL" -eq 0 && "$ec" -eq 0 && "$missing" -eq 0 ]]; then
    printf '%s\n' "RESULT: PASS" >>"$SUMMARY"
    echo
    echo "== summary =="
    cat "$SUMMARY"
    exit 0
  fi
  printf '%s\n' "RESULT: FAIL" >>"$SUMMARY"
  echo
  echo "== summary =="
  cat "$SUMMARY"
  exit 1
}

cmd_run() {
  FAIL=0
  EVIDENCE="$STATE_DIR/evidence/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  SUMMARY="$EVIDENCE/summary.txt"
  mkdir -p "$EVIDENCE"
  : >"$SUMMARY"
  local before after
  before="$(git_status)"
  if [[ -f "$INSTANCE" ]]; then
    cmd_cleanup || true
  fi
  trap on_exit EXIT
  cmd_launch "$EVIDENCE" || die "launch failed"
  note "launch: $CLI ($INSTALL_MODE)"
  cmd_doctor || die "doctor failed"
  note "doctor: ok"
  cmd_drive all || die "drive failed"
  after="$(git_status)"
  if [[ "$before" != "$after" ]]; then
    echo "----- git status before -----" >&2
    printf '%s\n' "$before" >&2
    echo "----- git status after -----" >&2
    printf '%s\n' "$after" >&2
    die "verification changed the checkout"
  fi
  note "checkout: unchanged"
}

command="${1:-run}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$command" in
  run) cmd_run ;;
  launch)
    cmd_launch || exit 1
    ;;
  doctor) cmd_doctor ;;
  drive) cmd_drive "$@" ;;
  cleanup) cmd_cleanup ;;
  -h|--help|help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
