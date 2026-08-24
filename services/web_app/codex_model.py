"""Optional localhost Codex CLI adapter; it can only return uncommitted candidates."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable
import uuid

from scripts.codex_task_router import run_review, select


CAUSE_CODES = {
    "knowledge_gap", "concept_confusion", "formula_condition", "method_choice",
    "reasoning_gap", "algebra_transform", "calculation", "misreading",
    "incomplete_cases", "expression", "careless", "unclear",
}


class ModelUnavailableError(Exception):
    """The optional local model path failed without changing domain state."""


class CodexNotebookModel:
    def __init__(
        self,
        output_root: Path,
        *,
        review: Callable[..., dict[str, Any]] = run_review,
        route_selector: Callable[[str, list[str]], dict[str, Any]] = select,
        max_active: int = 2,
    ) -> None:
        self.output_root = output_root.resolve()
        self.review = review
        self.route_selector = route_selector
        self.max_active = max_active
        self._active: set[tuple[str, str, int]] = set()
        self._active_lock = Lock()

    def extract(self, *, intake: Any, file_record: Any, image_path: Path) -> dict[str, Any]:
        frozen = {
            "intake_id": intake.intake_id,
            "input_version": intake.input_version,
            "media_type": file_record.media_type,
        }
        value = self._run("math-intake-candidate", frozen, [image_path])
        result = value["result"]
        if result.get("intake_id") != intake.intake_id or result.get("input_version") != intake.input_version:
            raise ModelUnavailableError("model response does not match the frozen intake")
        status = result.get("status")
        question = self._text(result.get("question_text"), "question_text", required=status == "complete")
        answer = self._text(result.get("answer_text"), "answer_text")
        if status not in {"complete", "unclear"}:
            raise ModelUnavailableError("model returned an unsupported intake status")
        return {
            **result,
            "question_text": question,
            "answer_text": answer,
            "route": self._route_metadata(value),
        }

    def grade(self, *, attempt: Any) -> dict[str, Any]:
        frozen = {
            "attempt_id": attempt.attempt_id,
            "input_version": attempt.input_version,
            "question_text": attempt.question_text,
            "answer_text": attempt.answer_text,
            "evidence": {"source": "user_confirmed"},
        }
        value = self._run("math-grade-candidate", frozen, [])
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
            "route": self._route_metadata(value),
        }

    def _run(self, task: str, frozen: dict[str, Any], images: list[Path]) -> dict[str, Any]:
        resource_id = str(frozen.get("intake_id") or frozen.get("attempt_id") or "")
        key = (task, resource_id, int(frozen["input_version"]))
        with self._active_lock:
            if key in self._active or len(self._active) >= self.max_active:
                raise ModelUnavailableError("Codex CLI candidate generation is already in progress")
            self._active.add(key)
        output = self.output_root / f"{task}-{uuid.uuid4().hex}.json"
        initial_output = output.with_suffix(output.suffix + ".initial.json")
        try:
            self.output_root.mkdir(parents=True, exist_ok=True)
            return self.review(
                self.route_selector(task, []),
                json.dumps(frozen, ensure_ascii=False, separators=(",", ":")),
                output,
                images,
            )
        except ModelUnavailableError:
            raise
        except Exception as exc:
            raise ModelUnavailableError("Codex CLI candidate generation failed") from exc
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
    def _route_metadata(value: dict[str, Any]) -> dict[str, Any]:
        route = value["route"]
        return {
            "task": route["task"],
            "model": route["model"],
            "reasoning_effort": route["reasoning_effort"],
            "escalated": "escalated_from" in value,
        }
