"""Unit tests for window geometry persistence (utils/window.py).

Pure-helper tests over a fake Tk window - the suite stays display-free.
The fake stands in for the real window surface the helpers use: resolved
winfo_* numbers, state(), attributes("-fullscreen"), geometry().
"""

import types

from utils.window import (
    _clamp_rect,
    _format_pos,
    _parse_geometry,
    _should_save_geometry,
    _visible,
    restore_window_geometry,
    save_window_geometry,
)


class FakeWindow(types.SimpleNamespace):
    """Replicates the Tk window surface the geometry helpers read."""

    def __init__(self, *, w, h, x, y, state="normal", mapped=True,
                 fullscreen=False, screen=(1920, 1080)):
        super().__init__()
        self.w, self.h, self.x, self.y = w, h, x, y
        self.state_val, self.mapped, self.fullscreen = state, mapped, fullscreen
        self.sw, self.sh = screen
        self.calls = []  # geometry(...) calls recorded

    def winfo_width(self):
        return self.w

    def winfo_height(self):
        return self.h

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def winfo_screenwidth(self):
        return self.sw

    def winfo_screenheight(self):
        return self.sh

    def state(self):
        return self.state_val

    def winfo_ismapped(self):
        return self.mapped

    def attributes(self, name, *rest):
        if name == "-fullscreen":
            if rest:
                self.fullscreen = bool(rest[0])
            return self.fullscreen
        return None

    def geometry(self, *args):
        self.calls.append(args)
        if args:
            return args[0]
        return f"{self.w}x{self.h}{self.x:+d}{self.y:+d}"


class TestParseGeometry:
    def test_plain_positive(self):
        assert _parse_geometry("1100x700+120+45") == (1100, 700, 120, 45)

    def test_absolute_negative_position(self):
        assert _parse_geometry("800x600+-340+120") == (800, 600, -340, 120)
        assert _parse_geometry("800x600+-340+-120") == (800, 600, -340, -120)

    def test_tolerates_spacing(self):
        assert _parse_geometry(" 800 x600+10+10") == (800, 600, 10, 10)

    def test_size_only_rejected(self):
        # We always save a position; "WxH" alone is not one of ours.
        assert _parse_geometry("800x600") is None

    def test_garbage(self):
        for bad in ("", None, "abc", "x600+10+10", "800x+10+10",
                    "800x600+10", "800x600+10+x"):
            assert _parse_geometry(bad) is None

    def test_zero_or_negative_sizes_rejected(self):
        assert _parse_geometry("0x600+10+10") is None
        assert _parse_geometry("-100x600+10+10") is None

    def test_overflow_string_rejected(self):
        # int() conversion limit: >4300 digits raises ValueError
        # (Python 3.11+) - the huge string must not slice-crash the parse.
        assert _parse_geometry("9" * 5000 + "x600+10+10") is None

    def test_huge_size_flows_to_clamp(self):
        rect = _parse_geometry("999999999999x600+10+10")
        assert rect is not None
        assert _clamp_rect(rect, 1920, 1080) == (1860, 600, 10, 10)


class TestClampRect:
    def test_roomy_unchanged(self):
        assert _clamp_rect((1100, 700, 120, 45), 1920, 1080) == (1100, 700, 120, 45)

    def test_oversized_capped_to_screen_minus_margin(self):
        assert _clamp_rect((3000, 3000, 100, 100), 1920, 1080) == (1860, 1020, 100, 100)

    def test_tiny_raised_to_floor(self):
        assert _clamp_rect((10, 10, 0, 0), 1920, 1080) == (100, 100, 0, 0)

    def test_position_untouched(self):
        assert _clamp_rect((800, 600, -340, 500), 1920, 1080) == (800, 600, -340, 500)


class TestVisible:
    def test_fully_on_screen(self):
        assert _visible((800, 600, 100, 100), 1920, 1080)

    def test_left_monitor_negative(self):
        assert _visible((800, 600, -700, 100), 1920, 1080)  # -700 + 800 > 0

    def test_above_monitor_negative(self):
        assert _visible((800, 600, 100, -400), 1920, 1080)  # -400 + 600 > 0

    def test_off_screen_right(self):
        assert not _visible((800, 600, 2000, 100), 1920, 1080)

    def test_off_screen_above(self):
        assert not _visible((800, 600, 100, -700), 1920, 1080)

    def test_off_screen_below(self):
        assert not _visible((800, 600, 100, 1200), 1920, 1080)


