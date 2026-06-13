import subprocess
import sys
from pathlib import Path

import pytest

from coop_data_doc import upgrade
from coop_data_doc.upgrade import (
    DependencyStatus,
    UpgradeError,
    UpgradePlan,
    apply_plan,
    build_plan,
    classify_update,
    detect_install_method,
    direct_dependencies,
)


# ---- classification (pure) ---------------------------------------------------


def test_classify_update():
    assert classify_update("1.2.3", "1.2.3") == "current"
    assert classify_update("1.2.3", "1.2.4") == "safe"
    assert classify_update("1.2.3", "1.9.0") == "safe"
    assert classify_update("1.2.3", "2.0.0") == "major"
    assert classify_update("1.2.3", None) == "unknown"
    assert classify_update("1.2.3", "garbage") == "unknown"
    # latest older than installed (e.g. pre-release installed) -> current
    assert classify_update("1.3.0", "1.2.9") == "current"


def test_direct_dependencies_excludes_extras():
    names = direct_dependencies()
    assert "sqlglot" in names and "click" in names and "questionary" in names
    assert "pytest" not in names  # dev extra
    assert "ruff" not in names


# ---- install-method detection -------------------------------------------------


def test_detect_pipx(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/pipx/venvs/coop-data-doc")
    method, checkout = detect_install_method()
    assert method == "pipx" and checkout is None


def test_detect_uv_tool(monkeypatch):
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/share/uv/tools/coop-data-doc")
    method, checkout = detect_install_method()
    assert method == "uv-tool" and checkout is None


def test_detect_git_checkout(tmp_path: Path, monkeypatch):
    root = tmp_path / "clone"
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text('name = "coop-data-doc"\n', encoding="utf-8")
    (root / ".venv").mkdir()
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    method, checkout = detect_install_method()
    assert method == "git-checkout"
    assert checkout == root


def test_detect_plain_pip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "some-venv"))
    method, checkout = detect_install_method()
    assert method == "pip" and checkout is None


# ---- plan building (network mocked) -------------------------------------------


def fake_fetch(versions):
    def fetch(name):
        return versions.get(name)

    return fetch


def test_build_plan_classifies_dependencies(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "plain"))
    monkeypatch.setattr(upgrade, "direct_dependencies", lambda: ["alpha", "beta", "gamma"])
    installed = {"alpha": "1.0.0", "beta": "2.0.0", "gamma": "3.0.0"}
    latest = {"alpha": "1.1.0", "beta": "3.0.0", "gamma": "3.0.0", "coop-data-doc": None}
    plan = build_plan(
        fetch=fake_fetch(latest),
        installed_version_of=lambda name: installed[name],
    )
    kinds = {d.name: d.kind for d in plan.dependencies}
    assert kinds == {"alpha": "safe", "beta": "major", "gamma": "current"}
    assert [d.name for d in plan.safe_updates] == ["alpha"]
    assert [d.name for d in plan.major_updates] == ["beta"]
    assert "not on PyPI yet, or offline" in plan.tool_note


def recording_runner(results=None):
    calls = []

    def runner(command, capture_output=True, text=True):
        calls.append(command)
        rc, out = (results or {}).get(tuple(command[:3]), (0, ""))
        return subprocess.CompletedProcess(command, rc, stdout=out, stderr="")

    runner.calls = calls
    return runner


# ---- applying (subprocess mocked) ---------------------------------------------


def make_plan(method, checkout=None, tool_note="", safe=()):
    return UpgradePlan(
        install_method=method,
        checkout=checkout,
        tool_installed="0.1.0",
        tool_note=tool_note,
        dependencies=[
            DependencyStatus(name=name, installed="1.0.0", latest="1.2.0", kind="safe")
            for name in safe
        ],
    )


def test_apply_pipx():
    runner = recording_runner()
    executed = apply_plan(make_plan("pipx", safe=["alpha"]), runner=runner)
    assert executed == [["pipx", "upgrade", "coop-data-doc"]]  # pipx manages deps itself


def test_apply_uv_tool():
    runner = recording_runner()
    executed = apply_plan(make_plan("uv-tool"), runner=runner)
    assert executed == [["uv", "tool", "upgrade", "coop-data-doc"]]


def test_apply_pip_with_safe_deps_never_majors(tmp_path):
    runner = recording_runner()
    plan = make_plan("pip", safe=["alpha", "beta"])
    plan.dependencies.append(
        DependencyStatus(name="danger", installed="1.0.0", latest="2.0.0", kind="major")
    )
    executed = apply_plan(plan, runner=runner)
    assert executed[0] == [sys.executable, "-m", "pip", "install", "-q", "-U", "coop-data-doc"]
    assert executed[1] == [
        sys.executable, "-m", "pip", "install", "-q", "-U", "alpha<2", "beta<2",
    ]
    flat = " ".join(" ".join(c) for c in executed)
    assert "danger" not in flat  # major bumps are never auto-applied


def test_apply_git_checkout_pulls_when_behind(tmp_path):
    runner = recording_runner()
    plan = make_plan(
        "git-checkout", checkout=tmp_path, tool_note="3 new commit(s) available"
    )
    executed = apply_plan(plan, runner=runner)
    assert executed[0] == ["git", "-C", str(tmp_path), "pull", "--ff-only"]
    assert executed[1] == [
        sys.executable, "-m", "pip", "install", "-q", "-U", str(tmp_path),
    ]


def test_apply_git_checkout_no_upstream_skips_pull(tmp_path):
    runner = recording_runner()
    plan = make_plan("git-checkout", checkout=tmp_path, tool_note="no upstream remote")
    executed = apply_plan(plan, runner=runner)
    assert executed == [
        [sys.executable, "-m", "pip", "install", "-q", "-U", str(tmp_path)],
    ]


def test_apply_raises_friendly_error_on_failure():
    def failing_runner(command, capture_output=True, text=True):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    with pytest.raises(UpgradeError, match="boom"):
        apply_plan(make_plan("pipx"), runner=failing_runner)
