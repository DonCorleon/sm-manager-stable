"""Settings page: form-driven editor for the operator-facing tunables.

GET  /settings      -- render the form, prefilled with current values.
POST /settings      -- validate every field per schema, write back.
"""

import logging
import os
import threading

from flask import Blueprint, redirect, render_template, request, url_for

from manager.config import (SECRET_KEY_FRAGMENTS, get_setting, load_settings,
                             save_settings)
from manager.settings_schema import SETTINGS_SCHEMA

# Exit code we use to signal start_manager.bat that we want to be relaunched.
# The bat file's :server-loop label catches this and re-execs python -m manager.
_RESTART_EXIT_CODE = 99
# How long to wait between sending the restart-page response and actually
# exiting -- gives the browser time to receive the page before our process
# disappears.
_RESTART_DELAY_SEC = 1.5

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")
log = logging.getLogger(__name__)


def _git_auth_context() -> dict:
    """Resolve the bits the git-auth-setup section in settings.html needs:
    deploy key presence + public key, manager remote URL, derived deploy-
    keys page URL, whether git itself is installed, and whether the
    remote needs SSH-key auth at all. Used in every render path;
    isolated here so the route bodies stay focused on the form-handling
    work."""
    from manager import self_update
    remote_url = self_update.get_remote_url() or self_update.MANAGER_REMOTE_URL
    return {
        "git_status": self_update.get_status(),
        "git_resolved": bool(self_update._resolve_binary("git")),
        "manager_remote": remote_url,
        "manager_remote_keys_url": self_update.github_keys_url(remote_url),
        "auth_required": self_update.remote_needs_auth(remote_url),
    }


def _current_value(item):
    """Resolve current value from settings.toml, fall back to schema default.

    For kind="bool" we accept the older string forms ("on"/"off"/"true"/"false")
    that may exist in legacy settings.toml files and return a real bool so
    the template's `{% if values[key] %}checked{% endif %}` works directly.
    """
    raw = get_setting(item.key, item.default)
    if item.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("on", "true", "1", "yes")
    return raw


def _build_gated_by_map() -> dict[str, str]:
    """Build {gated_key: controlling_key} from each bool item's `controls`
    list. Used by the template to add data-gated-by attributes."""
    out: dict[str, str] = {}
    for sec in SETTINGS_SCHEMA:
        for item in sec.items:
            for controlled in (item.controls or []):
                out[controlled] = item.key
    return out


def _coerce_and_validate(item, raw_value: str):
    """Return (coerced_value, error_message). error_message is empty on OK.

    For kind="bool" the caller passes the form's value for the key, which
    is "1" if checkbox was checked and "" / None if unchecked. We
    interpret any non-empty value as True. (See `index()` for how the
    POST handler reads the form.)
    """
    raw = (raw_value or "").strip()

    if item.kind == "int":
        if raw == "":
            return item.default, ""
        try:
            v = int(raw)
        except ValueError:
            return None, f"{item.label}: must be a number (got {raw!r})"
        if item.min is not None and v < item.min:
            return None, f"{item.label}: minimum is {item.min} (got {v})"
        if item.max is not None and v > item.max:
            return None, f"{item.label}: maximum is {item.max} (got {v})"
        return v, ""

    if item.kind == "choice":
        if raw not in (item.choices or []):
            return None, f"{item.label}: must be one of {item.choices}"
        return raw, ""

    if item.kind == "bool":
        # raw is "1" when the box was checked, empty otherwise.
        return bool(raw), ""

    if item.kind == "str":
        return raw, ""

    if item.kind == "secret":
        # Caller is responsible for the empty-means-preserve behaviour --
        # the form handler skips secret keys whose form value is blank
        # before reaching coerce. If we get here, raw is non-empty and we
        # treat it like str.
        return raw, ""

    return None, f"{item.label}: unknown kind {item.kind!r}"


@settings_bp.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        log.info("Settings form POSTed")
        errors: list[str] = []
        updates: dict[str, object] = {}

        for section in SETTINGS_SCHEMA:
            for item in section.items:
                # Bool keys are unique: absent in form == unchecked == False.
                # Non-bool keys absent from the form means the input was
                # disabled (gated by an unchecked bool); preserve the
                # current value rather than resetting to schema default.
                if item.kind != "bool" and item.key not in request.form:
                    log.debug("Skipping %s -- field disabled, preserving current",
                              item.key)
                    continue
                raw = request.form.get(item.key, "")
                # Secret fields: empty submission means "keep current value"
                # (so the operator can save other settings without re-typing
                # the secret each time). The page renders secrets with empty
                # value + a "(set; leave blank to keep)" placeholder.
                if item.kind == "secret" and not raw.strip():
                    continue
                coerced, err = _coerce_and_validate(item, raw)
                if err:
                    errors.append(err)
                else:
                    current = get_setting(item.key, item.default)
                    if coerced != current:
                        updates[item.key] = coerced
                        # Mask values for keys that look secret (any
                        # password / token / webhook). No such setting
                        # exists in today's schema, but this guards
                        # against future additions.
                        if any(frag in item.key.lower()
                               for frag in SECRET_KEY_FRAGMENTS):
                            log.info("Setting %s: *** -> *** (secret-key "
                                     "value redacted)", item.key)
                        else:
                            log.info("Setting %s: %r -> %r",
                                     item.key, current, coerced)

        if errors:
            log.warning("Settings save aborted (%d errors): %s",
                        len(errors), "; ".join(errors))
            # On error preserve what the user just typed; for bool items
            # use the form's presence to determine current view state.
            view_values = {}
            for item in (it for sec in SETTINGS_SCHEMA for it in sec.items):
                if item.kind == "bool":
                    view_values[item.key] = item.key in request.form
                else:
                    view_values[item.key] = request.form.get(item.key, _current_value(item))
            return render_template(
                "settings.html",
                schema=SETTINGS_SCHEMA,
                values=view_values,
                gated_by=_build_gated_by_map(),
                errors=errors,
                saved_count=0,
                active_tab=request.args.get("tab"),
                **_git_auth_context(),
            )

        # Apply -- merge into a fresh load so we preserve everything else.
        if updates:
            settings = load_settings()
            for k, v in updates.items():
                cursor = settings
                parts = k.split(".")
                for p in parts[:-1]:
                    if p not in cursor or not isinstance(cursor[p], dict):
                        cursor[p] = {}
                    cursor = cursor[p]
                cursor[parts[-1]] = v
            save_settings(settings)
            log.info("Settings saved (%d change(s)).", len(updates))

        return render_template(
            "settings.html",
            schema=SETTINGS_SCHEMA,
            values={item.key: _current_value(item)
                    for item in (it for sec in SETTINGS_SCHEMA for it in sec.items)},
            gated_by=_build_gated_by_map(),
            errors=[],
            saved_count=len(updates),
            active_tab=request.args.get("tab"),
            **_git_auth_context(),
        )

    # GET
    return render_template(
        "settings.html",
        schema=SETTINGS_SCHEMA,
        values={item.key: _current_value(item)
                for item in (it for sec in SETTINGS_SCHEMA for it in sec.items)},
        gated_by=_build_gated_by_map(),
        errors=[],
        saved_count=None,
        active_tab=request.args.get("tab"),
        discord_test_msg=request.args.get("discord_test_msg"),
        discord_test_ok=request.args.get("discord_test_ok") == "1",
        git_msg=request.args.get("git_msg"),
        git_ok=request.args.get("git_ok") == "1",
        **_git_auth_context(),
    )


