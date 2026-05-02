"""Workshop mod management.

Two responsibilities:

  1. Download / remove Steam Workshop mods on the operator's behalf,
     placing them in WS\\Mods\\<MOD<random>>\\ where the engine
     auto-discovers them.
  2. Maintain `data/mods_subscribed.json` -- the manager's record of
     which mod folders it installed, so the launch line can advertise
     the right Workshop IDs to connecting clients via `-mod="..."`
     (singular, see memory/soulmask_mod_flag.md).

The filesystem (`WS\\Mods\\`) is the source of truth for what's
LOADED. The manifest is just provenance: was this folder installed
by the manager (Workshop)? Or did the operator drop it in by hand
(Local dev mod)? Local mods are listed read-only -- the UI won't
remove them.

Soulmask has two Steam app IDs:
  - 3017310 = dedicated server (what SteamCMD installs to run)
  - 2646460 = the game (where Workshop items live)
We use 2646460 for `+workshop_download_item`. Don't confuse with
the SOULMASK_WINDOWS_APP_ID constant in paths.py (which is 3017310).
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Workshop content lives under the GAME's app id, not the server's.
WORKSHOP_GAME_APP_ID = 2646460

_manifest_lock = threading.Lock()


def manifest_path() -> Path:
    from manager.config import DATA_DIR
    return DATA_DIR / "mods_subscribed.json"


def mods_dir() -> Path:
    """`WS\\Mods\\` -- where the engine auto-discovers mod paks."""
    from manager.paths import install_dir
    return install_dir() / "WS" / "Mods"


def workshop_content_dir() -> Path:
    """`<steamcmd>\\steamapps\\workshop\\content\\2646460\\` -- where
    SteamCMD writes downloaded Workshop items."""
    from manager.paths import steamcmd_exe
    return (steamcmd_exe().parent / "steamapps" / "workshop"
            / "content" / str(WORKSHOP_GAME_APP_ID))


# ── Manifest helpers ───────────────────────────────────────────────────────


def _manifest_load() -> list[dict]:
    p = manifest_path()
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        # Defensive: keep only well-formed entries.
        return [
            e for e in data
            if isinstance(e, dict)
            and e.get("workshop_id") and e.get("folder")
        ]
    except (OSError, json.JSONDecodeError) as e:
        log.warning("mods manifest unreadable at %s: %s -- treating as empty",
                    p, e)
        return []


def _manifest_save(entries: list[dict]) -> None:
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    tmp.replace(p)


# ── Public read API ────────────────────────────────────────────────────────


def subscribed_workshop_ids() -> list[str]:
    """All Workshop IDs in the manifest, in insertion order. Used by
    wizard.build_launch_args to populate the `-mod="..."` flag."""
    return [e["workshop_id"] for e in _manifest_load()]


def _read_mod_metadata(folder_path: Path) -> dict:
    """Pull human-friendly metadata for a mod folder.

    Source priority:
      1. ModeInfo.json -- Soulmask's own per-mod metadata file. Written
         by the modkit's publishing pipeline; richest field set.
      2. <folder>.uplugin -- standard UE plugin manifest. Fallback when
         ModeInfo.json is missing (older / hand-built mods).
      3. Empty dict -- the row will fall back to displaying the folder
         ID as the name.

    Returns a normalized dict with str values (empty string if absent):
      {name, author, description, version, mod_id, url}
    `mod_id` is the Workshop ID per ModeInfo.json -- handy to confirm a
    Local-provenance folder is actually a Workshop mod the operator
    placed by hand."""
    out = {"name": "", "author": "", "description": "", "version": "",
           "mod_id": "", "url": ""}

    # 1. ModeInfo.json (Soulmask-canonical)
    mi_path = folder_path / "ModeInfo.json"
    if mi_path.is_file():
        try:
            with mi_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                out["name"] = str(data.get("ModName") or "").strip()
                out["author"] = str(data.get("ModAuthor") or "").strip()
                out["description"] = str(data.get("ModDescription") or "").strip()
                out["version"] = str(data.get("ModVersion") or "").strip()
                out["mod_id"] = str(data.get("ModID") or "").strip()
                out["url"] = str(data.get("ModUrl") or "").strip()
                if out["name"]:
                    return out
        except (OSError, json.JSONDecodeError):
            log.debug("metadata: failed to read %s", mi_path, exc_info=True)

    # 2. .uplugin fallback
    up_path = folder_path / f"{folder_path.name}.uplugin"
    if up_path.is_file():
        try:
            with up_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Don't clobber ModeInfo.json values we already grabbed.
                out["name"] = out["name"] or str(data.get("FriendlyName") or "").strip()
                out["author"] = out["author"] or str(data.get("CreatedBy") or "").strip()
                out["description"] = out["description"] or str(data.get("Description") or "").strip()
                out["version"] = out["version"] or str(data.get("VersionName") or "").strip()
                out["url"] = out["url"] or str(data.get("CreatedByURL") or "").strip()
        except (OSError, json.JSONDecodeError):
            log.debug("metadata: failed to read %s", up_path, exc_info=True)

    return out


def list_installed() -> list[dict]:
    """Cross-reference filesystem (`WS\\Mods\\`) with the manifest, plus
    human-friendly metadata read from each mod's ModeInfo.json /
    .uplugin. Each row:
      {folder, size_mb, mtime, provenance, workshop_id, added_at,
       name, author, description, version, mod_id, url}
    `provenance` is "workshop" if our manifest claims it, else "local"."""
    md = mods_dir()
    if not md.exists():
        return []

    manifest_by_folder = {e["folder"]: e for e in _manifest_load()}
    out: list[dict] = []
    for child in sorted(md.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if child.name.endswith(".tmp"):
            # In-progress copy from a previous install -- hide.
            continue
        meta = _read_mod_metadata(child)
        entry = {
            "folder": child.name,
            "size_mb": _dir_size_mb(child),
            "mtime": child.stat().st_mtime,
            "provenance": "local",
            "workshop_id": None,
            "added_at": None,
            **meta,
        }
        m = manifest_by_folder.get(child.name)
        if m:
            entry["provenance"] = "workshop"
            entry["workshop_id"] = m.get("workshop_id")
            entry["added_at"] = m.get("added_at")
        out.append(entry)
    return out


def _dir_size_mb(path: Path) -> float:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                fp = Path(root) / f
                try:
                    total += fp.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total / (1024 * 1024)


# ── Refusal pre-flight checks (shared by add + remove) ─────────────────────


def _refuse_if_servers_running() -> Optional[str]:
    """Returns a refusal reason string, or None if it's safe to proceed.
    SteamCMD can't lock the install dir while WSServer.exe holds files
    open, and rmtree on a mod folder while the server has the pak open
    will fail (or worse, leave a half-deleted state)."""
    from manager import lifecycle
    alive = [p for _, p in lifecycle.running_instances() if p.is_alive]
    if not alive:
        return None
    names = ", ".join(p.runtime_instance.instance.name for p in alive)
    return (f"Stop the server(s) first: {names}. The install dir is "
            "locked while WSServer.exe is running.")


# ── SteamCMD invocation (workshop_download_item) ───────────────────────────


def _run_steamcmd_download(workshop_id: str) -> int:
    """Run SteamCMD +workshop_download_item streaming to updates_log
    (source="steam"). Returns the SteamCMD process return code."""
    from manager import updates_log
    from manager.paths import steamcmd_exe

    cmd_path = steamcmd_exe()
    if not cmd_path.exists():
        updates_log.append("steam", f"ERROR: SteamCMD not at {cmd_path}")
        return -1

    cmd = [
        str(cmd_path),
        "+login", "anonymous",
        "+workshop_download_item",
        str(WORKSHOP_GAME_APP_ID), workshop_id,
        "+quit",
    ]
    updates_log.append("steam",
                       f"$ steamcmd +workshop_download_item "
                       f"{WORKSHOP_GAME_APP_ID} {workshop_id} +quit")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except OSError as e:
        updates_log.append("steam", f"ERROR: SteamCMD launch failed: {e}")
        return -1

    assert proc.stdout is not None
    line_buf = bytearray()
    deadline = time.monotonic() + 600  # 10 min cap; large mods can be slow
    timed_out = False
    try:
        while True:
            if time.monotonic() > deadline:
                timed_out = True
                break
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            line_buf.extend(chunk)
            while True:
                nl = line_buf.find(b"\n")
                if nl < 0:
                    break
                line = line_buf[:nl].decode("utf-8", errors="replace").rstrip("\r")
                del line_buf[:nl + 1]
                updates_log.append("steam", line)
        if line_buf:
            updates_log.append("steam",
                               line_buf.decode("utf-8", errors="replace"))
    except Exception as e:
        log.exception("_run_steamcmd_download read loop raised")
        updates_log.append("steam", f"ERROR: read loop: {e}")
        try:
            proc.kill()
        except Exception:
            pass
        return -1

    if timed_out:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        updates_log.append("steam", "ERROR: SteamCMD timed out after 600s")
        return -1
    return proc.wait()


# ── Install / Remove ───────────────────────────────────────────────────────


def _find_inner_mod_folder(workshop_dl_dir: Path) -> Optional[str]:
    """The successful workshop_download_item drops a single
    `MOD<random>\\` directory inside <workshop>\\<wsid>\\. Find it."""
    if not workshop_dl_dir.is_dir():
        return None
    for child in workshop_dl_dir.iterdir():
        if child.is_dir() and child.name.startswith("MOD"):
            return child.name
    return None


def _install_from_workshop(workshop_id: str,
                            replace: bool) -> tuple[bool, str]:
    """Body of the add operation. Caller must hold steamcmd lock +
    updates_log op."""
    from manager import updates_log

    # 1. Run SteamCMD to download / refresh the item.
    rc = _run_steamcmd_download(workshop_id)
    if rc != 0:
        return False, f"SteamCMD exited rc={rc}"

    # 2. Find the inner MOD<random> folder we got.
    src_root = workshop_content_dir() / workshop_id
    mod_folder = _find_inner_mod_folder(src_root)
    if mod_folder is None:
        return False, (f"Downloaded but no MOD<random> folder found in "
                       f"{src_root}. Workshop item may not be a Soulmask "
                       "mod or may have an unexpected layout.")

    src_mod = src_root / mod_folder
    target_mod = mods_dir() / mod_folder

    # 3. Conflict / replace handling.
    if target_mod.exists():
        with _manifest_lock:
            existing = _manifest_load()
        # Is this folder already tracked by the manager?
        tracked = next((e for e in existing
                        if e.get("folder") == mod_folder), None)
        if tracked is None:
            # Folder exists on disk but no manifest entry -> Local dev mod
            # with the same name. Don't touch it.
            return False, (
                f"CONFLICT: WS\\Mods\\{mod_folder} already exists but isn't "
                f"in the manager's subscription manifest. It looks like a "
                f"Local dev mod with the same name as Workshop item "
                f"{workshop_id}. Files left untouched -- resolve manually "
                "(remove the local folder OR don't subscribe to this "
                "Workshop item).")
        # Tracked. Check workshop_id agreement.
        if tracked.get("workshop_id") != workshop_id:
            return False, (
                f"CONFLICT: WS\\Mods\\{mod_folder} is tracked for Workshop "
                f"ID {tracked.get('workshop_id')}, not {workshop_id}. "
                "Two Workshop items appear to ship the same MOD folder name "
                "-- pick one.")
        # Same workshop_id, same folder. Need replace=True to proceed.
        if not replace:
            return False, (
                f"Already installed (workshop_id={workshop_id}, "
                f"folder={mod_folder}). Pass replace=1 to re-download.")
        # Replace flow proceeds to copy below.

    # 4. Copy: stage to .tmp dir then atomic rename.
    tmp_target = mods_dir() / f"{mod_folder}.tmp"
    if tmp_target.exists():
        shutil.rmtree(tmp_target, ignore_errors=True)
    mods_dir().mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src_mod, tmp_target)
    except OSError as e:
        return False, f"Copy to {tmp_target} failed: {e}"
    if target_mod.exists():
        try:
            shutil.rmtree(target_mod)
        except OSError as e:
            shutil.rmtree(tmp_target, ignore_errors=True)
            return False, f"Couldn't replace existing {target_mod}: {e}"
    try:
        os.rename(tmp_target, target_mod)
    except OSError as e:
        shutil.rmtree(tmp_target, ignore_errors=True)
        return False, f"Rename {tmp_target} -> {target_mod} failed: {e}"

    # 5. Update manifest atomically.
    with _manifest_lock:
        entries = _manifest_load()
        # Drop any existing entry for this workshop_id (re-install case).
        entries = [e for e in entries
                   if e.get("workshop_id") != workshop_id]
        entries.append({
            "workshop_id": workshop_id,
            "folder": mod_folder,
            "added_at": datetime.now().isoformat(timespec="seconds"),
        })
        _manifest_save(entries)

    from manager import updates_log as _ul
    _ul.append("steam",
               f"Installed Workshop ID {workshop_id} -> WS\\Mods\\"
               f"{mod_folder}\\")
    return True, f"Installed {mod_folder} (Workshop ID {workshop_id})"


def _remove_local(folder: str) -> tuple[bool, str]:
    """Body of the remove operation. Validates folder is in our manifest
    (Local mods are read-only), wipes the directory, drops the manifest
    entry."""
    from manager import updates_log

    # Validate folder is one we manage.
    with _manifest_lock:
        existing = _manifest_load()
    tracked = next((e for e in existing if e.get("folder") == folder), None)
    if tracked is None:
        return False, (f"{folder} is not in the manager's subscription "
                       "manifest -- only Workshop-installed mods can be "
                       "removed via this UI. Local dev mods are managed "
                       "by hand.")

    # Path-traversal guard: refuse anything not strictly under WS\Mods\.
    md = mods_dir().resolve()
    target = (md / folder).resolve()
    try:
        target.relative_to(md)
    except ValueError:
        return False, f"Refusing path outside WS\\Mods\\: {folder!r}"

    updates_log.append("steam", f"Removing WS\\Mods\\{folder}\\ ...")
    if target.exists():
        try:
            shutil.rmtree(target)
        except OSError as e:
            return False, f"rmtree({target}) failed: {e}"
        updates_log.append("steam", "  removed from disk")
    else:
        updates_log.append(
            "steam",
            "  (folder didn't exist on disk; clearing manifest entry only)")

    with _manifest_lock:
        entries = _manifest_load()
        entries = [e for e in entries if e.get("folder") != folder]
        _manifest_save(entries)
    updates_log.append("steam",
                       f"  manifest updated (removed Workshop ID "
                       f"{tracked.get('workshop_id')})")
    return True, f"Removed {folder}"


# ── Async kick-offs (called by routes) ─────────────────────────────────────


def start_add(workshop_id: str,
              replace: bool = False) -> tuple[bool, str]:
    """Refuse (False, reason) or kick off an install. The actual work
    runs on the background worker; output streams to the /updates SSE
    activity log.

    When `replace` is False and the workshop_id is already in the
    manifest, refuse early with a friendly message pointing the
    operator at the per-row Redownload button. The /mods/<folder>/
    redownload route is the entry point for that case -- it sets
    replace=True and bypasses this guard.
    """
    from manager import background, updates as steam_updates, updates_log

    workshop_id = (workshop_id or "").strip()
    if not workshop_id.isdigit() or int(workshop_id) <= 0:
        return False, ("Workshop ID must be a positive integer "
                       "(e.g. 3717432062).")

    if not replace:
        with _manifest_lock:
            existing = _manifest_load()
        already = next((e for e in existing
                        if e.get("workshop_id") == workshop_id), None)
        if already:
            folder = already.get("folder", "?")
            return False, (f"Workshop ID {workshop_id} is already installed "
                           f"as {folder}. Use the Redownload button on its "
                           "row to refresh it.")

    refusal = _refuse_if_servers_running()
    if refusal:
        return False, refusal

    if not steam_updates.try_acquire_steamcmd_lock():
        return False, ("Another SteamCMD operation is in progress "
                       "(poller mid-check or a manual op). Try again "
                       "in a few seconds.")

    op_label = (f"Re-install Workshop mod {workshop_id}" if replace
                else f"Install Workshop mod {workshop_id}")
    if not updates_log.begin_op(op_label, "steam"):
        steam_updates.release_steamcmd_lock()
        return False, ("Another /updates operation is in progress.")

    def _runner():
        try:
            try:
                ok, msg = _install_from_workshop(workshop_id, replace)
            except Exception as e:
                log.exception("mod-add runner raised")
                updates_log.end_op(False, f"{type(e).__name__}: {e}")
                return
            updates_log.end_op(ok, "" if ok else msg)
        finally:
            steam_updates.release_steamcmd_lock()

    background.submit("mod-add", _runner)
    return True, op_label + " started"


def start_redownload(folder: str) -> tuple[bool, str]:
    """Refuse (False, reason) or kick off a re-install of an existing
    Workshop mod, identified by its folder name (the MOD<random> dir
    under WS\\Mods\\). Looks up the workshop_id from the manifest and
    delegates to start_add with replace=True."""
    folder = (folder or "").strip()
    if not folder or "/" in folder or "\\" in folder or folder in (".", ".."):
        return False, f"Invalid folder name: {folder!r}"

    with _manifest_lock:
        existing = _manifest_load()
    tracked = next((e for e in existing if e.get("folder") == folder), None)
    if tracked is None:
        return False, (f"{folder} isn't tracked by the manager (no manifest "
                       "entry). Local-only mods can't be redownloaded -- "
                       "Workshop provenance is required.")
    workshop_id = tracked.get("workshop_id")
    if not workshop_id:
        return False, (f"Manifest entry for {folder} has no workshop_id; "
                       "can't redownload.")
    return start_add(workshop_id, replace=True)


def start_remove(folder: str) -> tuple[bool, str]:
    """Refuse (False, reason) or kick off a remove. No SteamCMD
    involvement so no steamcmd lock needed."""
    from manager import background, updates_log

    folder = (folder or "").strip()
    if not folder or "/" in folder or "\\" in folder or folder in (".", ".."):
        return False, f"Invalid folder name: {folder!r}"

    refusal = _refuse_if_servers_running()
    if refusal:
        return False, refusal

    op_label = f"Remove mod {folder}"
    if not updates_log.begin_op(op_label, "steam"):
        return False, "Another /updates operation is in progress."

    def _runner():
        try:
            ok, msg = _remove_local(folder)
        except Exception as e:
            log.exception("mod-remove runner raised")
            updates_log.end_op(False, f"{type(e).__name__}: {e}")
            return
        updates_log.end_op(ok, "" if ok else msg)

    background.submit("mod-remove", _runner)
    return True, op_label + " started"
