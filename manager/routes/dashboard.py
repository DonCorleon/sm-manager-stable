"""Dashboard routes -- live server status, start/stop, SSE stream.

The dashboard uses Server-Sent Events for live status updates. The wrapper
div on `dashboard.html` is wired to `/api/server/status/stream`; the
generator below pushes a freshly-rendered partial on every refresh tick
AND immediately when other code calls `dashboard_events.notify()`
(start/stop/lock toggle/backup op begin/end).

The legacy `/api/server/status` endpoint is kept for tools or HTMX
fallback, but no client uses it by default.
"""

import logging
import threading
import time

from flask import (Blueprint, Response, redirect, render_template,
                   request, url_for)

from manager import dashboard_events, lifecycle, updates
from manager.checks import run_all_checks
from manager.config import get_setting, load_settings

dashboard_bp = Blueprint("dashboard", __name__)
log = logging.getLogger(__name__)

# Min/max SSE timer interval. Min keeps the event loop responsive for live
# CPU/RAM ticks; max bounds keepalive cadence for proxies/buffers.
_MIN_REFRESH_SEC = 1
_MAX_REFRESH_SEC = 60

# Initial padding to bust Werkzeug's response buffer on first connect
# (same trick as the logs SSE -- without it the browser stays in the
# "connecting" state until enough bytes accumulate).
_INITIAL_PADDING = (":" + (" " * 2048) + "\n\n").encode("utf-8")
_RETRY_FRAME = b"retry: 5000\n\n"


def _can_show_status() -> bool:
    """Status panel only makes sense once setup is complete."""
    settings = load_settings()
    if "server" not in settings:
        return False
    checks = run_all_checks()
    return all(c.passed for c in checks)


def _build_status_context() -> dict:
    """Combined snapshot the dashboard renders from. Pulls server
    lifecycle state, Steam update state, and manager-update state in
    one place so the template stays simple."""
    from manager import self_update
    return {
        "status": lifecycle.get_status(),
        "updates": updates.get_state(),
        "manager_update": self_update.get_poll_state(),
    }


@dashboard_bp.route("/")
def index():
    refresh_sec = int(get_setting("ui.dashboard_refresh_sec", 5))
    if _can_show_status():
        log.debug("dashboard.index: setup complete, embedding live status panel")
        ctx = _build_status_context()
        return render_template("dashboard.html", setup_complete=True,
                               refresh_sec=refresh_sec, **ctx)
    log.debug("dashboard.index: setup incomplete, showing CTA")
    return render_template("dashboard.html", setup_complete=False,
                           refresh_sec=refresh_sec, status=None, updates=None)


@dashboard_bp.route("/api/server/status")
def api_status():
    """Legacy HTMX polling endpoint. Kept as a backup; the dashboard
    uses the SSE stream below by default."""
    ctx = _build_status_context()
    return render_template("_dashboard_status.html", **ctx)


@dashboard_bp.route("/api/server/status/stream")
def api_status_stream():
    """SSE: pushes the rendered status partial on every refresh tick AND
    on any call to `dashboard_events.notify()`. One persistent connection
    per open dashboard tab; replaces the prior 5-sec polling."""
    refresh_sec = max(_MIN_REFRESH_SEC,
                      min(_MAX_REFRESH_SEC,
                          int(get_setting("ui.dashboard_refresh_sec", 5))))
    log.info("SSE dashboard subscriber from %s (refresh=%ds, total=%d)",
             request.remote_addr, refresh_sec,
             dashboard_events.subscriber_count() + 1)

    # Capture the app for context-pushing inside the generator.
    from flask import current_app
    app = current_app._get_current_object()  # type: ignore[attr-defined]

    notify_event = dashboard_events.subscribe()

    def _render_frame() -> bytes:
        # Need a request context (not just app context) because the
        # partial uses `url_for(...)` which requires the request stack
        # for endpoint resolution. test_request_context() pushes both
        # an app and a request context for the duration of the `with`.
        with app.test_request_context():
            ctx = _build_status_context()
            html = render_template("_dashboard_status.html", **ctx)
        # Single SSE event named "status". The wrapper div listens for
        # this event and swaps innerHTML on each frame.
        # Each line of HTML must be prefixed with "data: " in SSE; we
        # collapse newlines so the swap target gets the full block as
        # one logical message. HTMX handles whitespace fine.
        flat = html.replace("\r", "").replace("\n", "")
        return f"event: status\ndata: {flat}\n\n".encode("utf-8")

    def generate():
        try:
            yield _RETRY_FRAME
            yield _INITIAL_PADDING
            # Initial render so the page paints immediately.
            yield _render_frame()
            log.verbose("SSE dashboard: initial frame sent")

            while True:
                # Wait for either timer expiry OR a notify() push.
                woke_early = notify_event.wait(timeout=refresh_sec)
                notify_event.clear()
                log.verbose("SSE dashboard: tick (woke_early=%s)", woke_early)
                yield _render_frame()
                if woke_early:
                    log.debug("SSE dashboard: pushed early (notify)")
        except GeneratorExit:
            log.debug("SSE dashboard: client disconnected")
        except Exception:
            # Defensive: any unexpected error in the generator must NOT
            # bubble up untraced -- it would manifest as a generic
            # 500 on the client with no useful context in the log.
            log.exception("SSE dashboard generator raised; closing stream")
        finally:
            dashboard_events.unsubscribe(notify_event)
            log.debug("SSE dashboard: unsubscribed (now %d)",
                      dashboard_events.subscriber_count())

    response = Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    response.direct_passthrough = True
    return response


