"""瑞成云（数米科技）验证码短信适配器。

服务商只提供 HTTP 接口，因此凭据仅从服务端环境变量读取，请求不写日志，
生产部署还必须使用固定出口 IP 和服务商 IP 白名单限制暴露面。
"""

from __future__ import annotations

import http.client
import os
import re
from typing import Callable
from urllib.parse import urlencode, urlsplit


DEFAULT_ENDPOINT = "http://115.28.112.245:8082/SendMT/SendMessage"
DEFAULT_SIGNATURE = "【云派】"
_ACCEPTED_CODES = {"00", "03"}


class SmsProviderError(RuntimeError):
    """可安全记录的通道异常；消息中不得包含手机号、验证码或凭据。"""


Transport = Callable[[str, bytes, float, float], bytes]


def _post_form(url: str, body: bytes, connect_timeout: float, read_timeout: float) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise SmsProviderError("SMS endpoint configuration is invalid")
    connection = http.client.HTTPConnection(
        parsed.hostname,
        parsed.port or 80,
        timeout=connect_timeout,
    )
    try:
        connection.request(
            "POST",
            parsed.path,
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "text/plain",
            },
        )
        if connection.sock is not None:
            connection.sock.settimeout(read_timeout)
        response = connection.getresponse()
        payload = response.read(4097)
        if response.status != 200 or len(payload) > 4096:
            raise SmsProviderError("SMS provider returned an invalid HTTP response")
        return payload
    except SmsProviderError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise SmsProviderError("SMS provider is temporarily unavailable") from exc
    finally:
        connection.close()


class RuichengSmsSender:
    """实现注册服务的 ``SmsSender`` 协议，不包含自动重试。"""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        signature: str = DEFAULT_SIGNATURE,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: Transport = _post_form,
    ) -> None:
        if not username or not password:
            raise ValueError("SMS username and password are required")
        if not signature.startswith("【") or not signature.endswith("】"):
            raise ValueError("SMS signature must use full-width square brackets")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path != "/SendMT/SendMessage"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid Ruicheng SMS endpoint")
        self._username = username
        self._password = password
        self._signature = signature
        self._endpoint = endpoint
        self._transport = transport

    @classmethod
    def from_environment(cls) -> "RuichengSmsSender":
        return cls(
            username=os.environ["RUICHENG_SMS_USERNAME"],
            password=os.environ["RUICHENG_SMS_PASSWORD"],
            signature=os.environ.get("RUICHENG_SMS_SIGNATURE", DEFAULT_SIGNATURE),
        )

    def send_verification(self, phone: str, code: str, ttl_seconds: int) -> str:
        if not re.fullmatch(r"1[3-9]\d{9}", phone) or not re.fullmatch(r"\d{6}", code):
            raise SmsProviderError("SMS verification payload is invalid")
        if ttl_seconds != 300:
            raise SmsProviderError("SMS template requires a 300-second code lifetime")
        content = (
            f"{self._signature} {code}（验证码）,该验证码5分钟有效。"
            "如果不是您本人操作，请忽略本条短信。"
        )
        if len(content) > 70:
            raise SmsProviderError("SMS verification content exceeds 70 characters")
        body = urlencode(
            {
                "UserName": self._username,
                "UserPass": self._password,
                "Mobile": phone,
                "Content": content,
                "Subid": "",
            },
            encoding="utf-8",
        ).encode("ascii")
        try:
            raw = self._transport(self._endpoint, body, 3.0, 5.0)
            text = raw.decode("utf-8", errors="strict").strip()
        except SmsProviderError:
            raise
        except (OSError, TimeoutError, UnicodeError) as exc:
            raise SmsProviderError("SMS provider is temporarily unavailable") from exc
        status, separator, receipt = text.partition(",")
        if status not in _ACCEPTED_CODES or not separator or not receipt.strip():
            safe_status = status if len(status) == 2 and status.isdigit() else "invalid"
            raise SmsProviderError(f"SMS provider rejected request ({safe_status})")
        return receipt.strip()
