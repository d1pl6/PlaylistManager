"""
Localization (i18n) support.

Per-language string catalogs live in INI files modeled on the theme
pipeline (``utils/theme.py``), loaded in this order per key:

* ``cfg/i18n/<lang>.ini``       user-editable catalog (profile-aware via
                                ``profile_store.cfg_dir()``, self-healed on
                                every ``set_language``: new keys are merged
                                in from the English defaults)
* ``app/i18n/<lang>.ini``       bundled read-only catalogs shipped with the
                                repo (a documented example lives there;
                                English needs no file)
* ``DEFAULT_STRINGS`` (code)    the English source of truth
* the key itself                last resort, so a stale key never raises

UI modules read translations at *widget-creation time* only - the same rule
as ``C[...]`` colours - because a language change applies on the next app
start, not live.

Usage::

    from utils.i18n import tr, trn

    label.config(text=tr("common.ok"))
    heading.config(text=trn("showcase.stats.songs", total, count=total))  # uses .one/.other rows

Plurals use ``<key>.one`` / ``<key>.other`` catalog rows and a ``{count}``
placeholder; ``trn`` picks the form by *count* (one/other for now -
two/few/many are a future extension point).  ``tr`` only calls
``str.format`` when keyword arguments are passed, so catalog strings that
contain literal braces (rare) are safe.
"""

import logging
from configparser import ConfigParser
from pathlib import Path

from services import profile_store as _profile_store
from utils.config import _safe_read_config, _write_ini_file

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"
STRING_SECTION = "strings"

#: User-editable catalogs (profile-aware, same root as settings/theme).
I18N_DIR = _profile_store.cfg_dir() / "i18n"

#: Bundled read-only catalogs shipped inside the app package (``app/i18n/``).
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "i18n"

