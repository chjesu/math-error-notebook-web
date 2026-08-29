"""Authorization and stable payloads for the read-only operations dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol


ROLE_SECTIONS = {
    "operations": ("overview", "tasks", "risk"),
    "reviewer": ("overview", "tasks", "content"),
    "security": ("overview", "risk", "privacy", "audit"),
    "administrator": ("overview", "tasks", "content", "risk", "privacy", "audit"),
}


class OperationsStore(Protocol):
    def get_operator_role(self, *, user_id: str) -> str | None: ...

    def dashboard(self, *, sections: tuple[str, ...], limit: int) -> dict[str, Any]: ...

    def record_access(self, *, user_id: str, role: str, sections: tuple[str, ...], occurred_at: datetime) -> None: ...


class InMemoryOperationsStore:
    """Small deterministic adapter for route and authorization tests."""

    def __init__(self, dashboard: dict[str, Any] | None = None) -> None:
        self.operators: dict[str, str] = {}
        self.dashboard_data = dashboard or {}
        self.audit_events: list[dict[str, Any]] = []

    def grant(self, *, user_id: str, role: str) -> None:
        if role not in ROLE_SECTIONS:
            raise ValueError("invalid operator role")
        self.operators[user_id] = role

    def get_operator_role(self, *, user_id: str) -> str | None:
        return self.operators.get(user_id)

    def dashboard(self, *, sections: tuple[str, ...], limit: int) -> dict[str, Any]:
        del limit
        return {name: self.dashboard_data.get(name, _empty_section(name)) for name in sections}

    def record_access(self, *, user_id: str, role: str, sections: tuple[str, ...], occurred_at: datetime) -> None:
        self.audit_events.append({"user_id": user_id, "role": role, "sections": list(sections), "occurred_at": occurred_at})


class OperationsService:
    def __init__(self, store: OperationsStore) -> None:
        self.store = store

    def session(self, *, user_id: str) -> dict[str, Any]:
        role = self.store.get_operator_role(user_id=user_id)
        if role not in ROLE_SECTIONS:
            raise PermissionError("operator access required")
        return {
            "role": role,
            "label": f"运营账号 ····{user_id[-8:]}",
            "sections": list(ROLE_SECTIONS[role]),
        }

    def dashboard(self, *, user_id: str, limit: int = 30) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("invalid dashboard limit")
        operator = self.session(user_id=user_id)
        sections = tuple(operator["sections"])
        snapshot = self.store.dashboard(sections=sections, limit=limit)
        now = datetime.now(timezone.utc)
        self.store.record_access(user_id=user_id, role=operator["role"], sections=sections, occurred_at=now)
        return {
            "operator": operator,
            "generated_at": now.isoformat(),
            "sections": snapshot,
        }


def _empty_section(name: str) -> Any:
    if name == "overview":
        return {"active_users": 0, "attention_tasks": 0, "candidate_questions": 0, "pending_privacy_cases": 0}
    if name == "risk":
        return {"sms_requested_today": 0, "sms_sent_today": 0, "sms_failed_today": 0, "rate_limited_today": 0}
    return []
