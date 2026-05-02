"""Discord webhook relay -- one-way game-event forwarding.

Architecture:

    [GameEvent stream] -> filter (settings) -> submit() -> [bounded queue]
                                                              |
                                                       [worker thread]
                                                              |
                                                          batch+post
                                                              |
                                                        Discord webhook

Why a worker thread + queue rather than posting directly from the
event handler:

  - Discord rate-limits webhooks (~30 messages/min per channel). A
    busy server with realtime relay would blow past it.
  - Discord can be down for minutes (DNS, TLS handshake, 5xx). The
    manager must not block on Discord availability -- the queue
    absorbs short outages, drop-oldest handles long ones.
  - Batching is configurable. With batch_interval=10 we coalesce
    up to ~25 events into one post; with 0 we go realtime.

Public surface:

    DiscordRelay(webhook_url, batch_interval_sec).start() / .submit() / .stop()
    format_event(GameEvent) -> RelayMessage | None
    post_test_message(webhook_url) -> (ok, msg)

The relay does NOT decide which events to forward. The caller
filters by settings and only submits relayable events. That keeps
this module a clean transport with no policy.
"""

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Bound the outbound queue so a long Discord outage doesn't grow
# unboundedly. Drop oldest when full; surface "lost N events" at the
# top of the next successful batch so the operator sees what happened.
_QUEUE_MAX = 500

# Discord per-message content cap is 2000 chars. We chunk a little
# under that to leave room for our prefix + a join newline.
_DISCORD_CONTENT_MAX = 1900

# Max events coalesced into a single batch post. Beyond this the post
# would risk exceeding the content cap; batching more is pointless.
_BATCH_MAX = 25

# Hard ceiling on retry-after we'll honour. Discord usually returns
# small values (sub-second to a minute). Anything longer than this
# indicates we'd be better off dropping the message and trying again
# next batch -- 30s is a sane wall.
_RETRY_AFTER_CAP = 30.0

# Default Discord username -- shows up next to the avatar in the
# channel. Operator can override per-relay if multi-webhook lands.
_DEFAULT_USERNAME = "Soulmask Manager"


@dataclass
class RelayMessage:
    """One message destined for Discord. `content` is the plain text
    (markdown supported); `embed` is an optional Discord embed dict
    for richer formatting; `image_bytes`/`image_filename` attach a
    raw image (PNG bytes) via Discord's multipart upload.

    Messages with image_bytes are POSTed individually (not batched
    with other text messages) since Discord's multipart endpoint is
    one-attachment-per-request and we don't want to merge unrelated
    events into one richly-formatted post."""
    content: str
    embed: Optional[dict] = None
    image_bytes: Optional[bytes] = None
    image_filename: str = "attachment.png"


# ── Event formatter ─────────────────────────────────────────────────────────


# POI types whose nearest-neighbour match is too generic to be
# useful as a "near X" label. Animal spawns, baby-animal spawns,
# generic resource pickups, etc. -- knowing you died near a chicken
# isn't informative. We filter these out and rely on the region
# label alone for those events.
_GENERIC_POI_KEYWORDS = (
    "animal", "spawn", "baby", "egg", "multiple", "pickup",
    "fireflies", "footprint",
)

# Maximum distance (UE units squared) for a "near X" label to be
# meaningful. ~7000 UE units = ~140 in-game grid units. Beyond this
# the POI is far enough that "near" misleads the reader.
_MAX_POI_DIST_SQ = 50_000_000


def _is_generic_poi(poi: dict) -> bool:
    """True if this POI is too generic for a 'near X' label."""
    blob = (str(poi.get("type", "")) + " " +
            str(poi.get("name", "")) + " " +
            str(poi.get("title", ""))).lower()
    return any(kw in blob for kw in _GENERIC_POI_KEYWORDS)


def _poi_label(poi: dict) -> str:
    """Pick the most descriptive label for a POI. Title is usually
    the best (e.g. 'Crocodile Lair' vs name='Berserk Sobek' vs
    type='Beast Lair') -- it's the human-readable description the
    datamine attached to the specific landmark."""
    label = (str(poi.get("title", "")).strip()
             or str(poi.get("type", "")).strip()
             or str(poi.get("name", "")).strip())
    return label


