"""Deterministic paper identity, frozen requirements and review aggregation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any

from .learning import ReviewTask, question_match_score, learning_day, normalized_question_text


def review_locator(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"code", "pdf_id", "error_id", "question_id", "stage", "kind"}:
        raise ValueError("invalid review locator")
    result = {}
    for key, raw in value.items():
        if key == "stage":
            if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 6:
                raise ValueError("invalid review stage")
        elif not isinstance(raw, str) or len(raw) > 180:
            raise ValueError("invalid review locator")
        if raw:
            result[key] = raw.strip() if isinstance(raw, str) else raw
    if result.get("kind") not in {None, "original", "recommendation"}:
        raise ValueError("invalid review kind")
    return result


def build_manifest(job_id: str, items: list[dict], tasks: list[ReviewTask]) -> list[dict]:
    by_error = {task.error_id: task for task in tasks}
    recommended = {item["error_id"] for item in items if item["kind"] == "recommendation"}
    manifest = []
    for index, item in enumerate(items, 1):
        task = by_error.get(item["error_id"])
        code = f"R{job_id[:12]}-{index:02d}"
        item["review_code"] = code
        manifest.append({
            "code": code, "error_id": item["error_id"], "question_id": item.get("question_id"),
            "kind": item["kind"], "stem_text": item["stem_text"],
            "task_id": task.task_id if task else None,
            "stage": task.stage if task else item.get("review_stage", 0),
            "due_at": task.due_at.isoformat() if task else None,
            "required": item["kind"] == "recommendation" or bool(item.get("requires_original")) or item["error_id"] not in recommended,
        })
    return manifest


def legacy_manifest(text: str, job_id: str, errors: list, tasks: list[ReviewTask], recommendations: dict, generated_at: datetime) -> list[dict]:
    """Only parse printed IDs from a verified, owned stored PDF, never model text."""
    text = re.split(r"(?:^|\n)\s*答案\s*(?=\n|$)", text, maxsplit=1)[0]
    headers = list(re.finditer(r"错题编号\s*([\w-]+)\s*[（(]\s*第\s*(\d)\s*阶段([^）)]*)[）)]", text))
    task_map = {task.error_id: task for task in tasks}
    items, usable_tasks = [], []
    for index, header in enumerate(headers):
        printed_id, stage = header[1], int(header[2])
        matching = [error for error in errors if error.error_id == printed_id or error.error_id[:8] == printed_id or
                    hashlib.sha256(f"desktop-error:error:{error.user_id}:{printed_id}".encode()).hexdigest()[:32] == error.error_id]
        if len(matching) != 1:
            continue
        error = matching[0]
        task = task_map.get(error.error_id)
        # Legacy papers lack a due-time snapshot. Never attach them to a later
        # occurrence of the same (recycled) stage/task ID.
        if not task or task.stage != stage or task.due_at > generated_at:
            continue
        section = text[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(text)]
        refs = re.findall(r"题库编号\s*([\w-]+)", section)
        rows = []
        for ref in refs:
            ids = {ref, hashlib.sha256(f"question:{ref}".encode()).hexdigest()[:32]}
            matches = [rec for rec in recommendations.get(error.error_id, []) if rec.question.question_id in ids]
            if len(matches) != 1:
                break
            question = matches[0].question
            rows.append({"kind": "recommendation", "error_id": error.error_id, "question_id": question.question_id, "stem_text": question.stem_text})
        else:
            usable_tasks.append(task)
            items.append({"kind": "original", "error_id": error.error_id, "stem_text": error.question_text,
                          "requires_original": "仅作推荐依据" not in header[3], "review_stage": stage})
            items.extend(rows)
    return build_manifest(job_id, items, usable_tasks)


def matching_items(manifest: list[dict], locator: dict, question_text: str, user_id: str) -> list[dict]:
    matches = []
    for item in manifest:
        if locator.get("code") and locator["code"].lower() != item["code"].lower():
            continue
        if locator.get("stage") and locator["stage"] != item["stage"]:
            continue
        if locator.get("kind") and locator["kind"] != item["kind"]:
            continue
        if ref := locator.get("error_id"):
            mapped = hashlib.sha256(f"desktop-error:error:{user_id}:{ref}".encode()).hexdigest()[:32]
            if ref not in {item["error_id"], item["error_id"][:8]} and mapped != item["error_id"]:
                continue
        if ref := locator.get("question_id"):
            if item["question_id"] not in {ref, hashlib.sha256(f"question:{ref}".encode()).hexdigest()[:32]}:
                continue
        # A visible code is authoritative only after content is checked. This
        # prevents a mistyped code from grading a different paper's question.
        if question_match_score(question_text, item["stem_text"]) < 0.92:
            continue
        if re.findall(r"\d+(?:\.\d+)?", normalized_question_text(question_text)) != re.findall(r"\d+(?:\.\d+)?", normalized_question_text(item["stem_text"])):
            continue
        matches.append(item)
    return matches


def unresolved_receipt(message: str) -> dict:
    return {"status": "review_unmatched", "message": message, "review_status": "waiting_match"}


def apply_submission(checkpoint: dict, *, code: str, candidate_id: str, verdict: str, now: datetime, get_task, complete) -> dict:
    manifest = checkpoint.get("review_manifest", [])
    item = next((row for row in manifest if row["code"] == code), None)
    if not item:
        return unresolved_receipt("复习定位未确认，判题结果已保留；请提供 PDF 名称和图片中的错题编号、阶段或复习码。")
    task_id = item["task_id"]
    receipts = checkpoint.setdefault("review_receipts", {})
    # Replay the original receipt before consulting a possibly recycled task.
    if task_id in receipts:
        return receipts[task_id] | {"replayed": True}
    if not item["required"]:
        return {"status": "review_reference_only", "message": "本题仅作推荐依据，不要求重做，不推进复习阶段，也不重复入本。", "error_id": item["error_id"]}
    task = get_task(task_id) if task_id else None
    if (not task or task.status not in {"pending", "ready"} or task.stage != item["stage"]
            or task.due_at != datetime.fromisoformat(item["due_at"])):
        return {"status": "review_stale", "message": "判题结果已保留；这份 PDF 对应的复习任务已变更或结束，未改变当前阶段。请使用当前复习计划。", "error_id": item["error_id"]}
    if verdict not in {"correct", "partial", "incorrect"}:
        return {"status": "needs_review", "message": "复习题证据不足，判题结果已保留，尚未推进阶段。请补充清晰的作答。"}
    submissions = checkpoint.setdefault("review_submissions", {})
    # One frozen result per printed question. Retried photos cannot overwrite
    # an earlier result or make an incomplete review pass.
    submissions.setdefault(code, {"candidate_id": candidate_id, "verdict": verdict, "submitted_at": now.isoformat()})
    required = [row for row in manifest if row["task_id"] == task_id and row["required"]]
    received = [submissions[row["code"]] for row in required if row["code"] in submissions]
    base = {"error_id": item["error_id"], "completed_question_count": len(received), "required_question_count": len(required)}
    if len(received) != len(required):
        return base | {"status": "review_waiting", "message": f"复习作答已保存（{len(received)}/{len(required)} 道必做题），尚未推进阶段。下一步：继续上传该组剩余必做题。"}
    if task.due_at > now:
        return base | {"status": "review_waiting", "message": "作答已保存，但尚未到本阶段复习时间，未提前推进。到期后可在会话重试确认。"}
    values = {row["verdict"] for row in received}
    result = "wrong" if "incorrect" in values else "partial" if "partial" in values else "correct"
    next_task = complete(task_id, result, f"pdf-review-{checkpoint['review_job_id'][:32]}-{item['error_id'][:16]}", now)
    if next_task:
        due = next_task.due_at.astimezone(timezone.utc).isoformat()
        day = learning_day(next_task.due_at)
        next_message = f"下一次：{day}，第 {next_task.stage} 阶段。"
    else:
        due, next_message = None, "六阶段复习已完成，已标记掌握。"
    receipt = base | {
        "status": "review_completed" if result == "correct" else "review_needs_correction",
        "review_status": "completed", "review_result": result, "completed_at": now.isoformat(),
        "next_stage": next_task.stage if next_task else None, "next_due_at": due,
        "message": ("本组必做题已全部独立做对。" if result == "correct" else "本组复习已记录，仍需订正；请先对照错因和解析改错。") + next_message + " 未重复创建错题。",
    }
    receipts[task_id] = receipt
    return receipt
