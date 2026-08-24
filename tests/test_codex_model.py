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
                    result = {"intake_id": frozen["intake_id"], "input_version": 1, "status": "complete", "question_text": "题目", "answer_text": "作答", "notes": None, "confidence": 0.99}
                else:
                    result = {"attempt_id": frozen["attempt_id"], "input_version": 1, "verdict": "incorrect", "first_error": "首错", "cause_code": "calculation", "cause_evidence": "证据", "correct_solution": "过程", "final_answer": "答案", "prevention_cue": "验算", "confidence": 0.98}
                return {"route": route, "result": result}

            def route(task, risks):
                return {"task": task, "model": "test", "reasoning_effort": "low", "risks": risks}

            model = CodexNotebookModel(root / "results", review=review, route_selector=route)
            intake = SimpleNamespace(intake_id="a" * 32, input_version=1)
            file_record = SimpleNamespace(media_type="image/png", original_name="q.png")
            extracted = model.extract(intake=intake, file_record=file_record, image_path=image)
            self.assertEqual(extracted["question_text"], "题目")
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


if __name__ == "__main__":
    unittest.main()
