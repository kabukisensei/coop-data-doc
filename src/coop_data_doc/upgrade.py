"""Tool self-update and dependency freshness (`coop-data-doc upgrade`).

This is the ONE part of the tool that intentionally touches the network
(PyPI metadata, `git fetch`). Documentation generation never imports this
module, so the offline guarantee for doc builds is untouched.

Pure logic (classification, planning) is separated from side effects
(network fetcher and subprocess runner are injectable) so tests never
need the network.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from coop_data_doc import __version__

PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"
NETWORK_TIMEOUT_SECONDS = 10
PACKAGE_NAME = "coop-data-doc"
# A pip spec from a version control system: "git+…", "hg+…", "svn+…", "bzr+…".
# Detect by scheme prefix, NOT a bare "+" substring — a local/editable path can
# legitimately contain "+" (e.g. /home/u/c++proj) and must not be mistaken for VCS.
_VCS_SPEC_RE = re.compile(r"^(git|hg|svn|bzr)\+")


def is_vcs_spec(spec: str | None) -> bool:
    """True when ``spec`` is a VCS install source (``git+https://…`` etc.)."""
    return bool(spec and _VCS_SPEC_RE.match(spec))


class UpgradeError(Exception):
    """A user-facing upgrade problem; the message is printable as-is."""


class LauncherLockedError(UpgradeError):
    """Windows can't replace the running launcher (`coop-data-doc.exe`) in
    place — a running executable is locked by the OS. Not really a failure:
    the message tells the user the one command to run from a fresh terminal.
    The CLI prints it plainly rather than as an error.
    """


@dataclass
class DependencyStatus:
    name: str
    installed: str
    latest: str | None  # None = lookup failed
    kind: str  # "current" | "safe" | "major" | "unknown"


@dataclass
class UpgradePlan:
    install_method: str  # "pipx" | "uv-tool" | "git-checkout" | "pip"
    checkout: Path | None
    tool_installed: str
    tool_note: str
    dependencies: list[DependencyStatus] = field(default_factory=list)
    pip_spec: str | None = None  # for "pip": the URL/VCS spec to reinstall from

    @property
    def safe_updates(self) -> list[DependencyStatus]:
        return [d for d in self.dependencies if d.kind == "safe"]

    @property
    def major_updates(self) -> list[DependencyStatus]:
        return [d for d in self.dependencies if d.kind == "major"]

    @property
    def is_vcs_install(self) -> bool:
        """Installed from a VCS spec (``git+…``). Such an install has no PyPI
        version to compare against — its source is a moving branch — so an
        upgrade should always re-pull rather than be skipped as 'up to date'.
        """
        return is_vcs_spec(self.pip_spec)


# -- pure helpers -------------------------------------------------------------


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:4])


def _major(version: str) -> int | None:
    match = re.match(r"\s*(\d+)", version)
    return int(match.group(1)) if match else None


def classify_update(installed: str, latest: str | None) -> str:
    """'current' | 'safe' (newer, same major) | 'major' | 'unknown'."""
    if latest is None:
        return "unknown"
    installed_major, latest_major = _major(installed), _major(latest)
    if installed_major is None or latest_major is None:
        return "unknown"
    if latest_major > installed_major:
        return "major"
    if latest_major == installed_major and _version_tuple(latest) > _version_tuple(installed):
        return "safe"
    return "current"


def direct_dependencies() -> list[str]:
    """Names of this package's direct runtime dependencies (extras excluded)."""
    try:
        requirements = metadata.requires(PACKAGE_NAME) or []
    except metadata.PackageNotFoundError:
        return []
    names: set[str] = set()
    for requirement in requirements:
        if ";" in requirement and "extra" in requirement.split(";", 1)[1]:
            continue
        match = re.match(r"[A-Za-z0-9._-]+", requirement.strip())
        if match:
            names.add(match.group(0))
    return sorted(names, key=str.lower)


