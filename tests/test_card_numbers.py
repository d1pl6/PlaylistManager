"""Card-grid registry numbers - Settings > show_playlist_numbers.

Widget-side half of the feature: ``CardGridManager.refresh_card_numbers``
fills each card's number label with the value ``playlistmanager --list``
prints (or hides every label while the setting is off), and
``set_show_numbers`` toggles it live from Settings.  The number map itself
is unit-tested in tests/test_playlist_store.py (``registry_numbers``).

Auto-skips without a DISPLAY, exactly like tests/test_i18n_gui.py:

    xvfb-run -a python -m pytest tests/test_card_numbers.py

conftest.py has already redirected the cfg/db paths to a temp tree; the
fixture re-points the registry file itself at a fresh ``tmp_path``.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"), reason="no DISPLAY (run under xvfb-run)"
)

# Registry order == --list order, so the cards (created in this order)
# must number 1. 2. 3. regardless of the middle row's platform.
ROWS = [
    {"name": "Chill", "platform": "spotify", "playlist_id": "pl1"},
    {"name": "Gone", "platform": "not_installed", "playlist_id": "pl2"},
    {"name": "Bangers", "platform": "youtube_music", "playlist_id": "pl3"},
]


def _gridded(label) -> bool:
    """True when the label is currently managed by the grid manager.

    ``winfo_ismapped`` is useless here: the root is withdrawn in tests, so
    a gridded child reports 0 either way.  ``winfo_manager`` goes back to
    ``""`` on ``grid_remove`` (empirically checked) but stays ``"grid"``
    while shown.
    """
    return label.winfo_manager() == "grid"


@pytest.fixture
def build(monkeypatch, tmp_path):
    """Build a CardGridManager over a sandboxed 3-row registry.

    Returns ``build(show_numbers: bool) -> CardGridManager``; each call
    creates fresh cards and assigns their identity the same way
    ``MainWindow._populate_grid`` does (name label + platform + id).
    """
    import tkinter as tk

    import services.playlist_store as ps
    from ui.card_grid import CardGridManager

    registry = tmp_path / "db" / "playlists.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(ROWS), encoding="utf-8")
    monkeypatch.setattr(ps, "playlists_json", registry)
    monkeypatch.setattr(ps, "_playlist_cache", None)
    monkeypatch.setattr(ps, "_cache_timestamp", 0.0)

    root = tk.Tk()
    root.withdraw()
    main_area = tk.Frame(root)
    content = tk.Frame(main_area)

    class _SF:  # duck-typed ScrollableFrame (only update_scrollregion is hit)
        def update_scrollregion(self):
            pass

    def _make(show_numbers: bool):
        grid = CardGridManager(
            main_area,
            content,
            _SF(),
            root,
            columns=2,
            song_manager=None,
            keybind_controller=None,
            show_log=True,
            show_stats=True,
            showcase_count=0,
            playlist_cover_img=None,
            close_playlist_img=None,
            reload_database_img=None,
            pin_img=None,
            pin_active_img=None,
            show_numbers=show_numbers,
            make_keybind_callbacks=lambda i: None,
            on_reload_requested=lambda i: None,
            start_recording=lambda i, name="": None,
            auto_resize=lambda: None,
            before_card_close=lambda i: None,
            after_card_close=lambda i: None,
            prune_frame_imgs=lambda c: None,
            get_search_results_height=lambda i: None,
            is_recording=lambda i: False,
            open_playlist_dialog=lambda: None,
        )
        grid.create_main_frame(len(ROWS))
        for card, row in zip(grid.cards, ROWS):
            card.name_label.config(text=row["name"])
            card.playlist_name = row["name"]
            card.platform = row["platform"]
            card.playlist_id = row["playlist_id"]
        return grid

    yield _make
    try:
        root.destroy()
    except Exception:
        pass


class TestRefreshCardNumbers:
    def test_setting_off_hides_every_label(self, build):
        grid = build(show_numbers=False)
        grid.refresh_card_numbers()
        assert all(not _gridded(c.number_label) for c in grid.cards)

    def test_numbers_follow_registry_order(self, build):
        grid = build(show_numbers=True)
        grid.refresh_card_numbers()
        assert [c.number_label.cget("text") for c in grid.cards] == [
            "1.",
            "2.",
            "3.",
        ]
        assert all(_gridded(c.number_label) for c in grid.cards)

    def test_name_label_text_is_never_prefixed(self, build):
        # name_label.cget("text") is the card's identity lookup key across
        # the codebase - the number must live in its own widget.
        grid = build(show_numbers=True)
        grid.refresh_card_numbers()
        assert [c.name_label.cget("text") for c in grid.cards] == [
            r["name"] for r in ROWS
        ]

    def test_identity_missing_from_registry_stays_hidden(self, build):
        grid = build(show_numbers=True)
        grid.cards[1].playlist_id = "not-in-registry"
        grid.cards[1].name_label.config(text="Renamed")
        grid.refresh_card_numbers()
        assert _gridded(grid.cards[0].number_label)
        assert not _gridded(grid.cards[1].number_label)
        assert _gridded(grid.cards[2].number_label)

    def test_live_toggle_on_then_off(self, build):
        grid = build(show_numbers=False)
        grid.set_show_numbers(True)
        assert all(_gridded(c.number_label) for c in grid.cards)
        assert grid.cards[2].number_label.cget("text") == "3."
        grid.set_show_numbers(False)
        assert all(not _gridded(c.number_label) for c in grid.cards)

    def test_apply_theme_keeps_name_palette_on_number(self, build):
        grid = build(show_numbers=True)
        grid.refresh_card_numbers()
        grid.apply_theme()  # must not raise, must not use the log palette
        number = grid.cards[0].number_label
        name = grid.cards[0].name_label
        assert str(number.cget("background")) == str(name.cget("background"))
        assert str(number.cget("foreground")) == str(name.cget("foreground"))


class TestSettingsToggle:
    """The Settings > Appearance checkbox: writes the setting, fires the callback."""

    def test_checkbox_writes_setting_and_notifies(self):
        import tkinter as tk

        from ui.settings_ui import show_settings_dialog
        from utils.config import get_setting, set_setting
        from utils.i18n import set_language, tr

        set_setting("show_playlist_numbers", False)
        seen: list[bool] = []
        root = tk.Tk()
        root.withdraw()
        try:
            set_language("en")
            show_settings_dialog(
                root, on_restart_app=lambda: None, on_numbers_change=seen.append
            )
            root.update_idletasks()
            dialog = next(
                w for w in root.winfo_children() if w.winfo_class() == "Toplevel"
            )

            def _find(widget):
                for child in widget.winfo_children():
                    if (
                        child.winfo_class() == "Checkbutton"
                        and child.cget("text") == tr("settings.show_playlist_numbers")
                    ):
                        return child
                    hit = _find(child)
                    if hit is not None:
                        return hit
                return None

            check = _find(dialog)
            assert check is not None, "show_playlist_numbers checkbox not built"
            check.invoke()
            assert seen == [True], "live callback did not fire with True"
            assert get_setting("show_playlist_numbers", False) is True
            check.invoke()
            assert seen == [True, False]
            assert get_setting("show_playlist_numbers", False) is False
        finally:
            set_setting("show_playlist_numbers", False)  # leave defaults behind
            try:
                root.destroy()
            except Exception:
                pass
