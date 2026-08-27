from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
from threading import Event, Thread
import unittest

from services.web_app.codex_model import CodexNotebookModel, ModelUnavailableError


def solution_review(route, review_input, output, images, thread_id=None, event_callback=None):
    frozen = json.loads(review_input)
    return {"route": route, "thread_id": thread_id or "thread-solution", "result": {
        "attempt_id": frozen["attempt_id"], "input_version": frozen["input_version"],
        "solution": "独立解题过程", "final_answer": "答案",
        "verification_checks": [{"left": "1+1", "right": "2", "variables": []}],
        "confidence": 0.99,
    }}


class CodexNotebookModelTests(unittest.TestCase):
    def test_extract_and_grade_return_bounded_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = (root / "q.png").resolve()
            image.write_bytes(b"image")

            def review(route, review_input, output, images, thread_id=None, event_callback=None):
                frozen = json.loads(review_input)
                self.assertEqual(images, [image])
                if route["task"].startswith("math-intake"):
                    result = {"intake_id": frozen["intake_id"], "input_version": 1, "status": "complete", "items": [{"item_no": 1, "status": "complete", "question_text": "题目", "answer_text": "作答", "notes": None, "confidence": 0.99}], "notes": None, "confidence": 0.99}
                elif route["task"].startswith("math-grade-solution"):
                    self.assertNotIn("answer_text", frozen)
                    result = {"attempt_id": frozen["attempt_id"], "input_version": 1, "solution": "1+1=2", "final_answer": "2", "verification_checks": [{"left": "1+1", "right": "2", "variables": []}], "confidence": 0.99}
                else:
                    self.assertEqual(frozen["evidence"]["verification_report"][0]["status"], "verified")
                    result = {"attempt_id": frozen["attempt_id"], "input_version": 1, "verdict": "incorrect", "first_error": "首错", "cause_code": "calculation", "cause_evidence": "证据", "knowledge_points": ["代数运算", "结果验算"], "correct_solution": "过程", "final_answer": "答案", "prevention_cue": "验算", "confidence": 0.98}
                return {"route": route, "thread_id": thread_id or "thread-test", "result": result}

            def route(task, risks):
                return {"task": task, "model": "test", "reasoning_effort": "low", "risks": risks}

            model = CodexNotebookModel(root / "results", review=review, harness_review=review, route_selector=route)
            intake = SimpleNamespace(intake_id="a" * 32, input_version=1)
            file_record = SimpleNamespace(media_type="image/png", original_name="q.png")
            extracted = model.extract(intake=intake, file_record=file_record, image_path=image)
            self.assertEqual(extracted["items"][0]["question_text"], "题目")
            attempt = SimpleNamespace(attempt_id="b" * 32, input_version=1, question_text="题目", answer_text="作答")
            graded = model.grade(attempt=attempt, image_path=image)
            self.assertEqual((graded["verdict"], graded["cause_code"]), ("incorrect", "calculation"))
            self.assertEqual(graded["knowledge_points"], ["代数运算", "结果验算"])
            self.assertEqual(list((root / "results").iterdir()), [])

    def test_rejects_a_response_for_another_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def review(route, review_input, output, images, thread_id=None, event_callback=None):
                return {"route": route, "thread_id": thread_id or "thread-test", "result": {"attempt_id": "c" * 32, "input_version": 1, "verdict": "unclear", "first_error": None, "cause_code": "unclear", "cause_evidence": None, "knowledge_points": [], "correct_solution": None, "final_answer": None, "prevention_cue": None, "confidence": 0.9}}

            model = CodexNotebookModel(Path(directory), review=solution_review, harness_review=review, route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"})
            attempt = SimpleNamespace(attempt_id="b" * 32, input_version=1, question_text="题目", answer_text="作答")
            with self.assertRaisesRegex(ModelUnavailableError, "frozen attempt"):
                model.grade(attempt=attempt, image_path=Path(directory) / "q.png")

    def test_extract_filters_recommendation_blocks_and_renumbers_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def review(route, review_input, output, images, thread_id=None, event_callback=None):
                frozen = json.loads(review_input)
                texts = (
                    "错题编号 ERR-1：第一道题",
                    "同类类型推荐题 1，题库编号 Q-1：推荐练习",
                    "错题编号 ERR-2：第二道题",
                    "题库编号 Q-2：另一道推荐练习",
                )
                return {"route": route, "thread_id": thread_id or "thread-test", "result": {
                    "intake_id": frozen["intake_id"], "input_version": 1, "status": "complete",
                    "items": [{"item_no": index, "status": "complete", "question_text": text, "answer_text": "", "confidence": 0.99} for index, text in enumerate(texts, 1)],
                    "confidence": 0.99,
                }}

            model = CodexNotebookModel(Path(directory), review=solution_review, harness_review=review, route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"})
            intake = SimpleNamespace(intake_id="a" * 32, input_version=1)
            file_record = SimpleNamespace(media_type="image/jpeg", original_name="paper.jpg")
            result = model.extract(intake=intake, file_record=file_record, image_path=Path(directory) / "paper.jpg")
            self.assertEqual([(item["item_no"], item["question_text"]) for item in result["items"]], [(1, "错题编号 ERR-1：第一道题"), (2, "错题编号 ERR-2：第二道题")])

    def test_unclear_extraction_without_readable_question_keeps_the_conversation_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def review(route, review_input, output, images, thread_id=None, event_callback=None):
                frozen = json.loads(review_input)
                return {"route": route, "thread_id": thread_id or "thread-unclear", "result": {
                    "intake_id": frozen["intake_id"], "input_version": 1, "status": "unclear",
                    "items": [{"item_no": 1, "status": "unclear", "question_text": "", "answer_text": "", "notes": "图片不清晰", "confidence": 0.4}],
                    "notes": "请用户补充题干", "confidence": 0.4,
                }}

            model = CodexNotebookModel(Path(directory), review=solution_review, harness_review=review, route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"})
            intake = SimpleNamespace(intake_id="a" * 32, input_version=1)
            file_record = SimpleNamespace(media_type="image/jpeg", original_name="paper.jpg")
            result = model.extract(intake=intake, file_record=file_record, image_path=Path(directory) / "paper.jpg")
            self.assertEqual((result["status"], result["items"], result["thread_id"]), ("unclear", [], "thread-unclear"))

    def test_same_resource_and_version_has_only_one_active_model_call(self) -> None:
        started = Event()
        release = Event()
        failures: list[Exception] = []

        def review(route, review_input, output, images, thread_id=None, event_callback=None):
            frozen = json.loads(review_input)
            started.set()
            release.wait(2)
            return {"route": route, "thread_id": thread_id or "thread-test", "result": {"attempt_id": frozen["attempt_id"], "input_version": 1, "verdict": "unclear", "first_error": None, "cause_code": "unclear", "cause_evidence": None, "knowledge_points": [], "correct_solution": None, "final_answer": None, "prevention_cue": None, "confidence": 0.9}}

        with tempfile.TemporaryDirectory() as directory:
            model = CodexNotebookModel(Path(directory), review=solution_review, harness_review=review, route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"})
            attempt = SimpleNamespace(attempt_id="b" * 32, input_version=1, question_text="题目", answer_text="作答")

            def first_call() -> None:
                try:
                    model.grade(attempt=attempt, image_path=Path(directory) / "q.png")
                except Exception as exc:
                    failures.append(exc)

            worker = Thread(target=first_call)
            worker.start()
            self.assertTrue(started.wait(1))
            with self.assertRaisesRegex(ModelUnavailableError, "already in progress"):
                model.grade(attempt=attempt, image_path=Path(directory) / "q.png")
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

    def test_cli_public_error_code_survives_adapter_boundary(self) -> None:
        class NetworkFailure(RuntimeError):
            public_code = "model_network_error"

        with tempfile.TemporaryDirectory() as directory:
            model = CodexNotebookModel(
                Path(directory),
                harness_review=lambda *args: (_ for _ in ()).throw(NetworkFailure("private diagnostic")),
                route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"},
            )
            intake = SimpleNamespace(intake_id="a" * 32, input_version=1)
            file_record = SimpleNamespace(media_type="image/png", original_name="q.png")
            with self.assertRaises(ModelUnavailableError) as raised:
                model.extract(intake=intake, file_record=file_record, image_path=Path(directory) / "q.png")
            self.assertEqual(raised.exception.code, "model_network_error")

    def test_grade_routes_only_difficult_questions_to_xhigh_or_max(self) -> None:
        selected = []

        def route(task, risks):
            selected.append(task)
            effort = "max" if task.endswith("-max") else "xhigh" if task.endswith("-hard") else "medium"
            return {"task": task, "model": "test", "reasoning_effort": effort, "risks": risks}

        def grade_review(route_value, review_input, output, images, thread_id=None, event_callback=None):
            frozen = json.loads(review_input)
            return {"route": route_value, "thread_id": thread_id or "thread-grade", "result": {
                "attempt_id": frozen["attempt_id"], "input_version": frozen["input_version"],
                "verdict": "correct", "first_error": None, "cause_code": None,
                "cause_evidence": None, "knowledge_points": [], "correct_solution": "过程", "final_answer": "答案",
                "prevention_cue": None, "confidence": 0.99,
            }}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "q.png"
            image.write_bytes(b"image")
            model = CodexNotebookModel(root, review=solution_review, harness_review=grade_review, route_selector=route)
            for index, question in enumerate(("求 x 的值", "如图，求轨迹", "证明该空间几何中的二面角结论"), 1):
                attempt = SimpleNamespace(attempt_id=f"{index:x}" * 32, input_version=1, question_text=question, answer_text="作答")
                model.grade(attempt=attempt, image_path=image)
        self.assertEqual(selected, [
            "math-grade-solution", "math-grade-adjudication",
            "math-grade-solution-hard", "math-grade-adjudication-hard",
            "math-grade-solution-max", "math-grade-adjudication-max",
        ])

    def test_grade_rejects_unbounded_verification_requests_before_adjudication(self) -> None:
        def bad_solution(route, review_input, output, images, thread_id=None, event_callback=None):
            frozen = json.loads(review_input)
            return {"route": route, "thread_id": thread_id or "thread-solution", "result": {
                "attempt_id": frozen["attempt_id"], "input_version": frozen["input_version"],
                "solution": "过程", "final_answer": "答案",
                "verification_checks": [{"left": "1", "right": "1", "variables": [], "command": "whoami"}],
                "confidence": 0.99,
            }}

        with tempfile.TemporaryDirectory() as directory:
            called = []
            model = CodexNotebookModel(
                Path(directory), review=bad_solution,
                harness_review=lambda *args: called.append(True),
                route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"},
            )
            attempt = SimpleNamespace(attempt_id="d" * 32, input_version=1, question_text="题目", answer_text="作答")
            with self.assertRaisesRegex(ModelUnavailableError, "verification checks"):
                model.grade(attempt=attempt, image_path=Path(directory) / "q.png")
            self.assertEqual(called, [])

    def test_chat_turn_accepts_durable_server_thread_and_validates_frozen_context(self) -> None:
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
                "knowledge_points": [], "correct_solution": None, "final_answer": None, "prevention_cue": None,
                "confidence": 0.98,
            }}

        with tempfile.TemporaryDirectory() as directory:
            model = CodexNotebookModel(
                Path(directory), conversation_review=conversation,
                route_selector=lambda task, risks: {"task": task, "model": "test", "reasoning_effort": "low"},
            )
            first = model.chat_turn(conversation_id="a" * 32, stage="intake", resource_id="a" * 32, input_version=1, user_message="第二行是题干", context={})
            second = model.chat_turn(conversation_id="a" * 32, stage="intake", resource_id="a" * 32, input_version=2, user_message="还有吗", context={}, thread_id=first["thread_id"])
            self.assertEqual(first["question_text"], "修正后的题目")
            self.assertEqual(second["action"], "ready")
            self.assertEqual(sessions, [None, "thread-abc"])

    def test_history_exposes_only_product_messages_from_structured_thread_items(self) -> None:
        packet = {"user_message": "第二行才是题干"}
        answer = {"assistant_message": "已按你的说明修正。"}
        entries = [
            {"turnId": "4", "item": {"type": "agentMessage", "text": json.dumps(answer, ensure_ascii=False)}},
            {"turnId": "3", "item": {"type": "userMessage", "content": [{"type": "text", "text": "Private prompt\nReview input:\n" + json.dumps(packet, ensure_ascii=False)}]}},
            {"turnId": "2", "item": {"type": "agentMessage", "text": json.dumps({"status": "complete"})}},
            {"turnId": "1", "item": {"type": "userMessage", "content": [{"type": "text", "text": "Private extraction prompt\nReview input:\n{}"}]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            model = CodexNotebookModel(Path(directory), history_reader=lambda thread, cursor, limit: {"items": entries, "next_cursor": "older"})
            page = model.history(thread_id="thread-abc", limit=20)
        self.assertEqual(page, {
            "items": [
                {"role": "user", "text": "第二行才是题干"},
                {"role": "assistant", "text": "已按你的说明修正。"},
            ],
            "next_cursor": "older",
        })

    def test_compaction_hides_the_thread_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = CodexNotebookModel(
                Path(directory),
                compactor=lambda thread_id: {"thread_id": thread_id, "status": "completed"},
            )
            self.assertEqual(model.compact(thread_id="thread-abc"), {"status": "completed"})


if __name__ == "__main__":
    unittest.main()
