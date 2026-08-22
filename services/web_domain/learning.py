"""Deterministic recommendation ranking and review scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re


@dataclass(frozen=True)
class Question:
    question_id: str
    stem_text: str
    answer_text: str | None
    grade: int | None
    difficulty: float | None
    source_title: str


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
