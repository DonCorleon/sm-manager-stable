"""Soulmask update detection.

Two operations:
  - read_local_buildid(): cheap, parses the .acf manifest on disk.
  - query_remote_buildid(): slow (~5-15 sec), shells out to SteamCMD.

A background poller runs query_remote_buildid every POLL_INTERVAL_SEC and
caches the result. Dashboard reads `get_state()` to render local vs remote
and an "update available" badge.

Don't poll faster than every ~5 min -- Valve will not appreciate it.
"""

import logging
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from manager.config import get_setting
from manager.paths import (
    SOULMASK_WINDOWS_APP_ID,
    appmanifest_path,
    steamcmd_exe,
)

log = logging.getLogger(__name__)

# Default poll interval if no setting exists. Operator can override via
# updates.poll_interval_min on the Settings page (restart required for
# the change -- the loop captures the value when the poller starts).
POLL_INTERVAL_DEFAULT_SEC = 600


def _poll_interval_sec() -> int:
    """Resolve from settings on each poller iteration -- new setting takes
    effect on the very next iteration without a manager restart."""
    return int(get_setting("updates.poll_interval_min", 10)) * 60


# ── State (read by get_state(), written by check_now() + the poller) ────────


_state_lock = threading.Lock()
_state: dict = {
    "local_buildid": None,       # str or None
    "remote_buildid": None,      # str or None
    "last_check_at": None,       # datetime or None
    "last_check_ok": False,
    "last_error": "",
}

_poller_started = False
_poller_lock = threading.Lock()


def get_state() -> dict:
    """Snapshot for dashboards. Always re-reads local buildid (cheap; the
    manifest may have just been updated by a successful install)."""
    local = read_local_buildid()
    with _state_lock:
        _state["local_buildid"] = local
        snap = dict(_state)
    snap["last_check_at_human"] = (
        snap["last_check_at"].strftime("%Y-%m-%d %H:%M:%S")
        if snap["last_check_at"] else ""
    )
    snap["update_available"] = bool(
        snap["local_buildid"]
        and snap["remote_buildid"]
        and snap["local_buildid"] != snap["remote_buildid"]
    )
    return snap


# ── Local manifest parse ────────────────────────────────────────────────────


def read_local_buildid() -> Optional[str]:
    """Parse the on-disk SteamCMD manifest for the installed buildid.
    Returns None if the manifest is missing (server not installed yet) or
    if parsing fails (file half-written during an install)."""
    manifest = appmanifest_path()
    if not manifest.exists():
        return None
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("read_local_buildid: failed to read %s: %s", manifest, e)
        return None
    # ACF format is a Valve KeyValues dump. The buildid line looks like:
    #   "buildid"   "12345678"
    m = re.search(r'"buildid"\s+"(\d+)"', text)
    return m.group(1) if m else None


# ── Remote query via SteamCMD ───────────────────────────────────────────────


def query_remote_buildid(stream: bool = False) -> Optional[str]:
    """Shell out to SteamCMD and parse the public branch buildid. None on
    any failure (SteamCMD missing, network down, parse miss).

    `stream=True` byte-streams SteamCMD's output line-by-line into
    manager.updates_log so the /updates SSE consumer sees progress as
    it happens (download bars, login flow, etc.). Defaults to False
    so the periodic background poller stays silent."""
    cmd_path = steamcmd_exe()
    if not cmd_path.exists():
        log.debug("query_remote_buildid: SteamCMD not present, skipping")
        return None

    cmd = [
        str(cmd_path),
        "+login", "anonymous",
        "+app_info_update", "1",     # force the cache to refresh
        "+app_info_print", str(SOULMASK_WINDOWS_APP_ID),
        "+quit",
    ]
    log.debug("query_remote_buildid: running %s", cmd)

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    started = time.monotonic()
    if stream:
        output, rc = _run_steamcmd_streaming(cmd, creationflags)
        if output is None:
            return None
    else:
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired:
            log.warning("query_remote_buildid: SteamCMD timed out after 120s")
            return None
        except OSError as e:
            log.warning("query_remote_buildid: SteamCMD launch failed: %s", e)
            return None
        output = result.stdout or ""
        rc = result.returncode

    elapsed = time.monotonic() - started
    log.debug("query_remote_buildid: SteamCMD finished in %.1fs (rc=%d, %d bytes)",
              elapsed, rc, len(output))

    # The output is a deeply-nested KeyValues block. Pull
    # branches -> public -> buildid.
    m = re.search(r'"public"\s*\{\s*"buildid"\s*"(\d+)"', output)
    if not m:
        log.warning("query_remote_buildid: did not find public/buildid in SteamCMD "
                    "output (rc=%d). First 500 chars: %r",
                    rc, output[:500])
        return None
    return m.group(1)


