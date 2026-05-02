"""Portable Git installer.

Downloads the official PortableGit self-extracting archive from
github.com/git-for-windows/git and unpacks it into
`<project_root>/portable/git/`. Lets the manager run on a Windows
machine that doesn't have Git for Windows installed system-wide --
just like the SteamCMD bootstrap path.

Called by `manager.install._run()` as part of the bootstrap step, so
the operator doesn't see a separate "install Git" button -- it's
folded into the same Setup flow that downloads SteamCMD and Soulmask.

PortableGit is the official "no installer, no admin" distribution from
the Git for Windows project. It includes git.exe AND a bundled
OpenSSH (ssh.exe, ssh-keygen.exe) so the deploy-key flow works without
depending on Windows OpenSSH being available either.

Footprint: ~50MB download, ~300MB extracted.

The synchronous entry point (`install_portable_git_sync`) does the
work; the dataclass-based state tracker is preserved so callers (the
SteamCMD installer, future stand-alone refresh) get the same
status/progress hooks the existing install.py uses.
"""

import logging
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from manager.config import PROJECT_ROOT, display_path

log = logging.getLogger(__name__)

# Where PortableGit lands. Gitignored via the `portable/` rule.
PORTABLE_DIR = PROJECT_ROOT / "portable"
GIT_DIR = PORTABLE_DIR / "git"

# GitHub release lookup: hits the public API, no auth needed. Rate
# limited at 60 requests/hour per IP for unauthenticated calls; we hit
# this once per machine so that's fine.
_RELEASES_API = "https://api.github.com/repos/git-for-windows/git/releases/latest"
_DOWNLOAD_TIMEOUT_SEC = 600   # 50MB on a slow connection
_EXTRACT_TIMEOUT_SEC = 300

# Status values mirror the SteamCMD install pattern.
STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


@dataclass
class _State:
    status: str = STATUS_IDLE
    step: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: str = ""
    output_lines: deque = field(default_factory=lambda: deque(maxlen=500))

    def snapshot(self) -> dict:
        end = self.finished_at or datetime.now()
        return {
            "status": self.status,
            "step": self.step,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S")
                          if self.started_at else "",
            "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M:%S")
                           if self.finished_at else "",
            "elapsed_seconds": int((end - self.started_at).total_seconds())
                               if self.started_at else 0,
            "error": self.error,
            "lines": list(self.output_lines),
        }


_state = _State()
_lock = threading.Lock()


# ── Public surface ──────────────────────────────────────────────────────────


def is_installed() -> bool:
    """True if a usable git.exe is present in the portable directory."""
    return any((GIT_DIR / sub / "git.exe").is_file()
               for sub in ("bin", "cmd", "mingw64/bin"))


def get_state() -> dict:
    with _lock:
        return _state.snapshot()


def is_running() -> bool:
    with _lock:
        return _state.status == STATUS_RUNNING


def start() -> bool:
    """Kick off the install in a daemon thread. False if already running."""
    if sys.platform != "win32":
        log.error("install_git: portable Git installer only supports Windows")
        return False
    with _lock:
        if _state.status == STATUS_RUNNING:
            log.warning("install_git: already running, ignoring re-entry")
            return False
        _state.status = STATUS_RUNNING
        _state.step = "Preparing"
        _state.started_at = datetime.now()
        _state.finished_at = None
        _state.error = ""
        _state.output_lines.clear()
    log.info("install_git: kicking off background portable-Git install")
    threading.Thread(target=_run, daemon=True,
                     name="install-portable-git").start()
    return True


