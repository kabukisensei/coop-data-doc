import sys
from pathlib import Path

from coop_data_doc import upgrade
from coop_data_doc.upgrade import (
    DependencyStatus,
    UpgradePlan,
    build_plan,
    classify_update,
    detect_install_method,
    direct_dependencies,
    manual_upgrade_command,
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
    assert "PyPI unreachable" in plan.tool_note


def make_plan(method, checkout=None, tool_note="", safe=()):
    return UpgradePlan(
        install_method=method,
        checkout=checkout,
        tool_installed="0.1.0",
        tool_note=tool_note,
        dependencies=[
            DependencyStatus(name=name, installed="1.0.0", latest="1.2.0", kind="safe") for name in safe
        ],
    )


def git_plan(method):
    """A plan for a pipx/uv-tool app installed from a git URL."""
    git_url = "git+https://github.com/kabukisensei/coop-data-doc.git"
    return UpgradePlan(
        install_method=method,
        checkout=None,
        tool_installed="0.13.0",
        tool_note=f"installed from {git_url}; upgrading re-pulls the latest commit",
        pip_spec=git_url,
    )


def test_vcs_detection_ignores_plus_in_local_paths():
    # a local/editable path containing "+" must NOT be treated as a VCS install
    from coop_data_doc.upgrade import is_vcs_spec

    assert is_vcs_spec("git+https://github.com/x/y.git")
    assert is_vcs_spec("git+ssh://git@host/x.git@main")
    assert not is_vcs_spec("-e /home/u/c++proj/coop-data-doc")
    assert not is_vcs_spec("file:///home/u/c++proj/coop-data-doc")
    assert not is_vcs_spec("/home/u/c++proj/coop-data-doc")
    assert not is_vcs_spec(None)


def test_pipx_editable_local_path_is_not_vcs():
    # pipx install whose recorded spec is a local "+"-containing path -> NOT VCS,
    # so it must fall through to the plain `pipx upgrade` command, never a
    # broken reinstall token.
    plan = UpgradePlan(
        install_method="pipx",
        checkout=None,
        tool_installed="0.14.0",
        tool_note="",
        pip_spec="-e /home/u/c++proj/coop-data-doc",
    )
    assert not plan.is_vcs_install
    assert manual_upgrade_command(plan) == "pipx upgrade coop-data-doc"


def test_build_plan_pipx_from_git_reads_origin(monkeypatch):
    # pipx git installs still carry direct_url.json — read it like a pip install
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/pipx/venvs/coop-data-doc")
    monkeypatch.setattr(upgrade, "direct_dependencies", lambda: [])
    git_url = "git+https://github.com/kabukisensei/coop-data-doc.git"
    plan = build_plan(fetch=lambda _n: None, origin=lambda: git_url)
    assert plan.install_method == "pipx"
    assert plan.pip_spec == git_url
    assert plan.is_vcs_install
    assert "re-pulls" in plan.tool_note  # not the misleading "not on PyPI" note


def test_build_plan_pipx_from_pypi_uses_upgrade(monkeypatch):
    # a pipx PyPI install (no direct_url) keeps the plain `pipx upgrade` command
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/pipx/venvs/coop-data-doc")
    monkeypatch.setattr(upgrade, "direct_dependencies", lambda: [])
    plan = build_plan(fetch=lambda _n: "9.9.9", origin=lambda: None)
    assert plan.install_method == "pipx"
    assert plan.pip_spec is None
    assert not plan.is_vcs_install
    assert manual_upgrade_command(plan) == "pipx upgrade coop-data-doc"


def test_build_plan_pip_from_git_reinstalls_from_url(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "plain"))
    monkeypatch.setattr(upgrade, "direct_dependencies", lambda: [])
    git_url = "git+https://github.com/kabukisensei/coop-data-doc.git"
    plan = build_plan(
        fetch=lambda _n: None,
        origin=lambda: git_url,
    )
    assert plan.install_method == "pip"
    assert plan.pip_spec == git_url
    assert "re-pulls" in plan.tool_note  # not the misleading "not on PyPI" note
    assert manual_upgrade_command(plan) == f'python -m pip install -U --force-reinstall "{git_url}"'


def test_pip_without_origin_falls_back_to_pypi(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "plain"))
    monkeypatch.setattr(upgrade, "direct_dependencies", lambda: [])
    plan = build_plan(fetch=lambda _n: None, origin=lambda: None)
    assert plan.pip_spec is None
    assert manual_upgrade_command(plan) == "python -m pip install -U coop-data-doc"


# ---- manual upgrade command (the printed guidance) ---------------------------


def test_manual_upgrade_command_per_method(tmp_path):
    git_url = "git+https://github.com/kabukisensei/coop-data-doc.git"
    assert manual_upgrade_command(make_plan("pipx")) == "pipx upgrade coop-data-doc"
    assert manual_upgrade_command(git_plan("pipx")) == "pipx reinstall coop-data-doc"
    assert manual_upgrade_command(make_plan("uv-tool")) == "uv tool upgrade coop-data-doc"
    assert manual_upgrade_command(git_plan("uv-tool")) == f"uv tool install --force {git_url}"
    assert manual_upgrade_command(make_plan("pip")) == "python -m pip install -U coop-data-doc"
    assert (
        manual_upgrade_command(git_plan("pip")) == f'python -m pip install -U --force-reinstall "{git_url}"'
    )
    # git-checkout is multi-step: newline-separated, NEVER "&&" (PowerShell 5.1
    # can't parse it). Both steps must be present.
    checkout_cmd = manual_upgrade_command(make_plan("git-checkout", checkout=tmp_path))
    assert "&&" not in checkout_cmd
    assert checkout_cmd.splitlines() == [
        f'git -C "{tmp_path}" pull --ff-only',
        f'python -m pip install -U "{tmp_path}"',
    ]
