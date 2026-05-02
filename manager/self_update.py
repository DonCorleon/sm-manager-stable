"""Manager self-update via `git pull`.

Two halves:

1. **Deploy key management**: the manager generates its own ed25519
   SSH keypair via `ssh-keygen`. The private key stays in
   `data/deploy_key` (gitignored, never logged). The public key is
   exposed on `/manager-updates` for the operator to paste into the
   repo's Deploy keys page on GitHub. New testers receiving a copy
   of the manager just generate their own key, email the public half
   to the operator, and the operator adds it as another deploy key
   on the same repo. Each machine gets its own revocable key.

2. **Git operations**: every git subprocess runs with
   `GIT_SSH_COMMAND="ssh -i <our key> -o IdentitiesOnly=yes
                    -o StrictHostKeyChecking=accept-new"`
   so it uses our deploy key and never falls back to ~/.ssh. We
   surface current SHA, upstream SHA, commits-behind, and an
   apply-update path that runs `git pull --ff-only` and triggers
   the existing exit-99 supervisor restart.

Apply NEVER force-resolves a dirty working tree -- if the operator
hand-edited a tracked file on the server, the pull is refused and the
UI tells them to resolve manually.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from manager.config import DATA_DIR, PROJECT_ROOT, get_setting

# Cache of resolved binary paths so we don't re-search every subprocess
# call. Cleared at module import (so a manager restart re-resolves).
_BINARY_CACHE: dict[str, Optional[str]] = {}

# Common Windows install locations searched if shutil.which() doesn't
# find the binary on PATH. Order matters -- system installs first,
# portable installs last (ours is the fallback when nothing else is
# present). The portable hints are computed lazily so the project root
# can move without rebaking paths.
_WINDOWS_HINTS_STATIC: dict[str, list[str]] = {
    "git": [
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
    ],
    "ssh-keygen": [
        # Windows OpenSSH (default since Windows 10 1809).
        r"C:\Windows\System32\OpenSSH\ssh-keygen.exe",
        # Git for Windows ships its own copy.
        r"C:\Program Files\Git\usr\bin\ssh-keygen.exe",
        r"C:\Program Files (x86)\Git\usr\bin\ssh-keygen.exe",
    ],
    "ssh": [
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        r"C:\Program Files\Git\usr\bin\ssh.exe",
        r"C:\Program Files (x86)\Git\usr\bin\ssh.exe",
    ],
}


def _portable_hints(name: str) -> list[str]:
    """Locations under <project>/portable/git/ where PortableGit puts
    the binaries. We search these LAST so a system install wins; if
    none exists, the operator-installed PortableGit is the fallback."""
    base = PROJECT_ROOT / "portable" / "git"
    if name == "git":
        return [str(base / "bin" / "git.exe"),
                str(base / "cmd" / "git.exe"),
                str(base / "mingw64" / "bin" / "git.exe")]
    if name == "ssh":
        return [str(base / "usr" / "bin" / "ssh.exe")]
    if name == "ssh-keygen":
        return [str(base / "usr" / "bin" / "ssh-keygen.exe")]
    return []


def _resolve_binary(name: str) -> Optional[str]:
    """Find a binary by name. Tries PATH first, then platform hints.
    Caches the result for the lifetime of this process so repeat calls
    don't re-walk the filesystem.

    Returns the absolute path on success, or None if the binary isn't
    found anywhere we know to look.
    """
    if name in _BINARY_CACHE:
        return _BINARY_CACHE[name]

    # 1) PATH lookup. shutil.which honours PATHEXT on Windows so it
    #    finds .exe / .cmd / .bat variants automatically.
    found = shutil.which(name)
    if found:
        log.verbose("resolve_binary(%s): found on PATH at %s", name, found)
        _BINARY_CACHE[name] = found
        return found

    # 2) Hint locations (Windows-only; Linux/Mac normally have these
    #    on PATH so the PATH lookup above finds them). Static system
    #    locations first; manager-installed portable last.
    if sys.platform == "win32":
        candidates = list(_WINDOWS_HINTS_STATIC.get(name, []))
        candidates.extend(_portable_hints(name))
        for raw in candidates:
            candidate = os.path.expandvars(raw)
            if os.path.isfile(candidate):
                log.info("resolve_binary(%s): found at hint location %s",
                         name, candidate)
                _BINARY_CACHE[name] = candidate
                return candidate

    log.warning("resolve_binary(%s): not found on PATH or hints", name)
    _BINARY_CACHE[name] = None
    return None

log = logging.getLogger(__name__)

# Filesystem layout. Both files end up under data/ which is gitignored.
DEPLOY_KEY_PATH = DATA_DIR / "deploy_key"
DEPLOY_KEY_PUB_PATH = DATA_DIR / "deploy_key.pub"

# The manager's canonical git remote. Hardcoded -- the project lives at
# this URL and the operator never needs to type it. ensure_remote()
# sets `origin` to this if no remote is configured yet (which happens
# on a fresh clone or after `git remote remove`).
MANAGER_REMOTE_URL = "https://github.com/DonCorleon/sm-manager-stable.git"

# Hard timeout for any single git or ssh-keygen subprocess call. Long
# enough for slow networks; short enough that a hung remote doesn't
# wedge the manager indefinitely.
_SUBPROCESS_TIMEOUT_SEC = 60

# Exit code that start_manager.bat's :server-loop catches to relaunch.
_RESTART_EXIT_CODE = 99


# ── Result dataclasses ──────────────────────────────────────────────────────


@dataclass
class CommandResult:
    """Plain-old subprocess result with a flag for "ran cleanly"."""
    ok: bool
    rc: int
    stdout: str
    stderr: str
    error: Optional[str] = None  # set when the subprocess itself couldn't launch


@dataclass
class UpdateStatus:
    """The state surfaced on /manager-updates."""
    deploy_key_present: bool
    deploy_pubkey: Optional[str]
    remote_url: Optional[str]
    current_sha: Optional[str]
    current_subject: Optional[str]
    upstream_sha: Optional[str]
    commits_behind: list[tuple[str, str]]   # [(short_sha, subject), ...]
    dirty: bool                              # working tree has uncommitted changes
    dirty_files: list[str]                   # [(porcelain_status, path), ...] when dirty
    last_check_error: Optional[str] = None


# ── Subprocess plumbing ─────────────────────────────────────────────────────


def _run(cmd: list[str], *, cwd: Path = PROJECT_ROOT,
         extra_env: Optional[dict] = None,
         timeout_sec: int = _SUBPROCESS_TIMEOUT_SEC,
         stream_source: Optional[str] = None) -> CommandResult:
    """Run a subprocess, capture stdout+stderr, return CommandResult.

    Never raises -- subprocess launch failures (binary missing, etc.)
    come back via .error, normal command failures via .rc and .stderr.

    Resolves the binary in cmd[0] via _resolve_binary so a manager
    process with a stripped PATH can still find Git for Windows /
    OpenSSH installs.

    `stream_source`: when set (e.g. "manager"), uses a Popen+read1 byte
    streaming path that appends each line to manager.updates_log so the
    /updates SSE consumer sees output as it happens. The stderr stream
    is merged into stdout in this mode (Discord-relay parsing already
    treats stdout as the source of truth and stderr as decoration).
    Default None preserves the silent capture path used by the periodic
    poller -- streaming every poll into updates_log would clutter it.
    """
    if cmd:
        resolved = _resolve_binary(cmd[0])
        if resolved is None:
            msg = (f"binary {cmd[0]!r} not on PATH and not in known "
                   f"install locations. ")
            if cmd[0] == "git":
                msg += ("Install Git for Windows from https://git-scm.com/ "
                        "and pick 'Git from the Windows Command Prompt' "
                        "in the installer; or restart the manager from a "
                        "shell that has git on PATH.")
            elif cmd[0] in ("ssh-keygen", "ssh"):
                msg += ("Install OpenSSH Client: Settings -> Apps -> "
                        "Optional features -> Add -> 'OpenSSH Client'. "
                        "Or in PowerShell as admin: "
                        "Add-WindowsCapability -Online -Name "
                        "OpenSSH.Client*")
            log.error("subprocess: %s", msg)
            if stream_source:
                from manager import updates_log
                updates_log.append(stream_source, f"$ {' '.join(cmd)}")
                updates_log.append(stream_source, f"ERROR: {msg}")
            return CommandResult(ok=False, rc=-1, stdout="", stderr="",
                                 error=msg)
        # Replace cmd[0] with the absolute path; argv[1:] unchanged.
        cmd = [resolved, *cmd[1:]]

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    log.verbose("subprocess: %s (cwd=%s, timeout=%ds, extra_env=%s)",
                cmd, cwd, timeout_sec,
                list(extra_env or {}))
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

    if stream_source:
        return _run_streaming(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec,
                              creationflags=creationflags,
                              stream_source=stream_source)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec,
            env=env,
            creationflags=creationflags,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        log.error("subprocess: binary not found at resolved path: %s", e)
        return CommandResult(ok=False, rc=-1, stdout="", stderr="",
                             error=f"binary not found: {e}")
    except subprocess.TimeoutExpired as e:
        log.error("subprocess: timeout after %ds: %s", timeout_sec, cmd)
        return CommandResult(ok=False, rc=-1, stdout="", stderr="",
                             error=f"timeout after {timeout_sec}s")
    except OSError as e:
        log.error("subprocess: OSError: %s", e)
        return CommandResult(ok=False, rc=-1, stdout="", stderr="",
                             error=str(e))

    log.verbose("subprocess result: rc=%d stdout=%dB stderr=%dB",
                proc.returncode, len(proc.stdout), len(proc.stderr))
    return CommandResult(
        ok=(proc.returncode == 0),
        rc=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _run_streaming(cmd: list, *, cwd: Path, env: dict, timeout_sec: int,
                   creationflags: int, stream_source: str) -> CommandResult:
    """Popen + read1 byte-streaming variant of _run.

    Each newline-terminated chunk lands in updates_log so the SSE
    consumer sees output live. Same merge-stderr-into-stdout pattern
    used by dependency_check.py -- the operator wants ONE chronological
    stream, not two interleaved channels. The full combined output is
    also accumulated and returned as CommandResult.stdout for callers
    that parse it (SteamCMD output regex, etc.)."""
    from manager import updates_log

    updates_log.append(stream_source, f"$ {' '.join(str(x) for x in cmd)}")

    try:
        # bufsize default (-1) gives a BufferedReader on stdout, whose
        # `read1(n)` returns whatever bytes are immediately available
        # without waiting for buffer fill. bufsize=0 returns a raw
        # FileIO that has no read1 method.
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge so timeline is one stream
            env=env,
            creationflags=creationflags,
        )
    except FileNotFoundError as e:
        msg = f"binary not found: {e}"
        log.error("subprocess (streaming): %s", msg)
        updates_log.append(stream_source, f"ERROR: {msg}")
        return CommandResult(ok=False, rc=-1, stdout="", stderr="",
                             error=msg)
    except OSError as e:
        msg = str(e)
        log.error("subprocess (streaming): OSError: %s", e)
        updates_log.append(stream_source, f"ERROR: {msg}")
        return CommandResult(ok=False, rc=-1, stdout="", stderr="",
                             error=msg)

    captured: list[str] = []
    line_buf = bytearray()
    deadline = time.monotonic() + timeout_sec
    timed_out = False
    try:
        assert proc.stdout is not None
        while True:
            if time.monotonic() > deadline:
                timed_out = True
                break
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            decoded = chunk.decode("utf-8", errors="replace")
            captured.append(decoded)
            # Split into lines, holding the trailing partial line in
            # line_buf until the next chunk completes it.
            line_buf.extend(chunk)
            while True:
                nl = line_buf.find(b"\n")
                if nl < 0:
                    break
                line = line_buf[:nl].decode("utf-8", errors="replace")
                del line_buf[:nl + 1]
                updates_log.append(stream_source, line)
        # Flush any trailing bytes that didn't end with a newline.
        if line_buf:
            updates_log.append(stream_source,
                               line_buf.decode("utf-8", errors="replace"))
            line_buf.clear()
    except Exception as e:
        log.exception("subprocess (streaming) read loop raised")
        updates_log.append(stream_source, f"ERROR: read loop: {e}")
        try:
            proc.kill()
        except Exception:
            pass
        return CommandResult(ok=False, rc=-1, stdout="".join(captured),
                             stderr="", error=str(e))

    if timed_out:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
        msg = f"timeout after {timeout_sec}s"
        log.error("subprocess (streaming): %s: %s", msg, cmd)
        updates_log.append(stream_source, f"ERROR: {msg}")
        return CommandResult(ok=False, rc=-1, stdout="".join(captured),
                             stderr="", error=msg)

    rc = proc.wait()
    full_stdout = "".join(captured)
    log.verbose("subprocess (streaming) result: rc=%d %dB",
                rc, len(full_stdout))
    return CommandResult(
        ok=(rc == 0),
        rc=rc,
        stdout=full_stdout,
        stderr="",  # merged into stdout
    )


def _git_ssh_env() -> dict:
    """GIT_SSH_COMMAND that pins git to our deploy key. -o IdentitiesOnly=yes
    prevents fallback to ~/.ssh; -o StrictHostKeyChecking=accept-new auto-
    adds github.com to known_hosts on first connect (safe -- the public
    SSH host keys for github.com are well-known and TOFU is acceptable
    here).

    Uses the absolute path to ssh.exe so a manager process with a
    stripped PATH still finds it -- otherwise git fork-execs `ssh` and
    inherits the same broken PATH."""
    if not DEPLOY_KEY_PATH.exists():
        return {}
    ssh_bin = _resolve_binary("ssh")
    if ssh_bin is None:
        # Fall back to bare "ssh" -- git might still find it via its own
        # bundled tools, but warn so the operator knows.
        log.warning("_git_ssh_env: ssh binary not resolvable; falling "
                    "back to bare 'ssh' in GIT_SSH_COMMAND")
        ssh_bin = "ssh"
    # Use forward slashes in the path for SSH portability on Windows;
    # quote both ssh path and key path to handle spaces in paths.
    ssh_path = Path(ssh_bin).as_posix()
    key_path = DEPLOY_KEY_PATH.resolve().as_posix()
    cmd = (f'"{ssh_path}" -i "{key_path}" -o IdentitiesOnly=yes '
           f'-o StrictHostKeyChecking=accept-new')
    return {"GIT_SSH_COMMAND": cmd}


# ── Deploy-key management ───────────────────────────────────────────────────


def deploy_key_present() -> bool:
    return DEPLOY_KEY_PATH.exists() and DEPLOY_KEY_PUB_PATH.exists()


def read_pubkey() -> Optional[str]:
    """Read the public key as text. None if the key hasn't been
    generated yet. Strips trailing newline."""
    if not DEPLOY_KEY_PUB_PATH.exists():
        return None
    try:
        return DEPLOY_KEY_PUB_PATH.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.error("read_pubkey: could not read %s: %s",
                  DEPLOY_KEY_PUB_PATH, e)
        return None


def generate_deploy_key(*, overwrite: bool = False,
                        comment: Optional[str] = None) -> tuple[bool, str]:
    """Generate an ed25519 SSH keypair via ssh-keygen.

    Returns (ok, message). On success message is the public key text.
    On failure message is human-readable error text suitable for the UI.

    `overwrite=False` (default) refuses if a key already exists; the
    UI surfaces a confirm dialog before passing overwrite=True.
    """
    log.info("deploy-key: generate request (overwrite=%s)", overwrite)

    if deploy_key_present() and not overwrite:
        msg = ("Deploy key already exists. Pass overwrite=True to "
               "regenerate (this REVOKES the current key -- you'll need "
               "to remove the old entry from GitHub Deploy keys and add "
               "the new one).")
        log.warning("deploy-key: refused -- %s", msg)
        return False, msg

    # _resolve_binary checks PATH AND known Windows install locations
    # (Windows OpenSSH, Git for Windows bundled OpenSSH).
    if _resolve_binary("ssh-keygen") is None:
        msg = ("ssh-keygen not found on PATH or in known install "
               "locations. Install OpenSSH Client: Settings -> Apps -> "
               "Optional features -> Add -> 'OpenSSH Client'. Or run in "
               "PowerShell as admin: Add-WindowsCapability -Online "
               "-Name OpenSSH.Client*")
        log.error("deploy-key: %s", msg)
        return False, msg

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Remove any existing files so ssh-keygen doesn't prompt.
    for p in (DEPLOY_KEY_PATH, DEPLOY_KEY_PUB_PATH):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            msg = f"could not remove existing {p.name}: {e}"
            log.error("deploy-key: %s", msg)
            return False, msg

    if comment is None:
        try:
            import socket as _socket
            host = _socket.gethostname()
        except Exception:
            host = "host"
        comment = f"sm-manager@{host}"

    cmd = [
        "ssh-keygen",
        "-t", "ed25519",
        "-f", str(DEPLOY_KEY_PATH),
        "-N", "",          # empty passphrase: required for non-interactive use
        "-C", comment,
    ]
    log.info("deploy-key: running ssh-keygen (target=%s, comment=%r)",
             DEPLOY_KEY_PATH, comment)
    result = _run(cmd, timeout_sec=15)
    if not result.ok:
        msg = (f"ssh-keygen exited rc={result.rc}: "
               f"{(result.stderr or result.stdout or result.error or '').strip()}")
        log.error("deploy-key: %s", msg)
        return False, msg

    pubkey = read_pubkey()
    if not pubkey:
        msg = "ssh-keygen completed but the .pub file is missing."
        log.error("deploy-key: %s", msg)
        return False, msg

    # Verbose-only: log the FIRST 30 chars of the pubkey so the audit
    # trail can match the on-disk file to a generation event without
    # leaking the full identity.
    log.info("deploy-key: generated successfully (algo=ed25519, "
             "comment=%r, fingerprint-prefix=%s...)", comment, pubkey[:30])
    return True, pubkey


# ── Git status surface ──────────────────────────────────────────────────────


def get_remote_url() -> Optional[str]:
    """Read `git remote get-url origin`. None if not configured."""
    res = _run(["git", "remote", "get-url", "origin"])
    if not res.ok:
        log.verbose("get_remote_url: git rc=%d stderr=%r",
                    res.rc, res.stderr.strip())
        return None
    url = res.stdout.strip()
    return url or None


_GITHUB_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https?://github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def remote_needs_auth(remote_url: Optional[str]) -> bool:
    """True if the remote URL requires SSH-key auth for git operations.

    SSH remotes (`git@host:repo.git`, `ssh://git@host/...`) need a
    deploy key registered on the remote. HTTPS remotes pull from
    public repos with no auth and from private repos via the git
    credential manager / cached PAT -- either way the manager's
    deploy-key UI doesn't apply.

    Used by the routes to gate the entire Auth-setup section when
    pointing at a public stable repo: showing "Deploy key not
    generated yet" on a clone that pulls fine over HTTPS is
    misleading noise.
    """
    if not remote_url:
        return False
    return remote_url.startswith("git@") or remote_url.startswith("ssh://")


def github_keys_url(remote_url: Optional[str]) -> Optional[str]:
    """Derive the GitHub deploy-keys page URL from a git remote URL.

    Accepts both SSH (`git@github.com:owner/repo.git`) and HTTPS
    (`https://github.com/owner/repo[.git]`) forms. Returns None for
    non-GitHub remotes (self-hosted git, gitlab, etc.) so the UI can
    hide the link rather than send the operator somewhere wrong.
    """
    if not remote_url:
        return None
    m = _GITHUB_REMOTE_RE.match(remote_url.strip())
    if not m:
        return None
    return f"https://github.com/{m['owner']}/{m['repo']}/settings/keys"


def set_remote_url(url: str) -> tuple[bool, str]:
    """Set or replace the origin remote. Returns (ok, message)."""
    log.info("git: setting remote 'origin' to %r", url)
    existing = get_remote_url()
    if existing:
        res = _run(["git", "remote", "set-url", "origin", url])
    else:
        res = _run(["git", "remote", "add", "origin", url])
    if not res.ok:
        return False, (res.stderr or res.error or "").strip() or "git failed"
    return True, "ok"


def ensure_remote() -> tuple[bool, str]:
    """Idempotent: configure `origin` to MANAGER_REMOTE_URL ONLY if no
    remote is configured at all. Once any URL is set, leave it alone --
    the operator may have intentionally chosen HTTPS (for initial push
    via Git Credential Manager) or pointed origin at a fork, and we
    don't want every page render to flip it back.

    Special case: a workstation that shares the .git directory with
    the server (e.g. via SMB mount) would otherwise see the URL flip
    on every /manager-updates render, breaking pushes from the
    workstation without a deploy key.

    Operator can manually switch to the SSH form via the shell:
        git remote set-url origin https://github.com/DonCorleon/sm-manager-stable.git
    """
    current = get_remote_url()
    if current:
        log.verbose("ensure_remote: origin already set to %r; leaving alone",
                    current)
        return True, "already set"
    log.info("ensure_remote: setting origin to %r (no remote was configured)",
             MANAGER_REMOTE_URL)
    return set_remote_url(MANAGER_REMOTE_URL)


def current_sha() -> Optional[str]:
    res = _run(["git", "rev-parse", "HEAD"])
    if not res.ok:
        return None
    return res.stdout.strip() or None


def current_subject() -> Optional[str]:
    res = _run(["git", "log", "-1", "--format=%s", "HEAD"])
    if not res.ok:
        return None
    return res.stdout.strip() or None


def working_tree_dirty() -> bool:
    return bool(dirty_file_list())


def dirty_file_list() -> list[str]:
    """Lines from `git status --porcelain --untracked-files=no`. Each
    line is `XY filename` where XY is the porcelain status code (e.g.
    ` M` = modified-not-staged, `M ` = modified-staged, `??` = untracked
    -- but untracked is filtered by the flag).

    Returns an empty list if the tree is clean. On git failure (rare),
    returns a single synthetic entry so the UI knows to be cautious.
    """
    res = _run(["git", "status", "--porcelain", "--untracked-files=no"])
    if not res.ok:
        log.warning("dirty_file_list: git status failed (rc=%d) -- "
                    "treating as dirty",
                    res.rc)
        return ["?? <git status failed; treating as dirty defensively>"]
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if lines:
        log.verbose("dirty_file_list: %d dirty entries", len(lines))
    return lines


def probe_remote(stream: bool = False) -> tuple[bool, str]:
    """Pre-pull connectivity + auth probe via `git ls-remote origin HEAD`.
    Returns (ok, message). On failure, the message is human-readable
    and distinguishes the three failure modes most likely to matter:
    network down, auth broken (wrong/missing deploy key), remote URL
    not pointing at a real repo. Surfaces failure BEFORE any state-
    changing fetch / pull runs.

    Read-only -- safe to call from any context."""
    if _resolve_binary("git") is None:
        return False, "git not available"
    if not get_remote_url():
        return False, "No 'origin' remote configured."
    if not deploy_key_present():
        return False, ("Deploy key not generated yet. Generate one on "
                       "/manager-updates first.")
    log.verbose("probe_remote: git ls-remote origin HEAD (10s timeout)")
    res = _run(["git", "ls-remote", "origin", "HEAD"],
               extra_env=_git_ssh_env(), timeout_sec=10,
               stream_source="manager" if stream else None)
    if res.ok:
        log.verbose("probe_remote: ok -- %s", res.stdout.strip()[:200])
        return True, "ok"
    err = (res.stderr or res.error or "").strip()
    lower = err.lower()
    if ("timeout" in lower or "could not resolve host" in lower
            or "unable to access" in lower):
        return False, ("Network: cannot reach the git remote. "
                       "(raw: " + err[:200] + ")")
    if "permission denied" in lower or "publickey" in lower or "401" in lower:
        return False, ("Auth: deploy key not accepted by the remote. "
                       "Verify the public key on /manager-updates is "
                       "registered at the repo's Deploy keys page on "
                       "GitHub. (raw: " + err[:200] + ")")
    if "not found" in lower or "does not exist" in lower or "404" in lower:
        return False, ("Remote: repo URL appears wrong or repo deleted "
                       "(404). (raw: " + err[:200] + ")")
    return False, "ls-remote failed: " + (err or f"rc={res.rc}")


def fetch(stream: bool = False) -> tuple[bool, str]:
    """Fetch from origin. Uses GIT_SSH_COMMAND -> deploy key.
    Returns (ok, error_message_or_empty)."""
    if not deploy_key_present():
        return False, "Deploy key not generated yet."
    if not get_remote_url():
        return False, "No 'origin' remote configured."
    # Probe first so failures surface with a clean message rather than
    # raw git output. Same auth/network path the actual fetch uses.
    probe_ok, probe_msg = probe_remote(stream=stream)
    if not probe_ok:
        return False, probe_msg
    res = _run(["git", "fetch", "origin", "main"], extra_env=_git_ssh_env(),
               stream_source="manager" if stream else None)
    if not res.ok:
        err = (res.stderr or res.error or "").strip()
        log.error("git fetch failed: %s", err)
        return False, err or f"git fetch rc={res.rc}"
    log.info("git fetch ok")
    return True, ""


def upstream_sha() -> Optional[str]:
    """SHA of origin/main as currently known LOCALLY (last fetch).
    Returns None if origin/main isn't tracked yet."""
    res = _run(["git", "rev-parse", "origin/main"])
    if not res.ok:
        return None
    return res.stdout.strip() or None


