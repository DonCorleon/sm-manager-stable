"""Pre-boot supervisor: detects boot crash loops and rolls back to the
last known-good SHA.

Invoked by start_manager.bat BEFORE each `python -m manager` launch.
The flow:

  1. The manager creates `data/.boot_in_progress` at startup and
     deletes it after running for >=60s without crashing
     ("stable-boot" mark).
  2. After the manager exits, the .bat re-enters server-loop and
     calls this script. If `.boot_in_progress` still exists, the
     manager crashed BEFORE reaching stable -- a fast crash. Increment
     the crash counter.
  3. If the counter hits CRASH_THRESHOLD AND we have a stored
     `last_known_good.txt` SHA, run `git reset --hard <sha>` so the
     next launch boots from a known-good revision.
  4. Reset the marker so this boot starts clean.

Why a separate script instead of code inside `manager/__main__.py`?
The whole point of R1 is to recover from the case where the manager
won't import (NameError / SyntaxError / missing dep). If the
recovery code lived inside the manager package, it would die on the
same import. This script imports nothing from the manager package
and uses only stdlib, so a totally broken manager tree doesn't break
recovery.

Bypass: create `data/.disable_auto_rollback` to suppress the
git reset (useful when intentionally debugging a broken commit).

Stdlib-only on purpose. No project imports.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"

BOOT_IN_PROGRESS = DATA_DIR / ".boot_in_progress"
LAST_KNOWN_GOOD = DATA_DIR / "last_known_good.txt"
CRASH_COUNTER = DATA_DIR / ".boot_crash_count"
DISABLE_ROLLBACK = DATA_DIR / ".disable_auto_rollback"
SHUTDOWN_CLEAN = DATA_DIR / ".shutdown_clean"
LOG_FILE = LOGS_DIR / "manager.log"

CRASH_THRESHOLD = 2


def log(msg: str) -> None:
    """Best-effort log to stdout AND manager.log so the supervisor's
    actions land in the same place as the manager's own output."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} BOOTLOADER {msg}"
    print(line, flush=True)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return default


