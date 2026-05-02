"""Cluster-aware start/stop orchestration.

start_all(): main first -> wait for Steam query to respond -> child.
stop_all():  child first -> graceful EchoPort SaveAndExit -> wait for exit -> main.

Single source of truth for InstanceProcess instances (keyed by map name) so
status snapshots reflect the same processes the start/stop ops manage.
"""

import logging
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Optional

import psutil

from manager import echo, query, updates
from manager.config import get_setting, load_settings
from manager.paths import (
    SOULMASK_WINDOWS_APP_ID,
    install_dir,
    steamcmd_exe,
)
from manager.process import InstanceProcess
from manager.wizard import (
    active_runtime_instances,
    build_launch_args,
    load_existing,
)

log = logging.getLogger(__name__)

# Persistent map_name -> InstanceProcess across requests, so the status
# panel and the start/stop ops see the same processes.
_processes: dict[str, InstanceProcess] = {}
_processes_lock = threading.Lock()

# Serialise start/stop ops so two clicks in quick succession can't interleave.
_op_lock = threading.Lock()

# Op-progress tracking. Set inside _start_impl/_stop_impl while the lock is
# held; cleared in their finally blocks. get_status() reads these without
# locking -- on CPython, simple variable reads are atomic enough for status.
_current_op_name: Optional[str] = None
_current_op_started_at: Optional[float] = None  # time.monotonic() at op start

# Per-instance previous-alive state, used by get_status() to detect
# unexpected death (alive -> dead WITHOUT shutdown_requested_at being
# set). Logged ONCE per crash so the manager.log carries an audit trail
# of "the server died at 14:23 and we did not ask it to."
_prev_alive_state: dict[str, bool] = {}


def _begin_op(name: str) -> None:
    global _current_op_name, _current_op_started_at
    log.verbose("op-state: begin '%s' (prev=%r)", name, _current_op_name)
    _current_op_name = name
    _current_op_started_at = time.monotonic()
    _notify_dashboard()


def _end_op() -> None:
    global _current_op_name, _current_op_started_at
    log.verbose("op-state: end '%s'", _current_op_name)
    _current_op_name = None
    _current_op_started_at = None
    _notify_dashboard()


def _notify_dashboard() -> None:
    """Wake any open dashboard SSE subscribers so they swap immediately
    instead of waiting for the next refresh tick."""
    try:
        from manager import dashboard_events
        dashboard_events.notify()
    except Exception:
        log.exception("dashboard notify raised (non-fatal)")


def _op_warning_level(elapsed: int) -> str:
    """Return 'ok' / 'slow' / 'very_slow' based on elapsed seconds."""
    if elapsed >= _op_very_slow_sec():
        return "very_slow"
    if elapsed >= _op_slow_sec():
        return "slow"
    return "ok"

# Default in-game countdown for graceful stop. Live settings hook below.
_DEFAULT_COUNTDOWN_SEC_FALLBACK = 30
_MIN_COUNTDOWN_SEC = 1     # 0 means "300 sec default" to Soulmask
_MAX_COUNTDOWN_SEC = 3600  # 1 hour cap

_READY_POLL_SEC = 3


# ── Live settings accessors -- no manager restart required ─────────────────


def _default_countdown_sec() -> int:
    return int(get_setting("operations.default_stop_countdown_sec",
                           _DEFAULT_COUNTDOWN_SEC_FALLBACK))


def _op_slow_sec() -> int:
    return int(get_setting("operations.slow_threshold_min", 5)) * 60


def _op_very_slow_sec() -> int:
    return int(get_setting("operations.very_slow_threshold_min", 15)) * 60


def _op_hard_cap_sec() -> int:
    return int(get_setting("operations.hard_cap_min", 30)) * 60


def _ensure_processes() -> list[InstanceProcess]:
    """Build/refresh InstanceProcess entries to match current wizard config.
    On first sight of a map that has no in-memory entry, scan psutil for an
    already-running WSServer.exe matching the wizard's EchoPort and adopt
    it -- this lets the manager survive restarts while servers stay up.

    Returns processes in start order (main first for cluster, only entry
    for single).
    """
    config = load_existing(load_settings())
    runtime = active_runtime_instances(config)
    install_root = install_dir()

    procs: list[InstanceProcess] = []
    with _processes_lock:
        # PID dedup across this pass so two RuntimeInstances configured
        # with the same EchoPort can't both adopt the same wrapper.
        adopted_pids_this_pass: set[int] = set()
        # Add or refresh entries for active maps.
        for ri in runtime:
            existing = _processes.get(ri.instance.map_name)
            if existing is None:
                # First time we've seen this map this manager-run -- check
                # for an orphan to adopt before creating a fresh handle.
                adopted = _try_adopt(ri, install_root,
                                     already_adopted_pids=adopted_pids_this_pass)
                if adopted is not None:
                    _processes[ri.instance.map_name] = adopted
                    if adopted.pid is not None:
                        adopted_pids_this_pass.add(adopted.pid)
                else:
                    _processes[ri.instance.map_name] = InstanceProcess(ri, install_root)
            else:
                # Update the runtime-instance reference so e.g. role/serverid
                # match the latest wizard save.
                existing.runtime_instance = ri
            procs.append(_processes[ri.instance.map_name])
        # Drop entries for maps no longer active (e.g. switched cluster->single).
        active_maps = {ri.instance.map_name for ri in runtime}
        stale = [m for m in _processes if m not in active_maps]
        for m in stale:
            sp = _processes.pop(m)
            if sp.is_alive:
                log.warning("Stale process for inactive map %s is still alive (PID %d) -- "
                            "it will keep running until manually killed", m, sp.pid)
    return procs


def _try_adopt(runtime_instance, install_root,
               already_adopted_pids: Optional[set[int]] = None,
               ) -> Optional[InstanceProcess]:
    """Look for an already-running WSServer.exe whose command line contains
    the EchoPort number we expect for this map. If found, return an adopted
    InstanceProcess wrapping it. Otherwise None.

    Matching by EchoPort is reliable because:
      - The wizard guarantees unique EchoPorts per instance.
      - The launch args include `-EchoPort=NNNNN` exactly.
      - Even if the wizard config changed since the process was spawned,
        the running process's EchoPort is what counts -- we shouldn't adopt
        something that has a different config from what we'd manage.

    `already_adopted_pids` is the set of PIDs adopted earlier in the same
    `_ensure_processes` pass; we skip those so two instances accidentally
    sharing an EchoPort can't both glom onto the same wrapper.
    """
    expected = f"-EchoPort={runtime_instance.instance.echo_port}"
    log.verbose("adopt scan: looking for %s on map=%s",
                expected, runtime_instance.instance.map_name)
    for p in psutil.process_iter(["pid", "name"]):
        try:
            pname = p.info.get("name") or ""
            if pname.lower() != "wsserver.exe":
                continue
            if already_adopted_pids and p.pid in already_adopted_pids:
                log.verbose("adopt scan: skipping PID %d (already adopted "
                            "in this pass)", p.pid)
                continue
            cmdline = p.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.verbose("adopt scan: psutil access denied / pid gone: %s", e)
            continue
        if any(expected in arg for arg in cmdline):
            log.info("Adopting orphaned WSServer.exe (PID %d) for %s "
                     "(matched on %s)", p.pid, runtime_instance.instance.name, expected)
            return InstanceProcess.adopt(runtime_instance, install_root, p)
    log.verbose("adopt scan: no match for %s", expected)
    return None


