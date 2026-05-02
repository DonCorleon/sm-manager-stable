"""/mods/* routes -- Workshop mod subscription management.

GET  /mods/                 page (input + list + activity-log inline)
POST /mods/add              { workshop_id, replace? } -> start_add
POST /mods/<folder>/remove  -> start_remove

The activity log on the page subscribes to the existing /updates/sse
stream and filters to source=steam, so no new SSE plumbing here.
"""

import logging

from flask import Blueprint, redirect, render_template, request, url_for

from manager import lifecycle, mods

mods_bp = Blueprint("mods", __name__, url_prefix="/mods")
log = logging.getLogger(__name__)


def _redirect_with_flash(msg: str, kind: str = "info"):
    return redirect(url_for("mods.index", msg=msg, kind=kind))


@mods_bp.route("/")
def index():
    """Mods page. Lists installed mods + add box + activity log."""
    log.debug("mods.index render")
    return render_template(
        "mods.html",
        mods_list=mods.list_installed(),
        server_status=lifecycle.get_status(),
        flash_msg=request.args.get("msg"),
        flash_kind=request.args.get("kind"),
    )


@mods_bp.route("/add", methods=["POST"])
def add():
    """Install (or re-install if replace=1) a Workshop mod by numeric ID."""
    workshop_id = (request.form.get("workshop_id") or "").strip()
    replace = request.form.get("replace") == "1"
    log.info("mods.add from %s (id=%r replace=%s)",
             request.remote_addr, workshop_id, replace)
    ok, reason = mods.start_add(workshop_id, replace=replace)
    if not ok:
        return _redirect_with_flash(reason, "error")
    return _redirect_with_flash(
        reason + " -- watch the activity log below.", "ok")


@mods_bp.route("/<folder>/redownload", methods=["POST"])
def redownload(folder: str):
    """Re-fetch an already-installed Workshop mod via SteamCMD,
    replacing the local files. Pulls the workshop_id from the
    manifest so the operator only has to click the row's button."""
    log.info("mods.redownload from %s (folder=%r)",
             request.remote_addr, folder)
    ok, reason = mods.start_redownload(folder)
    if not ok:
        return _redirect_with_flash(reason, "error")
    return _redirect_with_flash(
        reason + " -- watch the activity log below.", "ok")


@mods_bp.route("/<folder>/remove", methods=["POST"])
def remove(folder: str):
    """Remove a Workshop-installed mod (deletes WS\\Mods\\<folder>\\
    and drops the manifest entry). Local mods can't be removed via
    this route."""
    log.warning("mods.remove from %s (folder=%r)",
                request.remote_addr, folder)
    ok, reason = mods.start_remove(folder)
    if not ok:
        return _redirect_with_flash(reason, "error")
    return _redirect_with_flash(
        reason + " -- watch the activity log below.", "ok")
