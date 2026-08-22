"""Phone-code registration domain service with abuse and privacy controls.

The module deliberately has no web-framework or database dependency.  HTTP and
MySQL adapters can be added around these interfaces without putting provider
keys, plaintext OTPs, or database credentials in an agent/model process.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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


def _is_minor(birth_date: date, today: date) -> bool:
    try:
        eighteenth = birth_date.replace(year=birth_date.year + 18)
    except ValueError:  # 29 February reaches adulthood on 28 February.
        eighteenth = birth_date.replace(year=birth_date.year + 18, day=28)
    return today < eighteenth


class SendCodeStatus(str, Enum):
    ACCEPTED = "accepted"
    CAPTCHA_REQUIRED = "captcha_required"
    RETRY_LATER = "retry_later"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"


class RegistrationStatus(str, Enum):
    COMPLETE = "complete"
    INVALID_CODE = "invalid_code"
    EXPIRED = "expired"
    LOCKED = "locked"
    GUARDIAN_CONSENT_REQUIRED = "guardian_consent_required"


@dataclass(frozen=True)
class AuthConfig:
    code_ttl_seconds: int = 300
    resend_cooldown_seconds: int = 60
    max_code_attempts: int = 5
    phone_hour_limit: int = 5
    phone_day_limit: int = 5
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
    display_name: str
    birth_date: date
    guardian_consent_receipt: str | None
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


class GuardianConsentVerifier(Protocol):
    def verify(self, receipt: str, *, student_phone_hash: str, birth_date: date) -> bool: ...


class RegistrationStore(Protocol):
    """Persistence contract; production implements every mutation transactionally."""

    def count(self, dimension: str, subject_hash: str, since: datetime) -> int: ...

    def reserve_send(
        self,
        *,
        phone_hash: str,
        ip_hash: str,
        ip_prefix_hash: str,
        device_hash: str,
        tenant_hash: str,
        now: datetime,
        config: AuthConfig,
    ) -> tuple[bool, int]: ...

    def add_challenge(self, challenge: Challenge) -> None: ...

    def mark_delivery(self, challenge_id: str, status: str, receipt: str | None) -> None: ...

    def register(
        self,
        *,
        challenge_id: str,
        phone_hash: str,
        tenant_hash: str,
        phone_last4: str,
        code_hash: str,
        display_name: str,
        birth_date: date,
        guardian_consent_receipt: str | None,
        block_for_guardian: bool,
        session_hash: str,
        session_expires_at: datetime,
        now: datetime,
        max_attempts: int,
    ) -> tuple[RegistrationStatus, User | None]: ...

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


class InMemoryGuardianConsentVerifier:
    """Test adapter. Production verifies a server-issued, revocable receipt."""

    def __init__(self, accepted_receipts: set[str] | None = None) -> None:
        self._accepted = set(accepted_receipts or set())

    def verify(self, receipt: str, *, student_phone_hash: str, birth_date: date) -> bool:
        del student_phone_hash, birth_date
        return receipt in self._accepted


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
        self.audit_events: list[AuditEvent] = []
        self._send_times: dict[tuple[str, str], list[datetime]] = defaultdict(list)

    def count(self, dimension: str, subject_hash: str, since: datetime) -> int:
        with self._lock:
            values = self._send_times[(dimension, subject_hash)]
            return sum(item >= since for item in values)

    def reserve_send(
        self,
        *,
        phone_hash: str,
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
            if recent_phone:
                elapsed = int((now - recent_phone[-1]).total_seconds())
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
            ):
                self._send_times[(dimension, subject)].append(now)
            return True, config.resend_cooldown_seconds

    def add_challenge(self, challenge: Challenge) -> None:
        with self._lock:
            for current in self.challenges.values():
                if current.phone_hash == challenge.phone_hash and current.status in {"pending", "sent"}:
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

    def register(
        self,
        *,
        challenge_id: str,
        phone_hash: str,
        tenant_hash: str,
        phone_last4: str,
        code_hash: str,
        display_name: str,
        birth_date: date,
        guardian_consent_receipt: str | None,
        block_for_guardian: bool,
        session_hash: str,
        session_expires_at: datetime,
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
            if user is None and block_for_guardian:
                return RegistrationStatus.GUARDIAN_CONSENT_REQUIRED, None
            challenge.status = "verified"
            if user is None:
                user = User(
                    user_id=uuid.uuid4().hex,
                    phone_hash=phone_hash,
                    phone_last4=phone_last4,
                    display_name=display_name,
                    birth_date=birth_date,
                    guardian_consent_receipt=guardian_consent_receipt,
                    created_at=now,
                )
                self.users_by_phone[phone_hash] = user
            self.sessions[session_hash] = Session(session_hash, user.user_id, session_expires_at)
            return RegistrationStatus.COMPLETE, user

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
        guardian_consent_verifier: GuardianConsentVerifier,
        secret_pepper: bytes,
        config: AuthConfig | None = None,
    ) -> None:
        if len(secret_pepper) < 32:
            raise ValueError("secret_pepper must contain at least 32 bytes")
        self.store = store
        self.sms_sender = sms_sender
        self.captcha_verifier = captcha_verifier
        self.guardian_consent_verifier = guardian_consent_verifier
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
        phone: str,
        ip_address: str,
        device_id: str,
        captcha_token: str | None = None,
        tenant_scope: str = "public-registration",
        now: datetime | None = None,
    ) -> SendCodeResult:
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

        challenge_id = uuid.uuid4().hex
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = Challenge(
            challenge_id=challenge_id,
            phone_hash=subjects.phone_hash,
            tenant_hash=subjects.tenant_hash,
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
                "服务暂时不可用，请稍后重试。",
                retry_after_seconds=self._jitter(retry_after),
            )
        self.store.mark_delivery(challenge_id, "sent", receipt)
        self._audit(
            event="sms.request",
            now=now,
            phone=normalized,
            subjects=subjects,
            outcome="accepted",
            metadata={"challenge_id": challenge_id},
        )
        return SendCodeResult(
            SendCodeStatus.ACCEPTED,
            self.GENERIC_SEND_MESSAGE,
            challenge_id=challenge_id,
            retry_after_seconds=self._jitter(retry_after),
        )

    def register(
        self,
        *,
        challenge_id: str,
        phone: str,
        code: str,
        display_name: str,
        birth_date: date,
        guardian_consent_receipt: str | None,
        ip_address: str,
        device_id: str,
        tenant_scope: str = "public-registration",
        now: datetime | None = None,
    ) -> RegistrationResult:
        now = now or _utcnow()
        normalized = normalize_cn_mobile(phone)
        subjects = self._subjects(normalized, ip_address, device_id, tenant_scope)
        if not re.fullmatch(r"\d{6}", code):
            return RegistrationResult(RegistrationStatus.INVALID_CODE, "验证码无效或已过期。")
        if not display_name.strip() or len(display_name.strip()) > 80:
            raise ValueError("display_name is required and must not exceed 80 characters")
        if birth_date > now.date():
            raise ValueError("birth_date cannot be in the future")
        consent_valid = bool(
            guardian_consent_receipt
            and self.guardian_consent_verifier.verify(
                guardian_consent_receipt,
                student_phone_hash=subjects.phone_hash,
                birth_date=birth_date,
            )
        )
        session_token = secrets.token_urlsafe(32)
        status, user = self.store.register(
            challenge_id=challenge_id,
            phone_hash=subjects.phone_hash,
            tenant_hash=subjects.tenant_hash,
            phone_last4=normalized[-4:],
            code_hash=self._code_hash(challenge_id, code),
            display_name=display_name.strip(),
            birth_date=birth_date,
            guardian_consent_receipt=guardian_consent_receipt,
            block_for_guardian=_is_minor(birth_date, now.date()) and not consent_valid,
            session_hash=self._hash("session", session_token),
            session_expires_at=now + timedelta(days=self.config.session_ttl_days),
            now=now,
            max_attempts=self.config.max_code_attempts,
        )
        self._audit(
            event="registration.complete",
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
            RegistrationStatus.GUARDIAN_CONSENT_REQUIRED: "未成年人注册需先完成监护人同意。",
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
