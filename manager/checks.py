"""Bootstrap checks: what's present, what's missing.

Every check returns a CheckResult. The /setup/ page renders them as a list.
The setup wizard uses the `fixable` flag to decide which steps to offer.
"""

import logging
import platform
import sys
from dataclasses import dataclass
from typing import Optional

from manager.config import PROJECT_ROOT, display_path
from manager.paths import appmanifest_path, install_dir, server_exe, steamcmd_exe

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    fixable: bool = False  # True if the wizard can auto-resolve this


# Track previous check outcomes so we only log INFO when something
# CHANGES. run_all_checks fires on every dashboard render -- one
# INFO line per check per render is firehose noise.
_prev_outcomes: dict[str, bool] = {}


def _record(result: CheckResult) -> CheckResult:
    """Per-check log. DEBUG always; promote to INFO ONLY when the
    pass/fail flips since the last call. The summary line in
    run_all_checks() also only INFO-logs when anything changed."""
    status = "PASS" if result.passed else "FAIL"
    log.debug("Check [%s] %s: %s", status, result.name, result.detail)
    prev = _prev_outcomes.get(result.name)
    if prev is None:
        # First time we've seen this check -- log at INFO so the
        # operator gets a baseline on first manager boot.
        log.info("Check [%s] %s: %s", status, result.name, result.detail)
    elif prev != result.passed:
        log.info("Check [%s] %s: %s (was %s)",
                 status, result.name, result.detail,
                 "PASS" if prev else "FAIL")
    _prev_outcomes[result.name] = result.passed
    return result


def check_python_version() -> CheckResult:
    v = sys.version_info
    ok = v >= (3, 11)
    return _record(CheckResult(
        name="Python 3.11+",
        passed=ok,
        detail=f"Running Python {v.major}.{v.minor}.{v.micro} at {sys.executable}",
        fixable=False,
    ))


def check_platform() -> CheckResult:
    is_windows = platform.system() == "Windows"
    return _record(CheckResult(
        name="Windows OS",
        passed=is_windows,
        detail=f"{platform.system()} {platform.release()} (build {platform.version()})",
        fixable=False,
    ))


def check_install_location() -> CheckResult:
    """Refuse to bless a setup that would dump the dedicated-server
    files at a drive root (`C:\\`, `D:\\`).

    The default `install_dir = ".."` (relative to the manager's
    project root). If the operator cloned the manager into e.g.
    `C:\\sm-manager-stable\\` directly, install_dir resolves to
    `C:\\` and SteamCMD would write WSServer.exe, ~10 GB of game
    data, and the WS\\ tree to the C: root -- polluting the system
    drive and almost certainly NOT what the operator intended.

    Detection: `install_dir.parts` has length 1 on Windows when
    the path is a drive root (`('C:\\\\',)`) and on POSIX when
    it's literal `/` (`('/',)`). Anything deeper is fine.

    Not auto-fixable -- the operator has to move the manager
    directory themselves. The detail explains what to do.
    """
    inst = install_dir()
    parts = inst.parts
    at_root = len(parts) <= 1
    return _record(CheckResult(
        name="Install location",
        passed=not at_root,
        detail=(
            f"Install dir resolves to {display_path(inst)} -- a drive "
            f"root. SteamCMD would dump the Soulmask server "
            f"(~5--15 GB) directly here. Move the manager folder "
            f"INTO the directory where you want Soulmask installed "
            f"(e.g. D:\\Soulmask\\sm-manager) and restart -- the "
            f"manager defaults to '..' for install_dir, so the "
            f"parent of the manager folder is where the server lands."
            if at_root else
            f"Install dir = {display_path(inst)} "
            f"(manager root: {display_path(PROJECT_ROOT)})"
        ),
        fixable=False,
    ))


def check_steamcmd() -> CheckResult:
    p = steamcmd_exe()
    log.debug("Probing SteamCMD path: %s", p)
    found = p.exists()
    log.debug("  exists=%s", found)
    return _record(CheckResult(
        name="SteamCMD",
        passed=found,
        detail=f"{'Found at' if found else 'Missing -- expected at'} {display_path(p)}",
        fixable=True,
    ))


def check_git() -> CheckResult:
    """Git is required for the manager's self-update flow. The setup
    wizard installs PortableGit when no system git is found."""
    # Local import to avoid pulling self_update at module import time.
    from manager import install_git, self_update
    git = self_update._resolve_binary("git")
    if git:
        # Tell the operator WHICH git -- system install vs portable
        # tells them whether updates need a fresh download.
        portable = install_git.GIT_DIR.as_posix() in git.replace("\\", "/")
        kind = "PortableGit" if portable else "system git"
        return _record(CheckResult(
            name="Git",
            passed=True,
            detail=f"Found ({kind}) at {git}",
            fixable=False,
        ))
    return _record(CheckResult(
        name="Git",
        passed=False,
        detail="git.exe not on PATH or in known install locations. "
               "Setup wizard installs PortableGit.",
        fixable=True,
    ))


def check_install() -> CheckResult:
    exe = server_exe()
    manifest = appmanifest_path()
    log.debug("Probing install: WSServer.exe=%s, appmanifest=%s", exe, manifest)
    exe_exists = exe.exists()
    man_exists = manifest.exists()
    log.debug("  WSServer.exe exists=%s; appmanifest exists=%s", exe_exists, man_exists)
    if exe_exists and man_exists:
        return _record(CheckResult(
            name="Soulmask install",
            passed=True,
            detail=f"Found WSServer.exe and appmanifest under {display_path(exe.parent)}",
            fixable=False,
        ))
    missing_parts = []
    if not exe_exists:
        missing_parts.append(f"WSServer.exe at {display_path(exe)}")
    if not man_exists:
        missing_parts.append(f"appmanifest at {display_path(manifest)}")
    return _record(CheckResult(
        name="Soulmask install",
        passed=False,
        detail="Missing: " + "; ".join(missing_parts),
        fixable=True,
    ))


_prev_summary: Optional[tuple[int, int]] = None


def run_all_checks() -> list[CheckResult]:
    """Run every check. Logs at INFO only when results CHANGE since
    last call -- avoids per-render firehose. VERBOSE always logs a
    one-line summary."""
    global _prev_summary
    log.verbose("Running bootstrap checks...")
    results = [
        check_python_version(),
        check_platform(),
        check_install_location(),
        check_steamcmd(),
        check_git(),
        check_install(),
    ]
    passed = sum(c.passed for c in results)
    failed = len(results) - passed
    summary = (passed, failed)
    log.verbose("Bootstrap summary: %d passed, %d failed", passed, failed)
    if _prev_summary != summary:
        if failed == 0:
            log.info("Bootstrap: all %d checks passed.", len(results))
        else:
            log.info("Bootstrap: %d passed, %d failed (wizard can install "
                     "fixable items).", passed, failed)
        _prev_summary = summary
    return results
