"""Updates page routes -- /updates page covering BOTH Steam (game)
updates and Manager (git) updates in one place."""

import json
import logging
import threading
import time

from flask import Blueprint, Response, redirect, render_template, request, url_for

from manager import self_update, lifecycle, updates as steam_updates, updates_log

self_update_bp = Blueprint("self_update", __name__, url_prefix="/updates")
log = logging.getLogger(__name__)

# SSE wire constants (mirrors /logs/sse).
_SSE_INITIAL_PADDING = (":" + (" " * 2048) + "\n\n").encode("utf-8")
_SSE_RETRY_FRAME = b"retry: 5000\n\n"
_SSE_KEEPALIVE = b": keepalive\n\n"
_SSE_KEEPALIVE_INTERVAL_SEC = 25.0


@self_update_bp.route("/", methods=["GET"])
def index():
    """Render the unified updates page. Auto-configures `origin` if
    it's missing -- the operator never has to type the URL."""
    log.debug("updates.index: render")
    if self_update._resolve_binary("git"):
        try:
            self_update.ensure_remote()
        except Exception:
            log.exception("ensure_remote raised on page render")
    remote_url = self_update.get_remote_url() or self_update.MANAGER_REMOTE_URL
    return render_template(
        "updates.html",
        status=self_update.get_status(),
        steam=steam_updates.get_state(),
        server_status=lifecycle.get_status(),
        breaker=self_update.breaker_status(),
        git_resolved=self_update._resolve_binary("git"),
        manager_remote=remote_url,
        manager_remote_keys_url=self_update.github_keys_url(remote_url),
        auth_required=self_update.remote_needs_auth(remote_url),
        is_git_checkout=self_update.is_git_checkout(),
        flash_msg=request.args.get("msg"),
        flash_kind=request.args.get("kind"),
    )


def _redirect_with_flash(msg: str, kind: str = "info"):
    """Bounce back to the /updates page with a flash banner. kind is
    'info' / 'ok' / 'error' for styling. Used by Apply / Discard /
    Force-sync / Check / Steam ops -- everything whose UI lives on
    /updates."""
    return redirect(url_for("self_update.index", msg=msg, kind=kind))


def _redirect_to_settings_auth(msg: str, kind: str = "info"):
    """Bounce back to the Manager-update-detection tab on /settings
    with a git_msg banner. Used by generate-key / test-connection,
    whose UI moved to /settings 2026-05-02."""
    return redirect(url_for(
        "settings.index",
        tab="Manager update detection",
        git_msg=msg,
        git_ok=("1" if kind == "ok" else "0"),
    ))


# ── Manager (git) actions ──────────────────────────────────────────────────


@self_update_bp.route("/generate-key", methods=["POST"])
def generate_key():
    overwrite = request.form.get("overwrite") == "1"
    log.info("self_update.generate_key from %s (overwrite=%s)",
             request.remote_addr, overwrite)
    ok, message = self_update.generate_deploy_key(overwrite=overwrite)
    if ok:
        return _redirect_to_settings_auth(
            "Deploy key generated. Copy the public key below into the "
            "repo's Deploy keys page, then click Test connection.",
            "ok",
        )
    return _redirect_to_settings_auth(f"Generate failed: {message}", "error")


@self_update_bp.route("/test-connection", methods=["POST"])
def test_connection():
    log.info("self_update.test_connection from %s", request.remote_addr)
    if not self_update.start_test_connection():
        return _redirect_to_settings_auth(
            "Another update operation is in progress (manual click or "
            "auto-update poller). Try again in a few seconds.", "error")
    return _redirect_to_settings_auth(
        "Test-connection started -- watch the activity log on /updates.",
        "ok")


@self_update_bp.route("/check", methods=["POST"])
def check():
    """Manual check kicks off on the background worker. Result lands in
    the activity log. Shares state with the periodic poller so the
    dashboard card and /updates page always agree on what's been fetched."""
    log.info("self_update.check from %s", request.remote_addr)
    if not self_update.start_check():
        return _redirect_with_flash(
            "Another update operation is in progress (manual click or "
            "auto-update poller). Try again in a few seconds.", "error")
    return _redirect_with_flash(
        "Check started -- watch the activity log below.", "ok")


