"""Unified UI scale factor (HiDPI / display scaling).

Fonts scale automatically via fontconfig/``Xft.dpi`` — **not** via
``tk scaling``.  Verified 2026-08-14 on this machine: at 175% display scale
``tk scaling`` stays 1.333 (the physical screen DPI) while fonts still render
at 1.75x, and forcing ``tk scaling`` to 2.333 changes no font metric.  This
module computes the one scale factor the rest of the UI uses:

  - icons are PIL-resized to ``round(base_px * scale)`` (utils/icons.py),
  - fixed pixel geometry (window size, card size, minsize, wraplength) is
    multiplied via :func:`px`,
  - under a manual profile, fonts are adjusted via :func:`ui_font` with
    ``font_mult = profile / detected`` so text, icons and geometry all land
    on the chosen scale.

:func:`init` must be called right after ``tk.Tk()`` and before any widget is
created.  Headless (CLI) paths never call it and get scale 1.0.
"""

import logging
import os
import subprocess
from typing import Optional, Tuple

from utils.config import get_setting_value

logger = logging.getLogger(__name__)

BASE_DPI = 96.0
MIN_SCALE = 1.0
MAX_SCALE = 4.0

#: Options for the Settings dialog "UI scale" row.  ``auto`` follows the
#: display's Xft.dpi; a numeric value overrides it (fonts included, via
#: :func:`ui_font`).
UI_SCALE_PRESETS = ("auto", "1.0", "1.25", "1.5", "1.75", "2.0", "2.5", "3.0")

_scale: float = 1.0
_font_mult: float = 1.0


def _clamp(v: float) -> float:
    return min(MAX_SCALE, max(MIN_SCALE, v))


def _parse_dpi(raw: str) -> Optional[float]:
    """Parse an ``Xft.dpi`` value ('168', '150', ...) or return None."""
    try:
        v = float(raw.strip())
    except ValueError:
        return None
    if not 0 < v <= 500:
        return None
    return v


def _xrdb_xft_dpi() -> Optional[float]:
    """Read ``Xft.dpi`` from the X resource database via ``xrdb -query``.

    KDE/GNOME set this from the display scale (e.g. 150 at 156.25%, 168 at
    175%) and remove it entirely at 100%.  One subprocess at startup is
    cheap; failures degrade to the fallbacks in :func:`xft_dpi`.
    """
    try:
        proc = subprocess.run(
            ["xrdb", "-query"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines():
        if line.strip().startswith("Xft.dpi:"):
            return _parse_dpi(line.split(":", 1)[1])
    return None


def xft_dpi(root=None) -> Optional[float]:
    """Effective display DPI used for font rendering.

    Priority: the ``Xft.dpi`` X resource, then ``winfo_fpixels('1i')``
    (needs *root*), then the ``XFT_DPI`` env var.  ``None`` only when there
    is no display connection at all (pure Wayland without XWayland) — which
    is when the manual profile matters most.
    """
    dpi = _xrdb_xft_dpi()
    if dpi is not None:
        return dpi
    if root is not None:
        try:
            return float(root.winfo_fpixels("1i"))
        except Exception:
            pass
    env = os.environ.get("XFT_DPI")
    if env:
        dpi = _parse_dpi(env)
        if dpi is not None:
            return dpi
    return None


def init(root=None) -> None:
    """Compute and cache the UI scale and font multiplier.

    Call once, right after ``tk.Tk()`` and before any widget exists (the
    CLI never calls it — scale stays 1.0).  Calling again later (live
    re-apply) recomputes from the current settings.
    """
    global _scale, _font_mult

    profile = get_setting_value("ui_scale", "value", "auto").strip().lower()
    detected = xft_dpi(root)
    auto_scale = detected / BASE_DPI if detected else 1.0

    if profile == "auto":
        _scale = _clamp(auto_scale)
        _font_mult = 1.0
    else:
        try:
            _scale = _clamp(float(profile))
        except ValueError:
            logger.warning("Invalid ui_scale value %r - using auto", profile)
            _scale = _clamp(auto_scale)
            _font_mult = 1.0
        else:
            _font_mult = _scale / auto_scale if auto_scale else 1.0
    logger.debug("ui_scale=%s font_mult=%s detected_dpi=%s", _scale, _font_mult, detected)


def get_ui_scale() -> float:
    """The effective scale factor (1.0 before :func:`init` / headless)."""
    return _scale


def font_mult() -> float:
    """Font point-size multiplier; 1.0 in auto mode (fonts already scaled)."""
    return _font_mult


def px(n: float) -> int:
    """Scale a base-pixel size by the UI scale factor."""
    return max(1, round(n * _scale))


def ui_font(size: int, weight: str = "", family: str = "") -> Tuple[str, ...]:
    """Tk ``font=`` spec for one point size, applying the profile multiplier.

    ``family`` defaults to ``""``, which makes Tk use its default font
    family.  (A hardcoded family like "Noto" is fragile: it is not a real
    family name, and Tk's fuzzy fallback picks whichever installed font
    matches best - or the Tk default when nothing does.)

    In auto mode ``font_mult`` is 1.0 and this returns the plain
    ``("", size)`` — fonts are already rendered at the display scale by
    fontconfig, so no manual adjustment (double-scaling trap).
    """
    scaled = size if _font_mult == 1.0 else round(size * _font_mult)
    if weight:
        return (family, scaled, weight)
    return (family, scaled)