#: English source of truth and fallback for every language (like
#: ``DEFAULT_THEME`` for colours).  Grows with the call-site migration;
#: groups are keyed by UI domain so catalogs stay easy to maintain.
DEFAULT_STRINGS: dict[str, str] = {
    # --- common: tiny words reused across many modules --------------------
    "common.ok": "OK",
    "common.cancel": "Cancel",
    "common.close": "Close",
    "common.delete": "Delete",
    "common.save": "Save",
    "common.error": "Error",
    "common.add": "Add",
    # --- card/flow status codes ---------------------------------------------
    # The codes themselves are the stable wire format exchanged between
    # services and the UI (keybind_controller / playlist_sync ->
    # main_window / card_grid / showcase_manager) - they are never compared
    # as English text; these rows are what the UI shows.
    "card_status.busy": "Busy",
    "card_status.loading": "Loading",
    "card_status.added": "Added",
    "card_status.exists": "Exists",
    "card_status.duplicate": "Dup?",
    "card_status.error": "Error",
    "card_status.sync": "Sync",
    "card_status.ok": "OK",
    "card_status.no_tracks": "No tracks",
    "card_status.waiting": "Waiting",
    "card_status.removing": "Removing",
    "card_status.removed": "Removed",
    # --- showcase rows --------------------------------------------------------
    "showcase.stats.songs.one": "{count} song",
    "showcase.stats.songs.other": "{count} songs",
    "showcase.stats.followers.one": "{count} follower",
    "showcase.stats.followers.other": "{count} followers",
    # --- settings dialog (app/ui/settings_ui.py) ------------------------------
    "settings.title": "Settings",
    "settings.app_behavior": "App behavior",
    "settings.check_updates_startup": "Check for updates on startup",
    "settings.checking": "Checking...",
    "settings.check_updates_now": "Check for updates now",
    "settings.check_failed_retry": "Check failed - try again",
    "settings.up_to_date": "Up to date!",
    "settings.center_windows": "Center windows after launch",
    "settings.remember_window_geometry": "Remember window size/position",
    "settings.reset_window_geometry": "Reset window size/position",
    "settings.start_fullscreen": "Start app in fullscreen",
    "settings.auto_resize": "Auto-resize main window",
    "settings.global_key_listener": "Use global key listener",
    "settings.wayland_global_hint": "(not available on Wayland - use compositor shortcuts `playlistmanager add N`)",
    "settings.hide_in_tray": "Hide in tray on minimize",
    "settings.start_in_tray": "Start in tray (on next launch)",
    "settings.pin_unpin_buttons": "Pin/unpin buttons (on playlist cards)",
    "settings.sort_playlists_by": "Sort playlists by:",
    "settings.duplicate_check": "Duplicate check",
    "settings.extra_duplicate_check": "Extra duplicate check",
    "settings.dup_ask_hint": "(asks when a similar song is already in the playlist)",
    "settings.title_similarity": "Title similarity:",
    "settings.match_score_hint": "Match score required for a near-duplicate (higher = stricter)",
    "settings.duration_tolerance": "Duration tolerance:",
    "settings.duration_value": "{n}s",
    "settings.duration_hint": "Max length difference between matching tracks, in seconds",
    "settings.scanning": "Scanning...",
    "settings.scan_failed_retry": "Scan failed - try again",
    "settings.no_duplicates_found": "No duplicates found",
    "settings.check_duplicates_now": "Check for duplicates now",
    "settings.appearance": "Appearance",
    "settings.show_log_row": "Show log row (artist / song / status)",
    "settings.show_playlist_stats": "Show playlist stats (songs / duration / followers)",
    "settings.ask_before_song_remove": "Ask before removing a song",
    "settings.ask_before_playlist_remove": "Ask before removing the playlist",
    "settings.default_playlist_removal": "Default playlist removal:",
    "settings.show_last_n_added": "Show last N added songs:",
    "settings.zero_off_hint": "(0 = off)",
    "settings.ui_scale": "UI scale:",
    "settings.restart_to_apply": "(restart to apply)",
    "settings.font": "Font:",
    "settings.language": "Language:",
    # Native display names for the Settings language picker.  Every catalog
    # language row carries its own name unchanged ("Deutsch" is "Deutsch" in
    # German); the "en" row is what each catalog translates to its own word.
    "settings.lang_en": "English",
    "settings.lang_de": "Deutsch",
    "settings.data_saver": "Data saver:",
    "settings.cache_empty": "empty",
    "settings.thumbnails_modes_desc": "Off: Fetch on every launch\nDownload: Keep all thumbnails permanently\nDedupe: Save one copy per song and artist; ideal for large libraries across multiple platforms\nCache: Save only visible items; clear the cache when the PC restarts\nMax: Save metadata only; do not download thumbnails",
    "settings.clear_thumbnail_cache_title": "Clear Thumbnail Cache",
    "settings.clear_thumbnail_cache_ask": "Delete all downloaded and deduplicated thumbnails?\nThey will be re-fetched on demand.",
    "settings.clear_thumbnail_cache": "Clear thumbnail cache",
    "settings.card_columns": "Card columns:",
    "settings.theme_settings": "Theme Settings",
    "settings.like_button": "Like button (under remove button)",
    "settings.scrobble_added_songs": "Scrobble added songs",
    "settings.scrobble_platforms_from": "Platforms to scrobble from:",
    "settings.install_integration_first": "Install integration first",
    "settings.scrobble_keybind": "Scrobble keybind:",
    "settings.keybind_none": "(none)",
    "settings.record": "Record",
    "settings.clear": "Clear",
    "settings.capture_song_via": "Capture current song via:",
    "settings.soundcloud_mode_desc": "hybrid: prefers the browser extension (exact URL + play/pause),\nfalls back to recently-played on a receiver miss\napi: reads the last played track from SoundCloud's API\nextension: extension only (no API fallback)",
    "settings.profiles": "Profiles",
    "settings.profile": "Profile",
    "settings.profile_restart_ask": "The profile change takes effect after the app restarts.\n\nRestart now?",
    "settings.active_profile": "Active profile:",
    "settings.profiles_label": "Profiles:",
    "settings.switch_profile": "Switch profile",
    "settings.switch_profile_ask": "Switch to profile \"{sel}\"?\n\nThe app must restart to apply.",
    "settings.delete_profile_title": "Delete profile",
    "settings.default_profile_not_deletable": "The default profile cannot be deleted.",
    "settings.delete_profile_ask": "Are you sure you want to delete \"{sel}\" profile? This will delete all playlist databases that are not in other profiles.",
    "settings.rename": "Rename",
    "settings.edit": "Edit",
    "settings.profiles_note": "Plugins and their settings stay shared across all profiles.\nSwitching profiles restarts the app.",
    "settings.about": "About",
    "settings.author": "Author: d1pl",
    "settings.repo": "Repo: {url}",
    "settings.version": "Version: {version}",
    "settings.support": "Support:",
    "settings.monero": "monero: soon",
    # Combobox label rows (settings_ui resolves them per stored key).
    "settings.sort_key_name": "Name",
    "settings.sort_key_platform": "Platform",
    "settings.sort_key_added": "Added",
    "settings.sort_key_used": "Last used",
    "settings.thumbnail_mode_off": "Off",
    "settings.thumbnail_mode_download": "Download",
    "settings.thumbnail_mode_dedupe": "Dedupe",
    "settings.thumbnail_mode_cache": "Cache",
    "settings.thumbnail_mode_max": "Max",
    "settings.removal_mode_remove": "Remove",
    "settings.removal_mode_keep_db": "Keep database",
    # --- CLI (app/cli.py) -----------------------------------------------------
    "cli.no_playlists": "No playlists configured. Add one with 'playlistmanager -p add <URL>' or from the GUI.",
    "cli.list_item": "{i}. \"{name}\" ({platform})",
    "cli.result_line": "#{number} \"{name}\" ({platform}): {message}",
    "cli.fetch_details_failed": "error: failed to fetch playlist details: {error}",
    "cli.playlist_not_found": "error: playlist not found on {platform} (deleted, private, or an invalid URL)",
    "cli.not_owner_warning": "warning: you are not the owner or a collaborator of '{name}' - adding songs to it will fail on Spotify's side",
    "cli.added": "Added",
    "cli.updated": "Updated",
    "cli.import_failed_line": "#{number} \"{name}\" ({platform}): {action}, track import failed: {error}",
    "cli.imported_tracks.one": "imported {count} track",
    "cli.imported_tracks.other": "imported {count} tracks",
    "cli.no_playlists_short": "No playlists configured.",
    "cli.deleted_line": "#{number} \"{name}\" ({platform}): deleted",
    "cli.no_playlist_id_line": "#{number} \"{name}\" ({platform}): Error: no playlist_id - re-add it with 'playlistmanager -p add <URL>'",
    "cli.new_songs.one": "{count} new",
    "cli.new_songs.other": "{count} new",
    "cli.refreshed_line": "#{number} \"{name}\" ({platform}): refreshed ({note})",
    "cli.no_scrobble_backend": "error: no scrobble backend available (is Last.fm configured?)",
    "cli.scrobbled": "Scrobbled: {artist} - {title}",
    "cli.scrobble_failed": "error: scrobble failed for {title}",
    "cli.no_song_found": "error: no currently-playing song found",
    "cli.soundcloud_plugin_not_found": "error: SoundCloud plugin not found",
    "cli.prompt_client_id": "Client id: ",
    "cli.prompt_client_secret": "Client secret: ",
    "cli.prompt_refresh_token": "Refresh token (leave empty to reuse stored): ",
    "cli.credentials_required_soundcloud": "error: client id, client secret and a refresh token are all required (from your SoundCloud app's dashboard)",
    "cli.login_ok_soundcloud": "soundcloud: logged in as {display_name}",
    "cli.login_failed_soundcloud": "error: soundcloud login failed: {reason} (existing credentials left untouched)",
    "cli.prompt_arl_cookie": "ARL cookie: ",
    "cli.arl_cookie_required": "error: ARL cookie is required (log into Deezer in your browser, open DevTools → Application → Cookies → deezer.com → copy the 'arl' value)",
    "cli.login_ok_deezer": "deezer: logged in as user {display_name}",
    "cli.login_failed_deezer": "error: deezer login failed: {reason} (existing credentials left untouched)",
    "cli.unknown_platform": "error: unknown platform '{platform}' (use {options})",
    "cli.ym_no_terminal": "youtube_music: no terminal emulator found - manually run:\n  cd {auth_dir}\n  ytmusicapi browser\nand place the generated browser.json in {auth_dir}",
    "cli.ym_opened_terminal": "youtube_music: opened {auth_dir} and a terminal - run 'ytmusicapi browser' there and keep the generated browser.json in that folder",
    "cli.lastfm_gui_only": "lastfm: GUI-only at the moment - open the app's Settings -> Login/Accounts -> Last.fm, enter your API key + secret, and approve the browser authorization",
    "cli.credentials_required_spotify": "error: client id, client secret and a refresh token are all required (the refresh token comes from your Spotify app's dashboard)",
    "cli.login_ok_spotify": "spotify: logged in as {display_name}",
    "cli.login_failed_spotify": "error: spotify login failed: {reason} (existing credentials left untouched)",
    "cli.delete_credentials_failed": "error: failed to delete {platform} credentials: {error}",
    "cli.logged_out": "{platform}: logged out (deleted {deleted})",
    "cli.no_credentials": "{platform}: no credentials found",
    "cli.install_status_updated": "updated",
    "cli.install_status_installed": "installed",
    "cli.install_result": "{pid}: {action} ({display})",
    "cli.install_failed": "error: failed to install {pid}: {error}",
    "cli.not_installed": "{pid}: not installed (nothing to uninstall)",
    "cli.uninstall_failed": "error: failed to uninstall {pid}: {error}",
    # CLI count phrases (trn - .one/.other by count).
    "cli.removed_registry.one": "removed {count} playlist from the registry",
    "cli.removed_registry.other": "removed {count} playlists from the registry",
    "cli.deleted_dbs.one": "deleted {count} local database file",
    "cli.deleted_dbs.other": "deleted {count} local database files",
    "cli.purged": "purged",
    "cli.purge_decisions.one": "{count} pending duplicate decision",
    "cli.purge_decisions.other": "{count} pending duplicate decisions",
    "cli.purge_songs.one": "{count} remembered song",
    "cli.purge_songs.other": "{count} remembered songs",
    "cli.purge_errors.one": "{count} error",
    "cli.purge_errors.other": "{count} errors",
    "cli.uninstall_credentials.one": "{count} credential",
    "cli.uninstall_credentials.other": "{count} credentials",
    "cli.uninstall_playlists.one": "{count} playlist",
    "cli.uninstall_playlists.other": "{count} playlists",
    "cli.uninstall_dbs.one": "{count} DB",
    "cli.uninstall_dbs.other": "{count} DBs",
    "cli.uninstall_folders.one": "{count} folder",
    "cli.uninstall_folders.other": "{count} folders",
    # --- manage-integrations dialog (app/ui/manage_integrations_ui.py) --------
    "manage.dialog_title": "Manage integrations",
    "manage.heading": "Integrations",
    "manage.download_all": "Download all",
    "manage.uninstall_all": "Uninstall all",
    "manage.update_all": "Update all",
    "manage.no_integrations_available": "No integrations available",
    "manage.installed": "Installed",
    "manage.not_installed": "Not installed",
    "manage.uninstalling_ellipsis": "Uninstalling...",
    "manage.downloading_ellipsis": "Downloading...",
    "manage.updating_ellipsis": "Updating...",
    "manage.uninstall": "Uninstall",
    "manage.update": "Update",
    "manage.download": "Download",
    "manage.downloaded_but_reload_failed": "Downloaded, but reload failed: {error}",
    "manage.download_failed": "Download failed: {error}",
    "manage.plugin_installed": "{name} installed",
    "manage.updated_but_reload_failed": "Updated, but reload failed: {error}",
    "manage.update_failed": "Update failed: {error}",
    "manage.plugin_updated": "{name} updated",
    "manage.auth_dir_and_fallbacks": " (auth dir + fallback locations)",
    "manage.auth_dir_files": "auth/{pid} files",
    "manage.uninstall_dialog_title": "Uninstall integration",
    "manage.confirm_uninstall": "Uninstall {name}?\n\n",
    "manage.uninstall_deletes_locally": "This deletes locally:\n",
    "manage.uninstall_credentials": "  • credentials ({desc})\n",
    "manage.uninstall_plugin_folder": "  • the plugin folder ({desc})\n",
    "manage.uninstall_playlists": "  • its playlists from the registry\n",
    "manage.uninstall_song_databases": "  • the song databases (db/{pid}/)\n",
    "manage.uninstall_duplicate_records": "  • pending duplicate and error records\n\n",
    "manage.online_playlists_not_touched": "The online playlists themselves are NOT touched.",
    "manage.uninstall_all_dialog_title": "Uninstall all integrations",
    "manage.update_all_dialog_title": "Update all integrations",
    "manage.bulk_uninstall_description": "This deletes, for each platform, its credentials, plugin folder, playlist registry entries, song databases and pending duplicate and error records.\n\n",
    "manage.bulk_update_description": "Each plugin will be replaced with the latest version from GitHub.  Credentials, playlists and databases are kept.",
    # Uninstall footer - one template per case; the {details}/item rows are
    # plural-correct via trn (count is injected automatically).
    "manage.uninstall_failed": "Uninstall failed: {error}",
    "manage.failed_still_installed": " - the integration is still installed; retry from its row",
    "manage.failed_reload": " (reload: {reason})",
    "manage.uninstalled": "{name} uninstalled ({details})",
    "manage.folders_not_removed": " - plugin folder(s) not removed",
    "manage.integration_still_installed": " (integration still installed; retry from its row)",
    "manage.reload_expr": "reload ({reason})",
    "manage.with_warnings": "{summary} - with warnings ({warnings})",
    "manage.footer_credentials.one": "{count} credential file",
    "manage.footer_credentials.other": "{count} credential files",
    "manage.footer_playlists.one": "{count} playlist",
    "manage.footer_playlists.other": "{count} playlists",
    "manage.footer_databases.one": "{count} database file",
    "manage.footer_databases.other": "{count} database files",
    "manage.footer_pending.one": "{count} pending",
    "manage.footer_pending.other": "{count} pending",
    "manage.footer_duplicates.one": "{count} duplicate",
    "manage.footer_duplicates.other": "{count} duplicates",
    "manage.footer_errors.one": "{count} error",
    "manage.footer_errors.other": "{count} errors",
    "manage.footer_folders.one": "{count} folder",
    "manage.footer_folders.other": "{count} folders",
    # --- login dialog (app/ui/login_ui.py) -------------------------------------
    "login.dialog_title": "Login",
    "login.select_platform": "Select platform",
    "login.no_services": "No music services installed",
    "login.manage": "Manage",
    "login.manual_step_title": "Manual Step Required",
    "login.spotify_login": "Spotify Login",
    "login.lastfm_login": "Last.fm Login",
    "login.soundcloud_login": "SoundCloud Login",
    "login.spotify_credentials": "Spotify Credentials",
    "login.lastfm_credentials": "Last.fm Credentials",
    "login.soundcloud_credentials": "SoundCloud Credentials",
    "login.client_id": "Client ID",
    "login.client_secret": "Client Secret",
    "login.refresh_token": "Refresh Token",
    "login.api_key": "API Key",
    "login.api_secret": "API Secret",
    "login.credentials_deleted": "Credentials deleted",
    "login.no_credentials_file": "No credentials file found",
    "login.delete_failed": "Delete failed: {error}",
    "login.all_fields_required": "All fields are required",
    "login.testing": "Testing...",
    "login.verifying": "Verifying...",
    "login.ok_with_name": "OK: {name}",
    "login.ok_valid_save": "OK: credentials valid - press Save to authorize",
    "login.test": "Test",
    # --- app bootstrap + quit dialogs (app/app.py, controllers/app_controller.py) --
    "app.title": "PlaylistManager",
    "app.ym_auth_failed": "YouTube Music authentication failed:\n{error}",
    # user_log terminal lines (plain or %-style calls converted to tr()).
    "service.integration_unavailable": "{name} integration unavailable ({error})",
    "service.integration_loaded": "{name} integration loaded",
    "service.ytmusicapi_missing": "ytmusicapi not installed - YouTube Music integration disabled ({error})",
    "app.authenticated": "{name} authenticated",
    "app.reauthenticated": "{name} re-authenticated",
    "app.update_available": "Update v{version} available at {url}",
    "app.update_check_failed": "Update check failed: {error}",
    "app.tray_unavailable": "Tray unavailable - hide-to-tray disabled",
    "app.start_in_tray_unavailable": "Start-in-tray requested but the tray is unavailable - showing the window",
    "main.uninstalling_cards_failed": "Uninstalling {platform}: {reason} - their keybinds/databases may remain",
    "main.uninstall_cards_failure": "one or more playlist cards could not be closed",
    "login.browser_json_missing": "browser.json not detected yet - re-login will be picked up on restart",
    "auth.deleted_spotify": "Deleted Spotify credentials",
    "auth.deleted_lastfm": "Deleted Last.fm credentials",
    "auth.deleted_credential_path": "Deleted credentials: {path}",
    "quit.force_quit": "Force-quit",
    "quit.error_while_closing": "An error occurred while closing PlaylistManager:",
    # --- main window toolbar + header (app/ui/main_window.py) ----------------------
    "main.tooltip_login": "Log in to music services",
    "main.tooltip_add_playlist": "Add a playlist",
    "main.activity": "Activity",
    "main.tooltip_activity": "Errors and duplicate songs",
    "main.activity_count": "Activity ({n})",
    "main.title_choose_platform": "Choose Platform",
    "main.pick_platform_prompt": "Select platform to fetch playlists from:",
    "main.integration_error_title": "Integration Error",
    "main.integration_error_msg": "Add integrations following INTEGRATIONS.MD. Check your internet connection and check if the API is down.",
    "main.warning_no_internet": "No internet connection",
    "main.service_unreachable": "{name} service is unreachable",
    # --- playlist cards (app/ui/card.py, card_grid.py) ------------------------------
    "card.close_playlist_tip": "Close playlist",
    "card.pin_playlist": "Pin playlist",
    "card.unpin_playlist": "Unpin playlist",
    "card.record_keybind_tip": "Click to record a keybind",
    "card.reload_tip": "Reload from platform",
    "grid.empty_state": "Click '+' to add a playlist",
    # --- search bar (app/ui/search_manager.py) --------------------------------------
    "search.playlist_placeholder": "Search playlists...",
    "search.song_placeholder": "Search songs...",
    "search.no_matches": "No matches",
    # --- showcase rows (app/ui/showcase_manager.py) ----------------------------------
    "showcase.remove_tip": "Remove from playlist",
    "showcase.like_tip": "Like on Last.fm",
    "showcase.title_image": "Image",
    "showcase.title_remove_song": "Remove song",
    "showcase.confirm_remove_song": "Remove \"{title}\" from {playlist_name}?",
    "showcase.remove": "Remove",
    # --- activity window (app/ui/activity_window.py) ---------------------------------
    "activity.title": "PlaylistManager - Activity",
    "activity.tab_errors": "Errors",
    "activity.tab_duplicates": "Duplicates",
    "activity.clear": "Clear",
    "activity.no_errors": "No errors logged.",
    "activity.errors_count": "Errors ({count})",
    "activity.nothing_waiting": "Nothing waiting - hotkey adds resolved on their own.",
    "activity.scan_hint": "Run Settings → \"Check for duplicates now\" to search.",
    "activity.match_pct": " · {pct}% match",
    "activity.similar_song": "Similar song",
    "activity.already_in_playlist": "already in playlist:",
    "activity.existing_link": "existing on song.link ↗",
    "activity.trying_to_add": "trying to add:      ",
    "activity.new_link": "new on song.link ↗",
    "activity.dont_add": "Don't add",
    "activity.both_variants": "Both variants already in playlist - {name}{pct}",
    "activity.older": "older:  ",
    "activity.newer": "newer:  ",
    "activity.remove_newer": "Remove newer",
    "activity.not_duplicates": "Not duplicates",
    "activity.undo": "Undo",
    "activity.pending_songs_count": "Pending songs ({count})",
    "activity.scan_results_count": "Scan results ({count})",
    "activity.marked_pairs": "Marked / decided pairs ({count}) {arrow}",
    # --- playlist picker (app/ui/playlist_dialog.py) ---------------------------------
    "playlist_picker.select_hint": "Select a Playlist below",
    # --- theme picker (app/ui/settings_theme_ui.py) ----------------------------------
    "theme.settings_header": "Theme settings",
    "theme.combo_label": "Theme:",
    "theme.preset_default": "Default theme",
    "theme.preset_white": "White Theme",
    "theme.change": "Change",
    "theme.apply_title": "Apply Theme",
    "theme.save_title": "Save Theme",
    "theme.save_overwrite_ask": "Overwrite the theme \"{sel}\" with the current palette?",
    "theme.create_title": "Create Theme",
    "theme.name_label": "Theme name:",
    "theme.delete_title": "Delete Theme",
    "theme.delete_ask": "Delete the theme \"{sel}\"? The current palette is not affected.",
    "theme.create": "Create",
    "theme.swatch_root_bg": "Root background",
    "theme.swatch_frame_header_bg": "Frame header background",
    "theme.swatch_frame_main_bg": "Frame main background",
    "theme.swatch_frame_playlist_bg": "Frame playlist background",
    "theme.swatch_scrollable_frame_bg": "Scrollable frame background",
    "theme.swatch_label_default_bg": "Label default background",
    "theme.swatch_label_default_fg": "Label default foreground",
    "theme.swatch_label_playlist_bg": "Label playlist background",
    "theme.swatch_label_playlist_fg": "Label playlist foreground",
    "theme.swatch_playlist_name_bg": "Playlist name background",
    "theme.swatch_playlist_name_fg": "Playlist name foreground",
    "theme.swatch_playlist_log_bg": "Playlist log background",
    "theme.swatch_playlist_log_fg": "Playlist log foreground",
    "theme.swatch_playlist_good_bg": "Playlist good background",
    "theme.swatch_playlist_good_fg": "Playlist good foreground",
    "theme.swatch_playlist_warning_bg": "Playlist warning background",
    "theme.swatch_playlist_warning_fg": "Playlist warning foreground",
    "theme.swatch_playlist_error_bg": "Playlist error background",
    "theme.swatch_playlist_error_fg": "Playlist error foreground",
    "theme.swatch_checkbutton_bg": "Checkbutton background",
    "theme.swatch_checkbutton_fg": "Checkbutton foreground",
    "theme.swatch_checkbutton_selectcolor": "Checkbutton selectcolor",
    "theme.swatch_button_header_bg": "Button header background",
    "theme.swatch_button_header_fg": "Button header foreground",
    "theme.swatch_button_main_bg": "Button main background",
    "theme.swatch_button_main_fg": "Button main foreground",
    "theme.swatch_button_playlist_bg": "Button playlist background",
    "theme.swatch_button_playlist_fg": "Button playlist foreground",
    "theme.swatch_button_close_bg": "Button close background",
    "theme.swatch_button_close_fg": "Button close foreground",
    "theme.swatch_button_save_bg": "Button save background",
    "theme.swatch_button_save_fg": "Button save foreground",
    "theme.swatch_entry_default_bg": "Entry default background",
    "theme.swatch_entry_default_fg": "Entry default foreground",
    "theme.swatch_entry_default_readonly_bg": "Entry default readonlybackground",
    "theme.swatch_entry_playlist_bg": "Entry playlist background",
    "theme.swatch_entry_playlist_fg": "Entry playlist foreground",
    "theme.swatch_entry_playlist_readonly_bg": "Entry playlist readonlybackground",
    "theme.swatch_playlist_stats_bg": "Playlist stats background",
    "theme.swatch_playlist_stats_fg": "Playlist stats foreground",
    "theme.swatch_search_bar_bg": "Search bar background",
    "theme.swatch_search_bar_fg": "Search bar foreground",
    "theme.swatch_search_result_bg": "Search result background",
    "theme.swatch_search_result_fg": "Search result foreground",
    # --- close-playlist dialog (app/ui/close_playlist_dialog.py) ---------------------
    "close_dialog.title": "Close Playlist",
    "close_dialog.ask": "Close playlist \"{name}\"?",
    "close_dialog.explain": "Keep DB keeps the local song cache; Confirm deletes the playlist and its database.",
    "close_dialog.keep_db": "Keep DB",
    "close_dialog.confirm": "Confirm",
    # --- updater dialog (app/ui/updater_ui.py) ---------------------------------------
    "updater.title": "Update Available",
    "updater.available": "PlaylistManager v{version} is available!",
    "updater.current_version": "Current version: v{version}",
    "updater.browser_failed": "Could not open a browser. Download manually from:\n{url}",
    "updater.download": "Download",
    # --- profiles extra (app/ui/profiles_ui.py, sits under settings.*) ---------------
    "settings.add_profile": "Add profile",
    "settings.profile_name": "Profile name",
    "settings.profile_capture_label": "Capture (what this profile keeps separate):",
    "settings.profile_capture_logins": "Logins (login credentials)",
    "settings.profile_capture_playlists": "Playlists (playlists.json, extra.json, databases)",
    "settings.profile_capture_settings": "Settings (settings.ini, theme.ini)",
    "settings.rename_profile": "Rename profile",
    "settings.profile_default_not_renameable": "The default profile cannot be renamed.",
    "settings.profile_new_name": "New name",
    "settings.edit_profile_title": "Edit profile: {name}",
    "settings.profile_keep_separate": "What does this profile keep separate?",
    "settings.profile_changes_restart": "Changes take effect after the app restarts.",
}