def get_status() -> dict:
    """Snapshot for the dashboard. Always cheap; safe to call every poll."""
    config = load_existing(load_settings())
    procs = _ensure_processes()

    # Local import to avoid circulars; both modules need lifecycle.
    from manager import backups as _backups
    from manager import login_lock as _lock

    instances = []
    for proc in procs:
        # Detect alive -> dead transition WITHOUT a manager-requested
        # shutdown. This is the canonical signal of an unexpected
        # crash. Log once per transition (handled by storing the
        # current state in _prev_alive_state below).
        map_name = proc.runtime_instance.instance.map_name
        currently_alive = proc.is_alive
        was_alive = _prev_alive_state.get(map_name)
        if was_alive is True and not currently_alive:
            if proc.shutdown_requested_at is None and not proc.auto_restart_after_death:
                log.error(
                    "[%s] PROCESS DIED UNEXPECTEDLY -- the manager did not "
                    "request a shutdown and there's no auto-restart pending. "
                    "Likely causes: server crash, OOM-kill, manual taskkill. "
                    "Check WS.log for the last few lines before death.",
                    proc.runtime_instance.instance.name
                )
            else:
                log.info("[%s] process tree exited (manager-tracked).",
                         proc.runtime_instance.instance.name)
        _prev_alive_state[map_name] = currently_alive

        ri = proc.runtime_instance
        running = proc.is_alive
        stats = proc.get_stats() if running else None
        q = query.query_server("127.0.0.1", ri.instance.query_port) if running else None

        uptime = 0
        if running and proc.started_at:
            uptime = int(time.time() - proc.started_at.timestamp())

        # 'shutting down' fires from the moment we ACK SaveAndExit until the
        # tree exits. If the tree's already gone, we let .start() clear the
        # flag on next spawn -- but for display, we suppress it once running
        # is False (the badge is then "stopped").
        shutting_down = bool(
            running and proc.shutdown_requested_at is not None
        )
        # 'auto-restart pending' = instance died after a too-late cancel
        # and the lifecycle auto-restart phase is queued/running. The flag
        # is cleared when the start succeeds.
        auto_restart_pending = bool(
            (not running) and proc.auto_restart_after_death
        )

        short = _backups.instance_short(ri)
        instances.append({
            "name": ri.instance.name,
            "map_name": ri.instance.map_name,
            "short": short,
            "role": ri.role,
            "serverid": ri.serverid,
            "running": running,
            "shutting_down": shutting_down,
            "auto_restart_pending": auto_restart_pending,
            "adopted": proc.adopted,
            "pid": proc.pid,
            "started_at": proc.started_at.strftime("%Y-%m-%d %H:%M:%S") if proc.started_at else "",
            "uptime_seconds": uptime,
            "uptime_human": _format_duration(uptime) if uptime else "",
            "cpu_percent": (stats or {}).get("cpu_percent", 0.0),
            "memory_mb": (stats or {}).get("memory_mb", 0.0),
            "query": q,
            "game_port": ri.instance.game_port,
            "query_port": ri.instance.query_port,
            "echo_port": ri.instance.echo_port,
            "login_locked": _lock.effective_lock(short),
            "login_locked_per_instance": _lock.is_instance_locked(short),
        })

    op_in_progress = _op_lock.locked()
    op_name = _current_op_name if op_in_progress else None
    op_elapsed = (
        int(time.monotonic() - _current_op_started_at)
        if op_in_progress and _current_op_started_at else 0
    )

    return {
        "mode": config.mode,
        "instances": instances,
        "instance_count": len(instances),
        "running_count": sum(1 for i in instances if i["running"]),
        "any_running": any(i["running"] for i in instances),
        "all_running": bool(instances) and all(i["running"] for i in instances),
        "any_shutting_down": any(i["shutting_down"] for i in instances),
        "any_auto_restart_pending": any(i["auto_restart_pending"]
                                        for i in instances),
        "any_login_locked": any(i["login_locked"] for i in instances if i["running"]),
        "cluster_login_locked": _lock.is_cluster_locked(),
        "op_in_progress": op_in_progress,
        "op_name": op_name,                  # 'start' / 'stop' / None
        "op_elapsed_seconds": op_elapsed,
        "op_elapsed_human": _format_duration(op_elapsed) if op_elapsed else "",
        "op_warning_level": _op_warning_level(op_elapsed) if op_in_progress else "ok",
        "op_slow_threshold_human": _format_duration(_op_slow_sec()),
        "op_very_slow_threshold_human": _format_duration(_op_very_slow_sec()),
    }


def try_acquire_op_lock(label: str) -> bool:
    """Non-blocking acquire of the op lock + set the op banner. Used by
    callers OUTSIDE the start/stop/update helpers (restore, future
    chat-command engine, etc.). Returns False if another op is running.
    Caller MUST eventually call `release_op_lock()`.
    """
    if not _op_lock.acquire(blocking=False):
        return False
    _begin_op(label)
    return True


def release_op_lock() -> None:
    """Counterpart to try_acquire_op_lock. Idempotent: tolerates double-
    release for callers that wrap with try/finally and may release twice
    on weird paths.

    Releases the lock BEFORE clearing op-state so a concurrent
    get_status() read never sees op_in_progress=True with op_name=None
    (which renders as an empty op label on the dashboard)."""
    try:
        _op_lock.release()
    except RuntimeError:
        log.debug("release_op_lock: lock was already released (harmless)")
    _end_op()


