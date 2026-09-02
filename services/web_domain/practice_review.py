"""Deterministic paper identity, frozen requirements and review aggregation."""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from .learning import ReviewTask, question_match_score, learning_day, normalized_question_text


def review_item_key(item: dict) -> str | None:
    """Same printed exercise in the same frozen round, independent of PDF/code."""
    if not all(item.get(key) for key in ("error_id", "task_id", "due_at", "stem_text")):
        return None
    if item.get("kind") not in {"original", "recommendation"}:
        return None
    if item["kind"] == "recommendation" and not item.get("question_id"):
        return None
    try:
        due = datetime.fromisoformat(item["due_at"])
        if due.tzinfo is None:
            return None
        identity = [item["error_id"], item["task_id"], item["stage"], due.astimezone(timezone.utc).isoformat(),
                    item["kind"], item.get("question_id"), normalized_question_text(item["stem_text"])]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    except (ValueError, TypeError, KeyError):
        return None


def review_group_key(manifest: list[dict], task_id: str) -> tuple:
    rows = [item for item in manifest if item.get("task_id") == task_id and item.get("required")]
    keys = [review_item_key(item) for item in rows]
    return tuple(sorted(keys)) if keys and all(keys) else ()


def shared_review_checkpoints(checkpoints: dict[str, dict]) -> dict[str, dict]:
    """Read/transaction helper. Caller must supply only one account's live PDFs.

    Keep original timestamps and the first accepted grade; never merge rounds
    or copy a group receipt to a reprint with different required exercises.
    """
    result = deepcopy(checkpoints)
    submissions, receipts = {}, {}
    for checkpoint in result.values():
        manifest = checkpoint.get("review_manifest") or []
        for item in manifest:
            key = review_item_key(item)
            saved = (checkpoint.get("review_submissions") or {}).get(item.get("code"))
            if not key or not item.get("required") or not saved or saved.get("verdict") not in {"correct", "partial", "incorrect"} or not saved.get("candidate_id"):
                continue
            try:
                moment = datetime.fromisoformat(saved["submitted_at"])
                if moment.tzinfo is None:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            rank = (-int(saved.get("revision", 0)), moment, saved["candidate_id"])
            if key not in submissions or rank < submissions[key][0]:
                submissions[key] = (rank, saved | {"source_code": saved.get("source_code") or item["code"]})
        for task_id, receipt in (checkpoint.get("review_receipts") or {}).items():
            key = review_group_key(manifest, task_id)
            if key and receipt.get("status") in {"review_completed", "review_needs_correction"}:
                if key not in receipts or receipt.get("completed_at", "") < receipts[key].get("completed_at", ""):
                    receipts[key] = receipt
    for checkpoint in result.values():
        manifest = checkpoint.get("review_manifest") or []
        for item in manifest:
            key = review_item_key(item)
            if item.get("required") and key in submissions:
                checkpoint.setdefault("review_submissions", {})[item["code"]] = deepcopy(submissions[key][1])
        for task_id in {item.get("task_id") for item in manifest}:
            key = review_group_key(manifest, task_id)
            if key in receipts:
                checkpoint.setdefault("review_receipts", {})[task_id] = deepcopy(receipts[key])
    return result