def commits_behind() -> list[tuple[str, str]]:
    """List of (short_sha, subject) for commits between HEAD and
    origin/main. Empty if up to date OR if origin/main isn't tracked."""
    res = _run(["git", "log", "--format=%h\t%s", "HEAD..origin/main"])
    if not res.ok:
        return []
    out: list[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        if "\t" in line:
            sha, _, subj = line.partition("\t")
            out.append((sha.strip(), subj.strip()))
    return out


def get_status() -> UpdateStatus:
    """Compose the full UpdateStatus snapshot for the page."""
    dirty_files = dirty_file_list()
    return UpdateStatus(
        deploy_key_present=deploy_key_present(),
        deploy_pubkey=read_pubkey(),
        remote_url=get_remote_url(),
        current_sha=current_sha(),
        current_subject=current_subject(),
        upstream_sha=upstream_sha(),
        commits_behind=commits_behind(),
        dirty=bool(dirty_files),
        dirty_files=dirty_files,
    )


# ── Connection test (Test connection button) ───────────────────────────────


def test_connection(stream: bool = False) -> tuple[bool, str]:
    """Cheap auth test: `git ls-remote --heads origin main`. Returns
    (ok, message). Verifies the deploy key + remote URL combination."""
    if not deploy_key_present():
        return False, "Deploy key not generated yet."
    remote = get_remote_url()
    if not remote:
        return False, "No 'origin' remote configured."
    log.info("test_connection: ls-remote against %s", remote)
    res = _run(["git", "ls-remote", "--heads", "origin", "main"],
               extra_env=_git_ssh_env(), timeout_sec=20,
               stream_source="manager" if stream else None)
    if not res.ok:
        err = (res.stderr or res.error or "").strip()
        log.error("test_connection: failed (rc=%d): %s", res.rc, err)
        # Hint the most common failure: deploy key not registered.
        hint = ""
        if "Permission denied" in err or "publickey" in err:
            hint = (" -- usually means the deploy key has not been added "
                    "to the repo's Settings → Deploy keys page yet, or it "
                    "was added without 'Allow write access' (read access "
                    "is enough; you might just need to wait a moment).")
        return False, (err or f"git rc={res.rc}") + hint
    head_line = res.stdout.strip().splitlines()[0] if res.stdout.strip() else "(empty)"
    log.info("test_connection: ok (%s)", head_line)
    return True, head_line


# ── Recovery: discard / force-sync ─────────────────────────────────────────
#
# Both functions accept an EXPLICIT `repo_path`. Why: a previous version
# defaulted to `cwd=PROJECT_ROOT` and was called directly from a smoke
# test, which `git reset --hard HEAD`'d the workstation's own repo
# silently throwing away the uncommitted Write changes the test was
# supposed to be verifying. Tests now pass a tempdir + fake `git init`
# repo here. Production callers (route handlers) pass `None` to get the
# PROJECT_ROOT default, which is the intended live behaviour.


def discard_local_changes(repo_path: Optional[Path] = None,
                          stream: bool = False) -> tuple[bool, str]:
    """`git reset --hard HEAD` against repo_path (default: PROJECT_ROOT).

    Reverts every modified-tracked-file change. Untracked / gitignored
    files (`data/`, `logs/`, `portable/`, `steamcmd/`) are untouched.
    Local commits AHEAD of HEAD's last reset point are also untouched
    -- this only undoes uncommitted tracked-file edits.

    Use case: operator copy-pasted files into the working tree on the
    server and Apply now refuses with "dirty tree". One click reverts
    the hand-edits without touching server data."""
    cwd = repo_path if repo_path is not None else PROJECT_ROOT
    log.warning("discard_local_changes: git reset --hard HEAD (cwd=%s)", cwd)
    res = _run(["git", "reset", "--hard", "HEAD"], cwd=cwd,
               stream_source="manager" if stream else None)
    if not res.ok:
        err = (res.stderr or res.error or "").strip()
        log.error("discard_local_changes: failed (rc=%d): %s", res.rc, err)
        return False, err or f"git reset rc={res.rc}"
    summary = res.stdout.strip() or "ok"
    log.info("discard_local_changes: done -- %s", summary[:200])
    return True, summary


def force_sync_to_upstream(repo_path: Optional[Path] = None,
                           stream: bool = False) -> tuple[bool, str]:
    """`git fetch origin main` + `git reset --hard origin/main` against
    repo_path (default: PROJECT_ROOT).

    Most aggressive recovery short of re-clone: discards local commits
    AND uncommitted tracked-file edits, snapping the working tree to
    whatever origin/main currently points at. Untracked / gitignored
    files (`data/`, `logs/`, `portable/`, `steamcmd/`) are untouched.

    Use case: tree got into a state where `discard_local_changes` is
    insufficient (e.g. divergent local commits, or a partially-applied
    pull left rebase state behind). Operator clicks Force-sync to
    return to a known good point.

    When called WITHOUT explicit repo_path (production / route
    handler), runs the full deploy-key + probe preconditions first.
    When called WITH repo_path (test context), skips them on the
    assumption the caller has set up its own fixture."""
    cwd = repo_path if repo_path is not None else PROJECT_ROOT
    if repo_path is None:
        if not deploy_key_present():
            return False, "Deploy key not generated yet."
        if not get_remote_url():
            return False, "No 'origin' remote configured."
        probe_ok, probe_msg = probe_remote()
        if not probe_ok:
            return False, probe_msg
        env = _git_ssh_env()
    else:
        env = None  # test mode: caller controls auth / fixture

    log.warning("force_sync_to_upstream: git fetch origin main (cwd=%s)",
                cwd)
    sourcearg = "manager" if stream else None
    res = _run(["git", "fetch", "origin", "main"],
               cwd=cwd, extra_env=env, timeout_sec=120,
               stream_source=sourcearg)
    if not res.ok:
        err = (res.stderr or res.error or "").strip()
        log.error("force_sync_to_upstream: fetch failed (rc=%d): %s",
                  res.rc, err)
        return False, err or f"git fetch rc={res.rc}"

    log.warning("force_sync_to_upstream: git reset --hard origin/main "
                "(cwd=%s)", cwd)
    res = _run(["git", "reset", "--hard", "origin/main"], cwd=cwd,
               stream_source=sourcearg)
    if not res.ok:
        err = (res.stderr or res.error or "").strip()
        log.error("force_sync_to_upstream: reset failed (rc=%d): %s",
                  res.rc, err)
        return False, err or f"git reset rc={res.rc}"
    summary = res.stdout.strip() or "ok"
    log.info("force_sync_to_upstream: done -- %s", summary[:200])
    return True, summary


# ── Pre-apply compile check (R2) ───────────────────────────────────────────


def compile_check_ref(ref: str = "origin/main",
                       repo_path: Optional[Path] = None,
                       stream: bool = False) -> tuple[bool, str]:
    """Syntax-check every `manager/**/*.py` file at `ref`. Returns
    (ok, message); on failure the message names the offending module
    so the operator sees the actual problem (not just "compile failed").

    Used by `apply_update()` to abort BEFORE pulling code that won't
    even parse. Catches `SyntaxError`. Does NOT catch `NameError` /
    `ImportError` at module top -- that requires actually executing
    the module, and we don't want to import unreviewed code in this
    process. Module-level errors are caught later by the bootloader's
    crash-loop detection.

    Implementation: `git archive --format=tar` to a single .tar file,
    then iterate the tar members in memory and call `compile()` on
    each .py file's bytes. Avoids `tarfile.extractall` which on
    Windows takes 60-90s for ~100 files because Defender real-time
    protection serially scans every newly-created file in
    %LOCALAPPDATA%\\Temp\\. The single .tar gets scanned once (~ms);
    everything else stays in memory.

    repo_path defaults to PROJECT_ROOT; tests pass a tempdir. Read-only
    (git archive doesn't mutate); safe to run from any context."""
    import tarfile
    import tempfile
    from manager import updates_log

    cwd = repo_path if repo_path is not None else PROJECT_ROOT
    sourcearg = "manager" if stream else None

    # NamedTemporaryFile with delete=False so we can close it before
    # git archive writes to it (Windows can't share an open file with
    # another writer). We unlink in finally.
    with tempfile.NamedTemporaryFile(prefix="sm-update-compile-",
                                      suffix=".tar",
                                      delete=False) as tmp:
        tar_path = Path(tmp.name)

    try:
        log.verbose("compile_check_ref: git archive %s -> %s", ref, tar_path)
        res = _run(["git", "archive", "--output", str(tar_path),
                    "--format=tar", ref],
                   cwd=cwd, stream_source=sourcearg)
        if not res.ok:
            err = (res.stderr or res.error or "").strip()
            return False, f"git archive {ref}: " + (err or f"rc={res.rc}")

        log.info("compile_check_ref: in-memory syntax-check of %s", ref)
        if stream:
            updates_log.append("manager",
                               f"compile-check: scanning manager/**/*.py "
                               f"at {ref}")

        try:
            with tarfile.open(tar_path) as tf:
                count = 0
                saw_manager_dir = False
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    name = m.name
                    if not name.startswith("manager/"):
                        continue
                    saw_manager_dir = True
                    if not name.endswith(".py"):
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    src = f.read()
                    try:
                        compile(src, name, "exec")
                    except SyntaxError as e:
                        # e.lineno / e.offset / e.msg give a precise location
                        msg = (f"{name}:{e.lineno or '?'}: "
                               f"{e.msg}")
                        log.error("compile_check_ref: %s", msg)
                        if stream:
                            updates_log.append(
                                "manager",
                                f"compile-check: SYNTAX ERROR in {msg}")
                        return False, msg[:500]
                    count += 1
        except (tarfile.TarError, OSError) as e:
            return False, f"tar read failed: {e}"

        if not saw_manager_dir:
            return False, (f"upstream tree at {ref} has no manager/ "
                           "directory -- not a manager checkout?")

        log.info("compile_check_ref: %s passes (%d .py file(s))", ref, count)
        if stream:
            updates_log.append("manager",
                               f"compile-check: {count} .py file(s) OK")
        return True, "ok"
    finally:
        try:
            tar_path.unlink()
        except OSError:
            log.exception("compile_check_ref: failed to unlink %s "
                          "(non-fatal)", tar_path)


# ── Circuit breaker (R7) ───────────────────────────────────────────────────
#
# After enough consecutive failed Applies inside a rolling window, the
# breaker trips: Apply is disabled until the operator clicks Reset on
# the UI. Stops the "auto-update keeps trying every poll, keeps
# failing, fills the log" loop and forces a human to look at the
# actual error before re-arming.

CIRCUIT_BREAKER_FILE = DATA_DIR / "update_circuit_breaker.json"
CIRCUIT_BREAKER_THRESHOLD = 3
CIRCUIT_BREAKER_WINDOW_HOURS = 24


def _breaker_path(path: Optional[Path] = None) -> Path:
    return path if path is not None else CIRCUIT_BREAKER_FILE


def _read_breaker(path: Optional[Path] = None) -> dict:
    p = _breaker_path(path)
    if not p.exists():
        return {"failures": [], "last_error": ""}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"failures": [], "last_error": ""}
        return {
            "failures": [s for s in data.get("failures", [])
                         if isinstance(s, str)],
            "last_error": str(data.get("last_error", "")),
        }
    except (OSError, json.JSONDecodeError) as e:
        log.warning("circuit breaker: unreadable state at %s: %s -- "
                    "treating as empty", p, e)
        return {"failures": [], "last_error": ""}


