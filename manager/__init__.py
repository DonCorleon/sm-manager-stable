"""Soulmask Manager — local web admin for Soulmask 1.0 dedicated servers."""

# Register custom VERBOSE log level + Logger.verbose method as soon as
# any submodule of the package is imported. Submodules use log.verbose()
# without explicitly importing logging_setup; this ensures the patch is
# always in place before any verbose call fires (production launch via
# `python -m manager` AND standalone imports for tests / utilities).
import logging as _logging

_VERBOSE_LEVEL = 5
if _logging.getLevelName(_VERBOSE_LEVEL) != "VERBOSE":
    _logging.addLevelName(_VERBOSE_LEVEL, "VERBOSE")


def _verbose(self, message, *args, **kwargs):
    if self.isEnabledFor(_VERBOSE_LEVEL):
        self._log(_VERBOSE_LEVEL, message, args, **kwargs)


if not hasattr(_logging.Logger, "verbose"):
    _logging.Logger.verbose = _verbose  # type: ignore[attr-defined]


__version__ = "0.1.3"
