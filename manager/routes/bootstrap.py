"""Bootstrap (first-run) routes: status check, setup wizard, and preview."""

import json
import logging
import time

from flask import Blueprint, Response, redirect, render_template, request, url_for

from manager import install
from manager.checks import run_all_checks
from manager.config import load_settings, save_settings
from manager.wizard import (
    CURATED_TEMPLATES,
    MAPS,
    active_runtime_instances,
    build_launch_args,
    load_existing,
    parse_form,
    to_settings_dict,
)

bootstrap_bp = Blueprint("bootstrap", __name__, url_prefix="/setup")
log = logging.getLogger(__name__)

# SSE wire constants (mirrors /updates, /backups, /logs).
_SSE_INITIAL_PADDING = (":" + (" " * 2048) + "\n\n").encode("utf-8")
_SSE_RETRY_FRAME = b"retry: 5000\n\n"
_SSE_KEEPALIVE = b": keepalive\n\n"
_SSE_KEEPALIVE_INTERVAL_SEC = 25.0


@bootstrap_bp.route("/")
def setup_check():
    log.debug("bootstrap.setup_check: rendering status page")
    checks = run_all_checks()
    all_ok = all(c.passed for c in checks)
    settings = load_settings()
    wizard_done = "server" in settings
    log.debug("  context: all_ok=%s wizard_done=%s checks=%d",
              all_ok, wizard_done, len(checks))
    return render_template(
        "bootstrap_check.html",
        checks=checks,
        all_ok=all_ok,
        wizard_done=wizard_done,
    )


@bootstrap_bp.route("/wizard", methods=["GET", "POST"])
def wizard():
    settings = load_settings()
    bind_port = settings.get("network", {}).get("bind_port", 5000)
    log.debug("bootstrap.wizard: method=%s, bind_port=%d", request.method, bind_port)

    if request.method == "POST":
        log.info("Wizard form POSTed (mode=%s)", request.form.get("mode"))
        config, errors = parse_form(request.form, manager_bind_port=bind_port)

        if errors:
            log.warning("Wizard validation failed (%d errors): %s",
                        len(errors), "; ".join(errors))
            log.debug("  re-rendering wizard.html with user input + errors")
            return render_template(
                "wizard.html",
                config=config,
                templates=CURATED_TEMPLATES,
                maps=MAPS,
                errors=errors,
            )

        log.debug("  validation passed, merging into settings dict")
        new_settings = to_settings_dict(config, settings)
        save_settings(new_settings)
        log.info(
            "Wizard saved: mode=%s, main_map=%s, max_players=%d, template=%s",
            config.mode, config.main_map, config.max_players, config.gameplay_template,
        )
        log.debug("  redirecting to /setup/preview")
        return redirect(url_for("bootstrap.preview"))

    config = load_existing(settings)
    has_existing = "server" in settings
    log.info("Wizard form requested (existing config found: %s)", has_existing)
    log.debug("  rendering wizard.html (mode=%s main_map=%s)",
              config.mode, config.main_map)
    return render_template(
        "wizard.html",
        config=config,
        templates=CURATED_TEMPLATES,
        maps=MAPS,
        errors=[],
    )


@bootstrap_bp.route("/install", methods=["GET", "POST"])
def install_page():
    """GET shows progress; POST starts the install (or no-op if running)."""
    settings = load_settings()
    if "server" not in settings:
        log.info("Install requested but no wizard config saved -- redirecting to wizard")
        return redirect(url_for("bootstrap.wizard"))

    if request.method == "POST":
        log.info("Install POST received from %s", request.remote_addr)
        started = install.start()
        if not started:
            log.info("  install already running, just showing page")
        else:
            log.info("  install started")
        return redirect(url_for("bootstrap.install_page"))

    state = install.get_state()
    log.debug("install page render: status=%s step=%r line_count=%d",
              state["status"], state["step"], state["line_count"])
    return render_template("install.html", state=state)


@bootstrap_bp.route("/install/status")
def install_status():
    """LEGACY HTMX polling endpoint -- kept for any caller that hasn't
    moved to SSE yet. New page uses /setup/install/sse and patches
    DOM in place. Returns the same partial.

    When status is terminal (completed/failed) we return HTTP 286, which
    HTMX recognises as 'stop polling'. Otherwise normal 200."""
    state = install.get_state()
    body = render_template("_install_status.html", state=state)
    if state["status"] in ("completed", "failed"):
        log.debug("install_status: terminal state (%s) -- returning 286 to halt polling",
                  state["status"])
        return body, 286
    return body


@bootstrap_bp.route("/install/sse")
def install_sse():
    """SSE stream of install state changes. Replaces the HTMX every-2s
    poll on the /install page.

    Frame format: `event: state` JSON body matching install.get_state()
    -- {status, step, started_at, finished_at, elapsed_seconds, error,
    lines, line_count}. The frontend incrementally appends new lines
    (using line_count to detect deltas) and updates the status banner
    in place; no full-page reload."""
    notify = install.subscribe()
    log.debug("SSE /setup/install/sse subscriber from %s",
              request.remote_addr)

    def _frame() -> bytes:
        snap = install.get_state()
        body = json.dumps(snap, ensure_ascii=False)
        return f"event: state\ndata: {body}\n\n".encode("utf-8")

    def generate():
        try:
            yield _SSE_RETRY_FRAME
            yield _SSE_INITIAL_PADDING
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
            log.debug("SSE /setup/install/sse client disconnected")
        finally:
            install.unsubscribe(notify)

    response = Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    response.direct_passthrough = True
    return response


@bootstrap_bp.route("/preview")
def preview():
    log.debug("bootstrap.preview: requested")
    settings = load_settings()
    if "server" not in settings:
        log.info("Preview requested but no wizard config saved -- redirecting to wizard")
        return redirect(url_for("bootstrap.wizard"))

    config = load_existing(settings)
    log.debug("  building runtime instance list (mode=%s)", config.mode)
    instance_args = [
        {"runtime": ri, "args": build_launch_args(config, ri)}
        for ri in active_runtime_instances(config)
    ]
    log.debug("  %d active instance(s) for preview", len(instance_args))

    checks = run_all_checks()

    # Build the dynamic confirm-dialog message: only mention components
    # that ACTUALLY need to download/install given the current state of
    # the machine. Soulmask's app_update is idempotent, so we always
    # include it -- but the label flips between "Install" and
    # "Verify / update" based on whether the binary is already there.
    by_name = {c.name: c for c in checks}
    install_steps: list[str] = []
    if not by_name.get("SteamCMD") or not by_name["SteamCMD"].passed:
        install_steps.append("Download SteamCMD (~5 MB, <1 min)")
    if not by_name.get("Git") or not by_name["Git"].passed:
        install_steps.append("Download PortableGit (~50 MB, ~1-2 min)")
    if by_name.get("Soulmask install") and by_name["Soulmask install"].passed:
        install_steps.append("Verify / update Soulmask server (~minutes if "
                             "an update is available; instant otherwise)")
    else:
        install_steps.append("Download Soulmask server (~5-15 GB, 10-30 min)")
    confirm_msg = "This will:\n  - " + "\n  - ".join(install_steps) + \
                  "\n\nContinue?"

    return render_template(
        "wizard_preview.html",
        config=config,
        instance_args=instance_args,
        checks=checks,
        install_steps=install_steps,
        confirm_msg=confirm_msg,
    )