def stop_single_instance(map_name: str,
                         countdown_sec: int = _MIN_COUNTDOWN_SEC,
                         wait_timeout_sec: Optional[int] = None) -> bool:
    """Graceful stop of one instance only (for restore). Sends SaveAndExit
    via EchoPort and waits for the entire process tree to exit. Returns
    True on clean exit, False on EchoPort failure or timeout. NEVER kills
    -- world data preservation rule applies.

    Caller is expected to hold the op lock (typical pattern: under a
    surrounding restore op). This function does NOT acquire it itself so
    it composes cleanly with multi-instance restore flows.
    """
    procs = _ensure_processes()
    target = next((p for p in procs
                   if p.runtime_instance.instance.map_name == map_name), None)
    if target is None:
        log.error("stop_single_instance: no process for map %s", map_name)
        return False
    if not target.is_alive:
        log.info("stop_single_instance: %s already not running", map_name)
        return True

    ri = target.runtime_instance
    cd = max(_MIN_COUNTDOWN_SEC, min(_MAX_COUNTDOWN_SEC, countdown_sec))
    if wait_timeout_sec is None:
        wait_timeout_sec = _op_hard_cap_sec()

    log.info("[%s] single-instance stop via EchoPort (countdown %ds)",
             ri.instance.name, cd)
    try:
        resp = echo.send_command("127.0.0.1", ri.instance.echo_port,
                                 f"SaveAndExit {cd}")
        cleaned = resp.replace("\r", " ").replace("\n", " ")[:200].strip()
        log.info("[%s] EchoPort response: %s", ri.instance.name,
                 cleaned or "(empty)")
        target.shutdown_requested_at = datetime.now()
    except (OSError, ConnectionError) as e:
        log.error("[%s] EchoPort unreachable: %s -- single-stop FAILED",
                  ri.instance.name, e)
        return False

    log.info("[%s] waiting for tree to exit (timeout %ds)...",
             ri.instance.name, wait_timeout_sec)
    started = time.monotonic()
    last_heartbeat = 0
    while time.monotonic() - started < wait_timeout_sec:
        if not target.is_alive:
            elapsed = int(time.monotonic() - started)
            log.info("[%s] tree exited after %s",
                     ri.instance.name, _format_duration(elapsed))
            return True
        elapsed = int(time.monotonic() - started)
        if elapsed - last_heartbeat >= 30 and elapsed > 0:
            last_heartbeat = elapsed
            level_fn, label = _slow_log_for(elapsed)
            level_fn("[%s] %s -- still waiting (%s elapsed)",
                     ri.instance.name, label, _format_duration(elapsed))
        time.sleep(1)
    log.error("[%s] did NOT exit within %ds -- single-stop FAILED. "
              "Process is left ALIVE on purpose. Investigate manually.",
              ri.instance.name, wait_timeout_sec)
    return False


def start_single_instance(map_name: str) -> bool:
    """Start one instance and wait until ready. Used by restore. Caller
    is expected to hold the op lock."""
    config = load_existing(load_settings())
    procs = _ensure_processes()
    target = next((p for p in procs
                   if p.runtime_instance.instance.map_name == map_name), None)
    if target is None:
        log.error("start_single_instance: no process for map %s", map_name)
        return False
    ri = target.runtime_instance
    if target.is_alive:
        log.info("[%s] already running, skipping spawn", ri.instance.name)
        return True
    args = build_launch_args(config, ri)
    target.start(args)
    log.info("[%s] waiting for ready signals...", ri.instance.name)
    if _wait_for_ready(ri, config, target):
        log.info("[%s] READY.", ri.instance.name)
        _enable_chat_log(ri)
        return True
    log.error("[%s] did NOT become ready (cap reached or process tree died)",
              ri.instance.name)
    return False


def running_instances() -> list[tuple]:
    """Public accessor for currently-alive instances. Returns
    [(RuntimeInstance, InstanceProcess), ...] for every process whose tree
    is still alive. Used by the backups engine and any future feature that
    needs to know what's currently running without poking at lifecycle's
    private state.
    """
    return [(p.runtime_instance, p)
            for p in _ensure_processes() if p.is_alive]


def _format_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    days, rem = divmod(secs, 86400)
    return f"{days}d {rem // 3600}h"


# ── Start ───────────────────────────────────────────────────────────────────


def start_all() -> bool:
    """Kick off start sequence in a thread. Returns False if another op is
    already in progress (UI should retry or wait)."""
    if not _op_lock.acquire(blocking=False):
        log.warning("start_all: another op in progress, refusing")
        return False
    threading.Thread(target=_start_impl, daemon=True, name="server-start").start()
    return True


def start_one(map_name: str) -> bool:
    """Start ONE specific instance by map_name. Same op-lock + thread
    pattern as start_all, so only one start/stop op can be in flight
    cluster-wide. Returns False if another op holds the lock OR the
    map_name doesn't match a known instance.

    Caveat: in cluster mode, starting only the CHILD without the MAIN
    running will spawn the child but its cluster-link
    (-clientserverconnect=127.0.0.1:<mainserverport>) will fail until
    main exists. The frontend warns the operator about this; we don't
    block here -- recovery flows sometimes need to fight that order."""
    if not _op_lock.acquire(blocking=False):
        log.warning("start_one(%s): another op in progress, refusing",
                    map_name)
        return False
    threading.Thread(target=_start_one_impl, args=(map_name,),
                     daemon=True, name=f"server-start-{map_name}").start()
    return True


def _start_one_impl(map_name: str) -> None:
    """Inner single-instance start. Mirrors _start_impl but for one
    target. Holds the op-lock across the whole sequence so subsequent
    start/stop clicks are refused until this finishes."""
    _begin_op("start")
    try:
        log.info("=" * 50)
        log.info("START sequence: single target -- %s", map_name)
        log.info("=" * 50)
        ok = start_single_instance(map_name)
        if ok:
            _reapply_login_locks_safe()
            log.info("START sequence complete (one).")
        else:
            log.error("START sequence FAILED for %s -- see preceding "
                      "log lines.", map_name)
    except Exception:
        log.exception("start-one sequence raised")
    finally:
        _op_lock.release()
        _end_op()


def _start_impl(op_name: str = "start") -> None:
    """Inner start implementation. op_name parameter lets the
    update_and_restart sequence keep its banner label across phases."""
    _begin_op(op_name)
    try:
        config = load_existing(load_settings())
        procs = _ensure_processes()
        log.info("=" * 50)
        log.info("START sequence: mode=%s, %d instance(s)", config.mode, len(procs))
        log.info("=" * 50)

        for proc in procs:
            ri = proc.runtime_instance
            if proc.is_alive:
                log.info("[%s] already running (PID %d), skipping spawn",
                         ri.instance.name, proc.pid)
            else:
                args = build_launch_args(config, ri)
                proc.start(args)

            need_main_port = config.mode == "cluster" and ri.role == "main"
            signals_desc = "EchoPort"
            if need_main_port:
                signals_desc += " + cluster link port"
            log.info("[%s] waiting for ready signals (%s); slow servers "
                     "report up to 10 min start time, so we wait patiently...",
                     ri.instance.name, signals_desc)

            if _wait_for_ready(ri, config, proc):
                log.info("[%s] READY.", ri.instance.name)
                _enable_chat_log(ri)
            else:
                log.error("[%s] did NOT become ready within %s (hard cap). "
                          "Process is left RUNNING; manual investigation needed. "
                          "Continuing to next instance anyway -- subsequent ones "
                          "may not link correctly.",
                          ri.instance.name, _format_duration(_op_hard_cap_sec()))

        _reapply_login_locks_safe()
        log.info("START sequence complete.")
    except Exception:
        log.exception("start sequence raised")
    finally:
        # Release the lock BEFORE clearing the op-state vars so a
        # concurrent get_status() read never sees op_in_progress=True with
        # _current_op_name=None (which renders as an empty op label).
        _op_lock.release()
        _end_op()


