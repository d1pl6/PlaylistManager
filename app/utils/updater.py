import logging
import re
import threading
from configparser import ConfigParser
from pathlib import Path

import requests

from _version import __version__

logger = logging.getLogger(__name__)

GITHUB_OWNER = "d1pl6"
GITHUB_REPO = "PlaylistManager"
API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


def check(callback=None):
    """Check for updates on GitHub in a background thread.

    Args:
        callback: Optional callable(available: bool, latest_version: str | None, download_url: str | None, body: str | None, error: str | None)
    """

    def _check():
        try:
            settings_path = Path(__file__).resolve().parents[2] / "cfg" / "settings.ini"
            cfg = ConfigParser()
            cfg.read(str(settings_path))

            enabled = cfg.getboolean("update_check", "is_true", fallback=True)
            if not enabled:
                logger.debug("Update check is disabled in settings")
                if callback:
                    callback(False, None, None, None, None)
                return

            resp = requests.get(API_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            latest_tag = data.get("tag_name", "").lstrip("v")
            download_url = data.get("html_url", "")
            body = data.get("body", "")

            def parse_version(v: str) -> tuple:
                """Return a comparable tuple from a version string.

                Normalises to
                (major, minor, patch, extra, is_pre_release, suffix_count)
                so "1.0.1" > "1.0", "1.0.1.2" > "1.0.1",
                "1.0.1-beta1" > "1.0.1-beta0", and every pre-release sorts
                below its stable release ("1.0.1-beta1" < "1.0.1").
                """
                parts = re.split(r"[._\-]", v.strip().lstrip("vV"))
                nums = []
                pre_release = False
                suffix = 0
                for p in parts:
                    if p.isdigit():
                        nums.append(int(p))
                    else:
                        pre_release = True  # suffix: -beta, .rc1, etc.
                        suffix_match = re.search(r"(\d+)$", p)
                        if suffix_match:
                            suffix = int(suffix_match.group(1))
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

            available = parse_version(latest_tag) > parse_version(__version__)

            if available:
                logger.info(
                    "Update available: v%s (current: v%s)", latest_tag, __version__
                )
            else:
                logger.debug("App is up to date (v%s)", __version__)

            if callback:
                callback(
                    available, latest_tag or None, download_url or None, body or None, None
                )

        except requests.RequestException as e:
            logger.warning("Update check failed: %s", e)
            if callback:
                callback(False, None, None, None, f"Could not reach update server.\n{e}")
        except Exception as e:
            logger.warning("Update check failed: %s", e)
            if callback:
                callback(False, None, None, None, f"Update check failed.\n{e}")

    thread = threading.Thread(target=_check, daemon=True)
    thread.start()
