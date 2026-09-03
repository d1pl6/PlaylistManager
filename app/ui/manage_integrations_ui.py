"""Integration manage dialog: download / uninstall platform plugins.

Opened from the login window's "Manage" button.  Lists every platform
that has a download source (:data:`integration_manager.INTEGRATION_REPOS`)
plus any additional plugins already on disk, and lets the user:

* **Download** - fetch the plugin repo and install it into
  ``integrations/<platform>/`` (worker thread; network);
* **Uninstall** - remove the plugin folder *with database, etc.*:
  credentials, playlist registry entries, per-platform song databases
  (``db/<platform>/``) and duplicate-queue / error records.

All disk and network work lives in :mod:`services.integration_manager`;
this module only orchestrates the live registries and renders the UI.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Callable, Dict, List, Optional

from plugin_loader import PluginRegistry
from services import duplicate_queue, integration_manager
from ui.scrollable import ScrollableFrame
from utils.logging_config import user_log
from utils.scaling import ui_font
from utils.theme import C, btn_colors

logger = logging.getLogger(__name__)


def show_manage_dialog(
    parent,
    *,
    plugin_registry: Optional[object] = None,
    integration_registry: Optional[object] = None,
    keybind_controller: Optional[object] = None,
    on_uninstall: Optional[Callable[[str], None]] = None,
    on_plugins_changed: Optional[Callable[[], None]] = None,
):
    """Show the manage dialog (modal Toplevel); returns the Toplevel.

    Args:
        parent: tkinter parent (normally the login dialog).
        plugin_registry: the *live* PluginRegistry (the same object App /
            KeybindController hold) so uninstalls and downloads stay
            visible to the running app immediately.  When None a fresh
            registry is discovered.
        integration_registry: live IntegrationRegistry; the uninstall
            path unregisters the platform's integration so no consumer
            can resurrect it.
        keybind_controller: KeybindController whose flow controller and
            URL receiver for the removed platform are dropped via
            ``update_credentials(refreshed_ids=[platform])``.
        on_uninstall: ``callback(platform_id)`` invoked BEFORE the disk
            cleanup so the main window can close the platform's playlist
            cards through the canonical per-card teardown (keybind +
            registry entry + song DB + widget).
        on_plugins_changed: ``callback()`` invoked after a successful
            download or uninstall so the app can re-discover the plugin
            registry and register/swap integrations without a restart.
    """
    win_bg = C["frame_main_bg"]
    header_bg = C["frame_head_bg"]
    label_fg = C["label_def_fg"]
    content_bg = C["scrollable_frame_bg"]
    good_fg = C["label_playlist_good_fg"]
    bad_fg = C["label_playlist_error_fg"]

    if plugin_registry is None:
        plugin_registry = PluginRegistry().discover()

    win = tk.Toplevel(parent)
    win.title("Manage integrations")
    win.configure(background=win_bg)
    win.transient(parent)
    win.update_idletasks()
    win.grab_set()
    win.geometry("460x420")
    win.minsize(360, 280)

    tk.Label(
        win,
        text="Integrations",
        background=header_bg,
        foreground=label_fg,
        font=ui_font(14),
    ).pack(fill="x", pady=6)

    # Bulk-action bar: "Download all" fetches every catalog platform that
    # is not yet installed, "Uninstall all" removes every installed one.
    # The buttons are disabled while their bulk worker runs so the set
    # cannot change under it.
    actions = tk.Frame(win, background=win_bg)
    actions.pack(fill="x", padx=10, pady=(2, 4))

    btn_download_all = tk.Button(
        actions,
        text="Download all",
        cursor="hand2",
        **btn_colors(C["button_save_bg"], C["button_save_fg"]),
        highlightthickness=0,
        relief="raised",
        font=ui_font(9),
        state="disabled",
        command=lambda: _bulk_download_all(),
    )
    btn_download_all.pack(side="left")

    btn_uninstall_all = tk.Button(
        actions,
        text="Uninstall all",
        cursor="hand2",
        **btn_colors(C["button_close_bg"], C["button_close_fg"]),
        highlightthickness=0,
        relief="raised",
        font=ui_font(9),
        state="disabled",
        command=lambda: _bulk_uninstall_all(),
    )
    btn_uninstall_all.pack(side="left", padx=(8, 0))

    btn_update_all = tk.Button(
        actions,
        text="Update all",
        cursor="hand2",
        **btn_colors(C["button_save_bg"], C["button_save_fg"]),
        highlightthickness=0,
        relief="raised",
        font=ui_font(9),
        state="disabled",
        command=lambda: _bulk_update_all(),
    )
    btn_update_all.pack(side="left", padx=(8, 0))

    sf = ScrollableFrame(
        win,
        bg=content_bg,
        show_scrollbar=True,
        bind_all_mousewheel=True,
    )
    sf.pack(side="top", fill="both", expand=True, padx=10, pady=(4, 4))
    sf.style_scrollbar(
        C["button_main_bg"], content_bg,
    )
    rows_frame = sf.content

    footer = tk.Label(
        win,
        text="",
        background=win_bg,
        foreground=label_fg,
        font=ui_font(9),
        anchor="w",
    )
    footer.pack(fill="x", padx=12, pady=(0, 8))

    def _set_footer(text: str, *, ok: bool = False, error: bool = False) -> None:
        footer.config(
            text=text,
            foreground=good_fg if ok else (bad_fg if error else label_fg),
        )

    # Platform ids with a download/uninstall in flight.  Rendering their
    # rows busy (button disabled) removes the double-click window on the
    # same platform and makes the busy state survive a row refresh.
    _busy: set = set()
    # True while a bulk operation ("Download all" / "Uninstall all") runs.
    # Guards against starting a bulk op while per-platform or another bulk
    # op is in flight, and disables the bulk buttons (mutating the set
    # under a running bulk worker would be racy).
    _bulk_busy: list = [False]
    # Cached latest GitHub release numbers per platform id (populated by
    # _has_update on first access, invalidated on download/update/refresh).
    _update_cache: dict = {}

    def _has_update(pid: str) -> bool:
        """Return True if the installed plugin is behind the latest release.

        Caches the remote version per platform so the GitHub API is hit at
        most once per platform per dialog session.  Caches are cleared on
        download/update completion and on the initial _refresh_rows call
        that follows them.
        """
        if pid in _update_cache:
            return _update_cache[pid]
        plugin = plugin_registry.get(pid)
        if plugin is None or plugin.version is None:
            _update_cache[pid] = False
            return False
        remote = integration_manager.check_latest_version(pid)
        if remote is None:
            _update_cache[pid] = False
            return False
        has = remote > plugin.version
        _update_cache[pid] = has
        return has

    def _update_bulk_buttons() -> None:
        """Refresh bulk-button enabled state from the installed set + busy."""
        ids, installed = _platform_list()
        has_downloadable = any(
            pid in integration_manager.INTEGRATION_REPOS
            for pid in ids
            if pid not in installed and pid not in _busy
        )
        has_uninstallable = any(pid in installed for pid in ids)
        has_updatable = any(
            pid in installed
            and pid in integration_manager.INTEGRATION_REPOS
            and pid not in _busy
            and _has_update(pid)
            for pid in ids
        )
        try:
            btn_download_all.config(
                state="normal" if has_downloadable and not _bulk_busy[0] else "disabled"
            )
            btn_uninstall_all.config(
                state="normal" if has_uninstallable and not _bulk_busy[0] else "disabled"
            )
            btn_update_all.config(
                state="normal" if has_updatable and not _bulk_busy[0] else "disabled"
            )
        except tk.TclError:
            pass  # bulk bar destroyed with the dialog

    def _platform_list() -> "tuple[List[str], Dict[str, object]]":
        """(ordered platform ids, installed PluginInfo dict)."""
        installed = plugin_registry.get_all()
        ids: List[str] = []
        for pid in integration_manager.installable_ids():
            ids.append(pid)
        for pid in installed:
            if pid not in ids:
                ids.append(pid)
        return ids, installed

    def _refresh_rows() -> None:
        for child in rows_frame.winfo_children():
            child.destroy()

        ids, installed = _platform_list()
        if not ids:
            tk.Label(
                rows_frame,
                text="No integrations available",
                background=content_bg,
                foreground=label_fg,
                font=ui_font(10),
            ).pack(pady=20)
            return

        for pid in ids:
            is_installed = pid in installed
            busy = pid in _busy
            catalog_repo = integration_manager.INTEGRATION_REPOS.get(pid)
            if catalog_repo is not None:
                display_name = catalog_repo.display_name
            else:
                display_name = getattr(installed[pid], "display_name", pid)

            row = tk.Frame(rows_frame, background=content_bg, padx=8, pady=4)
            row.pack(fill="x", padx=4, pady=2)

            tk.Label(
                row,
                text=display_name,
                background=content_bg,
                foreground=label_fg,
                font=ui_font(10),
                anchor="w",
            ).pack(side="left")

            status_lbl = tk.Label(
                row,
                text="Installed" if is_installed else "Not installed",
                background=content_bg,
                foreground=good_fg if is_installed else label_fg,
                font=ui_font(9),
            )
            status_lbl.pack(side="left", padx=(8, 0))

            if busy:
                # In-flight download or uninstall: no button, just the
                # status label showing which phase we are in.
                status_lbl.config(
                    text="Uninstalling…" if is_installed else "Downloading…",
                    foreground=label_fg,
                )
                continue
            btn = None
            update_btn = None
            if is_installed:
                btn = tk.Button(
                    row,
                    text="Uninstall",
                    cursor="hand2",
                    **btn_colors(C["button_close_bg"], C["button_close_fg"]),
                    highlightthickness=0,
                    relief="raised",
                    font=ui_font(9),
                    command=lambda p=pid: _confirm_uninstall(p),
                )
                if catalog_repo is not None and _has_update(pid):
                    update_btn = tk.Button(
                        row,
                        text="Update",
                        cursor="hand2",
                        **btn_colors(C["button_save_bg"], C["button_save_fg"]),
                        highlightthickness=0,
                        relief="raised",
                        font=ui_font(9),
                        command=lambda p=pid, r=row, l=status_lbl: _start_update(p, r, l),
                    )
            elif catalog_repo is not None:
                btn = tk.Button(
                    row,
                    text="Download",
                    cursor="hand2",
                    **btn_colors(C["button_save_bg"], C["button_save_fg"]),
                    highlightthickness=0,
                    relief="raised",
                    font=ui_font(9),
                    command=lambda p=pid, r=row, l=status_lbl: _start_download(p, r, l),
                )
            else:
                continue  # custom plugin present but no repo: nothing to do

            if btn is not None:
                btn.pack(side="right")
            if update_btn is not None:
                update_btn.pack(side="right", padx=(4, 0))

        sf.update_scrollregion()
        _update_bulk_buttons()

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    # Dispatch target for worker completions: the Tk root, which outlives
    # both this dialog and the login dialog that hosts it.  A download or
    # uninstall finishing after the dialogs are closed must still deliver
    # its completion callback (including the app-level refresh); the login
    # window does not survive to do that - see _after_main.
    try:
        top = parent.winfo_toplevel()
        _dispatch_root = top.master if top.master is not None else top
    except Exception:
        _dispatch_root = parent

    def _after_main(fn) -> None:
        """Marshal *fn* to the main thread via the Tk root.

        The manage dialog (and the login dialog hosting it) can be closed
        while a download or uninstall worker is running; ``win.after`` on
        a destroyed Toplevel raises ``TclError`` and would silently drop
        the completion callback - including the ``on_plugins_changed``
        that makes the change apply to the running app.  The root
        outlives both dialogs, so it is the safe dispatch target; only
        when the whole app is quitting is the refresh skipped (picked up
        on next launch).
        """
        try:
            _dispatch_root.after(0, fn)
        except Exception:
            logger.debug("App is shutting down - download/uninstall applied lazily")

    def _start_download(pid: str, row: tk.Frame, status_lbl: tk.Label) -> None:
        if pid in _busy or _bulk_busy[0]:
            return  # already in flight or a bulk op owns the platforms
        _busy.add(pid)
        catalog_repo = integration_manager.INTEGRATION_REPOS[pid]
        btn = next(
            (w for w in row.winfo_children() if isinstance(w, tk.Button)), None
        )
        if btn is not None:
            btn.config(state="disabled", text="Downloading…")
        else:
            status_lbl.config(text="Downloading…", foreground=label_fg)

        def _worker() -> None:
            error: Optional[str] = None
            try:
                integration_manager.download_integration(pid)
            except Exception as e:
                logger.exception("Download failed for %s", pid)
                error = str(e)

            def _apply() -> None:
                nonlocal error
                _busy.discard(pid)
                # The app-level refresh must run even when the dialog was
                # closed mid-download - without it the installed plugin
                # stays invisible (no login tile, no keybind) until the
                # next launch.
                if error is None and on_plugins_changed is not None:
                    try:
                        on_plugins_changed()
                    except Exception as e:
                        logger.exception("Plugin rescan failed after download")
                        error = f"Downloaded, but reload failed: {e}"
                try:
                    dialog_open = bool(win.winfo_exists())
                except tk.TclError:
                    dialog_open = False
                if not dialog_open:
                    return
                _refresh_rows()
                if error is not None:
                    _set_footer(f"Download failed: {error}", error=True)
                else:
                    _set_footer(
                        f"{catalog_repo.display_name} installed",
                        ok=True,
                    )

            _after_main(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Uninstall (per-platform)
    # ------------------------------------------------------------------

    def _teardown_ui(pid: str) -> List[str]:
        """Drop a platform's live UI state before its disk cleanup.

        Runs on the main thread: cards (per-playlist keybind + store entry
        + DB), then the platform's flow/receiver, then the live registry
        objects - the listeners must not be able to resurrect the platform
        while the disk cleanup runs.  Best-effort: a failure is logged and
        the next step still runs (the disk cleanup below is idempotent and
        registry-independent, so ordering stays safe even with a hiccup).
        Returns the list of steps that failed ("" when none).
        """
        errors: List[str] = []
        try:
            if on_uninstall is not None:
                on_uninstall(pid)
        except Exception:
            logger.exception("Card teardown failed for %s", pid)
            errors.append("card teardown")
        try:
            if keybind_controller is not None:
                keybind_controller.update_credentials(refreshed_ids=[pid])
        except Exception:
            logger.exception("Flow teardown failed for %s", pid)
            errors.append("flow teardown")
        try:
            if integration_registry is not None:
                integration_registry.unregister(pid)
            plugin_registry.unregister(pid)
        except Exception:
            logger.exception("Registry unregister failed for %s", pid)
            errors.append("registry unregister")
        return errors

    def _uninstall_platform(pid: str, plugin, teardown_errors: List[str]) -> None:
        """Disk cleanup for one platform, run on a worker thread.

        Runs ``uninstall_platform_data`` (credentials / registry entries /
        song DBs / duplicate-queue / plugin dir) plus a re-purge for any
        error a flow wrote the moment the receiver stopped.  Then applies
        the result back on the main thread via *footer_for*.
        """

        def _worker() -> None:
            error: Optional[str] = None
            report = None
            try:
                report = integration_manager.uninstall_platform_data(pid, plugin=plugin)
                # Re-purge duplicate/error records: a flow that aborted the
                # moment the receiver/flow stopped may have written an
                # error between the first purge and now.
                duplicate_queue.purge_platform(pid)
            except Exception as e:
                logger.exception("Uninstall failed for %s", pid)
                error = str(e)

            def _apply() -> None:
                _busy.discard(pid)
                # App-level refresh no matter the outcome: on success it
                # drops the removed plugin; when the disk cleanup failed
                # (e.g. the plugin folder could not be removed) it
                # discovers the still-present plugin again and re-registers
                # the integration, so the platform is not lost from the
                # running session - the user can retry from its row.
                reload_failed: Optional[str] = None
                if on_plugins_changed is not None:
                    try:
                        on_plugins_changed()
                    except Exception as e:
                        logger.exception("Plugin rescan failed after uninstall")
                        reload_failed = str(e)
                try:
                    dialog_open = bool(win.winfo_exists())
                except tk.TclError:
                    dialog_open = False
                if not dialog_open:
                    return
                _refresh_rows()
                if error is not None:
                    text = f"Uninstall failed: {error}"
                    if plugin_registry.get(pid) is not None:
                        text += (" - the integration is still installed; "
                                 "retry from its row")
                    if reload_failed:
                        text += f" (reload: {reload_failed})"
                    _set_footer(text, error=True)
                    return
                name = _display_name(plugin, pid)
                parts = [
                    f"{report['credentials']} credential file(s)",
                    f"{report['playlists']} playlist(s)",
                    f"{report['databases']} database file(s)",
                    f"{report['pending']} pending",
                    f"{report['songs']} duplicate(s)",
                    f"{report['errors']} error(s)",
                    f"{report['plugin_dirs']} folder(s)",
                ]
                text = f"{name} uninstalled ({', '.join(parts)})"
                if report["plugin_dirs"] == 0:
                    text += " - plugin folder(s) not removed"
                    if plugin_registry.get(pid) is not None:
                        text += " (integration still installed; retry from its row)"
                    if reload_failed:
                        text += f" (reload: {reload_failed})"
                    _set_footer(text, error=True)
                    return
                if teardown_errors or reload_failed:
                    if reload_failed:
                        teardown_errors.append(f"reload ({reload_failed})")
                    _set_footer(
                        text + f" - with warnings ({', '.join(teardown_errors)})",
                        ok=True,
                    )
                else:
                    _set_footer(text, ok=True)

            _after_main(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    def _display_name(plugin, pid: str) -> str:
        """Human-readable name for the given platform id."""
        catalog_repo = integration_manager.INTEGRATION_REPOS.get(pid)
        if catalog_repo is not None:
            return catalog_repo.display_name
        if plugin is not None:
            return getattr(plugin, "display_name", pid)
        return pid

    def _confirm_uninstall(pid: str) -> None:
        if pid in _busy or _bulk_busy[0]:
            return  # already uninstalling or a bulk op owns the platforms
        plugin = plugin_registry.get(pid)
        name = _display_name(plugin, pid)

        # Describe the files the cleanup will actually touch - the
        # manifest-declared credential paths (auth-dir file + declared
        # fallbacks) and the real plugin directory, which may differ from
        # the naive "auth/<pid>" / "integrations/<pid>/" guesses for
        # third-party plugins.
        if plugin is not None and plugin.auth_paths:
            cred_names = sorted({p.name for p in plugin.auth_paths})
            cred_desc = ", ".join(cred_names)
            if len({p.parent for p in plugin.auth_paths}) > 1:
                cred_desc += " (auth dir + fallback locations)"
        else:
            cred_desc = f"auth/{pid} files"
        dir_desc = (
            str(plugin.directory)
            if plugin is not None
            else f"integrations/{pid}/"
        )

        if not messagebox.askyesno(
            "Uninstall integration",
            f"Uninstall {name}?\n\n"
            "This deletes locally:\n"
            f"  \u2022 credentials ({cred_desc})\n"
            f"  \u2022 the plugin folder ({dir_desc})\n"
            f"  \u2022 its playlists from the registry\n"
            f"  \u2022 the song databases (db/{pid}/)\n"
            "  \u2022 pending duplicate and error records\n\n"
            "The online playlists themselves are NOT touched.",
            parent=win,
        ):
            return

        _busy.add(pid)
        teardown_errors = _teardown_ui(pid)
        _uninstall_platform(pid, plugin, teardown_errors)

    # ------------------------------------------------------------------
    # Update (per-platform) — re-download replaces the plugin in-place
    # ------------------------------------------------------------------

    def _start_update(pid: str, row: tk.Frame, status_lbl: tk.Label) -> None:
        if pid in _busy or _bulk_busy[0]:
            return
        _busy.add(pid)
        catalog_repo = integration_manager.INTEGRATION_REPOS[pid]
        # Disable both buttons (Uninstall + Update) while updating.
        for w in row.winfo_children():
            if isinstance(w, tk.Button):
                w.config(state="disabled", text="Updating…")
        status_lbl.config(text="Updating…", foreground=label_fg)
        _update_cache.pop(pid, None)

        def _worker() -> None:
            error: Optional[str] = None
            try:
                integration_manager.download_integration(pid)
            except Exception as e:
                logger.exception("Update failed for %s", pid)
                error = str(e)

            def _apply() -> None:
                nonlocal error
                _busy.discard(pid)
                if error is None and on_plugins_changed is not None:
                    try:
                        on_plugins_changed()
                    except Exception as e:
                        logger.exception("Plugin rescan failed after update")
                        error = f"Updated, but reload failed: {e}"
                try:
                    dialog_open = bool(win.winfo_exists())
                except tk.TclError:
                    dialog_open = False
                if not dialog_open:
                    return
                _refresh_rows()
                if error is not None:
                    _set_footer(f"Update failed: {error}", error=True)
                else:
                    _set_footer(
                        f"{catalog_repo.display_name} updated",
                        ok=True,
                    )

            _after_main(_apply)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Download all / Uninstall all / Update all
    # ------------------------------------------------------------------

    def _bulk_download_all() -> None:
        """Download every catalog platform that is not yet installed."""
        if _bulk_busy[0]:
            return
        targets = [
            pid
            for pid in integration_manager.installable_ids()
            if pid not in plugin_registry.get_all() and pid not in _busy
        ]
        if not targets:
            return
        _bulk_busy[0] = True

        def _worker() -> None:
            failures: List[str] = []
            for pid in targets:
                _busy.add(pid)
                try:
                    integration_manager.download_integration(pid)
                except Exception as e:
                    logger.exception("Download failed for %s", pid)
                    failures.append(f"{_display_name(plugin_registry.get(pid), pid)}: {e}")
                finally:
                    _busy.discard(pid)

            def _apply() -> None:
                _bulk_busy[0] = False
                # Rescan even when some downloads failed: the successful
                # ones must become visible without reopening the dialog.
                if on_plugins_changed is not None:
                    try:
                        on_plugins_changed()
                    except Exception as e:
                        logger.exception("Plugin rescan failed after download-all")
                        failures.append(f"reload: {e}")
                try:
                    dialog_open = bool(win.winfo_exists())
                except tk.TclError:
                    dialog_open = False
                if not dialog_open:
                    return
                _refresh_rows()
                if failures:
                    _set_footer(
                        f"Download all: {len(targets) - len(failures)}/{len(targets)} "
                        f"installed ({len(failures)} failed)",
                        error=True,
                    )
                else:
                    _set_footer(
                        f"Downloaded all integrations ({len(targets)})",
                        ok=True,
                    )

            _after_main(_apply)

        threading.Thread(target=_worker, daemon=True).start()
        _refresh_rows()

    def _bulk_uninstall_all() -> None:
        """Uninstall every platform currently installed."""
        if _bulk_busy[0]:
            return
        installed = plugin_registry.get_all()
        targets = [pid for pid in installed if pid not in _busy]
        if not targets:
            return

        names = ", ".join(_display_name(installed[pid], pid) for pid in targets)
        if not messagebox.askyesno(
            "Uninstall all integrations",
            f"Uninstall all {len(targets)} integrations?\n\n"
            f"{names}\n\n"
            "This deletes, for each platform, its credentials, plugin folder, "
            "playlist registry entries, song databases and pending duplicate "
            "and error records.\n\n"
            "The online playlists themselves are NOT touched.",
            parent=win,
        ):
            return

        _bulk_busy[0] = True
        # Capture the plugin objects BEFORE teardown unregisters them: the
        # disk cleanup needs the manifest-declared credential paths
        # (auth_file_fallbacks), which vanish once unregister() drops the
        # PluginInfo (uninstall_platform_data would then fall back to the
        # hardcoded map and miss the repo-root fallback copy).
        plugins = {pid: plugin_registry.get(pid) for pid in targets}
        # Main-thread teardown for ALL platforms up front (cards, flows,
        # registries) before any disk cleanup starts - the listeners must
        # not resurrect a platform while the bulk disk sweep runs.
        teardown_errors: List[str] = []
        for pid in targets:
            teardown_errors.extend(_teardown_ui(pid))

        def _worker() -> None:
            failures: List[str] = []
            for pid in targets:
                _busy.add(pid)
                plugin = plugins.get(pid)
                try:
                    integration_manager.uninstall_platform_data(pid, plugin=plugin)
                    duplicate_queue.purge_platform(pid)
                except Exception as e:
                    logger.exception("Uninstall failed for %s", pid)
                    failures.append(f"{_display_name(plugin, pid)}: {e}")
                finally:
                    _busy.discard(pid)

            def _apply() -> None:
                _bulk_busy[0] = False
                # Rescan regardless of outcome so platforms whose folder the
                # cleanup could not remove are re-registered instead of
                # vanishing from the session (see per-platform _apply).
                reload_failed: Optional[str] = None
                if on_plugins_changed is not None:
                    try:
                        on_plugins_changed()
                    except Exception as e:
                        logger.exception("Plugin rescan failed after uninstall-all")
                        reload_failed = str(e)
                try:
                    dialog_open = bool(win.winfo_exists())
                except tk.TclError:
                    dialog_open = False
                if not dialog_open:
                    return
                _refresh_rows()
                if failures:
                    text = (
                        f"Uninstall all: {len(targets) - len(failures)}/{len(targets)} "
                        f"done ({len(failures)} failed)"
                    )
                    if reload_failed:
                        text += f" (reload: {reload_failed})"
                    _set_footer(text, error=True)
                else:
                    text = f"Uninstalled all integrations ({len(targets)})"
                    if teardown_errors or reload_failed:
                        if reload_failed:
                            teardown_errors.append(f"reload ({reload_failed})")
                        _set_footer(
                            text + f" - with warnings ({', '.join(sorted(set(teardown_errors)))})",
                            ok=True,
                        )
                    else:
                        _set_footer(text, ok=True)

            _after_main(_apply)

        threading.Thread(target=_worker, daemon=True).start()
        _refresh_rows()

    # ------------------------------------------------------------------
    # Update all
    # ------------------------------------------------------------------

    def _bulk_update_all() -> None:
        """Re-download every installed catalog platform that has an update."""
        if _bulk_busy[0]:
            return
        installed = plugin_registry.get_all()
        targets = [
            pid
            for pid in installed
            if pid in integration_manager.INTEGRATION_REPOS
            and pid not in _busy
            and _has_update(pid)
        ]
        if not targets:
            return

        names = ", ".join(_display_name(installed[pid], pid) for pid in targets)
        if not messagebox.askyesno(
            "Update all integrations",
            f"Update {len(targets)} integration(s)?\n\n"
            f"{names}\n\n"
            "Each plugin will be replaced with the latest version from "
            "GitHub.  Credentials, playlists and databases are kept.",
            parent=win,
        ):
            return

        _bulk_busy[0] = True
        for pid in targets:
            _update_cache.pop(pid, None)

        def _worker() -> None:
            failures: List[str] = []
            for pid in targets:
                _busy.add(pid)
                try:
                    integration_manager.download_integration(pid)
                except Exception as e:
                    logger.exception("Update failed for %s", pid)
                    failures.append(f"{_display_name(installed[pid], pid)}: {e}")
                finally:
                    _busy.discard(pid)

            def _apply() -> None:
                _bulk_busy[0] = False
                reload_failed: Optional[str] = None
                if on_plugins_changed is not None:
                    try:
                        on_plugins_changed()
                    except Exception as e:
                        logger.exception("Plugin rescan failed after update-all")
                        reload_failed = str(e)
                try:
                    dialog_open = bool(win.winfo_exists())
                except tk.TclError:
                    dialog_open = False
                if not dialog_open:
                    return
                _refresh_rows()
                if failures:
                    text = (
                        f"Update all: {len(targets) - len(failures)}/{len(targets)} "
                        f"done ({len(failures)} failed)"
                    )
                    if reload_failed:
                        text += f" (reload: {reload_failed})"
                    _set_footer(text, error=True)
                else:
                    text = f"Updated all integrations ({len(targets)})"
                    if reload_failed:
                        text += f" - reload failed: {reload_failed}"
                    _set_footer(text, ok=True)

            _after_main(_apply)

        threading.Thread(target=_worker, daemon=True).start()
        _refresh_rows()

    _refresh_rows()
    return win