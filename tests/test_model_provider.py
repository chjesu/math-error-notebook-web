from __future__ import annotations

from pathlib import Path
import unittest

from services.web_app.model_providers import (
    AliyunDashscopeProvider,
    DeepSeekHarnessProvider,
    ProviderConfigurationError,
    build_model_provider,
)
from services.web_domain.model_provider import ModelProviderError, ProviderErrorCategory


class FakeRuntime:
    def __init__(self, config) -> None:
        self.config = config
        self.closed = False

    def run_structured_turn(self, *args, **kwargs):
        return {"kind": "structured", "args": args, "kwargs": kwargs}

    def run_conversation_turn(self, *args, **kwargs):
        return {"kind": "conversation", "args": args, "kwargs": kwargs}

    def read_history(self, *args, **kwargs):
        return {"items": [], "next_cursor": None}

    def compact(self, thread_id):
        return {"status": "completed", "thread_id": thread_id}

    def close(self) -> None:
        self.closed = True


class FailingRuntime(FakeRuntime):
    def run_structured_turn(self, *args, **kwargs):
        error = RuntimeError("private provider diagnostic")
        error.public_code = "model_network_error"
        raise error


class UnknownFailingRuntime(FakeRuntime):
    def run_structured_turn(self, *args, **kwargs):
        error = RuntimeError("private provider diagnostic")
        error.public_code = "private_provider_code"
        raise error


class ModelProviderTests(unittest.TestCase):
    def test_default_factory_builds_deepseek_without_exposing_secret_value(self) -> None:
        environment = {"DEEPSEEK_API_KEY": "configured-at-runtime"}
        provider = build_model_provider(
            Path.cwd(), environ=environment, runtime_factory=FakeRuntime,
        )
        self.assertIsInstance(provider, DeepSeekHarnessProvider)
        self.assertEqual(provider.name, "deepseek")
        self.assertTrue(provider.capabilities.vision)
        child_environment = dict(provider.runtime.config.provider_environment)
        self.assertEqual(child_environment["HARNESS_API_KEY_ENV"], "DEEPSEEK_API_KEY")
        self.assertNotIn("configured-at-runtime", child_environment.values())

    def test_dashscope_factory_uses_https_aliyun_endpoint_and_explicit_model(self) -> None:
        environment = {
            "MODEL_PROVIDER": "dashscope",
            "DASHSCOPE_API_KEY": "configured-at-runtime",
            "DASHSCOPE_BASE_URL": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "DASHSCOPE_MODEL": "qwen-vl-max",
        }
        provider = build_model_provider(
            Path.cwd(), environ=environment, runtime_factory=FakeRuntime,
        )
        self.assertIsInstance(provider, AliyunDashscopeProvider)
        self.assertEqual(provider.name, "dashscope")
        child_environment = dict(provider.runtime.config.provider_environment)
        self.assertEqual(child_environment["HARNESS_BASE_URL"], environment["DASHSCOPE_BASE_URL"])
        self.assertEqual(child_environment["HARNESS_MODEL"], "qwen-vl-max")
        self.assertEqual(child_environment["HARNESS_API_KEY_ENV"], "DASHSCOPE_API_KEY")

    def test_dashscope_rejects_missing_secret_unsafe_endpoint_and_unknown_provider(self) -> None:
        with self.assertRaisesRegex(ProviderConfigurationError, "DASHSCOPE_API_KEY"):
            build_model_provider(Path.cwd(), environ={"MODEL_PROVIDER": "dashscope"}, runtime_factory=FakeRuntime)
        with self.assertRaisesRegex(ProviderConfigurationError, "endpoint"):
            build_model_provider(Path.cwd(), environ={
                "MODEL_PROVIDER": "dashscope", "DASHSCOPE_API_KEY": "configured-at-runtime",
                "DASHSCOPE_BASE_URL": "http://127.0.0.1:8000/compatible-mode/v1",
            }, runtime_factory=FakeRuntime)
        with self.assertRaisesRegex(ProviderConfigurationError, "MODEL_PROVIDER"):
            build_model_provider(Path.cwd(), environ={"MODEL_PROVIDER": "other"}, runtime_factory=FakeRuntime)

    def test_provider_delegates_runtime_operations_and_closes_it(self) -> None:
        provider = build_model_provider(
            Path.cwd(), environ={"DEEPSEEK_API_KEY": "configured-at-runtime"}, runtime_factory=FakeRuntime,
        )
        self.assertEqual(provider.run_structured_turn("route", "input", "output")["kind"], "structured")
        self.assertEqual(provider.run_conversation_turn("route", "input", "output")["kind"], "conversation")
        self.assertEqual(provider.read_history("thread", None, 10), {"items": [], "next_cursor": None})
        self.assertEqual(provider.compact("thread")["status"], "completed")
        provider.close()
        self.assertTrue(provider.runtime.closed)

    def test_provider_normalizes_retryable_runtime_failure(self) -> None:
        provider = build_model_provider(
            Path.cwd(), environ={"DEEPSEEK_API_KEY": "configured-at-runtime"}, runtime_factory=FailingRuntime,
        )
        with self.assertRaises(ModelProviderError) as raised:
            provider.run_structured_turn("route", "input", "output")
        self.assertEqual(raised.exception.category, ProviderErrorCategory.NETWORK)
        self.assertEqual(raised.exception.public_code, "model_network_error")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("private provider diagnostic", str(raised.exception))

    def test_provider_does_not_expose_unknown_runtime_error_codes(self) -> None:
        provider = build_model_provider(
            Path.cwd(), environ={"DEEPSEEK_API_KEY": "configured-at-runtime"}, runtime_factory=UnknownFailingRuntime,
        )
        with self.assertRaises(ModelProviderError) as raised:
            provider.run_structured_turn("route", "input", "output")
        self.assertEqual(raised.exception.public_code, "model_unavailable")
        self.assertEqual(raised.exception.category, ProviderErrorCategory.SERVICE)
        self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
