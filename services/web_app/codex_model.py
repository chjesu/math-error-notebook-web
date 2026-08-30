"""Optional localhost Codex app-server adapter; it only returns uncommitted candidates."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable
import uuid

from scripts.codex_task_router import compact_conversation, read_conversation_history, run_conversation_turn, run_structured_harness_turn, select
from services.web_domain.learning import VerifiedQuestionReference, cross_validate_reference
from .math_verifier import verify_equations


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
        review: Callable[..., dict[str, Any]] = run_structured_harness_turn,
        harness_review: Callable[..., dict[str, Any]] = run_structured_harness_turn,
        conversation_review: Callable[..., dict[str, Any]] = run_conversation_turn,
        history_reader: Callable[..., dict[str, Any]] = read_conversation_history,
        compactor: Callable[[str], dict[str, Any]] = compact_conversation,
        route_selector: Callable[[str, list[str]], dict[str, Any]] = select,
        max_active: int = 2,
    ) -> None:
        self.output_root = output_root.resolve()
        self.review = review
        self.harness_review = harness_review
        self.conversation_review = conversation_review
        self.history_reader = history_reader
        self.compactor = compactor
        self.route_selector = route_selector
        self.max_active = max_active
        self._active: set[tuple[str, str, int]] = set()
        self._grade_active: set[tuple[str, int]] = set()
        self._active_lock = Lock()

    def history(self, *, thread_id: str, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Translate persisted structured app-server items into product chat messages."""
        try:
            value = self.history_reader(thread_id, cursor, limit)
        except Exception as exc:
            raise ModelUnavailableError(
                "Codex app-server history read failed",
                code=getattr(exc, "public_code", "model_unavailable"),
            ) from exc
        entries = value.get("items") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            raise ModelUnavailableError("Codex app-server returned invalid history")
        messages: list[dict[str, str]] = []
        for entry in reversed(entries):
            item = entry.get("item") if isinstance(entry, dict) and isinstance(entry.get("item"), dict) else {}
            item_type = item.get("type")
            text = ""
            if item_type == "userMessage":
                content = item.get("content") if isinstance(item.get("content"), list) else []
                text = next((part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"), "")
            elif item_type == "agentMessage" and isinstance(item.get("text"), str):
                text = item["text"]
            else:
                continue
            packet = self._history_packet(text)
            field = "user_message" if item_type == "userMessage" else "assistant_message"
            message = packet.get(field) if isinstance(packet, dict) else None
            if isinstance(message, str) and message.strip():
                messages.append({"role": "user" if item_type == "userMessage" else "assistant", "text": message.strip()[:12_000]})
        next_cursor = value.get("next_cursor")
        return {
            "items": messages,
            "next_cursor": next_cursor if isinstance(next_cursor, str) and next_cursor else None,
        }

    def compact(self, *, thread_id: str) -> dict[str, Any]:
        """Compact a persisted thread without exposing its identifier to the browser."""
        try:
            value = self.compactor(thread_id)
        except Exception as exc:
            raise ModelUnavailableError(
                "Codex app-server compaction failed",
                code=getattr(exc, "public_code", "model_unavailable"),
            ) from exc
        if not isinstance(value, dict) or value.get("status") != "completed":
            raise ModelUnavailableError("Codex app-server returned invalid compaction status")
        return {"status": "completed"}

    @staticmethod
    def _history_packet(text: str) -> dict[str, Any]:
        candidate = text.rsplit("Review input:\n", 1)[-1].strip()
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

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
            question_text = self._text(raw_item.get("question_text"), "question_text")
            if not question_text:
                if item_status == "unclear":
                    continue
                raise ModelUnavailableError("model omitted required question_text")
            if self._is_supplementary_item(question_text):
                continue
            items.append({
                **raw_item,
                "item_no": len(items) + 1,
                "question_text": question_text,
                "answer_text": self._text(raw_item.get("answer_text"), "answer_text"),
            })
        if not items and status != "unclear":
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
        image_path: Path,
        thread_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        reference: VerifiedQuestionReference | None = None,
    ) -> dict[str, Any]:
        solution = self.solve(attempt=attempt, image_path=image_path)
        return self.grade_with_solution(
            attempt=attempt,
            image_path=image_path,
            solution=solution,
            thread_id=thread_id,
            event_callback=event_callback,
            reference=reference,
        )

    def solve(self, *, attempt: Any, image_path: Path) -> dict[str, Any]:
        """Freeze the independent solution separately so the batch can resume before grading."""
        difficulty = self._grade_difficulty(attempt.question_text)
        suffix = "" if difficulty == "normal" else f"-{difficulty}"
        solution = self._solve_independently(
            attempt=attempt,
            image_path=image_path,
            task=f"math-grade-solution{suffix}",
        )
        return {**solution, "difficulty": difficulty}

    def grade_with_solution(
        self,
        *,
        attempt: Any,
        image_path: Path,
        solution: dict[str, Any],
        thread_id: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        reference: VerifiedQuestionReference | None = None,
    ) -> dict[str, Any]:
        grade_key = (attempt.attempt_id, attempt.input_version)
        with self._active_lock:
            if grade_key in self._grade_active:
                raise ModelUnavailableError("model candidate generation is already in progress")
            self._grade_active.add(grade_key)
        try:
            difficulty = solution.get("difficulty")
            if difficulty not in {"normal", "hard", "max"}:
                raise ModelUnavailableError("independent solution returned invalid difficulty")
            if (
                not isinstance(solution.get("solution"), str)
                or not isinstance(solution.get("final_answer"), str)
                or not isinstance(solution.get("verification_checks"), list)
            ):
                raise ModelUnavailableError("independent solution is invalid")
            suffix = "" if difficulty == "normal" else f"-{difficulty}"
            frozen = {
                "attempt_id": attempt.attempt_id,
                "input_version": attempt.input_version,
                "question_text": attempt.question_text,
                "answer_text": attempt.answer_text,
                "evidence": {
                    "source": "user_confirmed_with_original_image",
                    "independent_solution": solution,
                    "verification_report": verify_equations(solution["verification_checks"]),
                    "difficulty": difficulty,
                },
            }
            if reference is not None:
                # The reference is introduced only after the independent solution is frozen.
                frozen["evidence"]["verified_question_reference"] = {
                    "question_id": reference.question_id,
                    "version_id": reference.version_id,
                    "version_no": reference.version_no,
                    "stem_text": reference.stem_text,
                    "answer_text": reference.answer_text,
                    "solution_text": reference.solution_text,
                    "source_title": reference.source_title,
                    "match_score": reference.match_score,
                }
            value = self._run(
                f"math-grade-adjudication{suffix}", frozen, [image_path], thread_id, event_callback,
            )
        finally:
            with self._active_lock:
                self._grade_active.discard(grade_key)
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
        knowledge_points = self._knowledge_points(result.get("knowledge_points"), required=required)
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
            "knowledge_points": knowledge_points,
            "correct_solution": correct_solution,
            "final_answer": final_answer,
            "prevention_cue": prevention_cue,
            "cross_validation": cross_validate_reference(reference, solution["final_answer"]) if reference is not None else None,
            "thread_id": value["thread_id"],
            "route": self._route_metadata(value),
        }

    def _solve_independently(self, *, attempt: Any, image_path: Path, task: str) -> dict[str, Any]:
        frozen = {
            "attempt_id": attempt.attempt_id,
            "input_version": attempt.input_version,
            "question_text": attempt.question_text,
            "evidence": {"source": "original_image"},
        }
        output = self.output_root / f"{task}-{uuid.uuid4().hex}.json"
        initial_output = output.with_suffix(output.suffix + ".initial.json")
        try:
            self.output_root.mkdir(parents=True, exist_ok=True)
            value = self.review(
                self.route_selector(task, []),
                json.dumps(frozen, ensure_ascii=False, separators=(",", ":")),
                output,
                [image_path],
                None,
                None,
            )
            result = value.get("result") if isinstance(value, dict) else None
            if not isinstance(result, dict) or result.get("attempt_id") != attempt.attempt_id or result.get("input_version") != attempt.input_version:
                raise ModelUnavailableError("independent solution does not match the frozen attempt")
            solution = self._text(result.get("solution"), "solution", required=True)
            final_answer = self._text(result.get("final_answer"), "final_answer", required=True)
            checks = self._verification_checks(result.get("verification_checks"))
            confidence = result.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
                raise ModelUnavailableError("independent solution returned invalid confidence")
            return {
                "solution": solution,
                "final_answer": final_answer,
                "verification_checks": checks,
                "confidence": float(confidence),
                "route": self._route_metadata(value),
            }
        except ModelUnavailableError:
            raise
        except Exception as exc:
            raise ModelUnavailableError(
                "independent math solution failed",
                code=getattr(exc, "public_code", "model_unavailable"),
            ) from exc
        finally:
            output.unlink(missing_ok=True)
            initial_output.unlink(missing_ok=True)

    @staticmethod
    def _grade_difficulty(question_text: str) -> str:
        compact = "".join(question_text.split()).casefold()
        maximum = ("证明", "空间几何", "立体几何", "二面角", "存在性")
        hard = ("如图", "图中", "解析几何", "圆锥曲线", "函数综合", "数列综合", "最值", "轨迹")
        if any(keyword in compact for keyword in maximum):
            return "max"
        if any(keyword in compact for keyword in hard):
            return "hard"
        return "normal"

    @staticmethod
    def _verification_checks(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > 8:
            raise ModelUnavailableError("independent solution returned invalid verification checks")
        checks = []
        for check in value:
            if not isinstance(check, dict) or set(check) != {"left", "right", "variables"}:
                raise ModelUnavailableError("independent solution returned invalid verification checks")
            left, right, variables = check["left"], check["right"], check["variables"]
            if (
                not isinstance(left, str) or not 1 <= len(left) <= 200
                or not isinstance(right, str) or not 1 <= len(right) <= 200
                or not isinstance(variables, list) or len(variables) > 3
                or any(not isinstance(name, str) or len(name) != 1 or not "a" <= name <= "z" for name in variables)
                or len(variables) != len(set(variables))
            ):
                raise ModelUnavailableError("independent solution returned invalid verification checks")
            checks.append({"left": left, "right": right, "variables": list(variables)})
        return checks

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
        cancel_event: Event | None = None,
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
            if cancel_event is not None:
                value = self.conversation_review(*arguments, event_callback, cancel_event)
            elif event_callback:
                value = self.conversation_review(*arguments, event_callback)
            else:
                value = self.conversation_review(*arguments)
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
        parsed["knowledge_points"] = self._knowledge_points(result.get("knowledge_points"), required=False)
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
                or not parsed["knowledge_points"]
                or not parsed["correct_solution"]
                or not parsed["final_answer"]
            ):
                raise ModelUnavailableError("model omitted a complete grading diagnosis")
        return parsed

    @staticmethod
    def _knowledge_points(value: Any, *, required: bool) -> list[str]:
        if not isinstance(value, list) or len(value) > 8:
            raise ModelUnavailableError("model returned invalid knowledge points")
        points = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 200:
                raise ModelUnavailableError("model returned invalid knowledge points")
            points.append(item.strip())
        if required and not points:
            raise ModelUnavailableError("model omitted required knowledge points")
        return points

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
        metadata = {
            "task": route["task"],
            "model": route["model"],
            "reasoning_effort": route["reasoning_effort"],
            "escalated": "escalated_from" in value,
        }
        for name in ("provider", "runtime", "version"):
            if name in route:
                metadata[name] = route[name]
        return metadata
