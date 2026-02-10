"""Skill: http_get_json — Fetch JSON from an HTTP endpoint with host allowlisting."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from kavi.skills.base import BaseSkill, SkillInput, SkillOutput


class HttpGetJsonInput(SkillInput):
    """Input for http_get_json skill."""

    url: str
    api_key_env: str | None = None
    headers: dict[str, str] | None = None
    timeout_s: float = 5.0
    max_bytes: int = 200_000
    allowed_hosts: list[str]


class HttpGetJsonOutput(SkillOutput):
    """Output for http_get_json skill."""

    url: str
    status_code: int = 0
    json: dict[str, Any] | None = None
    truncated: bool = False
    used_secret: bool = False
    error: str | None = None


class HttpGetJsonSkill(BaseSkill):
    """Fetch JSON from an HTTP endpoint with host allowlisting and optional API key."""

    name = "http_get_json"
    description = (
        "Fetch JSON from an HTTP endpoint with host allowlisting "
        "and optional API key from environment variable"
    )
    input_model = HttpGetJsonInput
    output_model = HttpGetJsonOutput
    side_effect_class = "NETWORK"

    def execute(self, input_data: HttpGetJsonInput) -> HttpGetJsonOutput:  # type: ignore[override]
        url = input_data.url

        # Validate URL scheme
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return HttpGetJsonOutput(
                url=url,
                error=f"Unsupported scheme: {parsed.scheme!r}",
            )

        # Validate host against allowlist
        host = parsed.hostname or ""
        if not host:
            return HttpGetJsonOutput(url=url, error="No hostname in URL")

        if host not in input_data.allowed_hosts:
            return HttpGetJsonOutput(
                url=url,
                error=f"Host {host!r} not in allowed_hosts",
            )

        # Build request headers
        req_headers: dict[str, str] = {}
        if input_data.headers:
            req_headers.update(input_data.headers)

        # Resolve API key from environment variable
        used_secret = False
        if input_data.api_key_env:
            api_key = os.environ.get(input_data.api_key_env)
            if api_key is None:
                return HttpGetJsonOutput(
                    url=url,
                    error=(
                        f"Environment variable "
                        f"{input_data.api_key_env!r} not set"
                    ),
                )
            req_headers["Authorization"] = f"Bearer {api_key}"
            used_secret = True

        # Perform HTTP GET via urllib
        req = urllib.request.Request(url, headers=req_headers, method="GET")
        try:
            with urllib.request.urlopen(
                req, timeout=input_data.timeout_s
            ) as resp:
                status_code = resp.status
                raw_body = resp.read(input_data.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            # HTTPError carries the response — read its body
            status_code = exc.code
            raw_body = exc.read(input_data.max_bytes + 1)
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "timed out" in reason.lower():
                return HttpGetJsonOutput(
                    url=url,
                    used_secret=used_secret,
                    error="Request timed out",
                )
            return HttpGetJsonOutput(
                url=url,
                used_secret=used_secret,
                error=f"URL error: {reason}",
            )
        except TimeoutError:
            return HttpGetJsonOutput(
                url=url,
                used_secret=used_secret,
                error="Request timed out",
            )

        # Check truncation
        truncated = len(raw_body) > input_data.max_bytes
        if truncated:
            raw_body = raw_body[: input_data.max_bytes]

        # Parse JSON
        try:
            data = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return HttpGetJsonOutput(
                url=url,
                status_code=status_code,
                truncated=truncated,
                used_secret=used_secret,
                error=f"Invalid JSON: {exc}",
            )

        if not isinstance(data, dict):
            return HttpGetJsonOutput(
                url=url,
                status_code=status_code,
                truncated=truncated,
                used_secret=used_secret,
                error=f"Expected JSON object, got {type(data).__name__}",
            )

        return HttpGetJsonOutput(
            url=url,
            status_code=status_code,
            json=data,
            truncated=truncated,
            used_secret=used_secret,
        )
