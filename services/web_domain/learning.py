"""Deterministic recommendation ranking and review scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata


@dataclass(frozen=True)
class Question:
    question_id: str
    stem_text: str
    answer_text: str | None
    grade: int | None
    difficulty: float | None
    source_title: str
    solution_text: str | None = None
    version_id: str | None = None
    version_no: int = 1
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class VerifiedQuestionReference:
    question_id: str
    version_id: str
    version_no: int
    stem_text: str
    answer_text: str
    solution_text: str | None
    source_title: str
    match_score: float
    options: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Recommendation:
    recommendation_id: str
    user_id: str
    error_id: str
    question: Question
    reason: str
    status: str


@dataclass(frozen=True)
class ReviewTask:
    task_id: str
    user_id: str
    error_id: str
    stage: int
    due_at: datetime
    status: str


_STOP_HAN = set("的一是了在和与或若求已知则为中有其")
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_CHINA_TIMEZONE = timezone(timedelta(hours=8))
DAILY_GRADE_TARGET = 24
DAILY_GRADE_LIMIT = 40
DAILY_RECOMMENDATION_TARGET = 12
DAILY_RECOMMENDATION_LIMIT = 24


def review_requires_original(stage: int, latest_result: str | None) -> bool:
    """Redo the source problem early in the cycle or after a failed review."""
    return stage <= 2 or latest_result in {"partial", "wrong"}


def learning_day(now: datetime | None = None) -> str:
    """Return the account quota date in the product's fixed China timezone."""
    return (now or datetime.now(timezone.utc)).astimezone(_CHINA_TIMEZONE).date().isoformat()


def learning_usage_payload(day: str, grade_count: int, recommendation_count: int, pending_grade_count: int = 0) -> dict:
    def item(count: int, target: int, limit: int) -> dict:
        return {
            "count": count,
            "target": target,
            "limit": limit,
            "remaining": max(0, limit - count),
            "target_reached": count >= target,
            "limit_reached": count >= limit,
        }

    return {
        "date": day,
        "timezone": "Asia/Shanghai",
        "grade": item(grade_count, DAILY_GRADE_TARGET, DAILY_GRADE_LIMIT) | {"pending": pending_grade_count, "batch_grace": True},
        "recommendation": item(recommendation_count, DAILY_RECOMMENDATION_TARGET, DAILY_RECOMMENDATION_LIMIT),
    }


def _normalized_math_source(text: str) -> str:
    value = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+", lambda item: "^" + item.group().translate(_SUPERSCRIPT_DIGITS), text)
    return unicodedata.normalize("NFKC", value).replace("√", "sqrt")


def normalized_question_text(text: str) -> str:
    # Storage paths are not part of the mathematics visible in a paper photo.
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"【(?:原题图|题图|附图)】", "", text)
    value = _normalized_math_source(text).casefold()
    value = re.sub(r"^\s*(?:题干|题目)\s*[:：]\s*", "", value)
    value = re.sub(r"^\s*\d+\s*[.、．]\s*", "", value)
    option = re.search(r"(?:^|\s)[a-f][.、．:：)]\s*", value)
    if option and option.start() > 10:
        value = value[:option.start()]
    # OCR commonly drops purely presentational LaTeX commands while the
    # frozen question bank keeps them.  Normalize both spellings before the
    # conservative similarity check used by legacy PDF review codes.
    value = re.sub(r"\\(?:vec|overrightarrow|overleftarrow|widehat|hat|bar)\s*", "", value)
    greek = "alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|lambda|mu|nu|xi|omicron|pi|rho|sigma|tau|upsilon|phi|varphi|chi|psi|omega"
    value = re.sub(rf"\\({greek})\b", r"\1", value)
    for symbol, name in {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "θ": "theta", "λ": "lambda", "μ": "mu", "π": "pi", "φ": "phi", "ω": "omega"}.items():
        value = value.replace(symbol, name)
    value = re.sub(r"\\(?:angle|cdot|times|perp|parallel)\b", "", value)
    value = re.sub(r"\\(?:left|right|displaystyle|textstyle|,|!)", "", value)
    value = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", value)
    value = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", value)
    return "".join(re.findall(r"[0-9a-z\u4e00-\u9fff+\-*/=<>≤≥√^_|]", value))


