from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from services.web_app import NotebookAsgiApp
from services.web_auth import InMemoryCaptchaVerifier, InMemoryRegistrationStore, RecordingSmsSender, RegistrationService
from services.web_domain import InMemoryNotebookStore, NotebookService


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
        )
        self.domain_store = InMemoryNotebookStore()
        self.notebook = NotebookService(self.domain_store, Path(self.temp.name))
        self.app = NotebookAsgiApp(self.auth_service, self.notebook, allowed_hosts={"example.test"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, path: str, *, method: str = "GET", payload: dict | None = None, body: bytes | None = None, content_type: str = "application/json", cookie: str | None = None, idempotency_key: str | None = None):
        raw = body if body is not None else json.dumps(payload or {}).encode("utf-8")
        requests = [{"type": "http.request", "body": raw, "more_body": False}]
        responses: list[dict] = []

        async def receive():
            return requests.pop(0)

        async def send(message):
            responses.append(message)

        headers = [(b"host", b"example.test"), (b"content-type", content_type.encode("latin-1")), (b"x-device-id", b"browser-device-001")]
        if cookie:
            headers.extend([(b"cookie", cookie.encode("ascii")), (b"origin", b"https://example.test")])
        if idempotency_key:
            headers.append((b"idempotency-key", idempotency_key.encode("ascii")))
        scope = {"type": "http", "method": method, "path": path, "scheme": "https", "client": ("203.0.113.7", 12345), "headers": headers}
        asyncio.run(self.app(scope, receive, send))
        started, finished = responses
        parsed = json.loads(finished["body"]) if finished["body"] else None
        response_headers = {key.decode("ascii"): value.decode("ascii") for key, value in started["headers"]}
        return started["status"], response_headers, parsed

    def login(self, phone: str) -> str:
        requested = self.call("/v1/auth/otp/request", method="POST", payload={"phone": phone})
        verified = self.call("/v1/auth/otp/verify", method="POST", payload={"phone": phone, "challenge_token": requested[2]["challenge_token"], "code": self.sender.deliveries[-1][1]})
        self.assertEqual(verified[0], 200)
        return verified[1]["set-cookie"].split(";", 1)[0]

    @staticmethod
    def multipart(filename: str, content: bytes) -> tuple[str, bytes]:
        boundary = "lzlm-test-boundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\nquestion_image\r\n"
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
        user_id = next(iter(self.auth_store.users_by_phone.values())).user_id
        self.domain_store.save_extraction_candidate(user_id=user_id, intake_id=intake_id, question_text="若 x+1=2，求 x。", answer_text="x=0", evidence={"page": 1})

        revised = self.call(f"/v1/intakes/{intake_id}", method="PATCH", payload={"input_version": 1, "question_text": "若 x+1=2，求 x。", "answer_text": "x=0"}, cookie=cookie)
        self.assertEqual((revised[0], revised[2]["input_version"]), (200, 2))
        confirmed = self.call(f"/v1/intakes/{intake_id}/confirm", method="POST", payload={"input_version": 2}, cookie=cookie, idempotency_key="grade-0001")
        self.assertEqual(confirmed[0], 202)
        candidate = self.domain_store.record_grade_candidate(user_id=user_id, attempt_id=confirmed[2]["resource_id"], input_version=2, verdict="incorrect", first_error="移项后符号错误", evidence="x+1=2 应得 x=1")

        result = self.call(f"/v1/grade-results/{candidate.candidate_id}", cookie=cookie)
        self.assertEqual((result[0], result[2]["verdict"]), (200, "incorrect"))
        committed = self.call(f"/v1/grade-results/{candidate.candidate_id}/commit", method="POST", payload={"input_version": 2}, cookie=cookie, idempotency_key="commit-0001")
        self.assertEqual(committed[0], 201)
        error_id = committed[2]["error_id"]
        self.assertEqual(self.call("/v1/errors", cookie=cookie)[2]["items"][0]["error_id"], error_id)
        self.assertEqual(self.call(f"/v1/errors/{error_id}", cookie=cookie)[0], 200)

        other_cookie = self.login("13900139000")
        denied = self.call(f"/v1/errors/{error_id}", cookie=other_cookie)
        self.assertEqual(denied[0], 404)
        self.assertEqual(denied[2]["error"]["code"], "not_found")

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


if __name__ == "__main__":
    unittest.main()
