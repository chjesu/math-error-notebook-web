"""Phone-code registration domain service with abuse and privacy controls.

The module deliberately has no web-framework or database dependency.  HTTP and
MySQL adapters can be added around these interfaces without putting provider
keys, plaintext OTPs, or database credentials in an agent/model process.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
from typing import Protocol
import uuid


CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")
PURPOSES = {"login", "register", "sensitive_export", "sensitive_delete"}
CURRENT_PROTOCOL_VERSION = "2026-08-23"
PASSWORD_PEPPER_VERSION = "v1"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_cn_mobile(value: str) -> str:
    """Return the national 11-digit form or raise ValueError."""

    digits = re.sub(r"[\s()-]", "", value.strip())
    if digits.startswith("+86"):
        digits = digits[3:]
    elif digits.startswith("0086"):
        digits = digits[4:]
    elif len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    if not CN_MOBILE.fullmatch(digits):
        raise ValueError("invalid mainland China mobile number")
    return digits


def _normalize_ip(value: str) -> str:
    return ipaddress.ip_address(value.strip()).compressed


def _normalize_ip_prefix(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    prefix = 24 if address.version == 4 else 56
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


class SendCodeStatus(str, Enum):
    ACCEPTED = "accepted"
    CAPTCHA_REQUIRED = "captcha_required"
    RETRY_LATER = "retry_later"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    PHONE_NOT_REGISTERED = "phone_not_registered"
    PHONE_ALREADY_REGISTERED = "phone_already_registered"
    LOCKED = "locked"


class RegistrationStatus(str, Enum):
    COMPLETE = "complete"
    INVALID_CODE = "invalid_code"
    EXPIRED = "expired"
    LOCKED = "locked"
    WEAK_PASSWORD = "weak_password"
    AGREEMENT_REQUIRED = "agreement_required"


@dataclass(frozen=True)
class AuthConfig:
    code_ttl_seconds: int = 300
    resend_cooldown_seconds: int = 60
    max_code_attempts: int = 5
    phone_hour_limit: int = 5
    phone_day_limit: int = 10
    ip_minute_limit: int = 10
    ip_hour_limit: int = 20
    ip_prefix_hour_limit: int = 30
    device_hour_limit: int = 10
    device_day_limit: int = 20
    tenant_hour_limit: int = 300
    global_day_limit: int = 10_000
    captcha_after_phone_day: int = 3
    captcha_after_ip_hour: int = 10
    session_ttl_days: int = 30
    scrypt_n: int = 2**14
    scrypt_r: int = 8
    scrypt_p: int = 1
    protocol_version: str = CURRENT_PROTOCOL_VERSION
    max_protocol_version_length: int = 32

    def __post_init__(self) -> None:
        if self.protocol_version != CURRENT_PROTOCOL_VERSION:
            raise ValueError(f"protocol_version must be {CURRENT_PROTOCOL_VERSION}")
        if self.max_protocol_version_length < len(CURRENT_PROTOCOL_VERSION):
            raise ValueError("max_protocol_version_length is too small")


@dataclass(frozen=True)
class SendCodeResult:
    status: SendCodeStatus
    message: str
    challenge_id: str | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True)
class RegistrationResult:
    status: RegistrationStatus
    message: str
    user_id: str | None = None
    session_token: str | None = None
    account_status: str | None = None


@dataclass
class Challenge:
    challenge_id: str
    phone_hash: str
    tenant_hash: str
    purpose: str
    code_hash: str
    expires_at: datetime
    created_at: datetime
    status: str = "pending"
    attempts: int = 0
    provider_receipt: str | None = None


@dataclass(frozen=True)
class User:
    user_id: str
    phone_hash: str
    phone_last4: str
    created_at: datetime
    status: str = "active"


@dataclass(frozen=True)
class Session:
    session_hash: str
    user_id: str
    expires_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    event: str
    occurred_at: datetime
    phone_masked: str
    phone_hash: str
    ip_hash: str
    ip_prefix_hash: str
    device_hash: str
    tenant_hash: str
    outcome: str
    metadata: dict[str, str | int | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestSubjects:
    phone_hash: str
    ip_hash: str
    ip_prefix_hash: str
    device_hash: str
    tenant_hash: str


class SmsSender(Protocol):
    def send_verification(self, phone: str, code: str, ttl_seconds: int) -> str: ...


class CaptchaVerifier(Protocol):
    def verify(self, token: str, *, ip_hash: str, phone_hash: str) -> bool: ...


class RegistrationStore(Protocol):
    """Persistence contract; production implements every mutation transactionally."""

    def count(self, dimension: str, subject_hash: str, since: datetime) -> int: ...

    def find_user(self, phone_hash: str) -> User | None: ...

    def reserve_send(
        self,
        *,
        phone_hash: str,
        cooldown_hash: str,
        ip_hash: str,
        ip_prefix_hash: str,
        device_hash: str,
        tenant_hash: str,
        now: datetime,
        config: AuthConfig,
    ) -> tuple[bool, int]: ...

    def add_challenge(self, challenge: Challenge) -> None: ...

    def mark_delivery(self, challenge_id: str, status: str, receipt: str | None) -> None: ...

    def prevalidate_code(
        self,
        *,
        challenge_id: str,
        purpose: str,
        phone_hash: str,
        tenant_hash: str,
        code_hash: str,
        now: datetime,
        max_attempts: int,
    ) -> RegistrationStatus: ...

    def complete(
        self,
        *,
        challenge_id: str,
        purpose: str,
        phone_hash: str,
        tenant_hash: str,
        phone_last4: str,
        code_hash: str,
        session_hash: str | None,
        session_expires_at: datetime | None,
        password_salt: bytes | None,
        password_hash: bytes | None,
        password_params: str | None,
        agreement_version: str | None,
        now: datetime,
        max_attempts: int,
    ) -> tuple[RegistrationStatus, User | None]: ...

    def get_session_user(self, session_hash: str, now: datetime) -> User | None: ...

    def revoke_session(self, session_hash: str, now: datetime) -> bool: ...

    def revoke_user_sessions(self, user_id: str, now: datetime) -> int: ...

    def deactivate_user(self, user_id: str, now: datetime) -> bool: ...

    def audit(self, event: AuditEvent) -> None: ...


class InMemoryCaptchaVerifier:
    """Test adapter. Production must validate a provider-signed, one-use token."""

    def __init__(self, accepted_tokens: set[str] | None = None) -> None:
        self._accepted = set(accepted_tokens or set())

    def verify(self, token: str, *, ip_hash: str, phone_hash: str) -> bool:
        del ip_hash, phone_hash
        if token not in self._accepted:
            return False
        self._accepted.remove(token)
        return True


class RecordingSmsSender:
    """Test adapter that records deliveries; never use as a production provider."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.deliveries: list[tuple[str, str, int]] = []

    def send_verification(self, phone: str, code: str, ttl_seconds: int) -> str:
        if self.fail:
            raise RuntimeError("sms provider unavailable")
        self.deliveries.append((phone, code, ttl_seconds))
        return f"test-{len(self.deliveries)}"