@self_update_bp.route("/discard", methods=["POST"])
def discard():
    """Revert tracked-file edits via `git reset --hard HEAD`. Untracked
    files (data/, logs/, portable/, steamcmd/) are preserved."""
    log.warning("self_update.discard from %s", request.remote_addr)
    if not self_update.start_discard():
        return _redirect_with_flash(
            "Another update operation is in progress (manual click or "
            "auto-update poller). Try again in a few seconds.", "error")
    return _redirect_with_flash(
        "Discard started -- watch the activity log below.", "ok")


@self_update_bp.route("/force-sync", methods=["POST"])
def force_sync():
    """Snap working tree to origin/main: fetch + `git reset --hard
    origin/main`. Drops local commits AND uncommitted edits. Untracked
    files preserved. Manager restart auto-fires on success."""
    log.warning("self_update.force_sync from %s", request.remote_addr)
    if not self_update.start_force_sync():
        return _redirect_with_flash(
            "Another update operation is in progress (manual click or "
            "auto-update poller). Try again in a few seconds.", "error")
    return _redirect_with_flash(
        "Force-sync started -- watch the activity log below. The manager "
        "will restart automatically when it succeeds.", "ok")


@self_update_bp.route("/apply", methods=["POST"])
def apply():
    """Kick off apply-update on the background worker. The request thread
    returns immediately so the page renders without blocking; operator
    watches git fetch + py_compile + git pull stream into the activity
    log. Manager restart auto-fires on success."""
    log.info("self_update.apply from %s", request.remote_addr)
    if not self_update.start_apply():
        return _redirect_with_flash(
            "Another update operation is in progress (manual click or "
            "auto-update poller). Try again in a few seconds.", "error")
    return _redirect_with_flash(
        "Apply started -- watch the activity log below. The manager "
        "will restart automatically when it succeeds.", "ok")


@self_update_bp.route("/reset-breaker", methods=["POST"])
def reset_breaker():
    """Manually clear the circuit-breaker state. Re-enables Apply after
    the operator has investigated whatever was causing failures."""
    log.warning("self_update.reset_breaker from %s", request.remote_addr)
    self_update.reset_breaker()
    return _redirect_with_flash(
        "Circuit breaker reset. Apply is re-enabled.", "ok",
    )


# ── Steam (game) actions ───────────────────────────────────────────────────


@self_update_bp.route("/check-steam", methods=["POST"])
def check_steam():
    """Kick off a Steam buildid check on the background worker. The
    request thread returns immediately; SteamCMD output streams into
    the activity log below. Shares state with the periodic poller so
    the dashboard card and /updates page agree on what's been fetched."""
    log.info("updates.check_steam from %s", request.remote_addr)
    if not steam_updates.start_check_steam():
        return _redirect_with_flash(
            "Another update operation is in progress (manual click or "
            "auto-update poller). Try again in a few seconds.", "error")
    return _redirect_with_flash(
        "Steam check started -- watch the activity log below.", "ok")


@self_update_bp.route("/app-update", methods=["POST"])
def app_update():
    """Manual SteamCMD app_update without `validate` (manifest diff
    only). The fast path: brings install up to current Steam manifest
    by downloading just changed files. Refuses if any server is
    running -- the install dir is locked while WSServer.exe is up."""
    log.info("updates.app_update from %s", request.remote_addr)
    ok, reason = lifecycle.start_app_update(validate=False)
    if not ok:
        return _redirect_with_flash(reason, "error")
    return _redirect_with_flash(
        reason + " -- watch the activity log below.", "ok")


@self_update_bp.route("/app-verify", methods=["POST"])
def app_verify():
    """Manual SteamCMD app_update WITH `validate`. Hashes every
    installed file against the Steam manifest and re-downloads
    anything corrupted or missing. The slow path -- use when you
    suspect install corruption."""
    log.info("updates.app_verify from %s", request.remote_addr)
    ok, reason = lifecycle.start_app_update(validate=True)
    if not ok:
        return _redirect_with_flash(reason, "error")
    return _redirect_with_flash(
        reason + " -- watch the activity log below.", "ok")


