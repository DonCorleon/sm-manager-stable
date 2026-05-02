"""Backup engine.

Snapshot pipeline:
  EchoPort `bk <name>` -> server writes <name>.db beside the live world.db
  -> wait for file size to settle -> sqlite3 PRAGMA integrity_check
  -> move out of the worlds dir into data/backups/<instance>/
  -> gzip in place -> append entry to data/backups.json.

A "cluster snapshot" is a set of per-instance entries that share a
timestamp. They are written together by `make_snapshot()` plus a single
manager-config bundle (settings.toml, setup.toml, GameXishu*.json,
Engine.ini) at data/backups/config/manager_<ts>.tar.gz.

This module implements the storage + pipeline layer; scheduler, /backups
page, and restore flow live in their own modules.
"""

import gzip
import json
import logging
import shutil
import sqlite3
import tarfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from manager import echo, paths, updates
from manager.config import DATA_DIR, PROJECT_ROOT, get_setting
from manager.wizard import MAP_CLOUDMIST, MAP_SHIFTINGSANDS, RuntimeInstance

log = logging.getLogger(__name__)

# ── Layout ──────────────────────────────────────────────────────────────────

BACKUPS_DIR = DATA_DIR / "backups"
CONFIG_BUNDLE_DIR = BACKUPS_DIR / "config"
INDEX_PATH = DATA_DIR / "backups.json"

_FILE_SETTLE_POLL_SEC = 0.25
_FILE_SETTLE_REQUIRED_STABLE_CHECKS = 3
_DEFAULT_FILE_WAIT_TIMEOUT_SEC = 60

# Default: 3 retries with 3-min waits between. Spec'd in BACKUPS.md.
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_DELAY_SEC = 180

# Serialise index r/w so concurrent snapshots don't race on the JSON file.
_index_lock = threading.RLock()


# ── Op-progress tracking (shared with the UI) ───────────────────────────────


_op_lock = threading.Lock()
_op_label: Optional[str] = None       # human label like "manual snapshot"
_op_started_at: Optional[float] = None
_op_progress: str = ""                # latest stage description for the UI
_op_progress_lock = threading.Lock()


def try_begin_op(label: str) -> bool:
    """Non-blocking lock-and-mark. Returns True if this caller now holds
    the snapshot op slot; False if another op is already running. Caller
    MUST eventually call `_end_op()` (handled inside `run_snapshot_under_op`)."""
    if not _op_lock.acquire(blocking=False):
        return False
    global _op_label, _op_started_at
    _op_label = label
    _op_started_at = time.monotonic()
    _set_progress(f"{label}: starting...")
    _notify_dashboard()
    return True


def _end_op() -> None:
    global _op_label, _op_started_at
    _op_label = None
    _op_started_at = None
    _set_progress("")
    try:
        _op_lock.release()
    except RuntimeError:
        # Already released; harmless but worth noting.
        log.debug("_end_op: lock already released")
    _notify_dashboard()


def _notify_dashboard() -> None:
    try:
        from manager import dashboard_events
        dashboard_events.notify()
    except Exception:
        log.exception("dashboard notify raised (non-fatal)")


def _set_progress(msg: str) -> None:
    """Update the user-visible progress string. Cheap; called from each
    pipeline stage. Notifies dashboard / /backups SSE subscribers so the
    new progress text pushes without browser polling."""
    global _op_progress
    with _op_progress_lock:
        _op_progress = msg
    _notify_dashboard()


def current_op_status() -> dict:
    """Snapshot the current op state for the UI. Always cheap."""
    in_progress = _op_lock.locked()
    elapsed = (int(time.monotonic() - _op_started_at)
               if in_progress and _op_started_at else 0)
    with _op_progress_lock:
        progress = _op_progress
    return {
        "in_progress": in_progress,
        "label": _op_label if in_progress else None,
        "elapsed_seconds": elapsed,
        "progress": progress,
    }


def run_snapshot_under_op(running: list[tuple], source: str,
                          **kwargs) -> "list[SnapshotLeg]":
    """Execute `make_snapshot` while holding the op slot acquired by
    `try_begin_op`. Always releases the slot on exit (success or
    exception). Intended target for `threading.Thread`."""
    try:
        return make_snapshot(running, source, **kwargs)
    except Exception:
        log.exception("run_snapshot_under_op: snapshot raised")
        return []
    finally:
        _end_op()


def run_inline_pre_op_snapshot(source: str) -> bool:
    """Fire a synchronous snapshot from inside another op (pre-shutdown,
    pre-update). The caller is already holding lifecycle's op lock so we
    don't acquire it; we DO take backups' op lock for the brief window
    so the dashboard banner shows the snapshot progress.

    Uses a single attempt with a tight file-wait timeout so a stuck
    snapshot doesn't block the parent op for minutes. Returns True if
    every running instance got a clean leg.
    """
    # Local import to dodge circular dep.
    from manager import lifecycle
    running = lifecycle.running_instances()
    if not running:
        log.info("[%s] no running instances; skipping inline snapshot", source)
        return True
    if not try_begin_op(f"{source} snapshot"):
        log.warning("[%s] another backup op in progress; skipping pre-op "
                    "snapshot to avoid deadlock", source)
        return False
    try:
        legs = make_snapshot(running, source,
                             max_attempts=1, retry_delay_sec=0)
    except Exception:
        log.exception("[%s] inline snapshot raised", source)
        return False
    finally:
        _end_op()
    ok = bool(legs) and all(not leg.partial and leg.file for leg in legs)
    log.info("[%s] inline snapshot result: ok=%s legs=%d",
             source, ok, len(legs))
    return ok


# ── Public dataclass (returned to UI / scheduler) ───────────────────────────


@dataclass
class SnapshotLeg:
    """One per-instance result of a cluster snapshot attempt."""
    ts: str                       # cluster snapshot id, shared across legs
    instance: str                 # short name (e.g. "cloudmist")
    file: Optional[str]           # relative path under PROJECT_ROOT, or None on failure
    size_bytes: int
    build_id: Optional[str]
    source: str
    integrity: str                # "ok" | "corrupt" | "unchecked"
    partial: bool                 # True if any of the cluster legs failed
    pinned: bool
    config_bundle: Optional[str]  # relative path under PROJECT_ROOT
    error: Optional[str] = None   # populated when this leg failed entirely

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        if d.get("error") is None:
            d.pop("error", None)
        return d


# ── Naming ──────────────────────────────────────────────────────────────────


def instance_short(ri: RuntimeInstance) -> str:
    """Short stable name for filesystem paths. Uses canonical map shorts
    for the two known maps; falls back to lowercased map_name otherwise so
    any future maps still get a usable directory."""
    if ri.instance.map_name == MAP_CLOUDMIST:
        return "cloudmist"
    if ri.instance.map_name == MAP_SHIFTINGSANDS:
        return "shiftingsands"
    return ri.instance.map_name.lower()


def _new_snapshot_ts() -> str:
    """Cluster snapshot timestamp. Local time, sortable lex order."""
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def _snapshot_leg_name(ts: str) -> str:
    """The argument we pass to `bk` -- becomes the .db filename in the
    worlds dir before we move it out."""
    return f"manager_{ts}"


# ── Pipeline helpers ────────────────────────────────────────────────────────


