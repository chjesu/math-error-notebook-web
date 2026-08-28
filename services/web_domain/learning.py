"""Deterministic recommendation ranking and review scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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


def normalized_question_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"^\s*(?:题干|题目)\s*[:：]\s*", "", value)
    value = re.sub(r"^\s*\d+\s*[.、．]\s*", "", value)
    option = re.search(r"(?:^|\s)[a-f][.、．:：)]\s*", value)
    if option and option.start() > 10:
        value = value[:option.start()]
    value = re.sub(r"\\(?:left|right|displaystyle|textstyle|,|!)", "", value)
    value = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", value)
    value = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", value)
    return "".join(re.findall(r"[0-9a-z\u4e00-\u9fff+\-*/=<>≤≥√^_|]", value))


def normalized_answer_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = re.sub(r"^\s*(?:最终)?答案\s*[:：]\s*", "", value)
    value = re.sub(r"^\s*(?:故选|选择)\s*", "", value)
    value = re.sub(r"\\(?:left|right|displaystyle|textstyle|,|!)", "", value)
    value = re.sub(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"(\1)/(\2)", value)
    value = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", value)
    return "".join(re.findall(r"[0-9a-z\u4e00-\u9fff+\-*/=<>≤≥√^_|]", value))


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


def cross_validate_reference(reference: VerifiedQuestionReference, independent_answer: str) -> dict[str, object]:
    expected = normalized_answer_text(reference.answer_text)
    actual = normalized_answer_text(independent_answer)
    status = "consistent" if expected and actual and expected == actual else "conflict"
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
