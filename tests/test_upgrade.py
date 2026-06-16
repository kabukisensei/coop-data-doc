import subprocess
import sys
from pathlib import Path

import pytest

from coop_data_doc import upgrade
from coop_data_doc.upgrade import (
    DependencyStatus,
    LauncherLockedError,
    UpgradeError,
    UpgradePlan,
    apply_plan,
    build_plan,
    classify_update,
    detect_install_method,
    direct_dependencies,
    manual_upgrade_command,
    manual_upgrade_message,
    needs_fresh_shell,
    running_launcher_path,
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
            DependencyStatus(name=name, installed="1.0.0", latest="1.2.0", kind="safe") for name in safe
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


def test_apply_pipx_from_git_reinstalls():
    # `pipx upgrade` no-ops on a git install, and `pipx install --force` FAILS
    # under pipx's uv backend ("venv already exists"). `pipx reinstall` re-pulls
    # the recorded git spec and works on both backends — do not "simplify" this.
    executed = apply_plan(git_plan("pipx"), runner=recording_runner())
    assert executed == [["pipx", "reinstall", "coop-data-doc"]]


def test_apply_uv_tool_from_git_force_installs():
    # uv tool install --force DOES re-fetch + re-pull a moving branch (verified)
    git_url = "git+https://github.com/kabukisensei/coop-data-doc.git"
    executed = apply_plan(git_plan("uv-tool"), runner=recording_runner())
    assert executed == [["uv", "tool", "install", "--force", git_url]]


def test_vcs_detection_ignores_plus_in_local_paths():
    # a local/editable path containing "+" must NOT be treated as a VCS install
    from coop_data_doc.upgrade import is_vcs_spec

    assert is_vcs_spec("git+https://github.com/x/y.git")
    assert is_vcs_spec("git+ssh://git@host/x.git@main")
    assert not is_vcs_spec("-e /home/u/c++proj/coop-data-doc")
    assert not is_vcs_spec("file:///home/u/c++proj/coop-data-doc")
    assert not is_vcs_spec("/home/u/c++proj/coop-data-doc")
    assert not is_vcs_spec(None)


def test_apply_pipx_editable_local_path_uses_upgrade():
    # pipx install whose recorded spec is a local "+"-containing path -> NOT VCS,
    # so it must fall through to `pipx upgrade`, never a broken reinstall token
    plan = UpgradePlan(
        install_method="pipx",
        checkout=None,
        tool_installed="0.14.0",
        tool_note="",
        pip_spec="-e /home/u/c++proj/coop-data-doc",
    )
    assert not plan.is_vcs_install
    assert apply_plan(plan, runner=recording_runner()) == [["pipx", "upgrade", "coop-data-doc"]]


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
    # a pipx PyPI install (no direct_url) keeps the plain `pipx upgrade` path
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/pipx/venvs/coop-data-doc")
    monkeypatch.setattr(upgrade, "direct_dependencies", lambda: [])
    plan = build_plan(fetch=lambda _n: "9.9.9", origin=lambda: None)
    assert plan.install_method == "pipx"
    assert plan.pip_spec is None
    assert not plan.is_vcs_install
    assert apply_plan(plan, runner=recording_runner()) == [["pipx", "upgrade", "coop-data-doc"]]


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
    executed = apply_plan(plan, runner=recording_runner())
    assert executed[0] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "-U",
        "--force-reinstall",
        git_url,
    ]


def test_pip_without_origin_falls_back_to_pypi(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "plain"))
    monkeypatch.setattr(upgrade, "direct_dependencies", lambda: [])
    plan = build_plan(fetch=lambda _n: None, origin=lambda: None)
    assert plan.pip_spec is None
    executed = apply_plan(plan, runner=recording_runner())
    assert executed[0] == [sys.executable, "-m", "pip", "install", "-q", "-U", "coop-data-doc"]


def test_apply_pip_with_safe_deps_never_majors(tmp_path):
    runner = recording_runner()
    plan = make_plan("pip", safe=["alpha", "beta"])
    plan.dependencies.append(DependencyStatus(name="danger", installed="1.0.0", latest="2.0.0", kind="major"))
    executed = apply_plan(plan, runner=runner)
    assert executed[0] == [sys.executable, "-m", "pip", "install", "-q", "-U", "coop-data-doc"]
    assert executed[1] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "-U",
        "alpha<2",
        "beta<2",
    ]
    flat = " ".join(" ".join(c) for c in executed)
    assert "danger" not in flat  # major bumps are never auto-applied