#: Known card-status codes (:func:`tr_status` translates only these).
#: Anything outside this set is an opaque plugin/flow message and passes
#: through unchanged.
CARD_STATUS_KEYS: frozenset[str] = frozenset({
    "busy", "loading", "added", "exists", "duplicate", "error",
    "sync", "ok", "no_tracks", "waiting", "removing", "removed",
})

_current_lang: str = DEFAULT_LANGUAGE
_catalog: dict[str, str] = {}
#: keys already reported for a missing format argument (warn once per key).
_warned_format_keys: set[str] = set()


def _validate_lang(lang: str) -> str:
    """Normalize a language code, returning ``DEFAULT_LANGUAGE`` on garbage.

    Allows letters, digits, ``-`` and ``_`` (``en-US`` style codes are
    lowered to ``en-us``).  Rejects empty/overlong/odd values so a corrupt
    settings file can never path-traverse out of the catalog dirs.
    """
    lang = (lang or "").strip().lower()
    if not lang or len(lang) > 32:
        return DEFAULT_LANGUAGE
    if not all(c.isalnum() or c in "-_" for c in lang):
        return DEFAULT_LANGUAGE
    return lang


def _user_file(lang: str) -> Path:
    return I18N_DIR / f"{lang}.ini"


def _bundled_file(lang: str) -> Path:
    return _BUNDLED_DIR / f"{lang}.ini"


