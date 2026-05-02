"""Boot-time dependency check.

Runs before any manager submodule imports a third-party dep. Reads
requirements.txt, sees what's installed via importlib.metadata, and
pip-installs any that are missing. Catches the case where the
manager self-updates to a commit that adds a new dep AND the
.bat-driven `pip install` step is skipped (the exit-99 restart loop
does NOT re-run pip; only a fresh `start_manager.bat` does).

stdlib-only on purpose -- this runs BEFORE manager.app/etc are
imported, so it cannot rely on Flask/requests/psutil/etc. being
present.

Output goes to stderr (no logging configured yet at this point).
"""

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

# Path to requirements.txt next to start_manager.bat (project root).
_REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"

# Match the leading distribution name in a requirement line, e.g.:
#   "Pillow>=10.0"      -> "Pillow"
#   "python-a2s>=1.3"   -> "python-a2s"
#   "flask"             -> "flask"
#   "pkg[extra]>=1.0"   -> "pkg"
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)")


def _parse_requirements(path: Path) -> list[tuple[str, str]]:
    """Return [(dist_name, raw_line), ...] for non-comment lines."""
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _NAME_RE.match(line)
        if not m:
            continue
        out.append((m.group(1), line))
    return out


def _is_installed(dist_name: str) -> bool:
    try:
        metadata.distribution(dist_name)
        return True
    except metadata.PackageNotFoundError:
        return False


def ensure_requirements() -> None:
    """If any line in requirements.txt names a dist that isn't
    importable, run pip install -r requirements.txt.

    Best-effort: missing requirements.txt or a pip failure is logged
    to stderr but does not abort. The downstream import block in
    __main__ will surface a clear error if a critical dep is still
    missing after this runs.
    """
    if not _REQUIREMENTS.is_file():
        sys.stderr.write(
            f"[deps] requirements.txt not found at {_REQUIREMENTS} -- "
            "skipping auto-install check\n")
        return

    try:
        reqs = _parse_requirements(_REQUIREMENTS)
    except Exception as e:
        sys.stderr.write(f"[deps] failed to parse requirements.txt: {e}\n")
        return

    missing = [(name, line) for name, line in reqs if not _is_installed(name)]
    if not missing:
        return

    sys.stderr.write(
        "[deps] {} requirement(s) missing; auto-installing...\n".format(
            len(missing)))
    for name, line in missing:
        sys.stderr.write(f"[deps]   - {line}\n")

    cmd = [
        sys.executable, "-u", "-m", "pip", "install",
        "--disable-pip-version-check",
        "--requirement", str(_REQUIREMENTS),
    ]
    # Stream pip's output through to stderr in real time, byte-by-byte.
    # subprocess.run() inherits the parent's stdio, which on Windows
    # is fully buffered when piped through a .bat -- so pip's progress
    # accumulates invisibly until the buffer fills or the process
    # dies, making `/updates/apply` look like it has hung. Use Popen
    # + os-level byte reads so the operator sees pip output land on
    # the terminal as it happens. Same model the WS.log tailer uses.
    try:
        # bufsize default (-1) gives a BufferedReader on stdout whose
        # `read1(n)` returns whatever bytes are immediately available
        # without waiting for the buffer to fill. bufsize=0 returns a
        # raw FileIO that has no read1 method, so the read loop below
        # would AttributeError on first read.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        sys.stderr.write(f"[deps] pip invocation failed: {e}\n")
        return

    try:
        assert proc.stdout is not None
        while True:
            # read1 returns whatever bytes are immediately available
            # without waiting for a newline / buffer fill. Empty bytes
            # = EOF (process closed stdout).
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            try:
                sys.stderr.buffer.write(chunk)
                sys.stderr.flush()
            except Exception:
                # If stderr is unwritable for any reason, just drain
                # the pipe so the child doesn't wedge on a full pipe.
                pass
        rc = proc.wait()
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        sys.stderr.write(f"[deps] pip stream loop failed: {e}\n")
        return

    if rc != 0:
        sys.stderr.write(
            f"[deps] pip exited {rc} -- some deps may still "
            "be missing; the import block in __main__ will surface a "
            "fatal error if so\n")
        return

    # Re-check so we can confirm in stderr that the install actually
    # resolved everything (e.g. wheel mismatch could still leave a
    # dist missing even with rc=0).
    still_missing = [name for name, _ in missing if not _is_installed(name)]
    if still_missing:
        sys.stderr.write(
            "[deps] WARN after pip install, still missing: "
            + ", ".join(still_missing) + "\n")
    else:
        sys.stderr.write("[deps] all requirements satisfied\n")
