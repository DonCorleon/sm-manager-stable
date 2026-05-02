"""EchoPort client.

EchoPort is Soulmask's built-in telnet-style admin console (port 18888 by
default). No authentication, raw line-based protocol -- bind to loopback
only. Used by the manager for graceful shutdown ('SaveAndExit'), broadcasts
('SayToSystemChannel'), player listing ('lp'), and chat-log toggle
('Set_OutputChats').

This is a one-shot client: open socket -> send command -> read response ->
close. It's not a long-running session. Higher levels can call it many
times.
"""

import logging
import re
import socket
from typing import Optional

log = logging.getLogger(__name__)


def send_command(host: str, port: int, command: str,
                 connect_timeout: float = 3.0,
                 read_timeout: float = 2.0) -> str:
    """Send a single command and return the server's response text.

    Raises ConnectionRefusedError / OSError if the EchoPort isn't listening
    (server starting or stopped). Caller should handle.
    """
    log.debug("echo connect: %s:%d", host, port)
    with socket.create_connection((host, port), timeout=connect_timeout) as sock:
        # Drain greeting / banner. EchoPort sends a short prompt on connect.
        sock.settimeout(read_timeout)
        banner = _drain(sock, max_bytes=4096)
        if banner:
            log.debug("  banner: %r", banner[:200])

        # Send the command. EchoPort is telnet-derived; some Soulmask builds
        # need \r\n (telnet line termination) and reject bare \n. Sending
        # both is harmless either way -- the parser sees one line.
        log.debug("  send: %r", command)
        sock.sendall((command + "\r\n").encode("utf-8", errors="replace"))

        # Read whatever the server returns (until the read timeout fires).
        response = _drain(sock, max_bytes=65536)
        log.debug("  recv: %r", response[:500])
        return response


def _drain(sock: socket.socket, max_bytes: int) -> str:
    """Read available data until the socket goes idle (read timeout fires)."""
    chunks: list[bytes] = []
    total = 0
    try:
        while total < max_bytes:
            data = sock.recv(min(4096, max_bytes - total))
            if not data:
                break
            chunks.append(data)
            total += len(data)
    except socket.timeout:
        pass  # done reading -- normal end-of-response
    return b"".join(chunks).decode("utf-8", errors="replace")


# ── List_OnlinePlayers (lp) parsing ────────────────────────────────────────
#
# `lp` returns a pipe-delimited table:
#
#   |              Account |       PlayerName |  PawnID    |        Position |
#   |    76561198000000000 |   'ExamplePlayer'| 4VJFE1XMZ...| V(X=..., Y=..., Z=...) |
#
# Lightweight (~250 bytes per online player). Safe to poll every 30-60 sec
# while populated. Position is in UE world coords -- pair with the verified
# UE -> tile-pixel transform in manager/map_render.py to render.

# V(X=121851.47, Y=60436.82, Z=26164.77)
_LP_POSITION_RE = re.compile(
    r"V\(X=(?P<x>-?\d+(?:\.\d+)?),\s*"
    r"Y=(?P<y>-?\d+(?:\.\d+)?),\s*"
    r"Z=(?P<z>-?\d+(?:\.\d+)?)\)"
)


def parse_lp(response: str) -> list[dict]:
    """Parse a `lp` (List_OnlinePlayers) response into a list of
    {steam_id, name, pawn_id, x, y, z} dicts. Empty list if no
    players online or the response is unparseable.

    Handles both the header row (skipped) and the data rows. Tolerant
    of extra whitespace, trailing newlines, and the response being
    truncated (some Soulmask builds cap response size; partial rows
    are skipped silently)."""
    out: list[dict] = []
    for line in response.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        # Split into cells on '|', trim, drop empty leading/trailing
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]
        if len(cells) < 4:
            continue
        # Skip the header row (first cell is literally "Account")
        if cells[0].lower().startswith("account"):
            continue

        steam_id = cells[0]
        # Steam IDs are all digits; if the cell isn't, this isn't a data row
        if not steam_id.isdigit():
            continue
        # Name is wrapped in single quotes -- strip them
        name = cells[1].strip("'\"")
        pawn_id = cells[2]
        # Position cell: V(X=..., Y=..., Z=...)
        m = _LP_POSITION_RE.search(cells[3])
        if not m:
            continue
        out.append({
            "steam_id": steam_id,
            "name": name,
            "pawn_id": pawn_id,
            "x": float(m.group("x")),
            "y": float(m.group("y")),
            "z": float(m.group("z")),
        })
    return out


def list_online_players(host: str, port: int, *,
                         connect_timeout: float = 3.0,
                         read_timeout: float = 2.0
                         ) -> Optional[list[dict]]:
    """Convenience: send `lp` and return parsed player list. Returns
    None on connection failure (server not running / EchoPort closed),
    [] when no players are online but the server responded."""
    try:
        response = send_command(host, port, "lp",
                                 connect_timeout=connect_timeout,
                                 read_timeout=read_timeout)
    except (ConnectionRefusedError, OSError) as e:
        log.debug("list_online_players: EchoPort %s:%d unreachable: %s",
                  host, port, e)
        return None
    return parse_lp(response)