def _wait_for_db_settled(path: Path, timeout_sec: float) -> bool:
    """Poll for `path` to exist and have a stable size for several
    consecutive checks (server has finished writing). True on success,
    False on timeout."""
    log.verbose("settle-wait: starting on %s (timeout=%.1fs, "
                "required-stable=%d, poll=%.2fs)",
                path, timeout_sec,
                _FILE_SETTLE_REQUIRED_STABLE_CHECKS, _FILE_SETTLE_POLL_SEC)
    deadline = time.monotonic() + timeout_sec
    last_size = -1
    stable = 0
    while time.monotonic() < deadline:
        try:
            sz = path.stat().st_size if path.exists() else -1
        except OSError as e:
            log.verbose("settle-wait: stat error on %s: %s -- treating "
                        "as not-ready", path, e)
            sz = -1
        if sz > 0 and sz == last_size:
            stable += 1
            log.verbose("settle-wait: %s size=%d (stable %d/%d)",
                        path, sz, stable, _FILE_SETTLE_REQUIRED_STABLE_CHECKS)
            if stable >= _FILE_SETTLE_REQUIRED_STABLE_CHECKS:
                log.debug("  file settled at %d bytes after %d stable checks: %s",
                          sz, stable, path)
                return True
        else:
            if sz != last_size:
                log.verbose("settle-wait: %s size %d -> %d (resetting stable)",
                            path, last_size, sz)
            stable = 0
            last_size = sz
        time.sleep(_FILE_SETTLE_POLL_SEC)
    log.warning("  file did not settle within %.1fs: %s", timeout_sec, path)
    return False


def _integrity_check(db_path: Path) -> str:
    """Run sqlite3 PRAGMA integrity_check on the .db file. Returns "ok" or
    "corrupt" (with details logged). Errors during the check itself
    (file unreadable, locked, etc.) also count as corrupt."""
    log.verbose("integrity_check: opening %s read-only (timeout=5s)", db_path)
    try:
        # uri=False; open read-only; short timeout so a server still using
        # the file doesn't hang us indefinitely (shouldn't happen since the
        # server already wrote the file out, but be defensive).
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            log.verbose("integrity_check: PRAGMA integrity_check on %s",
                        db_path)
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
        result = (row[0] or "").strip().lower() if row else ""
        log.verbose("integrity_check: raw result row=%r", row)
        if result == "ok":
            log.debug("  integrity_check: ok (%s)", db_path)
            return "ok"
        log.error("  integrity_check FAILED for %s: %s", db_path, row)
        return "corrupt"
    except sqlite3.Error as e:
        log.error("  integrity_check threw sqlite error for %s: %s", db_path, e)
        return "corrupt"
    except OSError as e:
        # Defensive: file vanished mid-check, permission flip, etc.
        log.error("  integrity_check OS error for %s: %s", db_path, e)
        return "corrupt"


