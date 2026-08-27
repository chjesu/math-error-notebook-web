from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from services.web_app import NotebookAsgiApp
from services.web_auth import AuthConfig, InMemoryCaptchaVerifier, InMemoryRegistrationStore, RecordingSmsSender, RegistrationService
from services.web_domain import GradeCandidate, InMemoryNotebookStore, NotebookService, Question


class NotebookE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.auth_store = InMemoryRegistrationStore()
        self.sender = RecordingSmsSender()
        self.auth_service = RegistrationService(
            store=self.auth_store,
            sms_sender=self.sender,
            captcha_verifier=InMemoryCaptchaVerifier(),
            secret_pepper=b"p" * 32,
            config=AuthConfig(resend_cooldown_seconds=0),
        )
        self.domain_store = InMemoryNotebookStore()
        self.notebook = NotebookService(self.domain_store, Path(self.temp.name))
        self.app = NotebookAsgiApp(
            self.auth_service,
            self.notebook,
            allowed_hosts={"example.test"},
            harness_internal_token="test-internal-token",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def png_bytes() -> bytes:
        stream = BytesIO()
        Image.new("RGB", (10, 10), "white").save(stream, format="PNG")
        return stream.getvalue()

    def call(self, path: str, *, method: str = "GET", payload: dict | None = None, body: bytes | None = None, content_type: str = "application/json", cookie: str | None = None, origin: str | None = "https://example.test", idempotency_key: str | None = None, extra_headers: dict[str, str] | None = None, client: tuple[str, int] = ("203.0.113.7", 12345)):
        route_path, _, query = path.partition("?")
        raw = body if body is not None else json.dumps(payload or {}).encode("utf-8")
        requests = [{"type": "http.request", "body": raw, "more_body": False}]
        responses: list[dict] = []

        async def receive():
            return requests.pop(0)

        async def send(message):
            responses.append(message)

        headers = [(b"host", b"example.test"), (b"content-type", content_type.encode("latin-1")), (b"x-device-id", b"browser-device-001")]
        if cookie:
            headers.append((b"cookie", cookie.encode("ascii")))
            if origin is not None:
                headers.append((b"origin", origin.encode("ascii")))
        if idempotency_key:
            headers.append((b"idempotency-key", idempotency_key.encode("ascii")))
        for key, value in (extra_headers or {}).items():
            headers.append((key.encode("ascii"), value.encode("ascii")))
        scope = {"type": "http", "method": method, "path": route_path, "query_string": query.encode("ascii"), "scheme": "https", "client": client, "headers": headers}
        asyncio.run(self.app(scope, receive, send))
        started = responses[0]
        response_headers = {key.decode("ascii"): value.decode("ascii") for key, value in started["headers"]}
        response_body = b"".join(message.get("body", b"") for message in responses[1:])
        parsed = json.loads(response_body) if response_headers.get("content-type", "").startswith("application/json") and response_body else response_body or None
        return started["status"], response_headers, parsed

    def test_public_shell_serves_only_fixed_brand_assets(self) -> None:
        home = self.call("/")
        self.assertEqual(home[0], 200)
        self.assertIn("李兆霖数学错题本".encode("utf-8"), home[2])
        for route in ("/", "/login", "/register", "/legal/terms", "/legal/privacy", "/errors", "/reviews", "/practice", "/progress", "/settings"):
            self.assertEqual(self.call(route)[0], 200)
        logo = self.call("/assets/branding/logo-symbol-color-64-v1.png")
        self.assertEqual(logo[1]["content-type"], "image/png")
        icons = self.call("/web/nav-icons.svg")
        self.assertEqual((icons[0], icons[1]["content-type"]), (200, "image/svg+xml"))
        self.assertIn(b'<symbol id="workbench"', icons[2])
        katex = self.call("/web/vendor/katex/katex.min.js")
        self.assertEqual((katex[0], katex[1]["content-type"]), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"KaTeX", katex[2])
        self.assertEqual(self.call("/web/vendor/katex/auto-render.min.js")[0], 200)
        self.assertEqual(self.call("/assets/branding/../README.md")[0], 401)

    def test_public_upload_cannot_claim_internal_pdf_purpose(self) -> None:
        cookie = self.login("13600136000")
        content_type, body = self.multipart("fake.pdf", b"%PDF-1.4\n%%EOF", purpose="practice_pdf")
        response = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="upload-internal")
        self.assertEqual(response[0], 400)

    def test_upload_requires_the_declared_idempotency_key(self) -> None:
        cookie = self.login("13400134000")
        content_type, body = self.multipart("question.png", b"\x89PNG\r\n\x1a\nimage")
        response = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie)
        self.assertEqual((response[0], response[2]["error"]["code"]), (400, "invalid_request"))

    def test_upload_idempotency_key_binds_one_exact_request(self) -> None:
        cookie = self.login("13300133000")
        content_type, body = self.multipart("question.png", b"\x89PNG\r\n\x1a\nimage")
        first = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="upload-replay-key")
        replay = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="upload-replay-key")
        changed_type, changed_body = self.multipart("question.png", b"\x89PNG\r\n\x1a\nchanged")
        changed = self.call("/v1/files", method="POST", body=changed_body, content_type=changed_type, cookie=cookie, idempotency_key="upload-replay-key")
        self.assertEqual((first[0], replay[0], changed[0]), (201, 201, 409))
        self.assertEqual(first[2]["file_id"], replay[2]["file_id"])
        self.assertEqual(len(self.domain_store.files), 1)
        self.assertEqual(len([path for path in Path(self.temp.name).rglob("*") if path.is_file()]), 1)

    def test_refresh_history_restores_owned_image_attachment_and_preview(self) -> None:
        class EmptyHistoryModel:
            @staticmethod
            def history(*, thread_id, cursor, limit):
                self.assertEqual((thread_id, cursor, limit), ("thread-with-image", None, 20))
                return {"items": [], "next_cursor": None}

        self.app.model_runner = EmptyHistoryModel()
        cookie = self.login("13200132000")
        other_cookie = self.login("13200132001")
        content = self.png_bytes()
        content_type, body = self.multipart("refresh-kept.png", content)
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="upload-refresh-image")
        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="intake-refresh-image")
        intake_id = created[2]["resource_id"]
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        self.domain_store.save_codex_thread(user_id=user.user_id, conversation_id=intake_id, thread_id="thread-with-image")

        pending = self.call("/v1/intakes", cookie=cookie)
        attachment = pending[2]["items"][0]["attachment"]
        self.assertEqual(attachment["name"], "refresh-kept.png")
        self.assertEqual(attachment["media_type"], "image/png")
        self.assertEqual(attachment["preview_url"], f"/v1/intakes/{intake_id}/source")
        self.assertNotIn("object_key", attachment)

        preview = self.call(attachment["preview_url"], cookie=cookie)
        self.assertEqual((preview[0], preview[1]["content-type"], preview[2]), (200, "image/png", content))
        self.assertEqual(preview[1]["cache-control"], "private,no-store")
        self.assertEqual(self.call(attachment["preview_url"], cookie=other_cookie)[0], 404)

        history = self.call("/v1/conversations/latest/messages", cookie=cookie)
        self.assertEqual(history[2]["items"], [{"role": "user", "text": "请整理这 1 个文件", "attachments": [attachment]}])

    def test_clear_conversation_removes_only_current_users_workbench_content(self) -> None:
        cookie = self.login("13200132002")
        other_cookie = self.login("13200132003")
        content_type, body = self.multipart("clear-me.png", self.png_bytes())
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="upload-clear-conversation")
        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="intake-clear-conversation")
        intake_id = created[2]["resource_id"]
        other_type, other_body = self.multipart("keep-me.png", self.png_bytes())
        other_uploaded = self.call("/v1/files", method="POST", body=other_body, content_type=other_type, cookie=other_cookie, idempotency_key="upload-keep-conversation")
        other_created = self.call("/v1/intakes", method="POST", payload={"file_id": other_uploaded[2]["file_id"]}, cookie=other_cookie, idempotency_key="intake-keep-conversation")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        other_user = self.auth_service.authenticate_session(other_cookie.split("=", 1)[1])
        self.domain_store.save_codex_thread(user_id=user.user_id, conversation_id=intake_id, thread_id="thread-clear")
        self.domain_store.save_codex_thread(user_id=other_user.user_id, conversation_id=other_created[2]["resource_id"], thread_id="thread-keep")

        cleared = self.call("/v1/conversations/latest", method="DELETE", cookie=cookie)
        self.assertEqual((cleared[0], cleared[2]), (204, None))
        self.assertEqual(self.call("/v1/intakes", cookie=cookie)[2], {"items": []})
        self.assertEqual(self.domain_store.list_recent_codex_threads(user_id=user.user_id), [])
        self.assertEqual(self.call(f"/v1/intakes/{intake_id}/source", cookie=cookie)[0], 404)
        self.assertEqual(len(self.call("/v1/intakes", cookie=other_cookie)[2]["items"]), 1)
        self.assertEqual(self.domain_store.list_recent_codex_threads(user_id=other_user.user_id), [(other_created[2]["resource_id"], "thread-keep")])
        self.assertEqual(self.domain_store.files[uploaded[2]["file_id"]].status, "ready")

    def test_cookie_writes_require_exact_same_origin(self) -> None:
        cookie = self.login("13500135000")
        cases = (
            ("/v1/auth/sensitive/otp/request", "POST", {"phone": "13500135000", "action": "export"}),
            ("/v1/session", "DELETE", {}),
            ("/v1/sessions", "DELETE", {}),
            ("/v1/exports", "POST", {}),
            ("/v1/account", "DELETE", {}),
        )
        for path, method, payload in cases:
            with self.subTest(path=path, origin="missing"):
                response = self.call(path, method=method, payload=payload, cookie=cookie, origin=None)
                self.assertEqual((response[0], response[2]["error"]["code"]), (403, "forbidden"))
            with self.subTest(path=path, origin="wrong"):
                response = self.call(path, method=method, payload=payload, cookie=cookie, origin="https://evil.example")
                self.assertEqual((response[0], response[2]["error"]["code"]), (403, "forbidden"))

    def test_harness_receipt_is_authoritative_idempotent_and_user_scoped(self) -> None:
        cookie = self.login("13500135001")
        other_cookie = self.login("13500135002")
        content_type, body = self.multipart("receipt.png", self.png_bytes())
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="receipt-upload")
        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="receipt-intake")
        intake_id = created[2]["resource_id"]
        self.call(f"/v1/intakes/{intake_id}/manual-candidate", method="POST", payload={"question_text": "若 x+1=2，求 x。", "answer_text": "x=0"}, cookie=cookie)
        confirmed = self.call(f"/v1/intakes/{intake_id}/confirm", method="POST", payload={"input_version": 1}, cookie=cookie, idempotency_key="receipt-grade")
        graded = self.call(f"/v1/attempts/{confirmed[2]['resource_id']}/manual-grade", method="POST", payload={
            "input_version": 1,
            "verdict": "incorrect",
            "first_error": "移项后符号错误",
            "cause_code": "algebra_transform",
            "evidence": "由 x+1=2 得到 x=0",
            "knowledge_points": ["一元一次方程", "等式性质与移项"],
            "correct_solution": "x=2-1=1",
            "final_answer": "x=1",
            "prevention_cue": "代回验算",
        }, cookie=cookie)
        candidate_id = graded[2]["result_id"]
        harness_origin = "http://example.test:3080"
        bound = self.call("/v1/harness/sessions/bind", method="POST", payload={"session_id": "session-receipt"}, cookie=cookie, origin=harness_origin)
        self.assertEqual((bound[0], bound[2]), (200, {"status": "bound"}))
        self.assertEqual(bound[1]["access-control-allow-origin"], harness_origin)

        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        bad_token = self.call(
            f"/v1/internal/harness/grade-results/{candidate_id}/commit",
            method="POST",
            payload={"session_id": "session-receipt", "input_version": 1},
            origin=None,
            client=("127.0.0.1", 3080),
            extra_headers={"authorization": "Bearer wrong-token"},
        )
        remote_client = self.call(
            f"/v1/internal/harness/grade-results/{candidate_id}/commit",
            method="POST",
            payload={"session_id": "session-receipt", "input_version": 1},
            origin=None,
            client=("203.0.113.8", 3080),
            extra_headers={"authorization": "Bearer test-internal-token"},
        )
        self.assertEqual((bad_token[0], remote_client[0]), (403, 403))
        saved = self.call(f"/v1/internal/harness/grade-results/{candidate_id}/commit", method="POST", payload={"session_id": "session-receipt", "input_version": 1}, **internal)
        replayed = self.call(f"/v1/internal/harness/grade-results/{candidate_id}/commit", method="POST", payload={"session_id": "session-receipt", "input_version": 1}, **internal)
        self.assertEqual(saved[2]["receipt"]["status"], "saved")
        self.assertEqual(saved[2]["receipt"]["knowledge_point_count"], 2)
        self.assertEqual(saved[2]["receipt"]["review_status"], "scheduled")
        self.assertEqual(replayed[2]["receipt"]["status"], "already_saved")
        self.assertEqual(saved[2]["receipt"]["error_id"], replayed[2]["receipt"]["error_id"])
        self.assertEqual(len(self.call("/v1/errors", cookie=cookie)[2]["items"]), 1)

        self.call("/v1/harness/sessions/bind", method="POST", payload={"session_id": "session-other"}, cookie=other_cookie, origin=harness_origin)
        denied = self.call(f"/v1/internal/harness/grade-results/{candidate_id}/commit", method="POST", payload={"session_id": "session-other", "input_version": 1}, **internal)
        self.assertEqual((denied[0], denied[2]["error"]["code"]), (404, "not_found"))

    def test_harness_receipt_explicitly_skips_correct_and_unclear_results(self) -> None:
        correct = GradeCandidate("a" * 32, "b" * 32, 1, "correct", None, None, "candidate")
        unclear = GradeCandidate("c" * 32, "d" * 32, 1, "unclear", None, None, "candidate")
        self.assertEqual(
            self.app._grade_receipt(correct),
            {
                "schema": "math-notebook-entry-receipt/v1",
                "status": "not_saved_correct",
                "knowledge_point_count": 0,
                "review_status": "not_scheduled",
                "message": "错题本记录检查：本题判定正确，未计入错题本。",
            },
        )
        self.assertEqual(self.app._grade_receipt(unclear)["status"], "needs_review")

    def test_deletion_keeps_durable_pending_state_when_domain_cleanup_fails(self) -> None:
        phone = "13400134000"
        cookie = self.login(phone)
        user_id = self.auth_service.authenticate_session(cookie.split("=", 1)[1]).user_id
        requested = self.call("/v1/auth/sensitive/otp/request", method="POST", payload={"phone": phone, "action": "delete"}, cookie=cookie)
        self.assertEqual(requested[0], 202)
        original = self.notebook.complete_user_deletion

        def fail_cleanup(*, user_id: str) -> dict:
            raise OSError("disk unavailable")

        self.notebook.complete_user_deletion = fail_cleanup  # type: ignore[method-assign]
        try:
            response = self.call("/v1/account", method="DELETE", payload={"phone": phone, "challenge_token": requested[2]["challenge_token"], "code": self.sender.deliveries[-1][1], "confirmation": "DELETE"}, cookie=cookie)
        finally:
            self.notebook.complete_user_deletion = original  # type: ignore[method-assign]
        self.assertEqual((response[0], response[2]["error"]["code"]), (503, "failed_retryable"))
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        self.assertIsNone(user)
        self.assertEqual(self.notebook.deletion_status(user_id=user_id)["status"], "pending")

    def login(self, phone: str) -> str:
        requested = self.call("/v1/auth/register/otp/request", method="POST", payload={"phone": phone})
        verified = self.call("/v1/auth/register/complete", method="POST", payload={"phone": phone, "challenge_token": requested[2]["challenge_token"], "code": self.sender.deliveries[-1][1], "password": "safe1234", "terms_version": "2026-08-23", "privacy_version": "2026-08-23"})
        self.assertEqual(verified[0], 200)
        return verified[1]["set-cookie"].split(";", 1)[0]

    @staticmethod
    def multipart(filename: str, content: bytes, purpose: str = "question_image") -> tuple[str, bytes]:
        boundary = "lzlm-test-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n{purpose}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n"
        ).encode("ascii") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
        return f"multipart/form-data; boundary={boundary}", body

    def test_phone_to_first_error_http_slice_and_cross_user_denial(self) -> None:
        cookie = self.login("13800138000")
        content_type, body = self.multipart("question.png", b"\x89PNG\r\n\x1a\nimage")
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="upload-0001")
        self.assertEqual(uploaded[0], 201)

        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="extract-0001")
        self.assertEqual(created[0], 202)
        intake_id = created[2]["resource_id"]
        manual = self.call(f"/v1/intakes/{intake_id}/manual-candidate", method="POST", payload={"question_text": "若 x+1=2，求 x。", "answer_text": "x=0"}, cookie=cookie)
        self.assertEqual((manual[0], manual[2]["status"]), (201, "waiting_confirmation"))
        repeated = self.call(f"/v1/intakes/{intake_id}/manual-candidate", method="POST", payload={"question_text": "若 x+1=2，求 x。", "answer_text": "x=0"}, cookie=cookie)
        self.assertEqual(repeated[2], manual[2])

        revised = self.call(f"/v1/intakes/{intake_id}", method="PATCH", payload={"input_version": 1, "question_text": "若 x+1=2，求 x。", "answer_text": "x=0"}, cookie=cookie)
        self.assertEqual((revised[0], revised[2]["input_version"]), (200, 2))
        confirmed = self.call(f"/v1/intakes/{intake_id}/confirm", method="POST", payload={"input_version": 2}, cookie=cookie, idempotency_key="grade-0001")
        self.assertEqual(confirmed[0], 202)
        graded = self.call(f"/v1/attempts/{confirmed[2]['resource_id']}/manual-grade", method="POST", payload={
            "input_version": 2,
            "verdict": "incorrect",
            "first_error": "移项后符号错误",
            "cause_code": "algebra_transform",
            "evidence": "把常数项移到等号右侧时没有变号",
            "knowledge_points": ["一元一次方程", "等式性质与移项"],
            "correct_solution": "x+1=2，所以 x=1",
            "final_answer": "x=1",
            "prevention_cue": "移项后立即检查符号",
        }, cookie=cookie)
        self.assertEqual(graded[0], 201)
        self.assertEqual(graded[2]["diagnosis"]["cause_code"], "algebra_transform")
        candidate_id = graded[2]["result_id"]

        result = self.call(f"/v1/grade-results/{candidate_id}", cookie=cookie)
        self.assertEqual((result[0], result[2]["verdict"]), (200, "incorrect"))
        committed = self.call(f"/v1/grade-results/{candidate_id}/commit", method="POST", payload={"input_version": 2}, cookie=cookie, idempotency_key="commit-0001")
        self.assertEqual(committed[0], 201)
        self.assertEqual(committed[2]["diagnosis"]["knowledge_points"], ["一元一次方程", "等式性质与移项"])
        error_id = committed[2]["error_id"]
        self.assertEqual(self.call("/v1/errors", cookie=cookie)[2]["items"][0]["error_id"], error_id)
        error_detail = self.call(f"/v1/errors/{error_id}", cookie=cookie)
        self.assertEqual(error_detail[0], 200)
        self.assertEqual(error_detail[2]["diagnosis"]["final_answer"], "x=1")
        self.assertEqual(error_detail[2]["diagnosis"]["knowledge_points"], ["一元一次方程", "等式性质与移项"])

        self.domain_store.add_question(Question("1" * 32, "解方程 x+2=4", "x=2", 10, 2.0, "公开验证题库"))
        recommended = self.call(f"/v1/errors/{error_id}/recommendations", method="POST", cookie=cookie, idempotency_key="recommend-0001")
        self.assertEqual(recommended[0], 200)
        self.assertEqual(recommended[2]["items"][0]["source"], "公开验证题库")
        self.assertNotIn("answer_text", recommended[2]["items"][0])
        reviews = self.call("/v1/reviews/today", cookie=cookie)
        self.assertEqual(reviews[2]["count"], 1)
        completed_review = self.call(f"/v1/reviews/{reviews[2]['items'][0]['review_id']}/complete", method="POST", payload={"result": "correct"}, cookie=cookie, idempotency_key="review-0001")
        self.assertEqual(completed_review[2]["next_review"]["stage"], 2)
        practice = self.call("/v1/practice-pdfs", method="POST", payload={"error_ids": [error_id]}, cookie=cookie, idempotency_key="practice-0001")
        self.assertEqual(practice[0], 201)
        downloaded = self.call(practice[2]["download_url"], cookie=cookie)
        self.assertEqual(downloaded[0], 200)
        self.assertTrue(downloaded[2].startswith(b"%PDF-"))

        other_cookie = self.login("13900139000")
        denied = self.call(f"/v1/errors/{error_id}", cookie=other_cookie)
        self.assertEqual(denied[0], 404)
        self.assertEqual(denied[2]["error"]["code"], "not_found")
        self.assertEqual(self.call(practice[2]["download_url"], cookie=other_cookie)[0], 404)
        self.assertEqual(self.call(f"/v1/intakes/{intake_id}/manual-candidate", method="POST", payload={"question_text": "越权题目"}, cookie=other_cookie)[0], 404)

        bank = self.call("/v1/bank/status", cookie=cookie)
        self.assertEqual((bank[0], bank[2]["question_count"]), (200, 1))
        mastered = self.call(f"/v1/errors/{error_id}/master", method="POST", cookie=cookie)
        self.assertEqual((mastered[0], mastered[2]["status"]), (200, "mastered"))
        removed = self.call(f"/v1/errors/{error_id}", method="DELETE", cookie=cookie)
        self.assertEqual((removed[0], removed[2]["status"]), (200, "removed"))

    def test_codex_candidates_use_the_same_confirmation_and_commit_gates(self) -> None:
        resumed_threads = []

        class FakeModel:
            def extract(_, *, intake, file_record, image_path, thread_id=None):
                resumed_threads.append(thread_id)
                self.assertTrue(image_path.is_file())
                self.assertEqual(image_path.parent.name, "model-previews")
                self.assertEqual(file_record.media_type, "image/png")
                return {"intake_id": intake.intake_id, "input_version": intake.input_version, "status": "complete", "items": [
                    {"item_no": 1, "status": "complete", "question_text": "若 x+1=2，求 x。", "answer_text": "x=0", "confidence": 0.98},
                    {"item_no": 2, "status": "complete", "question_text": "若 y-1=2，求 y。", "answer_text": "y=2", "confidence": 0.97},
                ], "confidence": 0.98, "thread_id": "thread-e2e", "route": {"task": "math-intake-adjudication", "model": "test"}}

            def grade(_, *, attempt, image_path, thread_id=None):
                resumed_threads.append(thread_id)
                self.assertTrue(image_path.is_file())
                self.assertEqual(image_path.parent.name, "model-previews")
                return {"attempt_id": attempt.attempt_id, "input_version": attempt.input_version, "verdict": "incorrect", "first_error": "移项后结果错误", "cause_code": "algebra_transform", "cause_evidence": "由 x+1=2 得到 x=0", "knowledge_points": ["一元一次方程", "等式性质与移项"], "correct_solution": "x=2-1=1", "final_answer": "x=1", "prevention_cue": "移项后验算", "confidence": 0.97, "thread_id": "thread-e2e", "route": {"task": "math-grade-adjudication", "model": "test"}}

        self.app.model_runner = FakeModel()
        cookie = self.login("13200132000")
        other_cookie = self.login("13200132001")
        content_type, body = self.multipart("model-question.png", self.png_bytes())
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="model-upload")
        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="model-extract")
        intake_id = created[2]["resource_id"]
        self.assertEqual(self.call(f"/v1/intakes/{intake_id}/model-candidate", method="POST", cookie=other_cookie)[0], 404)
        extracted = self.call(f"/v1/intakes/{intake_id}/model-candidate", method="POST", cookie=cookie)
        self.assertEqual((extracted[0], extracted[2]["status"], extracted[2]["question_text"]), (201, "waiting_confirmation", "若 x+1=2，求 x。"))
        self.assertEqual(
            [(item["item_no"], item["question_text"], item["answer_text"]) for item in extracted[2]["items"]],
            [(1, "若 x+1=2，求 x。", "x=0"), (2, "若 y-1=2，求 y。", "y=2")],
        )
        repeated = self.call(f"/v1/intakes/{intake_id}/model-candidate", method="POST", cookie=cookie)
        self.assertEqual((repeated[0], repeated[2]["model_status"], len(repeated[2]["items"])), (200, "existing", 2))
        refreshed = self.call(f"/v1/intakes/{intake_id}/model-candidate", method="POST", payload={"refresh": True}, cookie=cookie)
        self.assertEqual((refreshed[0], refreshed[2]["model_status"], len(refreshed[2]["items"])), (201, "complete", 2))
        confirmed = self.call(f"/v1/intakes/{intake_id}/confirm", method="POST", payload={"input_version": 1}, cookie=cookie, idempotency_key="model-grade")
        pending = self.call("/v1/intakes", cookie=cookie)
        self.assertEqual(
            [(item["item_no"], item["status"]) for item in pending[2]["items"]],
            [(1, "confirmed"), (2, "waiting_confirmation")],
        )
        self.assertEqual(self.call(f"/v1/attempts/{confirmed[2]['resource_id']}/model-grade", method="POST", payload={"input_version": 1}, cookie=other_cookie)[0], 404)
        graded = self.call(f"/v1/attempts/{confirmed[2]['resource_id']}/model-grade", method="POST", payload={"input_version": 1}, cookie=cookie)
        self.assertEqual((graded[0], graded[2]["verdict"], graded[2]["diagnosis"]["final_answer"]), (201, "incorrect", "x=1"))
        self.assertEqual([item["item_no"] for item in self.call("/v1/intakes", cookie=cookie)[2]["items"]], [2])
        self.assertEqual(resumed_threads, [None, "thread-e2e", "thread-e2e"])
        self.assertEqual(self.call("/v1/errors", cookie=cookie)[2]["items"], [])
        committed = self.call(f"/v1/grade-results/{graded[2]['result_id']}/commit", method="POST", payload={"input_version": 1}, cookie=cookie, idempotency_key="model-commit")
        self.assertEqual((committed[0], committed[2]["question_text"]), (201, "若 x+1=2，求 x。"))
        self.assertEqual(committed[2]["diagnosis"]["knowledge_points"], ["一元一次方程", "等式性质与移项"])
        second_id = refreshed[2]["items"][1]["intake_id"]
        second = self.call(f"/v1/intakes/{second_id}/confirm", method="POST", payload={"input_version": 1}, cookie=cookie, idempotency_key="model-grade-second")
        second_grade = self.call(f"/v1/attempts/{second[2]['resource_id']}/model-grade", method="POST", payload={"input_version": 1}, cookie=cookie)
        self.assertEqual((second_grade[0], resumed_threads[-1]), (201, "thread-e2e"))

    def test_model_endpoints_are_disabled_by_default(self) -> None:
        cookie = self.login("13100131000")
        response = self.call("/v1/intakes/" + "a" * 32 + "/model-candidate", method="POST", cookie=cookie)
        self.assertEqual((response[0], response[2]["error"]["code"]), (503, "model_unavailable"))

    def test_chat_loop_revises_model_candidates_without_bypassing_write_gates(self) -> None:
        class FakeLoopModel:
            def chat_turn(_, **values):
                if values.get("event_callback"):
                    values["event_callback"]({"type": "turn_started", "status": "inProgress"})
                    values["event_callback"]({"type": "agent_message_delta", "delta": "{"})
                if values["stage"] == "intake":
                    return {
                        "action": "ready" if values.get("event_callback") else "revise_intake",
                        "assistant_message": "候选已准备好。" if values.get("event_callback") else "已按你的说明修正题干。",
                        "question_text": "若 x+1=2，求 x。", "answer_text": "x=0", "confidence": 0.99,
                        "thread_id": values.get("thread_id") or "thread-loop-test",
                        "route": {"task": "math-notebook-loop", "model": "test"},
                    }
                return {
                    "action": "revise_grade", "assistant_message": "已重新判题。", "verdict": "incorrect",
                    "first_error": "移项结果错误", "cause_code": "algebra_transform",
                    "cause_evidence": "由 x+1=2 得到 x=0", "knowledge_points": ["一元一次方程"], "correct_solution": "x=2-1=1",
                    "final_answer": "x=1", "prevention_cue": "代回验算", "confidence": 0.99,
                    "thread_id": values.get("thread_id") or "thread-loop-test",
                    "route": {"task": "math-notebook-loop", "model": "test"},
                }

            def compact(_, *, thread_id):
                self.assertEqual(thread_id, "thread-loop-test")
                return {"status": "completed"}

        self.app.model_runner = FakeLoopModel()
        cookie = self.login("13000130000")
        other_cookie = self.login("13000130001")
        content_type, body = self.multipart("loop.png", self.png_bytes())
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="loop-upload")
        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="loop-intake")
        intake_id = created[2]["resource_id"]
        request = {"message": "题干是 x+1=2", "stage": "intake", "input_version": 1, "attempt_id": None, "candidate_id": None}
        denied = self.call(f"/v1/intakes/{intake_id}/chat-turn", method="POST", payload=request, cookie=other_cookie)
        self.assertEqual(denied[0], 404)
        revised = self.call(f"/v1/intakes/{intake_id}/chat-turn", method="POST", payload=request, cookie=cookie)
        self.assertEqual((revised[0], revised[2]["intake"]["status"], revised[2]["intake"]["question_text"]), (200, "waiting_confirmation", "若 x+1=2，求 x。"))
        self.assertEqual(self.call(f"/v1/intakes/{intake_id}/conversation/compact", method="POST", cookie=cookie)[2], {"status": "completed"})
        self.assertEqual(self.call(f"/v1/intakes/{intake_id}/conversation/stop", method="POST", cookie=cookie)[2], {"status": "idle"})
        streamed = self.call(f"/v1/intakes/{intake_id}/chat-turn-stream", method="POST", payload=request, cookie=cookie)
        stream_events = [json.loads(line) for line in streamed[2].splitlines()]
        self.assertEqual(streamed[1]["content-type"], "application/x-ndjson; charset=utf-8")
        self.assertIn("turn_started", [item.get("event", {}).get("type") for item in stream_events])
        self.assertEqual(stream_events[-1]["type"], "result")
        confirmed = self.call(f"/v1/intakes/{intake_id}/confirm", method="POST", payload={"input_version": 1}, cookie=cookie, idempotency_key="loop-confirm")
        grade_request = {"message": "请再检查第一处错误", "stage": "grade", "input_version": 1, "attempt_id": confirmed[2]["resource_id"], "candidate_id": None}
        graded = self.call(f"/v1/intakes/{intake_id}/chat-turn", method="POST", payload=grade_request, cookie=cookie)
        self.assertEqual((graded[0], graded[2]["candidate"]["verdict"], graded[2]["candidate"]["diagnosis"]["final_answer"]), (200, "incorrect", "x=1"))
        self.assertEqual(self.call("/v1/errors", cookie=cookie)[2]["items"], [])

    def test_latest_conversation_history_is_user_scoped_and_hides_thread_id(self) -> None:
        class FakeHistoryModel:
            def history(_, *, thread_id, cursor, limit):
                self.assertEqual(limit, 20)
                if thread_id == "thread-empty-test":
                    return {"items": [], "next_cursor": None}
                self.assertEqual(thread_id, "thread-history-test")
                if cursor == "older-page":
                    return {"items": [{"role": "user", "text": "更早的问题"}], "next_cursor": None}
                return {
                    "items": [{"role": "user", "text": "请再检查"}, {"role": "assistant", "text": "可以，我们继续核对。"}],
                    "next_cursor": "older-page",
                }

        original_list = self.domain_store.list_recent_codex_threads
        requested_limits = []

        def list_recent_codex_threads(*, user_id, limit):
            requested_limits.append(limit)
            return original_list(user_id=user_id, limit=limit)

        self.app.model_runner = FakeHistoryModel()
        self.domain_store.list_recent_codex_threads = list_recent_codex_threads
        cookie = self.login("13000130002")
        other_cookie = self.login("13000130003")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        self.domain_store.save_codex_thread(user_id=user.user_id, conversation_id="a" * 32, thread_id="thread-history-test")
        self.domain_store.save_codex_thread(user_id=user.user_id, conversation_id="b" * 32, thread_id="thread-empty-test")
        response = self.call("/v1/conversations/latest/messages", cookie=cookie)
        self.assertEqual(response[0], 200)
        self.assertEqual(requested_limits, [20])
        self.assertEqual(response[2]["items"][0], {"role": "user", "text": "请再检查"})
        self.assertNotIn("thread_id", response[2])
        self.assertNotIn("conversation_id", response[2])
        older = self.call(f"/v1/conversations/latest/messages?cursor={response[2]['next_cursor']}", cookie=cookie)
        self.assertEqual(older[2], {"items": [{"role": "user", "text": "更早的问题"}], "next_cursor": None})
        self.assertEqual(self.call(f"/v1/conversations/latest/messages?cursor={response[2]['next_cursor']}", cookie=other_cookie)[0], 404)
        self.assertEqual(self.call("/v1/conversations/latest/messages", cookie=other_cookie)[2], {"items": [], "next_cursor": None})

    def test_manual_grade_requires_first_error_and_large_body_is_413(self) -> None:
        cookie = self.login("13700137000")
        content_type, body = self.multipart("question.png", b"\x89PNG\r\n\x1a\nimage")
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="upload-validation")
        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="extract-validation")
        intake_id = created[2]["resource_id"]
        self.call(f"/v1/intakes/{intake_id}/manual-candidate", method="POST", payload={"question_text": "题目", "answer_text": "错误作答"}, cookie=cookie)
        confirmed = self.call(f"/v1/intakes/{intake_id}/confirm", method="POST", payload={"input_version": 1}, cookie=cookie, idempotency_key="grade-validation")
        invalid = self.call(f"/v1/attempts/{confirmed[2]['resource_id']}/manual-grade", method="POST", payload={"input_version": 1, "verdict": "incorrect"}, cookie=cookie)
        self.assertEqual((invalid[0], invalid[2]["error"]["code"]), (400, "invalid_request"))
        missing_knowledge = self.call(f"/v1/attempts/{confirmed[2]['resource_id']}/manual-grade", method="POST", payload={
            "input_version": 1, "verdict": "incorrect", "first_error": "首错", "cause_code": "calculation",
            "evidence": "计算结果与等式不符", "correct_solution": "1+1=2", "final_answer": "2",
        }, cookie=cookie)
        self.assertEqual((missing_knowledge[0], missing_knowledge[2]["error"]["code"]), (400, "invalid_request"))

        limited = NotebookAsgiApp(self.auth_service, self.notebook, allowed_hosts={"example.test"}, max_upload_bytes=8)
        original = self.app
        self.app = limited
        try:
            too_large = self.call("/v1/files", method="POST", body=b"012345678", content_type="multipart/form-data; boundary=x", cookie=cookie, idempotency_key="upload-too-large")
        finally:
            self.app = original
        self.assertEqual((too_large[0], too_large[2]["error"]["code"]), (413, "request_too_large"))

    def test_stale_candidate_and_unclear_result_cannot_enter_notebook(self) -> None:
        user = "a" * 32
        file = self.notebook.upload(user_id=user, purpose="question_image", original_name="q.png", content=b"\x89PNG\r\n\x1a\nimage")
        intake, _ = self.domain_store.create_intake(user_id=user, file_id=file.file_id, idempotency_key="extract-0002")
        self.domain_store.save_extraction_candidate(user_id=user, intake_id=intake.intake_id, question_text="题目", answer_text="答案", evidence={})
        revised = self.domain_store.revise_intake(user_id=user, intake_id=intake.intake_id, expected_version=1, question_text="题目修订", answer_text="答案")
        with self.assertRaisesRegex(RuntimeError, "input_version_changed"):
            self.domain_store.confirm_intake(user_id=user, intake_id=intake.intake_id, expected_version=1, idempotency_key="grade-stale")
        attempt_id, _ = self.domain_store.confirm_intake(user_id=user, intake_id=intake.intake_id, expected_version=revised.input_version, idempotency_key="grade-current")
        unclear = self.domain_store.record_grade_candidate(user_id=user, attempt_id=attempt_id, input_version=revised.input_version, verdict="unclear", first_error=None, evidence=None)
        with self.assertRaisesRegex(RuntimeError, "failed_final"):
            self.domain_store.commit_grade(user_id=user, candidate_id=unclear.candidate_id, expected_version=revised.input_version)
        correct = self.domain_store.record_grade_candidate(user_id=user, attempt_id=attempt_id, input_version=revised.input_version, verdict="correct", first_error=None, evidence=None)
        with self.assertRaisesRegex(RuntimeError, "failed_final"):
            self.domain_store.commit_grade(user_id=user, candidate_id=correct.candidate_id, expected_version=revised.input_version)

    def test_model_extraction_rejects_a_replaced_quarantine_object(self) -> None:
        class NeverCalledModel:
            def extract(self, **_kwargs):
                raise AssertionError("tampered bytes must not reach the model")

        self.app.model_runner = NeverCalledModel()
        cookie = self.login("13200132002")
        content_type, body = self.multipart("model-question.png", self.png_bytes())
        uploaded = self.call("/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie, idempotency_key="tamper-upload")
        created = self.call("/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie, idempotency_key="tamper-intake")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        record = self.domain_store.get_file(user_id=user.user_id, file_id=uploaded[2]["file_id"])
        assert record is not None
        self.notebook.files.resolve(record.object_key).write_bytes(b"replaced")
        response = self.call(f"/v1/intakes/{created[2]['resource_id']}/model-candidate", method="POST", cookie=cookie)
        self.assertEqual((response[0], response[2]["error"]["code"]), (409, "conflict"))


if __name__ == "__main__":
    unittest.main()
