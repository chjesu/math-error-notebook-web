from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from services.web_app import NotebookAsgiApp
from services.web_auth import AuthConfig, InMemoryCaptchaVerifier, InMemoryRegistrationStore, RecordingSmsSender, RegistrationService
from services.web_domain import ErrorEntry, InMemoryNotebookStore, Job, NotebookService, Question


class PrivacyHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.auth_store = InMemoryRegistrationStore()
        self.sender = RecordingSmsSender()
        self.auth = RegistrationService(
            store=self.auth_store,
            sms_sender=self.sender,
            captcha_verifier=InMemoryCaptchaVerifier(),
            secret_pepper=b"p" * 32,
            config=AuthConfig(scrypt_n=2**10),
        )
        self.store = InMemoryNotebookStore()
        self.notebook = NotebookService(self.store, Path(self.temp.name))
        self.app = NotebookAsgiApp(self.auth, self.notebook, allowed_hosts={"example.test"})
        self.sensitive_at = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, path: str, *, method: str = "GET", payload: dict | None = None, cookie: str | None = None, idempotency_key: str | None = None):
        requests = [{"type": "http.request", "body": json.dumps(payload or {}).encode("utf-8"), "more_body": False}]
        responses: list[dict] = []

        async def receive():
            return requests.pop(0)

        async def send(message):
            responses.append(message)

        headers = [(b"host", b"example.test"), (b"content-type", b"application/json"), (b"x-device-id", b"privacy-browser-001")]
        if cookie:
            headers.extend([(b"cookie", cookie.encode("ascii")), (b"origin", b"https://example.test")])
        if idempotency_key:
            headers.append((b"idempotency-key", idempotency_key.encode("ascii")))
        scope = {"type": "http", "method": method, "path": path, "scheme": "https", "client": ("203.0.113.7", 12345), "headers": headers}
        asyncio.run(self.app(scope, receive, send))
        started, finished = responses
        response_headers = {key.decode("ascii"): value.decode("ascii") for key, value in started["headers"]}
        body = finished.get("body", b"")
        parsed = json.loads(body) if response_headers.get("content-type", "").startswith("application/json") and body else body or None
        return started["status"], response_headers, parsed

    def account(self, phone: str) -> tuple[str, str]:
        requested = self.auth.request_code(purpose="register", phone=phone, ip_address="203.0.113.7", device_id="privacy-browser-001")
        result = self.auth.complete_registration(
            challenge_id=str(requested.challenge_id),
            phone=phone,
            code=self.sender.deliveries[-1][1],
            ip_address="203.0.113.7",
            device_id="privacy-browser-001",
            password="example-password1",
            terms_version="2026-08-23",
            privacy_version="2026-08-23",
        )
        self.assertIsNotNone(result.session_token)
        return str(result.user_id), f"__Host-lzlm_session={result.session_token}"

    def sensitive_payload(self, cookie: str, phone: str, action: str) -> dict[str, str]:
        token = cookie.split("=", 1)[1]
        self.sensitive_at += timedelta(minutes=2)
        requested = self.auth.request_sensitive_code(session_token=token, phone=phone, action=action, ip_address="203.0.113.7", device_id="privacy-browser-001", now=self.sensitive_at)
        return {"phone": phone, "challenge_token": str(requested.challenge_id), "code": self.sender.deliveries[-1][1]}

    def test_export_is_user_scoped_and_expired_exports_fail(self) -> None:
        user_id, cookie = self.account("13800138000")
        self.store.errors["e" * 32] = ErrorEntry("e" * 32, user_id, "a" * 32, "题目", "作答", "第一处错误", "open", self.auth_store.users_by_phone[next(iter(self.auth_store.users_by_phone))].created_at)
        export = self.call("/v1/exports", method="POST", payload=self.sensitive_payload(cookie, "13800138000", "export"), cookie=cookie, idempotency_key="privacy-export-0001")
        self.assertEqual(export[0], 201)
        self.assertEqual(set(export[2]), {"job_id", "download_url", "expires_at"})
        status = self.call(f"/v1/exports/{export[2]['job_id']}", cookie=cookie)
        self.assertEqual((status[0], status[2]["status"]), (200, "completed"))
        downloaded = self.call(export[2]["download_url"], cookie=cookie)
        self.assertEqual(downloaded[0], 200)
        self.assertEqual(downloaded[1]["content-type"], "application/json")
        self.assertEqual(downloaded[2]["data"]["errors"][0]["answer_text"], "作答")
        self.assertNotIn("password", json.dumps(downloaded[2], ensure_ascii=False).lower())
        self.assertEqual(self.call(export[2]["download_url"], cookie=cookie)[0], 200)
        self.assertEqual(self.call(export[2]["download_url"], cookie=cookie)[0], 200)
        self.assertEqual(self.call(export[2]["download_url"], cookie=cookie)[0], 404)
        events = [item for item in self.store.audit_events if item["resource_id"] == export[2]["job_id"]]
        self.assertEqual([item["event_type"] for item in events], ["export.downloaded", "export.downloaded", "export.downloaded", "export.download_denied"])

        _, other_cookie = self.account("13900139000")
        self.assertEqual(self.call(export[2]["download_url"], cookie=other_cookie)[0], 404)
        job = self.store.jobs[export[2]["job_id"]]
        self.store.jobs[job.job_id] = Job(job.job_id, job.user_id, job.job_type, job.resource_id, job.status, {**(job.checkpoint or {}), "expires_at": (self.notebook.EXPORT_TTL * -1 + self.auth_store.users_by_phone[next(iter(self.auth_store.users_by_phone))].created_at).isoformat()}, job.last_error_code)
        self.assertEqual(self.call(export[2]["download_url"], cookie=cookie)[0], 404)

    def test_delete_requires_matching_sensitive_action_and_makes_residuals_unavailable(self) -> None:
        user_id, cookie = self.account("13700137000")
        self.store.jobs["q" * 32] = Job("q" * 32, user_id, "extract", "r" * 32, "queued", None, None)
        export = self.call("/v1/exports", method="POST", payload=self.sensitive_payload(cookie, "13700137000", "export"), cookie=cookie, idempotency_key="privacy-export-delete")
        self.assertEqual(export[0], 201)

        delete_payload = self.sensitive_payload(cookie, "13700137000", "delete")
        wrong_confirmation = self.call("/v1/account", method="DELETE", payload={**delete_payload, "confirmation": "ERASE"}, cookie=cookie)
        self.assertEqual((wrong_confirmation[0], wrong_confirmation[2]["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.call("/v1/workbench", cookie=cookie)[0], 200)

        crossed = self.call("/v1/account", method="DELETE", payload={**self.sensitive_payload(cookie, "13700137000", "export"), "confirmation": "DELETE"}, cookie=cookie)
        self.assertEqual((crossed[0], crossed[2]["error"]["code"]), (403, "sensitive_verification_failed"))
        deleted = self.call("/v1/account", method="DELETE", payload={**delete_payload, "confirmation": "DELETE"}, cookie=cookie)
        self.assertEqual(deleted[0], 204)
        self.assertIn("Max-Age=0", deleted[1]["set-cookie"])
        self.assertEqual(self.call("/v1/workbench", cookie=cookie)[0], 401)
        self.assertEqual(self.store.jobs["q" * 32].status, "cancelled")
        self.assertIsNone(self.store.get_file(user_id=user_id, file_id=str(self.store.jobs[export[2]["job_id"]].checkpoint["file_id"])))
        with self.assertRaises(LookupError):
            self.notebook.download_export(user_id=user_id, job_id=export[2]["job_id"])

    def test_export_has_every_business_category_without_internal_fields_or_other_users(self) -> None:
        user_id, _ = self.account("13600136000")
        other_id, _ = self.account("13500135000")
        record = self.store.create_file(user_id=user_id, purpose="exam", original_name="paper.png", object_key="quarantine/user/paper.png", content_sha256="a" * 64, media_type="image/png", byte_size=12)
        intake, _ = self.store.create_intake(user_id=user_id, file_id=record.file_id, idempotency_key="privacy-export-intake")
        intake = self.store.save_extraction_candidate(user_id=user_id, intake_id=intake.intake_id, question_text="用户题目", answer_text="用户作答", evidence={})
        attempt_id, _ = self.store.confirm_intake(user_id=user_id, intake_id=intake.intake_id, expected_version=intake.input_version, idempotency_key="privacy-export-attempt")
        candidate = self.store.record_grade_candidate(user_id=user_id, attempt_id=attempt_id, input_version=intake.input_version, verdict="incorrect", first_error="第一处", evidence="证据")
        error = self.store.commit_grade(user_id=user_id, candidate_id=candidate.candidate_id, expected_version=intake.input_version)
        self.store.add_question(Question("q" * 32, "推荐题", "答案", 10, 2.0, "来源"))
        self.store.assign_recommendations(user_id=user_id, error_id=error.error_id)
        review = next(item for item in self.store.review_tasks.values() if item.user_id == user_id)
        self.store.complete_review(user_id=user_id, task_id=review.task_id, result="partial", idempotency_key="privacy-export-review")
        self.store.errors["o" * 32] = ErrorEntry("o" * 32, other_id, "x" * 32, "别人的题", "别人的作答", None, "open", self.sensitive_at)

        data = self.store.export_data(user_id=user_id)
        self.assertEqual(set(data), {"schema_version", "files", "intakes", "attempts", "grade_candidates", "errors", "recommendations", "learning_usage", "review_tasks", "review_attempts", "jobs"})
        self.assertTrue(all(data[name] for name in set(data) - {"schema_version"}))
        encoded = json.dumps(self.notebook._export_value(data), ensure_ascii=False)
        self.assertIn("用户题目", encoded)
        self.assertNotIn("别人的题", encoded)
        for forbidden in ("user_id", "object_key", "content_sha256", "checkpoint", "last_error_code", "quarantine/user"):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn('"job_type":"export"', encoded)

    def test_pending_deletion_can_resume_after_file_purge_failure(self) -> None:
        user_id, _ = self.account("13400134000")
        job = self.notebook.create_export(user_id=user_id, idempotency_key="privacy-delete-resume")
        file_id = str(job.checkpoint["file_id"])
        object_key = self.store.files[file_id].object_key
        path = self.notebook.files.root / object_key
        self.assertTrue(path.is_file())
        self.assertEqual(self.notebook.prepare_user_deletion(user_id=user_id)["status"], "pending")
        with mock.patch.object(Path, "unlink", side_effect=OSError("simulated purge failure")):
            self.assertEqual(self.app.resume_pending_deletions(), 0)
        self.assertEqual(self.notebook.deletion_status(user_id=user_id)["last_error_code"], "file_purge_failed")
        self.assertTrue(path.is_file())
        self.assertEqual(self.app.resume_pending_deletions(), 1)
        self.assertEqual(self.notebook.deletion_status(user_id=user_id)["status"], "completed")
        self.assertFalse(path.exists())
        with self.assertRaises(LookupError):
            self.notebook.download_export(user_id=user_id, job_id=job.job_id)

    def test_application_recovery_waits_for_auth_deactivation_before_completion(self) -> None:
        user_id, cookie = self.account("13300133000")
        job = self.notebook.create_export(user_id=user_id, idempotency_key="privacy-auth-resume")
        file_id = str(job.checkpoint["file_id"])
        path = self.notebook.files.root / self.store.files[file_id].object_key
        delete_payload = {**self.sensitive_payload(cookie, "13300133000", "delete"), "confirmation": "DELETE"}
        with mock.patch.object(self.auth, "deactivate_account", return_value=False):
            failed = self.call("/v1/account", method="DELETE", payload=delete_payload, cookie=cookie)
        self.assertEqual((failed[0], failed[2]["error"]["code"]), (503, "failed_retryable"))
        self.assertEqual(self.notebook.deletion_status(user_id=user_id)["status"], "pending")
        self.assertEqual(self.notebook.deletion_status(user_id=user_id)["last_error_code"], "auth_deactivation_failed")
        self.assertTrue(path.is_file())

        restarted = NotebookAsgiApp(self.auth, self.notebook, allowed_hosts={"example.test"})
        with mock.patch.object(self.auth, "deactivate_account", return_value=False):
            self.assertEqual(restarted.resume_pending_deletions(), 0)
        self.assertEqual(self.notebook.deletion_status(user_id=user_id)["status"], "pending")
        self.assertIsNotNone(self.auth.authenticate_session(cookie.split("=", 1)[1]))
        self.assertTrue(path.is_file())

        restarted = NotebookAsgiApp(self.auth, self.notebook, allowed_hosts={"example.test"})
        self.assertEqual(restarted.resume_pending_deletions(), 1)
        self.assertEqual(self.notebook.deletion_status(user_id=user_id)["status"], "completed")
        self.assertIsNone(self.auth.authenticate_session(cookie.split("=", 1)[1]))
        self.assertFalse(path.exists())

    def test_domain_cleanup_failure_is_retryable_and_preserves_the_session(self) -> None:
        user_id, cookie = self.account("13200132000")
        delete_payload = {**self.sensitive_payload(cookie, "13200132000", "delete"), "confirmation": "DELETE"}
        with mock.patch.object(self.store, "deactivate_user_data", side_effect=OSError("database unavailable")):
            failed = self.call("/v1/account", method="DELETE", payload=delete_payload, cookie=cookie)
        self.assertEqual((failed[0], failed[2]["error"]["code"]), (503, "failed_retryable"))
        deletion = self.notebook.deletion_status(user_id=user_id)
        self.assertEqual((deletion["status"], deletion["last_error_code"]), ("pending", "domain_cleanup_failed"))
        self.assertIsNotNone(self.auth.authenticate_session(cookie.split("=", 1)[1]))

    def test_export_requires_the_declared_idempotency_key(self) -> None:
        _, cookie = self.account("13100131000")
        proof = self.sensitive_payload(cookie, "13100131000", "export")
        response = self.call("/v1/exports", method="POST", payload=proof, cookie=cookie)
        self.assertEqual((response[0], response[2]["error"]["code"]), (400, "invalid_request"))
        retried = self.call("/v1/exports", method="POST", payload=proof, cookie=cookie, idempotency_key="privacy-export-retry")
        self.assertEqual(retried[0], 201)

    def test_auth_deactivation_error_record_failure_still_returns_retryable(self) -> None:
        _, cookie = self.account("13000130000")
        delete_payload = {**self.sensitive_payload(cookie, "13000130000", "delete"), "confirmation": "DELETE"}
        with mock.patch.object(self.auth, "deactivate_account", return_value=False), mock.patch.object(self.notebook, "record_deletion_error", side_effect=OSError("audit unavailable")):
            failed = self.call("/v1/account", method="DELETE", payload=delete_payload, cookie=cookie)
        self.assertEqual((failed[0], failed[2]["error"]["code"]), (503, "failed_retryable"))

    def test_history_routes_return_the_application_shell(self) -> None:
        for path in ("/login", "/register", "/legal/terms", "/legal/privacy"):
            response = self.call(path)
            self.assertEqual(response[0], 200)
            self.assertIn("李兆霖数学错题本".encode("utf-8"), response[2])


if __name__ == "__main__":
    unittest.main()