def _run_steamcmd_streaming(cmd: list, creationflags: int
                            ) -> tuple[Optional[str], int]:
    """Popen+read1 byte streaming for SteamCMD, mirroring self_update's
    streaming path. Returns (full_combined_output, returncode). On
    failure returns (None, -1) and writes an ERROR line to updates_log."""
    from manager import updates_log
    updates_log.append("steam", f"$ {' '.join(str(x) for x in cmd)}")
    try:
        # bufsize default (-1) gives a BufferedReader whose read1(n)
        # returns whatever bytes are immediately available; bufsize=0
        # returns raw FileIO with no read1 method.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError as e:
        log.warning("query_remote_buildid: SteamCMD launch failed: %s", e)
        updates_log.append("steam", f"ERROR: SteamCMD launch failed: {e}")
        return None, -1

    captured: list[str] = []
    line_buf = bytearray()
    deadline = time.monotonic() + 120
    timed_out = False
    try:
        assert proc.stdout is not None
        while True:
            if time.monotonic() > deadline:
                timed_out = True
                break
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            captured.append(chunk.decode("utf-8", errors="replace"))
            line_buf.extend(chunk)
            while True:
                nl = line_buf.find(b"\n")
                if nl < 0:
                    break
                line = line_buf[:nl].decode("utf-8", errors="replace")
                del line_buf[:nl + 1]
                updates_log.append("steam", line)
        if line_buf:
            updates_log.append("steam",
                               line_buf.decode("utf-8", errors="replace"))
    except Exception as e:
        log.exception("query_remote_buildid: read loop raised")
        updates_log.append("steam", f"ERROR: read loop: {e}")
        try:
            proc.kill()
        except Exception:
            pass
        return None, -1

    if timed_out:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        log.warning("query_remote_buildid: SteamCMD timed out after 120s")
        updates_log.append("steam", "ERROR: SteamCMD timed out after 120s")
        return None, -1

    rc = proc.wait()
    return "".join(captured), rc


def check_now(stream: bool = False) -> dict:
    """Run a single update check synchronously and update state. Returns the
    new state snapshot. Safe to call from any thread.

    `stream=True` streams SteamCMD output to updates_log -- pass it from
    the user-triggered button (background worker), NOT the periodic
    poller (the latter would clutter the page log every 10 minutes)."""
    log.info("Update check starting (querying Steam for remote buildid)...")
    remote = query_remote_buildid(stream=stream)
    with _state_lock:
        _state["last_check_at"] = datetime.now()
        if remote is None:
            _state["last_check_ok"] = False
            _state["last_error"] = ("SteamCMD did not return a buildid -- check "
                                    "manager.log for parse / network details")
            log.warning("Update check FAILED")
        else:
            _state["last_check_ok"] = True
            _state["last_error"] = ""
            old = _state.get("remote_buildid")
            _state["remote_buildid"] = remote
            if old != remote:
                log.info("Update check OK: remote build = %s (was %s)", remote, old)
            else:
                log.info("Update check OK: remote build unchanged at %s", remote)
    return get_state()


# ── Background poller ───────────────────────────────────────────────────────


def start_poller() -> None:
    """Start the background poller exactly once per process. Safe to call
    repeatedly (idempotent). Runs check_now() at process start, then every
    POLL_INTERVAL_SEC after."""
    global _poller_started
    with _poller_lock:
        if _poller_started:
            return
        _poller_started = True

    log.info("Starting update poller: interval %ds", _poll_interval_sec())
    threading.Thread(target=_poller_loop, daemon=True,
                     name="update-poller").start()