def ensure_i18n_file(lang: str) -> Path:
    """Merge missing English defaults into the user's ``<lang>.ini``.

    Same semantics as ``ensure_theme_file``: existing/unknown keys are
    preserved (a translator's work is never overwritten), new keys are
    appended as English placeholders, and the file is only written when
    something changed or it did not exist.  Keys the bundled catalog for
    *lang* already translates are skipped - the bundled file serves those
    and a user copy would only shadow it with the English default.  New
    keys therefore appear in every catalog automatically instead of
    falling through silently.
    """
    lang = _validate_lang(lang)
    if lang == DEFAULT_LANGUAGE:
        return _user_file(lang)  # English needs no file (code IS the catalog)
    path = _user_file(lang)
    cfg = ConfigParser()
    if path.exists():
        cfg = _safe_read_config(cfg, path)
    # Keys the bundled catalog already translates: leave them to the bundle
    # (a user-file English placeholder would shadow the translation).
    bundled_keys: set[str] = set()
    bundled_path = _bundled_file(lang)
    if bundled_path.exists():
        bundled_cfg = _safe_read_config(ConfigParser(), bundled_path)
        if STRING_SECTION in bundled_cfg:
            bundled_keys = set(bundled_cfg.options(STRING_SECTION))
    changed = False
    # Retire stale self-heal placeholders now covered by the bundled file.
    # A row that is byte-identical to the English default was written by an
    # earlier heal run (never by a translator), so once the bundled file
    # grows a translation for it the placeholder would shadow the German -
    # prune it and let the bundle win.  Real user edits (value != default)
    # are unaffected: they are deliberate overrides and stay.
    if STRING_SECTION in cfg and bundled_keys:
        for key in list(cfg[STRING_SECTION]):
            default = DEFAULT_STRINGS.get(key)
            if default is None:  # legacy user row, no code counterpart
                continue
            # Reader-normalized comparison (leading/trailing whitespace and
            # continuation-line indent do not survive the INI round trip).
            if key in bundled_keys and cfg[STRING_SECTION][key].strip() == default.strip():
                del cfg[STRING_SECTION][key]
                changed = True
    if STRING_SECTION not in cfg:
        cfg[STRING_SECTION] = {}
        changed = True
    for key, value in DEFAULT_STRINGS.items():
        if key not in cfg[STRING_SECTION] and key not in bundled_keys:
            # ConfigParser's BasicInterpolation rejects a lone '%' at set()
            # time, so values are stored double-escaped and unravel back to
            # '%' on read (cfg.items/get interpolates "%%" -> "%").
            cfg[STRING_SECTION][key] = value.replace("%", "%%")
            changed = True
    if not path.exists() or changed:
        _write_ini_file(path, cfg)
    return path


