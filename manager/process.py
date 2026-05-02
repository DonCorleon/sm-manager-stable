"""Single-instance process control for one WSServer.exe.

Wraps subprocess.Popen + a cached psutil.Process so we can read CPU% and
memory cheaply on every status poll. The psutil.Process accumulates CPU
time between calls -- first call returns 0 (baseline), subsequent calls
return % over the interval since the prior call. Polling every 5 sec gives
us a 5-second-window average.
"""

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil

log = logging.getLogger(__name__)

# Cache once: number of logical cores. psutil.cpu_percent on a process is
# per-core (can exceed 100% for multi-threaded), but Task Manager normalises
# to total system capacity. We divide by core count so dashboard numbers
# match what you see in Task Manager when you compare side by side.
_CPU_COUNT = psutil.cpu_count(logical=True) or 1


def _safe_name(p: psutil.Process) -> str:
    """psutil.Process.name() can raise after the process exits; tolerate it."""
    try:
        return p.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "?"


class InstanceProcess:
    """Tracks a single WSServer.exe spawn AND its descendant tree.

    The WSServer.exe we spawn is just a wrapper. It launches
    BootstrapPackagedGame.exe which in turn launches WS-Win64-Shipping.exe
    (the actual game). The wrapper itself sits near 0% CPU / sub-MB RAM
    while the descendants do the real work, so reporting only the wrapper's
    stats is misleading.

    We resolve this by walking the process tree on every stats call and
    summing across the wrapper plus all descendants. psutil.Process objects
    are cached per PID so cpu_percent() can compute meaningful intervals
    between polls.
    """

    def __init__(self, runtime_instance, install_root: Path):
        self.runtime_instance = runtime_instance  # wizard.RuntimeInstance
        self.install_root = install_root
        self.popen: Optional[subprocess.Popen] = None
        self.started_at: Optional[datetime] = None
        self._psu: Optional[psutil.Process] = None
        self.adopted: bool = False  # True if we found this process running on startup
        # Set by lifecycle._stop_impl when SaveAndExit has been ACK'd by the
        # server. Cleared on next .start(). Used so the dashboard can show
        # "shutting down" between the ACK and the actual tree exit (the
        # countdown + save phase, often 30-90s).
        self.shutdown_requested_at: Optional[datetime] = None
        # Set by routes.dashboard.cancel_shutdown when the operator clicks
        # Cancel during a countdown. The waiting _stop_impl loop sees this
        # flag and either: (a) confirms cc cancelled the shutdown (instance
        # still alive after a settle window) and clears shutdown state, or
        # (b) discovers the instance died anyway (countdown was 0s, cc was
        # too late) and sets auto_restart_after_death so _stop_impl spins
        # the instance back up before releasing the op lock.
        self.cancel_pending: bool = False
        self.auto_restart_after_death: bool = False
        # Cache of psutil.Process per PID across the whole tree. New children
        # added on first sighting (and primed for cpu_percent); dead ones
        # pruned each refresh.
        self._tree_cache: dict[int, psutil.Process] = {}

    @classmethod
    def adopt(cls, runtime_instance, install_root: Path,
              psu_proc: psutil.Process) -> "InstanceProcess":
        """Wrap an already-running WSServer.exe that the manager did not
        spawn (e.g. survived a manager restart). No Popen handle, so
        liveness is tracked entirely via psutil. Started_at comes from the
        OS process create time so uptime stays accurate."""
        inst = cls(runtime_instance, install_root)
        inst._psu = psu_proc
        inst.adopted = True
        try:
            inst.started_at = datetime.fromtimestamp(psu_proc.create_time())
            psu_proc.cpu_percent(interval=None)  # prime baseline
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            log.warning("[%s] adopt: psutil call failed: %s", inst.name, e)
        return inst

    @property
    def name(self) -> str:
        return self.runtime_instance.instance.name

    @property
    def pid(self) -> Optional[int]:
        # Adopted processes have no Popen handle; PID comes from psutil.
        if self.popen:
            return self.popen.pid
        if self._psu:
            return self._psu.pid
        return None

    @property
    def is_alive(self) -> bool:
        """True if the wrapper OR any descendant in the tree is still running.

        Windows does NOT kill children when the parent dies, so 'the server'
        being up is really 'any process in the tree is up'. The actual game
        runs in WS-Win64-Shipping.exe, a grandchild of our spawned wrapper.

        Adopted processes (no Popen handle) check via psutil only.

        Side-effect: refreshes the tree cache so this also picks up children
        spawned since the last poll, and drops PIDs that have exited.
        """
        self._refresh_tree()
        if self.popen and self.popen.poll() is None:
            return True
        # No Popen handle (adopted) or wrapper dead -- check the tree cache.
        return len(self._tree_cache) > 0

    @property
    def is_wrapper_alive(self) -> bool:
        """Just the WSServer.exe wrapper, ignoring descendants."""
        if self.popen:
            return self.popen.poll() is None
        # Adopted: no Popen, but _psu IS the wrapper.
        if self._psu:
            try:
                return self._psu.is_running() and self._psu.status() != psutil.STATUS_ZOMBIE
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return False
        return False

    def start(self, args: list[str]) -> None:
        """Spawn WSServer.exe with the given arg list. The first arg should be
        the map name (positional); subsequent are -flag/-key=value pairs.
        Detached console so the server keeps running independent of us."""
        exe = self.install_root / "WSServer.exe"
        cmd = [str(exe), *args]
        log.info("[%s] spawning WSServer.exe", self.name)
        log.debug("  cwd: %s", self.install_root)
        log.debug("  cmd: %s", cmd)

        creationflags = 0
        if sys.platform == "win32":
            # Give the server its own console window. Useful for the operator
            # to observe directly via RDP, AND ensures Ctrl+C in our manager
            # doesn't propagate to the server.
            creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]

        self.popen = subprocess.Popen(
            cmd,
            cwd=str(self.install_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,  # the server logs to its own WS.log
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self.started_at = datetime.now()
        # Fresh start -- clear any leftover lifecycle flags from prior life.
        self.shutdown_requested_at = None
        self.cancel_pending = False
        self.auto_restart_after_death = False
        log.info("[%s] PID %d", self.name, self.popen.pid)

        # Set up psutil so subsequent get_stats() can return CPU%.
        try:
            self._psu = psutil.Process(self.popen.pid)
            # First call seeds the baseline; the value it returns isn't useful.
            self._psu.cpu_percent(interval=None)
        except psutil.NoSuchProcess:
            log.warning("[%s] process disappeared immediately after spawn", self.name)
            self._psu = None

    def _refresh_tree(self) -> list[psutil.Process]:
        """Refresh the cache of psutil.Process objects for our wrapper +
        descendants. Two responsibilities:
          1) If the wrapper is alive, walk its children(recursive=True) and
             pick up any newly-spawned descendants (e.g. WS-Win64-Shipping
             coming up after BootstrapPackagedGame).
          2) Drop any cached PIDs that have actually exited. This is what
             lets is_alive return False once the tree is truly dead -- even
             if the wrapper went away first and we can't walk from it.
        Primes cpu_percent on first sight so subsequent polls return
        meaningful intervals.
        """
        if self._psu is None and self.popen:
            try:
                self._psu = psutil.Process(self.popen.pid)
                self._psu.cpu_percent(interval=None)
            except psutil.NoSuchProcess:
                self._psu = None

        # 1) Walk from wrapper if we can.
        seen_via_wrapper: set[int] = set()
        if self._psu is not None:
            try:
                tree = [self._psu] + self._psu.children(recursive=True)
                for p in tree:
                    seen_via_wrapper.add(p.pid)
                    if p.pid not in self._tree_cache:
                        self._tree_cache[p.pid] = p
                        try:
                            p.cpu_percent(interval=None)
                            log.debug("[%s] tracking new descendant PID %d (%s)",
                                      self.name, p.pid, _safe_name(p))
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # Wrapper gone -- cache is now the only source of truth.
                pass

        # 2) Validate every cached PID independently. A cached process can be
        # dead even if we couldn't enumerate it via children() (wrapper gone).
        for pid in list(self._tree_cache):
            try:
                p = self._tree_cache[pid]
                if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                    self._tree_cache.pop(pid, None)
                    log.debug("[%s] descendant PID %d exited", self.name, pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._tree_cache.pop(pid, None)
                log.debug("[%s] descendant PID %d gone", self.name, pid)

        return list(self._tree_cache.values())

    def get_stats(self) -> Optional[dict]:
        """Aggregate CPU% and RSS across wrapper + descendants."""
        if not self.is_alive:
            return None
        tree = self._refresh_tree()
        if not tree:
            return None

        total_cpu = 0.0
        total_rss = 0
        live_pids: list[int] = []
        for p in tree:
            try:
                total_cpu += p.cpu_percent(interval=None)
                total_rss += p.memory_info().rss
                live_pids.append(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Normalise per-core CPU% to total system CPU% (Task Manager style).
        total_cpu_normalised = total_cpu / _CPU_COUNT
        log.debug("[%s] stats across %d procs (PIDs %s): "
                  "cpu=%.1f%% (raw %.1f%% / %d cores) rss=%.0fMB",
                  self.name, len(live_pids), live_pids,
                  total_cpu_normalised, total_cpu, _CPU_COUNT,
                  total_rss / (1024 * 1024))
        return {
            "cpu_percent": total_cpu_normalised,
            "memory_mb": total_rss / (1024 * 1024),
        }

    def kill(self) -> None:
        """Hard-terminate the WHOLE tree -- wrapper + every descendant.

        Killing only the wrapper (Popen.kill) is what we used to do, and it
        was wrong: BootstrapPackagedGame.exe and WS-Win64-Shipping.exe survived
        as orphans, the manager reported 'stopped', but the server was still
        chewing RAM. Now we walk the tree first and kill children before the
        parent so they can't escape.
        """
        # Build PID set: wrapper + live tree children + cached descendants.
        pids: set[int] = set()
        if self.popen:
            pids.add(self.popen.pid)
        try:
            if self._psu and self._psu.is_running():
                for child in self._psu.children(recursive=True):
                    pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        pids.update(self._tree_cache.keys())

        if not pids:
            log.debug("[%s] kill: no PIDs to kill", self.name)
            return

        log.warning("[%s] hard-killing tree: PIDs %s", self.name, sorted(pids))
        for pid in pids:
            try:
                p = psutil.Process(pid)
                pname = _safe_name(p)
                p.kill()
                log.debug("[%s]   killed PID %d (%s)", self.name, pid, pname)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                log.debug("[%s]   PID %d already gone or no access: %s",
                          self.name, pid, e)
