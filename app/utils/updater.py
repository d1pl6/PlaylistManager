import logging
import re
import threading
import time

import requests

from _version import __version__
from utils.config import (
    UPDATE_CHECK_DEFAULT_INTERVAL,
    UPDATE_CHECK_INTERVALS,
    UPDATE_CHECK_INTERVAL_SECONDS,
    get_setting_value,
    set_setting_value,
)

logger = logging.getLogger(__name__)

GITHUB_OWNER = "d1pl6"
GITHUB_REPO = "PlaylistManager"
API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


def read_interval() -> str:
    """The normalized [update_check] interval value.

    One of ``UPDATE_CHECK_INTERVALS`` ("off" / "launch" / a timed value);
    anything else in the INI falls back to the default ("launch").
    """
    value = get_setting_value(
        "update_check", "interval", UPDATE_CHECK_DEFAULT_INTERVAL
    )
    if value not in UPDATE_CHECK_INTERVALS:
        return UPDATE_CHECK_DEFAULT_INTERVAL
    return value


def interval_due(interval: str, last_check: float, now: float) -> bool:
    """Whether a check is due given *last_check* and *now* (epoch seconds).

    ``"off"`` never checks; ``"launch"`` always checks; a timed interval
    checks once ``interval`` seconds have elapsed since the last check.  A
    missing/zero stamp counts as due, so a fresh install checks on first
    launch.
    """
    if interval == "off":
        return False
    if interval == "launch":
        return True
    seconds = UPDATE_CHECK_INTERVAL_SECONDS.get(interval)
    if not seconds:
        return True
    return now - last_check >= seconds


def _last_check() -> float:
    """Epoch-seconds stamp of the last completed check attempt (0 = none)."""
    raw = get_setting_value("update_check", "last_check", "0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _stamp_last_check() -> None:
    """Persist the check time so the next launch can space itself out.

    Best-effort: a settings-write failure must not fail the check itself.
    """
    try:
        set_setting_value("update_check", "last_check", str(int(time.time())))
    except Exception:
        logger.debug(
            "Failed to persist update-check timestamp", exc_info=True
        )


def parse_version(v: str) -> tuple:
    """Return a comparable tuple from a version string.

    Normalises to
    (major, minor, patch, extra, is_pre_release, suffix_count)
    so "1.0.1" > "1.0", "1.0.1.2" > "1.0.1",
    "1.0.1-beta1" > "1.0.1-beta0", "1.0.1-beta.2" == "1.0.1-beta2",
    and every pre-release sorts below its stable release
    ("1.0.1-beta1" < "1.0.1").
    """
    parts = re.split(r"[._\-]", v.strip().lstrip("vV"))
    nums = []
    pre_release = False
    suffix = 0
    for i, p in enumerate(parts):
        if p.isdigit():
            nums.append(int(p))
            continue
        # First non-numeric part marks the pre-release suffix
        # (-beta, .rc1, ...).  Its number may be attached ("-beta2") or a
        # separate dotted part ("-beta.2") - take the first one found so
        # both spellings compare equal.
        pre_release = True
        suffix_match = re.search(r"(\d+)$", p)
        if suffix_match:
            suffix = int(suffix_match.group(1))
        else:
            for q in parts[i + 1:]:
                if q.isdigit():
                    suffix = int(q)
                    break
        break
    # Keep every numeric component; pad short versions to 4 so
    # "1.0.1.2" > "1.0.1" instead of truncating to equality.
    nums = (nums + [0, 0, 0, 0])[:4]
    return (
        nums[0],
        nums[1],
        nums[2],
        nums[3],
        -1 if pre_release else 0,
        suffix,
    )


def check(callback=None, *, force: bool = False):
    """Check for updates on GitHub in a background thread.

    Args:
        callback: Optional callable(available, latest_version, download_url, body, error)
        force: If True, bypasses the [update_check] interval setting.
    """

    def _check():
        try:
            if not force:
                interval = read_interval()
                last_check = _last_check()
                if not interval_due(interval, last_check, time.time()):
                    logger.debug(
                        "Update check skipped: interval %r, last check %s",
                        interval,
                        last_check or "never",
                    )
                    if callback:
                        callback(False, None, None, None, None)
                    return
            # Stamp before the network attempt: a failed check (offline,
            # rate limit) still spaces the next one, so a flaky network
            # cannot turn a timed interval into a per-launch hammer.
            _stamp_last_check()

            resp = requests.get(API_URL, timeout=10)

            # GitHub answers 403 when the anonymous API rate limit is
            # exhausted - that is NOT a network failure and the UI should
            # not claim the server is unreachable.
            if resp.status_code == 403:
                err = (
                    "GitHub API rate limit reached - update check "
                    "skipped. Try again later."
                )
                logger.warning("Update check skipped: %s", err)
                if callback:
                    callback(False, None, None, None, err)
                return

            resp.raise_for_status()
            data = resp.json()

            latest_tag = data.get("tag_name", "").lstrip("v")
            download_url = data.get("html_url", "")
            body = data.get("body", "")

            available = parse_version(latest_tag) > parse_version(__version__)

            if available:
                logger.info(
                    "Update v%s available at %s",
                    latest_tag,
                    download_url,
                )
            else:
                logger.debug("App is up to date (v%s)", __version__)

            if callback:
                callback(
                    available,
                    latest_tag or None,
                    download_url or None,
                    body or None,
                    None,
                )

        except requests.RequestException as e:
            logger.warning("Update check failed: %s", e)
            if callback:
                callback(False, None, None, None, f"Could not reach update server.\n{e}")
        except Exception as e:
            logger.warning("Update check failed: %s", e)
            if callback:
                callback(False, None, None, None, f"Update check failed.\n{e}")

    threading.Thread(target=_check, daemon=True).start()