def _write_breaker(state: dict, path: Optional[Path] = None) -> None:
    p = _breaker_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp.replace(p)


def _trim_failures(failures: list[str], now: Optional[datetime] = None
                   ) -> list[str]:
    """Drop ISO-format timestamps older than the window."""
    cutoff = (now or datetime.now()) - timedelta(
        hours=CIRCUIT_BREAKER_WINDOW_HOURS)
    out: list[str] = []
    for s in failures:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            continue
        if dt >= cutoff:
            out.append(s)
    return out


def breaker_status(path: Optional[Path] = None) -> dict:
    """Snapshot for the UI. Trims expired failure timestamps before
    counting so a breaker tripped 25h ago auto-clears."""
    state = _read_breaker(path)
    state["failures"] = _trim_failures(state["failures"])
    n = len(state["failures"])
    return {
        "tripped": n >= CIRCUIT_BREAKER_THRESHOLD,
        "failures_in_window": n,
        "last_error": state["last_error"],
        "window_hours": CIRCUIT_BREAKER_WINDOW_HOURS,
        "threshold": CIRCUIT_BREAKER_THRESHOLD,
    }


def record_apply_failure(error: str, path: Optional[Path] = None) -> dict:
    """Log a failed Apply. Returns the new breaker_status() snapshot.
    Caller can inspect `tripped` to know if the breaker just clicked."""
    state = _read_breaker(path)
    state["failures"] = _trim_failures(state["failures"])
    state["failures"].append(datetime.now().isoformat(timespec="seconds"))
    state["last_error"] = (error or "").strip()[:500]
    _write_breaker(state, path)
    n = len(state["failures"])
    if n >= CIRCUIT_BREAKER_THRESHOLD:
        log.error("circuit breaker TRIPPED: %d failed Applies in %dh "
                  "window. Auto-updates paused until manual reset. "
                  "Last error: %s", n, CIRCUIT_BREAKER_WINDOW_HOURS,
                  state["last_error"][:200])
    else:
        log.warning("circuit breaker: failure %d/%d in %dh window",
                    n, CIRCUIT_BREAKER_THRESHOLD,
                    CIRCUIT_BREAKER_WINDOW_HOURS)
    return breaker_status(path)


