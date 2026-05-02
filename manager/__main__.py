"""Entry point: `python -m manager` launches the Flask app."""

import ctypes
import logging
import sys
import traceback

# Defer manager imports into main() so we can catch and surface ImportErrors
# clearly. (If they ran at module import time, they'd dump a stack trace
# before any of our logging is set up.)


def _disable_console_quick_edit() -> None:
    """Clear the ENABLE_QUICK_EDIT_MODE flag on stdin's console handle so
    a stray operator click in the cmd window can't pause the manager.

    Win32 console "Quick Edit" lets the user click & drag to select text,
    but during the selection the parent process's console output buffer
    fills and blocks. With Python's logging routing every log call (file
    + console) through one shared lock, that means EVERY thread that
    tries to log gets stuck behind the frozen console -- the entire
    manager appears hung until the operator presses Enter.

    Empirically observed in manager.log as multi-minute gaps with zero
    log activity from any thread (dashboard SSE, scheduler, request
    handlers all silent). Disabling QE at startup makes that impossible.
    Window resize / scroll / Ctrl+C still work; the only thing lost is
    click-to-select-text in the manager's own console.

    No-op on non-Windows. Best-effort -- if the call fails for any
    reason we log a debug line and carry on.
    """
    if sys.platform != "win32":
        return
    STD_INPUT_HANDLE = -10
    ENABLE_EXTENDED_FLAGS = 0x0080
    ENABLE_QUICK_EDIT_MODE = 0x0040
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    try:
        kernel32 = ctypes.windll.kernel32
        # GetStdHandle returns a HANDLE (void*); declare so 64-bit handles
        # don't get truncated to int.
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle == INVALID_HANDLE_VALUE:
            logging.getLogger(__name__).debug(
                "console QE disable: no stdin console handle (likely "
                "running headless / piped) -- skipping")
            return
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(ctypes.c_void_p(handle),
                                        ctypes.byref(mode)):
            logging.getLogger(__name__).debug(
                "console QE disable: GetConsoleMode failed -- skipping")
            return
        # ENABLE_EXTENDED_FLAGS must be ON when modifying QE; otherwise
        # the QE bit is ignored. So clear QE and set EXTENDED.
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        if new_mode == mode.value:
            return  # already where we want it
        if kernel32.SetConsoleMode(ctypes.c_void_p(handle), new_mode):
            logging.getLogger(__name__).info(
                "Console Quick Edit Mode disabled (operator clicks in "
                "the cmd window won't pause the manager).")
        else:
            logging.getLogger(__name__).debug(
                "console QE disable: SetConsoleMode failed -- skipping")
    except Exception:
        logging.getLogger(__name__).debug(
            "console QE disable raised (non-fatal)", exc_info=True)


