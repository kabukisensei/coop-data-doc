"""End-to-end determinism: same inputs => byte-identical documentation,
regardless of where the build runs."""

from pathlib import Path

from test_cli import run, setup_workspace


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_full_build_twice_byte_identical(tmp_path: Path):
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    setup_workspace(ws_a)
    setup_workspace(ws_b)
    assert run(["build", "--non-interactive", "--skip-html"], ws_a).exit_code == 0
    assert run(["build", "--non-interactive", "--skip-html"], ws_b).exit_code == 0
    docs_a = tree_bytes(ws_a / "data-docs")
    docs_b = tree_bytes(ws_b / "data-docs")
    assert docs_a.keys() == docs_b.keys()
    for relative in docs_a:
        assert docs_a[relative] == docs_b[relative], f"non-deterministic: {relative}"


def test_rebuild_over_existing_output_idempotent(tmp_path: Path):
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    first = tree_bytes(tmp_path / "data-docs")
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    assert tree_bytes(tmp_path / "data-docs") == first


def test_warm_parse_cache_matches_cold_build(tmp_path: Path):
    """The incremental parse cache is transparent: a warm-cache build must be
    byte-identical to a cold build (issue #17's non-negotiable constraint).

    Sequence: cold (--no-parse-cache) -> warm (cache from the cold run's write) ->
    cold again. All three doc trees must match to the byte.
    """
    setup_workspace(tmp_path)
    # cold: cache is bypassed on read but still WRITTEN, so the next run is warm
    assert run(["build", "--non-interactive", "--skip-html", "--no-parse-cache"], tmp_path).exit_code == 0
    cold = tree_bytes(tmp_path / "data-docs")
    # warm: every SQL file is a cache hit
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    warm = tree_bytes(tmp_path / "data-docs")
    # cold again for good measure
    assert run(["build", "--non-interactive", "--skip-html", "--no-parse-cache"], tmp_path).exit_code == 0
    cold_again = tree_bytes(tmp_path / "data-docs")
    assert warm.keys() == cold.keys() == cold_again.keys()
    for relative in cold:
        assert warm[relative] == cold[relative], f"warm != cold: {relative}"
        assert cold_again[relative] == cold[relative], f"cold != cold: {relative}"