def _load_catalog(lang: str) -> dict[str, str]:
    """Effective catalog for *lang*: English defaults overlaid by the
    bundled file (if any), then the user file (user wins)."""
    catalog = dict(DEFAULT_STRINGS)
    for path in (_bundled_file(lang), _user_file(lang)):
        if not path.exists():
            continue
        cfg = ConfigParser()
        cfg = _safe_read_config(cfg, path)
        if STRING_SECTION in cfg:
            for key, value in cfg.items(STRING_SECTION):
                catalog[key] = value
    return catalog


def set_language(lang: str) -> None:
    """Activate *lang* for this process (``en`` is the built-in default).

    Non-English languages first self-heal their user catalog file, then
    load it over the bundled file and the code defaults; the loader order
    is user file -> bundled file -> English -> key.  Invalid codes fall
    back to English.  Restart the app to re-run with a different language.
    """
    global _current_lang, _catalog
    lang = _validate_lang(lang)
    if lang == DEFAULT_LANGUAGE:
        _current_lang = DEFAULT_LANGUAGE
        _catalog = {}
        return
    file_path = ensure_i18n_file(lang)
    logger.debug("Language '%s' active (catalog %s)", lang, file_path)
    _current_lang = lang
    _catalog = _load_catalog(lang)


def current_language() -> str:
    return _current_lang