def main():
    # Auto-install missing requirements BEFORE importing any manager
    # submodule that might pull a third-party dep. Covers the case
    # where a self-update added a new dep (Pillow, etc.) and the
    # manager restarted via exit-99 -- which skips the .bat's
    # `pip install` step.
    try:
        from manager.dependency_check import ensure_requirements
        ensure_requirements()
    except Exception as e:  # never block boot on the auto-installer
        sys.stderr.write(
            f"[deps] dependency check skipped: {type(e).__name__}: {e}\n")

    try:
        from manager import __version__
        from manager.app import create_app
        from manager.config import PROJECT_ROOT, SETTINGS_PATH, display_path, load_settings
        from manager.logging_setup import LOGS_DIR, configure_logging
    except ImportError as e:
        sys.stderr.write("\n" + "=" * 60 + "\n")
        sys.stderr.write("FATAL: an import failed before the manager could start.\n")
        sys.stderr.write("=" * 60 + "\n")
        sys.stderr.write(f"  {type(e).__name__}: {e}\n\n")
        sys.stderr.write("Most common cause: stale virtualenv missing a new dependency.\n")
        sys.stderr.write("Fix:  rmdir /s /q venv   then re-run start_manager.bat\n\n")
        sys.stderr.write("Full traceback:\n")
        traceback.print_exc()
        sys.exit(2)

    configure_logging()
    log = logging.getLogger(__name__)
    log.debug("All manager modules imported successfully.")

    # Disable Windows console "Quick Edit Mode" -- when the operator
    # clicks in the cmd window with QE on, the console output buffer
    # blocks until they press a key. Python's logging writes to both
    # file and console under one lock, so when console output blocks
    # EVERY log call (from every thread) blocks. Manager appears
    # frozen until the operator presses a key. We've hit this twice
    # (visible in manager.log as 30s+ gaps with zero activity from
    # any thread). Fixing once at startup so a stray click can't lock
    # the process up.
    _disable_console_quick_edit()

    # R9: register an atexit handler so a clean Ctrl+C / Flask shutdown
    # also leaves the shutdown_clean marker behind. atexit doesn't fire
    # on os._exit; the explicit os._exit(99) sites already write the
    # marker themselves before calling os._exit. Belt-and-suspenders.
    import atexit
    from manager import self_update as _su
    atexit.register(_su.write_shutdown_clean_marker)

    log.info("=" * 60)
    log.info("Soulmask Manager v%s starting", __version__)
    log.info("Project root: %s", PROJECT_ROOT)  # absolute is the right anchor here
    log.info("Settings file: %s", display_path(SETTINGS_PATH))
    log.info("Log directory: %s", display_path(LOGS_DIR))
    log.info("=" * 60)

    settings = load_settings()
    host = settings["network"]["bind_host"]
    port = settings["network"]["bind_port"]
    log.debug("Network: host=%s port=%d", host, port)

    # Show both raw setting and the resolved path so configuration mistakes are obvious.
    from manager.paths import install_dir as _install, steamcmd_exe as _steamcmd
    log.info("Setting paths.install_dir = %r  -> %s",
             settings["paths"]["install_dir"], display_path(_install()))
    log.info("Setting paths.steamcmd_exe = %r  -> %s",
             settings["paths"]["steamcmd_exe"], display_path(_steamcmd()))

    # Dump full settings at DEBUG (with secret keys redacted).
    from manager.config import redact_dict
    log.debug("Full settings (secrets redacted):")
    for section, body in settings.items():
        if isinstance(body, dict):
            log.debug("  [%s] %s", section, redact_dict(body))
        else:
            log.debug("  %s = %r", section, body)

    app = create_app()

    # Background services -- start AFTER app build so any import errors
    # surface before the web server claims its port.
    from manager import (background, backup_scheduler, backups,
                          discord_integration, player_tracker, self_update,
                          tailer, updates)
    # Generic worker queue MUST start before any module that submits to
    # it (discord_integration enqueues join-map work as soon as a
    # join event fires from tailer replay).
    background.start()
    tailer.setup_tailers()
    updates.start_poller()                # Steam build-id poller
    self_update.start_update_poller()     # git-commits-behind poller
    backup_scheduler.start_scheduler()
    # Discord relay + players_db journal. Must come AFTER tailer.setup_
    # tailers() since it subscribes to the EventStream that's created in
    # there. Idempotent if called more than once.
    discord_integration.start()
    # Live online-player tracker. Polls EchoPort `lp` every 30s while
    # any game-server instance is running; pushes snapshots to /map
    # subscribers via SSE. Safe to start unconditionally -- gates
    # itself on lifecycle.running_instances() each iteration.
    player_tracker.start_tracker()

    # Orphan-file reconciliation: scan data/backups/ for .gz files not
    # referenced by the index. Catches the rare case where the manager
    # crashed between gzip-write and index-append. Logged-only; never
    # deletes anything -- operator decides.
    try:
        backups.reconcile_orphans()
    except Exception:
        log.exception("orphan-reconcile raised at boot (non-fatal)")

    # Re-apply persisted login-lock state to any servers we adopted on
    # boot. _start_impl already does this after a user-clicked Start;
    # this covers the manager-restart case where servers were running
    # and we just attached to them.
    def _bootstrap_login_locks():
        # Small delay so adoption discovery has a moment to find
        # processes via psutil scan.
        import threading as _t
        _t.Event().wait(5)
        try:
            from manager import lifecycle, login_lock
            running = lifecycle.running_instances()
            if running:
                log.info("Boot: re-applying login-lock state to %d adopted "
                         "instance(s)", len(running))
                login_lock.apply_locks()
        except Exception:
            log.exception("boot login-lock apply raised (non-fatal)")
    import threading as _threading
    _threading.Thread(target=_bootstrap_login_locks, daemon=True,
                      name="boot-login-lock").start()

    # R1: Stable-boot signal for the bootloader. After 60s of uptime
    # without crashing, clear `data/.boot_in_progress` (so bootloader
    # doesn't count this run as a fast crash on next launch) and
    # record the current SHA as last-known-good (the rollback target).
    def _mark_boot_stable():
        import threading as _t
        _t.Event().wait(60)
        try:
            from manager import self_update
            from manager.config import DATA_DIR as _DATA
            marker = _DATA / ".boot_in_progress"
            if marker.exists():
                marker.unlink()
            sha = self_update.current_sha()
            if sha:
                (_DATA / "last_known_good.txt").write_text(
                    sha + "\n", encoding="utf-8")
                log.info("Stable boot reached; recorded last_known_good=%s",
                         sha[:12])
            else:
                log.warning("Stable boot reached but current_sha() returned "
                            "None -- last_known_good NOT updated")
        except Exception:
            log.exception("mark_boot_stable raised (non-fatal)")
    _threading.Thread(target=_mark_boot_stable, daemon=True,
                      name="mark-boot-stable").start()

    log.info("Listening on http://%s:%d  (Ctrl+C to stop)", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