def _is_tcp_listening(host: str, port: int, timeout: float = 1.5) -> bool:
    """Quick TCP connect check. Returns True if a listener accepts the
    connection. Used as a low-cost readiness signal for both EchoPort and
    the cluster link port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            log.verbose("tcp-probe: %s:%d listening", host, port)
            return True
    except (OSError, ConnectionError) as e:
        log.verbose("tcp-probe: %s:%d not yet listening (%s)", host, port, e)
        return False


def _wait_for_ready(ri, config, proc=None) -> bool:
    """Wait until the instance is truly ready (EchoPort + cluster link port
    if main). Patient: heartbeats every 30s, escalating warning tone as
    elapsed time grows, hard cap from operations.hard_cap_min. Returns True
    if ready, False if cap reached OR if the spawned process tree dies
    before becoming ready.

    `proc` is the InstanceProcess we just spawned. If provided, the loop
    aborts immediately when its tree dies -- saves up to hard_cap_min of
    pointless port-probing while the op-lock is held, which previously
    left the dashboard stuck on "in progress" until the operator
    restarted the manager. Default None preserves backwards compat for
    any caller that doesn't have a proc handle (none currently)."""
    is_cluster_main = config.mode == "cluster" and ri.role == "main"

    echo_ready = False
    main_port_ready = not is_cluster_main

    started = time.monotonic()
    last_heartbeat_log = 0
    hard_cap = _op_hard_cap_sec()
    while True:
        elapsed = int(time.monotonic() - started)
        if elapsed >= hard_cap:
            return False

        # Abort if the process tree died (genuine crash OR operator
        # closed the WSServer cmd window). Without this the wait loop
        # polls EchoPort forever and the op-lock stays held; dashboard
        # appears "in progress" indefinitely.
        if proc is not None and not proc.is_alive:
            log.error("[%s] process tree died during wait_for_ready "
                      "after %s -- aborting wait. Check WS.log for the "
                      "last lines from the dying process.",
                      ri.instance.name, _format_duration(elapsed))
            return False

        if not echo_ready and _is_tcp_listening("127.0.0.1", ri.instance.echo_port):
            echo_ready = True
            log.info("[%s] EchoPort %d listening (after %s)",
                     ri.instance.name, ri.instance.echo_port,
                     _format_duration(elapsed))
        if not main_port_ready and _is_tcp_listening("127.0.0.1", config.mainserverport):
            main_port_ready = True
            log.info("[%s] cluster link port %d listening (after %s) -- "
                     "child can now wire up", ri.instance.name,
                     config.mainserverport, _format_duration(elapsed))
        if echo_ready and main_port_ready:
            return True

        # Heartbeat every 30 sec with escalating tone.
        if elapsed - last_heartbeat_log >= 30 and elapsed > 0:
            last_heartbeat_log = elapsed
            missing = []
            if not echo_ready:
                missing.append(f"EchoPort:{ri.instance.echo_port}")
            if not main_port_ready:
                missing.append(f"clusterlink:{config.mainserverport}")
            level_fn, label = _slow_log_for(elapsed)
            level_fn("[%s] %s -- still waiting on [%s] (%s elapsed)",
                     ri.instance.name, label, ", ".join(missing),
                     _format_duration(elapsed))

        time.sleep(_READY_POLL_SEC)


def _slow_log_for(elapsed: int):
    """Return (logging fn, tone label) based on elapsed seconds."""
    if elapsed >= _op_very_slow_sec():
        return log.error, "VERY SLOW"
    if elapsed >= _op_slow_sec():
        return log.warning, "slow"
    return log.info, "in progress"


def _maybe_run_pre_shutdown_backup() -> None:
    """Setting-gated pre-shutdown snapshot. Logged failure is non-fatal:
    the operator clicked Stop, we're not going to refuse based on a
    flaky EchoPort or a slow disk. Spec: BACKUPS.md 'Pre-shutdown +
    pre-update backups'."""
    if not bool(get_setting("backups.pre_shutdown_backup_enabled", True)):
        log.info("pre-shutdown backup disabled in settings; skipping")
        return
    try:
        from manager import backups
        ok = backups.run_inline_pre_op_snapshot("pre-shutdown")
        if not ok:
            log.warning("pre-shutdown backup did NOT complete cleanly "
                        "(continuing with shutdown anyway)")
    except Exception:
        log.exception("pre-shutdown backup raised (continuing)")


def _run_mandatory_pre_update_backup() -> None:
    """Mandatory pre-update snapshot. The rollback target if SteamCMD
    update corrupts the world DB. Per BACKUPS.md, cannot be disabled --
    not gated on any setting. Failure is still non-fatal (logged) so a
    backup hiccup doesn't strand the operator without an update path."""
    try:
        from manager import backups
        ok = backups.run_inline_pre_op_snapshot("pre-update")
        if not ok:
            log.error("pre-update backup did NOT complete cleanly. "
                      "Continuing with update -- but if it goes wrong, "
                      "the rollback target may be missing or stale.")
    except Exception:
        log.exception("pre-update backup raised (continuing with update)")


def _reapply_login_locks_safe() -> None:
    """Re-apply persisted login-lock state to all running instances. Used
    after start/adoption so the lock survives a manager restart."""
    try:
        from manager import login_lock
        result = login_lock.apply_locks()
        if result:
            log.info("Login-lock state re-applied across %d instance(s): %s",
                     len(result), result)
    except Exception:
        log.exception("login-lock re-apply raised (non-fatal)")


def _enable_chat_log(ri) -> None:
    """Send Set_OutputChats 1 via EchoPort so chat appears in WS.log.
    Idempotent; setting persists across server restarts but we send on every
    start for safety. Failure is non-fatal (logged at WARNING)."""
    try:
        resp = echo.send_command("127.0.0.1", ri.instance.echo_port,
                                 "Set_OutputChats 1")
        cleaned = resp.replace("\r", " ").replace("\n", " ")[:200].strip()
        log.info("[%s] chat-log enabled (Set_OutputChats 1)%s",
                 ri.instance.name,
                 f" -- resp: {cleaned}" if cleaned else "")
    except (OSError, ConnectionError) as e:
        log.warning("[%s] could not enable chat-log via EchoPort: %s",
                    ri.instance.name, e)


# ── Stop ────────────────────────────────────────────────────────────────────