def test_apply_git_checkout_pulls_when_behind(tmp_path):
    runner = recording_runner()
    plan = make_plan("git-checkout", checkout=tmp_path, tool_note="3 new commit(s) available")
    executed = apply_plan(plan, runner=runner)
    assert executed[0] == ["git", "-C", str(tmp_path), "pull", "--ff-only"]
    assert executed[1] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-q",
        "-U",
        str(tmp_path),
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


# ---- Windows running-launcher lock -------------------------------------------
#
# Tests force sys.platform="win32" on whatever OS they run, so they use
# forward-slash launcher paths: PurePosixPath (the ambient Path off Windows)
# won't split backslashes, whereas a real WindowsPath splits both. "/" parses
# correctly under both flavours, so the assertions hold cross-platform.

WIN_EXE = "C:/Users/quiddity/.local/bin/coop-data-doc.exe"


def _as_windows_launcher(monkeypatch, argv0=WIN_EXE):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", [argv0, "upgrade"])


def test_running_launcher_path_none_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", [WIN_EXE, "upgrade"])
    assert running_launcher_path() is None


def test_running_launcher_path_detects_exe(monkeypatch):
    _as_windows_launcher(monkeypatch)
    launcher = running_launcher_path()
    assert launcher is not None and launcher.name == "coop-data-doc.exe"


def test_running_launcher_path_none_when_launched_via_python_m(monkeypatch):
    # `python -m coop_data_doc upgrade` -> argv[0] is a .py file, not the .exe,
    # so the launcher isn't locked and self-replacement is fine.
    _as_windows_launcher(monkeypatch, argv0="C:/x/coop_data_doc/__main__.py")
    assert running_launcher_path() is None


def test_running_launcher_path_none_with_empty_argv(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", [])
    assert running_launcher_path() is None


def test_needs_fresh_shell_scoped_to_managed_tools(monkeypatch):
    _as_windows_launcher(monkeypatch)
    # pipx/uv-tool always re-link the launcher -> proactively blocked
    assert needs_fresh_shell(make_plan("pipx")) is not None
    assert needs_fresh_shell(make_plan("uv-tool")) is not None
    # pip/git-checkout may do useful partial work -> handled reactively, not here
    assert needs_fresh_shell(make_plan("pip")) is None
    assert needs_fresh_shell(make_plan("git-checkout")) is None


def test_needs_fresh_shell_none_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", [WIN_EXE, "upgrade"])
    assert needs_fresh_shell(make_plan("pipx")) is None


def test_apply_pipx_on_locked_launcher_refuses_without_running_anything(monkeypatch):
    _as_windows_launcher(monkeypatch)
    runner = recording_runner()
    with pytest.raises(LauncherLockedError) as excinfo:
        apply_plan(make_plan("pipx"), runner=runner)
    assert runner.calls == []  # no doomed pipx churn
    assert "pipx upgrade coop-data-doc" in str(excinfo.value)
    assert "new terminal" in str(excinfo.value).lower()


# Realistic pip failure: the WinError 32 line — and EVERY mention of the package
# — sit above 5 package-agnostic trailing lines, so they all fall outside _run's
# last-5-lines message tail. The reactive net therefore can only see the marker
# by scanning the FULL stderr; if the `_run` full-stderr plumbing regressed and it
# fell back to str(exc) (the truncated tail), this test would fail. The precondition
# is asserted in the test so a future shallow edit can't silently neuter it.
_BURIED_LOCK_STDERR = "\n".join(
    [
        "Collecting coop-data-doc",
        "Installing collected packages: coop-data-doc",
        "  Attempting uninstall: coop-data-doc",
        r"ERROR: Could not install packages due to an OSError: [WinError 32] The process "
        r"cannot access the file because it is being used by another process: "
        r"'C:\\Users\\quiddity\\.local\\bin\\coop-data-doc.exe'",
        # --- the last 5 lines: no marker, no package name ---
        "  Rolling back uninstall of the previous version",
        r"  Moving to c:\\users\\quiddity\\appdata\\local\\temp\\pip-uninstall-a1b2c3\\",
        "WARNING: There was an error checking the latest version of pip.",
        "Consider using the `--user` option or check the permissions.",
        "note: This error originates from a subprocess, and is likely not a problem with pip.",
    ]
)


def test_apply_reactively_converts_buried_winerror32_to_guidance(monkeypatch):
    # pip isn't blocked proactively; if its re-link still hits the Windows lock,
    # the raw traceback is swapped for the actionable guidance — even when the
    # marker is buried far above pip's rollback trailer (the case str(exc) misses).
    tail = "\n".join(_BURIED_LOCK_STDERR.splitlines()[-5:])
    assert "WinError 32" not in tail and "coop-data-doc" not in tail  # genuinely buried

    _as_windows_launcher(monkeypatch, argv0="C:/x/coop_data_doc/__main__.py")  # not detected as launcher

    def locked_runner(command, capture_output=True, text=True):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=_BURIED_LOCK_STDERR)

    plan = UpgradePlan(install_method="pip", checkout=None, tool_installed="0.1.0", tool_note="")
    with pytest.raises(LauncherLockedError) as excinfo:
        apply_plan(plan, runner=locked_runner)
    assert "python -m pip install -U coop-data-doc" in str(excinfo.value)


