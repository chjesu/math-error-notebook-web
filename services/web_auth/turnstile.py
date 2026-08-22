"""Cloudflare Turnstile server-side CAPTCHA verification adapter."""

from __future__ import annotations

import json
import os
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
Transport = Callable[[bytes, float], bytes]


def _post(body: bytes, timeout: float) -> bytes:
    request = Request(
        SITEVERIFY_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS URL.
        payload = response.read(65_537)
        if response.status != 200 or len(payload) > 65_536:
            raise ValueError("invalid Turnstile response")
        return payload


class TurnstileCaptchaVerifier:
    """Fail closed unless Siteverify confirms token, hostname and action."""

    def __init__(
        self,
        *,
        secret: str,
        allowed_hostnames: set[str],
        expected_action: str = "otp_request",
        transport: Transport = _post,
    ) -> None:
        if not secret or not allowed_hostnames or not expected_action:
            raise ValueError("Turnstile secret, hostnames and action are required")
        self._secret = secret
        self._hostnames = {item.lower() for item in allowed_hostnames}
        self._action = expected_action
        self._transport = transport

    @classmethod
    def from_environment(cls) -> "TurnstileCaptchaVerifier":
        hostnames = {
            item.strip().lower()
            for item in os.environ["LZLM_ALLOWED_HOSTS"].split(",")
            if item.strip()
        }
        return cls(
            secret=os.environ["TURNSTILE_SECRET_KEY"],
            allowed_hostnames=hostnames,
            expected_action=os.environ.get("TURNSTILE_EXPECTED_ACTION", "otp_request"),
        )

    def verify(self, token: str, *, ip_hash: str, phone_hash: str) -> bool:
        del ip_hash, phone_hash
        if not token or len(token) > 2048:
            return False
        body = urlencode({"secret": self._secret, "response": token}).encode("ascii")
        try:
            payload = self._transport(body, 5.0)
            result = json.loads(payload.decode("utf-8"))
        except (OSError, TimeoutError, UnicodeError, ValueError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(result, dict)
            and result.get("success") is True
            and str(result.get("hostname", "")).lower() in self._hostnames
            and result.get("action") == self._action
        )
