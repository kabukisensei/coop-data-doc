"""Make the src layout importable when the package isn't installed."""

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