class InMemoryRegistrationStore:
    """Atomic reference adapter used by tests and local development only."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.challenges: dict[str, Challenge] = {}
        self.users_by_phone: dict[str, User] = {}
        self.sessions: dict[str, Session] = {}
        self.password_credentials: dict[str, tuple[bytes, bytes, str]] = {}
        self.agreements: dict[str, tuple[str, datetime]] = {}
        self.audit_events: list[AuditEvent] = []
        self._send_times: dict[tuple[str, str], list[datetime]] = defaultdict(list)

    def count(self, dimension: str, subject_hash: str, since: datetime) -> int:
        with self._lock:
            values = self._send_times[(dimension, subject_hash)]
            return sum(item >= since for item in values)

    def find_user(self, phone_hash: str) -> User | None:
        with self._lock:
            return self.users_by_phone.get(phone_hash)

    def reserve_send(
        self,
        *,
        phone_hash: str,
        cooldown_hash: str,
        ip_hash: str,
        ip_prefix_hash: str,
        device_hash: str,
        tenant_hash: str,
        now: datetime,
        config: AuthConfig,
    ) -> tuple[bool, int]:
        """Atomically check limits and reserve one provider attempt."""

        with self._lock:
            minute = now - timedelta(minutes=1)
            hour = now - timedelta(hours=1)
            day = now - timedelta(days=1)
            phone_times = self._send_times[("phone", phone_hash)]
            recent_phone = [item for item in phone_times if item >= day]
            self._send_times[("phone", phone_hash)] = recent_phone
            cooldown_times = self._send_times[("cooldown", cooldown_hash)]
            recent_cooldowns = [item for item in cooldown_times if item >= day]
            self._send_times[("cooldown", cooldown_hash)] = recent_cooldowns
            if recent_cooldowns:
                elapsed = int((now - recent_cooldowns[-1]).total_seconds())
                if elapsed < config.resend_cooldown_seconds:
                    return False, config.resend_cooldown_seconds - elapsed
            limits = (
                ("phone", phone_hash, hour, config.phone_hour_limit),
                ("phone", phone_hash, day, config.phone_day_limit),
                ("ip", ip_hash, minute, config.ip_minute_limit),
                ("ip", ip_hash, hour, config.ip_hour_limit),
                ("ip_prefix", ip_prefix_hash, hour, config.ip_prefix_hour_limit),
                ("device", device_hash, hour, config.device_hour_limit),
                ("device", device_hash, day, config.device_day_limit),
                ("tenant", tenant_hash, hour, config.tenant_hour_limit),
                ("global", "all", day, config.global_day_limit),
            )
            for dimension, subject, since, maximum in limits:
                if self.count(dimension, subject, since) >= maximum:
                    return False, config.resend_cooldown_seconds
            for dimension, subject in (
                ("phone", phone_hash),
                ("ip", ip_hash),
                ("ip_prefix", ip_prefix_hash),
                ("device", device_hash),
                ("tenant", tenant_hash),
                ("global", "all"),
                ("cooldown", cooldown_hash),
            ):
                self._send_times[(dimension, subject)].append(now)
            return True, config.resend_cooldown_seconds

    def add_challenge(self, challenge: Challenge) -> None:
        with self._lock:
            for current in self.challenges.values():
                if (
                    current.phone_hash == challenge.phone_hash
                    and current.purpose == challenge.purpose
                    and current.status in {"pending", "sent"}
                ):
                    current.status = "cancelled"
            self.challenges[challenge.challenge_id] = challenge

    def mark_delivery(self, challenge_id: str, status: str, receipt: str | None) -> None:
        if status not in {"sent", "delivery_failed"}:
            raise ValueError("invalid delivery status")
        with self._lock:
            challenge = self.challenges[challenge_id]
            if challenge.status != "pending":
                raise ValueError("challenge is not pending")
            challenge.status = status
            challenge.provider_receipt = receipt

    def prevalidate_code(
        self,
        *,
        challenge_id: str,
        purpose: str,
        phone_hash: str,
        tenant_hash: str,
        code_hash: str,
        now: datetime,
        max_attempts: int,
    ) -> RegistrationStatus:
        """Cheaply reject bad registration codes before password hashing."""

        with self._lock:
            challenge = self.challenges.get(challenge_id)
            if (
                challenge is None
                or challenge.phone_hash != phone_hash
                or challenge.tenant_hash != tenant_hash
                or challenge.purpose != purpose
            ):
                return RegistrationStatus.INVALID_CODE
            if challenge.status == "locked" or challenge.attempts >= max_attempts:
                return RegistrationStatus.LOCKED
            if challenge.status != "sent":
                return RegistrationStatus.INVALID_CODE
            if now >= challenge.expires_at:
                challenge.status = "expired"
                return RegistrationStatus.EXPIRED
            if not secrets.compare_digest(challenge.code_hash, code_hash):
                challenge.attempts += 1
                if challenge.attempts >= max_attempts:
                    challenge.status = "locked"
                    return RegistrationStatus.LOCKED
                return RegistrationStatus.INVALID_CODE
            return RegistrationStatus.COMPLETE

    def complete(
        self,
        *,
        challenge_id: str,
        purpose: str,
        phone_hash: str,
        tenant_hash: str,
        phone_last4: str,
        code_hash: str,
        session_hash: str | None,
        session_expires_at: datetime | None,
        password_salt: bytes | None,
        password_hash: bytes | None,
        password_params: str | None,
        agreement_version: str | None,
        now: datetime,
        max_attempts: int,
    ) -> tuple[RegistrationStatus, User | None]:
        """Verify, consume, create/reuse user, and persist session atomically."""

        with self._lock:
            challenge = self.challenges.get(challenge_id)
            if (
                challenge is None
                or challenge.phone_hash != phone_hash
                or challenge.tenant_hash != tenant_hash
                or challenge.purpose != purpose
            ):
                return RegistrationStatus.INVALID_CODE, None
            if challenge.status == "locked" or challenge.attempts >= max_attempts:
                return RegistrationStatus.LOCKED, None
            if challenge.status != "sent":
                return RegistrationStatus.INVALID_CODE, None
            if now >= challenge.expires_at:
                challenge.status = "expired"
                return RegistrationStatus.EXPIRED, None
            if not secrets.compare_digest(challenge.code_hash, code_hash):
                challenge.attempts += 1
                if challenge.attempts >= max_attempts:
                    challenge.status = "locked"
                    return RegistrationStatus.LOCKED, None
                return RegistrationStatus.INVALID_CODE, None
            user = self.users_by_phone.get(phone_hash)
            if purpose in {"login", "sensitive_export", "sensitive_delete"} and (user is None or user.status != "active"):
                return RegistrationStatus.LOCKED, None
            if purpose == "register" and user is not None:
                return RegistrationStatus.INVALID_CODE, None
            challenge.status = "verified"
            if purpose == "register":
                if None in {password_salt, password_hash, password_params, agreement_version}:
                    return RegistrationStatus.INVALID_CODE, None
                user = User(
                    user_id=uuid.uuid4().hex,
                    phone_hash=phone_hash,
                    phone_last4=phone_last4,
                    created_at=now,
                )
                self.users_by_phone[phone_hash] = user
                self.password_credentials[user.user_id] = (password_salt, password_hash, password_params)  # type: ignore[arg-type]
                self.agreements[user.user_id] = (agreement_version, now)  # type: ignore[arg-type]
            if session_hash and session_expires_at:
                self.sessions[session_hash] = Session(session_hash, user.user_id, session_expires_at)
            return RegistrationStatus.COMPLETE, user

    def get_session_user(self, session_hash: str, now: datetime) -> User | None:
        with self._lock:
            session = self.sessions.get(session_hash)
            if session is None or session.expires_at <= now:
                return None
            return next(
                (user for user in self.users_by_phone.values() if user.user_id == session.user_id and user.status == "active"),
                None,
            )

    def revoke_session(self, session_hash: str, now: datetime) -> bool:
        del now
        with self._lock:
            return self.sessions.pop(session_hash, None) is not None

    def revoke_user_sessions(self, user_id: str, now: datetime) -> int:
        del now
        with self._lock:
            matches = [key for key, value in self.sessions.items() if value.user_id == user_id]
            for key in matches:
                del self.sessions[key]
            return len(matches)

    def deactivate_user(self, user_id: str, now: datetime) -> bool:
        with self._lock:
            for phone_hash, user in self.users_by_phone.items():
                if user.user_id == user_id:
                    if user.status == "active":
                        self.users_by_phone[phone_hash] = User(user.user_id, user.phone_hash, user.phone_last4, user.created_at, "deleted")
                        self.revoke_user_sessions(user_id, now)
                    return user.status in {"active", "deleted"}
            return False

    def audit(self, event: AuditEvent) -> None:
        with self._lock:
            self.audit_events.append(event)


class RegistrationService:
    GENERIC_SEND_MESSAGE = "如请求符合条件，验证码将发送至该手机号。"

    def __init__(
        self,
        *,
        store: RegistrationStore,
        sms_sender: SmsSender,
        captcha_verifier: CaptchaVerifier,
        secret_pepper: bytes,
        config: AuthConfig | None = None,
    ) -> None:
        if len(secret_pepper) < 32:
            raise ValueError("secret_pepper must contain at least 32 bytes")
        self.store = store
        self.sms_sender = sms_sender
        self.captcha_verifier = captcha_verifier
        self.secret_pepper = secret_pepper
        self.config = config or AuthConfig()

    def _hash(self, kind: str, value: str) -> str:
        return hmac.new(
            self.secret_pepper,
            f"{kind}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _code_hash(self, challenge_id: str, code: str) -> str:
        return self._hash("otp", f"{challenge_id}:{code}")

    def _subjects(
        self, phone: str, ip_address: str, device_id: str, tenant_scope: str
    ) -> RequestSubjects:
        if not 8 <= len(device_id.strip()) <= 200:
            raise ValueError("device_id length must be between 8 and 200")
        if not 1 <= len(tenant_scope.strip()) <= 128:
            raise ValueError("tenant_scope is required and must not exceed 128 characters")
        return RequestSubjects(
            phone_hash=self._hash("phone", phone),
            ip_hash=self._hash("ip", _normalize_ip(ip_address)),
            ip_prefix_hash=self._hash("ip_prefix", _normalize_ip_prefix(ip_address)),
            device_hash=self._hash("device", device_id.strip()),
            tenant_hash=self._hash("tenant", tenant_scope.strip()),
        )

    @staticmethod
    def _masked(phone: str) -> str:
        return f"{phone[:3]}****{phone[-4:]}"

    @staticmethod
    def _jitter(seconds: int) -> int:
        """Add non-negative jitter without shortening the actual cooldown."""

        return max(1, (seconds * (100 + secrets.randbelow(16)) + 99) // 100)

    def _audit(
        self,
        *,
        event: str,
        now: datetime,
        phone: str,
        subjects: RequestSubjects,
        outcome: str,
        metadata: dict[str, str | int | bool] | None = None,
    ) -> None:
        self.store.audit(
            AuditEvent(
                event=event,
                occurred_at=now,
                phone_masked=self._masked(phone),
                phone_hash=subjects.phone_hash,
                ip_hash=subjects.ip_hash,
                ip_prefix_hash=subjects.ip_prefix_hash,
                device_hash=subjects.device_hash,
                tenant_hash=subjects.tenant_hash,
                outcome=outcome,
                metadata=metadata or {},
            )
        )

    def request_code(
        self,
        *,
        purpose: str,
        phone: str,
        ip_address: str,
        device_id: str,
        captcha_token: str | None = None,
        tenant_scope: str = "public-registration",
        now: datetime | None = None,
    ) -> SendCodeResult:
        if purpose not in PURPOSES:
            raise ValueError("unsupported purpose")
        now = now or _utcnow()
        normalized = normalize_cn_mobile(phone)
        subjects = self._subjects(normalized, ip_address, device_id, tenant_scope)
        phone_day = self.store.count("phone", subjects.phone_hash, now - timedelta(days=1))
        ip_hour = self.store.count("ip", subjects.ip_hash, now - timedelta(hours=1))
        captcha_needed = (
            phone_day >= self.config.captcha_after_phone_day
            or ip_hour >= self.config.captcha_after_ip_hour
        )
        if captcha_needed and not (
            captcha_token
            and self.captcha_verifier.verify(
                captcha_token, ip_hash=subjects.ip_hash, phone_hash=subjects.phone_hash
            )
        ):
            self._audit(
                event="sms.request",
                now=now,
                phone=normalized,
                subjects=subjects,
                outcome="captcha_required",
            )
            return SendCodeResult(
                SendCodeStatus.CAPTCHA_REQUIRED,
                "请先完成人机验证后重试。",
            )

        reserved, retry_after = self.store.reserve_send(
            phone_hash=subjects.phone_hash,
            cooldown_hash=self._hash("cooldown", subjects.phone_hash),
            ip_hash=subjects.ip_hash,
            ip_prefix_hash=subjects.ip_prefix_hash,
            device_hash=subjects.device_hash,
            tenant_hash=subjects.tenant_hash,
            now=now,
            config=self.config,
        )
        if not reserved:
            self._audit(
                event="sms.request",
                now=now,
                phone=normalized,
                subjects=subjects,
                outcome="rate_limited",
            )
            return SendCodeResult(
                SendCodeStatus.RETRY_LATER,
                self.GENERIC_SEND_MESSAGE,
                challenge_id=secrets.token_hex(16),
                retry_after_seconds=self._jitter(retry_after),
            )

        user = self.store.find_user(subjects.phone_hash)
        if purpose == "login" and user is None:
            self._audit(event="otp.request", now=now, phone=normalized, subjects=subjects, outcome="phone_not_registered", metadata={"purpose": purpose})
            return SendCodeResult(SendCodeStatus.PHONE_NOT_REGISTERED, "该手机号尚未注册。")
        if purpose == "register" and user is not None:
            self._audit(event="otp.request", now=now, phone=normalized, subjects=subjects, outcome="phone_already_registered", metadata={"purpose": purpose})
            return SendCodeResult(SendCodeStatus.PHONE_ALREADY_REGISTERED, "该手机号已注册。")
        if purpose in {"login", "sensitive_export", "sensitive_delete"} and (user is None or user.status != "active"):
            return SendCodeResult(SendCodeStatus.LOCKED, self.GENERIC_SEND_MESSAGE)

        challenge_id = uuid.uuid4().hex
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = Challenge(
            challenge_id=challenge_id,
            phone_hash=subjects.phone_hash,
            tenant_hash=subjects.tenant_hash,
            purpose=purpose,
            code_hash=self._code_hash(challenge_id, code),
            expires_at=now + timedelta(seconds=self.config.code_ttl_seconds),
            created_at=now,
        )
        self.store.add_challenge(challenge)
        try:
            receipt = self.sms_sender.send_verification(
                normalized, code, self.config.code_ttl_seconds
            )
        except Exception:
            self.store.mark_delivery(challenge_id, "delivery_failed", None)
            self._audit(
                event="sms.request",
                now=now,
                phone=normalized,
                subjects=subjects,
                outcome="provider_failed",
            )
            return SendCodeResult(
                SendCodeStatus.TEMPORARILY_UNAVAILABLE,
                self.GENERIC_SEND_MESSAGE,
                challenge_id=secrets.token_hex(16),
                retry_after_seconds=self._jitter(retry_after),
            )
        self.store.mark_delivery(challenge_id, "sent", receipt)
        self._audit(
            event="sms.request",
            now=now,
            phone=normalized,
            subjects=subjects,
            outcome="accepted",
            metadata={"purpose": purpose},
        )
        return SendCodeResult(
            SendCodeStatus.ACCEPTED,
            self.GENERIC_SEND_MESSAGE,
            challenge_id=challenge_id,
            retry_after_seconds=self._jitter(retry_after),
        )

    def _complete(
        self,
        *,
        purpose: str,
        challenge_id: str,
        phone: str,
        code: str,
        ip_address: str,
        device_id: str,
        password: str | None = None,
        terms_version: str | None = None,
        privacy_version: str | None = None,
        create_session: bool = True,
        tenant_scope: str = "public-registration",
        now: datetime | None = None,
    ) -> RegistrationResult:
        if purpose not in PURPOSES:
            raise ValueError("unsupported purpose")
        agreement_version = None
        if purpose in {"login", "register"}:
            versions = (terms_version, privacy_version)
            if any(
                not isinstance(version, str)
                or not 1 <= len(version) <= self.config.max_protocol_version_length
                or not secrets.compare_digest(version, self.config.protocol_version)
                for version in versions
            ):
                return RegistrationResult(RegistrationStatus.AGREEMENT_REQUIRED, "agreement_required")
            agreement_version = f"terms:{self.config.protocol_version}|privacy:{self.config.protocol_version}"
        if purpose == "register" and (
            password is None
            or not 6 <= len(password) <= 20
            or any(character.isspace() or not character.isprintable() for character in password)
        ):
            return RegistrationResult(RegistrationStatus.WEAK_PASSWORD, "weak_password")
        now = now or _utcnow()
        normalized = normalize_cn_mobile(phone)
        subjects = self._subjects(normalized, ip_address, device_id, tenant_scope)
        if not re.fullmatch(r"\d{6}", code):
            return RegistrationResult(RegistrationStatus.INVALID_CODE, "验证码无效或已过期。")
        code_hash = self._code_hash(challenge_id, code)
        if purpose == "register":
            prevalidated = self.store.prevalidate_code(
                challenge_id=challenge_id,
                purpose=purpose,
                phone_hash=subjects.phone_hash,
                tenant_hash=subjects.tenant_hash,
                code_hash=code_hash,
                now=now,
                max_attempts=self.config.max_code_attempts,
            )
            if prevalidated is not RegistrationStatus.COMPLETE:
                self._audit(
                    event=f"{purpose}.complete",
                    now=now,
                    phone=normalized,
                    subjects=subjects,
                    outcome=prevalidated.value,
                )
                messages = {
                    RegistrationStatus.INVALID_CODE: "验证码无效或已过期。",
                    RegistrationStatus.EXPIRED: "验证码无效或已过期。",
                    RegistrationStatus.LOCKED: "验证次数过多，请重新获取验证码。",
                }
                return RegistrationResult(prevalidated, messages[prevalidated])
        session_token = secrets.token_urlsafe(32) if create_session else None
        salt = secrets.token_bytes(16) if purpose == "register" else None
        password_pepper = hmac.new(
            self.secret_pepper,
            f"password-pepper-{PASSWORD_PEPPER_VERSION}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        password_hash = (
            hashlib.scrypt(
                password.encode("utf-8"), salt=password_pepper + salt,
                n=self.config.scrypt_n, r=self.config.scrypt_r, p=self.config.scrypt_p, dklen=64,
            ) if salt and password is not None else None
        )
        password_params = (
            f"scrypt;n={self.config.scrypt_n};r={self.config.scrypt_r};p={self.config.scrypt_p};dklen=64;pepper={PASSWORD_PEPPER_VERSION}"
            if salt else None
        )
        status, user = self.store.complete(
            challenge_id=challenge_id,
            purpose=purpose,
            phone_hash=subjects.phone_hash,
            tenant_hash=subjects.tenant_hash,
            phone_last4=normalized[-4:],
            code_hash=code_hash,
            session_hash=self._hash("session", session_token) if session_token else None,
            session_expires_at=now + timedelta(days=self.config.session_ttl_days) if session_token else None,
            password_salt=salt,
            password_hash=password_hash,
            password_params=password_params,
            agreement_version=agreement_version,
            now=now,
            max_attempts=self.config.max_code_attempts,
        )
        self._audit(
            event=f"{purpose}.complete",
            now=now,
            phone=normalized,
            subjects=subjects,
            outcome=status.value,
        )
        messages = {
            RegistrationStatus.COMPLETE: "注册完成。",
            RegistrationStatus.INVALID_CODE: "验证码无效或已过期。",
            RegistrationStatus.EXPIRED: "验证码无效或已过期。",
            RegistrationStatus.LOCKED: "验证次数过多，请重新获取验证码。",
            RegistrationStatus.WEAK_PASSWORD: "weak_password",
            RegistrationStatus.AGREEMENT_REQUIRED: "agreement_required",
        }
        if status is not RegistrationStatus.COMPLETE or user is None:
            return RegistrationResult(status, messages[status])
        return RegistrationResult(
            status,
            messages[status],
            user.user_id,
            session_token,
            user.status,
        )

    def login(self, **kwargs: object) -> RegistrationResult:
        return self._complete(purpose="login", **kwargs)  # type: ignore[arg-type]

    def complete_registration(self, **kwargs: object) -> RegistrationResult:
        return self._complete(purpose="register", **kwargs)  # type: ignore[arg-type]

    def request_sensitive_code(self, *, session_token: str, phone: str, action: str, ip_address: str, device_id: str, captcha_token: str | None = None, now: datetime | None = None) -> SendCodeResult:
        if action not in {"export", "delete"}:
            raise ValueError("unsupported sensitive action")
        user = self.authenticate_session(session_token, now=now)
        normalized = normalize_cn_mobile(phone)
        if user is None or user.phone_hash != self._hash("phone", normalized):
            return SendCodeResult(SendCodeStatus.LOCKED, self.GENERIC_SEND_MESSAGE)
        return self.request_code(purpose=f"sensitive_{action}", phone=normalized, ip_address=ip_address, device_id=device_id, captcha_token=captcha_token, now=now)

    def verify_sensitive(self, session_token: str, phone: str, challenge_id: str, code: str, action: str, *, ip_address: str, device_id: str, now: datetime | None = None) -> User | None:
        if action not in {"export", "delete"}:
            raise ValueError("unsupported sensitive action")
        current = now or _utcnow()
        user = self.authenticate_session(session_token, now=current)
        normalized = normalize_cn_mobile(phone)
        if user is None or user.phone_hash != self._hash("phone", normalized) or not re.fullmatch(r"\d{6}", code):
            return None
        result = self._complete(purpose=f"sensitive_{action}", challenge_id=challenge_id, phone=normalized, code=code, ip_address=ip_address, device_id=device_id, now=current, create_session=False)
        return user if result.status is RegistrationStatus.COMPLETE and result.user_id == user.user_id else None

    def authenticate_session(
        self, session_token: str, *, now: datetime | None = None
    ) -> User | None:
        if not session_token:
            return None
        return self.store.get_session_user(
            self._hash("session", session_token), now or _utcnow()
        )

    def logout(self, session_token: str, *, now: datetime | None = None) -> bool:
        if not session_token:
            return False
        return self.store.revoke_session(
            self._hash("session", session_token), now or _utcnow()
        )

    def logout_all(self, session_token: str, *, now: datetime | None = None) -> bool:
        current = now or _utcnow()
        user = self.authenticate_session(session_token, now=current)
        if user is None:
            return False
        self.store.revoke_user_sessions(user.user_id, current)
        return True

    def deactivate_account(self, user_id: str, *, now: datetime | None = None) -> bool:
        return self.store.deactivate_user(user_id, now or _utcnow())
