"""Live-tail log viewer routes.

  GET  /logs           -- the page (tabbed viewer)
  GET  /logs/sse/<name>-- Server-Sent Events stream of a single tailer

The SSE generator subscribes to the tailer, yields each new line as a
properly-framed SSE message, and unsubscribes on client disconnect (the
generator's GeneratorExit fires when the browser closes the connection or
navigates away).
"""

import json
import logging
import time
from queue import Empty, Queue

from flask import Blueprint, Response, abort, render_template

from manager.tailer import get_all_tailers, get_friendly_labels, get_tailer

logs_bp = Blueprint("logs", __name__, url_prefix="/logs")
log = logging.getLogger(__name__)

# Per-client queue size. If the browser falls behind, lines are dropped
# (we'd rather lose old lines than memory-leak forever).
_QUEUE_MAX = 5000

# How long queue.get() blocks per iteration. Short so we can also send a
# keepalive at a regular cadence below.
_QUEUE_GET_TIMEOUT_SEC = 2.0

# Send a keepalive comment at this cadence even when there's no log data.
# Critical for two reasons:
#   1. Forces a write so a closed-by-the-browser connection is detected
#      promptly (Werkzeug only notices on next write attempt). Without it,
#      stale subscribers stack up across page refreshes.
#   2. Defeats any HTTP timeouts on the wire (proxies, middleware, etc.).
_KEEPALIVE_INTERVAL_SEC = 3.0

# Padding for the first message. Werkzeug's dev server buffers small
# responses until enough bytes accumulate -- without this, the first ~50
# bytes of SSE data sit in the buffer and the browser stays in "connecting".
# 2 KB reliably busts through. Browsers ignore SSE comment lines.
#
# All SSE-frame constants below are bytes -- direct_passthrough=True on the
# Response means Werkzeug expects bytes from the generator, not str.
_INITIAL_PADDING = (":" + (" " * 2048) + "\n\n").encode("utf-8")
_RETRY_FRAME = b"retry: 5000\n\n"
_HISTORY_END_FRAME = b": history-end\n\n"
_KEEPALIVE_FRAME = b": keepalive\n\n"


@logs_bp.route("/")
def index():
    return render_template(
        "logs.html",
        tailers=get_all_tailers(),
        labels=get_friendly_labels(),
    )


@logs_bp.route("/sse/<name>")
def sse_log(name: str):
    tailer = get_tailer(name)
    if tailer is None:
        abort(404)

    # Resolve the translate-on-render setting once per connection so the
    # SSE generator doesn't reload it for every line.
    from manager.config import get_setting
    translate = bool(get_setting("ui.translate_chinese_terms", True))

    queue: Queue[tuple[float, str]] = Queue(maxsize=_QUEUE_MAX)

    def callback(ts: float, line: str) -> None:
        try:
            queue.put_nowait((ts, line))
        except Exception:
            # Queue full -- drop the line. Better than blocking the tailer
            # thread (which would block ALL subscribers).
            pass

    unsubscribe = tailer.subscribe(callback)
    log.debug("SSE client connected to /logs/sse/%s "
              "(subscribers now %d, translate=%s)",
              name, tailer.subscriber_count, translate)

    def generate():
        try:
            # First two bytes-on-the-wire actions: tell the browser what
            # retry interval to use on disconnect (5 sec), and emit padding
            # to bust Werkzeug's response buffer.
            yield _RETRY_FRAME
            yield _INITIAL_PADDING

            # Initial burst: send buffered recent history so the browser
            # paints something immediately rather than waiting for live lines.
            for ts, line in tailer.get_history():
                yield _format_sse(ts, line, translate=translate)
            yield _HISTORY_END_FRAME

            # Live updates. We yield SOMETHING at least every
            # _KEEPALIVE_INTERVAL_SEC seconds so closed connections are
            # detected promptly (Werkzeug raises GeneratorExit on the next
            # write to a dead client).
            last_keepalive = time.monotonic()
            while True:
                try:
                    ts, line = queue.get(timeout=_QUEUE_GET_TIMEOUT_SEC)
                    yield _format_sse(ts, line, translate=translate)
                except Empty:
                    pass

                if time.monotonic() - last_keepalive >= _KEEPALIVE_INTERVAL_SEC:
                    yield _KEEPALIVE_FRAME
                    last_keepalive = time.monotonic()
        except GeneratorExit:
            log.debug("SSE client disconnected from /logs/sse/%s", name)
        finally:
            unsubscribe()
            log.debug("SSE unsubscribed from %s (subscribers now %d)",
                      name, tailer.subscriber_count)

    response = Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",  # nginx hint -- harmless without nginx
        "Connection": "keep-alive",
    })
    # Critical: tell Werkzeug not to fool with the iterator (e.g. compute
    # Content-Length by exhausting it, which would block forever on an
    # infinite SSE generator). Stream chunks straight to the socket.
    response.direct_passthrough = True
    return response


def _format_sse(ts: float, line: str, *,
                translate: bool = False) -> bytes:
    """Emit one SSE message as bytes. Payload is JSON so multi-line / weird
    chars in the log line don't break the SSE framing.

    When `translate` is True, the payload also includes a `line_html`
    field with translation spans inserted (Chinese phrases + BP_/DaoJu_
    asset names get [English] annotations alongside). The browser uses
    line_html as innerHTML when present, falling back to escapeHtml(line)
    when not. See manager/translations.py for the dictionary."""
    body: dict = {"ts": ts, "line": line}
    if translate:
        from manager.translations import annotate_line
        body["line_html"] = annotate_line(line)
    payload = json.dumps(body, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")