def practice_paper_progress(papers: list[dict]) -> list[dict]:
    checkpoints = shared_review_checkpoints({paper["task_id"]: paper.get("_checkpoint") or {} for paper in papers})
    result = []
    for paper in papers:
        checkpoint = checkpoints[paper["task_id"]]
        manifest = checkpoint.get("review_manifest") or []
        rows, seen = [], set()
        for item in manifest:
            key = review_item_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            saved = (checkpoint.get("review_submissions") or {}).get(item["code"]) if item.get("required") else None
            verdict = saved.get("verdict") if saved else None
            rows.append({"item_id": key, "review_code": item["code"], "task_id": item["task_id"],
                         "error_id": item["error_id"], "question_id": item.get("question_id"),
                         "question_text": item["stem_text"], "kind": item["kind"], "stage": item["stage"],
                         "required": bool(item.get("required")), "status": "reference_only" if not item.get("required") else "correct" if verdict == "correct" else "needs_correction" if verdict in {"partial", "incorrect"} else "pending",
                         "verdict": verdict, "submitted_at": saved.get("submitted_at") if saved else None,
                         "inherited_from_code": saved.get("source_code") if saved and saved.get("source_code") != item["code"] else None})
        required = [row for row in rows if row["required"]]
        answered = [row for row in required if row["submitted_at"]]
        groups = []
        for task_id in dict.fromkeys(item.get("task_id") for item in manifest):
            group_items = [item for item in manifest if item.get("task_id") == task_id]
            keys = set(review_group_key(manifest, task_id))
            if not keys:
                continue
            group_rows = [row for row in rows if row["item_id"] in keys]
            receipt = (checkpoint.get("review_receipts") or {}).get(task_id, {})
            groups.append({"task_id": task_id, "error_id": group_items[0]["error_id"], "stage": group_items[0]["stage"],
                           "answered_count": sum(bool(row["submitted_at"]) for row in group_rows), "required_count": len(keys),
                           "completed": receipt.get("status") in {"review_completed", "review_needs_correction"}})
        progress = {"available": bool(manifest) and len(seen) == len(manifest), "required_count": len(required),
                    "answered_count": len(answered), "pending_count": len(required) - len(answered),
                    "correct_count": sum(row["status"] == "correct" for row in answered),
                    "needs_correction_count": sum(row["status"] == "needs_correction" for row in answered),
                    "completed_group_count": sum(group["completed"] for group in groups), "group_count": len(groups),
                    "groups": groups, "items": rows}
        signature = sorted((row["item_id"], row["required"]) for row in rows)
        plan_id = hashlib.sha256(json.dumps(signature).encode()).hexdigest() if progress["available"] else paper["task_id"]
        result.append({key: value for key, value in paper.items() if key != "_checkpoint"} |
                      {"plan_id": plan_id, "progress": progress})
    return result


def review_code_state(papers: list[dict], code: str) -> dict | None:
    """Canonical item/group state; callers never need to infer it from PDF history."""
    matches = [(paper, row) for paper in papers for row in paper.get("progress", {}).get("items", [])
               if str(row.get("review_code", "")).lower() == code.lower()]
    if len(matches) != 1:
        return None
    paper, item = matches[0]
    group_rows = [row for row in paper["progress"]["items"]
                  if row.get("task_id") == item.get("task_id") and row.get("required")]
    group = next((row for row in paper["progress"].get("groups", []) if row.get("task_id") == item.get("task_id")), {})
    pending = [{"review_code": row["review_code"], "kind": row["kind"], "question_id": row.get("question_id"),
                "question_text": row["question_text"]} for row in group_rows if row["status"] == "pending"]
    if item["status"] == "pending":
        action = "upload_this_item"
    elif item["status"] == "needs_correction":
        action = "upload_correction"
    elif pending:
        action = "submit_remaining_required"
    elif group.get("completed"):
        action = "already_completed"
    else:
        action = "retry_group_confirmation"
    return {
        "review_code": item["review_code"], "status": item["status"], "verdict": item.get("verdict"),
        "inherited_from_code": item.get("inherited_from_code"), "error_id": item["error_id"],
        "question_id": item.get("question_id"), "kind": item["kind"], "stage": item["stage"],
        "answered_count": group.get("answered_count", 0), "required_count": group.get("required_count", len(group_rows)),
        "group_completed": bool(group.get("completed")), "pending_items": pending,
        "recommended_action": action,
    }