def _location_context(event) -> str:
    """Return a human-readable location suffix for this event, or
    empty string if no location is known.

    Format: 'near *<POI>* in **<region>**' (Discord markdown). Both
    parts are optional; either or neither may be present depending
    on what world_db can resolve.

    Spatial enrichment is the original Phase 4 feature: bare 'Don
    died' becomes 'Don died near Sand Dunes Dungeon in Barren
    Sandland'. Cheap (linear NN scan over ~10-14k POIs per level,
    sub-millisecond)."""
    raw = event.raw or {}
    loc = raw.get("location") or raw.get("source")  # invasion prep uses 'source'
    if not loc or not isinstance(loc, dict):
        return ""
    if "x" not in loc or "y" not in loc:
        return ""
    try:
        from manager.paths import level_for_server
        from manager import world_db
    except Exception:
        return ""
    level = level_for_server(event.server)
    try:
        pos_x = int(loc["x"])
        pos_y = int(loc["y"])
    except (TypeError, ValueError):
        return ""

    parts: list[str] = []
    try:
        # Look at the 5 nearest POIs and pick the first non-generic
        # one within range. That way a player sitting on top of an
        # Animal Spawn but next to a Beast Lair gets "near Beast Lair".
        nn = world_db.nearest_poi(pos_x, pos_y, level=level, max_results=5)
        for poi in (nn or []):
            d2 = poi.get("d2") or 0
            if d2 > _MAX_POI_DIST_SQ:
                break  # all subsequent are further; stop early
            if _is_generic_poi(poi):
                continue
            label = _poi_label(poi)
            if label:
                parts.append(f"near *{label}*")
                break
    except Exception:
        log.debug("spatial enrichment: nearest_poi lookup failed",
                  exc_info=True)
    try:
        r = world_db.find_region_for(pos_x, pos_y, level=level)
        if r and r.get("name"):
            parts.append(f"in **{r['name']}**")
    except Exception:
        log.debug("spatial enrichment: region lookup failed",
                  exc_info=True)

    return (" " + " ".join(parts)) if parts else ""


def format_event(event) -> Optional[RelayMessage]:
    """Translate a GameEvent into a RelayMessage. Returns None for
    events that should never go to Discord (parser internal events,
    things we don't have a template for yet).

    Caller is expected to have already decided this event's category
    is enabled in settings before calling. We do not consult settings
    here.

    Spatial enrichment: events that carry a UE-coord location get
    a 'near *POI* in **region**' suffix appended via _location_context.
    """
    kind = event.kind
    actor = event.actor
    summary = event.summary

    # ── Player presence ──
    if kind == "joined":
        return RelayMessage(content=f"➕ **{actor}** joined the server")
    if kind == "left":
        return RelayMessage(content=f"➖ **{actor}** left the server")

    where = _location_context(event)

    # ── Player deaths ──
    # No killer attribution (WS.log doesn't carry it). Pending
    # HookLogger upgrade for richer combat details.
    if kind == "died":
        return RelayMessage(content=f"💀 **{actor}** {summary}{where}")

    # ── Thrall economy ──
    if kind == "recruited":
        return RelayMessage(content=f"🤝 **{actor}** recruited *{summary}*{where}")

    # NOTE: handlers for "killed", "thrall killed", "knocked down",
    # "thrall lost", "thrall down", and "invasion prep/scout/settle"
    # were removed 2026-05-02. Those events fired on log-pattern
    # matches that didn't carry enough context to produce a useful
    # message (kill attribution missing, invasion data noisy). They
    # will return via the [HOOK]-mod channel where the BP author
    # controls the payload directly.

    # Unknown / unhandled kind -- silently skipped.
    return None


# ── Relay ───────────────────────────────────────────────────────────────────


