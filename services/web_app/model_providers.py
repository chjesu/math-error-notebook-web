"""Provider profiles that reuse the fixed Harness transport without leaking suppliers upstream."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from services.web_domain.model_provider import (
    ModelProvider,
    ModelProviderError,
    ProviderCapabilities,
    ProviderErrorCategory,
)
from .harness_runtime import HarnessRuntimeAdapter, HarnessRuntimeConfig


_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_ROUTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CAPABILITIES = ProviderCapabilities(
    vision=True,
    # The application validates JSON Schema, but the current Harness transport
    # does not yet advertise strict provider-side response_format enforcement.
    json_schema=False,
    streaming=True,
    conversation_history=True,
    compaction=True,
    cancellation=True,
)


class ProviderConfigurationError(ValueError):
    """Fail-closed deployment configuration error with no secret material."""


@dataclass(frozen=True)
class ProviderSettings:
    name: str
    display_name: str
    internal_route: str
    api_key_env: str
    base_url: str
    model: str
    max_tokens: int
    reasoning: str | None

    @property
    def harness_environment(self) -> tuple[tuple[str, str], ...]:
        values = {
            "HARNESS_PROVIDER": self.internal_route,
            "HARNESS_PROVIDER_NAME": self.display_name,
            "HARNESS_API_KEY_ENV": self.api_key_env,
            "HARNESS_API_PROTOCOL": "openai-completions",
            "HARNESS_BASE_URL": self.base_url,
            "HARNESS_MODEL": self.model,
            "HARNESS_INPUT_MODALITIES": "text,image",
            "HARNESS_MAX_TOKENS": str(self.max_tokens),
        }
        if self.reasoning:
            values["HARNESS_REASONING"] = self.reasoning
        return tuple(sorted(values.items()))


def _provider_name(environment: Mapping[str, str]) -> str:
    explicit = environment.get("MODEL_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    legacy_endpoint = environment.get("HARNESS_BASE_URL", "").casefold()
    legacy_key_name = environment.get("HARNESS_API_KEY_ENV", "").strip()
    if "dashscope" in legacy_endpoint or ".maas.aliyuncs.com" in legacy_endpoint or legacy_key_name == "DASHSCOPE_API_KEY":
        return "dashscope"
    return "deepseek"


def _required_secret(environment: Mapping[str, str], name: str) -> None:
    if not environment.get(name, "").strip():
        raise ProviderConfigurationError(f"{name} must be configured in the runtime environment")


def _validate_model(model: str) -> str:
    value = model.strip()
    if not _MODEL.fullmatch(value):
        raise ProviderConfigurationError("model identifier is invalid")
    return value


def _validate_internal_route(value: str) -> str:
    route = value.strip()
    if not _ROUTE.fullmatch(route) or route != "notebook-provider":
        raise ProviderConfigurationError("HARNESS_PROVIDER route identifier is invalid")
    return route


def _validate_endpoint(provider: str, endpoint: str) -> str:
    value = endpoint.strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigurationError(f"{provider} endpoint must be an HTTPS provider URL") from exc
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or port not in {None, 443}:
        raise ProviderConfigurationError(f"{provider} endpoint must be an HTTPS provider URL")
    hostname = (parsed.hostname or "").casefold()
    if provider == "deepseek":
        allowed = hostname == "api.deepseek.com" and parsed.path in {"", "/v1"}
    else:
        allowed_host = hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or hostname.endswith(".maas.aliyuncs.com")
        allowed = allowed_host and parsed.path == "/compatible-mode/v1"
    if not allowed:
        raise ProviderConfigurationError(f"{provider} endpoint is not an allowed official endpoint")
    return value


def _settings(environment: Mapping[str, str]) -> ProviderSettings:
    name = _provider_name(environment)
    if name not in {"deepseek", "dashscope"}:
        raise ProviderConfigurationError("MODEL_PROVIDER must be deepseek or dashscope")
    internal_route = _validate_internal_route(environment.get("HARNESS_PROVIDER", "notebook-provider"))
    max_tokens_text = environment.get("HARNESS_MAX_TOKENS", "32768").strip()
    try:
        max_tokens = int(max_tokens_text)
    except ValueError as exc:
        raise ProviderConfigurationError("HARNESS_MAX_TOKENS must be an integer") from exc
    if not 1 <= max_tokens <= 256_000:
        raise ProviderConfigurationError("HARNESS_MAX_TOKENS must be between 1 and 256000")
    reasoning = environment.get("HARNESS_REASONING", "").strip() or None
    if name == "deepseek":
        key_name = "DEEPSEEK_API_KEY"
        endpoint = environment.get("DEEPSEEK_BASE_URL") or environment.get("HARNESS_BASE_URL") or "https://api.deepseek.com"
        model = environment.get("DEEPSEEK_MODEL") or environment.get("HARNESS_MODEL") or "deepseek-v4-flash-vision-exp"
        display_name = environment.get("HARNESS_PROVIDER_NAME", "DeepSeek")
    else:
        key_name = "DASHSCOPE_API_KEY"
        endpoint = environment.get("DASHSCOPE_BASE_URL") or environment.get("HARNESS_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = environment.get("DASHSCOPE_MODEL") or environment.get("HARNESS_MODEL") or "qwen-vl-max"
        display_name = environment.get("HARNESS_PROVIDER_NAME", "Aliyun Bailian Qwen-VL")
    _required_secret(environment, key_name)
    return ProviderSettings(
        name=name,
        display_name=display_name.strip()[:128] or name,
        internal_route=internal_route,
        api_key_env=key_name,
        base_url=_validate_endpoint(name, endpoint),
        model=_validate_model(model),
        max_tokens=max_tokens,
        reasoning=reasoning,
    )


class _HarnessBackedProvider(ModelProvider):
    capabilities = _CAPABILITIES

    def __init__(self, name: str, runtime: HarnessRuntimeAdapter) -> None:
        self.name = name
        self.runtime = runtime

    @staticmethod
    def _error(exc: Exception) -> ModelProviderError:
        public_code = getattr(exc, "public_code", "model_unavailable")
        if public_code not in {
            "model_authentication_error", "model_rate_limited", "model_network_error",
            "model_interrupted", "model_unavailable",
        }:
            public_code = "model_unavailable"
        mapping = {
            "model_authentication_error": (ProviderErrorCategory.AUTHENTICATION, False),
            "model_rate_limited": (ProviderErrorCategory.RATE_LIMIT, True),
            "model_network_error": (ProviderErrorCategory.NETWORK, True),
            "model_interrupted": (ProviderErrorCategory.INTERRUPTED, False),
        }
        category, retryable = mapping.get(public_code, (ProviderErrorCategory.SERVICE, False))
        return ModelProviderError(
            "Model provider request failed",
            category=category,
            public_code=public_code,
            retryable=retryable,
        )

    def _invoke(self, operation: Callable[..., dict[str, Any]], *args, **kwargs) -> dict[str, Any]:
        try:
            return operation(*args, **kwargs)
        except ModelProviderError:
            raise
        except Exception as exc:
            raise self._error(exc) from exc

    def run_structured_turn(self, *args, **kwargs) -> dict[str, Any]:
        return self._invoke(self.runtime.run_structured_turn, *args, **kwargs)

    def run_conversation_turn(self, *args, **kwargs) -> dict[str, Any]:
        return self._invoke(self.runtime.run_conversation_turn, *args, **kwargs)

    def read_history(self, *args, **kwargs) -> dict[str, Any]:
        return self._invoke(self.runtime.read_history, *args, **kwargs)

    def compact(self, *args, **kwargs) -> dict[str, Any]:
        return self._invoke(self.runtime.compact, *args, **kwargs)

    def close(self) -> None:
        self.runtime.close()


class DeepSeekHarnessProvider(_HarnessBackedProvider):
    """Default local provider using DeepSeek through the fixed Harness runtime."""


class AliyunDashscopeProvider(_HarnessBackedProvider):
    """Qwen-VL provider using the official OpenAI-compatible Bailian endpoint.

    Protocol: https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
    """


def build_model_provider(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    runtime_factory: Callable[[HarnessRuntimeConfig], HarnessRuntimeAdapter] = HarnessRuntimeAdapter,
) -> ModelProvider:
    environment = os.environ if environ is None else environ
    settings = _settings(environment)
    runtime_environment = dict(environment)
    runtime_environment.update(dict(settings.harness_environment))
    runtime_environment["MODEL_PROVIDER"] = settings.name
    config = HarnessRuntimeConfig.from_environment(project_root, environ=runtime_environment)
    runtime = runtime_factory(config)
    provider_type = DeepSeekHarnessProvider if settings.name == "deepseek" else AliyunDashscopeProvider
    return provider_type(settings.name, runtime)


__all__ = [
    "AliyunDashscopeProvider",
    "DeepSeekHarnessProvider",
    "ProviderConfigurationError",
    "ProviderSettings",
    "build_model_provider",
]