def record_apply_success(path: Optional[Path] = None) -> None:
    """Apply went through. Clear all prior failures + last_error so the
    breaker is fully re-armed for any future incident."""
    state = _read_breaker(path)
    if state["failures"] or state["last_error"]:
        log.info("circuit breaker: Apply succeeded; clearing %d prior "
                 "failure(s)", len(state["failures"]))
    _write_breaker({"failures": [], "last_error": ""}, path)


def reset_breaker(path: Optional[Path] = None) -> None:
    """Manual reset by the operator via the UI. Same as success-clear
    but logged at WARNING since it's an explicit human override."""
    state = _read_breaker(path)
    log.warning("circuit breaker: manual reset (had %d failure(s), "
                "last error: %s)",
                len(state["failures"]), state["last_error"][:200])
    _write_breaker({"failures": [], "last_error": ""}, path)


# ── Apply ───────────────────────────────────────────────────────────────────


def apply_update(stream: bool = False) -> tuple[bool, str]:
    """Run `git pull --ff-only origin main`. Returns (ok, message). On
    success, the manager should be restarted via the existing exit-99
    supervisor; routes layer schedules that.

    Order of operations:
      0. Circuit breaker check (R7). If 3+ Applies have failed in the
         last 24h, refuse until operator manually resets.
      1. Auth + clean-tree preconditions.
      2. Fetch origin/main so the local ref is current.
      3. Compile-check the upstream tree (R2). If it fails to byte-
         compile we abort here -- shipping broken code would crash the
         manager on next boot.
      4. ff-only pull (always succeeds at this point: we already
         fetched and verified).

    Any False return path records the failure with the breaker; True
    returns clear it. Force-sync deliberately bypasses the breaker
    since it's the recovery path the operator uses WHEN Apply is
    broken."""
    bs = breaker_status()
    if bs["tripped"]:
        return False, ("Auto-updates paused: circuit breaker tripped "
                       f"after {bs['failures_in_window']} failed Applies "
                       f"in the last {bs['window_hours']}h. "
                       f"Last error: {bs['last_error']}\n\n"
                       "Investigate the underlying issue, then click "
                       "'Reset breaker' on the Updates page to re-arm.")

    if not deploy_key_present():
        msg = "Deploy key not generated yet."
        record_apply_failure(msg)
        return False, msg
    if not get_remote_url():
        msg = "No 'origin' remote configured."
        record_apply_failure(msg)
        return False, msg
    if working_tree_dirty():
        msg = ("Working tree has uncommitted changes. Resolve or stash "
               "them on the server before applying an update -- ff-only "
               "pull won't merge.")
        # Dirty tree is a "your repo state is wrong" failure, not a
        # transient one. It WILL keep failing every retry until the
        # operator intervenes -- exactly what the breaker is for.
        record_apply_failure(msg)
        return False, msg

    ok, msg = fetch(stream=stream)
    if not ok:
        record_apply_failure(msg)
        return False, msg

    ok, msg = compile_check_ref("origin/main", stream=stream)
    if not ok:
        full_msg = ("Pre-apply compile check rejected upstream "
                    "(would crash on boot). Aborted before pulling. "
                    "Details:\n" + msg)
        record_apply_failure(full_msg)
        return False, full_msg

    log.info("apply_update: git pull --ff-only origin main")
    res = _run(["git", "pull", "--ff-only", "origin", "main"],
               extra_env=_git_ssh_env(),
               timeout_sec=120,
               stream_source="manager" if stream else None)
    if not res.ok:
        err = (res.stderr or res.error or "").strip()
        log.error("apply_update: pull failed (rc=%d): %s", res.rc, err)
        full = err or f"git pull rc={res.rc}"
        record_apply_failure(full)
        return False, full
    log.info("apply_update: pull complete: %s", res.stdout.strip()[:300])
    record_apply_success()
    return True, res.stdout.strip()


