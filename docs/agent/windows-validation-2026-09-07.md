# Native Windows validation — 2026-09-07

Starting revision: bdd0ad06b730695087978f32d1c3edd595038b8f.
Branch: feature/coop-desktop-windows-validation. core.autocrlf=false.
Windows 11 Pro 10.0.26200 x64, Python 3.12.10.

The full source suite reproduced inconsistent path separators in lineage.v1:
relative doc used forward slashes but absolute doc_path used Windows backslashes.
Absolute doc_path and source_path now consistently use Path.as_posix().
The existing native test verifies the doc suffix and that source_path opens a real file.

Commands from this isolated checkout:

- .venv/Scripts/python.exe -m pip install --disable-pip-version-check --no-cache-dir -e ".[dev]": exit 0.
- .venv/Scripts/python.exe -m pytest -q: baseline 633 passed, 1 failed; after fix 634 passed in 61.92s, exit 0.
- .venv/Scripts/python.exe -m ruff check src tests: All checks passed, exit 0.
- .venv/Scripts/python.exe -m ruff format --check src tests: 64 files already formatted, exit 0.

Backup: .backups/windows-20260907/cli.py.bak and README.md.bak (ignored).
Evidence logs: ../../outputs/evidence/coop-data-doc-desktop-*-fixed.log in the Windows validation parent.
This verifies source behavior only. Managed wheel inclusion and installed Desktop lineage acceptance remain open.
No release version, tag, publication, client workspace, or existing user installation changed.

The real Windows Node-to-Python JSONL setup run exposed a second issue: Python
pipes defaulted to cp1252, so the em dash in a prompt ID was corrupted by the
UTF-8 client and the returned ID failed protocol validation. JSONL now explicitly
configures standard input/output as UTF-8 before reading; terminal IO is unchanged.
A real subprocess regression sets PYTHONIOENCODING=cp1252 and PYTHONUTF8=0 and
round-trips an em dash, accented text, CJK and emoji in IDs/answers.

- .venv/Scripts/python.exe -m pytest -q: 635 passed in 247.37s, exit 0.
- .venv/Scripts/python.exe -m ruff check src tests: passed.
- .venv/Scripts/python.exe -m ruff format --check src tests: 64 files already formatted.
- Agent node tests/jsonl-live.test.mjs with this editable executable first on PATH:
  19 prompts answered, exactly one complete event, exit 0, generated config parsed.
  Logs: ../../outputs/evidence/data-doc-utf8-{full,lint}.log and jsonl-native-utf8-fixed.log.