@dashboard_bp.route("/server/start", methods=["POST"])
def server_start():
    """Start servers. The `target` form value selects what to start:
      - "all" (default, omitted = all): start every instance.
      - <map_name>: start that one instance only (cluster mode use).
    Validates the map_name against known instances so a bogus form
    value can't smuggle arbitrary input into lifecycle.start_one."""
    target = (request.form.get("target") or "all").strip()
    if target == "all":
        log.info("Start request (all) from %s", request.remote_addr)
        started = lifecycle.start_all()
        if not started:
            log.warning("  start_all refused (op already in progress)")
        return redirect(url_for("dashboard.index"))

    # Per-instance start. Validate map_name against the known set so a
    # bogus `target=<garbage>` from a hand-crafted POST is rejected.
    from manager.world_db import KNOWN_LEVELS
    if target not in KNOWN_LEVELS:
        log.warning("Start request from %s: rejected unknown target %r",
                    request.remote_addr, target)
        return redirect(url_for("dashboard.index"))

    log.info("Start request (target=%s) from %s",
             target, request.remote_addr)
    started = lifecycle.start_one(target)
    if not started:
        log.warning("  start_one(%s) refused (op already in progress "
                    "or unknown map)", target)
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/server/stop", methods=["POST"])
def server_stop():
    countdown_raw = request.form.get("countdown_sec", "30").strip()
    try:
        countdown = int(countdown_raw)
    except ValueError:
        log.warning("Stop request: bad countdown_sec=%r, defaulting to 30",
                    countdown_raw)
        countdown = 30
    log.info("Stop request from %s (countdown=%ds)", request.remote_addr, countdown)
    stopped = lifecycle.stop_all(countdown_sec=countdown)
    if not stopped:
        log.warning("  stop_all refused (op already in progress)")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/server/update-restart", methods=["POST"])
def server_update_restart():
    countdown_raw = request.form.get("countdown_sec", "30").strip()
    try:
        countdown = int(countdown_raw)
    except ValueError:
        countdown = 30
    log.info("Update+Restart request from %s (stop countdown=%ds)",
             request.remote_addr, countdown)
    ok = lifecycle.update_and_restart(countdown_sec=countdown)
    if not ok:
        log.warning("  update_and_restart refused (op already in progress)")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/server/cancel-shutdown", methods=["POST"])
def server_cancel_shutdown():
    log.info("Cancel-shutdown request from %s", request.remote_addr)
    result = lifecycle.cancel_stop()
    if not result["any"]:
        log.warning("  cancel: no instance was in countdown")
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/server/login-lock/cluster", methods=["POST"])
def login_lock_cluster():
    from manager import login_lock
    locked = request.form.get("locked", "0") == "1"
    actor = request.remote_addr or "?"
    log.info("Cluster login-lock toggle from %s -> %s", actor, locked)
    login_lock.set_cluster_lock(locked, actor_ip=actor)
    threading.Thread(target=login_lock.apply_locks,
                     daemon=True, name="apply-locks-cluster").start()
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/server/login-lock/instance/<short>", methods=["POST"])
def login_lock_instance(short: str):
    from manager import login_lock
    locked = request.form.get("locked", "0") == "1"
    actor = request.remote_addr or "?"
    log.info("Per-instance login-lock toggle from %s: %s -> %s",
             actor, short, locked)
    login_lock.set_instance_lock(short, locked, actor_ip=actor)
    threading.Thread(target=login_lock.apply_locks,
                     daemon=True, name="apply-locks-instance").start()
    return redirect(url_for("dashboard.index"))


@dashboard_bp.route("/api/updates/check-now", methods=["POST"])
def updates_check_now():
    """Trigger an immediate (synchronous) update check. Useful for the
    'Check now' button next to the Update info."""
    log.info("Manual update check from %s", request.remote_addr)
    threading.Thread(target=updates.check_now, daemon=True,
                     name="manual-update-check").start()
    return redirect(url_for("dashboard.index"))


