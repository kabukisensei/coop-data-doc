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


class UpgradeError(Exception):
    """A user-facing upgrade problem; the message is printable as-is."""


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
    ``pip install -U coop-data-doc`` would hit PyPI (where the package isn't
    published yet) and silently no-op, so for a git/url install we must
    reinstall from the recorded URL instead. Returns e.g.
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
        raise UpgradeError(f"`{' '.join(command)}` failed:\n{tail}")
    return completed


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
    pip_spec = origin() if method == "pip" else None

    if method == "git-checkout" and checkout is not None:
        tool_note = _git_checkout_note(checkout, runner)
    elif method == "pip" and pip_spec and "+" in pip_spec:
        tool_note = f"installed from {pip_spec}; upgrading re-pulls the latest commit"
    else:
        latest = fetch(PACKAGE_NAME)
        if latest is None:
            tool_note = "could not determine the latest release (not on PyPI yet, or offline)"
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


def apply_plan(plan: UpgradePlan, runner=subprocess.run) -> list[list[str]]:
    """Run the upgrade: the tool first, then non-breaking dependency bumps.

    Returns the commands executed (useful for reporting and tests).
    Major dependency upgrades are never applied automatically — they are
    reported so a human can review release notes first.
    """
    executed: list[list[str]] = []

    if plan.install_method == "pipx":
        command = ["pipx", "upgrade", PACKAGE_NAME]
    elif plan.install_method == "uv-tool":
        command = ["uv", "tool", "upgrade", PACKAGE_NAME]
    elif plan.install_method == "git-checkout" and plan.checkout is not None:
        if "new commit(s)" in plan.tool_note:
            pull = ["git", "-C", str(plan.checkout), "pull", "--ff-only"]
            _run(pull, runner)
            executed.append(pull)
        command = [sys.executable, "-m", "pip", "install", "-q", "-U", str(plan.checkout)]
    elif plan.pip_spec:
        # installed from a git/URL via plain pip: reinstall from that exact
        # source. --force-reinstall guarantees a moving branch is re-pulled
        # even when the version string is unchanged. An editable install
        # arrives as "-e <path>" — split it into separate argv tokens.
        spec_tokens = ["-e", plan.pip_spec[3:]] if plan.pip_spec.startswith("-e ") else [plan.pip_spec]
        command = [sys.executable, "-m", "pip", "install", "-q", "-U", "--force-reinstall", *spec_tokens]
    else:
        command = [sys.executable, "-m", "pip", "install", "-q", "-U", PACKAGE_NAME]
    _run(command, runner)
    executed.append(command)

    # pipx/uv-tool manage their own venv's dependencies on upgrade; only
    # plain-pip/checkout installs need explicit non-breaking dep bumps
    if plan.install_method in ("pip", "git-checkout") and plan.safe_updates:
        specs = [f"{dep.name}<{(_major(dep.latest) or 0) + 1}" for dep in plan.safe_updates]
        dep_command = [sys.executable, "-m", "pip", "install", "-q", "-U", *specs]
        _run(dep_command, runner)
        executed.append(dep_command)

    return executed
