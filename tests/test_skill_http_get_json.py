"""Tests for http_get_json skill — all mocked, no real network."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from kavi.skills.http_get_json import (
    HttpGetJsonInput,
    HttpGetJsonOutput,
    HttpGetJsonSkill,
)

# ---- helpers ----

def _mock_response(body: bytes, status: int = 200) -> MagicMock:
    """Create a mock that behaves like urllib.request.urlopen() context."""
    resp = MagicMock()
    resp.status = status
    resp.read = MagicMock(return_value=body)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---- model tests ----

class TestHttpGetJsonModels:

    def test_valid_input_minimal(self):
        inp = HttpGetJsonInput(
            url="https://api.example.com/data",
            allowed_hosts=["api.example.com"],
        )
        assert inp.url == "https://api.example.com/data"
        assert inp.api_key_env is None
        assert inp.headers is None
        assert inp.timeout_s == 5.0
        assert inp.max_bytes == 200_000

    def test_valid_input_full(self):
        inp = HttpGetJsonInput(
            url="https://api.example.com/data",
            api_key_env="MY_API_KEY",
            headers={"Accept": "application/json"},
            timeout_s=10.0,
            max_bytes=100_000,
            allowed_hosts=["api.example.com"],
        )
        assert inp.api_key_env == "MY_API_KEY"
        assert inp.timeout_s == 10.0

    def test_missing_required_fields(self):
        with pytest.raises(Exception):
            HttpGetJsonInput(url="https://example.com")  # type: ignore[call-arg]

    def test_output_model_defaults(self):
        out = HttpGetJsonOutput(url="https://example.com")
        assert out.status_code == 0
        assert out.json is None
        assert out.truncated is False
        assert out.used_secret is False
        assert out.error is None


# ---- skill execution tests ----

class TestHttpGetJsonSkill:

    def test_attributes(self):
        skill = HttpGetJsonSkill()
        assert skill.name == "http_get_json"
        assert skill.input_model is HttpGetJsonInput
        assert skill.output_model is HttpGetJsonOutput
        assert skill.side_effect_class == "NETWORK"

    def test_rejects_disallowed_host(self):
        skill = HttpGetJsonSkill()
        result = skill.execute(HttpGetJsonInput(
            url="https://evil.com/data",
            allowed_hosts=["api.example.com"],
        ))
        assert result.error is not None
        assert "evil.com" in result.error
        assert "not in allowed_hosts" in result.error

    def test_rejects_unsupported_scheme(self):
        skill = HttpGetJsonSkill()
        result = skill.execute(HttpGetJsonInput(
            url="ftp://api.example.com/file",
            allowed_hosts=["api.example.com"],
        ))
        assert result.error is not None
        assert "Unsupported scheme" in result.error

    def test_rejects_no_hostname(self):
        skill = HttpGetJsonSkill()
        result = skill.execute(HttpGetJsonInput(
            url="http:///path",
            allowed_hosts=["example.com"],
        ))
        assert result.error is not None
        assert "No hostname" in result.error

    def test_successful_get(self):
        skill = HttpGetJsonSkill()
        body = json.dumps({"status": "ok", "count": 42}).encode()
        mock_resp = _mock_response(body, 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/data",
                allowed_hosts=["api.example.com"],
            ))

        assert result.status_code == 200
        assert result.json == {"status": "ok", "count": 42}
        assert result.truncated is False
        assert result.used_secret is False
        assert result.error is None

    def test_api_key_from_env(self, monkeypatch: pytest.MonkeyPatch):
        skill = HttpGetJsonSkill()
        monkeypatch.setenv("TEST_API_KEY", "secret-token-123")
        body = json.dumps({"authed": True}).encode()
        mock_resp = _mock_response(body, 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_urlopen:
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/data",
                api_key_env="TEST_API_KEY",
                allowed_hosts=["api.example.com"],
            ))

        assert result.used_secret is True
        assert result.json == {"authed": True}
        # Verify Authorization header was set
        req_arg = mock_urlopen.call_args[0][0]
        assert req_arg.get_header("Authorization") == "Bearer secret-token-123"

    def test_missing_env_var(self):
        skill = HttpGetJsonSkill()
        result = skill.execute(HttpGetJsonInput(
            url="https://api.example.com/data",
            api_key_env="NONEXISTENT_KEY_12345",
            allowed_hosts=["api.example.com"],
        ))
        assert result.error is not None
        assert "NONEXISTENT_KEY_12345" in result.error
        assert "not set" in result.error

    def test_custom_headers_passed(self):
        skill = HttpGetJsonSkill()
        body = json.dumps({"ok": True}).encode()
        mock_resp = _mock_response(body, 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ) as mock_urlopen:
            skill.execute(HttpGetJsonInput(
                url="https://api.example.com/data",
                headers={"X-Custom": "value"},
                allowed_hosts=["api.example.com"],
            ))

        req_arg = mock_urlopen.call_args[0][0]
        assert req_arg.get_header("X-custom") == "value"

    def test_timeout_error(self):
        skill = HttpGetJsonSkill()

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            side_effect=urllib.error.URLError(
                reason=TimeoutError("timed out")
            ),
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/slow",
                allowed_hosts=["api.example.com"],
                timeout_s=1.0,
            ))

        assert result.error == "Request timed out"
        assert result.status_code == 0

    def test_connection_error(self):
        skill = HttpGetJsonSkill()

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            side_effect=urllib.error.URLError(
                reason="Connection refused"
            ),
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/down",
                allowed_hosts=["api.example.com"],
            ))

        assert result.error is not None
        assert "URL error" in result.error

    def test_truncation(self):
        skill = HttpGetJsonSkill()
        # Create a response larger than max_bytes
        large_value = "x" * 500
        body = json.dumps({"data": large_value}).encode()
        mock_resp = _mock_response(body, 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/big",
                allowed_hosts=["api.example.com"],
                max_bytes=100,
            ))

        assert result.truncated is True
        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_non_dict_json_rejected(self):
        skill = HttpGetJsonSkill()
        body = json.dumps([1, 2, 3]).encode()
        mock_resp = _mock_response(body, 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/list",
                allowed_hosts=["api.example.com"],
            ))

        assert result.error is not None
        assert "Expected JSON object" in result.error
        assert result.status_code == 200

    def test_invalid_json_body(self):
        skill = HttpGetJsonSkill()
        mock_resp = _mock_response(b"not json at all", 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/bad",
                allowed_hosts=["api.example.com"],
            ))

        assert result.error is not None
        assert "Invalid JSON" in result.error
        assert result.status_code == 200

    def test_http_error_status_still_parses(self):
        skill = HttpGetJsonSkill()
        body = json.dumps({"error": "not found"}).encode()
        # HTTPError with a readable body
        exc = urllib.error.HTTPError(
            url="https://api.example.com/missing",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            side_effect=exc,
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://api.example.com/missing",
                allowed_hosts=["api.example.com"],
            ))

        assert result.status_code == 404
        assert result.json == {"error": "not found"}
        assert result.error is None

    def test_validate_and_run(self):
        skill = HttpGetJsonSkill()
        body = json.dumps({"ok": True}).encode()
        mock_resp = _mock_response(body, 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            result = skill.validate_and_run({
                "url": "https://api.example.com/data",
                "allowed_hosts": ["api.example.com"],
            })

        assert result["status_code"] == 200
        assert result["json"] == {"ok": True}

    def test_multiple_allowed_hosts(self):
        skill = HttpGetJsonSkill()
        body = json.dumps({"source": "backup"}).encode()
        mock_resp = _mock_response(body, 200)

        with patch(
            "kavi.skills.http_get_json.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            result = skill.execute(HttpGetJsonInput(
                url="https://backup.example.com/data",
                allowed_hosts=["api.example.com", "backup.example.com"],
            ))

        assert result.json == {"source": "backup"}
        assert result.error is None
