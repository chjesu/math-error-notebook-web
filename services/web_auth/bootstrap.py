"""Production dependency factory for the phone-registration ASGI app."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from .asgi import AuthAsgiApp
from .mysql_store import MySqlRegistrationStore
from .registration import RegistrationService
from .ruicheng_sms import RuichengSmsSender
from .turnstile import TurnstileCaptchaVerifier


class RejectingGuardianConsentVerifier:
    """Fail closed until the separate guardian-consent flow issues receipts."""

    def verify(self, receipt: str, *, student_phone_hash: str, birth_date: Any) -> bool:
        del receipt, student_phone_hash, birth_date
        return False


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _pepper() -> bytes:
    try:
        value = base64.b64decode(_required("LZLM_AUTH_PEPPER_B64"), validate=True)
    except ValueError as exc:
        raise RuntimeError("LZLM_AUTH_PEPPER_B64 must be valid base64") from exc
    if len(value) < 32:
        raise RuntimeError("LZLM_AUTH_PEPPER_B64 must decode to at least 32 bytes")
    return value


def _allowed_hosts() -> set[str]:
    values = {item.strip().lower() for item in _required("LZLM_ALLOWED_HOSTS").split(",")}
    values.discard("")
    if not values:
        raise RuntimeError("LZLM_ALLOWED_HOSTS must contain at least one hostname")
    return values


def _mysql_connection_factory():
    host = _required("LZLM_MYSQL_HOST")
    ssl_ca = os.environ.get("LZLM_MYSQL_SSL_CA", "").strip()
    if not ssl_ca and host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("LZLM_MYSQL_SSL_CA is required for a remote MySQL server")
    if ssl_ca and not Path(ssl_ca).is_file():
        raise RuntimeError("LZLM_MYSQL_SSL_CA does not name a readable file")
    settings: dict[str, Any] = {
        "host": host,
        "port": int(os.environ.get("LZLM_MYSQL_PORT", "3306")),
        "user": _required("LZLM_MYSQL_USER"),
        "password": _required("LZLM_MYSQL_PASSWORD"),
        "database": _required("LZLM_MYSQL_DATABASE"),
        "charset": "utf8mb4",
        "autocommit": False,
        "connect_timeout": 5,
        "read_timeout": 5,
        "write_timeout": 5,
    }
    if ssl_ca:
        settings["ssl"] = {"ca": ssl_ca, "check_hostname": True}

    import pymysql

    def connect():
        return pymysql.connect(**settings)

    return connect


def create_app() -> AuthAsgiApp:
    """Build the app for ``uvicorn --factory``; raises on unsafe config."""

    hosts = _allowed_hosts()
    service = RegistrationService(
        store=MySqlRegistrationStore(_mysql_connection_factory()),
        sms_sender=RuichengSmsSender.from_environment(),
        captcha_verifier=TurnstileCaptchaVerifier.from_environment(),
        guardian_consent_verifier=RejectingGuardianConsentVerifier(),
        secret_pepper=_pepper(),
    )
    return AuthAsgiApp(service, allowed_hosts=hosts)
