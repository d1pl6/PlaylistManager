import json
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

playlists_json = Path(__file__).resolve().parents[2] / "db" / "playlists.json"


class PlaylistStore:
    @staticmethod
    def load_playlists():
        if os.path.exists(playlists_json) and os.path.getsize(playlists_json) > 0:
            try:
                with open(playlists_json, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to read playlists.json: {e}")
        return []

    @staticmethod
    def get_existing_names(platform: str = ""):
        playlists = PlaylistStore.load_playlists()
        if platform:
            return {
                p.get("name")
                for p in playlists
                if p.get("platform", "youtube_music") == platform
            }
        return {p.get("name") for p in playlists}

    @staticmethod
    def find_playlist(name: str, platform: str = ""):
        playlists = PlaylistStore.load_playlists()
        for p in playlists:
            if p.get("name") == name:
                stored_platform = p.get("platform")
                if not platform or stored_platform == platform:
                    return p
        return None

    @staticmethod
    def add_playlist(
        name: str,
        platform: str = "youtube_music",
        playlist_id: str = "",
        thumbnail_url: str = "",
    ):
        playlists = PlaylistStore.load_playlists()
        playlists.append(
            {
                "name": name,
                "platform": platform,
                "hotkey": "",
                "playlist_id": playlist_id,
                "thumbnail_url": thumbnail_url,
            }
        )
        PlaylistStore._write(playlists)

    @staticmethod
    def update_thumbnail(name: str, platform: str, thumbnail_url: str):
        playlists = PlaylistStore.load_playlists()
        for p in playlists:
            if p.get("name") == name and p.get("platform") == platform:
                p["thumbnail_url"] = thumbnail_url
                break
        PlaylistStore._write(playlists)

    @staticmethod
    def update_keybind(name: str, platform: str, hotkey: str):
        playlists = PlaylistStore.load_playlists()
        for p in playlists:
            if p.get("name") == name and p.get("platform") == platform:
                p["hotkey"] = hotkey
                break
        PlaylistStore._write(playlists)

    @staticmethod
    def delete_playlist(name: str, platform: str = ""):
        playlists = PlaylistStore.load_playlists()
        if platform:
            playlists = [
                p
                for p in playlists
                if not (
                    p.get("name") == name
                    and p.get("platform", "youtube_music") == platform
                )
            ]
        else:
            playlists = [p for p in playlists if p.get("name") != name]
        PlaylistStore._write(playlists)

    @staticmethod
    def _write(playlists):
        try:
            with open(playlists_json, "w", encoding="utf-8") as f:
                json.dump(playlists, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to write playlists.json: {e}")