SHUTDOWN_CLEAN_MARKER = DATA_DIR / ".shutdown_clean"


def write_shutdown_clean_marker() -> None:
    """Touch the marker that bootloader.py checks on next boot. If
    absent at boot, the previous run terminated uncleanly (kill -9,
    power loss, BSOD, mid-pull SIGINT) and bootloader runs
    `git fsck --no-progress` + `git gc --auto` to clean up any
    half-written objects from an interrupted git op (R9)."""
    try:
        SHUTDOWN_CLEAN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SHUTDOWN_CLEAN_MARKER.touch()
        log.debug("Shutdown-clean marker written: %s", SHUTDOWN_CLEAN_MARKER)
    except OSError:
        log.exception("Could not write shutdown-clean marker (non-fatal)")


def schedule_restart_after(delay_sec: float = 1.5) -> None:
    """Same exit-99 mechanism the /settings/restart-manager route uses.
    Run from a daemon thread so the response can flush first."""
    import threading

    def _delayed():
        threading.Event().wait(delay_sec)
        log.warning("self_update: scheduled exit(%d) firing now",
                    _RESTART_EXIT_CODE)
        # Mark this as a clean shutdown -- the marker survives os._exit
        # since the file write completes before exit. Bootloader on
        # next launch sees it and skips fsck/repack.
        write_shutdown_clean_marker()
        # R1: clear data/.boot_in_progress so the bootloader doesn't
        # count an Apply-or-ForceSync-triggered restart as a fast
        # crash. mark_boot_stable clears this marker after 60s of
        # uptime, but Apply usually fires well before that window
        # (operator clicks Apply seconds after the page loads).
        try:
            marker = DATA_DIR / ".boot_in_progress"
            if marker.exists():
                marker.unlink()
        except OSError:
            log.exception("Could not clear boot_in_progress marker "
                          "(non-fatal)")
        time.sleep(0)  # noop (kept for symmetry with settings flow)
        os._exit(_RESTART_EXIT_CODE)

    log.warning("self_update: manager restart scheduled in %.1fs "
                "(exit code %d)", delay_sec, _RESTART_EXIT_CODE)
    threading.Thread(target=_delayed, daemon=True,
                     name="self-update-restart").start()