def test_apply_unrelated_winerror32_is_not_mislabelled(monkeypatch):
    # WinError 32 on an UNRELATED file (a dep DLL), not via the launcher and not
    # naming our package, must stay a plain UpgradeError — not a launcher lock.
    _as_windows_launcher(monkeypatch, argv0="C:/x/coop_data_doc/__main__.py")  # not the launcher

    def locked_runner(command, capture_output=True, text=True):
        stderr = (
            "ERROR: Could not install packages due to an OSError: [WinError 32] The "
            "process cannot access the file because it is being used by another "
            r"process: 'C:\\venv\\Lib\\site-packages\\some_dependency\\_native.pyd'"
        )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    plan = UpgradePlan(install_method="pip", checkout=None, tool_installed="0.1.0", tool_note="")
    with pytest.raises(UpgradeError) as excinfo:
        apply_plan(plan, runner=locked_runner)
    assert not isinstance(excinfo.value, LauncherLockedError)


def test_apply_winerror32_via_launcher_is_converted(monkeypatch):
    # Even if the stderr doesn't name the package, being started via the .exe
    # launcher is enough attribution to convert.
    _as_windows_launcher(monkeypatch)  # argv0 IS coop-data-doc.exe

    def locked_runner(command, capture_output=True, text=True):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="OSError: [WinError 32] being used by another process"
        )

    plan = UpgradePlan(install_method="pip", checkout=None, tool_installed="0.1.0", tool_note="")
    with pytest.raises(LauncherLockedError):
        apply_plan(plan, runner=locked_runner)


def test_apply_non_lock_failure_still_raises_plain_upgrade_error(monkeypatch):
    # a genuine (non-lock) failure must NOT be relabelled as a launcher lock
    _as_windows_launcher(monkeypatch, argv0="C:/x/coop_data_doc/__main__.py")

    def failing_runner(command, capture_output=True, text=True):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="network unreachable")

    plan = UpgradePlan(install_method="pip", checkout=None, tool_installed="0.1.0", tool_note="")
    with pytest.raises(UpgradeError, match="network unreachable") as excinfo:
        apply_plan(plan, runner=failing_runner)
    assert not isinstance(excinfo.value, LauncherLockedError)


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
    # can't parse it). Both steps must be present and each indents in the message.
    checkout_cmd = manual_upgrade_command(make_plan("git-checkout", checkout=tmp_path))
    assert "&&" not in checkout_cmd
    assert checkout_cmd.splitlines() == [
        f'git -C "{tmp_path}" pull --ff-only',
        f'python -m pip install -U "{tmp_path}"',
    ]
    message = manual_upgrade_message(make_plan("git-checkout", checkout=tmp_path))
    assert "&&" not in message
    assert f'    python -m pip install -U "{tmp_path}"' in message  # every line indented
