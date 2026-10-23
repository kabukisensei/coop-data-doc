"""Make the src layout importable when the package isn't installed."""

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _pin_serial_sql_parse(monkeypatch):
    """Default every test to serial SQL parsing (COOP_DATA_DOC_JOBS=1) so the
    suite doesn't pay ProcessPoolExecutor spawn cost on every build — the tiny
    fixture estate has >=4 SQL files, which would otherwise trip the pool.

    This only sets the *default* worker count. The dedicated parallel-parse
    tests pass an explicit ``--jobs N`` (which always wins over the env default)
    or set ``COOP_DATA_DOC_JOBS`` themselves, so they still exercise the real
    pool. Determinism guarantees a serial-default run is byte-identical to a
    parallel one, so pinning the default does not weaken any assertion.
    """
    monkeypatch.setenv("COOP_DATA_DOC_JOBS", "1")


@pytest.fixture(autouse=True)
def _fence_config_discovery(monkeypatch, tmp_path_factory):
    """Never let a test discover a coop-data-doc.yml above its own folder.

    Upward discovery walks to the filesystem root, so a test that runs the CLI in
    a temp folder without a config (e.g. ``status`` with no config) would find a
    real config in the developer's home or above the checkout and start scanning
    that estate (the 2026-10-23 audit hit exactly this). The fence
    ``COOP_DATA_DOC_DISCOVERY_ROOTS`` limits discovery to the suite's temp root
    and this checkout. Tests that drive discovery themselves still work: every
    tmp_path lives under the temp root.
    """
    checkout = Path(__file__).resolve().parent.parent
    roots = [str(tmp_path_factory.getbasetemp().resolve()), str(checkout)]
    monkeypatch.setenv("COOP_DATA_DOC_DISCOVERY_ROOTS", os.pathsep.join(roots))
