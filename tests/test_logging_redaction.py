"""Redaction tests for :mod:`utils.logging_config`.

Guarantees that ``Authorization`` / ``Cookie`` / token values can never
reach log output - not even under ``--trace`` with third-party header
dumps - and that :func:`register_secret` catches bare (unlabelled)
secrets such as exception echoes.  Pure ``redact()`` tests first, then
the handler-level filter and the ``configure_logging`` wiring.
"""

import logging

from utils.logging_config import (
    MASK_STRING,
    _RedactionFilter,
    configure_logging,
    redact,
    register_secret,
)


class TestRedactPatterns:
    """The label-driven masking layer (no registration needed)."""

    def test_authorization_header_line(self):
        token = "eyJhbGciOiJIUzI1NiJ9.abc.def"
        out = redact(f"Authorization: Bearer {token} GET /me")
        assert token not in out
        assert MASK_STRING in out

    def test_cookie_json_whole_value_masked(self):
        out = redact('Incoming: "Cookie": "arl=SecretArlVal; sid=SessKey12"')
        assert "SecretArlVal" not in out
        assert "SessKey12" not in out
        assert MASK_STRING in out

    def test_credential_keys_in_json(self):
        out = redact(
            '{"client_secret": "SecretClientVal", '
            '"refresh_token": "RefreshTokVal1", "arl": "ArlValue12345"}'
        )
        for secret in ("SecretClientVal", "RefreshTokVal1", "ArlValue12345"):
            assert secret not in out
        assert out.count(MASK_STRING) == 3

    def test_url_query_client_id_masked_tail_kept(self):
        out = redact("https://api.soundcloud.com/me?client_id=ScId123456&limit=5")
        assert "ScId123456" not in out
        assert "limit=5" in out

    def test_bare_bearer_with_padding(self):
        out = redact("got bearer AbcDefGhIjKl== back")
        assert "AbcDefGhIjKl==" not in out
        assert MASK_STRING in out

    def test_set_cookie_value_masked(self):
        out = redact('resp: "set-cookie": "sid=XyzCookie99"')
        assert "XyzCookie99" not in out

    def test_youtube_headers_json(self):
        out = redact(
            '{"Authorization": "SAPISIDHASH AbCdEf12345", '
            '"Cookie": "SAPISID=AbCdEf123456", "X-Goog-AuthUser": "0"}'
        )
        assert "SAPISIDHASH" not in out
        assert "SAPISID=AbCdEf123456" not in out
        # Non-credential keys survive.
        assert "X-Goog-AuthUser" in out

    def test_innocent_text_untouched(self):
        for text in (
            "authorization",
            "JWT in response",
            "consideration",
            "the cookie jar is empty",
            "https://example.com/consideration",
            "limit=5&client_id missing",
        ):
            assert redact(text) == text

    def test_empty_text(self):
        assert redact("") == ""
        assert redact(None or "") == ""


class TestRegisterSecret:
    """The value-registry layer: bare secrets get masked too."""

    def test_registered_value_masked_bare(self):
        register_secret("SuperSecretValue12345")
        out = redact("token received: SuperSecretValue12345 ok")
        assert "SuperSecretValue12345" not in out
        assert MASK_STRING in out

    def test_short_values_ignored(self):
        register_secret("abcd")
        assert redact("abcd") == "abcd"

    def test_non_string_values_ignored(self):
        register_secret(None, 42, b"bytes12345")
        assert redact("42") == "42"

    def test_whitespace_stripped_before_registering(self):
        register_secret("  PaddedSecretValue123  ")
        out = redact("PaddedSecretValue123 here")
        assert "PaddedSecretValue123" not in out


class TestRedactionFilter:
    """The handler-level filter that rewrites records."""

    def _record(self, msg, args=None):
        return logging.LogRecord(
            "pm.test", logging.ERROR, "module.py", 1, msg, args, None
        )

    def test_formats_and_masks_record_with_args(self):
        filt = _RedactionFilter()
        record = self._record(
            "Authorization: Bearer %s", ("Tok123456789Tok",)
        )
        assert filt.filter(record) is True
        assert "Tok123456789Tok" not in record.getMessage()
        assert MASK_STRING in record.getMessage()
        # The record is fully formatted and args cleared so no later
        # stage can re-introduce the raw value.
        assert record.args == ()

    def test_benign_record_left_untouched(self):
        filt = _RedactionFilter()
        record = self._record("playlist %s added", ("Chill Mix",))
        assert filt.filter(record) is True
        assert record.args == ("Chill Mix",)
        assert record.getMessage() == "playlist Chill Mix added"

    def test_second_pass_is_noop(self):
        filt = _RedactionFilter()
        record = self._record(
            "Cookie: arl=%s", ("Tok123456789Tok",)
        )
        assert filt.filter(record) is True
        first = record.getMessage()
        assert "Tok123456789Tok" not in first
        assert filt.filter(record) is True
        assert record.getMessage() == first


class TestConfigureLoggingWiring:
    """configure_logging attaches the filter and the output is masked."""

    def test_filter_attached_to_root_handlers(self):
        configure_logging(1)
        root = logging.getLogger()
        assert root.handlers
        for handler in root.handlers:
            assert any(
                isinstance(f, _RedactionFilter) for f in handler.filters
            ), "root handler missing the redaction filter"

    def test_emitted_output_is_masked(self, capsys):
        configure_logging(3)  # --trace: everything, incl. third-party
        logging.getLogger("pm.redaction.test").info(
            'Session: "Cookie": "arl=SuperSecretCookieVal"'
        )
        err = capsys.readouterr().err
        assert "SuperSecretCookieVal" not in err
        assert MASK_STRING in err