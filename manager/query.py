"""Steam server query (A2S protocol) wrapper.

Used to determine 'is the server actually accepting connections?' which is
more meaningful than 'is the process alive?'. A running process can still be
loading the world for 60-180 sec before it answers queries.
"""

import logging
from typing import Optional

import a2s

log = logging.getLogger(__name__)


def query_server(host: str, query_port: int, timeout: float = 2.0) -> Optional[dict]:
    """Query a Source-protocol server. Returns a small dict on success or
    None on any failure (no answer / connection refused / parse error).
    """
    try:
        info = a2s.info((host, query_port), timeout=timeout)
    except OSError as e:
        # Most common: ConnectionRefusedError (server not up yet) or timeout.
        log.debug("a2s.info(%s:%d) socket: %s", host, query_port, e)
        return None
    except Exception as e:
        # python-a2s can raise its own BrokenMessageError; catch broadly so
        # one weird packet doesn't crash a status poll.
        log.debug("a2s.info(%s:%d) %s: %s", host, query_port, type(e).__name__, e)
        return None

    return {
        "name": info.server_name,
        "map": info.map_name,
        "players": info.player_count,
        "max_players": info.max_players,
        "ping_ms": int((info.ping or 0) * 1000),
    }