def add_practice_calendar(calendar: dict, papers: list[dict]) -> dict:
    """PDF progress is current; activity is attributed to the actual local day."""
    days = {day["date"]: day for day in calendar["days"]}
    for day in days.values():
        day.update(practice_plans=[], practice_activity=[], submitted_question_count=0,
                   paper_answered_count=0, paper_required_count=0)
    seen_plans, seen_activity, planned_items = set(), set(), {}
    for paper in papers:
        if paper.get("generated_at"):
            date = learning_day(datetime.fromisoformat(paper["generated_at"]))
            key = (date, paper["plan_id"])
            if date in days and key not in seen_plans:
                seen_plans.add(key)
                days[date]["practice_plans"].append(paper)
                items = planned_items.setdefault(date, {})
                items.update({row["item_id"]: row for row in paper["progress"]["items"] if row["required"]})
        for row in paper["progress"]["items"]:
            if not row["submitted_at"] or row["item_id"] in seen_activity:
                continue
            seen_activity.add(row["item_id"])
            date = learning_day(datetime.fromisoformat(row["submitted_at"]))
            if date in days:
                days[date]["practice_activity"].append(row | {"filename": paper["filename"]})
                days[date]["submitted_question_count"] += 1
    for date, items in planned_items.items():
        days[date]["paper_answered_count"] = sum(bool(row["submitted_at"]) for row in items.values())
        days[date]["paper_required_count"] = len(items)
    calendar["summary"]["submitted_question_count"] = sum(day["submitted_question_count"] for day in days.values())
    return calendar


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
        base = f"R{job_id[:12]}-{index:02d}"
        checksum = hashlib.sha256(
            f"{base}:{item['error_id']}:{item.get('question_id') or ''}:{item['kind']}".encode("utf-8")
        ).hexdigest()[:6].upper()
        code = f"{base}-{checksum}"
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


def fixed_plan_items(manifest: list[dict], completions: list[dict], tasks: list[ReviewTask]) -> list[dict]:
    """Read-only status of the frozen rounds, including completion on another PDF."""
    by_task = {task.task_id: task for task in tasks}
    groups = {row["error_id"]: row for row in reversed(manifest)}
    items = []
    for error_id, row in reversed(list(groups.items())):
        due = datetime.fromisoformat(row["due_at"]) if row.get("due_at") else None
        finished = sorted((entry for entry in completions if due and entry["task_id"] == row.get("task_id")
                           and entry["stage"] == row["stage"] and entry["completed_at"] >= due), key=lambda entry: entry["completed_at"])
        current = by_task.get(row.get("task_id"))
        state, result, completed_at = "unavailable", None, None
        if finished:
            result = finished[0]["result"]
            completed_at = finished[0]["completed_at"].isoformat()
            state = "completed" if result == "correct" else "needs_correction"
        elif current and current.error_id == error_id and current.stage == row["stage"] and current.due_at == due:
            state = "pending"
        items.append({"error_id": error_id, "stage": row["stage"], "status": state, "result": result, "completed_at": completed_at})
    return items


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


def checked_review_code(value: str) -> bool:
    return bool(re.fullmatch(r"R[0-9a-f]{12}-\d{2}-[0-9A-F]{6}", value, flags=re.IGNORECASE))


def identity_matching_items(manifest: list[dict], locator: dict, user_id: str) -> list[dict]:
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
        matches.append(item)
    return matches


def matching_items(manifest: list[dict], locator: dict, question_text: str, user_id: str) -> list[dict]:
    matches = []
    for item in identity_matching_items(manifest, locator, user_id):
        # A checksummed code, or a question-bank identifier whose uniqueness is
        # checked across this account's frozen papers by the caller, is a stable
        # identity. Legacy codes still require the conservative content check.
        if checked_review_code(str(locator.get("code") or "")) or locator.get("question_id"):
            matches.append(item)
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


def apply_submission(checkpoint: dict, *, code: str, candidate_id: str, verdict: str, now: datetime, get_task, complete,
                     allow_correction: bool = False) -> dict:
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
    # Ordinary retries are idempotent. An explicit correction may replace an
    # unfinished-stage result while preserving its audit trail.
    saved = submissions.get(code)
    if saved is None:
        submissions[code] = {"candidate_id": candidate_id, "verdict": verdict, "submitted_at": now.isoformat(), "revision": 0}
    elif allow_correction and saved.get("candidate_id") != candidate_id:
        history = list(saved.get("history") or [])
        history.append({
            "candidate_id": saved.get("candidate_id"), "verdict": saved.get("verdict"),
            "submitted_at": saved.get("corrected_at") or saved.get("submitted_at"),
        })
        submissions[code] = {
            "candidate_id": candidate_id, "verdict": verdict, "submitted_at": saved["submitted_at"],
            "corrected_at": now.isoformat(), "previous_candidate_id": saved.get("candidate_id"),
            "previous_verdict": saved.get("verdict"), "revision": int(saved.get("revision", 0)) + 1,
            "history": history,
        }
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