@self_update_bp.route("/update-restart", methods=["POST"])
def update_restart():
    """Trigger Update + Restart on the game server(s). Same engine as
    the dashboard's button. Lands back on /updates."""
    countdown_raw = request.form.get("countdown_sec", "30").strip()
    try:
        countdown = int(countdown_raw)
    except ValueError:
        countdown = 30
    log.info("updates.update_restart from %s (stop countdown=%ds)",
             request.remote_addr, countdown)
    ok = lifecycle.update_and_restart(countdown_sec=countdown)
    if not ok:
        return _redirect_with_flash(
            "Update + Restart refused -- another op is already in progress.",
            "error",
        )
    return _redirect_with_flash(
        f"Update + Restart started ({countdown}s in-game warning, "
        "then SteamCMD app_update, then start). Watch the dashboard "
        "for progress.",
        "ok",
    )


# ── SSE: live update-activity log ──────────────────────────────────────────


@self_update_bp.route("/sse")
def sse_updates():
    """SSE stream of the unified manager+steam update activity log.

    Frame format: `event: line` with JSON payload `{seq, ts, source, line}`.
    On connect, the most recent ~500 backlog lines are sent so a refresh
    or late join sees recent context. Then live lines stream as they
    land. Keepalive comments every 25 sec keep the connection alive
    behind any reverse proxy.

    `event: op` frames carry op-state transitions (idle -> running ->
    done/failed) so the frontend can update its "Apply in progress..."
    banner without a separate poll."""
    notify = updates_log.subscribe()
    last_seq_seen = 0
    log.debug("SSE /updates/sse subscriber from %s (subs now %d)",
              request.remote_addr, updates_log.subscriber_count())

    def _format_line(entry: tuple) -> bytes:
        seq, ts, source, line = entry
        body = {"seq": seq, "ts": ts, "source": source, "line": line}
        return f"event: line\ndata: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8")

    def _format_op(state: dict) -> bytes:
        body = {
            "phase": state.get("phase", "idle"),
            "name": state.get("name", ""),
            "source": state.get("source", ""),
            "started_at": state.get("started_at", 0),
            "finished_at": state.get("finished_at", 0),
            "error": state.get("error", ""),
        }
        return f"event: op\ndata: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8")

    def generate():
        nonlocal last_seq_seen
        try:
            yield _SSE_RETRY_FRAME
            yield _SSE_INITIAL_PADDING
            # Send current op-state up front so the page banner is
            # accurate the moment SSE connects.
            yield _format_op(updates_log.get_op_state())
            # Backfill recent lines so a refresh shows continuity.
            backlog = updates_log.snapshot(since_seq=0, max_count=500)
            for entry in backlog:
                yield _format_line(entry)
                if entry[0] > last_seq_seen:
                    last_seq_seen = entry[0]

            last_keepalive = time.monotonic()
            last_op_phase = updates_log.get_op_state().get("phase", "idle")
            while True:
                woke = notify.wait(timeout=_SSE_KEEPALIVE_INTERVAL_SEC)
                notify.clear()
                if woke:
                    # Drain everything new since our last seq.
                    new_entries = updates_log.snapshot(
                        since_seq=last_seq_seen, max_count=500)
                    for entry in new_entries:
                        yield _format_line(entry)
                        if entry[0] > last_seq_seen:
                            last_seq_seen = entry[0]
                    # Op-state may have changed (begin_op / end_op);
                    # only push if the phase transitioned to avoid
                    # spamming op frames on every line.
                    cur = updates_log.get_op_state()
                    if cur.get("phase") != last_op_phase:
                        yield _format_op(cur)
                        last_op_phase = cur.get("phase", "idle")
                    last_keepalive = time.monotonic()
                if time.monotonic() - last_keepalive >= _SSE_KEEPALIVE_INTERVAL_SEC:
                    yield _SSE_KEEPALIVE
                    last_keepalive = time.monotonic()
        except GeneratorExit:
            log.debug("SSE /updates/sse client disconnected")
        finally:
            updates_log.unsubscribe(notify)

    response = Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    response.direct_passthrough = True
    return response


# ── Backwards-compat redirect: old /manager-updates URL → /updates ─────────

_legacy_bp = Blueprint("self_update_legacy", __name__,
                       url_prefix="/manager-updates")


@_legacy_bp.route("/", defaults={"path": ""})
@_legacy_bp.route("/<path:path>")
def legacy_redirect(path: str):
    """Saved bookmarks pointing at /manager-updates/* now redirect to
    /updates/. Single-user manager so this is convenience, not strict
    compatibility."""
    target = "/updates/" + path
    if request.query_string:
        target += "?" + request.query_string.decode("ascii", errors="replace")
    return redirect(target, code=301)
