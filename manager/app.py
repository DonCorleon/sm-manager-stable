"""Flask app factory."""

import logging

from flask import Flask, request

from manager.config import redact_dict
from manager.routes.backups import backups_bp
from manager.routes.bootstrap import bootstrap_bp
from manager.routes.dashboard import dashboard_bp
from manager.routes.logs import logs_bp
from manager.routes.map import map_bp
from manager.routes.mods import mods_bp
from manager.routes.self_update import self_update_bp, _legacy_bp as self_update_legacy_bp
from manager.routes.settings import settings_bp

log = logging.getLogger(__name__)

# Endpoints that are hit on a regular timer by HTMX. Logged at DEBUG (not
# INFO) so the firehose stays opt-in and the INFO stream stays meaningful.
# Add new polling endpoints here as they appear.
_POLLING_PATHS = frozenset({
    "/setup/install/status",
    "/api/server/status",
})


def create_app() -> Flask:
    log.info("Creating Flask app...")
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder=None,
    )
    log.debug("Flask instance created. template_folder=templates, static_folder=None")

    app.register_blueprint(dashboard_bp)
    log.info("Registered blueprint: dashboard (routes: /)")
    app.register_blueprint(bootstrap_bp)
    log.info("Registered blueprint: bootstrap (routes: /setup/...)")
    app.register_blueprint(logs_bp)
    log.info("Registered blueprint: logs (routes: /logs/...)")
    app.register_blueprint(settings_bp)
    log.info("Registered blueprint: settings (routes: /settings/...)")
    app.register_blueprint(backups_bp)
    log.info("Registered blueprint: backups (routes: /backups/...)")
    app.register_blueprint(map_bp)
    log.info("Registered blueprint: map (routes: /map/...)")
    app.register_blueprint(mods_bp)
    log.info("Registered blueprint: mods (routes: /mods/...)")
    app.register_blueprint(self_update_bp)
    log.info("Registered blueprint: self_update (routes: /updates/...)")
    app.register_blueprint(self_update_legacy_bp)
    log.info("Registered blueprint: self_update_legacy "
             "(routes: /manager-updates/* -> 301 to /updates/*)")
    log.debug("Total URL rules registered: %d", len(list(app.url_map.iter_rules())))
    for rule in app.url_map.iter_rules():
        log.debug("  route: %-7s %s -> %s", ",".join(rule.methods - {"HEAD", "OPTIONS"}),
                  rule.rule, rule.endpoint)

    @app.context_processor
    def _inject_ui_theme():
        # Make `ui_theme` available in every template (read by base.html
        # to set <html data-theme="...">). Falls back to "dark" if the
        # setting is missing or settings haven't been loaded yet (e.g.
        # very early bootstrap). Read fresh per-request so a save on
        # /settings reflects immediately on the next render -- no need
        # to restart the manager.
        from manager.config import get_setting
        try:
            theme = get_setting("ui.theme", "dark") or "dark"
        except Exception:
            theme = "dark"
        return {"ui_theme": theme}

    @app.before_request
    def _log_request():
        # Polling endpoints log at DEBUG so they don't bury real activity.
        is_polling = request.path in _POLLING_PATHS
        level = logging.DEBUG if is_polling else logging.INFO
        log.log(level, "HTTP %s %s from %s",
                request.method, request.path, request.remote_addr)
        # At DEBUG, dump the rest of the request: query args, form (redacted),
        # and a couple of useful headers. Keep it on separate lines for grep.
        if log.isEnabledFor(logging.DEBUG) and not is_polling:
            if request.args:
                log.debug("  query: %s", dict(request.args))
            if request.form:
                log.debug("  form:  %s", redact_dict(request.form.to_dict(flat=True)))
            ua = request.headers.get("User-Agent", "")
            log.debug("  UA:    %s", ua[:100])
            log.debug("  Host:  %s", request.headers.get("Host", ""))

    @app.after_request
    def _log_response(response):
        # Polling endpoints get a one-line debug log only if we're at DEBUG.
        if request.path in _POLLING_PATHS:
            return response
        log.debug("  resp:  %s %s (%d bytes)", response.status_code,
                  response.status, response.calculate_content_length() or 0)
        return response

    @app.errorhandler(404)
    def _not_found(_e):
        # Tile 404s are a normal Leaflet behaviour at the edge of the
        # tile grid (it asks for tiles just past the bounds and we
        # respond 404). Logging them at WARNING floods manager.log
        # with hundreds of entries per page-load. Keep tile 404s at
        # DEBUG; everything else stays at WARNING so genuine missing
        # routes still surface.
        if request.path.startswith("/map/tiles/"):
            log.debug("404 (tile out of range) %s from %s",
                      request.path, request.remote_addr)
        else:
            log.warning("404 for %s %s from %s",
                        request.method, request.path, request.remote_addr)
        return "Not found", 404

    @app.errorhandler(500)
    def _server_error(e):
        log.exception("500 handling %s %s: %s", request.method, request.path, e)
        return "Server error -- check manager.log", 500

    log.info("Flask app ready.")
    return app