def available_languages() -> list[str]:
    """Sorted language codes: ``en`` plus every catalog file (bundled +
    user), deduplicated.  Feeds the Settings language picker.  Non-language
    reference catalogs (the ``example.ini`` template) are excluded."""
    langs = {DEFAULT_LANGUAGE}
    for base_dir in (_BUNDLED_DIR, I18N_DIR):
        try:
            for path in base_dir.glob("*.ini"):
                if path.stem.lower() != "example":  # template, not a language
                    langs.add(path.stem.lower())
        except OSError:
            continue
    return sorted(langs)


def language_display(code: str) -> str:
    """Native display name for a language code for the Settings picker.

    Rows live under ``settings.lang_<code>`` (each catalog carries its own
    language's name unchanged; the "en" row is translated per catalog).
    Codes without a row fall back to the code itself, so a user-dropped
    ``fr.ini`` still shows up selectable as "fr" until a name row exists.
    """
    key = f"settings.lang_{code}"
    name = tr(key)
    return code if name == key else name


def language_code_for(display: str) -> str:
    """Reverse of :func:`language_display` -- the Settings picker resolves
    the selected display name back to its language code.  Unknown displays
    fall back to the active language."""
    for code in available_languages():
        if language_display(code) == display:
            return code
    return current_language()