# ── Background poller (manager-update awareness on the dashboard) ──────────


_poll_state_lock = threading.Lock()
_poll_state: dict = {
    "last_check_at": None,        # datetime or None
    "last_check_ok": False,
    "last_error": "",
    "current_sha": None,
    "current_subject": None,
    "upstream_sha": None,
    "commits_behind_count": 0,    # quick render-time number
}

_poll_started = False
_poll_started_lock = threading.Lock()


def _manager_poll_interval_sec() -> int:
    """Poll cadence for manager update checks. Default 60 min --
    git fetch is cheap but the manager update cadence is human, not
    machine-paced, so polling every minute would just be noise."""
    return int(get_setting("manager_updates.poll_interval_min", 60)) * 60


def _manager_poll_enabled() -> bool:
    """Toggle exposed in Settings. Default ON so the operator finds out
    about updates without having to remember to check."""
    raw = get_setting("manager_updates.poll_enabled", True)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("on", "true", "1", "yes")


def get_poll_state() -> dict:
    """Snapshot of the most recent poll result for the dashboard. Cheap
    -- no subprocess work happens here, only state-cache read."""
    with _poll_state_lock:
        snap = dict(_poll_state)
    snap["last_check_at_human"] = (
        snap["last_check_at"].strftime("%Y-%m-%d %H:%M:%S")
        if snap["last_check_at"] else ""
    )
    snap["update_available"] = bool(snap.get("commits_behind_count"))
    snap["enabled"] = _manager_poll_enabled()
    return snap