class DiscordRelay:
    """Background worker that drains a queue of RelayMessages and
    posts them to a Discord webhook URL. Thread-safe. Stoppable.

    Failure modes handled:
      - Network unreachable / DNS failure -> log warning, drop the
        current batch, keep running.
      - HTTP 429 rate limit -> sleep for Retry-After (capped),
        retry once; if still 429, give up on the batch.
      - HTTP 5xx -> log + give up on the batch.
      - Queue full -> drop oldest, increment lost-count; the next
        successful post starts with "(lost N earlier event(s))".
    """

    def __init__(self, webhook_url: str,
                 batch_interval_sec: float = 10.0,
                 username: str = _DEFAULT_USERNAME):
        if not webhook_url:
            raise ValueError("webhook_url is required")
        self._url = webhook_url
        self._batch_interval = max(0.0, float(batch_interval_sec))
        self._username = username
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._lost_count = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Idempotent: start the worker if not already running."""
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._loop, daemon=True, name="discord-relay")
        self._worker.start()
        log.info("Discord relay started (batch_interval=%.1fs)",
                 self._batch_interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to exit. Joins for up to `timeout` sec."""
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=timeout)
        log.info("Discord relay stopped")

    def submit(self, msg: RelayMessage) -> None:
        """Enqueue a message for posting. Never blocks; drops oldest
        if the queue is full and bumps the lost-count so the next
        successful post can surface the gap."""
        if not isinstance(msg, RelayMessage):
            raise TypeError(f"submit expects RelayMessage, got {type(msg)}")
        try:
            self._queue.put_nowait(msg)
            return
        except queue.Full:
            pass
        # Make room.
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            self._lost_count += 1
        # Now there's space (worker may have also drained one in the
        # interim, but put_nowait will succeed either way).
        try:
            self._queue.put_nowait(msg)
        except queue.Full:
            # Vanishingly rare race; just bump lost_count again.
            with self._lock:
                self._lost_count += 1

    def lost_count(self) -> int:
        """For tests / observability: how many events we've dropped
        since last successful post."""
        with self._lock:
            return self._lost_count

    # ── Worker loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            batch = self._collect_batch()
            if batch or self._lost_count:
                self._post_batch(batch)

    def _collect_batch(self) -> list:
        """Fill a batch up to _BATCH_MAX. Behaviour depends on
        batch_interval_sec:
          - >0: sleep that long (interruptible by stop), then drain
            whatever's in the queue.
          - 0: block for at least one message (with 1s polling so
            stop() is responsive), then opportunistically pull more.
        """
        batch: list = []
        if self._batch_interval > 0:
            self._stop.wait(self._batch_interval)
            if self._stop.is_set():
                return batch
            while len(batch) < _BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
        else:
            # Realtime mode.
            try:
                batch.append(self._queue.get(timeout=1.0))
            except queue.Empty:
                return batch
            while len(batch) < _BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
        return batch

    def _post_batch(self, batch: list) -> None:
        with self._lock:
            lost = self._lost_count
            self._lost_count = 0

        # Split: text-only messages get aggregated + chunked into one
        # or more JSON posts. Image-bearing messages each get their
        # own multipart post (Discord's attachment endpoint is one
        # at a time, and bundling unrelated events into a single
        # rich post would lose context).
        text_only = [m for m in batch if not m.image_bytes]
        image_msgs = [m for m in batch if m.image_bytes]

        parts: list[str] = []
        if lost > 0:
            parts.append(f"_(lost {lost} earlier event(s) while "
                         f"Discord was unreachable)_")
        for m in text_only:
            if m.content:
                parts.append(m.content)

        for chunk in self._chunk_for_discord(parts, _DISCORD_CONTENT_MAX):
            self._post_one({"content": chunk, "username": self._username})

        for img_msg in image_msgs:
            self._post_image(img_msg)

    def _post_image(self, msg: RelayMessage, attempt: int = 1) -> None:
        """POST a multipart message with a single image attachment.
        Discord renders the image inline in the channel."""
        if not msg.image_bytes:
            return
        files = {
            "file": (msg.image_filename, msg.image_bytes, "image/png"),
        }
        # `payload_json` part carries the message content (text caption)
        # and other webhook fields. Without it we'd just post a bare
        # image; with it we get text + image together.
        payload_json = json.dumps({
            "content": msg.content or "",
            "username": self._username,
        })
        data = {"payload_json": payload_json}
        try:
            r = requests.post(self._url, files=files, data=data, timeout=30)
        except requests.RequestException as e:
            log.warning("Discord image POST failed: %s", e)
            return

        if r.status_code in (200, 204):
            return
        if r.status_code == 429:
            retry = self._retry_after_seconds(r)
            log.info("Discord image POST rate-limited; sleeping %.1fs", retry)
            time.sleep(min(retry, _RETRY_AFTER_CAP))
            if attempt < 2:
                self._post_image(msg, attempt + 1)
            return
        log.warning("Discord image POST returned %d: %s",
                    r.status_code, r.text[:200])

    @staticmethod
    def _chunk_for_discord(parts, max_len: int):
        """Yield strings each <= max_len, joining parts with newlines.
        A single part longer than max_len is yielded as-is (Discord
        will reject it with 400; the caller can see the rejection in
        logs and know the formatter produced something too big)."""
        cur: list[str] = []
        cur_len = 0
        for p in parts:
            sep = 1 if cur else 0  # newline cost when joining
            if cur and (cur_len + sep + len(p)) > max_len:
                yield "\n".join(cur)
                cur = [p]
                cur_len = len(p)
            else:
                cur.append(p)
                cur_len += sep + len(p)
        if cur:
            yield "\n".join(cur)

    def _post_one(self, payload: dict, attempt: int = 1) -> None:
        try:
            r = requests.post(self._url, json=payload, timeout=10)
        except requests.RequestException as e:
            log.warning("Discord relay POST failed: %s", e)
            return  # batch lost; queue keeps draining

        if r.status_code in (200, 204):
            return
        if r.status_code == 429:
            retry = self._retry_after_seconds(r)
            log.info("Discord relay rate-limited; sleeping %.1fs", retry)
            time.sleep(min(retry, _RETRY_AFTER_CAP))
            if attempt < 2:
                self._post_one(payload, attempt + 1)
            return
        log.warning("Discord relay returned %d: %s",
                    r.status_code, r.text[:200])

    @staticmethod
    def _retry_after_seconds(response) -> float:
        """Extract Retry-After from a Discord 429 response. Discord
        returns it in BOTH the header AND the JSON body (`retry_after`,
        in seconds, can be a float). Header is canonical; body is
        a fallback for older clients. Cap at _RETRY_AFTER_CAP."""
        h = response.headers.get("Retry-After") or \
            response.headers.get("retry-after")
        if h:
            try:
                return float(h)
            except ValueError:
                pass
        try:
            j = response.json()
            v = j.get("retry_after")
            if v is not None:
                return float(v)
        except (ValueError, AttributeError):
            pass
        return 1.0


# ── Synchronous test helper (used by the Settings page Test button) ────────


def post_test_message(webhook_url: str,
                      content: Optional[str] = None,
                      timeout_sec: float = 10.0) -> tuple[bool, str]:
    """Post a single test message synchronously. Returns (ok, message).
    On failure, message is a human-readable explanation suitable for
    surfacing on the settings page."""
    if not webhook_url:
        return False, "no webhook URL configured"
    payload = {
        "content": content or ("\U0001F7E2 Soulmask Manager connected -- "
                               "test message from /settings/."),
        "username": _DEFAULT_USERNAME,
    }
    try:
        r = requests.post(webhook_url, json=payload, timeout=timeout_sec)
    except requests.RequestException as e:
        return False, f"network error: {e}"
    if r.status_code in (200, 204):
        return True, f"posted ok (HTTP {r.status_code})"
    if r.status_code == 401:
        return False, ("HTTP 401 Unauthorized -- webhook URL appears "
                       "invalid or revoked")
    if r.status_code == 404:
        return False, ("HTTP 404 -- webhook does not exist (was it "
                       "deleted from the channel?)")
    return False, f"HTTP {r.status_code}: {(r.text or '')[:200]}"