# ── Mutual exclusion ───────────────────────────────────────────────────────
# `_steamcmd_lock` serialises every SteamCMD invocation: this module's
# poller, the operator-clicked Check Steam, AND the big app_update run
# inside lifecycle.update_and_restart. Without it, two simultaneous
# steamcmd.exe processes can race on Steam's package cache + registry
# locks, which during a 5-15 minute app_update download is the
# scariest of the failure modes.
#
# Pollers / clicks try_acquire(blocking=False) and bail if held.
# lifecycle._run_app_update should acquire blocking with a generous
# timeout (see acquire_steamcmd_blocking) since the operator already
# committed to the update by clicking Update+Restart.

_steamcmd_lock = threading.Lock()


def try_acquire_steamcmd_lock() -> bool:
    """Non-blocking acquire of the SteamCMD lock. Returns True if the
    caller now holds the lock and MUST eventually call
    `release_steamcmd_lock()`. False if held by something else."""
    return _steamcmd_lock.acquire(blocking=False)


def acquire_steamcmd_blocking(timeout_sec: float = 60.0) -> bool:
    """Blocking acquire with timeout. Used by lifecycle._run_app_update
    where the operator has explicitly initiated Update+Restart and we
    should wait for the poller's current iteration to finish (typically
    <30s) rather than abort the operation. Returns True if acquired
    within the timeout, False otherwise."""
    return _steamcmd_lock.acquire(blocking=True, timeout=timeout_sec)


def release_steamcmd_lock() -> None:
    try:
        _steamcmd_lock.release()
    except Exception:
        log.exception("release_steamcmd_lock raised (non-fatal)")


def _poller_loop() -> None:
    # Initial check fires after a short delay so we don't slow startup.
    time.sleep(15)
    while True:
        # Skip silently on a fresh install before SteamCMD is laid down --
        # the wizard installs it during /setup/. Without this gate the
        # poller would set last_error to "SteamCMD did not return a
        # buildid" on every cycle and clutter /updates with red banners
        # before the operator has even configured anything.
        from manager.paths import steamcmd_exe
        if not steamcmd_exe().exists():
            log.debug("Steam poller: SteamCMD not installed yet -- "
                      "skipping iteration silently")
            time.sleep(_poll_interval_sec())
            continue
        # Skip iteration if any other SteamCMD op is in progress
        # (operator-clicked Check Steam, or the big app_update inside
        # Update+Restart). Don't queue, just bail and try next interval.
        if not _steamcmd_lock.acquire(blocking=False):
            log.debug("Steam poller: skipping iteration -- "
                      "_steamcmd_lock held (operator op in flight)")
        else:
            try:
                check_now()
            except Exception:
                log.exception("Update poller iteration crashed")
            finally:
                release_steamcmd_lock()
        time.sleep(_poll_interval_sec())


# ── Async kick-off (user-triggered button on /updates page) ────────────────


def start_check_steam() -> bool:
    """Run check_now(stream=True) on the background-worker thread. The
    operator watches SteamCMD's output land live in the /updates SSE
    log instead of staring at a blocked request thread for ~30-90 sec.

    Returns False if either another update op is already in progress
    (manual click) OR the steam poller is mid-check."""
    from manager import background, updates_log

    if not try_acquire_steamcmd_lock():
        log.info("start_check_steam: refused -- _steamcmd_lock held "
                 "(poller mid-check or another op in flight)")
        return False

    if not updates_log.begin_op("Check Steam for game updates", "steam"):
        # Defensive: lock was acquired but updates_log refuses. Release
        # so the next caller can succeed.
        release_steamcmd_lock()
        return False

    def _work() -> tuple[bool, str]:
        state = check_now(stream=True)
        return (bool(state.get("last_check_ok")),
                state.get("last_error", ""))

    def _runner():
        try:
            try:
                ok, msg = _work()
            except Exception as e:
                log.exception("Steam check raised")
                updates_log.end_op(False, f"{type(e).__name__}: {e}")
                return
            updates_log.end_op(ok, "" if ok else msg)
        finally:
            release_steamcmd_lock()

    background.submit("steam-check-now", _runner)
    return True
