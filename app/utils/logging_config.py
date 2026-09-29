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

Credential redaction
--------------------

Every record that reaches the root handler passes through
:class:`_RedactionFilter`, attached by :func:`configure_logging`, which
masks:

* labelled secret values - the ``Authorization`` / ``Cookie`` /
  ``Proxy-Authorization`` / ``X-PM-Token`` headers, and the credential
  keys ``client_secret``, ``refresh_token``, ``access_token``,
  ``api_key``, ``api_secret``, ``session_key``, ``auth_token``,
  ``arl``, ``sid`` and ``jwt`` - in every form they appear in logs
  (header lines, JSON dicts, ini lines, URL query pairs);
* ``Bearer <token>`` occurrences;
* every value registered via :func:`register_secret` - the app's auth
  service and the platform plugins register each credential they load or
  save, so even a BARE secret (an exception message, a raw server echo)
  is replaced instead of printed.

The filter runs on the already-formatted message and rewrites
``record.msg``, so no later handler/format stage can reintroduce a
secret.  It also covers third-party debug output: ytmusicapi / requests /
urllib3 dump request headers (``Authorization`` / ``Cookie``) at
``--trace``.
"""

import logging
import re

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


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------

# Placeholder every masked secret is replaced with.
MASK_STRING = "<redacted>"

# Values registered via register_secret() are only masked when they look
# like real credentials: real credentials (refresh tokens, JWTs, cookies,
# ARLs, session keys, client secrets) are all longer than 8 characters.
# Masking short values would garble ordinary log text on every replace.
_MIN_SECRET_LENGTH = 8
_MAX_SECRET_LENGTH = 4096

# Secrets registered at runtime (see register_secret).  Defensive layer:
# the regexes below only catch *labelled* values; a registered secret is
# masked wherever its exact value appears, label or not.
_REGISTERED_SECRETS: set[str] = set()


def register_secret(*values: object) -> None:
    """Add credential values that :func:`redact` must always mask.

    Called with every credential the app or a platform plugin loads or
    saves (ARL / JWT, refresh tokens, API secrets, cookies, authorization
    headers).  :func:`redact` then replaces any occurrence of the exact
    value in log output - even when the value appears bare, e.g. inside
    an exception message or a raw server echo.

    Values shorter than 8 characters (and non-strings) are ignored: they
    are not real credentials, and replacing them would garble ordinary
    log text.
    """
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if _MIN_SECRET_LENGTH <= len(value) <= _MAX_SECRET_LENGTH:
            _REGISTERED_SECRETS.add(value)


# Header-style values: the whole value after the label is masked.  Runs
# on header lines ("Authorization: Bearer ..."), JSON dicts
# ('"Cookie": "..."'), and query pairs ("?cookie=...").
_HEADER_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?:^|[\s,{;\"'=&?-])"
    r"(authorization|proxy-authorization|cookie|set-cookie|x-pm-token)\b"
    r"[\"']?\s*[:=]\s*"
    r"['\"]?(?P<value>[^'\",}\r\n]+)"
)

# Credential keys in JSON / ini / URL-query form; only the value is
# masked, so "client_id=abc&limit=5" keeps the "&limit=5" tail.
_TOKEN_VALUE_RE = re.compile(
    r"(?ix)"
    r"(?:^|[\s,{;\"'=&?-])"
    r"(client[-_]id|client[-_]secret|refresh[-_]token|access[-_]token"
    r"|api[-_]key|api[-_]secret|session[-_]key|auth[-_]token"
    r"|arl|sid|jwt)\b"
    r"[\"']?\s*[:=]\s*"
    r"['\"]?(?P<value>[^'\",\s&}{;]+)"
)

# Bare "Bearer <token>" occurrences that carry no Authorization label
# (e.g. "invalid bearer token abc.def" inside an error message).
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")


def _mask_value(match: re.Match) -> str:
    """Replace only the ``value`` group of a match with :data:`MASK_STRING`.

    The match always ends at the value group, so splicing from the value
    start is exact - and unlike a ``replace(value, ...)`` it can never
    hit a label fragment that happens to equal the value (e.g.
    ``client_secret=secret``).
    """
    value_start = match.start("value") - match.start()
    return match.group(0)[:value_start] + MASK_STRING


def _mask_bearer(match: re.Match) -> str:
    return "Bearer " + MASK_STRING


def redact(text: str) -> str:
    """Return *text* with every known credential masked.

    Three layers, applied in order:

    1. ``Bearer <token>`` occurrences.
    2. Labelled secret values (see the module docstring for the key
       list): header lines, JSON dicts, ini lines, URL query pairs.
    3. Every value previously handed to :func:`register_secret` - this
       catches secrets appearing WITHOUT any label.

    An already-masked value is never re-scanned by a later layer.
    """
    if not text:
        return text
    masked = _BEARER_RE.sub(_mask_bearer, text)
    masked = _HEADER_VALUE_RE.sub(_mask_value, masked)
    masked = _TOKEN_VALUE_RE.sub(_mask_value, masked)
    for secret in _REGISTERED_SECRETS:
        if secret in masked:
            masked = masked.replace(secret, MASK_STRING)
    return masked


class _RedactionFilter(logging.Filter):
    """Handler-level filter that masks credentials in every record.

    ``filter()`` formats the record's message, redacts it, and rewrites
    ``record.msg`` (clearing ``record.args``), so every later formatting
    stage - including formatting code we do not control - can only ever
    see the masked text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # A record can pass through the filter once per handler; only the
        # first pass needs to do (and re-base) the rewrite.
        if getattr(record, "_pm_redacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:
            # getMessage() failed (mismatched %-args, ...).  Leave the
            # record alone: the normal formatting path reports the
            # original failure, and there is no formatted text to redact.
            return True
        masked = redact(message)
        if masked != message:
            record.msg = masked
            record.args = ()
            record._pm_redacted = True
        return True


def _attach_redaction_filter() -> None:
    """Attach the credential-masking filter to every root-logger handler.

    ``basicConfig(force=True)`` rebuilds the handler set on every call,
    so the filter is (re)attached after configuration.  The filter goes
    on the HANDLERS, not the root logger: record propagation only walks
    parent handlers, never parent filters.
    """
    filt = _RedactionFilter()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if not any(isinstance(f, _RedactionFilter) for f in handler.filters):
            handler.addFilter(filt)


def user_log(logger: logging.Logger, msg: str, *args, **kwargs) -> None:
    """Emit a user-facing status line (shown in normal runs).

    Use for auth results, credential locations and similar lines the user
    should see even without --debug.  Everything else belongs at INFO or
    below so it stays hidden in normal runs.

    User-facing messages come through ``tr()`` / ``trn()`` (utils/i18n):
    pass the already-formatted string as ``msg`` with no ``args``
    (``user_log(logger, tr("app.tray_unavailable"))``).  Prefer that over
    ``%``-style arguments - translations keep working and the waste case
    (formatting a disabled message) is bounded.
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

    Credentials are masked by :func:`redact` on every record regardless
    of verbosity - ``--trace`` never reveals Authorization / Cookie /
    token values (see the module docstring).
    """
    levels = (USER_LEVEL, logging.INFO, logging.DEBUG, TRACE_LEVEL)
    level = levels[max(0, min(verbosity, 3))]

    logging.basicConfig(level=level, format=_LOG_FORMAT, force=True)

    if verbosity >= 3:
        for name in _DEBUG_LIBRARIES:
            logging.getLogger(name).setLevel(logging.DEBUG)

    _attach_redaction_filter()