def _find_git() -> str | None:
    """Locate git the same way self_update._resolve_binary does, but
    without importing the manager package."""
    if shutil.which("git"):
        return "git"
    for candidate in (
        ROOT / "portable" / "git" / "cmd" / "git.exe",
        ROOT / "portable" / "git" / "bin" / "git.exe",
        Path(r"C:\Program Files\Git\bin\git.exe"),
        Path(r"C:\Program Files\Git\cmd\git.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _attempt_rollback(sha: str) -> bool:
    """git reset --hard <sha> against the project root. Returns True
    on success, False on any failure (which is logged)."""
    if DISABLE_ROLLBACK.exists():
        log(f"rollback suppressed by {DISABLE_ROLLBACK.name} marker; "
            f"would have reset to {sha[:12]}")
        return False
    git = _find_git()
    if git is None:
        log(f"rollback target {sha[:12]} but no git binary "
            f"available -- skipping")
        return False
    log(f"AUTO-ROLLBACK: git reset --hard {sha[:12]}")
    try:
        res = subprocess.run([git, "reset", "--hard", sha],
                             cwd=str(ROOT),
                             capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"AUTO-ROLLBACK failed: {type(e).__name__}: {e}")
        return False
    if res.returncode != 0:
        msg = (res.stderr or res.stdout or "").strip()
        log(f"AUTO-ROLLBACK rc={res.returncode}: {msg[:300]}")
        return False
    log(f"AUTO-ROLLBACK ok: {res.stdout.strip()[:300]}")
    return True


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"could not remove {p.name}: {e}")


def _check_unclean_shutdown() -> None:
    """R9: if the shutdown_clean marker is missing, the previous run
    terminated uncleanly (kill -9, BSOD, mid-pull SIGINT, power loss).
    Run `git fsck --no-progress` + `git gc --auto --quiet` to clean
    up any half-written objects before any state-changing git op runs
    in this boot.

    Both commands are conservative: fsck is read-only; gc --auto only
    repacks if loose-object count crosses gc.auto threshold. Worst
    case the boot takes a few extra seconds the next time after a
    forced kill -- acceptable trade for not having to manually
    `git fsck` after every BSOD."""
    if SHUTDOWN_CLEAN.exists():
        _safe_unlink(SHUTDOWN_CLEAN)
        return
    if not (ROOT / ".git").exists():
        return  # not a git checkout (or test fixture); nothing to fsck
    git = _find_git()
    if git is None:
        log("unclean shutdown detected but no git available; "
            "skipping fsck/gc")
        return
    log("unclean shutdown detected (no .shutdown_clean marker); "
        "running git fsck + git gc --auto")
    for argv in (["fsck", "--no-progress"], ["gc", "--auto", "--quiet"]):
        try:
            res = subprocess.run([git, *argv],
                                 cwd=str(ROOT),
                                 capture_output=True, text=True,
                                 timeout=180)
        except (subprocess.TimeoutExpired, OSError) as e:
            log(f"git {' '.join(argv)} failed: {type(e).__name__}: {e}")
            continue
        if res.returncode != 0:
            log(f"git {' '.join(argv)} rc={res.returncode}: "
                f"{(res.stderr or res.stdout).strip()[:200]}")
        else:
            out = (res.stdout or "").strip()
            log(f"git {' '.join(argv)} ok"
                + (f" -- {out[:200]}" if out else ""))


def preboot(root: Path | None = None) -> int:
    """Run the pre-boot crash-detection logic. Returns the new crash
    counter value (0 if not in a crash-detection state).

    `root` lets smoke tests redirect to a tempdir. Production callers
    pass nothing and use the module-level paths.
    """
    if root is not None:
        # Override module paths for the duration of this call.
        # Simpler than threading a context through every helper.
        local_data = root / "data"
        local_logs = root / "logs"
        global DATA_DIR, LOGS_DIR, BOOT_IN_PROGRESS, LAST_KNOWN_GOOD
        global CRASH_COUNTER, DISABLE_ROLLBACK, SHUTDOWN_CLEAN, LOG_FILE, ROOT
        DATA_DIR = local_data
        LOGS_DIR = local_logs
        BOOT_IN_PROGRESS = local_data / ".boot_in_progress"
        LAST_KNOWN_GOOD = local_data / "last_known_good.txt"
        CRASH_COUNTER = local_data / ".boot_crash_count"
        DISABLE_ROLLBACK = local_data / ".disable_auto_rollback"
        SHUTDOWN_CLEAN = local_data / ".shutdown_clean"
        LOG_FILE = local_logs / "manager.log"
        ROOT = root.resolve()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Run fsck/gc BEFORE crash-detection logic. If the previous run
    # crashed mid-pull, fsck cleans the half-written objects so the
    # subsequent rollback (if needed) sees a consistent repo.
    _check_unclean_shutdown()

    count = 0
    if BOOT_IN_PROGRESS.exists():
        # Previous run left the marker behind => crashed before reaching
        # stable. Treat as a fast crash.
        count = _read_int(CRASH_COUNTER) + 1
        try:
            CRASH_COUNTER.write_text(str(count), encoding="utf-8")
        except OSError as e:
            log(f"could not write crash counter: {e}")
        log(f"detected previous boot crash "
            f"({count}/{CRASH_THRESHOLD} fast crashes)")

        if count >= CRASH_THRESHOLD:
            if LAST_KNOWN_GOOD.exists():
                try:
                    sha = LAST_KNOWN_GOOD.read_text(encoding="utf-8").strip()
                except OSError as e:
                    log(f"could not read last_known_good.txt: {e}")
                    sha = ""
                if sha and _attempt_rollback(sha):
                    # Reset counter so we don't roll back AGAIN if the
                    # rolled-back code happens to also fast-crash on
                    # this hardware (env issue, missing dep) -- one
                    # auto-rollback per crash episode is enough; further
                    # action belongs to the operator.
                    _safe_unlink(CRASH_COUNTER)
            else:
                log("would auto-rollback but no last_known_good.txt yet "
                    "(manager has never recorded a stable boot on this "
                    "install) -- counter stays armed for next attempt")

        _safe_unlink(BOOT_IN_PROGRESS)
    else:
        # Previous run reached stable (cleared the marker itself).
        # Reset any stale crash counter.
        if CRASH_COUNTER.exists():
            _safe_unlink(CRASH_COUNTER)

    # Mark this boot in-progress. Manager clears it after >= 60s of
    # uptime to indicate "I reached the listening state and stayed
    # there".
    try:
        BOOT_IN_PROGRESS.touch()
    except OSError as e:
        log(f"could not create boot-in-progress marker: {e}")

    return count


def main() -> int:
    preboot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
