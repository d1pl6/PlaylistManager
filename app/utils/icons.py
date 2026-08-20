"""Scaled icon loading.

``tk.PhotoImage`` is pixel-fixed - it never scales with display DPI, while
fonts do (see utils/scaling.py), so at 175% a 16px close button sits next
to 1.75x text.  :class:`IconService` resizes the base PNG to
``round(base_px * ui_scale)`` with LANCZOS and caches the result per
(path, size).

**Main thread only** - this creates ``ImageTk.PhotoImage`` objects.
"""

import logging
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image, ImageTk

from utils.scaling import get_ui_scale

logger = logging.getLogger(__name__)

# PIL >= 9.1 moved the constant to Image.Resampling; keep the old location
# working for older pillow builds.
_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS


class IconService:
    """Loads and caches scaled ``tk.PhotoImage`` icons.

    The cache holds strong references - ``ImageTk.PhotoImage`` is otherwise
    garbage-collected and the icon silently vanishes.
    """

    _cache: Dict[Tuple[str, int], ImageTk.PhotoImage] = {}

    @classmethod
    def get(cls, path, base_px: int) -> ImageTk.PhotoImage:
        """Return the icon at ``base_px`` scaled by the current UI scale."""
        size = max(1, round(base_px * get_ui_scale()))
        key = (str(path), size)
        img = cls._cache.get(key)
        if img is None:
            with Image.open(path) as pil:
                if pil.size != (size, size):
                    pil = pil.resize((size, size), _RESAMPLE)
                img = ImageTk.PhotoImage(pil)
            cls._cache[key] = img
        return img