def cancel_stop() -> dict:
    """Operator clicked "Cancel shutdown" during an in-flight stop op.

    For each instance currently in countdown (`shutdown_requested_at` set):
      1. Send `cc` (StopCloseServer) over EchoPort.
      2. Mark `cancel_pending` so the waiting _stop_impl loop reacts.
    Then re-apply persisted login-lock state (typically `sl 1`).

    The wait loop in _stop_impl picks up `cancel_pending` on its next
    iteration, settles 3 seconds to confirm whether `cc` worked, and
    either clears the shutdown flag (cc succeeded) or sets
    `auto_restart_after_death = True` (cc was too late, instance will
    die and we'll spin it back up).

    Returns {"acted_on": [map_names], "any": bool}.
    """
    procs = _ensure_processes()
    acted: list[str] = []
    for proc in procs:
        ri = proc.runtime_instance
        if proc.shutdown_requested_at is None and not proc.cancel_pending:
            # Either not in shutdown, or already handled in a prior cancel.
            continue
        log.info("[%s] cancel: sending cc to EchoPort", ri.instance.name)
        try:
            resp = echo.send_command("127.0.0.1", ri.instance.echo_port, "cc")
            cleaned = resp.replace("\r", " ").replace("\n", " ")[:200].strip()
            log.info("[%s]   cc response: %s", ri.instance.name,
                     cleaned or "(empty)")
        except (OSError, ConnectionError) as e:
            log.warning("[%s] cc send failed: %s", ri.instance.name, e)
        proc.cancel_pending = True
        acted.append(ri.instance.map_name)

    # Re-apply persisted login-lock state. If the operator had a manual
    # cluster lock on, this preserves it; otherwise this fires `sl 1`.
    if acted:
        try:
            from manager import login_lock
            login_lock.apply_locks()
        except Exception:
            log.exception("cancel: post-cancel login-lock apply raised")

    return {"acted_on": acted, "any": bool(acted)}


def stop_all(countdown_sec: Optional[int] = None) -> bool:
    """Trigger a graceful stop in a background thread. countdown_sec is the
    in-game warning before save+exit. None = use the configured default.
    Clamped to [1, 3600]; passing 0 is silently bumped to 1 because Soulmask
    interprets bare 0 as 'use the default 300 sec'."""
    if not _op_lock.acquire(blocking=False):
        log.warning("stop_all: another op in progress, refusing")
        return False
    if countdown_sec is None:
        countdown_sec = _default_countdown_sec()
    cd = max(_MIN_COUNTDOWN_SEC, min(_MAX_COUNTDOWN_SEC, int(countdown_sec)))
    if cd != countdown_sec:
        log.info("stop_all: countdown clamped from %s to %d", countdown_sec, cd)
    threading.Thread(target=_stop_impl, args=(cd,),
                     daemon=True, name="server-stop").start()
    return True


def _do_auto_restart_phase() -> None:
    """After a stop sequence, if any process is dead AND has
    `auto_restart_after_death=True` (set when a Cancel arrived too late
    for cc to take effect), spin those instances back up.

    Runs while the op_lock is still held by the caller, so no other op
    can interleave. Intentionally never raises -- a restart failure
    leaves the auto_restart_after_death flag clear and the operator can
    use the regular Start button.
    """
    config = load_existing(load_settings())
    procs = _ensure_processes()
    pending = [p for p in procs if p.auto_restart_after_death]
    if not pending:
        return
    log.info("=" * 50)
    log.info("AUTO-RESTART phase: %d instance(s) -- %s",
             len(pending),
             [p.runtime_instance.instance.name for p in pending])
    log.info("=" * 50)
    for proc in pending:
        ri = proc.runtime_instance
        if proc.is_alive:
            # Race: instance came back up somehow. Just clear the flag.
            log.info("[%s] auto-restart: already alive, clearing flag",
                     ri.instance.name)
            proc.auto_restart_after_death = False
            continue
        try:
            args = build_launch_args(config, ri)
            proc.start(args)  # this clears auto_restart_after_death
            log.info("[%s] auto-restart: waiting for ready signals...",
                     ri.instance.name)
            if _wait_for_ready(ri, config, proc):
                log.info("[%s] auto-restart: READY", ri.instance.name)
                _enable_chat_log(ri)
            else:
                log.error("[%s] auto-restart: did NOT become ready "
                          "(cap reached or process tree died).",
                          ri.instance.name)
        except Exception:
            log.exception("[%s] auto-restart raised", ri.instance.name)
            proc.auto_restart_after_death = False
    _reapply_login_locks_safe()


def _per_instance_countdown(ri, full_countdown_sec: int) -> int:
    """Pick the actual countdown for this instance: full duration if anyone
    is connected (so we warn them), 1s (effectively immediate) if the
    server is empty -- no point making nobody wait. Falls back to the full
    countdown if the Steam query fails (assume someone is there)."""
    info = query.query_server("127.0.0.1", ri.instance.query_port, timeout=2.0)
    if info is None:
        log.info("[%s] player-count check: query failed -- using full %ds "
                 "countdown to be safe", ri.instance.name, full_countdown_sec)
        return full_countdown_sec
    n = info.get("players", 0) or 0
    if n == 0:
        log.info("[%s] player-count check: 0/%d connected -- shutting down "
                 "immediately (no one to warn)", ri.instance.name,
                 info.get("max_players", 0) or 0)
        return _MIN_COUNTDOWN_SEC
    log.info("[%s] player-count check: %d/%d connected -- using full %ds countdown",
             ri.instance.name, n, info.get("max_players", 0) or 0, full_countdown_sec)
    return full_countdown_sec


