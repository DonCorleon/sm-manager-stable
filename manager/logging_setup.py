"""Logging configuration: rotating file + console + secret redaction.

Custom level VERBOSE (5) sits BELOW DEBUG (10) so it's the most detailed
band. Operator semantics:
  INFO    -- normal user, default. Major events, op start/end, errors.
  DEBUG   -- building / testing. Detailed but reasonable.
  VERBOSE -- fault hunting. Every branch, every value, every loop tick.

Use `log.verbose(...)` (added below to the Logger class) inside hot
loops, deep state-machine ticks, sub-step pipelines.
"""

import logging
import logging.handlers
import re
import time
from pathlib import Path

from manager.config import PROJECT_ROOT, load_settings

LOGS_DIR = PROJECT_ROOT / "logs"

# Custom level. Numeric value chosen so:
#   NOTSET (0) < VERBOSE (5) < DEBUG (10) < INFO (20)
# meaning a logger set to VERBOSE captures everything down to and
# including VERBOSE; a logger set to DEBUG hides VERBOSE; etc.
VERBOSE = 5
logging.addLevelName(VERBOSE, "VERBOSE")


def _verbose(self, message, *args, **kwargs):
    """Method patched onto logging.Logger so callers can `log.verbose(...)`.
    Logger.isEnabledFor short-circuits the format work when we're above
    VERBOSE level, same as the stock log methods."""
    if self.isEnabledFor(VERBOSE):
        self._log(VERBOSE, message, args, **kwargs)


# Patch into the Logger class once at import time.
if not hasattr(logging.Logger, "verbose"):
    logging.Logger.verbose = _verbose  # type: ignore[attr-defined]

# Patterns scrubbed from every log line. Extend as new secret shapes appear.
_REDACT_PATTERNS = [
    # Launch args: -PSW=... -adminpsw=... -rconpsw=...
    (re.compile(r'(-PSW=)"?[^"\s]+"?'), r'\1***'),
    (re.compile(r'(-adminpsw=)"?[^"\s]+"?'), r'\1***'),
    (re.compile(r'(-rconpsw=)"?[^"\s]+"?'), r'\1***'),
    # LogNet login-request line carries the password as ?PSW=<hex>
    (re.compile(r'(\?PSW=)[^?\s&]+'), r'\1***'),
    # Discord webhook URL token segment.
    (re.compile(r'(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_\-]+'), r'\1***'),
]


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        for pattern, replacement in _REDACT_PATTERNS:
            msg = pattern.sub(replacement, msg)
        return msg


class _WindowsSafeTimedRotatingFileHandler(
        logging.handlers.TimedRotatingFileHandler):
    """TimedRotatingFileHandler that survives Windows lock contention.

    On Windows, if anything (Notepad, VSCode, antivirus, a tail viewer)
    has manager.log open at the rollover instant, the rename fails
    with PermissionError WinError 32. The base handler does NOT advance
    `rolloverAt` on failure, so EVERY subsequent log emit re-attempts
    the rotation, prints the full traceback to stderr, then writes the
    line. The terminal saturates and the manager appears hung.

    This subclass:
      - Catches OSError/PermissionError around doRollover().
      - Advances `rolloverAt` to the next interval anyway so we don't
        retry on every emit.
      - Logs a single WARNING per failed rollover so the operator sees
        the cause (instead of a flood of identical tracebacks).

    The current file just keeps growing until the next interval. Less
    bad than the disaster mode it replaces.
    """

    def doRollover(self):  # noqa: D401 (matches base class signature)
        try:
            super().doRollover()
            return
        except (OSError, PermissionError) as e:
            # Reopen the stream we (or the base class) closed before the
            # failed rename. Without this, all subsequent emits crash on
            # write-to-closed-file.
            if self.stream is None or self.stream.closed:
                self.stream = self._open()
            # Push rolloverAt forward by one interval so we don't retry
            # on the very next emit. computeRollover(now) works for
            # when='midnight' the same way the base class uses it.
            now = int(time.time())
            self.rolloverAt = self.computeRollover(now)
            # Single audible warning. Use a real logger (not print) so
            # it lands in the (still-open) log AND on the console. We
            # gate via a class attribute so multi-day failures still
            # get one warning per day, not per emit.
            sys_log = logging.getLogger("manager.logging_setup")
            sys_log.warning(
                "Log rotation skipped: %s. Likely cause: another process "
                "(Notepad / editor / antivirus) holds %s open. Will try "
                "again at next interval.",
                e, self.baseFilename,
            )


def configure_logging() -> None:
    """Set up handlers first, then read settings for level. Order matters:
    if settings.toml is missing, ensure_settings() inside load_settings()
    will log "creating defaults" — and that line needs to be captured."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Width 7 fits "VERBOSE", "WARNING", "CRITICAL".
    formatter = _RedactingFormatter(
        fmt="%(asctime)s [%(levelname)-7s] %(name)s:%(lineno)d  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_path = LOGS_DIR / "manager.log"
    file_handler = _WindowsSafeTimedRotatingFileHandler(
        filename=log_path,
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)  # default until settings load
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Werkzeug's per-request logging is noisy and unformatted; we replace it
    # with our own before_request hook in app.py.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    log = logging.getLogger(__name__)
    log.info("Log handlers attached. File: %s", log_path)

    # Now read settings (any log lines produced during this are captured).
    settings = load_settings()
    level_name = settings.get("logging", {}).get("level", "INFO").upper()
    # Translate VERBOSE -> our custom int. getattr(logging, "VERBOSE")
    # also works because logging.addLevelName makes it available as a
    # module attribute, but be explicit for clarity.
    if level_name == "VERBOSE":
        level = VERBOSE
    else:
        level = getattr(logging, level_name, logging.INFO)
    root.setLevel(level)

    if level == VERBOSE:
        log.warning(
            "VERBOSE logging active -- every branch, every value, every "
            "loop tick will land in manager.log. Use only for fault "
            "hunting; expect a firehose. Set [logging] level = \"DEBUG\" "
            "or \"INFO\" in settings.toml for normal use."
        )
    elif level == logging.DEBUG:
        log.warning(
            "DEBUG logging active -- detailed build/testing output. "
            "Set [logging] level = \"INFO\" in settings.toml for normal use."
        )
    else:
        log.info("Log level: %s (set [logging] level in settings.toml to change)",
                 level_name)
