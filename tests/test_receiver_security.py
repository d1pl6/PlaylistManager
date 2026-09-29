"""URL-receiver Host-header security tests (Stage 2 hardening).

Every extension receiver binds the loopback interface and must refuse
requests whose ``Host`` header is anything but ``127.0.0.1[:port]`` /
``localhost[:port]`` **before** any route code runs, closing the
DNS-rebinding window (werkzeug rejects malformed Hosts but serves a
well-formed header naming an attacker domain that resolves to
127.0.0.1).

The receivers live in the (gitignored) plugin repos; each module is
imported with a skip when the plugin is not present in this checkout,
mirroring test_ytmusic_auth.py.  Only the Flask app is built via the
lazy ``_ensure_app`` - no socket is ever bound, and the per-flow token
machinery is not exercised (that is per-plugin-repo testing).
"""

import pytest


def _receiver_mod(platform, module):
    try:
        mod = __import__(
            f"integrations.{platform}.{module}", fromlist=["*"]
        )
    except ImportError:
        pytest.skip(f"{platform} plugin not installed in this checkout")
    return mod


@pytest.fixture(params=[
    ("youtube_music", "youtube_music_receiver", "URLReceiverManager", "/status", "/receive-url"),
    ("soundcloud", "soundcloud_receiver", "SoundCloudURLReceiverManager", "/status", "/receive-url"),
    ("deezer", "deezer_receiver", "DeezerReceiverManager", "/status", "/receive-track"),
    ("tidal", "tidal_receiver", "TidalReceiverManager", "/status", "/receive-track"),
])
def receiver(request):
    platform, module, cls_name, status_path, post_path = request.param
    mod = _receiver_mod(platform, module)
    manager = getattr(mod, cls_name)()
    manager._ensure_app()
    # Youtube/Deezer store the app as ``self.app``, SoundCloud as
    # ``self._app`` - resolve whichever the module uses.
    app = getattr(manager, "app", None) or manager._app
    client = app.test_client()
    return {
        "platform": platform,
        "client": client,
        "status_path": status_path,
        "post_path": post_path,
    }


class TestLoopbackHostAllowlist:
    def test_loopback_host_allowed(self, receiver):
        resp = receiver["client"].get(
            receiver["status_path"], headers={"Host": "127.0.0.1:5000"}
        )
        assert resp.status_code == 200

    def test_localhost_host_allowed(self, receiver):
        resp = receiver["client"].get(
            receiver["status_path"], headers={"Host": "localhost:5000"}
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("evil", [
        "evil.example.com",
        "127.0.0.1.evil.example.com",
        "127.0.0.1:5000.evil.example.com",
        "evil.example.com:5000",
        "[::1]:5000",  # loopback, but not the interface we bind
        "",
    ])
    def test_foreign_host_rejected(self, receiver, evil):
        resp = receiver["client"].get(
            receiver["status_path"], headers={"Host": evil}
        )
        assert resp.status_code == 403

    def test_foreign_host_rejected_on_post_too(self, receiver):
        """The host guard runs before route code, so a POST carrying the
        right token from a foreign Host is still refused."""
        resp = receiver["client"].post(
            receiver["post_path"],
            json={"url": "https://music.youtube.com/watch?v=abcdefghijk"},
            headers={
                "Host": "evil.example.com",
                "X-PM-Token": "forged",
            },
        )
        assert resp.status_code == 403