def _stop_impl(countdown_sec: Optional[int] = None,
               op_name: str = "stop") -> None:
    if countdown_sec is None:
        countdown_sec = _default_countdown_sec()
    """Inner stop implementation. The op_name parameter lets the
    update_and_restart sequence reuse this without the dashboard banner
    showing "stop" mid-operation."""
    _begin_op(op_name)
    try:
        # Pre-shutdown auto-backup. Setting-gated, fires before any
        # SaveAndExit so the snapshot reflects pre-countdown state and
        # gives a clean rollback target. Failure is logged, not fatal --
        # if backup fails, operator already approved the stop.
        _maybe_run_pre_shutdown_backup()

        # Reverse order: child first, main last.
        procs = list(reversed(_ensure_processes()))
        log.info("=" * 50)
        log.info("STOP sequence: %d instance(s), countdown=%ds", len(procs), countdown_sec)
        log.info("=" * 50)

        for proc in procs:
            ri = proc.runtime_instance

            # Pre-loop cancel check: the operator clicked Cancel BETWEEN
            # instances (e.g. child got SaveAndExit, main hasn't been
            # touched yet). Without this, main would still be told to
            # save+exit even though the operator wants to abort.
            # cancel_stop() sets cancel_pending only on procs whose
            # shutdown was already in flight, so checking _any_ here
            # also implicitly distinguishes "real cancel" from "fresh
            # stop request".
            if any(p.cancel_pending for p in procs):
                pending = [p.runtime_instance.instance.name
                           for p in procs if p.cancel_pending]
                log.info("STOP sequence: cancel detected for %s before "
                         "[%s] was processed -- aborting remaining stops. "
                         "Already-shutting-down instances will be handled "
                         "by their own wait loops.",
                         pending, ri.instance.name)
                return

            if not proc.is_alive:
                log.info("[%s] not running, skipping", ri.instance.name)
                continue

            # Per-instance countdown: 1s if no players online, full
            # countdown otherwise. Players in CloudMist shouldn't be made
            # to wait while we honour a countdown for an empty ShiftingSands.
            effective_cd = _per_instance_countdown(ri, countdown_sec)

            log.info("[%s] graceful shutdown via EchoPort 127.0.0.1:%d (countdown %ds)",
                     ri.instance.name, ri.instance.echo_port, effective_cd)
            # Lock new logins BEFORE the countdown starts so latecomers
            # don't slip in during the warning window. Best-effort -- a
            # failure here does NOT abort the shutdown.
            try:
                from manager import login_lock
                login_lock.force_lock_for_shutdown(ri)
            except Exception:
                log.exception("[%s] login-lock for shutdown raised (continuing)",
                              ri.instance.name)
            # Manager-prefixed shutdown announcement. Soulmask emits its
            # own countdown messages once SaveAndExit lands; this just
            # makes the cause + login-lock context explicit. Skip the
            # broadcast for very short countdowns (empty-server case)
            # since there's nobody to read it.
            if effective_cd >= 30:
                try:
                    from manager import broadcasts
                    broadcasts.warn_pre_shutdown(ri, effective_cd,
                                                 reason="manual stop")
                except Exception:
                    log.exception("[%s] shutdown broadcast raised (continuing)",
                                  ri.instance.name)
            try:
                resp = echo.send_command(
                    "127.0.0.1", ri.instance.echo_port,
                    f"SaveAndExit {effective_cd}",
                )
                cleaned = resp.replace("\r", " ").replace("\n", " ")[:200].strip()
                log.info("[%s] EchoPort response: %s", ri.instance.name,
                         cleaned or "(empty -- command sent, no ack text)")
                # The server has accepted the shutdown command. Mark the
                # instance as 'shutting down' so the dashboard reflects the
                # countdown phase (running -> shutting_down -> stopped).
                proc.shutdown_requested_at = datetime.now()
                _notify_dashboard()  # badge -> shutting down
            except (OSError, ConnectionError) as e:
                # CRITICAL: do NOT terminate as a fallback. Killing without
                # a save risks corrupting world.db. We leave the process
                # alive and bail out of the stop sequence so cluster ordering
                # is preserved (don't stop main while child is still up).
                log.error("[%s] EchoPort unreachable (%s) -- CANNOT send SaveAndExit. "
                          "Process is left ALIVE on purpose to protect world data. "
                          "ABORTING stop sequence (will not stop further instances).",
                          ri.instance.name, e)
                return

            log.info("[%s] waiting for ENTIRE process tree to exit gracefully "
                     "(slow VMs can take 10+ min; we wait patiently)...",
                     ri.instance.name)
            wait_started = time.monotonic()
            last_heartbeat_log = 0
            while True:
                elapsed = int(time.monotonic() - wait_started)

                # Operator clicked Cancel mid-countdown. Two outcomes:
                # (a) cc succeeded -- instance still alive after settle ->
                #     clear shutdown state, break this wait, continue
                #     stop sequence (which will skip this instance because
                #     shutdown_requested_at is now None and is_alive is True;
                #     actually, we just break the while and move on).
                # (b) cc was late -- instance died anyway. Mark for
                #     auto-restart and fall through to the exit branch.
                if proc.cancel_pending:
                    log.info("[%s] cancel detected -- settling 3s to see if "
                             "cc succeeded", ri.instance.name)
                    time.sleep(3)
                    if proc.is_alive:
                        log.info("[%s] cc succeeded; instance still alive. "
                                 "Clearing shutdown state.",
                                 ri.instance.name)
                        proc.shutdown_requested_at = None
                        proc.cancel_pending = False
                        _notify_dashboard()  # badge -> running
                        # Don't try to stop further instances after a cancel.
                        log.info("STOP sequence cancelled by operator.")
                        return
                    log.info("[%s] cc was too late; instance exited. "
                             "Marking auto-restart-after-death.",
                             ri.instance.name)
                    proc.auto_restart_after_death = True
                    proc.cancel_pending = False
                    _notify_dashboard()  # badge -> auto-restart pending
                    break

                if not proc.is_alive:  # is_alive refreshes the tree internally
                    log.info("[%s] tree fully exited after %s",
                             ri.instance.name, _format_duration(elapsed))
                    break

                if elapsed >= _op_hard_cap_sec():
                    log.error("[%s] tree STILL ALIVE after %s (hard monitoring cap). "
                              "NOT killing -- world data preservation comes first. "
                              "Process is left running; manual investigation needed. "
                              "ABORTING rest of stop sequence.",
                              ri.instance.name, _format_duration(elapsed))
                    return

                # Heartbeat every 30 sec with escalating tone.
                if elapsed - last_heartbeat_log >= 30 and elapsed > 0:
                    last_heartbeat_log = elapsed
                    level_fn, label = _slow_log_for(elapsed)
                    level_fn("[%s] %s -- still waiting for graceful exit (%s elapsed)",
                             ri.instance.name, label, _format_duration(elapsed))

                time.sleep(1)

        # If any instance was marked auto-restart-after-death (cancel hit
        # too late), spin them back up before releasing the op lock.
        _do_auto_restart_phase()

        log.info("STOP sequence complete.")
    except Exception:
        log.exception("stop sequence raised")
    finally:
        # Release the lock BEFORE clearing the op-state vars so a
        # concurrent get_status() read never sees op_in_progress=True with
        # _current_op_name=None (which renders as an empty op label).
        _op_lock.release()
        _end_op()


# ── Update + Restart (one-click) ────────────────────────────────────────────


def update_and_restart(countdown_sec: Optional[int] = None) -> bool:
    """Full cycle: graceful stop -> SteamCMD app_update -> start. All under
    a single op_lock so no other op interleaves. The dashboard banner shows
    op_name = "update+restart" throughout."""
    if not _op_lock.acquire(blocking=False):
        log.warning("update_and_restart: another op in progress, refusing")
        return False
    if countdown_sec is None:
        countdown_sec = _default_countdown_sec()
    cd = max(_MIN_COUNTDOWN_SEC, min(_MAX_COUNTDOWN_SEC, int(countdown_sec)))
    threading.Thread(target=_update_and_restart_impl, args=(cd,),
                     daemon=True, name="update-and-restart").start()
    return True


