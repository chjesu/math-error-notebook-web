"""Stable application boundary for model suppliers and their runtime adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class ProviderErrorCategory(str, Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    SERVICE = "service"
    INVALID_RESPONSE = "invalid_response"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class ProviderCapabilities:
    vision: bool
    json_schema: bool
    streaming: bool
    conversation_history: bool
    compaction: bool
    cancellation: bool


class ModelProviderError(RuntimeError):
    """Sanitized provider failure; private diagnostics stay behind the adapter."""

    def __init__(
        self,
        message: str,
        *,
        category: ProviderErrorCategory,
        public_code: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.public_code = public_code
        self.retryable = retryable


class ModelProvider(ABC):
    """One provider contract consumed by the notebook orchestration layer."""

    name: str
    capabilities: ProviderCapabilities

    @abstractmethod
    def run_structured_turn(
        self,
        route: dict[str, Any],
        review_input: str,
        output_path: Path,
        images: list[Path] | None = None,
        thread_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def run_conversation_turn(
        self,
        route: dict[str, Any],
        review_input: str,
        output_path: Path,
        session_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: Any = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def read_history(
        self, thread_id: str, cursor: str | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def compact(self, thread_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError
