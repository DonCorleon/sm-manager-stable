"""Login-lock engine.

State persisted in settings.toml under `[runtime]`:
    login_lock                = false        # cluster-wide override
    login_lock_per_instance   = { ... }      # per-instance map -> bool

Effective lock for an instance = cluster OR per-instance. Cluster lock
on top wins regardless of per-instance state; cluster lock off lets
per-instance state through.

Live enforcement uses EchoPort `sl 0` (lock) / `sl 1` (unlock). The
server defaults to unlocked (sl 1) on every start, so we always re-apply
on adoption / startup / restart. EchoPort calls are cheap and idempotent.

Two transient cases NOT reflected in persisted state:
- Auto-engage during shutdown countdown: fire `sl 0` directly, don't touch
  persisted state. The server is dying anyway; lock dies with it.
- Cancel-shutdown unlock: re-apply persisted state via apply_locks().
"""

import logging
from typing import Optional

from manager import echo
from manager.config import get_setting, set_setting
from manager.wizard import RuntimeInstance

# Module-level convenience: surfaces in the logger format as "manager.login_lock".

log = logging.getLogger(__name__)


# ── Persisted state accessors ───────────────────────────────────────────────


def is_cluster_locked() -> bool:
    return bool(get_setting("runtime.login_lock", False))


def is_instance_locked(short: str) -> bool:
    """Per-instance lock state. `short` matches backups.instance_short()."""
    table = get_setting("runtime.login_lock_per_instance", {}) or {}
    if not isinstance(table, dict):
        return False
    return bool(table.get(short, False))


def effective_lock(short: str) -> bool:
    return is_cluster_locked() or is_instance_locked(short)


def set_cluster_lock(locked: bool, actor_ip: Optional[str] = None) -> None:
    set_setting("runtime.login_lock", bool(locked))
    log.info("Cluster login-lock set to %s by %s",
             locked, actor_ip or "(unknown)")
    _notify_dashboard()


def set_instance_lock(short: str, locked: bool,
                      actor_ip: Optional[str] = None) -> None:
    table = get_setting("runtime.login_lock_per_instance", {}) or {}
    if not isinstance(table, dict):
        table = {}
    table[short] = bool(locked)
    set_setting("runtime.login_lock_per_instance", table)
    log.info("Per-instance login-lock for %s set to %s by %s",
             short, locked, actor_ip or "(unknown)")
    _notify_dashboard()


def _notify_dashboard() -> None:
    try:
        from manager import dashboard_events
        dashboard_events.notify()
    except Exception:
        log.exception("dashboard notify raised (non-fatal)")


# ── EchoPort enforcement ────────────────────────────────────────────────────


def _send_sl(ri: RuntimeInstance, locked: bool) -> bool:
    """Send `sl 0` (lock) or `sl 1` (unlock) to one instance. Returns
    True on a successful EchoPort round-trip, False otherwise. Failure
    is logged but never raised."""
    cmd = "sl 0" if locked else "sl 1"
    log.verbose("[%s] _send_sl: dispatching '%s' to 127.0.0.1:%d",
                ri.instance.name, cmd, ri.instance.echo_port)
    try:
        resp = echo.send_command("127.0.0.1", ri.instance.echo_port, cmd)
        cleaned = resp.replace("\r", " ").replace("\n", " ")[:200].strip()
        log.info("[%s] %s -> %s", ri.instance.name, cmd, cleaned or "(empty)")
        return True
    except (OSError, ConnectionError) as e:
        log.warning("[%s] EchoPort unreachable, could not send %s: %s",
                    ri.instance.name, cmd, e)
        return False
    except Exception as e:
        # Defensive: anything else from the echo layer (shouldn't happen,
        # but we don't want a single instance's failure to abort the
        # cluster-wide apply_locks() loop).
        log.error("[%s] unexpected error sending %s: %s",
                  ri.instance.name, cmd, e)
        return False


def apply_locks() -> dict:
    """Bring live login-lock state in line with persisted state on every
    currently-running instance. Returns {short: bool_success}.

    Safe to call repeatedly (idempotent at the protocol level). Called
    on manager startup, after every successful start, and after any
    toggle from the UI.
    """
    # Local import to avoid circular dep (lifecycle imports login_lock for
    # the auto-engage during shutdown).
    from manager import backups, lifecycle

    running = lifecycle.running_instances()
    if not running:
        log.debug("apply_locks: no running instances; nothing to do")
        return {}
    out: dict[str, bool] = {}
    for ri, _proc in running:
        short = backups.instance_short(ri)
        wanted = effective_lock(short)
        ok = _send_sl(ri, wanted)
        out[short] = ok
        log.debug("apply_locks: %s -> %s (ok=%s)", short, wanted, ok)
    return out


def force_lock_for_shutdown(ri: RuntimeInstance) -> bool:
    """Fire `sl 0` on one instance regardless of persisted state. Used by
    lifecycle when a shutdown countdown begins -- we want NEW logins
    blocked during the warning window even if the operator hasn't toggled
    the lock setting on. Persisted state is NOT mutated."""
    log.info("[%s] forcing sl 0 (shutdown auto-lock)", ri.instance.name)
    return _send_sl(ri, locked=True)


def is_any_running_locked() -> bool:
    """True if at least one running instance is currently lock-effective.
    Used by the dashboard to show the global 'logins locked' pill."""
    from manager import backups, lifecycle
    for ri, _ in lifecycle.running_instances():
        if effective_lock(backups.instance_short(ri)):
            return True
    return False