def _update_and_restart_impl(countdown_sec: int) -> None:
    _begin_op("update+restart")
    try:
        log.info("=" * 50)
        log.info("UPDATE+RESTART sequence starting (stop countdown %ds)", countdown_sec)
        log.info("=" * 50)

        # PHASE 0: mandatory pre-update backup. This is the rollback
        # target if the update breaks the world DB; cannot be disabled.
        _run_mandatory_pre_update_backup()

        # PHASE 1: graceful stop. Reuse _stop_impl but it will call
        # _begin_op/_end_op/_op_lock.release -- we don't want that here
        # since we're already holding the lock. So run the stop body
        # directly without re-acquiring. Suppress the in-stop-phase
        # pre-shutdown backup since PHASE 0 just took an identical one
        # seconds ago (verified byte-identical .gz output in field).
        _do_stop_phase(countdown_sec, skip_pre_shutdown_backup=True)

        # PHASE 2: SteamCMD app_update. Only safe with all servers stopped.
        # If anything in the tree is still alive, the install dir is locked
        # and SteamCMD will fail / corrupt. _do_stop_phase logs an error and
        # returns early if any tree wouldn't exit; check for that.
        any_still_alive = any(p.is_alive for p in _ensure_processes())
        if any_still_alive:
            log.error("UPDATE+RESTART: at least one server is still alive after stop "
                      "phase -- ABORTING update so install dir isn't corrupted. "
                      "Resolve the stuck instance manually and try again.")
            return

        log.info("PHASE 2: running SteamCMD app_update for app %d",
                 SOULMASK_WINDOWS_APP_ID)
        if not _run_app_update():
            log.error("UPDATE+RESTART: SteamCMD app_update failed -- NOT restarting "
                      "servers. Install dir may be in a partial state; investigate.")
            return

        # Refresh local buildid display immediately after a successful update.
        new_local = updates.read_local_buildid()
        log.info("PHASE 2 done. New local buildid: %s", new_local)

        # PHASE 3: start. Same situation -- run body directly, don't re-lock.
        _do_start_phase()

        log.info("UPDATE+RESTART sequence complete.")
    except Exception:
        log.exception("update+restart raised")
    finally:
        # Release the lock BEFORE clearing the op-state vars so a
        # concurrent get_status() read never sees op_in_progress=True with
        # _current_op_name=None (which renders as an empty op label).
        _op_lock.release()
        _end_op()


def _do_stop_phase(countdown_sec: int,
                   skip_pre_shutdown_backup: bool = False) -> None:
    """The body of _stop_impl, factored out so update_and_restart can call
    it without nested op-lock acquisition.

    `skip_pre_shutdown_backup` lets update_and_restart suppress the
    pre-shutdown snapshot when it has ALREADY run the mandatory
    pre-update one seconds earlier. The two snapshots in that flow
    captured byte-identical .gz files in field tests (verified
    2026-04-29 against backups.json + manager.log: 19:06:58 pre-update
    and 19:07:05 pre-shutdown produced identical 8,372,321 / 8,645,488
    byte outputs for cloudmist / shiftingsands). Doubling the operator
    wait by ~8s and disk by ~17 MB per Update+Restart for zero extra
    information. Plain Stop (not Update+Restart) keeps the
    pre-shutdown backup -- there it's the only safety net."""
    if not skip_pre_shutdown_backup:
        _maybe_run_pre_shutdown_backup()
    else:
        log.info("PHASE 1: skipping pre-shutdown backup (already covered "
                 "by the mandatory pre-update backup in PHASE 0)")
    procs = list(reversed(_ensure_processes()))
    log.info("PHASE 1 (stop): %d instance(s), countdown=%ds",
             len(procs), countdown_sec)
    for proc in procs:
        ri = proc.runtime_instance
        if not proc.is_alive:
            log.info("[%s] not running, skipping", ri.instance.name)
            continue

        # Per-instance countdown: empty server stops immediately.
        effective_cd = _per_instance_countdown(ri, countdown_sec)

        log.info("[%s] graceful shutdown via EchoPort 127.0.0.1:%d (countdown %ds)",
                 ri.instance.name, ri.instance.echo_port, effective_cd)
        try:
            from manager import login_lock
            login_lock.force_lock_for_shutdown(ri)
        except Exception:
            log.exception("[%s] login-lock for update-shutdown raised (continuing)",
                          ri.instance.name)
        if effective_cd >= 30:
            try:
                from manager import broadcasts
                broadcasts.warn_pre_shutdown(ri, effective_cd,
                                             reason="update + restart")
            except Exception:
                log.exception("[%s] update-shutdown broadcast raised (continuing)",
                              ri.instance.name)
        try:
            resp = echo.send_command(
                "127.0.0.1", ri.instance.echo_port,
                f"SaveAndExit {effective_cd}",
            )
            cleaned = resp.replace("\r", " ").replace("\n", " ")[:200].strip()
            log.info("[%s] EchoPort response: %s", ri.instance.name,
                     cleaned or "(empty -- command sent, no ack text)")
            proc.shutdown_requested_at = datetime.now()
        except (OSError, ConnectionError) as e:
            log.error("[%s] EchoPort unreachable (%s) -- ABORTING update+restart",
                      ri.instance.name, e)
            return

        wait_started = time.monotonic()
        last_heartbeat_log = 0
        while True:
            elapsed = int(time.monotonic() - wait_started)
            if not proc.is_alive:
                log.info("[%s] tree fully exited after %s",
                         ri.instance.name, _format_duration(elapsed))
                break
            if elapsed >= _op_hard_cap_sec():
                log.error("[%s] tree STILL ALIVE after %s -- ABORTING update+restart",
                          ri.instance.name, _format_duration(elapsed))
                return
            if elapsed - last_heartbeat_log >= 30 and elapsed > 0:
                last_heartbeat_log = elapsed
                level_fn, label = _slow_log_for(elapsed)
                level_fn("[%s] %s -- still waiting for graceful exit (%s elapsed)",
                         ri.instance.name, label, _format_duration(elapsed))
            time.sleep(1)


def _do_start_phase() -> None:
    """The body of _start_impl, factored out so update_and_restart can call
    it without nested op-lock acquisition."""
    config = load_existing(load_settings())
    procs = _ensure_processes()
    log.info("PHASE 3 (start): mode=%s, %d instance(s)", config.mode, len(procs))
    for proc in procs:
        ri = proc.runtime_instance
        if proc.is_alive:
            log.info("[%s] already running (PID %d), skipping spawn",
                     ri.instance.name, proc.pid)
        else:
            args = build_launch_args(config, ri)
            proc.start(args)
        need_main_port = config.mode == "cluster" and ri.role == "main"
        signals_desc = "EchoPort"
        if need_main_port:
            signals_desc += " + cluster link port"
        log.info("[%s] waiting for ready signals (%s)...",
                 ri.instance.name, signals_desc)
        if _wait_for_ready(ri, config, proc):
            log.info("[%s] READY.", ri.instance.name)
            _enable_chat_log(ri)
        else:
            log.error("[%s] did NOT become ready (cap reached or "
                      "process tree died); continuing to next instance",
                      ri.instance.name)


