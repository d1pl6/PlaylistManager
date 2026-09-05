"""
Central logging configuration.

A normal run (no flags) is quiet: only messages that actually matter to
the user are shown - errors, warnings, and ``USER``-level status lines
(auth results, credential locations, ...).  All the internal INFO/DEBUG
noise is hidden unless verbosity is requested:

  --verbose / -v   INFO and above
  --debug / -vv    DEBUG and above (everything)
  --trace / -vvv   TRACE and above + third-party debug loggers
                   (requests / urllib3 / ytmusicapi / werkzeug / ...)

Custom levels (so they survive the module-level ``basicConfig`` levels):

  TRACE  = 5   ultra-verbose internals (payload dumps) - only with --trace
  NETWORK= 15  HTTP / network round trips (access logs, fetch errors) -
               hidden at --verbose, shown from --debug up
  USER   = 25  user-facing status - visible in normal runs
"""

import logging

TRACE_LEVEL = 5
NETWORK_LEVEL = 15
USER_LEVEL = 25

logging.addLevelName(TRACE_LEVEL, "TRACE")
logging.addLevelName(NETWORK_LEVEL, "NETWORK")
logging.addLevelName(USER_LEVEL, "USER")

_LOG_FORMAT = "%(asctime)s %(levelname)s: %(message)s"

# Third-party loggers whose debug output is only useful when tracing deep
# into HTTP / auth behavior (--trace).
_DEBUG_LIBRARIES = ("requests", "urllib3", "ytmusicapi", "werkzeug", "flask", "PIL")


def user_log(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """Emit a user-facing status line (shown in normal runs).

    Use for auth results, credential locations and similar lines the user
    should see even without --debug.  Everything else belongs at INFO or
    below so it stays hidden in normal runs.

    Prefer ``%``-style arguments (``user_log(logger, "%s ready", name)``)
    over pre-built f-strings: the formatting is skipped entirely when the
    level is disabled, while an f-string is built at the call site no
    matter what.
    """
    logger.log(USER_LEVEL, msg, *args, **kwargs)


def trace_log(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """Emit an ultra-verbose line (only shown with --trace).

    Use for payload dumps and other output too noisy even for DEBUG.
    Must be called with ``%``-style arguments (``trace_log(logger,
    "Song data: %s", song_data)``) - f-strings are evaluated at the call
    site even when TRACE is disabled, defeating the point of the level.
    """
    logger.log(TRACE_LEVEL, msg, *args, **kwargs)


def network_log(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """Emit a network layer line (hidden at --verbose, shown from --debug up).

    Use for HTTP / network round trips and their failures: server access
    logs, thumbnail/endpoint fetch errors, and similar low-noise-per-value
    diagnostics.  These are too chatty for an INFO ``--verbose`` run but
    useful when debugging connectivity, so they sit on their own level
    between DEBUG and INFO.

    Prefer ``%``-style arguments (``network_log(logger, "%s -> %s",
    verb, url)``) over f-strings: formatting is skipped entirely when the
    level is disabled.
    """
    logger.log(NETWORK_LEVEL, msg, *args, **kwargs)


def configure_logging(verbosity: int = 0) -> None:
    """Configure root logging for the whole app.

    Args:
        verbosity:
            0  default - USER and above (errors + user-facing status)
            1  (-v)    - INFO and above (NETWORK+TRACE hidden)
            2  (--debug / -vv) - DEBUG and above (NETWORK visible)
            3  (--trace / -vvv) - TRACE and above + third-party debug
    """
    levels = (USER_LEVEL, logging.INFO, logging.DEBUG, TRACE_LEVEL)
    level = levels[max(0, min(verbosity, 3))]

    logging.basicConfig(level=level, format=_LOG_FORMAT, force=True)

    if verbosity >= 3:
        for name in _DEBUG_LIBRARIES:
            logging.getLogger(name).setLevel(logging.DEBUG)
