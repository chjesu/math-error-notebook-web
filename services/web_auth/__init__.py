"""Security-critical domain services for the future multi-user Web edition."""

from .registration import (
    AuthConfig,
    InMemoryCaptchaVerifier,
    InMemoryGuardianConsentVerifier,
    InMemoryRegistrationStore,
    RecordingSmsSender,
    RegistrationResult,
    RegistrationService,
    SendCodeResult,
    normalize_cn_mobile,
)
from .asgi import AuthAsgiApp
from .mysql_store import MySqlRegistrationStore
from .ruicheng_sms import RuichengSmsSender, SmsProviderError
from .turnstile import TurnstileCaptchaVerifier

__all__ = [
    "AuthConfig",
    "AuthAsgiApp",
    "InMemoryCaptchaVerifier",
    "InMemoryGuardianConsentVerifier",
    "InMemoryRegistrationStore",
    "MySqlRegistrationStore",
    "RecordingSmsSender",
    "RegistrationResult",
    "RegistrationService",
    "RuichengSmsSender",
    "SendCodeResult",
    "SmsProviderError",
    "TurnstileCaptchaVerifier",
    "normalize_cn_mobile",
]