@settings_bp.route("/test-discord", methods=["POST"])
def test_discord():
    """Post a test message to the configured Discord webhook so the
    operator can verify the URL is correct without waiting for an
    in-game event. Result surfaces back via query string."""
    log.info("Discord test connection requested by %s", request.remote_addr)
    url = get_setting("discord.webhook_url", "")
    if not url:
        return redirect(url_for(
            "settings.index",
            tab="Discord webhook relay",
            discord_test_msg=("No webhook URL configured. Save one in "
                              "the Discord section above first."),
            discord_test_ok="0",
        ))
    from manager.discord_relay import post_test_message
    ok, msg = post_test_message(url)
    log.info("Discord test result: ok=%s msg=%s", ok, msg)
    return redirect(url_for(
        "settings.index",
        tab="Discord webhook relay",
        discord_test_msg=("Test posted -- check your Discord channel. "
                          "(" + msg + ")") if ok else ("Test failed: " + msg),
        discord_test_ok="1" if ok else "0",
    ))


@settings_bp.route("/restarting", methods=["GET"])
def restarting():
    """Render the "manager is restarting" probe-and-redirect page.
    Used by /updates after Apply / Force-sync (which schedule the
    manager exit-99). Optional ?to=/path query arg controls where the
    page navigates to once the manager is back up; defaults to /. The
    page actively probes that URL every second instead of blindly
    counting down."""
    target = request.args.get("to", "/")
    # Defensive: only allow same-site relative paths so this page
    # can't be coerced into bouncing the operator to an external site
    # via a crafted query string.
    if not target.startswith("/"):
        target = "/"
    return render_template("settings_restarting.html",
                           reload_after_sec=15, to=target)


@settings_bp.route("/restart-manager", methods=["POST"])
def restart_manager():
    """Schedule a process exit with code 99. start_manager.bat's :server-loop
    catches that and relaunches us. The browser's redirect-page polls until
    the new manager is up.

    Note: any in-flight ops (a running server-start sequence, an active
    install, etc.) get cut off. The dashboard shows op_in_progress=False
    on the new boot. The actual game-server processes survive (they're
    detached) and will be re-adopted on the next status poll thanks to
    the orphan-adoption logic in lifecycle._try_adopt.
    """
    log.warning("Manager restart requested by %s -- exiting with code %d "
                "in %.1fs (supervisor in start_manager.bat will relaunch)",
                request.remote_addr, _RESTART_EXIT_CODE, _RESTART_DELAY_SEC)

    def _delayed_exit():
        # Use threading.Event().wait so a clean shutdown is possible if
        # something else terminates us first; otherwise hard-exit.
        threading.Event().wait(_RESTART_DELAY_SEC)
        log.warning("Restart timer fired -- os._exit(%d)", _RESTART_EXIT_CODE)
        # R9: signal a clean shutdown so the bootloader skips its
        # post-unclean-shutdown fsck on the next boot.
        from manager import self_update
        from manager.config import DATA_DIR
        self_update.write_shutdown_clean_marker()
        # R1: a deliberate operator-clicked restart is NOT a fast
        # crash. Clear data/.boot_in_progress so the bootloader on
        # the next launch doesn't count this run against the crash
        # threshold and trigger a rollback. The mark_boot_stable
        # daemon clears this same marker after 60s of uptime, but
        # if the operator restarts BEFORE that 60s elapses (e.g.
        # right after applying a settings change) it will still be
        # present.
        try:
            marker = DATA_DIR / ".boot_in_progress"
            if marker.exists():
                marker.unlink()
        except OSError:
            log.exception("Could not clear boot_in_progress marker "
                          "(non-fatal)")
        os._exit(_RESTART_EXIT_CODE)

    threading.Thread(target=_delayed_exit, daemon=True,
                     name="manager-restart").start()
    return render_template("settings_restarting.html",
                           reload_after_sec=15)