def tr(key: str, **fmt) -> str:
    """Translate *key* (optionally formatted with ``**fmt``).

    Lookup order: active language catalog -> English ``DEFAULT_STRINGS``
    -> the key itself.  ``str.format`` runs only when keyword arguments
    were passed; a missing argument or a broken ``.format`` in a catalog
    string returns the raw template (warned once per key) instead of
    raising into the UI.
    """
    text = _catalog.get(key) or DEFAULT_STRINGS.get(key) or key
    if fmt:
        try:
            text = text.format(**fmt)
        except (KeyError, IndexError, ValueError) as e:
            if key not in _warned_format_keys:
                _warned_format_keys.add(key)
                logger.warning(
                    "tr(%r) format failed (%s); returning the raw template", key, e
                )
    return text


def trn(key: str, n: int, **fmt) -> str:
    """Translate a plural string by *n* (``<key>.one`` / ``<key>.other``).

    *n* is injected into ``fmt`` as ``{count}`` unless the caller already
    passed it, so templates use the CLDR-style placeholder ``{count}``
    either way.
    """
    fmt.setdefault("count", n)
    plural_key = f"{key}.one" if n == 1 else f"{key}.other"
    return tr(plural_key, **fmt)


def tr_status(status: str) -> str:
    """Display text for a card-status *code*, passing other strings through.

    Known codes (``CARD_STATUS_KEYS``) render via their
    ``card_status.<code>`` catalog row.  Arbitrary flow/plugin status
    messages are not catalog keys and must not be mangled - they pass
    through unchanged (their translation is a plugin-boundary concern).
    """
    if status in CARD_STATUS_KEYS:
        return tr(f"card_status.{status}")
    return status