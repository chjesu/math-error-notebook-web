"""Optional localhost Codex app-server adapter; it only returns uncommitted candidates."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable
import uuid

from scripts.codex_task_router import run_conversation_turn, run_review, run_structured_harness_turn, select


CAUSE_CODES = {
    "knowledge_gap", "concept_confusion", "formula_condition", "method_choice",
    "reasoning_gap", "algebra_transform", "calculation", "misreading",
    "incomplete_cases", "expression", "careless", "unclear",
}


class ModelUnavailableError(Exception):
    """The optional local model path failed without changing domain state."""

    def __init__(self, message: str, *, code: str = "model_unavailable") -> None:
        super().__init__(message)
        self.code = code


class CodexNotebookModel:
    def __init__(
        self,
        output_root: Path,
        *,
        review: Callable[..., dict[str, Any]] = run_review,
        harness_review: Callable[..., dict[str, Any]] = run_structured_harness_turn,
        conversation_review: Callable[..., dict[str, Any]] = run_conversation_turn,
        route_selector: Callable[[str, list[str]], dict[str, Any]] = select,
        max_active: int = 2,
    ) -> None:
        self.output_root = output_root.resolve()
        self.review = review
        self.harness_review = harness_review
        self.conversation_review = conversation_review
        self.route_selector = route_selector
        self.max_active = max_active
        self._active: set[tuple[str, str, int]] = set()
        self._active_lock = Lock()

    def extract(
        self,
        *,
        intake: Any,
        file_record: Any,
        image_path: Path,
        thread_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        frozen = {
            "intake_id": intake.intake_id,
            "input_version": intake.input_version,
            "media_type": file_record.media_type,
        }
        value = self._run("math-intake-adjudication", frozen, [image_path], thread_id, event_callback)
        result = value["result"]
        if result.get("intake_id") != intake.intake_id or result.get("input_version") != intake.input_version:
            raise ModelUnavailableError("model response does not match the frozen intake")
        status = result.get("status")
        if status not in {"complete", "unclear"}:
            raise ModelUnavailableError("model returned an unsupported intake status")
        raw_items = result.get("items")
        if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 20:
            raise ModelUnavailableError("model returned an invalid intake item list")
        items = []
        for source_item_no, raw_item in enumerate(raw_items, 1):
            if not isinstance(raw_item, dict) or raw_item.get("item_no") != source_item_no:
                raise ModelUnavailableError("model returned unordered intake items")
            item_status = raw_item.get("status")
            if item_status not in {"complete", "unclear"}:
                raise ModelUnavailableError("model returned an unsupported intake item status")
            question_text = self._text(raw_item.get("question_text"), "question_text", required=True)
            if self._is_supplementary_item(question_text):
                continue
            items.append({
                **raw_item,
                "item_no": len(items) + 1,
                "question_text": question_text,
                "answer_text": self._text(raw_item.get("answer_text"), "answer_text"),
            })
        if not items:
            raise ModelUnavailableError("model returned no target questions")
        return {
            **result,
            "items": items,
            "thread_id": value["thread_id"],
            "route": self._route_metadata(value),
        }

    def grade(
        self,
        *,
        attempt: Any,
        thread_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        frozen = {
            "attempt_id": attempt.attempt_id,
            "input_version": attempt.input_version,
            "question_text": attempt.question_text,
            "answer_text": attempt.answer_text,
            "evidence": {"source": "user_confirmed"},
        }
        value = self._run("math-grade-adjudication", frozen, [], thread_id, event_callback)
        result = value["result"]
        if result.get("attempt_id") != attempt.attempt_id or result.get("input_version") != attempt.input_version:
            raise ModelUnavailableError("model response does not match the frozen attempt")
        verdict = result.get("verdict")
        if verdict not in {"correct", "partial", "incorrect", "unclear"}:
            raise ModelUnavailableError("model returned an unsupported verdict")
        required = verdict in {"partial", "incorrect"}
        first_error = self._text(result.get("first_error"), "first_error", required=required) or None
        cause_code = result.get("cause_code")
        cause_evidence = self._text(result.get("cause_evidence"), "cause_evidence", required=required) or None
        correct_solution = self._text(result.get("correct_solution"), "correct_solution", required=required) or None
        final_answer = self._text(result.get("final_answer"), "final_answer", required=required) or None
        prevention_cue = self._text(result.get("prevention_cue"), "prevention_cue") or None
        if required and cause_code not in CAUSE_CODES:
            raise ModelUnavailableError("model returned an unsupported cause code")
        if not required:
            first_error = None
        return {
            **result,
            "first_error": first_error,
            "cause_code": cause_code if cause_code in CAUSE_CODES else None,
            "cause_evidence": cause_evidence,
            "correct_solution": correct_solution,
            "final_answer": final_answer,
            "prevention_cue": prevention_cue,
            "thread_id": value["thread_id"],
            "route": self._route_metadata(value),
        }

    def chat_turn(
        self,
        *,
        conversation_id: str,
        stage: str,
        resource_id: str,
        input_version: int,
        user_message: str,
        context: dict[str, Any],
        thread_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if stage not in {"intake", "grade"}:
            raise ModelUnavailableError("unsupported conversation stage")
        message = self._text(user_message, "user_message", required=True)
        frozen = {
            "conversation_id": conversation_id,
            "stage": stage,
            "resource_id": resource_id,
            "input_version": input_version,
            "user_message": message,
            "context": context,
        }
        key = ("math-notebook-loop", conversation_id, input_version)
        with self._active_lock:
            if key in self._active or len(self._active) >= self.max_active:
                raise ModelUnavailableError("model candidate generation is already in progress")
            self._active.add(key)
        output = self.output_root / f"math-notebook-loop-{uuid.uuid4().hex}.json"
        try:
            self.output_root.mkdir(parents=True, exist_ok=True)
            arguments = (
                self.route_selector("math-notebook-loop", []),
                json.dumps(frozen, ensure_ascii=False, separators=(",", ":")),
                output,
                thread_id,
            )
            value = self.conversation_review(*arguments, event_callback) if event_callback else self.conversation_review(*arguments)
            result = value["result"]
            if any((
                result.get("conversation_id") != conversation_id,
                result.get("stage") != stage,
                result.get("resource_id") != resource_id,
                result.get("input_version") != input_version,
            )):
                raise ModelUnavailableError("model response does not match the frozen conversation")
            parsed = self._validate_turn(result, stage)
            resolved_thread = value.get("thread_id") or value.get("session_id")
            if not isinstance(resolved_thread, str) or not resolved_thread:
                raise ModelUnavailableError("Codex app-server omitted the thread id")
            return {**parsed, "thread_id": resolved_thread, "route": self._route_metadata(value)}
        except ModelUnavailableError:
            raise
        except Exception as exc:
            raise ModelUnavailableError(
                "Codex app-server conversation turn failed",
                code=getattr(exc, "public_code", "model_unavailable"),
            ) from exc
        finally:
            output.unlink(missing_ok=True)
            with self._active_lock:
                self._active.discard(key)

    def _validate_turn(self, result: dict[str, Any], stage: str) -> dict[str, Any]:
        action = result.get("action")
        allowed = {"respond", "ready", "revise_intake" if stage == "intake" else "revise_grade"}
        if action not in allowed:
            raise ModelUnavailableError("model returned an unsupported conversation action")
        parsed = dict(result)
        parsed["assistant_message"] = self._text(result.get("assistant_message"), "assistant_message", required=True)
        confidence = result.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
            raise ModelUnavailableError("model returned invalid confidence")
        parsed["confidence"] = float(confidence)
        for name in ("question_text", "answer_text", "first_error", "cause_evidence", "correct_solution", "final_answer", "prevention_cue"):
            parsed[name] = self._text(result.get(name), name) or None
        if action == "revise_intake" and not parsed["question_text"]:
            raise ModelUnavailableError("model omitted required question_text")
        if action == "revise_grade":
            verdict = result.get("verdict")
            if verdict not in {"correct", "partial", "incorrect", "unclear"}:
                raise ModelUnavailableError("model returned an unsupported verdict")
            required = verdict in {"partial", "incorrect"}
            if required and (
                not parsed["first_error"]
                or result.get("cause_code") not in CAUSE_CODES
                or not parsed["cause_evidence"]
                or not parsed["correct_solution"]
                or not parsed["final_answer"]
            ):
                raise ModelUnavailableError("model omitted a complete grading diagnosis")
        return parsed

    def _run(
        self,
        task: str,
        frozen: dict[str, Any],
        images: list[Path],
        thread_id: str | None,
        event_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        resource_id = str(frozen.get("intake_id") or frozen.get("attempt_id") or "")
        key = (task, resource_id, int(frozen["input_version"]))
        with self._active_lock:
            if key in self._active or len(self._active) >= self.max_active:
                raise ModelUnavailableError("model candidate generation is already in progress")
            self._active.add(key)
        output = self.output_root / f"{task}-{uuid.uuid4().hex}.json"
        initial_output = output.with_suffix(output.suffix + ".initial.json")
        try:
            self.output_root.mkdir(parents=True, exist_ok=True)
            value = self.harness_review(
                self.route_selector(task, []),
                json.dumps(frozen, ensure_ascii=False, separators=(",", ":")),
                output,
                images,
                thread_id,
                event_callback,
            )
            resolved_thread = value.get("thread_id") or value.get("session_id")
            if not isinstance(resolved_thread, str) or not resolved_thread:
                raise ModelUnavailableError("Codex app-server omitted the thread id")
            return {**value, "thread_id": resolved_thread}
        except ModelUnavailableError:
            raise
        except Exception as exc:
            raise ModelUnavailableError(
                "Codex app-server candidate generation failed",
                code=getattr(exc, "public_code", "model_unavailable"),
            ) from exc
        finally:
            output.unlink(missing_ok=True)
            initial_output.unlink(missing_ok=True)
            with self._active_lock:
                self._active.discard(key)

    @staticmethod
    def _text(value: Any, name: str, *, required: bool = False) -> str:
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value.strip()
        else:
            raise ModelUnavailableError(f"model returned invalid {name}")
        if required and not text:
            raise ModelUnavailableError(f"model omitted required {name}")
        if len(text) > 12_000:
            raise ModelUnavailableError(f"model returned oversized {name}")
        return text

    @staticmethod
    def _is_supplementary_item(question_text: str) -> bool:
        compact = "".join(question_text.split()).casefold()
        return compact.startswith(("同类类型推荐题", "同类题推荐", "同类型推荐题", "相似题推荐", "推荐题", "题库编号q-"))

    @staticmethod
    def _route_metadata(value: dict[str, Any]) -> dict[str, Any]:
        route = value["route"]
        return {
            "task": route["task"],
            "model": route["model"],
            "reasoning_effort": route["reasoning_effort"],
            "escalated": "escalated_from" in value,
        }