def pip_install_origin() -> str | None:
    """The spec to reinstall from when the install came from a URL/VCS.

    pip records the original source in PEP 610 ``direct_url.json``. A bare
    ``pip install -U coop-data-doc`` resolves against PyPI and would ignore the
    git/URL the tool was actually installed from (switching to the PyPI release,
    or no-op'ing a same-version git branch), so for a URL/VCS install we must
    reinstall from the recorded source instead. Returns e.g.
    ``git+https://github.com/.../coop-data-doc.git``, or None for a normal
    PyPI install (or when the metadata is unavailable).
    """
    try:
        raw = metadata.distribution(PACKAGE_NAME).read_text("direct_url.json")
    except (metadata.PackageNotFoundError, FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    url = info.get("url")
    if not url:
        return None
    if "vcs_info" in info:
        vcs = info["vcs_info"]
        ref = vcs.get("requested_revision")
        spec = f"{vcs.get('vcs', 'git')}+{url}"
        return f"{spec}@{ref}" if ref else spec  # keep the pinned branch/ref
    if info.get("dir_info", {}).get("editable"):
        # editable install (`pip install -e`) — preserve it as editable
        return f"-e {url[len('file://') :] if url.startswith('file://') else url}"
    return url  # local directory or a direct archive URL


def detect_install_method() -> tuple[str, Path | None]:
    """How this interpreter's copy of the tool was installed.

    Returns (method, checkout_path); checkout_path is set only for
    "git-checkout" — a venv living inside a clone of this project.
    """
    prefix = Path(sys.prefix).resolve()
    as_posix = prefix.as_posix()
    if "/pipx/venvs/" in as_posix:
        return "pipx", None
    if "/uv/tools/" in as_posix:
        return "uv-tool", None
    for candidate in [prefix, *prefix.parents]:
        pyproject = candidate / "pyproject.toml"
        try:
            if pyproject.is_file() and 'name = "coop-data-doc"' in pyproject.read_text(
                encoding="utf-8", errors="replace"
            ):
                if (candidate / ".git").exists():
                    return "git-checkout", candidate
                return "pip", None
        except OSError:
            continue
    return "pip", None


# -- side-effecting collaborators (injectable for tests) ----------------------


def fetch_latest_version(name: str) -> str | None:
    """Latest release on PyPI, or None when unknown (404 / no network)."""
    try:
        with urllib.request.urlopen(
            PYPI_JSON_URL.format(name=name), timeout=NETWORK_TIMEOUT_SECONDS
        ) as response:
            return json.load(response)["info"]["version"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def _run(command: list[str], runner=subprocess.run) -> subprocess.CompletedProcess:
    completed = runner(command, capture_output=True, text=True)
    if completed.returncode != 0:
        tail = "\n".join((completed.stderr or "").splitlines()[-5:])
        error = UpgradeError(f"`{' '.join(command)}` failed:\n{tail}")
        # The printable message keeps only the last few lines, but the lock
        # marker can sit far above pip's rollback/cleanup trailer — stash the
        # FULL stderr so the reactive lock check below can still find it.
        error.stderr = completed.stderr or ""
        raise error
    return completed


# -- Windows running-launcher lock --------------------------------------------
#
# A process started through a console-script `.exe` (`coop-data-doc.exe`) holds
# an OS lock on that file for its whole lifetime, and Windows refuses to rename
# or delete a running executable. So when the tool is launched via its own
# launcher it cannot replace that launcher in place: pipx/uv/pip all fail at the
# final re-link step with "WinError 32: ... being used by another process". POSIX
# has no such restriction (a running file can be unlinked), so this is Windows
# only. The cure is to run the upgrade from a fresh shell where the tool isn't
# running — exactly what these helpers detect and tell the user to do.
_WINDOWS_LOCK_HINTS = ("WinError 32", "being used by another process")


def running_launcher_path() -> Path | None:
    """This tool's own ``.exe`` launcher when we were started through it on
    Windows (and therefore can't replace it in place), else ``None``.

    Detected from ``sys.argv[0]``: a pip/distlib console-script wrapper sets it
    to the full path of the ``.exe``. Running via ``python -m coop_data_doc``
    points ``argv[0]`` at a ``.py`` file instead, so self-replacement is fine.
    """
    if sys.platform != "win32":
        return None
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    path = Path(argv0)
    if path.suffix.lower() == ".exe" and path.stem.lower().startswith(PACKAGE_NAME):
        return path
    return None


def needs_fresh_shell(plan: UpgradePlan) -> Path | None:
    """The launcher path when ``plan`` cannot be applied in place because we are
    the running Windows launcher, else ``None``.

    Scoped to pipx/uv-tool: those are wholesale managed reinstalls that ALWAYS
    re-link the launcher, so the lock is certain. pip/git-checkout may do useful
    partial work (a dependency bump, a ``git pull``) and only the final re-link
    might fail, so they are attempted and handled reactively instead.
    """
    launcher = running_launcher_path()
    if launcher is not None and plan.install_method in ("pipx", "uv-tool"):
        return launcher
    return None


def _looks_like_locked_launcher(stderr: str, *, via_launcher: bool) -> bool:
    """True when ``stderr`` is the Windows running-launcher lock for OUR tool.

    Requires the sharing-violation marker AND attribution to this package: we
    were started through the ``.exe`` launcher, or the OS error names the
    package itself. Without that, an unrelated locked file (a dependency DLL, an
    antivirus-held temp wheel) carrying the generic "being used by another
    process" text would be mislabelled — and the "run from a fresh shell" advice
    wouldn't help. Honesty rule: only claim the launcher lock when it's ours.
    """
    text = stderr or ""
    if not any(hint in text for hint in _WINDOWS_LOCK_HINTS):
        return False
    return via_launcher or PACKAGE_NAME in text


def manual_upgrade_command(plan: UpgradePlan) -> str:
    """The exact command(s) to run from a fresh terminal to finish the upgrade.

    Mirrors what :func:`apply_plan` would have run, but reads back as something
    a human can copy-paste (``python`` rather than the absolute interpreter
    path, no ``-q``). Multi-step methods return newline-separated commands —
    NOT ``&&``-chained, which Windows PowerShell 5.1 (the in-box shell, and the
    one this Windows-only guidance is shown in) can't parse.
    """
    if plan.install_method == "pipx":
        verb = "reinstall" if plan.is_vcs_install else "upgrade"
        return f"pipx {verb} {PACKAGE_NAME}"
    if plan.install_method == "uv-tool":
        if plan.is_vcs_install:
            return f"uv tool install --force {plan.pip_spec}"
        return f"uv tool upgrade {PACKAGE_NAME}"
    if plan.install_method == "git-checkout" and plan.checkout is not None:
        return f'git -C "{plan.checkout}" pull --ff-only\npython -m pip install -U "{plan.checkout}"'
    if plan.pip_spec:
        if plan.pip_spec.startswith("-e "):
            return f'python -m pip install -U --force-reinstall -e "{plan.pip_spec[3:]}"'
        return f'python -m pip install -U --force-reinstall "{plan.pip_spec}"'
    return f"python -m pip install -U {PACKAGE_NAME}"


def manual_upgrade_message(plan: UpgradePlan) -> str:
    """User-facing guidance for the running-launcher lock — the why and the
    command(s) to run from a fresh terminal."""
    # Indent EVERY command line so multi-step guidance (git-checkout) stays
    # readable and copy-pasteable.
    block = "\n".join(f"    {line}" for line in manual_upgrade_command(plan).splitlines())
    return (
        f"Windows can't replace {PACKAGE_NAME}'s launcher while the tool is "
        "running, so the upgrade can't finish in place.\n\n"
        "Run this in a NEW terminal (where the tool isn't running):\n\n"
        f"{block}\n\n"
        f"Then confirm with:  {PACKAGE_NAME} --version"
    )


# -- planning & applying -------------------------------------------------------


def _git_checkout_note(checkout: Path, runner=subprocess.run) -> str:
    has_upstream = (
        runner(
            ["git", "-C", str(checkout), "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    if not has_upstream:
        return (
            "running from a git checkout with no upstream remote — upgrading "
            "reinstalls from the local working tree"
        )
    fetched = runner(["git", "-C", str(checkout), "fetch", "--quiet"], capture_output=True, text=True)
    if fetched.returncode != 0:
        return "running from a git checkout; `git fetch` failed (offline?)"
    behind = runner(
        ["git", "-C", str(checkout), "rev-list", "--count", "HEAD..@{upstream}"],
        capture_output=True,
        text=True,
    )
    count = (behind.stdout or "").strip()
    if behind.returncode == 0 and count.isdigit() and int(count) > 0:
        return f"{count} new commit(s) available on the upstream branch"
    return "checkout is up to date with its upstream"


def build_plan(
    fetch=fetch_latest_version,
    runner=subprocess.run,
    installed_version_of=metadata.version,
    origin=pip_install_origin,
) -> UpgradePlan:
    method, checkout = detect_install_method()
    # pip records the install source (PEP 610 direct_url.json) for ANY install
    # method, including pipx/uv-tool venvs — so read it for all three. Only a
    # git-checkout uses the working tree instead of a recorded spec.
    pip_spec = origin() if method in ("pip", "pipx", "uv-tool") else None

    if method == "git-checkout" and checkout is not None:
        tool_note = _git_checkout_note(checkout, runner)
    elif is_vcs_spec(pip_spec):
        tool_note = f"installed from {pip_spec}; upgrading re-pulls the latest commit"
    else:
        latest = fetch(PACKAGE_NAME)
        if latest is None:
            tool_note = "could not determine the latest release (offline, or PyPI unreachable)"
        else:
            kind = classify_update(__version__, latest)
            tool_note = (
                f"latest release is {latest}"
                if kind != "current"
                else f"already on the latest release ({latest})"
            )

    plan = UpgradePlan(
        install_method=method,
        checkout=checkout,
        tool_installed=__version__,
        tool_note=tool_note,
        pip_spec=pip_spec,
    )

    for name in direct_dependencies():
        try:
            installed = installed_version_of(name)
        except metadata.PackageNotFoundError:
            continue
        latest = fetch(name)
        plan.dependencies.append(
            DependencyStatus(
                name=name,
                installed=installed,
                latest=latest,
                kind=classify_update(installed, latest),
            )
        )
    return plan


def _tool_upgrade_command(plan: UpgradePlan) -> list[str]:
    """The argv that upgrades the tool itself for this install method."""
    if plan.install_method == "pipx":
        # `pipx upgrade` compares versions and won't re-pull a moving git branch.
        # Use `pipx reinstall` (NOT `pipx install --force`): it re-pulls from the
        # recorded spec, preserves the pinned @ref, and — crucially — works under
        # pipx's uv backend, which `install --force` fails against ("venv already
        # exists / use --clear"). uv is auto-selected whenever it's on PATH.
        return (
            ["pipx", "reinstall", PACKAGE_NAME] if plan.is_vcs_install else ["pipx", "upgrade", PACKAGE_NAME]
        )
    if plan.install_method == "uv-tool":
        # `uv tool install --force <url>` does re-fetch the remote and re-pull the
        # latest commit on a moving branch (verified), so it's correct here.
        if plan.is_vcs_install:
            return ["uv", "tool", "install", "--force", plan.pip_spec]
        return ["uv", "tool", "upgrade", PACKAGE_NAME]
    if plan.install_method == "git-checkout" and plan.checkout is not None:
        return [sys.executable, "-m", "pip", "install", "-q", "-U", str(plan.checkout)]
    if plan.pip_spec:
        # installed from a git/URL via plain pip: reinstall from that exact
        # source. --force-reinstall guarantees a moving branch is re-pulled
        # even when the version string is unchanged. An editable install
        # arrives as "-e <path>" — split it into separate argv tokens.
        spec_tokens = ["-e", plan.pip_spec[3:]] if plan.pip_spec.startswith("-e ") else [plan.pip_spec]
        return [sys.executable, "-m", "pip", "install", "-q", "-U", "--force-reinstall", *spec_tokens]
    return [sys.executable, "-m", "pip", "install", "-q", "-U", PACKAGE_NAME]


def apply_plan(plan: UpgradePlan, runner=subprocess.run) -> list[list[str]]:
    """Run the upgrade: the tool first, then non-breaking dependency bumps.

    Returns the commands executed (useful for reporting and tests).
    Major dependency upgrades are never applied automatically — they are
    reported so a human can review release notes first.
    """
    # We can't replace our own launcher while it's running on Windows; bail out
    # with copy-paste guidance instead of churning through a doomed reinstall.
    if needs_fresh_shell(plan) is not None:
        raise LauncherLockedError(manual_upgrade_message(plan))

    executed: list[list[str]] = []

    if (
        plan.install_method == "git-checkout"
        and plan.checkout is not None
        and "new commit(s)" in plan.tool_note
    ):
        pull = ["git", "-C", str(plan.checkout), "pull", "--ff-only"]
        _run(pull, runner)
        executed.append(pull)

    command = _tool_upgrade_command(plan)
    try:
        _run(command, runner)
    except UpgradeError as exc:
        # pip/git-checkout aren't caught proactively (they may do useful partial
        # work). If the re-link still hit the Windows lock, swap the raw pipx/pip
        # traceback for the actionable "run it from a fresh shell" guidance. Scan
        # the FULL stderr (the marker may sit above pip's rollback trailer, which
        # the 5-line message tail drops), and only when the lock is attributable
        # to our own launcher (see _looks_like_locked_launcher).
        full_stderr = getattr(exc, "stderr", "") or str(exc)
        if _looks_like_locked_launcher(full_stderr, via_launcher=running_launcher_path() is not None):
            raise LauncherLockedError(manual_upgrade_message(plan)) from exc
        raise
    executed.append(command)

    # pipx/uv-tool manage their own venv's dependencies on upgrade; only
    # plain-pip/checkout installs need explicit non-breaking dep bumps
    if plan.install_method in ("pip", "git-checkout") and plan.safe_updates:
        specs = [f"{dep.name}<{(_major(dep.latest) or 0) + 1}" for dep in plan.safe_updates]
        dep_command = [sys.executable, "-m", "pip", "install", "-q", "-U", *specs]
        _run(dep_command, runner)
        executed.append(dep_command)

    return executed