def check_for_updates_now(stream: bool = False) -> dict:
    """Run one fetch + status compute synchronously and update poll
    state. Used both by the poller AND by /manager-updates 'Check for
    updates' button so both paths share state.

    `stream=True` writes to updates_log for the SSE consumer; the
    periodic poller passes False to stay silent."""
    if not deploy_key_present():
        log.verbose("manager-update check skipped: no deploy key yet")
        with _poll_state_lock:
            _poll_state["last_check_at"] = datetime.now()
            _poll_state["last_check_ok"] = False
            _poll_state["last_error"] = "no deploy key"
        return get_poll_state()

    if _resolve_binary("git") is None:
        log.verbose("manager-update check skipped: git not resolvable")
        with _poll_state_lock:
            _poll_state["last_check_at"] = datetime.now()
            _poll_state["last_check_ok"] = False
            _poll_state["last_error"] = "git not available"
        return get_poll_state()

    log.info("Manager update check starting (git fetch origin main)...")
    ok, msg = fetch(stream=stream)
    with _poll_state_lock:
        _poll_state["last_check_at"] = datetime.now()
        if not ok:
            _poll_state["last_check_ok"] = False
            _poll_state["last_error"] = msg or "fetch failed"
            log.warning("Manager update check FAILED: %s", msg)
            return get_poll_state()
        _poll_state["last_check_ok"] = True
        _poll_state["last_error"] = ""
        _poll_state["current_sha"] = current_sha()
        _poll_state["current_subject"] = current_subject()
        _poll_state["upstream_sha"] = upstream_sha()
        behind = commits_behind()
        _poll_state["commits_behind_count"] = len(behind)
        if behind:
            log.info("Manager update check OK: %d commit(s) behind upstream",
                     len(behind))
        else:
            log.info("Manager update check OK: up to date")
    return get_poll_state()