def install_portable_git_sync() -> None:
    """Synchronous installer used by the bootstrap (manager.install._run)
    so PortableGit lands as part of the same Setup flow that downloads
    SteamCMD. Raises on failure.

    Skips the install if PortableGit is already extracted -- idempotent
    so re-running setup is safe.
    """
    if sys.platform != "win32":
        raise RuntimeError(
            "Portable Git installer only supports Windows. On other "
            "platforms install git via the system package manager."
        )
    if is_installed():
        _add_line(f"PortableGit already present under {GIT_DIR}",
                  level=logging.INFO)
        return
    with _lock:
        # Mark RUNNING so any concurrent /manager-updates page render
        # sees consistent state. The caller is on the install thread,
        # not the request thread, so we don't need a separate worker.
        _state.status = STATUS_RUNNING
        _state.step = "Installing portable Git"
        _state.started_at = datetime.now()
        _state.finished_at = None
        _state.error = ""
        _state.output_lines.clear()
    try:
        _do_install_steps()
        _set_status(STATUS_COMPLETED)
    except Exception as e:
        _add_line(f"FATAL: {type(e).__name__}: {e}", level=logging.ERROR)
        _set_status(STATUS_FAILED, error=f"{type(e).__name__}: {e}")
        raise


# ── Internal logging helpers ────────────────────────────────────────────────


def _add_line(line: str, level: int = logging.DEBUG) -> None:
    line = line.rstrip()
    if not line:
        return
    with _lock:
        _state.output_lines.append((datetime.now().strftime("%H:%M:%S"), line))
    log.log(level, "[install-git] %s", line)


def _set_step(step: str) -> None:
    with _lock:
        _state.step = step
    log.info("[install-git] STEP: %s", step)
    _add_line(f"--- {step} ---", level=logging.INFO)


def _set_status(status: str, error: str = "") -> None:
    with _lock:
        _state.status = status
        _state.finished_at = datetime.now()
        if error:
            _state.error = error
    log.info("[install-git] STATUS -> %s%s",
             status, f" ({error})" if error else "")


# ── Thread main ─────────────────────────────────────────────────────────────


def _run() -> None:
    """Background-thread entry point used by start()."""
    try:
        _do_install_steps()
        _set_status(STATUS_COMPLETED)
    except Exception as e:
        log.exception("install_git thread crashed")
        _add_line(f"FATAL: {type(e).__name__}: {e}", level=logging.ERROR)
        _set_status(STATUS_FAILED, error=f"{type(e).__name__}: {e}")


def _do_install_steps() -> None:
    """Body shared by the async (start/_run) and sync
    (install_portable_git_sync) entry points. Raises on failure."""
    # 1. Resolve the asset URL via the GitHub API.
    _set_step("Looking up latest PortableGit release")
    version, asset_url = _find_portable_git_asset()
    _add_line(f"Latest PortableGit version: {version}", level=logging.INFO)
    _add_line(f"Asset URL: {asset_url}")

    # 2. Download the SFX to a tmp file.
    _set_step("Downloading PortableGit (~50 MB)")
    PORTABLE_DIR.mkdir(parents=True, exist_ok=True)
    sfx_path = PORTABLE_DIR / "PortableGit.7z.exe"
    _download(asset_url, sfx_path)

    # 3. Extract via the SFX's silent flags.
    _set_step("Extracting PortableGit (~300 MB on disk; takes 30-60 sec)")
    if GIT_DIR.exists():
        _add_line(f"Removing existing {display_path(GIT_DIR)} for clean install",
                  level=logging.INFO)
        try:
            import shutil
            shutil.rmtree(GIT_DIR)
        except OSError as e:
            log.warning("[install-git] could not remove existing %s: %s",
                        GIT_DIR, e)
    GIT_DIR.mkdir(parents=True, exist_ok=True)
    _extract(sfx_path, GIT_DIR)

    # 4. Verify.
    _set_step("Verifying")
    if not is_installed():
        raise RuntimeError(
            f"PortableGit extracted but no git.exe found under "
            f"{display_path(GIT_DIR)}. Extraction may have failed silently."
        )
    _add_line(f"git.exe present under {display_path(GIT_DIR)}",
              level=logging.INFO)

    # 5. Clean up the SFX archive.
    try:
        sfx_path.unlink(missing_ok=True)
        _add_line(f"Removed installer SFX {sfx_path.name}",
                  level=logging.INFO)
    except OSError as e:
        log.warning("[install-git] could not remove SFX: %s", e)

    # 6. Bust the binary-resolver cache so the new git is picked up
    #    without requiring a manager restart.
    try:
        from manager import self_update
        self_update._BINARY_CACHE.clear()
        _add_line("Cleared binary-resolver cache.", level=logging.INFO)
    except Exception:
        log.exception("[install-git] could not clear resolver cache")

    _set_step("Done")


