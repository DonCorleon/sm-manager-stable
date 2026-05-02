"""Helpers for in-game broadcasts via EchoPort `say`.

`say <msg>` is the SayToSystemChannel command; the message appears in
the system / world channel for every connected player. Empty servers
still log it (audit trail in WS.log) thanks to OutputChats=1.

Manager-originated broadcasts are prefixed `[Manager]` so they're
visually distinct from server / player chat.
"""

import logging
from typing import Optional

from manager import echo
from manager.wizard import RuntimeInstance

log = logging.getLogger(__name__)

PREFIX = "[Manager]"


def say_to(ri: RuntimeInstance, message: str) -> bool:
    """Fire one `say` to one instance. Returns True on EchoPort
    round-trip success, False otherwise. Failure is logged at WARNING
    only -- broadcasts are advisory."""
    cmd = f"say {PREFIX} {message}"
    log.verbose("[%s] broadcast dispatch: 127.0.0.1:%d cmd=%r",
                ri.instance.name, ri.instance.echo_port, cmd)
    try:
        echo.send_command("127.0.0.1", ri.instance.echo_port, cmd)
        log.info("[%s] said: %s", ri.instance.name, message)
        return True
    except (OSError, ConnectionError) as e:
        log.warning("[%s] could not say %r: %s", ri.instance.name, message, e)
        return False
    except Exception as e:
        log.error("[%s] unexpected error during say %r: %s",
                  ri.instance.name, message, e)
        return False


def say_to_all_running(message: str) -> int:
    """Fire to every running instance. Returns count of successful sends."""
    from manager import lifecycle
    n = 0
    for ri, _ in lifecycle.running_instances():
        if say_to(ri, message):
            n += 1
    return n


def warn_pre_save(secs_until: int) -> int:
    """Standard pre-snapshot broadcast. Returns count of successful sends."""
    if secs_until >= 60:
        msg = f"Auto-save in {secs_until // 60} minute(s) -- brief lag possible."
    else:
        msg = f"Auto-save in {secs_until} second(s)."
    return say_to_all_running(msg)


def warn_pre_shutdown(ri: RuntimeInstance, countdown_sec: int,
                      reason: Optional[str] = None) -> bool:
    """Single broadcast at the start of a shutdown countdown.
    Soulmask itself emits its own countdown messages once `SaveAndExit X`
    has been received; this prefix line makes the cause explicit and
    flags the login lock. No follow-up T-30sec warning -- the game
    handles ongoing countdown messaging."""
    if countdown_sec >= 60:
        time_str = f"{countdown_sec // 60} minute(s)"
    else:
        time_str = f"{countdown_sec} second(s)"
    suffix = f" ({reason})" if reason else ""
    msg = (f"Server shutting down in {time_str} for save+exit{suffix}. "
           f"New logins blocked.")
    return say_to(ri, msg)