def start_update_poller() -> None:
    """Start the background manager-update poller exactly once.
    Mirrors manager.updates.start_poller for parity."""
    global _poll_started
    with _poll_started_lock:
        if _poll_started:
            return
        _poll_started = True
    log.info("Starting manager-update poller: interval %ds (enabled=%s)",
             _manager_poll_interval_sec(), _manager_poll_enabled())
    threading.Thread(target=_update_poller_loop, daemon=True,
                     name="manager-update-poller").start()


def _update_poller_loop() -> None:
    # Initial check after a small delay so boot logs settle and the
    # ssh agent / known_hosts paths have a moment to materialise.
    threading.Event().wait(20)
    while True:
        if not _manager_poll_enabled():
            log.verbose("manager-update poller: disabled in settings; "
                        "sleeping a full interval")
            threading.Event().wait(_manager_poll_interval_sec())
            continue
        # Mutual exclusion with operator-clicked git ops: if an op
        # currently holds _git_lock (Apply / Check / etc.), skip this
        # iteration silently and try again next interval. Avoids two
        # simultaneous `git fetch` racing on the same repo.
        if not _git_lock.acquire(blocking=False):
            log.debug("manager-update poller: skipping iteration -- "
                      "git lock held (operator op in flight)")
        else:
            try:
                check_for_updates_now()
            except Exception:
                log.exception("manager-update poller iteration crashed")
            finally:
                try:
                    _git_lock.release()
                except Exception:
                    log.exception("poller: _git_lock release raised "
                                  "(non-fatal)")
        # Notify any open dashboard SSE subscribers so the card refreshes
        # on next render without waiting for the regular timer tick.
        try:
            from manager import dashboard_events
            dashboard_events.notify()
        except Exception:
            pass
        threading.Event().wait(_manager_poll_interval_sec())


# ── Async kick-off wrappers (user-triggered, stream to updates_log) ────────
#
# Each /updates/* button enqueues onto manager.background and returns
# immediately. The Flask request thread never blocks on git/network. The
# operator watches the live output via the /updates/sse stream.
#
# ── Mutual exclusion ──
# `_git_lock` serialises all git work on this repo: the periodic poller
# AND every operator-clicked git op (apply / check / test_connection /
# discard / force_sync). Acquired non-blocking. If held when an
# operator clicks, the click is refused with a user-facing message
# (the auto-update poller is probably mid-fetch). If held when the
# poller wakes up, the poller skips that iteration silently.
#
# Without this lock, two simultaneous `git fetch` against the same
# repo race on .git/refs/remotes/*.lock; an apply pull body could see
# intermediate state. Failure mode is rare in practice (poller fires
# every ~10 min, operator clicks are rare) but real and ugly when it
# bites.

_git_lock = threading.Lock()


def try_acquire_git_lock() -> bool:
    """Non-blocking acquire of the cross-cutting git op lock. Returns
    True if the caller now holds the lock (and MUST eventually call
    `_git_lock.release()`); False otherwise."""
    return _git_lock.acquire(blocking=False)


def _bg_run_under_op(op_label: str, work, on_success=None,
                     release_lock: Optional[threading.Lock] = None) -> None:
    """Internal runner: called on the background-worker thread. Brackets
    `work` with begin_op/end_op so the SSE consumer sees a clean
    transition. `on_success` (optional callable) runs after a successful
    work() -- used by Apply to schedule the restart only on success.
    `release_lock` (optional) is released in a finally block so the lock
    is always returned, even if work() raises."""
    from manager import updates_log
    try:
        try:
            ok, msg = work()
        except Exception as e:
            log.exception("update op %r raised", op_label)
            updates_log.end_op(False, f"{type(e).__name__}: {e}")
            return
        updates_log.end_op(ok, "" if ok else msg)
        if ok and on_success is not None:
            try:
                on_success()
            except Exception:
                log.exception("update op %r post-success hook raised",
                              op_label)
    finally:
        if release_lock is not None:
            try:
                release_lock.release()
            except Exception:
                # Lock not held / released twice -- non-fatal; log
                # so the bug surfaces but don't crash the worker.
                log.exception("release_lock raised in _bg_run_under_op "
                              "(non-fatal)")


def _start_git_op(op_name: str, queue_label: str, work,
                  on_success=None) -> bool:
    """Shared body for start_apply / start_check / start_test_connection
    / start_discard / start_force_sync. Returns True if the op was
    queued; False if either the git lock is held (poller mid-fetch or
    another operator op) OR updates_log already shows a running op.

    Acquires _git_lock first; releases via _bg_run_under_op's finally
    so the lock is always returned even on exception."""
    if not try_acquire_git_lock():
        log.info("start_git_op: refused %r -- git lock held "
                 "(poller mid-fetch or another op in flight)", op_name)
        return False
    from manager import background, updates_log
    if not updates_log.begin_op(op_name, "manager"):
        # Defensive: lock was acquired but updates_log refuses. Release
        # so the next caller can succeed.
        try:
            _git_lock.release()
        except Exception:
            pass
        return False
    background.submit(queue_label, _bg_run_under_op,
                      op_name, work, on_success, _git_lock)
    return True


def start_apply() -> bool:
    """Kick off `apply_update(stream=True)` on the background worker.
    On success schedules the manager restart. Returns False if either
    another git op is already running OR the auto-update poller is
    mid-fetch."""
    return _start_git_op(
        "Apply manager update", "updates-apply",
        lambda: apply_update(stream=True),
        lambda: schedule_restart_after(delay_sec=1.5),
    )


def start_check() -> bool:
    """Kick off `check_for_updates_now(stream=True)`. Returns False if
    another git op is in progress (manual or poller)."""
    def _work():
        state = check_for_updates_now(stream=True)
        # check_for_updates_now returns a dict; convert to (ok, msg) for
        # _bg_run_under_op's contract.
        return (bool(state.get("last_check_ok")),
                state.get("last_error", ""))

    return _start_git_op("Check for manager updates", "updates-check", _work)


def start_test_connection() -> bool:
    return _start_git_op(
        "Test git connection", "updates-test-connection",
        lambda: test_connection(stream=True),
    )


def start_discard() -> bool:
    return _start_git_op(
        "Discard local changes", "updates-discard",
        lambda: discard_local_changes(stream=True),
    )


def start_force_sync() -> bool:
    """Force-sync to upstream + restart on success."""
    return _start_git_op(
        "Force-sync to upstream", "updates-force-sync",
        lambda: force_sync_to_upstream(stream=True),
        lambda: schedule_restart_after(delay_sec=1.5),
    )