# ── Steps ───────────────────────────────────────────────────────────────────


def _find_portable_git_asset() -> tuple[str, str]:
    """Query the GitHub releases API for the latest tag and find the
    64-bit PortableGit SFX. Returns (version, download_url).

    Raises RuntimeError on API failure or missing asset.
    """
    log.info("[install-git] GET %s", _RELEASES_API)
    try:
        resp = requests.get(_RELEASES_API, timeout=30,
                            headers={"Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        raise RuntimeError(f"could not reach GitHub API: {e}")
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub releases API returned HTTP {resp.status_code}: "
            f"{resp.text[:200]}"
        )
    data = resp.json()
    tag = data.get("tag_name", "?")
    assets = data.get("assets", [])
    log.verbose("[install-git] release %s has %d assets", tag, len(assets))

    # Asset name is like "PortableGit-2.45.0-64-bit.7z.exe".
    for asset in assets:
        name = asset.get("name", "")
        if (name.startswith("PortableGit-") and
                name.endswith("-64-bit.7z.exe")):
            url = asset.get("browser_download_url")
            if url:
                return tag, url

    available = [a.get("name", "?") for a in assets]
    raise RuntimeError(
        f"Could not find PortableGit-*-64-bit.7z.exe among release "
        f"assets. Available: {available}"
    )


def _download(url: str, dest: Path) -> None:
    """Stream-download `url` to `dest` with progress logging."""
    log.info("[install-git] downloading %s -> %s", url, dest)
    try:
        resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT_SEC, stream=True)
    except requests.RequestException as e:
        raise RuntimeError(f"download failed: {e}")
    if resp.status_code != 200:
        raise RuntimeError(
            f"download returned HTTP {resp.status_code} for {url}"
        )

    total = int(resp.headers.get("content-length") or 0)
    _add_line(f"HTTP {resp.status_code}, content-length={total or '?'}",
              level=logging.INFO)

    bytes_got = 0
    last_log = time.monotonic()
    with dest.open("wb") as fout:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            fout.write(chunk)
            bytes_got += len(chunk)
            # Periodic progress, throttled to once every 2 sec.
            now = time.monotonic()
            if now - last_log >= 2.0:
                if total:
                    pct = 100.0 * bytes_got / total
                    _add_line(f"Downloaded {bytes_got:,}/{total:,} bytes "
                              f"({pct:.1f}%)", level=logging.INFO)
                else:
                    _add_line(f"Downloaded {bytes_got:,} bytes",
                              level=logging.INFO)
                last_log = now
    _add_line(f"Download complete: {bytes_got:,} bytes",
              level=logging.INFO)


def _extract(sfx_path: Path, dest_dir: Path) -> None:
    """Run the PortableGit SFX with silent-extract flags.

    The SFX is a 7zip self-extracting archive. `-y` answers yes to
    prompts; `-o<dir>` specifies the output directory (no space between
    flag and value -- standard 7zip convention).
    """
    cmd = [str(sfx_path), "-y", f"-o{dest_dir}"]
    _add_line(f"Run: {' '.join(cmd)}", level=logging.INFO)
    log.debug("[install-git] subprocess: %s", cmd)

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    try:
        proc = subprocess.run(
            cmd,
            timeout=_EXTRACT_TIMEOUT_SEC,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"PortableGit SFX did not finish extracting within "
            f"{_EXTRACT_TIMEOUT_SEC}s. Disk full or slow IO?"
        )
    except OSError as e:
        raise RuntimeError(f"could not run SFX: {e}")

    if proc.stdout:
        for line in proc.stdout.splitlines():
            _add_line(line)
    if proc.returncode != 0:
        if proc.stderr:
            _add_line(f"stderr: {proc.stderr.strip()}", level=logging.ERROR)
        raise RuntimeError(
            f"PortableGit SFX exited with rc={proc.returncode}. "
            f"Most common: not enough disk space (need ~300MB free)."
        )
    _add_line("Extraction complete.", level=logging.INFO)
