"""Deterministic task claiming and recoverable delivery workflow."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / "data" / "workflows"
REQUIRED_FILES = (
    "AGENTS.md",
    "PROJECT_ARCHITECTURE.md",
    "config/model-routing.json",
    "schemas/engineering-review-result.schema.json",
    "services/web_auth/registration.py",
    "services/web_auth/migrations/0001_phone_registration.sql",
)
STEPS = (
    ("requirements", "PO", False),
    ("architecture", "ARCH", False),
    ("implementation", "BE", False),
    ("deterministic_tests", "QA", False),
    ("luna_requirements_review", "PO", False),
    ("terra_implementation_review", "ARCH", False),
    ("sol_security_review", "SEC", False),
    ("integration_test", "QA", False),
    ("deploy_approval", "DM", True),
)
ID_RE = re.compile(r"^WEB-[A-Z0-9][A-Z0-9._-]{2,63}$")


def now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds")


def path_for(workflow_id: str) -> Path:
    if not ID_RE.fullmatch(workflow_id):
        raise ValueError("workflow id must match WEB-[A-Z0-9._-]{3,64}")
    return WORKFLOW_DIR / f"{workflow_id}.json"


def read(workflow_id: str) -> dict:
    path = path_for(workflow_id)
    if not path.is_file():
        raise ValueError(f"workflow not found: {workflow_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def write(workflow_id: str, payload: dict) -> None:
    WORKFLOW_DIR.mkdir(parents=True, exist_ok=True)
    path = path_for(workflow_id)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class Lock:
    def __init__(self, workflow_id: str) -> None:
        self.path = path_for(workflow_id).with_suffix(".lock")
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError("workflow is being updated; retry shortly") from exc
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def doctor() -> dict:
    missing = [name for name in REQUIRED_FILES if not (ROOT / name).is_file()]
    routing = json.loads((ROOT / "config" / "model-routing.json").read_text(encoding="utf-8"))
    return {
        "status": "ok" if not missing else "failed",
        "project_root": str(ROOT),
        "missing": missing,
        "routes": sorted(routing.get("tasks", {})),
        "database": "MySQL 8 (external; no local user database)",
    }


def start(workflow_id: str, label: str) -> dict:
    path = path_for(workflow_id)
    if path.exists():
        raise ValueError(f"workflow already exists: {workflow_id}")
    payload = {
        "schema": "web-registration-workflow/v1",
        "id": workflow_id,
        "label": label,
        "created_at": timestamp(),
        "updated_at": timestamp(),
        "steps": [
            {
                "name": name,
                "role": role,
                "human_only": human_only,
                "status": "pending",
                "owner": None,
                "lease_expires_at": None,
                "artifacts": [],
                "note": "",
            }
            for name, role, human_only in STEPS
        ],
    }
    write(workflow_id, payload)
    return summary(payload)


def step(payload: dict, name: str) -> tuple[int, dict]:
    for index, item in enumerate(payload["steps"]):
        if item["name"] == name:
            return index, item
    raise ValueError(f"unknown workflow step: {name}")


def claim(workflow_id: str, step_name: str, role: str, owner: str, lease_minutes: int) -> dict:
    if not owner.strip() or not 1 <= lease_minutes <= 240:
        raise ValueError("owner and a 1-240 minute lease are required")
    with Lock(workflow_id):
        payload = read(workflow_id)
        index, item = step(payload, step_name)
        if any(previous["status"] != "completed" for previous in payload["steps"][:index]):
            raise ValueError("previous workflow steps are incomplete")
        if role != item["role"]:
            raise ValueError(f"step requires role {item['role']}")
        expires = item.get("lease_expires_at")
        active = expires and datetime.fromisoformat(expires) > now()
        if active and item.get("owner") != owner:
            raise ValueError("step has an active owner lease")
        implementation_owner = payload["steps"][2].get("owner")
        if step_name in {"terra_implementation_review", "sol_security_review"}:
            if implementation_owner and implementation_owner == owner:
                raise ValueError("implementation owner cannot be the only reviewer")
        item.update(
            status="in_progress",
            owner=owner,
            lease_expires_at=timestamp(now() + timedelta(minutes=lease_minutes)),
        )
        payload["updated_at"] = timestamp()
        write(workflow_id, payload)
        return summary(payload)


def update(
    workflow_id: str,
    step_name: str,
    owner: str,
    status: str,
    artifact: str | None,
    note: str | None,
    human_approved_by: str | None,
) -> dict:
    if status not in {"in_progress", "completed", "blocked"}:
        raise ValueError("unsupported status")
    with Lock(workflow_id):
        payload = read(workflow_id)
        index, item = step(payload, step_name)
        if item.get("owner") != owner:
            raise ValueError("only the current owner may update the step")
        if any(previous["status"] != "completed" for previous in payload["steps"][:index]):
            raise ValueError("previous workflow steps are incomplete")
        if status == "completed" and not artifact:
            raise ValueError("completed step requires an evidence artifact")
        if item["human_only"] and status == "completed" and not human_approved_by:
            raise ValueError("deploy approval requires --human-approved-by")
        if artifact and artifact not in item["artifacts"]:
            item["artifacts"].append(artifact)
        item["status"] = status
        item["note"] = note or item["note"]
        if human_approved_by:
            item["human_approved_by"] = human_approved_by
        if status == "completed":
            item["completed_at"] = timestamp()
            item["lease_expires_at"] = None
        payload["updated_at"] = timestamp()
        write(workflow_id, payload)
        return summary(payload)


def summary(payload: dict) -> dict:
    next_item = next((item for item in payload["steps"] if item["status"] != "completed"), None)
    return {
        "workflow_id": payload["id"],
        "label": payload["label"],
        "next_step": next_item["name"] if next_item else None,
        "next_role": next_item["role"] if next_item else None,
        "complete": next_item is None,
        "manifest": str(path_for(payload["id"]).resolve()),
    }


def output(value: dict, as_json: bool) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")) if as_json else value)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Web project task workflow")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status"):
        command = sub.add_parser(name)
        if name == "status":
            command.add_argument("workflow_id")
        command.add_argument("--json", action="store_true")
    command = sub.add_parser("start")
    command.add_argument("--id", required=True)
    command.add_argument("--label", required=True)
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("claim")
    command.add_argument("workflow_id")
    command.add_argument("--step", required=True)
    command.add_argument("--role", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--lease-minutes", type=int, default=60)
    command.add_argument("--json", action="store_true")
    command = sub.add_parser("update")
    command.add_argument("workflow_id")
    command.add_argument("--step", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--status", required=True)
    command.add_argument("--artifact")
    command.add_argument("--note")
    command.add_argument("--human-approved-by")
    command.add_argument("--json", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "start":
            result = start(args.id, args.label)
        elif args.command == "claim":
            result = claim(args.workflow_id, args.step, args.role, args.owner, args.lease_minutes)
        elif args.command == "update":
            result = update(
                args.workflow_id,
                args.step,
                args.owner,
                args.status,
                args.artifact,
                args.note,
                args.human_approved_by,
            )
        else:
            payload = read(args.workflow_id)
            result = {**summary(payload), "steps": payload["steps"]}
        output(result, args.json)
        return 0 if result.get("status") != "failed" else 2
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        output({"status": "error", "error": str(exc)}, getattr(args, "json", False))
        return 2


if __name__ == "__main__":
    sys.exit(main())