def _gzip_file(src: Path, dst: Path, *, compresslevel: int = 6) -> None:
    """Stream src into a gzipped dst. Caller is responsible for unlinking
    src after a successful gzip if that's desired. Raises OSError on
    any IO failure -- caller must handle."""
    src_size = src.stat().st_size
    log.verbose("gzip: %s (%d bytes) -> %s (level=%d)",
                src, src_size, dst, compresslevel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fin, gzip.open(dst, "wb", compresslevel=compresslevel) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    out_size = dst.stat().st_size
    log.verbose("gzip: complete -- %d bytes (%.1f%% of source) at %s",
                out_size, 100.0 * out_size / max(src_size, 1), dst)


def _free_disk_gb(path: Path) -> float:
    """Free space on the filesystem holding `path`, in gigabytes.
    Returns 0.0 on any OS error rather than raising -- callers use the
    result for a soft warning, never for correctness."""
    try:
        usage = shutil.disk_usage(path)
        gb = usage.free / (1024 ** 3)
        log.verbose("disk_free: %s -> %.2f GB", path, gb)
        return gb
    except OSError as e:
        log.warning("disk_free: could not stat %s (%s) -- returning 0.0",
                    path, e)
        return 0.0


def _rel_to_project(p: Path) -> str:
    """Format a path as PROJECT_ROOT-relative POSIX so the index file is
    portable (operator can move the manager dir without rewriting paths)."""
    try:
        return p.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return p.resolve().as_posix()


# ── Manager-config bundle ───────────────────────────────────────────────────


def _capture_config_bundle(ts: str) -> Optional[Path]:
    """Capture settings.toml (which includes the wizard's [server] section)
    + GameXishu*.json + Engine.ini into a single tar.gz at
    data/backups/config/manager_<ts>.tar.gz. Returns the path, or None if
    nothing was captured (rare)."""
    out = CONFIG_BUNDLE_DIR / f"manager_{ts}.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True)

    settings_path = DATA_DIR / "settings.toml"

    captured = []
    with tarfile.open(out, "w:gz") as t:
        if settings_path.exists():
            t.add(settings_path, arcname="settings.toml")
            captured.append("settings.toml")

        # GameXishu*.json -- copy whatever exists in the live config dir.
        try:
            saved_dir = paths.saved_dir()
            gp_dir = saved_dir / "GameplaySettings"
            if gp_dir.is_dir():
                for j in sorted(gp_dir.glob("GameXishu*.json")):
                    t.add(j, arcname=f"GameplaySettings/{j.name}")
                    captured.append(f"GameplaySettings/{j.name}")
            # Engine.ini -- read-only snapshot.
            engine_ini = paths.engine_ini_path()
            if engine_ini.exists():
                t.add(engine_ini, arcname="Config/WindowsServer/Engine.ini")
                captured.append("Config/WindowsServer/Engine.ini")
        except Exception:
            # paths require settings.toml to be present; if anything throws
            # we still want the rest of the bundle written.
            log.exception("config bundle: error reading server-side config files")

    if not captured:
        log.warning("config bundle: nothing captured -- removing empty archive %s", out)
        out.unlink(missing_ok=True)
        return None

    log.info("config bundle: %d files -> %s (%d bytes)",
             len(captured), out, out.stat().st_size)
    return out


# ── Index r/w ───────────────────────────────────────────────────────────────


def _empty_index() -> dict:
    return {"snapshots": []}


def _load_index() -> dict:
    """Read backups.json. Returns an empty index if the file doesn't exist
    or fails to parse (operator can recover by hand if needed; we don't
    want a corrupt index to block new backups)."""
    with _index_lock:
        if not INDEX_PATH.exists():
            return _empty_index()
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.error("backups.json unreadable (%s); returning empty index. "
                      "Existing snapshot files on disk are untouched.", e)
            return _empty_index()


def _save_index(idx: dict) -> None:
    """Atomically write the index back to disk. Raises OSError on
    persistent failure (caller decides whether to retry / abort)."""
    with _index_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_PATH.with_suffix(".json.tmp")
        n_snaps = len(idx.get("snapshots", []))
        log.verbose("index save: %d snapshot(s) -> %s (via %s)",
                    n_snaps, INDEX_PATH, tmp.name)
        try:
            tmp.write_text(json.dumps(idx, indent=2), encoding="utf-8")
            tmp.replace(INDEX_PATH)
            log.verbose("index save: complete")
        except OSError as e:
            log.error("index save FAILED: %s -- index may be stale on disk",
                      e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _append_snapshot_entries(legs: list[SnapshotLeg]) -> None:
    """Append legs to the index, atomically. If the index write fails,
    the legs are NOT in the index but the .gz files exist on disk --
    the boot-time orphan reconciliation pass picks those up."""
    with _index_lock:
        idx = _load_index()
        for leg in legs:
            idx["snapshots"].append(leg.to_dict())
            log.verbose("index append: %s/%s file=%s size=%d",
                        leg.ts, leg.instance, leg.file, leg.size_bytes)
        try:
            _save_index(idx)
        except OSError:
            log.error("Failed to persist %d new index entr(ies); the "
                      "underlying .gz files are on disk and will be "
                      "discovered by orphan reconciliation on next boot.",
                      len(legs))
            raise


# ── Per-instance leg ────────────────────────────────────────────────────────


def _run_bk_leg(ri: RuntimeInstance,
                worlds_dir: Path,
                ts: str,
                source: str,
                config_bundle: Optional[Path],
                file_wait_timeout_sec: float = _DEFAULT_FILE_WAIT_TIMEOUT_SEC,
                ) -> Optional[SnapshotLeg]:
    """One instance's leg of a cluster snapshot.

    Steps: send `bk manager_<ts>` -> wait for the .db to land in
    `worlds_dir` -> integrity check -> gzip into
    `data/backups/<instance>/manager_<ts>.db.gz` -> return SnapshotLeg.

    Returns None on any failure (integrity / timeout / EchoPort error /
    gzip). The caller decides whether to retry this leg.
    """
    short = instance_short(ri)
    leg_name = _snapshot_leg_name(ts)
    src_db = worlds_dir / f"{leg_name}.db"
    dst_dir = BACKUPS_DIR / short
    dst_gz = dst_dir / f"{leg_name}.db.gz"

    log.info("[%s] bk leg starting (ts=%s, source=%s)", short, ts, source)
    log.info("[%s]   target src: %s", short, src_db)
    log.info("[%s]   target dst: %s", short, dst_gz)

    log.verbose("[%s] _run_bk_leg phase=stale-check src_db=%s exists=%s",
                short, src_db, src_db.exists())
    # If a stale file from a prior aborted run exists, clear it so we know
    # the bytes we read came from this run.
    if src_db.exists():
        log.warning("[%s] stale source DB exists at %s -- removing before bk",
                    short, src_db)
        try:
            src_db.unlink()
        except OSError as e:
            log.error("[%s] failed to remove stale source DB: %s -- aborting leg",
                      short, e)
            return None

    # Issue the bk command.
    log.verbose("[%s] _run_bk_leg phase=echo-bk port=%d cmd='bk %s'",
                short, ri.instance.echo_port, leg_name)
    try:
        resp = echo.send_command("127.0.0.1", ri.instance.echo_port,
                                 f"bk {leg_name}")
        cleaned = resp.replace("\r", " ").replace("\n", " ")[:200].strip()
        log.info("[%s]   EchoPort bk response: %s", short,
                 cleaned or "(empty)")
    except (OSError, ConnectionError) as e:
        log.error("[%s] EchoPort unreachable (%s) -- bk leg failed", short, e)
        return None

    # Wait for the file to land + settle.
    log.verbose("[%s] _run_bk_leg phase=wait-settle target=%s timeout=%.0fs",
                short, src_db, file_wait_timeout_sec)
    if not _wait_for_db_settled(src_db, file_wait_timeout_sec):
        log.error("[%s] bk file never settled at %s within %.0fs",
                  short, src_db, file_wait_timeout_sec)
        return None

    try:
        size_bytes_raw = src_db.stat().st_size
    except OSError as e:
        log.error("[%s] could not stat source DB after settle: %s -- "
                  "leg failed", short, e)
        return None
    log.info("[%s]   source DB landed: %d bytes", short, size_bytes_raw)

    # Integrity check.
    log.verbose("[%s] _run_bk_leg phase=integrity-check", short)
    integrity = _integrity_check(src_db)
    if integrity != "ok":
        log.error("[%s] integrity check FAILED -- discarding source, "
                  "leg failed", short)
        try:
            src_db.unlink()
        except OSError as e:
            log.warning("[%s] could not remove corrupt source after "
                        "integrity fail: %s", short, e)
        return None

    # Move + gzip. Stream-gzip directly from src to dst, then unlink src.
    log.verbose("[%s] _run_bk_leg phase=gzip src=%s dst=%s",
                short, src_db, dst_gz)
    try:
        _gzip_file(src_db, dst_gz)
    except OSError as e:
        log.error("[%s] gzip failed: %s -- leg failed", short, e)
        # Clean up partial output so it doesn't get picked up by the
        # orphan-reconciliation pass at next boot.
        try:
            dst_gz.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    try:
        src_db.unlink()
    except OSError as e:
        log.warning("[%s] could not remove source DB after gzip "
                    "(snapshot still valid): %s", short, e)

    try:
        size_gz = dst_gz.stat().st_size
    except OSError as e:
        log.error("[%s] gzip output vanished mid-pipeline: %s", short, e)
        return None
    log.info("[%s]   gzipped: %d bytes (%.1f%% of source)",
             short, size_gz, 100.0 * size_gz / max(size_bytes_raw, 1))

    build_id = updates.read_local_buildid()
    bundle_rel = _rel_to_project(config_bundle) if config_bundle else None

    leg = SnapshotLeg(
        ts=ts,
        instance=short,
        file=_rel_to_project(dst_gz),
        size_bytes=size_gz,
        build_id=build_id,
        source=source,
        integrity=integrity,
        partial=False,         # set by caller after all legs known
        pinned=False,
        config_bundle=bundle_rel,
    )
    log.info("[%s] bk leg COMPLETE: %s", short, leg.file)
    return leg


# ── Cluster orchestrator ────────────────────────────────────────────────────


def make_snapshot(running: list[tuple],
                  source: str,
                  *,
                  max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
                  retry_delay_sec: int = _DEFAULT_RETRY_DELAY_SEC,
                  file_wait_timeout_sec: float = _DEFAULT_FILE_WAIT_TIMEOUT_SEC,
                  ) -> list[SnapshotLeg]:
    """Run one cluster snapshot across all currently-running instances.

    `running` is list of (RuntimeInstance, InstanceProcess|None). Only the
    RuntimeInstance is read here (worlds_dir derived from map_name); the
    process handle is accepted for caller convenience and ignored.

    Per-instance retry: each leg is attempted up to `max_attempts` times
    with `retry_delay_sec` between attempts. After all attempts are
    exhausted, any still-failed legs are recorded as failed entries with
    `partial=True` set on every entry in the snapshot (the spec keeps a
    partial snapshot rather than discarding the successful legs).

    Synchronous: blocks the calling thread until done. Callers (route
    handler, scheduler) should run it in a daemon thread.
    """
    if not running:
        log.warning("make_snapshot: no running instances -- nothing to do")
        return []

    ts = _new_snapshot_ts()
    log.info("=" * 50)
    log.info("CLUSTER SNAPSHOT %s starting (source=%s, %d instance(s))",
             ts, source, len(running))
    log.info("=" * 50)
    _set_progress(f"snapshot {ts}: starting ({len(running)} instance(s))")

    # Disk-free pre-check. Spec: abort with logged warning if low.
    warn_gb = float(get_setting("backups.disk_free_warn_gb", 2.0))
    free_gb = _free_disk_gb(DATA_DIR)
    if free_gb < warn_gb:
        log.error("ABORTING snapshot %s: only %.2f GB free on %s "
                  "(threshold %.2f GB). Free up disk and retry.",
                  ts, free_gb, DATA_DIR, warn_gb)
        _set_progress(f"snapshot {ts}: ABORTED (low disk: {free_gb:.2f} GB)")
        return []
    log.info("  disk-free check ok: %.2f GB free (threshold %.2f GB)",
             free_gb, warn_gb)

    # Source-specific pre-snapshot broadcast. Scheduled sources got their
    # T-3min / T-30sec warnings via the scheduler. Manual snapshots are
    # operator-driven and otherwise silent -- we still want connected
    # players to know why the brief tick-rate lag is happening, so we
    # fire one immediate say.
    if source == "manual":
        try:
            from manager import broadcasts
            broadcasts.say_to_all_running(
                "Saving world snapshot -- brief lag possible."
            )
        except Exception:
            log.exception("manual-snapshot broadcast raised (non-fatal)")

    # Capture the config bundle once for the whole cluster snapshot.
    _set_progress(f"snapshot {ts}: capturing manager config bundle")
    bundle = _capture_config_bundle(ts)

    # Plan legs: instance_short -> (RuntimeInstance, worlds_dir).
    plan: dict[str, tuple[RuntimeInstance, Path]] = {}
    for ri, _proc in running:
        worlds_dir = paths.world_db_path(ri.instance.map_name).parent
        plan[instance_short(ri)] = (ri, worlds_dir)

    # Per-leg retry loop -- run all legs in parallel within each attempt.
    results: dict[str, SnapshotLeg] = {}
    pending = set(plan.keys())

    for attempt in range(1, max_attempts + 1):
        log.info("snapshot %s: attempt %d/%d for %d leg(s): %s",
                 ts, attempt, max_attempts, len(pending), sorted(pending))
        log.verbose("snapshot %s: attempt %d plan=%s",
                    ts, attempt,
                    {s: str(plan[s][1]) for s in pending})
        _set_progress(f"snapshot {ts}: attempt {attempt}/{max_attempts} -- "
                      f"running bk on {sorted(pending)}")

        threads = []
        attempt_results: dict[str, Optional[SnapshotLeg]] = {}
        result_lock = threading.Lock()

        def _worker(short: str, ri: RuntimeInstance, worlds_dir: Path) -> None:
            leg = _run_bk_leg(ri, worlds_dir, ts, source, bundle,
                              file_wait_timeout_sec=file_wait_timeout_sec)
            with result_lock:
                attempt_results[short] = leg

        for short in pending:
            ri, worlds_dir = plan[short]
            t = threading.Thread(
                target=_worker, args=(short, ri, worlds_dir),
                daemon=True, name=f"bk-{short}-{ts}",
            )
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        # Collect successes.
        for short, leg in attempt_results.items():
            if leg is not None:
                results[short] = leg
                pending.discard(short)

        if not pending:
            log.info("snapshot %s: all %d leg(s) succeeded on attempt %d",
                     ts, len(results), attempt)
            break

        if attempt < max_attempts:
            log.warning("snapshot %s: %d leg(s) still failing (%s) -- "
                        "waiting %ds before retry",
                        ts, len(pending), sorted(pending), retry_delay_sec)
            _set_progress(f"snapshot {ts}: {len(pending)} leg(s) failed "
                          f"({sorted(pending)}) -- retrying in {retry_delay_sec}s")
            time.sleep(retry_delay_sec)

    # Determine partial flag.
    partial = bool(pending)
    if partial:
        log.error("snapshot %s: PARTIAL after %d attempts -- legs still "
                  "failing: %s. Successful legs are preserved.",
                  ts, max_attempts, sorted(pending))

    # Mark successful legs partial=True if any leg failed; index them.
    for leg in results.values():
        leg.partial = partial

    # Build placeholder entries for failed legs so the UI sees them.
    failed_entries: list[SnapshotLeg] = []
    for short in pending:
        ri, _ = plan[short]
        failed_entries.append(SnapshotLeg(
            ts=ts,
            instance=short,
            file=None,
            size_bytes=0,
            build_id=updates.read_local_buildid(),
            source=source,
            integrity="unchecked",
            partial=True,
            pinned=False,
            config_bundle=_rel_to_project(bundle) if bundle else None,
            error="all retry attempts failed",
        ))

    all_entries = list(results.values()) + failed_entries
    _append_snapshot_entries(all_entries)

    # Rotation: prune oldest unpinned legs per-instance down to
    # backups.keep_last_n. Pinned entries are exempt and don't count
    # against the limit. Only prunes if the snapshot produced at least
    # one new entry (a pure-failure run skips rotation).
    if results:
        try:
            keep = int(get_setting("backups.keep_last_n", 48))
            _enforce_rotation(keep)
        except Exception:
            log.exception("snapshot %s: rotation pass raised (non-fatal)", ts)

    log.info("=" * 50)
    log.info("CLUSTER SNAPSHOT %s %s: %d ok, %d failed",
             ts, "PARTIAL" if partial else "COMPLETE",
             len(results), len(pending))
    log.info("=" * 50)
    _set_progress(f"snapshot {ts}: "
                  f"{'PARTIAL' if partial else 'complete'} "
                  f"-- {len(results)} ok, {len(pending)} failed")
    return all_entries


# ── Read API for /backups page (used in step 3) ─────────────────────────────


def list_snapshots() -> list[dict]:
    """Return all snapshot entries newest-first."""
    idx = _load_index()
    snaps = list(idx.get("snapshots", []))
    snaps.sort(key=lambda s: s.get("ts", ""), reverse=True)
    return snaps


def reconcile_orphans() -> dict:
    """Walk `data/backups/<short>/*.db.gz` for files NOT referenced by
    any leg in the index, and `data/backups/config/*.tar.gz` for
    bundles not referenced by any snapshot.

    Used at manager boot to catch the rare case where the manager
    crashed between gzip-write and index-append (so the gz file is on
    disk but no leg points to it).

    Conservative: we LOG the orphans at WARNING with their paths, and
    we DO NOT delete them automatically -- the operator decides. They
    can either re-index by hand (recreate a leg pointing at the file)
    or `del` them once they've confirmed nothing important is there.

    Returns a dict { instances: {short: [paths]}, config_bundles: [paths] }
    so callers (boot scripts, future UI) can surface the report.
    """
    report: dict[str, object] = {"instances": {}, "config_bundles": []}
    if not BACKUPS_DIR.exists():
        log.verbose("orphan-reconcile: %s does not exist; nothing to do",
                    BACKUPS_DIR)
        return report

    # Index entries currently referenced.
    idx = _load_index()
    referenced_files: set[str] = set()
    referenced_bundles: set[str] = set()
    for s in idx.get("snapshots", []):
        f = s.get("file")
        if f:
            referenced_files.add((PROJECT_ROOT / f).resolve().as_posix())
        b = s.get("config_bundle")
        if b:
            referenced_bundles.add((PROJECT_ROOT / b).resolve().as_posix())

    # Walk per-instance subdirs.
    for inst_dir in BACKUPS_DIR.iterdir():
        if not inst_dir.is_dir():
            continue
        if inst_dir.name in ("config",):
            continue
        orphans = []
        for f in inst_dir.glob("*.db.gz"):
            key = f.resolve().as_posix()
            if key not in referenced_files:
                orphans.append(_rel_to_project(f))
        if orphans:
            report["instances"][inst_dir.name] = orphans
            log.warning("orphan-reconcile: %s has %d orphan .db.gz "
                        "file(s) with no index entry: %s",
                        inst_dir.name, len(orphans), orphans)

    # Config bundles
    if CONFIG_BUNDLE_DIR.exists():
        for f in CONFIG_BUNDLE_DIR.glob("*.tar.gz"):
            key = f.resolve().as_posix()
            if key not in referenced_bundles:
                rel = _rel_to_project(f)
                report["config_bundles"].append(rel)  # type: ignore[union-attr]
                log.warning("orphan-reconcile: orphan config bundle "
                            "with no referencing leg: %s", rel)

    if not report["instances"] and not report["config_bundles"]:
        log.verbose("orphan-reconcile: no orphans found")
    else:
        log.info("orphan-reconcile: found %d orphan instance(s) and %d "
                 "orphan config bundle(s). Files were NOT deleted; "
                 "operator should review and decide.",
                 len(report["instances"]),
                 len(report["config_bundles"]))  # type: ignore[arg-type]
    return report


def get_snapshot_legs(ts: str) -> list[dict]:
    """All legs for a given cluster snapshot ts (one per instance)."""
    return [s for s in list_snapshots() if s.get("ts") == ts]


def set_pinned(ts: str, instance: str, pinned: bool) -> bool:
    """Toggle pin flag on a single leg. Returns True if the entry existed."""
    with _index_lock:
        idx = _load_index()
        hit = False
        for s in idx.get("snapshots", []):
            if s.get("ts") == ts and s.get("instance") == instance:
                s["pinned"] = bool(pinned)
                hit = True
        if hit:
            _save_index(idx)
            log.info("snapshot %s/%s pinned=%s", ts, instance, pinned)
        return hit


# ── Restore ─────────────────────────────────────────────────────────────────


def _cold_copy_leg(ri: RuntimeInstance, ts: str,
                   source: str) -> Optional[SnapshotLeg]:
    """Snapshot leg for a STOPPED instance: read the live world.db
    directly off disk, integrity-check, gzip into the backups dir.

    The bk-via-EchoPort path needs the server alive; this is the
    equivalent for instances that are down. Used by restore's
    pre-restore safety snapshot when the affected instance isn't
    currently running.
    """
    short = instance_short(ri)
    leg_name = _snapshot_leg_name(ts)
    src_db = paths.world_db_path(ri.instance.map_name)
    dst_dir = BACKUPS_DIR / short
    dst_gz = dst_dir / f"{leg_name}.db.gz"

    log.info("[%s] cold-copy leg starting (ts=%s, source=%s)",
             short, ts, source)
    if not src_db.exists():
        log.warning("[%s] no live world.db at %s -- skipping cold copy",
                    short, src_db)
        return None

    size_bytes_raw = src_db.stat().st_size
    log.info("[%s]   src: %s (%d bytes, mtime=%s)",
             short, src_db, size_bytes_raw,
             datetime.fromtimestamp(src_db.stat().st_mtime).isoformat(timespec="seconds"))

    # Integrity check the file before we trust it as a backup. If the
    # operator is restoring AFTER a crash that left the live db corrupt,
    # we DO want to know -- so we record the integrity result, but we
    # still write the backup either way (it's the only record of that
    # state and the operator might want it).
    integrity = _integrity_check(src_db)
    if integrity != "ok":
        log.warning("[%s] live db integrity check NOT ok (%s) -- backup "
                    "still being written so the operator has a record",
                    short, integrity)

    try:
        _gzip_file(src_db, dst_gz)
    except OSError as e:
        log.error("[%s] cold-copy gzip failed: %s", short, e)
        return None

    size_gz = dst_gz.stat().st_size
    log.info("[%s]   gzipped: %d bytes (%.1f%% of source)",
             short, size_gz, 100.0 * size_gz / max(size_bytes_raw, 1))

    return SnapshotLeg(
        ts=ts,
        instance=short,
        file=_rel_to_project(dst_gz),
        size_bytes=size_gz,
        build_id=updates.read_local_buildid(),
        source=source,
        integrity=integrity,
        partial=False,
        pinned=False,
        config_bundle=None,
    )


def restore_snapshot(ts: str,
                     instance_shorts: list[str],
                     include_manager_config: bool = False,
                     ) -> dict:
    """Restore one or more legs of a cluster snapshot, preserving the
    pre-restore running state of each affected instance.

    For each requested instance:
      0. Pre-restore safety snapshot (always, before anything destructive):
          - if RUNNING: bk via EchoPort
          - if STOPPED: cold copy of live world.db
         These get written to the index with source="pre-restore" so
         they show up on /backups as a one-click rollback.
      1. (only if running) graceful single-instance stop.
      2. Decompress the selected snapshot's .db.gz into the live worlds
         dir as world.db.
      3. (only if was running before) start the instance back up.

    Servers that were stopped going in stay stopped after the restore.
    Servers that were running get restarted in canonical cluster order
    (main first so the cluster-link port is up before the child).

    If include_manager_config is True, ALSO extract the source snapshot's
    config bundle over the manager config locations after step 2.

    Caller is expected NOT to hold lifecycle's op lock; this function
    acquires it for the duration of the operation.

    Returns a dict { ok: bool, instances: {short: status_str}, error: str|None }.
    """
    from manager import lifecycle

    log.info("=" * 50)
    log.info("RESTORE %s starting (instances=%s, include_config=%s)",
             ts, instance_shorts, include_manager_config)
    log.info("=" * 50)

    if not lifecycle.try_acquire_op_lock(f"restore {ts}"):
        return {"ok": False, "error": "another op in progress",
                "instances": {}}

    # Reorder the operator's selection into canonical cluster order: main
    # first, child second. The stop phase uses reverse (child first); the
    # start phase uses forward (main first). Form order is NOT a reliable
    # proxy for cluster role -- legs land in the index in thread-finish
    # order during the original snapshot.
    canonical_order: list[str] = []
    try:
        from manager.config import load_settings
        from manager.wizard import active_runtime_instances, load_existing
        config = load_existing(load_settings())
        for ri in active_runtime_instances(config):
            short = instance_short(ri)
            if short in instance_shorts:
                canonical_order.append(short)
    except Exception:
        log.exception("restore: could not derive canonical order; "
                      "falling back to form order")
        canonical_order = list(instance_shorts)
    if canonical_order != list(instance_shorts):
        log.info("restore: reordering instances to canonical %s "
                 "(form was %s)", canonical_order, instance_shorts)

    instance_shorts = canonical_order

    # Snapshot per-instance running state ONCE so we don't fight a race
    # later in the flow (e.g. an instance that was running when we checked
    # but exits during the pre-restore phase).
    running_now = {instance_short(ri)
                   for ri, _ in lifecycle.running_instances()}
    selected_running = [s for s in instance_shorts if s in running_now]
    selected_stopped = [s for s in instance_shorts if s not in running_now]
    log.info("restore: per-instance state: running=%s, stopped=%s",
             selected_running, selected_stopped)

    stop_order = [s for s in reversed(canonical_order) if s in selected_running]
    start_order = [s for s in canonical_order if s in selected_running]

    result: dict[str, str] = {short: "pending" for short in instance_shorts}
    err: Optional[str] = None
    try:
        # Source-snapshot lookup.
        legs = get_snapshot_legs(ts)
        if not legs:
            err = f"snapshot {ts} not found"
            log.error("restore: %s", err)
            return {"ok": False, "error": err, "instances": result}
        legs_by_short = {l["instance"]: l for l in legs}

        # ── PHASE 0: pre-restore safety snapshot ────────────────────────
        # Always taken, regardless of whether the affected instances are
        # up or down. Lands in the index so it shows on /backups as the
        # operator's one-click rollback path.
        pre_ts = _new_snapshot_ts()
        pre_legs: list[SnapshotLeg] = []
        log.info("restore: PHASE 0 -- pre-restore safety snapshot ts=%s",
                 pre_ts)

        # Broadcast to running instances only (no point talking to a
        # stopped server).
        if selected_running:
            try:
                from manager import broadcasts
                for short in selected_running:
                    ri = _find_runtime_instance_by_short(short)
                    if ri is None:
                        continue
                    broadcasts.say_to(
                        ri,
                        "Restoring world from backup -- you will be disconnected "
                        "briefly. Reconnect in 1-2 minutes."
                    )
            except Exception:
                log.exception("restore: pre-restore broadcast raised "
                              "(non-fatal)")

        for short in instance_shorts:
            ri = _find_runtime_instance_by_short(short)
            if ri is None:
                log.error("[restore/%s] no runtime instance found -- "
                          "skipping pre-restore leg", short)
                continue
            if short in selected_running:
                worlds_dir = paths.world_db_path(ri.instance.map_name).parent
                leg = _run_bk_leg(ri, worlds_dir, pre_ts, "pre-restore",
                                  config_bundle=None)
            else:
                leg = _cold_copy_leg(ri, pre_ts, "pre-restore")
            if leg is not None:
                pre_legs.append(leg)
            else:
                log.warning("[restore/%s] pre-restore leg failed -- "
                            "continuing without rollback target for this leg",
                            short)
        if pre_legs:
            _append_snapshot_entries(pre_legs)
            log.info("restore: pre-restore snapshot %s saved (%d leg(s))",
                     pre_ts, len(pre_legs))

        # ── PHASE 1: stop running selected instances (child first) ─────
        log.info("restore: PHASE 1 -- stop %s", stop_order)
        log.verbose("restore: PHASE 1 entering with running=%s stopped=%s",
                    selected_running, selected_stopped)
        for short in stop_order:
            ri = _find_runtime_instance_by_short(short)
            if ri is None:
                continue
            if not lifecycle.stop_single_instance(ri.instance.map_name,
                                                  countdown_sec=1):
                result[short] = "stop-failed"
                err = err or f"could not stop {short}"
                log.error("[restore/%s] stop failed", short)

        # ── PHASE 2: swap DBs for ALL selected ──────────────────────────
        log.info("restore: PHASE 2 -- swap DBs")
        for short in instance_shorts:
            if result[short] == "stop-failed":
                continue
            leg = legs_by_short.get(short)
            if leg is None:
                result[short] = "no-such-leg"
                continue
            if not leg.get("file"):
                result[short] = "leg-failed (no file)"
                continue
            ri = _find_runtime_instance_by_short(short)
            if ri is None:
                result[short] = "no-runtime-mapping"
                continue

            map_name = ri.instance.map_name
            world_db = paths.world_db_path(map_name)
            backup_gz = (PROJECT_ROOT / leg["file"]).resolve()
            if not backup_gz.exists():
                result[short] = "backup-file-missing"
                log.error("[restore/%s] backup file missing on disk: %s",
                          short, backup_gz)
                continue

            log.info("[restore/%s] world_db=%s backup_gz=%s",
                     short, world_db, backup_gz)
            ok, status = _safe_restore_to_world_db(backup_gz, world_db)
            if not ok:
                result[short] = status
                log.error("[restore/%s] safe-restore failed: %s "
                          "(live world.db is intact)", short, status)
                continue
            result[short] = "restored"

        # ── PHASE 2b: manager config restoration ───────────────────────
        if include_manager_config:
            cfg_bundle = legs[0].get("config_bundle")
            if cfg_bundle:
                bundle_path = (PROJECT_ROOT / cfg_bundle).resolve()
                if bundle_path.exists():
                    try:
                        _restore_manager_config(bundle_path)
                        log.info("[restore] manager config restored from %s",
                                 bundle_path)
                        result["__manager_config__"] = "restored"
                    except OSError as e:
                        log.error("[restore] could not restore config bundle: %s", e)
                        result["__manager_config__"] = f"failed: {e}"
                else:
                    log.error("[restore] config bundle missing on disk: %s",
                              bundle_path)
                    result["__manager_config__"] = "missing"
            else:
                log.warning("[restore] no config bundle in snapshot")
                result["__manager_config__"] = "no-bundle-in-snapshot"

        # ── PHASE 3: restart only previously-running instances ─────────
        # Instances that were stopped going in stay stopped (per spec).
        log.info("restore: PHASE 3 -- restart %s (others stay stopped)",
                 start_order)
        for short in start_order:
            if result.get(short) != "restored":
                continue
            ri = _find_runtime_instance_by_short(short)
            if ri is None:
                continue
            if lifecycle.start_single_instance(ri.instance.map_name):
                result[short] = "restored+started"
            else:
                result[short] = "restored (start FAILED)"

        # Mark stopped-and-stayed-stopped instances explicitly so the
        # caller / log reader can see the path that was taken.
        for short in selected_stopped:
            if result.get(short) == "restored":
                result[short] = "restored (kept stopped)"

        # Re-apply persisted login-lock state on the instances we just
        # brought back. A fresh server boot defaults to unlocked; if the
        # operator had cluster or per-instance lock on, the restored
        # instance must come up locked too. _start_impl already does
        # this for normal Start ops; restore goes through start_single
        # which does NOT, so we call it here.
        if start_order:
            try:
                lifecycle._reapply_login_locks_safe()
            except Exception:
                log.exception("restore: post-start login-lock apply raised "
                              "(non-fatal)")

        ok = all(result.get(s, "").startswith("restored")
                 for s in instance_shorts)

        # Mark the source snapshot's legs in the index with the restore
        # timestamp. Cluster-wide stamp -- every leg of the snapshot
        # gets the stamp, regardless of which legs were actually picked
        # in the dialog. The stamp is what protects the snapshot from
        # Clear-all-but-latest until a newer backup is taken.
        try:
            _mark_legs_restored(ts)
        except Exception:
            log.exception("restore: could not mark source snapshot "
                          "(non-fatal)")

        log.info("=" * 50)
        log.info("RESTORE %s %s: %s (pre-restore snapshot=%s)",
                 ts, "COMPLETE" if ok else "PARTIAL/FAILED",
                 result, pre_ts if pre_legs else "(none)")
        log.info("=" * 50)
        return {"ok": ok, "error": err, "instances": result,
                "pre_restore_ts": pre_ts if pre_legs else None}
    finally:
        lifecycle.release_op_lock()


def _mark_legs_restored(source_ts: str) -> None:
    """Stamp every leg of `source_ts` with restored_at=now. Cluster-wide:
    even legs the operator didn't pick in the restore dialog get the
    stamp, because the cluster moves as one unit -- if the operator
    rolled back cloudmist's world to S, they almost certainly want
    shiftingsands' leg of S preserved as a safety target too.

    The stamp serves two purposes:
      1. /backups page shows '↻ restored from <ts>' on the source row.
      2. Clear-all-but-latest treats the snapshot as protected (cluster-
         level) until any newer snapshot lands and consumes the stamp."""
    now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _index_lock:
        idx = _load_index()
        any_change = False
        for s in idx.get("snapshots", []):
            if s.get("ts") != source_ts:
                continue
            s["restored_at"] = now_iso
            any_change = True
        if any_change:
            _save_index(idx)
            log.info("Marked snapshot %s as restored at %s "
                     "(cluster-wide stamp on all legs)",
                     source_ts, now_iso)


def _ts_to_dt(ts_str: str) -> "Optional[datetime]":
    """Parse a snapshot ts (`YYYY-MM-DD_HHMMSS`). Returns None on bad input."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d_%H%M%S")
    except (ValueError, TypeError):
        return None


def _restored_at_to_dt(s: str) -> "Optional[datetime]":
    """Parse a restored_at timestamp (`YYYY-MM-DD HH:MM:SS`). None on bad input."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _protected_snapshot_tses(snapshots: list[dict]) -> set[str]:
    """Return the set of cluster snapshot tses currently under
    'restored-from' protection.

    A snapshot ts is protected iff:
      - At least one of its legs has `restored_at` set, AND
      - No snapshot anywhere in the index has a ts later than that
        `restored_at` (i.e. the protection has not yet been consumed by
        a fresher backup).

    Cluster-level by design: the rule is the same for both legs of a
    cluster snapshot, so both legs survive Clear-all-but-latest as a
    unit until a newer backup is taken.
    """
    if not snapshots:
        return set()
    # Latest ts across the whole index, EXCLUDING pre-restore snapshots
    # themselves. The pre-restore snapshot is created moments before the
    # restore at PHASE 0, so its ts is necessarily > the restored_at it
    # protects. If we let it count toward "latest_ts", the source
    # snapshot's protection is consumed by the very safety net we just
    # took for it -- which would make the protection feature useless on
    # the very next Clear-all click.
    #
    # Rationale: a fresh manual / scheduled / post-logoff snapshot
    # legitimately means "the operator is doing new work; the old
    # restored-from snapshot is no longer special." A pre-restore
    # snapshot just means "we're about to restore"; that's a side-effect
    # of the restore itself, not new work.
    latest_ts_dt = None
    for s in snapshots:
        if s.get("source") == "pre-restore":
            continue
        dt = _ts_to_dt(s.get("ts", ""))
        if dt is not None and (latest_ts_dt is None or dt > latest_ts_dt):
            latest_ts_dt = dt
    if latest_ts_dt is None:
        # No non-pre-restore snapshots -- everything restored-from is
        # protected.
        protected: set[str] = set()
        for s in snapshots:
            if s.get("restored_at") and s.get("ts"):
                protected.add(s["ts"])
        return protected

    # For each ts, find the first restored_at present on any of its
    # legs (legs of the same ts share one stamp by design).
    restored_by_ts: dict[str, str] = {}
    for s in snapshots:
        rat = s.get("restored_at")
        ts = s.get("ts", "")
        if rat and ts and ts not in restored_by_ts:
            restored_by_ts[ts] = rat

    protected: set[str] = set()
    for ts, rat in restored_by_ts.items():
        rat_dt = _restored_at_to_dt(rat)
        if rat_dt is None:
            continue
        # Protected if NO snapshot in the index is fresher than the
        # restore time. Equivalently: latest_ts_dt <= rat_dt.
        if latest_ts_dt <= rat_dt:
            protected.add(ts)
    return protected


def delete_all_except_latest() -> int:
    """Per-instance: keep the newest non-pinned leg, plus all pinned
    legs, plus all legs of snapshots under cluster-wide restored-from
    protection. Delete everything else. Returns count deleted.

    Cluster-protection rule: if a snapshot has been restored from and
    no newer backup has landed since the restore, that snapshot's
    legs (all of them) survive Clear-all-but-latest. As soon as any
    fresher backup lands, the protection is consumed and the next
    Clear-all will sweep the previously-protected snapshot.
    """
    with _index_lock:
        idx = _load_index()
        snaps = list(idx.get("snapshots", []))
        protected_tses = _protected_snapshot_tses(snaps)
        if protected_tses:
            log.info("clear-all: cluster-protected tses (skipped from prune): %s",
                     sorted(protected_tses))

        # Group by instance
        by_instance: dict[str, list[dict]] = {}
        for s in snaps:
            by_instance.setdefault(s.get("instance", "?"), []).append(s)

        to_remove: list[dict] = []
        for inst, legs in by_instance.items():
            legs.sort(key=lambda s: s.get("ts", ""), reverse=True)
            unpinned = [s for s in legs if not s.get("pinned")]
            newest_unpinned_ts = unpinned[0].get("ts") if unpinned else None
            for leg in legs:
                if leg.get("pinned"):
                    continue
                if leg.get("ts") == newest_unpinned_ts:
                    continue
                if leg.get("ts") in protected_tses:
                    continue
                to_remove.append(leg)

        if not to_remove:
            return 0

        for leg in to_remove:
            f_rel = leg.get("file")
            if f_rel:
                f_abs = (PROJECT_ROOT / f_rel).resolve()
                try:
                    f_abs.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("clear-all: could not unlink %s: %s", f_abs, e)

        kept = [s for s in snaps if s not in to_remove]
        idx["snapshots"] = kept

        # Orphaned config bundles (same logic as rotation).
        remaining_bundles = {s.get("config_bundle")
                             for s in idx["snapshots"]
                             if s.get("config_bundle")}
        for leg in to_remove:
            bundle = leg.get("config_bundle")
            if bundle and bundle not in remaining_bundles:
                bp = (PROJECT_ROOT / bundle).resolve()
                try:
                    bp.unlink(missing_ok=True)
                    log.info("clear-all: deleted orphaned config bundle %s", bp)
                except OSError as e:
                    log.warning("clear-all: could not unlink %s: %s", bp, e)

        _save_index(idx)
        log.info("clear-all: removed %d leg(s); kept newest unpinned + all pinned",
                 len(to_remove))
        return len(to_remove)


def delete_snapshot_all_legs(ts: str) -> tuple[int, int]:
    """Delete every leg of a single cluster snapshot at the given ts.
    Skips legs that are pinned (operator must unpin first).
    Returns (deleted_count, skipped_pinned_count).
    """
    legs = get_snapshot_legs(ts)
    deleted = 0
    skipped = 0
    for leg in legs:
        if leg.get("pinned"):
            skipped += 1
            continue
        if delete_snapshot_leg(ts, leg.get("instance", "")):
            deleted += 1
    log.info("delete_snapshot_all_legs %s: deleted=%d skipped(pinned)=%d",
             ts, deleted, skipped)
    return deleted, skipped


def _gunzip_file(src_gz: Path, dst: Path) -> None:
    """Decompress src_gz -> dst (overwriting dst). Stream-based."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src_gz, "rb") as fin, dst.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)


def _safe_restore_to_world_db(backup_gz: Path, world_db: Path) -> tuple[bool, str]:
    """Decompress `backup_gz` -> a staging file beside `world_db`,
    run sqlite3 PRAGMA integrity_check on the result, atomic-rename
    into place ONLY if integrity passes.

    On any failure (decompress error or corrupt backup) the staging
    file is removed and the live `world_db` is untouched. This is the
    safe path for restore: we never overwrite the live db with a file
    we haven't independently verified.

    Returns (ok, status_string). status_string is human-readable for
    the index/UI.
    """
    staging = world_db.with_name(world_db.name + ".restoring")
    log.verbose("safe-restore: backup_gz=%s world_db=%s staging=%s",
                backup_gz, world_db, staging)
    log.info("[restore] decompressing %s -> staging %s",
             backup_gz, staging)
    try:
        _gunzip_file(backup_gz, staging)
    except OSError as e:
        log.error("[restore] decompress to staging failed: %s "
                  "(live db unchanged)", e)
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"decompress-failed: {e}"

    size = staging.stat().st_size
    integrity = _integrity_check(staging)
    log.info("[restore] staging %s (%d bytes) integrity=%s",
             staging.name, size, integrity)
    if integrity != "ok":
        log.error("[restore] BACKUP INTEGRITY FAILED (%s) at %s -- "
                  "live db at %s is UNCHANGED. The backup file may "
                  "be corrupt; pick a different snapshot.",
                  integrity, backup_gz, world_db)
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return False, "backup-corrupt"

    try:
        # Path.replace is atomic on Windows when both paths are on
        # the same volume (they are -- both inside the worlds dir).
        staging.replace(world_db)
        log.info("[restore] integrity ok; atomic rename %s -> %s "
                 "complete", staging.name, world_db.name)
        return True, "restored"
    except OSError as e:
        log.error("[restore] atomic rename %s -> %s failed: %s "
                  "(live db unchanged)", staging.name, world_db.name, e)
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"rename-failed: {e}"


def _restore_manager_config(bundle_path: Path) -> None:
    """Extract a snapshot's config tar.gz over the manager config locations.

    settings.toml -> data/settings.toml
    GameplaySettings/*.json -> install_dir/WS/Saved/GameplaySettings/
    Config/WindowsServer/Engine.ini -> install_dir/WS/Saved/Config/WindowsServer/

    Backs up the existing settings.toml to data/settings.toml.before-restore
    so the operator can roll back hand-edits.
    """
    settings_path = DATA_DIR / "settings.toml"
    if settings_path.exists():
        backup_to = settings_path.with_suffix(".toml.before-restore")
        shutil.copy2(settings_path, backup_to)
        log.info("[restore-config] saved current settings.toml to %s",
                 backup_to)

    saved_dir = paths.saved_dir()
    with tarfile.open(bundle_path, "r:gz") as t:
        for member in t.getmembers():
            if not member.isfile():
                continue
            data = t.extractfile(member)
            if data is None:
                continue
            name = member.name
            if name == "settings.toml":
                target = settings_path
            elif name.startswith("GameplaySettings/"):
                target = saved_dir / name
            elif name.startswith("Config/"):
                target = saved_dir / name
            else:
                log.warning("[restore-config] skipping unknown member %r",
                            name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as fout:
                shutil.copyfileobj(data, fout)
            log.info("[restore-config] wrote %s", target)


def _find_runtime_instance_by_short(short: str) -> Optional[RuntimeInstance]:
    """Reverse the instance_short() mapping to find the RuntimeInstance.
    Used by restore which knows snapshots by short name and needs the
    full runtime info for stop/start."""
    from manager.config import load_settings
    from manager.wizard import active_runtime_instances, load_existing
    config = load_existing(load_settings())
    for ri in active_runtime_instances(config):
        if instance_short(ri) == short:
            return ri
    return None


# ── Index housekeeping ──────────────────────────────────────────────────────


def _enforce_rotation(keep_last_n: int) -> int:
    """Per-instance retention. For each instance, keep at most
    `keep_last_n` UNPINNED legs (newest by ts); delete the rest. Pinned
    legs are exempt -- they don't count against the budget AND they're
    never pruned. Returns the count of legs removed.

    Side-effects:
      - Removes the leg's gzipped DB file from disk
      - Removes the index entry
      - Removes any orphaned config bundle whose ts no longer appears in
        the index (shared bundle -- only delete when no leg references it)
    """
    if keep_last_n <= 0:
        log.warning("rotation: keep_last_n=%d invalid; skipping prune",
                    keep_last_n)
        return 0
    with _index_lock:
        idx = _load_index()
        snaps = list(idx.get("snapshots", []))
        # Group by instance, sort newest-first within each group.
        by_instance: dict[str, list[dict]] = {}
        for s in snaps:
            by_instance.setdefault(s.get("instance", "?"), []).append(s)
        for inst, legs in by_instance.items():
            legs.sort(key=lambda s: s.get("ts", ""), reverse=True)

        to_remove: list[dict] = []
        for inst, legs in by_instance.items():
            unpinned = [s for s in legs if not s.get("pinned")]
            if len(unpinned) <= keep_last_n:
                continue
            # The oldest unpinned legs beyond the budget go.
            excess = unpinned[keep_last_n:]
            for leg in excess:
                to_remove.append(leg)
                log.info("rotation: %s pruning leg %s/%s (over keep=%d)",
                         inst, leg.get("ts"), inst, keep_last_n)

        if not to_remove:
            log.debug("rotation: nothing to prune (limit=%d)", keep_last_n)
            return 0

        # Drop files + index entries.
        kept_set = {(s.get("ts"), s.get("instance")): s for s in snaps
                    if s not in to_remove}
        for leg in to_remove:
            f_rel = leg.get("file")
            if f_rel:
                f_abs = (PROJECT_ROOT / f_rel).resolve()
                try:
                    f_abs.unlink(missing_ok=True)
                except OSError as e:
                    log.warning("rotation: could not unlink %s: %s",
                                f_abs, e)

        idx["snapshots"] = list(kept_set.values())

        # Orphaned config bundles: any bundle whose ts no longer has any
        # remaining leg referencing it.
        remaining_bundles = {s.get("config_bundle")
                             for s in idx["snapshots"]
                             if s.get("config_bundle")}
        for leg in to_remove:
            bundle = leg.get("config_bundle")
            if bundle and bundle not in remaining_bundles:
                bp = (PROJECT_ROOT / bundle).resolve()
                try:
                    bp.unlink(missing_ok=True)
                    log.info("rotation: deleted orphaned config bundle %s",
                             bp)
                except OSError as e:
                    log.warning("rotation: could not unlink config bundle %s: %s",
                                bp, e)

        _save_index(idx)
        log.info("rotation: pruned %d leg(s)", len(to_remove))
        return len(to_remove)


def delete_snapshot_leg(ts: str, instance: str) -> bool:
    """Delete one leg's gzipped DB and remove its index entry. Refuses if
    the leg is pinned. Returns True on success."""
    with _index_lock:
        idx = _load_index()
        snaps = idx.get("snapshots", [])
        target = None
        for s in snaps:
            if s.get("ts") == ts and s.get("instance") == instance:
                target = s
                break
        if target is None:
            log.warning("delete_snapshot_leg: no entry for %s/%s", ts, instance)
            return False
        if target.get("pinned"):
            log.warning("delete_snapshot_leg: %s/%s is pinned -- refusing",
                        ts, instance)
            return False

        # Remove the gzipped file (best effort).
        f_rel = target.get("file")
        if f_rel:
            f_abs = (PROJECT_ROOT / f_rel).resolve()
            try:
                f_abs.unlink(missing_ok=True)
                log.info("deleted snapshot file: %s", f_abs)
            except OSError as e:
                log.warning("could not unlink %s: %s (continuing)", f_abs, e)

        idx["snapshots"] = [s for s in snaps
                            if not (s.get("ts") == ts and s.get("instance") == instance)]

        # If no other leg references the same config bundle, remove it too.
        bundle = target.get("config_bundle")
        if bundle:
            still_referenced = any(s.get("config_bundle") == bundle
                                   for s in idx["snapshots"])
            if not still_referenced:
                bp = (PROJECT_ROOT / bundle).resolve()
                try:
                    bp.unlink(missing_ok=True)
                    log.info("deleted orphaned config bundle: %s", bp)
                except OSError as e:
                    log.warning("could not unlink config bundle %s: %s",
                                bp, e)

        _save_index(idx)
        log.info("snapshot %s/%s deleted", ts, instance)
        return True