class TestFormatPos:
    def test_positive(self):
        assert _format_pos(120) == "+120"

    def test_negative_is_absolute(self):
        # "-340" would mean right-edge distance in Tk, not x=-340.
        assert _format_pos(-340) == "+-340"

    def test_zero(self):
        assert _format_pos(0) == "+0"


class TestShouldSaveGeometry:
    def test_normal_mapped_nonfullscreen_saves(self):
        win = FakeWindow(w=1, h=1, x=0, y=0)
        assert _should_save_geometry(win) is True

    def test_fullscreen_blocks(self):
        win = FakeWindow(w=1, h=1, x=0, y=0, fullscreen=True)
        assert _should_save_geometry(win) is False

    def test_zoomed_blocks(self):
        win = FakeWindow(w=1, h=1, x=0, y=0, state="zoomed")
        assert _should_save_geometry(win) is False

    def test_iconic_blocks(self):
        win = FakeWindow(w=1, h=1, x=0, y=0, state="iconic")
        assert _should_save_geometry(win) is False

    def test_exception_safe(self):
        class BlowsUp:
            def state(self):
                raise RuntimeError("window gone")

            def winfo_ismapped(self):
                return True

            def attributes(self, *_):
                return False

        assert _should_save_geometry(BlowsUp()) is False


class TestSaveGeometry:
    def test_builds_string_from_resolved_winfo(self):
        win = FakeWindow(w=1100, h=700, x=120, y=45)
        assert save_window_geometry(win) == "1100x700+120+45"
        assert not win.calls  # pure winfo reads, never geometry()

    def test_negative_position(self):
        win = FakeWindow(w=800, h=600, x=-340, y=-120)
        assert save_window_geometry(win) == "800x600+-340+-120"

    def test_skips_non_normal_states(self):
        for state in ("zoomed", "iconic", "withdrawn"):
            win = FakeWindow(w=1100, h=700, x=120, y=45, state=state)
            assert save_window_geometry(win) == ""

    def test_skips_unmapped(self):
        win = FakeWindow(w=1100, h=700, x=120, y=45, mapped=False)
        assert save_window_geometry(win) == ""

    def test_skips_fullscreen(self):
        win = FakeWindow(w=1100, h=700, x=120, y=45, fullscreen=True)
        assert save_window_geometry(win) == ""


class TestRestoreGeometry:
    def test_applies_valid_geometry(self):
        win = FakeWindow(w=800, h=600, x=0, y=0)
        assert restore_window_geometry(win, "1100x700+120+45") is True
        assert win.calls[-1] == ("1100x700+120+45",)

    def test_applies_negative_absolute_position(self):
        win = FakeWindow(w=800, h=600, x=0, y=0)
        assert restore_window_geometry(win, "800x600+-340+120") is True
        # The restore must keep the '+-' absolute form - a '-' separator
        # would silently mean right-edge in Tk.
        assert win.calls[-1] == ("800x600+-340+120",)

    def test_empty_or_garbage_returns_false(self):
        win = FakeWindow(w=800, h=600, x=0, y=0)
        assert restore_window_geometry(win, "") is False
        assert restore_window_geometry(win, "not-a-geometry") is False
        assert not win.calls

    def test_off_screen_rejected(self):
        win = FakeWindow(w=800, h=600, x=0, y=0)
        assert restore_window_geometry(win, "800x600+5000+5000") is False
        assert not win.calls

    def test_oversized_clamped_then_applied(self):
        win = FakeWindow(w=800, h=600, x=0, y=0)
        assert restore_window_geometry(win, "3000x3000+100+100") is True
        assert win.calls[-1] == ("1860x1020+100+100",)

    def test_round_trip(self):
        win = FakeWindow(w=1100, h=700, x=120, y=45)
        saved = save_window_geometry(win)
        win2 = FakeWindow(w=1, h=1, x=0, y=0)
        assert restore_window_geometry(win2, saved) is True
        assert win2.calls[-1] == ("1100x700+120+45",)

    def test_negative_round_trip(self):
        win = FakeWindow(w=800, h=600, x=-340, y=-120)
        saved = save_window_geometry(win)
        win2 = FakeWindow(w=1, h=1, x=0, y=0)
        assert restore_window_geometry(win2, saved) is True
        assert win2.calls[-1] == ("800x600+-340+-120",)