def _run_app_update() -> bool:
    """Run SteamCMD +app_update, streaming output to manager.log. Returns
    True on rc=0, False otherwise. Uses subprocess directly (not shared
    with install.py) so update+restart doesn't depend on install state.

    Acquires the cross-cutting SteamCMD lock so a poll-time
    `+app_info_print` from manager.updates can't run simultaneously
    with the multi-GB `+app_update`. Operator already committed by
    clicking Update+Restart, so we wait blocking with a generous
    timeout (poller iterations are normally <30s); abort cleanly if
    it really won't release."""
    from manager import updates as _steam_updates
    if not _steam_updates.acquire_steamcmd_blocking(timeout_sec=60.0):
        log.error("_run_app_update: could not acquire steamcmd lock "
                  "within 60s -- aborting update. Investigate stuck "
                  "poller or duplicate steamcmd processes.")
        return False
    try:
        # Update+Restart historically always validated; preserve that.
        return _run_app_update_locked(validate=True)
    finally:
        _steam_updates.release_steamcmd_lock()


def _run_app_update_locked(validate: bool = True) -> bool:
    """Body of _run_app_update; runs while holding the steamcmd lock.
    Split out so the lock acquire/release is a thin wrapper.

    `validate=True` adds Steam's `validate` keyword to the app_update
    line: every installed file is hashed and re-downloaded if it
    doesn't match the manifest. Slower but catches corruption.
    `validate=False` does a manifest-diff update only -- faster, used
    by the new manual Update button on /updates."""
    cmd_path = steamcmd_exe()
    if not cmd_path.exists():
        log.error("_run_app_update: SteamCMD not present at %s", cmd_path)
        return False

    target = install_dir()
    app_update_cmd = ["+app_update", str(SOULMASK_WINDOWS_APP_ID)]
    if validate:
        app_update_cmd.append("validate")
    cmd = [
        str(cmd_path),
        "+force_install_dir", str(target),
        "+login", "anonymous",
        *app_update_cmd,
        "+quit",
    ]
    log.info("_run_app_update(validate=%s): %s", validate, cmd)

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError as e:
        log.error("_run_app_update: failed to launch SteamCMD: %s", e)
        return False

    assert proc.stdout is not None
    # Stream raw bytes (same trick as install.py to bypass Windows pipe
    # buffering during long Steam server waits).
    buf = bytearray()
    while True:
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            i_n = buf.find(b"\n")
            i_r = buf.find(b"\r")
            if i_n == -1 and i_r == -1:
                break
            if i_n == -1: i = i_r
            elif i_r == -1: i = i_n
            else: i = min(i_n, i_r)
            line_bytes = bytes(buf[:i])
            if buf[i:i + 2] == b"\r\n":
                del buf[:i + 2]
            else:
                del buf[:i + 1]
            if line_bytes:
                line = line_bytes.decode("utf-8", errors="replace")
                # Promote interesting lines to INFO so the operator sees
                # update progress at the default INFO log level. Without
                # this the 5-30 min update goes silent at INFO (DEBUG
                # firehose contains everything).
                if any(marker in line for marker in
                       ("Update state", "Success!", "ERROR!", "FAILED",
                        "Install state", "verifying", "complete")):
                    log.info("[steamcmd] %s", line)
                else:
                    log.debug("[steamcmd] %s", line)
                # Tee to /updates SSE activity log so the operator can
                # watch progress on the page that triggered the op.
                # Best-effort -- a deque write + Event set, no I/O.
                try:
                    from manager import updates_log
                    updates_log.append("steam", line)
                except Exception:
                    pass
    if buf:
        log.debug("[steamcmd] %s", bytes(buf).decode("utf-8", errors="replace"))
    proc.wait()
    elapsed = time.monotonic() - started
    log.info("_run_app_update: SteamCMD exited rc=%d after %.1fs",
             proc.returncode, elapsed)
    return proc.returncode == 0


# ── Manual SteamCMD ops (manual Update / Verify buttons on /updates) ────────


def start_app_update(validate: bool = False) -> tuple[bool, str]:
    """Async kick-off for a manual SteamCMD app_update from /updates.
    `validate=False` is the quick "Update" button (manifest diff).
    `validate=True` is the "Verify" button (hashes every file).

    Refuses with a human-readable reason if:
      - any game-server instance is currently alive (SteamCMD can't
        lock the install dir while WSServer.exe holds it open).
      - another /updates op is already running (begin_op refuses).
      - the steamcmd lock is held (poller mid-check or another op).

    On accept, drops onto the background worker, holds the steamcmd
    lock + updates_log op-state for the duration, and tees SteamCMD
    output to the /updates activity log so the operator watches
    progress live.

    Does NOT auto-restart anything afterward -- the operator does that
    explicitly. (Update+Restart from the dashboard remains the
    coupled flow.)"""
    from manager import background, updates as _steam_updates, updates_log

    # Refuse if any server is alive. SteamCMD's app_update needs an
    # exclusive lock on the install dir; with WSServer.exe holding
    # files open it'll fail mid-stream and leave the install in a
    # half-applied state. Catch this BEFORE acquiring the steamcmd
    # lock so we don't briefly block the poller for nothing.
    alive = [p for _, p in running_instances() if p.is_alive]
    if alive:
        names = ", ".join(p.runtime_instance.instance.name for p in alive)
        return (False,
                f"Stop the server(s) first: {names}. SteamCMD can't "
                "update the install while WSServer.exe holds files open.")

    if not _steam_updates.try_acquire_steamcmd_lock():
        return (False,
                "Another SteamCMD operation is in progress (poller "
                "mid-check or manual op). Try again in a few seconds.")

    op_label = ("Verify game files (SteamCMD)" if validate
                else "Update game (SteamCMD)")
    if not updates_log.begin_op(op_label, "steam"):
        _steam_updates.release_steamcmd_lock()
        return (False,
                "Another update operation is in progress on /updates. "
                "Wait for it to finish.")

    def _runner():
        ok = False
        try:
            try:
                ok = _run_app_update_locked(validate=validate)
            except Exception as e:
                log.exception("start_app_update runner raised")
                updates_log.end_op(False, f"{type(e).__name__}: {e}")
                return
            updates_log.end_op(
                ok,
                "" if ok else "SteamCMD exited non-zero -- check the "
                              "activity log for details.")
        finally:
            _steam_updates.release_steamcmd_lock()

    background.submit(
        "steam-app-verify" if validate else "steam-app-update",
        _runner,
    )
    return (True, op_label + " started")