def normalized_answer_text(text: str) -> str:
    value = _normalized_math_source(text).casefold()
    value = re.sub(r"^\s*(?:最终)?答案\s*[:：]\s*", "", value)
    value = re.sub(r"^\s*(?:故选|选择)\s*", "", value)
    value = re.sub(r"\\leq?\b", "≤", value)
    value = re.sub(r"\\geq?\b", "≥", value)
    value = re.sub(r"\\in\b", "∈", value)
    value = re.sub(r"\\(?:left|right|displaystyle|textstyle|,|!)", "", value)
    value = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", value)
    value = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", value)
    return "".join(re.findall(r"[0-9a-z\u4e00-\u9fff+\-*/=<>≤≥∈√^_|]", value))


def _canonical_answer_parts(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    value = _normalized_math_source(text).casefold()
    value = re.sub(r"\\(?:r\\n|n)(?=\s*\(\d{1,2}\))", "\n", value)
    markers = list(re.finditer(r"(?<![a-z0-9])\((\d{1,2})\)", value))
    parts = [
        (marker.group(1), value[marker.end(): markers[index + 1].start() if index + 1 < len(markers) else None])
        for index, marker in enumerate(markers)
    ] if markers else [("", value)]
    canonical: list[tuple[str, tuple[str, ...]]] = []
    for number, part in parts:
        part = re.sub(r"\\leq?\b", "≤", part)
        part = re.sub(r"\\geq?\b", "≥", part)
        part = re.sub(r"\\in\b", "∈", part)
        if "为" in part:
            prefix, suffix = part.rsplit("为", 1)
            if len(normalized_answer_text(prefix)) <= 24 and re.search(r"[0-9=<>≤≥∈]", suffix):
                part = suffix
        part = re.sub(r"([a-z])\s*∈\s*\[\s*([^,，]+?)\s*[,，]\s*([^\]]+?)\s*\]", r"\2≤\1≤\3", part)
        alternatives = tuple(sorted(filter(None, (normalized_answer_text(item) for item in re.split(r"(?:或者|或|\bor\b)", part)))))
        if alternatives:
            canonical.append((number, alternatives))
    return tuple(canonical)


def question_anchor(text: str) -> str | None:
    value = unicodedata.normalize("NFKC", text)
    phrases = re.findall(r"[\u4e00-\u9fff]{3,}", value)
    if phrases:
        return max(phrases, key=len)[:8]
    tokens = re.findall(r"[A-Za-z0-9]{4,}", value)
    return max(tokens, key=len)[:12] if tokens else None


def question_match_score(source: str, candidate: str) -> float:
    left, right = normalized_question_text(source), normalized_question_text(candidate)
    if not left or not right or min(len(left), len(right)) < 8:
        return 0.0
    length_ratio = min(len(left), len(right)) / max(len(left), len(right))
    if length_ratio < 0.8:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def question_number_tokens(text: str) -> tuple[str, ...]:
    """Keep numeric boundaries that disappear in compact LaTeX normalization."""
    value = _normalized_math_source(text)
    value = re.sub(r"\\frac\s*([0-9])\s*([0-9])", r"\1/\2", value)
    return tuple(re.findall(r"\d+(?:\.\d+)?", value))


def cross_validate_reference(reference: VerifiedQuestionReference, independent_answer: str) -> dict[str, object]:
    expected_parts = _canonical_answer_parts(reference.answer_text)
    actual_parts = _canonical_answer_parts(independent_answer)
    expected = json.dumps(expected_parts, ensure_ascii=False, separators=(",", ":"))
    actual = json.dumps(actual_parts, ensure_ascii=False, separators=(",", ":"))
    comparable_expected = expected_parts
    option_label = normalized_answer_text(reference.answer_text)
    if reference.options and len(option_label) == 1 and "a" <= option_label <= "z":
        option_index = ord(option_label) - ord("a")
        if option_index < len(reference.options):
            option_text = re.sub(
                r"^\s*[A-Z]\s*[.、．:：)]\s*", "", reference.options[option_index], flags=re.IGNORECASE,
            )
            comparable_expected = _canonical_answer_parts(option_text)
    status = "consistent" if comparable_expected and actual_parts and comparable_expected == actual_parts else "conflict"
    return {
        "schema": "question-bank-cross-validation/v1",
        "status": status,
        "question_id": reference.question_id,
        "version_id": reference.version_id,
        "version_no": reference.version_no,
        "source_title": reference.source_title,
        "match_score": round(reference.match_score, 4),
        "reference_answer_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        "independent_answer_sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest(),
    }


def reference_validation_from_evidence(evidence: str | None) -> dict[str, object] | None:
    if not evidence:
        return None
    try:
        value = json.loads(evidence)
    except (TypeError, json.JSONDecodeError):
        return None
    result = value.get("cross_validation") if isinstance(value, dict) else None
    return result if isinstance(result, dict) and result.get("schema") == "question-bank-cross-validation/v1" else None


def reference_adjudication_from_evidence(evidence: str | None) -> dict[str, object] | None:
    if not evidence:
        return None
    try:
        value = json.loads(evidence)
    except (TypeError, json.JSONDecodeError):
        return None
    result = value.get("reference_adjudication") if isinstance(value, dict) else None
    return result if isinstance(result, dict) and result.get("schema") == "question-bank-reference-adjudication/v1" else None


def reference_conflict_resolved(evidence: str | None) -> bool:
    validation = reference_validation_from_evidence(evidence)
    adjudication = reference_adjudication_from_evidence(evidence)
    return bool(
        validation
        and validation.get("status") == "conflict"
        and adjudication
        and adjudication.get("status") in {"consistent", "reference_preferred"}
        and adjudication.get("reference_answer_sha256") == validation.get("reference_answer_sha256")
        and adjudication.get("independent_answer_sha256") == validation.get("independent_answer_sha256")
    )


def math_tokens(text: str) -> set[str]:
    latin = set(re.findall(r"[a-z0-9]+", text.lower()))
    han = {character for character in text if "\u4e00" <= character <= "\u9fff" and character not in _STOP_HAN}
    return latin | han


def rank_questions(error_text: str, questions: list[Question], limit: int) -> list[tuple[Question, str]]:
    anchors = math_tokens(error_text)
    ranked: list[tuple[int, str, Question, str]] = []
    for question in questions:
        shared = sorted(anchors & math_tokens(question.stem_text))
        if not shared:
            continue
        reason = "题干共同要素：" + "、".join(shared[:3])
        ranked.append((-len(shared), question.question_id, question, reason))
    return [(question, reason) for _, _, question, reason in sorted(ranked)[: max(1, min(limit, 3))]]


def next_review(stage: int, result: str, completed_at: datetime) -> tuple[int, datetime] | None:
    if result not in {"correct", "partial", "wrong"} or not 1 <= stage <= 6:
        raise ValueError("invalid review result")
    if result == "correct":
        if stage == 6:
            return None
        target = stage + 1
        delay_days = {2: 1, 3: 3, 4: 7, 5: 14, 6: 30}[target]
    elif result == "partial":
        target, delay_days = stage, 1
    else:
        target, delay_days = max(1, stage - 1), 1
    return target, completed_at + timedelta(days=delay_days)


def calendar_month_range(month: str) -> tuple[datetime, datetime]:
    if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month) is None:
        raise ValueError("invalid calendar month")
    year, month_number = map(int, month.split("-"))
    start = datetime(year, month_number, 1, tzinfo=_CHINA_TIMEZONE)
    end = datetime(year + (month_number == 12), 1 if month_number == 12 else month_number + 1, 1, tzinfo=_CHINA_TIMEZONE)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def build_review_calendar(
    month: str,
    *,
    errors: list[dict[str, object]],
    review_tasks: list[dict[str, object]],
    review_attempts: list[dict[str, object]],
    total_error_count: int,
    now: datetime | None = None,
) -> dict[str, object]:
    start, end = calendar_month_range(month)
    current = _aware_utc(now or datetime.now(timezone.utc))
    days: dict[str, dict[str, object]] = {}
    summary = {
        "new_error_count": 0,
        "due_review_count": 0,
        "due_completed_count": 0,
        "completed_review_count": 0,
        "correct_review_count": 0,
        "needs_correction_count": 0,
        "overdue_review_count": 0,
    }

    def day_record(day_key: str) -> dict[str, object]:
        return days.setdefault(day_key, {
            "date": day_key,
            "new_error_count": 0,
            "due_review_count": 0,
            "completed_review_count": 0,
            "needs_correction_count": 0,
            "overdue_review_count": 0,
            "stage_counts": {str(stage): 0 for stage in range(1, 7)},
            "items": [],
        })

    def event(kind: str, when: datetime, record: dict[str, object]) -> dict[str, object]:
        moment = _aware_utc(when)
        return {
            "type": kind,
            "error_id": str(record["error_id"]),
            "question_text": str(record.get("question_text") or "未命名题目"),
            "first_error": record.get("first_error"),
            "knowledge_points": _knowledge_points(record.get("evidence")),
            "stage": record.get("stage"),
            "result": record.get("result"),
            "status": record.get("status"),
            "overdue": kind == "due" and record.get("status") in {"pending", "ready"} and moment < current,
            "original_due_date": moment.astimezone(_CHINA_TIMEZONE).date().isoformat() if kind == "due" else None,
        }

    def add_event(kind: str, when: datetime, record: dict[str, object]) -> None:
        moment = _aware_utc(when)
        if not start <= moment < end:
            return
        day = day_record(moment.astimezone(_CHINA_TIMEZONE).date().isoformat())
        day["items"].append(event(kind, when, record))
        if kind == "new":
            day["new_error_count"] += 1
            summary["new_error_count"] += 1
        elif kind == "due":
            day["due_review_count"] += 1
            summary["due_review_count"] += 1
            stage = str(record.get("stage"))
            if stage in day["stage_counts"]:
                day["stage_counts"][stage] += 1
            if record.get("status") == "completed":
                summary["due_completed_count"] += 1
            if record.get("status") in {"pending", "ready"} and moment < current:
                day["overdue_review_count"] += 1
                summary["overdue_review_count"] += 1
        else:
            day["completed_review_count"] += 1
            summary["completed_review_count"] += 1
            if record.get("result") == "correct":
                summary["correct_review_count"] += 1
            elif record.get("result") in {"partial", "wrong"}:
                day["needs_correction_count"] += 1
                summary["needs_correction_count"] += 1

    for item in errors:
        add_event("new", item["created_at"], item)
    for item in review_tasks:
        if item.get("status") != "cancelled":
            add_event("due", item["due_at"], item)
    for item in review_attempts:
        add_event("completed", item["completed_at"], item)

    # Reuse recorded due/completion pairs, not today's status, for past day-end
    # snapshots. Repeated stages can overwrite due_at; expose that gap rather
    # than inventing an old deadline using the current scheduling rules.
    attempts_by_task: dict[str, list[datetime]] = {}
    for item in review_attempts:
        attempts_by_task.setdefault(str(item.get("task_id")), []).append(_aware_utc(item["completed_at"]))
    backlog_items = []
    periods = []
    gaps = []
    for item in review_tasks:
        due = _aware_utc(item["due_at"])
        completions = sorted(attempts_by_task.get(str(item.get("task_id")), []))
        ended = next((value for value in completions if value >= due), None)
        status = item.get("status")
        if status == "completed" and ended is None and completions:
            ended = completions[-1]  # imported early completion: never an overdue period
        if status == "cancelled" or (status == "completed" and ended is None):
            gaps.append((due, current))
            continue
        if (status in {"pending", "ready"} and any(value < due for value in completions)) or len(completions) > 1:
            origin = _aware_utc(item.get("error_created_at") or min(due, *completions))
            gaps.append((origin, max(completions)))
        if due >= end or (ended is not None and ended < start):
            continue
        index = len(backlog_items)
        backlog_items.append(event("due", due, item))
        periods.append((index, due, ended))
    today = current.astimezone(_CHINA_TIMEZONE).date()
    cursor = start
    while cursor < end:
        date = cursor.astimezone(_CHINA_TIMEZONE).date()
        day = day_record(date.isoformat())
        day_end = cursor + timedelta(days=1)
        day["backlog_indices"] = [
            index for index, due, ended in periods
            if date < today and due < day_end and (ended is None or ended >= day_end)
        ]
        day["history_complete"] = date >= today or not any(a < day_end and b > cursor for a, b in gaps)
        cursor = day_end

    summary["planned_completion_percent"] = round(summary["due_completed_count"] * 100 / summary["due_review_count"]) if summary["due_review_count"] else 0
    summary["review_accuracy_percent"] = round(summary["correct_review_count"] * 100 / summary["completed_review_count"]) if summary["completed_review_count"] else 0
    return {"month": month, "total_error_count": total_error_count, "summary": summary, "days": [days[key] for key in sorted(days)], "backlog_items": backlog_items}


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _knowledge_points(value: object) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return []
    points = payload.get("knowledge_points") if isinstance(payload, dict) else None
    return [str(item) for item in points if isinstance(item, str) and item.strip()][:8] if isinstance(points, list) else []
