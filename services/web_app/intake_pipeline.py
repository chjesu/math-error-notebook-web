"""Resumable stage processor for uploaded math-work images."""

from __future__ import annotations

import json
import hashlib
import re
from typing import Any

from services.web_domain import NotebookService, cross_validate_reference, reference_validation_from_evidence
from services.web_domain.intake_batch import BatchClaim, IntakeBatchFailure
from .codex_model import ModelUnavailableError


class NotebookIntakeBatchProcessor:
    """Run model work outside transactions and commit only fenced operation receipts."""

    def __init__(self, notebook: NotebookService, model_runner: Any) -> None:
        self.notebook = notebook
        self.model_runner = model_runner

    def process_stage(self, stage: str, claim: BatchClaim, repository) -> int | None:
        try:
            if stage == "slicing":
                return self._slice(claim, repository)
            if stage == "solving":
                self._solve(claim, repository)
                return None
            if stage == "grading":
                self._grade(claim, repository)
                return None
            raise IntakeBatchFailure("invalid_batch_stage", retryable=False)
        except IntakeBatchFailure:
            raise
        except ModelUnavailableError as exc:
            raise IntakeBatchFailure(exc.code, retryable=True) from exc
        except LookupError as exc:
            raise IntakeBatchFailure("input_unavailable", retryable=False) from exc
        except RuntimeError as exc:
            if str(exc) in {"daily_grade_limit", "reference_conflict", "reference_state_changed"}:
                raise IntakeBatchFailure(str(exc), retryable=False) from exc
            raise

    def _slice(self, claim: BatchClaim, repository) -> int:
        total_items = 0
        for snapshot in repository.list_files(batch_id=claim.batch_id):
            operation_key = f"slice:{snapshot.ordinal}"
            existing = repository.get_operation(claim, operation_key)
            if existing is not None:
                total_items += len(existing.result.get("intake_ids", ()))
                continue
            file_record = self._current_file(claim, repository, snapshot.file_id)
            intakes = self.notebook.store.get_file_intakes(user_id=claim.user_id, file_id=snapshot.file_id)
            if intakes:
                primary = intakes[0]
            else:
                primary = self.notebook.store.create_intake(
                    user_id=claim.user_id,
                    file_id=snapshot.file_id,
                    idempotency_key=f"batch-slice-{claim.batch_id}-{snapshot.ordinal}",
                    batch_claim=claim,
                    batch_stage="slicing",
                )[0]
            result = None
            candidates = None
            if primary.status == "extracting":
                with self.notebook.files.model_preview(snapshot.object_key, snapshot.content_sha256) as image_path:
                    result = self.model_runner.extract(
                        intake=primary,
                        file_record=file_record,
                        image_path=image_path,
                        thread_id=self.notebook.store.get_codex_thread(
                            user_id=claim.user_id, conversation_id=primary.intake_id,
                        ),
                    )
                self._current_file(claim, repository, snapshot.file_id)
                raw_items = result.get("items")
                if not isinstance(raw_items, list):
                    raise ModelUnavailableError("model returned an invalid intake item list")
                candidates = [
                    {
                        "item_no": index,
                        "question_text": str(item.get("question_text") or "").strip(),
                        "answer_text": str(item.get("answer_text") or "").strip(),
                    }
                    for index, item in enumerate(raw_items, 1)
                    if isinstance(item, dict) and str(item.get("question_text") or "").strip()
                ]
                if not candidates:
                    raise ModelUnavailableError("model returned no target questions")
            elif all(item.status == "waiting_confirmation" for item in intakes):
                pass
            else:
                raise IntakeBatchFailure("intake_state_conflict", retryable=False)

            def commit_slice() -> tuple[dict[str, Any], None]:
                saved = self.notebook.store.get_file_intakes(user_id=claim.user_id, file_id=snapshot.file_id)
                if saved and all(item.status == "waiting_confirmation" for item in saved):
                    pass
                elif saved and saved[0].status == "extracting" and candidates is not None and result is not None:
                    saved = self.notebook.store.save_extraction_candidates(
                        user_id=claim.user_id,
                        intake_id=saved[0].intake_id,
                        items=candidates,
                        evidence={"source": "intake_batch", "batch_id": claim.batch_id, "route": result.get("route")},
                        batch_claim=claim,
                        batch_stage="slicing",
                    )
                else:
                    raise IntakeBatchFailure("intake_state_conflict", retryable=False)
                thread_id = result.get("thread_id") if result is not None else None
                if isinstance(thread_id, str) and thread_id:
                    self.notebook.store.save_codex_thread(
                        user_id=claim.user_id, conversation_id=saved[0].intake_id, thread_id=thread_id,
                        batch_claim=claim, batch_stage="slicing",
                    )
                return {"intake_ids": [item.intake_id for item in saved]}, None

            operation, _ = repository.run_fenced_operation(
                claim,
                operation_key=operation_key,
                stage="slicing",
                ordinal=snapshot.ordinal,
                action=commit_slice,
                completed_files_delta=1,
            )
            total_items += len(operation.result["intake_ids"])
        return total_items

    def _solve(self, claim: BatchClaim, repository) -> None:
        intake_ids = self._intake_ids(claim, repository)
        self.notebook.store.reserve_grade_batch(
            user_id=claim.user_id,
            intake_ids=intake_ids,
            batch_claim=claim,
            batch_stage="solving",
        )
        for ordinal, intake_id in enumerate(intake_ids, 1):
            operation_key = f"solve:{ordinal}"
            if repository.get_operation(claim, operation_key) is not None:
                continue
            intake = self.notebook.store.get_intake(user_id=claim.user_id, intake_id=intake_id)
            if intake is None:
                raise LookupError("intake not found")
            attempt_id = self.notebook.store.confirm_intake(
                user_id=claim.user_id,
                intake_id=intake_id,
                expected_version=intake.input_version,
                idempotency_key=f"batch-solve-{claim.batch_id}-{ordinal}",
                batch_claim=claim,
                batch_stage="solving",
            )[0]
            attempt = self.notebook.store.get_attempt(user_id=claim.user_id, attempt_id=attempt_id)
            file_record = self._current_file(claim, repository, intake.file_id)
            if attempt is None:
                raise LookupError("attempt input not found")
            with self.notebook.files.model_preview(file_record.object_key, file_record.content_sha256) as image_path:
                solution = self.model_runner.solve(attempt=attempt, image_path=image_path)
            self._current_file(claim, repository, intake.file_id)
            solution = self._freeze_solution(solution)
            repository.run_fenced_operation(
                claim,
                operation_key=operation_key,
                stage="solving",
                ordinal=ordinal,
                action=lambda: ({"intake_id": intake_id, "attempt_id": attempt_id, "solution": solution}, None),
            )

    def _grade(self, claim: BatchClaim, repository) -> None:
        intake_ids = self._intake_ids(claim, repository)
        for ordinal, intake_id in enumerate(intake_ids, 1):
            operation_key = f"grade:{ordinal}"
            if repository.get_operation(claim, operation_key) is not None:
                continue
            solved = repository.get_operation(claim, f"solve:{ordinal}")
            if solved is None:
                raise IntakeBatchFailure("solution_missing", retryable=False)
            solution = solved.result.get("solution")
            if not isinstance(solution, dict):
                raise IntakeBatchFailure("solution_missing", retryable=False)
            solution = self._freeze_solution(solution)
            independent_answer = self._independent_answer(solution)
            solve_sha256 = self._solution_sha256(solution)
            attempt_id = str(solved.result["attempt_id"])
            attempt = self.notebook.store.get_attempt(user_id=claim.user_id, attempt_id=attempt_id)
            intake = self.notebook.store.get_intake(user_id=claim.user_id, intake_id=intake_id)
            if attempt is None or intake is None:
                raise LookupError("attempt not found")
            self._current_file(claim, repository, intake.file_id)
            recovered = self.notebook.store.find_grade_candidate_for_attempt(
                user_id=claim.user_id,
                attempt_id=attempt_id,
                batch_id=claim.batch_id,
                operation_key=operation_key,
                solve_sha256=solve_sha256,
            )
            if recovered is not None:
                def commit_recovered() -> tuple[dict[str, Any], dict[str, Any]]:
                    candidate = self.notebook.store.find_grade_candidate_for_attempt(
                        user_id=claim.user_id,
                        attempt_id=attempt_id,
                        batch_id=claim.batch_id,
                        operation_key=operation_key,
                        solve_sha256=solve_sha256,
                    )
                    if candidate is None:
                        raise LookupError("grade candidate not found")
                    recovered_validation = self._authoritative_reference_validation(
                        question_text=attempt.question_text,
                        independent_answer=independent_answer,
                    )
                    if reference_validation_from_evidence(candidate.evidence) != recovered_validation:
                        raise IntakeBatchFailure("reference_state_changed", retryable=False)
                    if recovered_validation is not None and recovered_validation.get("status") == "consistent":
                        self.notebook.store.link_attempt_question(
                            user_id=claim.user_id,
                            attempt_id=attempt_id,
                            question_id=str(recovered_validation["question_id"]),
                            expected_reference_validation=recovered_validation,
                            independent_answer=independent_answer,
                            batch_claim=claim,
                            batch_stage="grading",
                        )
                    error = self.notebook.store.get_error_by_attempt(user_id=claim.user_id, attempt_id=attempt_id)
                    if candidate.verdict in {"partial", "incorrect"} and error is None:
                        try:
                            error = self.notebook.store.commit_grade(
                                user_id=claim.user_id,
                                candidate_id=candidate.candidate_id,
                                expected_version=candidate.input_version,
                                expected_reference_validation=recovered_validation,
                                independent_answer=independent_answer,
                                batch_claim=claim,
                                batch_stage="grading",
                            )
                        except RuntimeError as exc:
                            if str(exc) != "reference_conflict":
                                raise
                    self.notebook.store.finish_grade_usage(
                        user_id=claim.user_id, intake_id=intake_id, counted=candidate.verdict != "unclear",
                        batch_claim=claim, batch_stage="grading",
                    )
                    result = {
                        "intake_id": intake_id,
                        "attempt_id": attempt_id,
                        "candidate_id": candidate.candidate_id,
                        "error_id": error.error_id if error else None,
                    }
                    return result, self._item_event(intake, candidate, error_id=error.error_id if error else None)

                repository.run_fenced_operation(
                    claim,
                    operation_key=operation_key,
                    stage="grading",
                    ordinal=ordinal,
                    action=commit_recovered,
                    completed_items_delta=1,
                )
                continue
            file_record = self._current_file(claim, repository, intake.file_id)
            reference = self.notebook.store.find_verified_question(question_text=attempt.question_text)
            with self.notebook.files.model_preview(file_record.object_key, file_record.content_sha256) as image_path:
                result = self.model_runner.grade_with_solution(
                    attempt=attempt,
                    image_path=image_path,
                    solution=solution,
                    thread_id=self.notebook.store.get_codex_thread(
                        user_id=claim.user_id, conversation_id=intake.intake_id,
                    ),
                    reference=reference,
                )
            self._current_file(claim, repository, intake.file_id)
            cross_validation = self._authoritative_reference_validation(
                question_text=attempt.question_text,
                independent_answer=independent_answer,
            )
            verdict, first_error, evidence = self._grade_values(
                result,
                cross_validation=cross_validation,
                batch_operation={
                    "schema": "intake-grade-operation/v1",
                    "batch_id": claim.batch_id,
                    "operation_key": operation_key,
                    "solve_sha256": solve_sha256,
                },
            )
            confidence = result.get("confidence")
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
                raise ModelUnavailableError("model returned invalid confidence")

            def commit_grade() -> tuple[dict[str, Any], dict[str, Any]]:
                candidate = self.notebook.store.find_grade_candidate_for_attempt(
                    user_id=claim.user_id,
                    attempt_id=attempt_id,
                    batch_id=claim.batch_id,
                    operation_key=operation_key,
                    solve_sha256=solve_sha256,
                )
                if candidate is None:
                    candidate = self.notebook.store.record_grade_candidate(
                        user_id=claim.user_id,
                        attempt_id=attempt_id,
                        input_version=attempt.input_version,
                        verdict=verdict,
                        first_error=first_error,
                        evidence=evidence,
                        confidence=float(confidence),
                        batch_claim=claim,
                        batch_stage="grading",
                    )
                validation = reference_validation_from_evidence(candidate.evidence)
                if validation is not None and validation.get("status") == "consistent":
                    self.notebook.store.link_attempt_question(
                        user_id=claim.user_id,
                        attempt_id=attempt_id,
                        question_id=str(validation["question_id"]),
                        expected_reference_validation=validation,
                        independent_answer=independent_answer,
                        batch_claim=claim,
                        batch_stage="grading",
                    )
                error = self.notebook.store.get_error_by_attempt(user_id=claim.user_id, attempt_id=attempt_id)
                if candidate.verdict in {"partial", "incorrect"} and error is None:
                    try:
                        error = self.notebook.store.commit_grade(
                            user_id=claim.user_id,
                            candidate_id=candidate.candidate_id,
                            expected_version=candidate.input_version,
                            expected_reference_validation=validation,
                            independent_answer=independent_answer,
                            batch_claim=claim,
                            batch_stage="grading",
                        )
                    except RuntimeError as exc:
                        if str(exc) != "reference_conflict":
                            raise
                self.notebook.store.finish_grade_usage(
                    user_id=claim.user_id, intake_id=intake_id, counted=candidate.verdict != "unclear",
                    batch_claim=claim, batch_stage="grading",
                )
                thread_id = result.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    self.notebook.store.save_codex_thread(
                        user_id=claim.user_id, conversation_id=intake.intake_id, thread_id=thread_id,
                        batch_claim=claim, batch_stage="grading",
                    )
                error_id = error.error_id if error else None
                operation_result = {
                    "intake_id": intake_id,
                    "attempt_id": attempt_id,
                    "candidate_id": candidate.candidate_id,
                    "error_id": error_id,
                }
                return operation_result, self._item_event(intake, candidate, error_id=error_id)

            repository.run_fenced_operation(
                claim,
                operation_key=operation_key,
                stage="grading",
                ordinal=ordinal,
                action=commit_grade,
                completed_items_delta=1,
            )

    @staticmethod
    def _intake_ids(claim: BatchClaim, repository) -> list[str]:
        intake_ids: list[str] = []
        for snapshot in repository.list_files(batch_id=claim.batch_id):
            operation = repository.get_operation(claim, f"slice:{snapshot.ordinal}")
            if operation is None or not isinstance(operation.result.get("intake_ids"), list):
                raise IntakeBatchFailure("slice_result_missing", retryable=False)
            intake_ids.extend(str(value) for value in operation.result["intake_ids"])
        return intake_ids

    def _current_file(self, claim: BatchClaim, repository, file_id: str):
        snapshot = next((item for item in repository.list_files(batch_id=claim.batch_id) if item.file_id == file_id), None)
        file_record = self.notebook.store.get_file(user_id=claim.user_id, file_id=file_id)
        if (
            snapshot is None
            or file_record is None
            or file_record.status != "ready"
            or file_record.object_key != snapshot.object_key
            or file_record.content_sha256 != snapshot.content_sha256
            or file_record.media_type != snapshot.media_type
            or file_record.byte_size != snapshot.byte_size
        ):
            raise IntakeBatchFailure("input_unavailable", retryable=False)
        return file_record

    @staticmethod
    def _independent_answer(solution: dict[str, Any]) -> str:
        value = solution.get("final_answer")
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 12_000:
            raise ModelUnavailableError("independent solution is invalid")
        return value.strip()

    @staticmethod
    def _freeze_solution(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ModelUnavailableError("independent solution is invalid")
        solution = payload.get("solution")
        final_answer = payload.get("final_answer")
        checks = payload.get("verification_checks")
        confidence = payload.get("confidence")
        difficulty = payload.get("difficulty")
        if (
            not isinstance(solution, str) or not 1 <= len(solution.strip()) <= 12_000
            or not isinstance(final_answer, str) or not 1 <= len(final_answer.strip()) <= 12_000
            or not isinstance(checks, list) or len(checks) > 8
            or not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1
            or difficulty not in {"normal", "hard", "max"}
        ):
            raise ModelUnavailableError("independent solution is invalid")
        frozen_checks: list[dict[str, Any]] = []
        for check in checks:
            if not isinstance(check, dict) or set(check) != {"left", "right", "variables"}:
                raise ModelUnavailableError("independent solution is invalid")
            left, right, variables = check["left"], check["right"], check["variables"]
            if (
                not isinstance(left, str) or not 1 <= len(left) <= 200
                or not isinstance(right, str) or not 1 <= len(right) <= 200
                or not isinstance(variables, list) or len(variables) > 3
                or any(not isinstance(name, str) or re.fullmatch(r"[a-z]", name) is None for name in variables)
                or len(variables) != len(set(variables))
            ):
                raise ModelUnavailableError("independent solution is invalid")
            frozen_checks.append({"left": left, "right": right, "variables": list(variables)})
        frozen = {
            "solution": solution.strip(),
            "final_answer": final_answer.strip(),
            "verification_checks": frozen_checks,
            "confidence": float(confidence),
            "difficulty": difficulty,
        }
        if len(json.dumps(frozen, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 32_768:
            raise ModelUnavailableError("independent solution is oversized")
        return frozen

    @staticmethod
    def _solution_sha256(solution: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(solution, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _authoritative_reference_validation(
        self,
        *,
        question_text: str,
        independent_answer: str,
    ) -> dict[str, object] | None:
        reference = self.notebook.store.find_verified_question(question_text=question_text)
        return cross_validate_reference(reference, independent_answer) if reference is not None else None

    @staticmethod
    def _item_event(intake, candidate, *, error_id: str | None) -> dict[str, Any]:
        if candidate.verdict == "correct":
            notebook_status = "not_saved_correct"
        elif candidate.verdict == "unclear" or error_id is None:
            notebook_status = "needs_review"
        else:
            notebook_status = "saved"
        question_text = intake.question_text
        answer_text = intake.answer_text
        return {
            "item_no": intake.item_no,
            "question_text": question_text[:2_000],
            "answer_text": answer_text[:2_000],
            "snapshot_truncated": len(question_text) > 2_000 or len(answer_text) > 2_000,
            "verdict": candidate.verdict,
            "auto_saved": notebook_status == "saved",
            "notebook_status": notebook_status,
        }

    @staticmethod
    def _grade_values(
        payload: dict[str, Any],
        *,
        cross_validation: dict[str, Any] | None,
        batch_operation: dict[str, str],
    ) -> tuple[str, str | None, str]:
        verdict = payload.get("verdict")
        if verdict not in {"correct", "partial", "incorrect", "unclear"}:
            raise ModelUnavailableError("model returned an unsupported verdict")
        required = verdict in {"partial", "incorrect"}
        first_error = str(payload.get("first_error") or "").strip() or None
        cause_code = str(payload.get("cause_code") or "").strip() or None
        cause_evidence = str(payload.get("cause_evidence") or "").strip() or None
        points = payload.get("knowledge_points", [])
        solution = str(payload.get("correct_solution") or "").strip() or None
        answer = str(payload.get("final_answer") or "").strip() or None
        cue = str(payload.get("prevention_cue") or "").strip() or None
        allowed_causes = {
            "knowledge_gap", "concept_confusion", "formula_condition", "method_choice", "reasoning_gap",
            "algebra_transform", "calculation", "misreading", "incomplete_cases", "expression", "careless", "unclear",
        }
        if (
            not isinstance(points, list) or len(points) > 8
            or any(not isinstance(point, str) or not point.strip() or len(point.strip()) > 200 for point in points)
            or any(len(value or "") > 12_000 for value in (first_error, cause_evidence, solution, answer, cue))
            or required and (not first_error or cause_code not in allowed_causes or not cause_evidence or not points or not solution or not answer)
        ):
            raise ModelUnavailableError("model returned an incomplete diagnosis")
        if not required:
            first_error = None
        diagnosis: dict[str, Any] = {
            "schema": "math-error-diagnosis/v1",
            "batch_operation": dict(batch_operation),
            "cause_code": cause_code if required else None,
            "cause_evidence": cause_evidence if required else None,
            "knowledge_points": [point.strip() for point in points] if required else [],
            "correct_solution": solution if required else None,
            "final_answer": answer,
            "prevention_cue": cue if required else None,
        }
        if cross_validation is not None:
            expected = {
                "schema", "status", "question_id", "version_id", "version_no", "source_title",
                "match_score", "reference_answer_sha256", "independent_answer_sha256",
            }
            if set(cross_validation) != expected or cross_validation.get("schema") != "question-bank-cross-validation/v1":
                raise ModelUnavailableError("model returned invalid cross validation")
            if cross_validation.get("status") not in {"consistent", "conflict"}:
                raise ModelUnavailableError("model returned invalid cross validation")
            if any(re.fullmatch(r"[0-9a-f]{32}", str(cross_validation.get(key, ""))) is None for key in ("question_id", "version_id")):
                raise ModelUnavailableError("model returned invalid cross validation")
            version_no, score = cross_validation.get("version_no"), cross_validation.get("match_score")
            if not isinstance(version_no, int) or isinstance(version_no, bool) or version_no < 1:
                raise ModelUnavailableError("model returned invalid cross validation")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.92 <= float(score) <= 1:
                raise ModelUnavailableError("model returned invalid cross validation")
            if not isinstance(cross_validation.get("source_title"), str) or not 1 <= len(cross_validation["source_title"]) <= 255:
                raise ModelUnavailableError("model returned invalid cross validation")
            if any(re.fullmatch(r"[0-9a-f]{64}", str(cross_validation.get(key, ""))) is None for key in ("reference_answer_sha256", "independent_answer_sha256")):
                raise ModelUnavailableError("model returned invalid cross validation")
            diagnosis["cross_validation"] = dict(cross_validation)
        return verdict, first_error, json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":"))
