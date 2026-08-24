from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
from threading import Event, Thread
import unittest

from services.web_app.codex_model import CodexNotebookModel, ModelUnavailableError


class CodexNotebookModelTests(unittest.TestCase):
    def test_extract_and_grade_return_bounded_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = (root / "q.png").resolve()
            image.write_bytes(b"image")

            def review(route, review_input, output, images):
                frozen = json.loads(review_input)
                self.assertEqual(images, [image] if route["task"].startswith("math-intake") else [])
                if route["task"].startswith("math-intake"):
                    result = {"intake_id": frozen["intake_id"], "input_version": 1, "status": "complete", "items": [{"item_no": 1, "status": "complete", "question_text": "题目", "answer_text": "作答", "notes": None, "confidence": 0.99}], "notes": None, "confidence": 0.99}
                else:
                    result = {"attempt_id": frozen["attempt_id"], "input_version": 1, "verdict": "incorrect", "first_error": "首错", "cause_code": "calculation", "cause_evidence": "证据", "correct_solution": "过程", "final_answer": "答案", "prevention_cue": "验算", "confidence": 0.98}
                return {"route": route, "result": result}

            def route(task, risks):
                return {"task": task, "model": "test", "reasoning_effort": "low", "risks": risks}

            model = CodexNotebookModel(root / "results", review=review, route_selector=route)
            intake = SimpleNamespace(intake_id="a" * 32, input_version=1)
            file_record = SimpleNamespace(media_type="image/png", original_name="q.png")
            extracted = model.extract(intake=intake, file_record=file_record, image_path=image)
            self.assertEqual(extracted["items"][0]["question_text"], "题目")
            attempt = SimpleNamespace(attempt_id="b" * 32, input_version=1, question_text="题目", answer_text="作答")
            graded = model.grade(attempt=attempt)
            self.assertEqual((graded["verdict"], graded["cause_code"]), ("incorrect", "calculation"))
            self.assertEqual(list((root / "results").iterdir()), [])

    def test_rejects_a_response_for_another_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def review(route, review_input, output, images):
                return {"route": route, "result": {"attempt_id": "c" * 32, "input_version": 1, "verdict": "unclear", "first_error": None, "cause_code": "unclear", "cause_evidence": None, "correct_solution": None, "final_answer": None, "prevention_cue": None, "confidence": 0.9}}

            model = CodexNotebookModel(Path(directory), review=review, route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"})
            attempt = SimpleNamespace(attempt_id="b" * 32, input_version=1, question_text="题目", answer_text="作答")
            with self.assertRaisesRegex(ModelUnavailableError, "frozen attempt"):
                model.grade(attempt=attempt)

    def test_same_resource_and_version_has_only_one_active_model_call(self) -> None:
        started = Event()
        release = Event()
        failures: list[Exception] = []

        def review(route, review_input, output, images):
            frozen = json.loads(review_input)
            started.set()
            release.wait(2)
            return {"route": route, "result": {"attempt_id": frozen["attempt_id"], "input_version": 1, "verdict": "unclear", "first_error": None, "cause_code": "unclear", "cause_evidence": None, "correct_solution": None, "final_answer": None, "prevention_cue": None, "confidence": 0.9}}

        with tempfile.TemporaryDirectory() as directory:
            model = CodexNotebookModel(Path(directory), review=review, route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"})
            attempt = SimpleNamespace(attempt_id="b" * 32, input_version=1, question_text="题目", answer_text="作答")

            def first_call() -> None:
                try:
                    model.grade(attempt=attempt)
                except Exception as exc:
                    failures.append(exc)

            worker = Thread(target=first_call)
            worker.start()
            self.assertTrue(started.wait(1))
            with self.assertRaisesRegex(ModelUnavailableError, "already in progress"):
                model.grade(attempt=attempt)
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

    def test_chat_turn_reuses_server_held_session_and_validates_frozen_context(self) -> None:
        sessions = []

        def conversation(route, review_input, output, session_id):
            packet = json.loads(review_input)
            sessions.append(session_id)
            return {"route": route, "session_id": "thread-abc", "result": {
                "conversation_id": packet["conversation_id"], "stage": packet["stage"],
                "resource_id": packet["resource_id"], "input_version": packet["input_version"],
                "action": "revise_intake" if session_id is None else "ready",
                "assistant_message": "已修正" if session_id is None else "可以确认",
                "question_text": "修正后的题目", "answer_text": "作答", "verdict": None,
                "first_error": None, "cause_code": None, "cause_evidence": None,
                "correct_solution": None, "final_answer": None, "prevention_cue": None,
                "confidence": 0.98,
            }}

        with tempfile.TemporaryDirectory() as directory:
            model = CodexNotebookModel(
                Path(directory), conversation_review=conversation,
                route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"},
            )
            first = model.chat_turn(conversation_id="a" * 32, stage="intake", resource_id="a" * 32, input_version=1, user_message="第二行是题干", context={})
            second = model.chat_turn(conversation_id="a" * 32, stage="intake", resource_id="a" * 32, input_version=2, user_message="还有吗", context={})
            self.assertEqual(first["question_text"], "修正后的题目")
            self.assertEqual(second["action"], "ready")
            self.assertEqual(sessions, [None, "thread-abc"])


if __name__ == "__main__":
    unittest.main()
