"""Backup management routes -- /backups page, manual run, pin/delete.

Restore lives here too once step 5 lands. For now this is the read +
manual-trigger surface so the operator can validate the engine end-to-end
against the live cluster.
"""

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, redirect, render_template, request, url_for

from manager import backups, dashboard_events, lifecycle
from manager import updates as updates_module
from manager.config import PROJECT_ROOT

backups_bp = Blueprint("backups", __name__, url_prefix="/backups")
log = logging.getLogger(__name__)

# SSE wire constants -- match /logs and /updates patterns.
_SSE_INITIAL_PADDING = (":" + (" " * 2048) + "\n\n").encode("utf-8")
_SSE_RETRY_FRAME = b"retry: 5000\n\n"
_SSE_KEEPALIVE = b": keepalive\n\n"
_SSE_KEEPALIVE_INTERVAL_SEC = 25.0


def _format_size(n_bytes: int) -> str:
    """Human-friendly size string."""
    if n_bytes < 1024:
        return f"{n_bytes} B"
    if n_bytes < 1024 ** 2:
        return f"{n_bytes / 1024:.1f} KB"
    if n_bytes < 1024 ** 3:
        return f"{n_bytes / 1024 ** 2:.1f} MB"
    return f"{n_bytes / 1024 ** 3:.2f} GB"


def _format_age(ts: str) -> str:
    """Friendly 'N min ago' / 'N hr ago' from the YYYY-MM-DD_HHMMSS ts string."""
    try:
        when = datetime.strptime(ts, "%Y-%m-%d_%H%M%S")
    except ValueError:
        return "?"
    delta = datetime.now() - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m ago"
    days = secs // 86400
    return f"{days}d {(secs % 86400) // 3600}h ago"


def _grouped_snapshots() -> list[dict]:
    """Snapshots grouped by ts -> {ts, age, source, build_id, partial,
    legs:[...]}. Newest first. legs[i] = original index entry plus
    `size_human` and `pinned` flags surfaced for the template."""
    raw = backups.list_snapshots()
    by_ts: OrderedDict[str, dict] = OrderedDict()
    for entry in raw:
        ts = entry.get("ts", "?")
        if ts not in by_ts:
            by_ts[ts] = {
                "ts": ts,
                "age": _format_age(ts),
                "source": entry.get("source", "?"),
                "build_id": entry.get("build_id"),
                "partial": entry.get("partial", False),
                "config_bundle": entry.get("config_bundle"),
                "legs": [],
                "any_pinned": False,
                "total_size": 0,
                "restored_at": None,
            }
        bucket = by_ts[ts]
        size = entry.get("size_bytes", 0)
        leg = {
            **entry,
            "size_human": _format_size(size),
        }
        bucket["legs"].append(leg)
        bucket["total_size"] += size
        if entry.get("pinned"):
            bucket["any_pinned"] = True
        # Promote partial/build_id if any leg has it set; should be uniform
        # but the index isn't strictly invariant.
        if entry.get("partial"):
            bucket["partial"] = True
        if entry.get("build_id") and not bucket["build_id"]:
            bucket["build_id"] = entry["build_id"]
        # Pick the most recent restored_at across all legs (they SHOULD
        # all share the same value when set, but pick max defensively).
        rat = entry.get("restored_at")
        if rat and (bucket["restored_at"] is None
                    or rat > bucket["restored_at"]):
            bucket["restored_at"] = rat

    out = list(by_ts.values())
    for s in out:
        s["total_size_human"] = _format_size(s["total_size"])
    return out


# ── Routes ──────────────────────────────────────────────────────────────────


@backups_bp.route("/")
def index():
    log.debug("backups.index: rendering")
    snapshots = _grouped_snapshots()
    op = backups.current_op_status()
    running = lifecycle.running_instances()
    return render_template(
        "backups.html",
        snapshots=snapshots,
        op=op,
        running_count=len(running),
        running_names=[ri.instance.name for ri, _ in running],
    )


@backups_bp.route("/sse")
def sse_op_state():
    """Push backup op-state changes via SSE so the page can refresh
    its DOM without browser-side polling.

    Frame format: `event: op` with JSON body matching
    backups.current_op_status() (in_progress, label, elapsed_seconds,
    progress). The frontend triggers a full page reload when the
    in_progress flag transitions, since op begin/end can change which
    snapshots are listed (a finished snapshot needs to appear in the
    table). For mid-op progress changes (label/progress/elapsed) the
    frontend updates DOM in place without reloading."""
    notify = dashboard_events.subscribe()
    log.debug("SSE /backups/sse subscriber from %s "
              "(dashboard subs now %d)",
              request.remote_addr, dashboard_events.subscriber_count())

    def _frame() -> bytes:
        snap = backups.current_op_status()
        body = json.dumps(snap, ensure_ascii=False)
        return f"event: op\ndata: {body}\n\n".encode("utf-8")

    def generate():
        try:
            yield _SSE_RETRY_FRAME
            yield _SSE_INITIAL_PADDING
            # Initial frame so the page can immediately reflect current
            # state on (re)connect.
            yield _frame()

            last_keepalive = time.monotonic()
            while True:
                woke = notify.wait(timeout=_SSE_KEEPALIVE_INTERVAL_SEC)
                notify.clear()
                if woke:
                    yield _frame()
                    last_keepalive = time.monotonic()
                if time.monotonic() - last_keepalive >= _SSE_KEEPALIVE_INTERVAL_SEC:
                    yield _SSE_KEEPALIVE
                    last_keepalive = time.monotonic()
        except GeneratorExit:
            log.debug("SSE /backups/sse client disconnected")
        finally:
            dashboard_events.unsubscribe(notify)

    response = Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    response.direct_passthrough = True
    return response


@backups_bp.route("/run", methods=["POST"])
def run_now():
    """Kick off a manual cluster snapshot. Runs in a daemon thread so the
    request returns immediately; the page polls for state."""
    running = lifecycle.running_instances()
    if not running:
        log.warning("backups.run_now: no running instances -- nothing to back up")
        return redirect(url_for("backups.index"))

    log.info("Manual backup request from %s for %d instance(s)",
             request.remote_addr, len(running))

    if not backups.try_begin_op("manual snapshot"):
        log.warning("backups.run_now: refused -- another backup op in progress")
        return redirect(url_for("backups.index"))

    threading.Thread(
        target=backups.run_snapshot_under_op,
        args=(running, "manual"),
        daemon=True, name="manual-snapshot",
    ).start()
    return redirect(url_for("backups.index"))


@backups_bp.route("/<ts>/<instance>/pin", methods=["POST"])
def pin(ts: str, instance: str):
    pinned = request.form.get("pinned", "1") == "1"
    log.info("Pin toggle from %s: %s/%s -> %s",
             request.remote_addr, ts, instance, pinned)
    backups.set_pinned(ts, instance, pinned)
    return redirect(url_for("backups.index"))


@backups_bp.route("/<ts>/<instance>/delete", methods=["POST"])
def delete(ts: str, instance: str):
    log.info("Delete leg request from %s: %s/%s",
             request.remote_addr, ts, instance)
    if not backups.delete_snapshot_leg(ts, instance):
        log.warning("delete: failed (entry missing or pinned)")
    return redirect(url_for("backups.index"))


@backups_bp.route("/<ts>/delete", methods=["POST"])
def delete_snapshot(ts: str):
    """Delete every leg of one cluster snapshot. Pinned legs survive
    (operator must unpin first to delete the whole thing)."""
    log.info("Delete cluster snapshot request from %s: %s",
             request.remote_addr, ts)
    deleted, skipped = backups.delete_snapshot_all_legs(ts)
    log.info("  deleted=%d, skipped (pinned)=%d", deleted, skipped)
    return redirect(url_for("backups.index"))


@backups_bp.route("/clear-all", methods=["POST"])
def clear_all():
    """Per-instance: keep only the newest unpinned leg + all pinned legs.
    Used to trim a long history quickly."""
    log.info("Clear-all request from %s", request.remote_addr)
    n = backups.delete_all_except_latest()
    log.info("  cleared %d leg(s)", n)
    return redirect(url_for("backups.index"))


@backups_bp.route("/<ts>/restore-prepare")
def restore_prepare(ts: str):
    """Render the confirmation form for a restore."""
    legs = backups.get_snapshot_legs(ts)
    if not legs:
        log.warning("restore_prepare: snapshot %s not found", ts)
        return redirect(url_for("backups.index"))

    # Cross-build warning if any leg has a different build than current.
    current_build = updates_module.read_local_buildid()
    snap_build = next((l.get("build_id") for l in legs if l.get("build_id")),
                      None)
    cross_build_warning = (snap_build is not None
                           and current_build is not None
                           and snap_build != current_build)

    return render_template(
        "backup_restore.html",
        ts=ts,
        legs=legs,
        snap_build=snap_build,
        current_build=current_build,
        cross_build_warning=cross_build_warning,
        has_config_bundle=any(l.get("config_bundle") for l in legs),
    )


@backups_bp.route("/<ts>/restore", methods=["POST"])
def restore(ts: str):
    """Kick off the restore in a background thread."""
    selected = request.form.getlist("instances")
    include_config = request.form.get("include_manager_config") == "on"

    if not selected:
        log.warning("restore: no instances selected for %s", ts)
        return redirect(url_for("backups.restore_prepare", ts=ts))

    log.info("Restore request from %s for ts=%s instances=%s include_config=%s",
             request.remote_addr, ts, selected, include_config)

    # Op-lock semantics:
    # - backups._op_lock gates "no concurrent snapshot can run".
    # - lifecycle._op_lock gates "no concurrent start/stop/update".
    # restore_snapshot acquires lifecycle's lock for the duration; we
    # acquire backups' lock here AND hand it to the worker thread (no
    # release in between) so a manual snapshot click can't race in
    # during the gap.
    if not backups.try_begin_op(f"restore {ts}"):
        log.warning("restore: refused -- another backup op in progress")
        return redirect(url_for("backups.index"))

    def _worker():
        # try_begin_op is held; release in the finally regardless of
        # how restore_snapshot terminates.
        try:
            backups.restore_snapshot(ts, selected, include_config)
        except Exception:
            log.exception("restore worker raised")
        finally:
            backups._end_op()

    threading.Thread(target=_worker, daemon=True,
                     name=f"restore-{ts}").start()
    return redirect(url_for("backups.index"))


@backups_bp.route("/download/<ts>/<instance>")
def download(ts: str, instance: str):
    """Download a snapshot's gzipped DB file. Useful for off-server
    archival by the operator."""
    from flask import send_file
    legs = backups.get_snapshot_legs(ts)
    target = next((l for l in legs if l.get("instance") == instance), None)
    if target is None or not target.get("file"):
        log.warning("download: no entry for %s/%s", ts, instance)
        return ("not found", 404)
    abs_path = (PROJECT_ROOT / target["file"]).resolve()
    if not abs_path.exists():
        log.warning("download: file vanished from disk: %s", abs_path)
        return ("file missing on disk", 410)
    log.info("download: %s/%s (%s)", ts, instance, abs_path)
    return send_file(abs_path, as_attachment=True,
                     download_name=Path(target["file"]).name)
