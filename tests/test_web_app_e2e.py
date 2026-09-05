from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from services.web_app import NotebookAsgiApp
from services.web_auth import AuthConfig, InMemoryCaptchaVerifier, InMemoryRegistrationStore, RecordingSmsSender, RegistrationService
from services.web_domain import ErrorEntry, GradeCandidate, InMemoryNotebookStore, NotebookService, Question, Recommendation, ReviewTask, cross_validate_reference
from services.web_domain.notebook import Attempt


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
            if requests:
                return requests.pop(0)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

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

    def test_public_shell_exposes_auth_pages_but_protects_product_pages(self) -> None:
        home = self.call("/")
        self.assertEqual((home[0], home[1]["location"]), (303, "/login"))
        for route in ("/login", "/register", "/legal/terms", "/legal/privacy"):
            self.assertEqual(self.call(route)[0], 200)
        cookie = self.login("13600136001")
        for route in ("/", "/errors", "/practice", "/progress", "/settings"):
            self.assertEqual(self.call(route, cookie=cookie)[0], 200)
        self.assertNotIn("/reviews", self.app.static_files)
        self.assertIn("/progress", self.app.static_files)
        logo = self.call("/assets/branding/logo-symbol-color-64-v1.png")
        self.assertEqual(logo[1]["content-type"], "image/png")
        for route in (
            "/web/app.js",
            "/web/nav-icons.svg",
            "/web/vendor/katex/katex.min.js",
            "/web/vendor/katex/auto-render.min.js",
        ):
            protected = self.call(route)
            self.assertEqual((protected[0], protected[1]["location"]), (303, "/login"))
        icons = self.call("/web/nav-icons.svg", cookie=cookie)
        self.assertEqual((icons[0], icons[1]["content-type"]), (200, "image/svg+xml"))
        self.assertIn(b'<symbol id="workbench"', icons[2])
        katex = self.call("/web/vendor/katex/katex.min.js", cookie=cookie)
        self.assertEqual((katex[0], katex[1]["content-type"]), (200, "text/javascript; charset=utf-8"))
        self.assertIn(b"KaTeX", katex[2])
        self.assertEqual(self.call("/web/vendor/katex/auto-render.min.js", cookie=cookie)[0], 200)
        self.assertEqual(self.call("/assets/branding/../README.md")[0], 401)

    def test_question_assets_are_integrity_checked_and_account_scoped(self) -> None:
        cookie = self.login("13600136011")
        other_cookie = self.login("13600136012")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        assert user is not None
        content = self.png_bytes()
        digest = hashlib.sha256(content).hexdigest()
        filename = f"{digest}.png"
        self.notebook.storage.save_bytes(f"bank-assets/{filename}", content, "image/png")
        self.domain_store.errors["a" * 32] = ErrorEntry(
            "a" * 32, user.user_id, "b" * 32, f"题目\n![原题图](bank-assets/{filename})",
            "", None, "open", datetime.now(timezone.utc), None, None,
        )

        self.assertEqual(self.call(f"/v1/question-assets/{filename}")[0], 401)
        own = self.call(f"/v1/question-assets/{filename}", cookie=cookie)
        self.assertEqual((own[0], own[1]["content-type"], own[2]), (200, "image/png", content))
        self.assertEqual(self.call(f"/v1/question-assets/{filename}", cookie=other_cookie)[0], 404)
        self.assertEqual(self.call("/v1/question-assets/not-an-asset.png", cookie=cookie)[0], 404)
        self.notebook.storage.resolve(f"bank-assets/{filename}").write_bytes(b"corrupted")
        self.assertEqual(self.call(f"/v1/question-assets/{filename}", cookie=cookie)[0], 409)

    def test_retired_admin_routes_are_not_found_with_or_without_login(self) -> None:
        cookie = self.login("13600136010")
        for route in ("/admin", "/admin/", "/web/admin.html", "/web/admin.js", "/v1/admin", "/v1/admin/dashboard?limit=25"):
            for session in (None, cookie):
                for method in ("GET", "POST"):
                    with self.subTest(route=route, logged_in=bool(session), method=method):
                        response = self.call(route, method=method, cookie=session)
                        self.assertEqual(response[0], 404)
                        self.assertEqual(response[2]["error"]["code"], "not_found")
        self.assertEqual(self.call("/v1/workbench", cookie=cookie)[0], 200)
        self.assertEqual(self.call("/v1/workbench")[0], 401)

    def test_harness_token_usage_is_bound_to_the_authenticated_user(self) -> None:
        cookie = self.login("13600136020")
        other_cookie = self.login("13600136021")
        harness_origin = "https://example.test"
        session_id = "session-token-usage"
        bound = self.call(
            "/v1/harness/sessions/bind", method="POST", payload={"session_id": session_id},
            cookie=cookie, origin=harness_origin,
        )
        self.assertEqual((bound[0], bound[2]), (200, {"status": "bound"}))
        usage = self.call(
            "/v1/harness/sessions/usage", method="POST",
            payload={
                "session_id": session_id, "uncached_input_tokens": 120,
                "output_tokens": 40, "cache_read_tokens": 80, "cache_write_tokens": 5,
            },
            cookie=cookie, origin=harness_origin,
        )
        self.assertEqual((usage[0], usage[2]), (200, {"status": "recorded"}))
        record = next(iter(self.domain_store.model_usage_sessions.values()))
        self.assertEqual(
            (record["uncached_input_tokens"], record["output_tokens"], record["cache_read_tokens"], record["cache_write_tokens"]),
            (120, 40, 80, 5),
        )
        rebound = self.call(
            "/v1/harness/sessions/bind", method="POST", payload={"session_id": session_id},
            cookie=other_cookie, origin=harness_origin,
        )
        self.assertEqual(rebound[0], 403)

    def test_harness_navigation_status_is_read_only_and_account_scoped(self) -> None:
        cookie = self.login("13600136022")
        other_cookie = self.login("13600136023")
        harness_origin = "https://example.test"
        empty = self.call("/v1/harness/navigation-status", cookie=cookie, origin=harness_origin)
        self.assertEqual(empty[0], 200)
        self.assertNotIn("access-control-allow-origin", empty[1])
        self.assertEqual(empty[2]["errors"]["count"], 0)
        self.assertEqual(empty[2]["practice"]["count"], 0)
        self.assertEqual(empty[2]["progress"], {"due_count": 0, "needs_correction_count": 0, "deferred_count": 0})

        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        now = datetime.now(timezone.utc)
        error_id = "7" * 32
        task_id = "8" * 32
        self.domain_store.errors[error_id] = ErrorEntry(error_id, user.user_id, "9" * 32, "题目", "作答", "错因", "open", now)
        self.domain_store.review_tasks[task_id] = ReviewTask(task_id, user.user_id, error_id, 1, now - timedelta(minutes=1), "pending")
        changed = self.call("/v1/harness/navigation-status", cookie=cookie, origin=harness_origin)
        self.assertEqual((changed[2]["errors"]["count"], changed[2]["progress"]["due_count"]), (1, 1))
        self.assertNotEqual(changed[2]["errors"]["revision"], empty[2]["errors"]["revision"])
        deferred = self.call(f"/v1/reviews/{task_id}/defer", method="POST", payload={"days": 3, "reason": "prerequisite_not_learned"}, cookie=cookie, idempotency_key="nav-defer")
        self.assertEqual((deferred[0], deferred[2]["review"]["deferred"], deferred[2]["review"]["stage"]), (200, True, 1))
        repeated = self.call(f"/v1/reviews/{task_id}/defer", method="POST", payload={"days": 3, "reason": "prerequisite_not_learned"}, cookie=cookie, idempotency_key="nav-defer")
        self.assertEqual(repeated[2]["review"]["due_at"], deferred[2]["review"]["due_at"])
        delayed_status = self.call("/v1/harness/navigation-status", cookie=cookie, origin=harness_origin)
        self.assertEqual(delayed_status[2]["progress"], {"due_count": 0, "needs_correction_count": 0, "deferred_count": 1})
        self.assertEqual(self.call(f"/v1/reviews/{task_id}/defer", method="POST", payload={"days": 1, "reason": "prerequisite_not_learned"}, cookie=other_cookie, idempotency_key="other-defer")[0], 404)
        resumed = self.call(f"/v1/reviews/{task_id}/resume", method="POST", payload={}, cookie=cookie, idempotency_key="nav-resume")
        self.assertEqual((resumed[0], resumed[2]["review"]["deferred"]), (200, False))
        self.domain_store.complete_review(user_id=user.user_id, task_id=task_id, result="partial", idempotency_key="nav-partial", now=datetime.now(timezone.utc))
        corrected = self.call("/v1/harness/navigation-status", cookie=cookie, origin=harness_origin)
        self.assertEqual(corrected[2]["progress"], {"due_count": 0, "needs_correction_count": 1, "deferred_count": 0})

        isolated = self.call("/v1/harness/navigation-status", cookie=other_cookie, origin=harness_origin)
        self.assertEqual(isolated[2]["errors"]["count"], 0)
        self.assertNotEqual(isolated[2]["scope"], changed[2]["scope"])
        denied = self.call("/v1/harness/navigation-status", cookie=cookie, origin="https://evil.example")
        self.assertEqual((denied[0], denied[2]["error"]["code"]), (403, "forbidden"))
        preflight = self.call("/v1/harness/navigation-status", method="OPTIONS", origin="http://example.test:3080")
        self.assertEqual((preflight[0], preflight[2]["error"]["code"]), (403, "forbidden"))

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

    def test_intake_batch_creation_snapshot_and_owned_get(self) -> None:
        class RecordingEngine:
            def __init__(self):
                self.started = 0

            def start(self):
                self.started += 1

        engine = RecordingEngine()
        self.app.intake_batch_engine = engine
        cookie = self.login("13100131001")
        content_type, body = self.multipart("question.png", self.png_bytes())
        uploaded = self.call(
            "/v1/files", method="POST", body=body, content_type=content_type,
            cookie=cookie, idempotency_key="batch-upload",
        )
        created = self.call(
            "/v1/intake/batches", method="POST", payload={"file_ids": [uploaded[2]["file_id"]]},
            cookie=cookie, idempotency_key="batch-create",
        )
        replay = self.call(
            "/v1/intake/batches", method="POST", payload={"file_ids": [uploaded[2]["file_id"]]},
            cookie=cookie, idempotency_key="batch-create",
        )
        self.assertEqual((created[0], replay[0], engine.started), (202, 202, 2))
        self.assertEqual(created[2], replay[2])
        self.assertEqual(created[2]["status"], "pending")
        self.assertEqual(created[2]["events_url"], f"/v1/intake/batches/{created[2]['batch_id']}/events")
        fetched = self.call(f"/v1/intake/batches/{created[2]['batch_id']}", cookie=cookie)
        self.assertEqual((fetched[0], fetched[2]["last_event_id"]), (200, 1))
        other = self.login("13100131002")
        self.assertEqual(self.call(f"/v1/intake/batches/{created[2]['batch_id']}", cookie=other)[0], 404)

    def test_intake_batch_creation_returns_rate_limit_after_three_active_batches(self) -> None:
        class RecordingEngine:
            def start(self):
                pass

        self.app.intake_batch_engine = RecordingEngine()
        cookie = self.login("13100131009")
        content_type, body = self.multipart("question.png", self.png_bytes())
        uploaded = self.call(
            "/v1/files", method="POST", body=body, content_type=content_type,
            cookie=cookie, idempotency_key="batch-limit-upload",
        )
        created = [
            self.call(
                "/v1/intake/batches",
                method="POST",
                payload={"file_ids": [uploaded[2]["file_id"]]},
                cookie=cookie,
                idempotency_key=f"batch-limit-{index}",
            )
            for index in range(3)
        ]
        replay = self.call(
            "/v1/intake/batches",
            method="POST",
            payload={"file_ids": [uploaded[2]["file_id"]]},
            cookie=cookie,
            idempotency_key="batch-limit-0",
        )
        rejected = self.call(
            "/v1/intake/batches",
            method="POST",
            payload={"file_ids": [uploaded[2]["file_id"]]},
            cookie=cookie,
            idempotency_key="batch-limit-overflow",
        )
        self.assertEqual([response[0] for response in created], [202, 202, 202])
        self.assertEqual((replay[0], replay[2]["batch_id"]), (202, created[0][2]["batch_id"]))
        self.assertEqual((rejected[0], rejected[2]["error"]["code"]), (429, "batch_limit_reached"))

    def test_intake_batch_sse_replays_terminal_events_and_hides_cursor_from_other_users(self) -> None:
        cookie = self.login("13100131003")
        content_type, body = self.multipart("question.png", self.png_bytes())
        uploaded = self.call(
            "/v1/files", method="POST", body=body, content_type=content_type,
            cookie=cookie, idempotency_key="sse-upload",
        )
        user_id = next(iter(self.auth_store.users_by_phone.values())).user_id
        batch = self.domain_store.batch_repository.create_batch(
            user_id=user_id,
            file_ids=[uploaded[2]["file_id"]],
            idempotency_key="sse-batch",
        )[0]
        claim = self.domain_store.batch_repository.claim_next(worker_id="test-worker", lease_seconds=300)
        assert claim is not None
        self.domain_store.batch_repository.transition(claim, expected="pending", target="slicing")
        self.domain_store.batch_repository.record_operation(
            claim, operation_key="slice:1", stage="slicing", ordinal=1,
            result={"intake_ids": ["i1"]}, completed_files_delta=1,
        )
        self.domain_store.batch_repository.transition(claim, expected="slicing", target="solving", total_items=1)
        self.domain_store.batch_repository.record_operation(
            claim, operation_key="solve:1", stage="solving", ordinal=1, result={"ok": True},
        )
        self.domain_store.batch_repository.transition(claim, expected="solving", target="grading")
        self.domain_store.batch_repository.record_operation(
            claim, operation_key="grade:1", stage="grading", ordinal=1,
            result={"ok": True}, completed_items_delta=1,
            event_data={
                "item_no": 1, "question_text": "1+1=?", "answer_text": "3",
                "verdict": "incorrect", "auto_saved": True, "notebook_status": "saved",
                "snapshot_truncated": False,
            },
        )
        self.domain_store.batch_repository.transition(claim, expected="grading", target="completed")

        discovery = self.call("/v1/intake/batches/active", cookie=cookie)
        self.assertIsNone(discovery[2]["batch"])
        self.assertRegex(discovery[2]["recovery_cursor"], r"^(0|[1-9][0-9]{0,15})$")
        recovered = self.call("/v1/intake/batches/active?updated_after=0", cookie=cookie)
        self.assertEqual((recovered[0], recovered[2]["batch"]["batch_id"]), (200, batch.batch_id))
        self.assertIn("recovery_cursor", recovered[2])
        self.assertEqual(
            self.call("/v1/intake/batches/active?updated_after=9999999999999", cookie=cookie)[0],
            400,
        )

        replay = self.call(
            f"/v1/intake/batches/{batch.batch_id}/events", cookie=cookie,
            extra_headers={"Last-Event-ID": "2"},
        )
        self.assertEqual((replay[0], replay[1]["content-type"]), (200, "text/event-stream; charset=utf-8"))
        self.assertIn(b"event: item_completed", replay[2])
        self.assertIn(b'"auto_saved":true', replay[2])
        self.assertIn(b"event: batch_completed", replay[2])
        self.assertNotIn(b"id: 1\n", replay[2])
        other = self.login("13100131004")
        hidden = self.call(
            f"/v1/intake/batches/{batch.batch_id}/events", cookie=other,
            extra_headers={"Last-Event-ID": "999999"},
        )
        self.assertEqual((hidden[0], hidden[2]["error"]["code"]), (404, "not_found"))

    def test_intake_batch_sse_caps_live_connections_and_releases_on_disconnect(self) -> None:
        cookie = self.login("13100131010")
        content_type, body = self.multipart("question.png", self.png_bytes())
        uploaded = self.call(
            "/v1/files", method="POST", body=body, content_type=content_type,
            cookie=cookie, idempotency_key="sse-limit-upload",
        )
        user_id = next(iter(self.auth_store.users_by_phone.values())).user_id
        batch = self.domain_store.batch_repository.create_batch(
            user_id=user_id,
            file_ids=[uploaded[2]["file_id"]],
            idempotency_key="sse-limit-batch",
        )[0]

        async def scenario() -> None:
            route = f"/v1/intake/batches/{batch.batch_id}/events"
            scope = {
                "type": "http", "method": "GET", "path": route, "query_string": b"",
                "scheme": "https", "client": ("203.0.113.7", 12345),
                "headers": [
                    (b"host", b"example.test"),
                    (b"content-type", b"application/json"),
                    (b"x-device-id", b"browser-device-001"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"https://example.test"),
                ],
            }

            async def open_stream(disconnect: asyncio.Event):
                first_request = True
                messages: list[dict] = []
                started = asyncio.Event()

                async def receive():
                    nonlocal first_request
                    if first_request:
                        first_request = False
                        return {"type": "http.request", "body": b"", "more_body": False}
                    await disconnect.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    messages.append(message)
                    if message["type"] == "http.response.start":
                        started.set()

                task = asyncio.create_task(self.app(scope, receive, send))
                await asyncio.wait_for(started.wait(), timeout=1)
                return task, messages

            first_disconnect = asyncio.Event()
            second_disconnect = asyncio.Event()
            first_task, first_messages = await open_stream(first_disconnect)
            second_task, second_messages = await open_stream(second_disconnect)
            third_disconnect = asyncio.Event()
            third_task, third_messages = await open_stream(third_disconnect)
            await asyncio.wait_for(third_task, timeout=1)
            self.assertEqual(third_messages[0]["status"], 429)

            first_disconnect.set()
            second_disconnect.set()
            await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=1)
            self.assertEqual(first_messages[0]["status"], 200)
            self.assertEqual(second_messages[0]["status"], 200)

            fourth_disconnect = asyncio.Event()
            fourth_task, fourth_messages = await open_stream(fourth_disconnect)
            self.assertEqual(fourth_messages[0]["status"], 200)
            fourth_disconnect.set()
            await asyncio.wait_for(fourth_task, timeout=1)

        asyncio.run(scenario())

    def test_harness_bridge_enqueues_one_async_batch_and_returns_owned_status(self) -> None:
        class RecordingEngine:
            def __init__(self):
                self.started = 0

            def start(self):
                self.started += 1

        engine = RecordingEngine()
        self.app.intake_batch_engine = engine
        cookie = self.login("13100131005")
        bound = self.call(
            "/v1/harness/sessions/bind", method="POST", payload={"session_id": "harness-session-1"}, cookie=cookie,
        )
        self.assertEqual(bound[0], 200)
        content = self.png_bytes()
        digest = hashlib.sha256(content).hexdigest()
        created = self.call(
            "/v1/internal/harness/intake-batches",
            method="POST",
            payload={
                "session_id": "harness-session-1",
                "attachments": [{
                    "attachment_id": f"sha256:{digest}", "name": "question.png",
                    "media_type": "image/png", "data": base64.b64encode(content).decode("ascii"),
                }],
            },
            extra_headers={"Authorization": "Bearer test-internal-token"},
            client=("127.0.0.1", 12345),
        )
        self.assertEqual((created[0], created[2]["status"], engine.started), (202, "pending", 1))
        fetched = self.call(
            f"/v1/internal/harness/intake-batches/{created[2]['batch_id']}",
            method="GET",
            extra_headers={
                "Authorization": "Bearer test-internal-token",
                "X-LZLM-Session-ID": "harness-session-1",
            },
            client=("127.0.0.1", 12345),
        )
        self.assertEqual((fetched[0], fetched[2]["batch_id"]), (200, created[2]["batch_id"]))
        renamed = self.call(
            "/v1/internal/harness/intake-batches", method="POST",
            payload={"session_id": "harness-session-1", "attachments": [{
                "attachment_id": f"sha256:{digest}", "name": "clipboard.png",
                "media_type": "image/png", "data": base64.b64encode(content).decode("ascii"),
            }]},
            extra_headers={"Authorization": "Bearer test-internal-token"},
            client=("127.0.0.1", 12345),
        )
        self.assertEqual(renamed[0], 202)
        self.assertEqual(renamed[2]["batch_id"], created[2]["batch_id"])
        self.assertEqual(len(self.domain_store.files), 1)

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
        harness_origin = "https://example.test"
        bound = self.call("/v1/harness/sessions/bind", method="POST", payload={"session_id": "session-receipt"}, cookie=cookie, origin=harness_origin)
        self.assertEqual((bound[0], bound[2]), (200, {"status": "bound"}))
        self.assertNotIn("access-control-allow-origin", bound[1])

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

        missing_confirmation = self.call(
            "/v1/internal/harness/errors/remove", method="POST",
            payload={"session_id": "session-receipt", "error_id": saved[2]["receipt"]["error_id"], "confirmation_text": "请移除这道错题"},
            **internal,
        )
        self.assertEqual((missing_confirmation[0], missing_confirmation[2]["error"]["code"]), (403, "forbidden"))
        wrong_owner = self.call(
            "/v1/internal/harness/errors/remove", method="POST",
            payload={"session_id": "session-other", "error_id": saved[2]["receipt"]["error_id"], "confirmation_text": f"确认移除错题 {saved[2]['receipt']['error_id']}"},
            **internal,
        )
        self.assertEqual((wrong_owner[0], wrong_owner[2]["error"]["code"]), (404, "not_found"))
        removed = self.call(
            "/v1/internal/harness/errors/remove", method="POST",
            payload={"session_id": "session-receipt", "error_id": saved[2]["receipt"]["error_id"], "confirmation_text": f"确认移除错题 {saved[2]['receipt']['error_id']}"},
            **internal,
        )
        self.assertEqual((removed[0], removed[2]["receipt"]["status"]), (200, "removed"))
        self.assertEqual(removed[2]["receipt"]["schema"], "math-notebook-removal-receipt/v1")
        self.assertEqual(self.domain_store.errors[saved[2]["receipt"]["error_id"]].status, "removed")
        self.assertEqual(self.call("/v1/errors", cookie=cookie)[2]["items"], [])

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
                "reference_status": "not_found",
            },
        )
        self.assertEqual(self.app._grade_receipt(unclear)["status"], "needs_review")

    def test_harness_attachment_bridge_freezes_grades_and_writes_authoritative_receipts(self) -> None:
        cookie = self.login("13500135003")
        harness_origin = "https://example.test"
        bound = self.call(
            "/v1/harness/sessions/bind",
            method="POST",
            payload={"session_id": "session-process"},
            cookie=cookie,
            origin=harness_origin,
        )
        self.assertEqual(bound[0], 200)
        self.domain_store.add_question(Question(
            "1" * 32,
            "若 x+1=2，求 x。",
            "x=1",
            7,
            1.0,
            "公开验证题库",
            solution_text="移项得 x=1。",
            version_id="2" * 32,
            version_no=3,
        ))
        content = self.png_bytes()
        digest = hashlib.sha256(content).hexdigest()
        content_type, body = self.multipart("legacy-upload.png", content)
        uploaded = self.call(
            "/v1/files", method="POST", body=body, content_type=content_type, cookie=cookie,
            idempotency_key="legacy-before-harness",
        )
        created = self.call(
            "/v1/intakes", method="POST", payload={"file_id": uploaded[2]["file_id"]}, cookie=cookie,
            idempotency_key="legacy-intake-before-harness",
        )
        self.call(
            f"/v1/intakes/{created[2]['resource_id']}/manual-candidate",
            method="POST",
            payload={"question_text": "旧识别文本", "answer_text": ""},
            cookie=cookie,
        )
        payload = {
            "session_id": "session-process",
            "attachment": {
                "attachment_id": f"sha256:{digest}",
                "name": "two-questions.png",
                "media_type": "image/png",
                "data": base64.b64encode(content).decode("ascii"),
            },
            "items": [
                {
                    "item_no": 1,
                    "question_text": "若 x+1=2，求 x。",
                    "answer_text": "x=0",
                    "verdict": "incorrect",
                    "first_error": "移项时常数项处理错误",
                    "cause_code": "algebra_transform",
                    "cause_evidence": "学生把 1 移到等号右边后仍写成 0",
                    "knowledge_points": ["一元一次方程", "等式性质与移项"],
                    "correct_solution": "由 x+1=2，移项得 x=2-1=1。",
                    "final_answer": "x=1",
                    "prevention_cue": "移项后代回原式验算",
                    "confidence": 0.98,
                },
                {
                    "item_no": 2,
                    "question_text": "计算 2+3。",
                    "answer_text": "5",
                    "verdict": "correct",
                    "first_error": "",
                    "cause_code": "",
                    "cause_evidence": "",
                    "knowledge_points": ["整数加法"],
                    "correct_solution": "2+3=5。",
                    "final_answer": "5",
                    "prevention_cue": "保持书写清晰",
                    "confidence": 0.99,
                },
            ],
        }
        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        self.app._harness_sessions.clear()
        first = self.call("/v1/internal/harness/intakes/process", method="POST", payload=payload, **internal)
        replay = self.call("/v1/internal/harness/intakes/process", method="POST", payload=payload, **internal)
        frozen_state = (
            dict(self.domain_store.candidates), dict(self.domain_store.errors), dict(self.domain_store.review_tasks),
            self.domain_store.learning_usage(user_id=next(iter(self.domain_store.intakes.values())).user_id),
        )
        changed_grade = json.loads(json.dumps(payload))
        changed_grade["items"][1].update(
            verdict="incorrect", first_error="圆心判断错误", cause_code="geometry",
            cause_evidence="把圆心写成三角形顶点", knowledge_points=["圆的几何性质"],
            correct_solution="应先确定圆心。", final_answer="半径为 99", prevention_cue="先画辅助线", confidence=0.51,
        )
        changed_grade_replay = self.call(
            "/v1/internal/harness/intakes/process", method="POST", payload=changed_grade, **internal,
        )
        owner_id = next(iter(self.domain_store.intakes.values())).user_id
        self.app._harness_processes.add((owner_id, digest))
        concurrent_conflict = self.call(
            "/v1/internal/harness/intakes/process", method="POST", payload=changed_grade, **internal,
        )
        self.assertIn((owner_id, digest), self.app._harness_processes)
        self.app._harness_processes.discard((owner_id, digest))
        changed_text = json.loads(json.dumps(payload))
        changed_text["items"][0]["question_text"] = "若 x + 1 = 2，求 x（模型重试时的等价整理）。"
        changed_text["items"][0]["answer_text"] = "学生写的是 x = 0。"
        changed_text_replay = self.call("/v1/internal/harness/intakes/process", method="POST", payload=changed_text, **internal)
        changed_count = json.loads(json.dumps(payload))
        changed_count["items"] = changed_count["items"][:1]
        changed_count_replay = self.call("/v1/internal/harness/intakes/process", method="POST", payload=changed_count, **internal)
        renamed = json.loads(json.dumps(payload))
        renamed["attachment"]["name"] = "clipboard.png"
        renamed_replay = self.call("/v1/internal/harness/intakes/process", method="POST", payload=renamed, **internal)
        self.assertEqual(renamed_replay[0], 200)
        self.assertEqual(renamed_replay[2]["results"], replay[2]["results"])

        self.assertEqual((first[0], replay[0], changed_grade_replay[0], changed_text_replay[0], changed_count_replay[0]), (200, 200, 200, 200, 200))
        self.assertEqual((concurrent_conflict[0], concurrent_conflict[2]["error"]["code"]), (409, "conflict"))
        self.assertEqual(changed_text_replay[2]["results"], replay[2]["results"])
        self.assertEqual(changed_count_replay[2]["results"], replay[2]["results"])
        self.assertEqual([item["receipt_status"] for item in first[2]["results"]], ["saved", "not_saved_correct"])
        self.assertEqual([item["receipt_status"] for item in replay[2]["results"]], ["already_saved", "not_saved_correct"])
        self.assertEqual([item["receipt_status"] for item in changed_grade_replay[2]["results"]], ["already_saved", "not_saved_correct"])
        self.assertEqual([item["verdict"] for item in changed_grade_replay[2]["results"]], ["incorrect", "correct"])
        self.assertIn("题库第 3 版参考答案确定性校验一致", first[2]["results"][0]["receipt_message"])
        self.assertEqual(first[2]["results"][0]["error_id"], replay[2]["results"][0]["error_id"])
        self.assertEqual(first[2]["results"][0]["error_id"], changed_grade_replay[2]["results"][0]["error_id"])
        self.assertEqual(first[2]["results"][0]["knowledge_points"], ["一元一次方程", "等式性质与移项"])
        self.assertEqual(
            frozen_state,
            (dict(self.domain_store.candidates), dict(self.domain_store.errors), dict(self.domain_store.review_tasks),
             self.domain_store.learning_usage(user_id=next(iter(self.domain_store.intakes.values())).user_id)),
        )
        self.assertEqual(len(self.domain_store.files), 1)
        self.assertEqual(len(self.domain_store.intakes), 2)
        self.assertEqual(len(self.domain_store.attempts), 2)
        self.assertEqual(len(self.domain_store.errors), 1)
        self.assertEqual(len(self.domain_store.review_tasks), 1)
        self.assertEqual(len(self.call("/v1/errors", cookie=cookie)[2]["items"]), 1)

        unbound = payload | {"session_id": "session-unbound"}
        denied = self.call("/v1/internal/harness/intakes/process", method="POST", payload=unbound, **internal)
        self.assertEqual((denied[0], denied[2]["error"]["code"]), (403, "forbidden"))

        stream = BytesIO()
        Image.new("RGB", (10, 10), "blue").save(stream, format="PNG")
        cleanup_content = stream.getvalue()
        cleanup_digest = hashlib.sha256(cleanup_content).hexdigest()
        cleanup_payload = json.loads(json.dumps(payload))
        cleanup_payload["attachment"].update(
            attachment_id=f"sha256:{cleanup_digest}", name="cleanup-failure.png",
            data=base64.b64encode(cleanup_content).decode("ascii"),
        )
        finish_grade_usage = self.domain_store.finish_grade_usage

        def fail_usage_cleanup(**_kwargs):
            raise RuntimeError("usage cleanup failed")

        self.domain_store.finish_grade_usage = fail_usage_cleanup
        try:
            with self.assertRaisesRegex(RuntimeError, "usage cleanup failed"):
                self.call("/v1/internal/harness/intakes/process", method="POST", payload=cleanup_payload, **internal)
        finally:
            self.domain_store.finish_grade_usage = finish_grade_usage
        self.assertNotIn((owner_id, cleanup_digest), self.app._harness_processes)

    def test_harness_reference_conflict_requires_frozen_semantic_adjudication(self) -> None:
        cookie = self.login("13500135004")
        other_cookie = self.login("13500135005")
        harness_origin = "https://example.test"
        self.call(
            "/v1/harness/sessions/bind", method="POST",
            payload={"session_id": "session-reference-review"}, cookie=cookie, origin=harness_origin,
        )
        self.call(
            "/v1/harness/sessions/bind", method="POST",
            payload={"session_id": "session-reference-other"}, cookie=other_cookie, origin=harness_origin,
        )
        self.domain_store.add_question(Question(
            "4" * 32,
            "若 x+1=2，求 x。",
            "x=1",
            7,
            1.0,
            "公开验证题库",
            solution_text="等式两边同时减去 1，得到 x=1。",
            version_id="5" * 32,
            version_no=6,
        ))
        content = self.png_bytes()
        digest = hashlib.sha256(content).hexdigest()
        payload = {
            "session_id": "session-reference-review",
            "attachment": {
                "attachment_id": f"sha256:{digest}",
                "name": "semantic-reference.png",
                "media_type": "image/png",
                "data": base64.b64encode(content).decode("ascii"),
            },
            "items": [{
                "item_no": 1,
                "question_text": "若 x+1=2，求 x。",
                "answer_text": "x=0",
                "verdict": "incorrect",
                "first_error": "移项时常数项处理错误",
                "cause_code": "algebra_transform",
                "cause_evidence": "学生把等号左侧的 1 消去后写成 x=0",
                "knowledge_points": ["一元一次方程", "等式性质"],
                "correct_solution": "两边同时减去 1，得到 x=1。",
                "final_answer": "方程的唯一实数解是 x=1",
                "prevention_cue": "完成移项后代回原式验算",
                "confidence": 0.96,
            }],
        }
        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        processed = self.call("/v1/internal/harness/intakes/process", method="POST", payload=payload, **internal)
        self.assertEqual(processed[0], 200)
        result = processed[2]["results"][0]
        self.assertEqual(result["receipt_status"], "needs_review")
        self.assertEqual(result["reference_review"], {
            "source_title": "公开验证题库",
            "version_no": 6,
            "independent_answer": "方程的唯一实数解是 x=1",
            "reference_answer": "x=1",
            "reference_solution": "等式两边同时减去 1，得到 x=1。",
        })
        self.assertEqual(len(self.domain_store.errors), 0)

        uncertain = self.call(
            "/v1/internal/harness/reference-conflicts/adjudicate", method="POST",
            payload={"session_id": "session-reference-review", "items": [{
                "candidate_id": result["candidate_id"], "input_version": result["input_version"],
                "status": "uncertain", "rationale": "现有图片证据不足，无法确认两种答案写法是否严格等价。",
            }]},
            **internal,
        )
        self.assertEqual((uncertain[0], uncertain[2]["results"][0]["status"]), (200, "needs_review"))
        self.assertEqual(len(self.domain_store.errors), 0)

        denied = self.call(
            "/v1/internal/harness/reference-conflicts/adjudicate", method="POST",
            payload={"session_id": "session-reference-other", "items": [{
                "candidate_id": result["candidate_id"], "input_version": result["input_version"],
                "status": "consistent", "rationale": "两份答案都明确给出唯一实数解 x=1，因此数学结论完全一致。",
            }]},
            **internal,
        )
        self.assertEqual((denied[0], denied[2]["error"]["code"]), (404, "not_found"))

        self.domain_store.add_question(Question(
            "4" * 32, "若 x+1=2，求 x。", "x=2", 7, 1.0, "公开验证题库",
            solution_text="更新后的解析。", version_id="6" * 32, version_no=7,
        ))
        stale = self.call(
            "/v1/internal/harness/reference-conflicts/adjudicate", method="POST",
            payload={"session_id": "session-reference-review", "items": [{
                "candidate_id": result["candidate_id"], "input_version": result["input_version"],
                "status": "consistent", "rationale": "两份答案都明确给出唯一实数解 x=1，因此数学结论完全一致。",
            }]},
            **internal,
        )
        self.assertEqual((stale[0], stale[2]["error"]["code"]), (409, "reference_conflict"))
        self.domain_store.add_question(Question(
            "4" * 32, "若 x+1=2，求 x。", "x=1", 7, 1.0, "公开验证题库",
            solution_text="等式两边同时减去 1，得到 x=1。", version_id="5" * 32, version_no=6,
        ))

        adjudicated = self.call(
            "/v1/internal/harness/reference-conflicts/adjudicate", method="POST",
            payload={"session_id": "session-reference-review", "items": [{
                "candidate_id": result["candidate_id"], "input_version": result["input_version"],
                "status": "consistent", "rationale": "独立答案与题库答案都明确给出唯一实数解 x=1，题库解析也验证了同一移项过程。",
            }]},
            **internal,
        )
        self.assertEqual((adjudicated[0], adjudicated[2]["results"][0]["status"]), (200, "saved"))
        self.assertIn("第二阶段语义复核一致", adjudicated[2]["results"][0]["receipt_message"])
        self.assertEqual(len(self.domain_store.errors), 1)
        self.assertEqual(adjudicated[2]["results"][0]["error_id"], next(iter(self.domain_store.errors)))
        evidence = json.loads(next(iter(self.domain_store.errors.values())).evidence)
        self.assertEqual(evidence["reference_adjudication"]["status"], "consistent")

    def test_verified_reference_overrides_conflicting_independent_grade(self) -> None:
        cookie = self.login("13500135016")
        harness_origin = "https://example.test"
        self.call(
            "/v1/harness/sessions/bind", method="POST",
            payload={"session_id": "session-reference-preferred"}, cookie=cookie, origin=harness_origin,
        )
        self.domain_store.add_question(Question(
            "9" * 32,
            "若 x+1=2，求 x。",
            "x=1",
            7,
            1.0,
            "公开验证题库",
            solution_text="等式两边同时减去 1，得到 x=1。",
            version_id="8" * 32,
            version_no=3,
        ))
        content = self.png_bytes()
        digest = hashlib.sha256(content).hexdigest()
        processed = self.call(
            "/v1/internal/harness/intakes/process", method="POST",
            payload={
                "session_id": "session-reference-preferred",
                "attachment": {
                    "attachment_id": f"sha256:{digest}", "name": "reference-preferred.png",
                    "media_type": "image/png", "data": base64.b64encode(content).decode("ascii"),
                },
                "items": [{
                    "item_no": 1, "question_text": "若 x+1=2，求 x。", "answer_text": "x=1",
                    "verdict": "incorrect", "first_error": "学生答案与独立推导不一致",
                    "cause_code": "calculation", "cause_evidence": "独立推导错误地得到 x=2",
                    "knowledge_points": ["一元一次方程"], "correct_solution": "错误地移项得到 x=2。",
                    "final_answer": "x=2", "prevention_cue": "代回检验", "confidence": 0.91,
                }],
            },
            origin=None, client=("127.0.0.1", 3080),
            extra_headers={"authorization": "Bearer test-internal-token"},
        )
        self.assertEqual(processed[0], 200)
        result = processed[2]["results"][0]
        self.assertEqual(result["receipt_status"], "needs_review")

        adjudicated = self.call(
            "/v1/internal/harness/reference-conflicts/adjudicate", method="POST",
            payload={"session_id": "session-reference-preferred", "items": [{
                "candidate_id": result["candidate_id"], "input_version": result["input_version"],
                "status": "conflict",
                "rationale": "独立推导得到 x=2，但题库当前验证答案和代回检验均表明 x=1，应以题库解析为准。",
                "authoritative_grade": {
                    "verdict": "correct", "first_error": "", "cause_code": "", "cause_evidence": "",
                    "knowledge_points": ["一元一次方程"], "prevention_cue": "", "confidence": 0.99,
                },
            }]},
            origin=None, client=("127.0.0.1", 3080),
            extra_headers={"authorization": "Bearer test-internal-token"},
        )
        self.assertEqual((adjudicated[0], adjudicated[2]["results"][0]["status"]), (200, "not_saved_correct"))
        self.assertEqual(adjudicated[2]["results"][0]["error_id"], "")
        self.assertIn("已按题库答案与解析重新判题", adjudicated[2]["results"][0]["receipt_message"])
        self.assertEqual(len(self.domain_store.errors), 0)
        revised = list(self.domain_store.candidates.values())[-1]
        diagnosis = json.loads(revised.evidence or "{}")
        self.assertEqual(revised.verdict, "correct")
        self.assertEqual(diagnosis["final_answer"], "x=1")
        self.assertEqual(diagnosis["correct_solution"], "等式两边同时减去 1，得到 x=1。")
        self.assertEqual(diagnosis["reference_adjudication"]["status"], "reference_preferred")
        self.assertIsNone(self.domain_store.find_reference_conflict_candidate(
            user_id=self.auth_service.authenticate_session(cookie.split("=", 1)[1]).user_id,
            question_text="若 x+1=2，求 x。",
        ))

    def test_harness_rechecks_historical_false_conflict_without_reupload(self) -> None:
        cookie = self.login("13500135006")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        assert user is not None
        harness_origin = "https://example.test"
        self.call(
            "/v1/harness/sessions/bind", method="POST",
            payload={"session_id": "session-historical-conflict"}, cookie=cookie, origin=harness_origin,
        )
        stem = "已知圆C经过两点，求圆方程、弦所在直线及参数范围。"
        question = Question(
            "7" * 32,
            stem,
            r"(1)$(x-2)^{2}+(y-3)^{2}=4$\n(2)$3x-4y+1=0$或$x=1$；\n(3)$\sqrt{13}-2\le m\le \sqrt{13}+2$",
            10,
            4.0,
            "公开验证题库",
            solution_text="三问依次由圆心、弦长和两圆位置关系得到。",
            version_id="8" * 32,
            version_no=1,
        )
        self.domain_store.add_question(question)
        attempt_id = "9" * 32
        self.domain_store.attempts[attempt_id] = Attempt(
            attempt_id, user.user_id, "a" * 32, 1, stem, "第（3）问未作答", "grade_ready",
        )
        final_answer = "(1) (x-2)²+(y-3)²=4；(2) x=1 或 3x-4y+1=0；(3) √13-2≤m≤√13+2。"
        old_conflict = {
            "schema": "question-bank-cross-validation/v1", "status": "conflict",
            "question_id": question.question_id, "version_id": question.version_id, "version_no": 1,
            "source_title": question.source_title, "match_score": 1.0,
            "reference_answer_sha256": "b" * 64, "independent_answer_sha256": "c" * 64,
        }
        candidate = self.domain_store.record_grade_candidate(
            user_id=user.user_id, attempt_id=attempt_id, input_version=1, verdict="partial",
            first_error="第（3）问未作答", evidence=json.dumps({
                "schema": "math-error-diagnosis/v1", "cause_code": "reasoning_gap",
                "cause_evidence": "第（3）问缺少作答。", "knowledge_points": ["圆与圆的位置关系"],
                "correct_solution": "完整解析", "final_answer": final_answer, "prevention_cue": "逐问检查",
                "cross_validation": old_conflict,
            }, ensure_ascii=False),
        )
        self.assertEqual(candidate.status, "candidate")
        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        rechecked = self.call(
            "/v1/internal/harness/reference-conflicts/recheck", method="POST",
            payload={"session_id": "session-historical-conflict", "question_text": stem}, **internal,
        )
        self.assertEqual((rechecked[0], rechecked[2]["result"]["receipt_status"]), (200, "saved"))
        self.assertIsNone(rechecked[2]["result"]["reference_review"])
        self.assertIn("确定性校验一致", rechecked[2]["result"]["receipt_message"])
        self.assertEqual(len(self.domain_store.errors), 1)
        self.assertEqual(rechecked[2]["result"]["error_id"], next(iter(self.domain_store.errors)))
        self.assertEqual(next(iter(self.domain_store.errors.values())).question_id, question.question_id)

    def test_harness_reads_verified_question_reference_by_exact_id(self) -> None:
        cookie = self.login("13500135007")
        harness_origin = "https://example.test"
        self.call(
            "/v1/harness/sessions/bind", method="POST",
            payload={"session_id": "session-question-reference"}, cookie=cookie, origin=harness_origin,
        )
        question = Question(
            "0a6af0c9d4a40cdfbbd15394b247d37a", "若 x+5=10，求 x。", "x=5", 10, 2.0, "授权题库",
            solution_text="等式两边同时减去 5，得到 x=5。", version_id="8" * 32, version_no=3,
        )
        self.domain_store.add_question(question, license_status="user_authorized")
        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        response = self.call(
            "/v1/internal/harness/question-bank/reference", method="POST",
            payload={"session_id": "session-question-reference", "question_id": question.question_id}, **internal,
        )
        self.assertEqual((response[0], response[2]["result"]["reference_answer"]), (200, "x=5"))
        self.assertEqual(response[2]["result"]["reference_solution"], "等式两边同时减去 5，得到 x=5。")
        self.assertEqual(response[2]["result"]["version_no"], 3)
        self.domain_store.add_question(
            Question("9" * 32, "受限题", "答案", 5, 1.0, "受限来源"), license_status="restricted",
        )
        forbidden_reference = self.call(
            "/v1/internal/harness/question-bank/reference", method="POST",
            payload={"session_id": "session-question-reference", "question_id": "9" * 32}, **internal,
        )
        self.assertEqual(forbidden_reference[0], 404)

    def test_harness_prepares_only_exact_verified_references_for_fast_grading(self) -> None:
        cookie = self.login("13500135008")
        self.call(
            "/v1/harness/sessions/bind", method="POST",
            payload={"session_id": "session-fast-reference"}, cookie=cookie, origin="https://example.test",
        )
        question = Question(
            "1" * 32, "若 x+5=10，求 x。", "x=5", 10, 2.0, "授权题库",
            solution_text="等式两边同时减去 5，得到 x=5。", version_id="2" * 32, version_no=4,
        )
        self.domain_store.add_question(question, license_status="user_authorized")
        content = self.png_bytes()
        digest = hashlib.sha256(content).hexdigest()
        attachment = {
            "attachment_id": f"sha256:{digest}", "name": "question.png", "media_type": "image/png",
            "data": base64.b64encode(content).decode("ascii"),
        }
        blank_review = {"code": "", "pdf_id": "", "error_id": "", "question_id": "", "stage": 0, "kind": ""}
        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        fuzzy_only = self.call(
            "/v1/internal/harness/grading-references", method="POST",
            payload={"session_id": "session-fast-reference", "attachment": attachment, "items": [
                {"item_no": 1, "question_text": question.stem_text, "review": blank_review},
            ]}, **internal,
        )
        self.assertEqual((fuzzy_only[0], fuzzy_only[2]["items"][0]["grading_strategy"]), (200, "independent"))
        exact_review = blank_review | {"question_id": question.question_id}
        exact = self.call(
            "/v1/internal/harness/grading-references", method="POST",
            payload={"session_id": "session-fast-reference", "attachment": attachment, "items": [
                {"item_no": 1, "question_text": "OCR 可有轻微误差", "review": exact_review},
            ]}, **internal,
        )
        prepared = exact[2]["items"][0]
        self.assertEqual((exact[0], prepared["grading_strategy"]), (200, "verified_reference"))
        self.assertEqual(
            (prepared["reference"]["question_id"], prepared["reference"]["version_no"], prepared["reference"]["reference_answer"]),
            (question.question_id, 4, "x=5"),
        )

    def test_fast_grading_accepts_unchanged_choice_reference_with_options(self) -> None:
        cookie = self.login("13500135019")
        session_id = "session-choice-reference"
        self.call("/v1/harness/sessions/bind", method="POST", cookie=cookie,
                  payload={"session_id": session_id}, origin="https://example.test")
        question = Question(
            "6" * 32, "计算 2+3，选择正确答案。", "A", 10, 1.0, "授权题库",
            solution_text="2+3=5，故选 A。", options=("A．5", "B．6", "C．7", "D．8"),
        )
        self.domain_store.add_question(question, license_status="user_authorized")
        content = self.png_bytes()
        attachment = {"attachment_id": f"sha256:{hashlib.sha256(content).hexdigest()}",
                      "name": "choice.png", "media_type": "image/png", "data": base64.b64encode(content).decode("ascii")}
        review = {"question_id": question.question_id}
        internal = {"origin": None, "client": ("127.0.0.1", 3080),
                    "extra_headers": {"authorization": "Bearer test-internal-token"}}
        frozen = self.call("/v1/internal/harness/grading-references", method="POST", payload={
            "session_id": session_id, "attachment": attachment,
            "items": [{"item_no": 1, "question_text": question.stem_text, "review": review}],
        }, **internal)[2]["items"][0]
        self.assertEqual(frozen["grading_strategy"], "verified_reference")
        item = {
            "item_no": 1, "question_text": question.stem_text, "answer_text": "A", "verdict": "correct",
            "first_error": "", "cause_code": "", "cause_evidence": "学生选 A，与参考一致",
            "knowledge_points": ["整数加法"], "correct_solution": frozen["reference"]["reference_solution"],
            "final_answer": "B", "prevention_cue": "核对选项", "confidence": 0.99,
            "grading_strategy": "verified_reference", "review": review,
        }
        payload = {"session_id": session_id, "attachment": attachment, "items": [item]}
        rejected = self.call("/v1/internal/harness/intakes/process", method="POST", payload=payload, **internal)
        self.assertEqual((rejected[0], rejected[2]["error"]["code"]), (409, "reference_changed"))
        self.assertEqual(len(self.domain_store.candidates), 0)
        item["final_answer"] = frozen["reference"]["reference_answer"]
        first = self.call("/v1/internal/harness/intakes/process", method="POST", payload=payload, **internal)
        replay = self.call("/v1/internal/harness/intakes/process", method="POST", payload=payload, **internal)
        self.assertEqual((first[0], replay[0]), (200, 200))
        self.assertEqual(first[2]["results"], replay[2]["results"])
        self.assertEqual(first[2]["results"][0]["verdict"], "correct")
        self.assertEqual(len(self.domain_store.candidates), 1)

    def test_changed_verified_reference_does_not_poison_intake_before_host_refresh_retry(self) -> None:
        cookie = self.login("13500135018")
        session_id = "session-reference-refresh"
        self.call(
            "/v1/harness/sessions/bind", method="POST",
            payload={"session_id": session_id}, cookie=cookie, origin="https://example.test",
        )
        question_id = "7" * 32
        old_question = Question(
            question_id, "若 x+5=10，求 x。", "x=5", 10, 2.0, "授权题库",
            solution_text="等式两边减 5，得 x=5。", version_id="8" * 32, version_no=1,
        )
        self.domain_store.add_question(old_question, license_status="user_authorized")
        content = self.png_bytes()
        digest = hashlib.sha256(content).hexdigest()
        attachment = {
            "attachment_id": f"sha256:{digest}", "name": "reference-refresh.png", "media_type": "image/png",
            "data": base64.b64encode(content).decode("ascii"),
        }
        review = {"code": "", "pdf_id": "", "error_id": "", "question_id": question_id, "stage": 0, "kind": ""}
        internal = {
            "origin": None, "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        frozen = self.call(
            "/v1/internal/harness/grading-references", method="POST",
            payload={"session_id": session_id, "attachment": attachment, "items": [
                {"item_no": 1, "question_text": old_question.stem_text, "review": review},
            ]}, **internal,
        )[2]["items"][0]
        self.domain_store.add_question(Question(
            question_id, old_question.stem_text, "x=6", 10, 2.0, "授权题库",
            solution_text="当前校正版答案为 x=6。", version_id="9" * 32, version_no=2,
        ), license_status="user_authorized")
        item = {
            "item_no": 1, "question_text": old_question.stem_text, "answer_text": "x=5",
            "verdict": "incorrect", "first_error": "使用了旧参考", "cause_code": "calculation",
            "cause_evidence": "学生答案与当前答案不同", "knowledge_points": ["一元一次方程"],
            "correct_solution": frozen["reference"]["reference_solution"],
            "final_answer": frozen["reference"]["reference_answer"], "prevention_cue": "核对当前版本",
            "confidence": 0.9, "grading_strategy": "verified_reference", "review": review,
        }
        payload = {"session_id": session_id, "attachment": attachment, "items": [item]}
        changed = self.call("/v1/internal/harness/intakes/process", method="POST", payload=payload, **internal)
        user_id = next(iter(self.domain_store.intakes.values())).user_id
        usage_after_rejection = self.domain_store.learning_usage(user_id=user_id)
        self.assertEqual((changed[0], changed[2]["error"]["code"]), (409, "reference_changed"))
        self.assertTrue(all(intake.status == "waiting_confirmation" for intake in self.domain_store.intakes.values()))
        self.assertEqual((len(self.domain_store.attempts), len(self.domain_store.candidates)), (0, 0))
        self.assertEqual((usage_after_rejection["grade"]["count"], usage_after_rejection["grade"]["pending"]), (0, 0))

        refreshed = self.call(
            "/v1/internal/harness/grading-references", method="POST",
            payload={"session_id": session_id, "attachment": attachment, "items": [
                {"item_no": 1, "question_text": old_question.stem_text, "review": review},
            ]}, **internal,
        )[2]["items"][0]
        retry_payload = json.loads(json.dumps(payload))
        retry_payload["items"][0].update(
            correct_solution=refreshed["reference"]["reference_solution"],
            final_answer=refreshed["reference"]["reference_answer"],
        )
        retry = self.call("/v1/internal/harness/intakes/process", method="POST", payload=retry_payload, **internal)
        replay = self.call("/v1/internal/harness/intakes/process", method="POST", payload=retry_payload, **internal)
        usage_after_replay = self.domain_store.learning_usage(user_id=user_id)
        self.assertEqual((retry[0], replay[0]), (200, 200))
        self.assertEqual((retry[2]["results"][0]["receipt_status"], replay[2]["results"][0]["receipt_status"]), ("review_unmatched", "review_unmatched"))
        self.assertEqual(retry[2]["results"][0]["candidate_id"], replay[2]["results"][0]["candidate_id"])
        self.assertEqual(len(self.domain_store.candidates), 1)
        self.assertEqual((usage_after_replay["grade"]["count"], usage_after_replay["grade"]["pending"]), (1, 0))

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

    def test_daily_learning_usage_is_account_scoped_and_reports_targets(self) -> None:
        cookie = self.login("13900139000")
        other_cookie = self.login("13900139001")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        assert user is not None
        self.domain_store.reserve_grade_batch(user_id=user.user_id, intake_ids=["a" * 32])
        self.domain_store.finish_grade_usage(user_id=user.user_id, intake_id="a" * 32, counted=True)
        usage = self.call("/v1/learning-usage", cookie=cookie)
        other = self.call("/v1/learning-usage", cookie=other_cookie)
        self.assertEqual((usage[0], usage[2]["grade"]["count"], usage[2]["grade"]["target"], usage[2]["grade"]["limit"]), (200, 1, 24, 40))
        self.assertEqual((usage[2]["recommendation"]["target"], usage[2]["recommendation"]["limit"]), (8, 24))
        self.assertEqual(other[2]["grade"]["count"], 0)

    def test_review_selection_scope_is_server_owned_and_stable(self) -> None:
        cookie = self.login("13900139000")
        other_cookie = self.login("13900139001")
        own = self.call("/v1/errors", cookie=cookie)
        again = self.call("/v1/errors", cookie=cookie)
        other = self.call("/v1/errors", cookie=other_cookie)
        self.assertEqual((own[0], again[0], other[0]), (200, 200, 200))
        self.assertRegex(own[2]["selection_scope"], r"^[0-9a-f]{24}$")
        self.assertEqual(own[2]["selection_scope"], again[2]["selection_scope"])
        self.assertNotEqual(own[2]["selection_scope"], other[2]["selection_scope"])
        self.assertNotIn("user_id", own[2])

    def test_pending_practice_review_link_is_user_selected_and_account_scoped(self) -> None:
        cookie = self.login("13800138010")
        other_cookie = self.login("13800138011")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        assert user is not None
        now = datetime.now(timezone.utc)
        error = ErrorEntry("e" * 32, user.user_id, "0" * 32, "原错题", "原作答", "原错因", "open", now)
        self.domain_store.errors[error.error_id] = error
        task = ReviewTask("t" * 32, user.user_id, error.error_id, 1, now - timedelta(days=1), "ready")
        self.domain_store.review_tasks[task.task_id] = task
        self.domain_store._review_keys[(user.user_id, error.error_id, 1)] = task.task_id
        question = Question("q" * 32, "解方程 x+2=4", "x=2", 10, 2.0, "公开验证题库")
        self.domain_store.add_question(question)
        self.domain_store.recommendations["r" * 32] = Recommendation(
            "r" * 32, user.user_id, error.error_id, question, "同知识点", "assigned"
        )
        paper = self.notebook.create_practice_pdf(
            user_id=user.user_id, error_ids=[error.error_id], idempotency_key="pending-link-paper"
        )
        recommendation = paper.checkpoint["review_manifest"][1]
        attempt_id = "a" * 32
        self.domain_store.attempts[attempt_id] = Attempt(
            attempt_id, user.user_id, "i" * 32, 1, question.stem_text, question.answer_text, "grade_ready"
        )
        evidence = json.dumps({
            "schema": "math-error-diagnosis/v1", "knowledge_points": ["方程"],
            "practice_review": {"status": "unmatched", "locator": {"kind": "recommendation"}},
        }, ensure_ascii=False)
        candidate = self.domain_store.record_grade_candidate(
            user_id=user.user_id, attempt_id=attempt_id, input_version=1, verdict="correct",
            first_error=None, evidence=evidence,
        )
        own = self.call("/v1/practice-review-links", cookie=cookie)
        other = self.call("/v1/practice-review-links", cookie=other_cookie)
        self.assertEqual((own[0], own[2]["count"], other[2]["count"]), (200, 1, 0))
        self.assertEqual(own[2]["items"][0]["options"][0]["code"], recommendation["code"])
        denied = self.call(
            f"/v1/practice-review-links/{candidate.candidate_id}", method="POST",
            payload={"input_version": 1, "code": recommendation["code"]}, cookie=other_cookie,
        )
        self.assertEqual(denied[0], 404)
        linked = self.call(
            f"/v1/practice-review-links/{candidate.candidate_id}", method="POST",
            payload={"input_version": 1, "code": recommendation["code"]}, cookie=cookie,
        )
        self.assertEqual((linked[0], linked[2]["receipt"]["status"]), (200, "review_waiting"))
        self.assertEqual(self.call("/v1/practice-review-links", cookie=cookie)[2]["count"], 0)
        self.assertEqual(len(self.domain_store.errors), 1)

    def test_harness_context_and_pdf_reflow_are_account_scoped_and_preserve_frozen_state(self) -> None:
        cookie = self.login("13800138012")
        other_cookie = self.login("13800138013")
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        assert user is not None
        harness_origin = "https://example.test"
        for session_id, owner_cookie in (("session-context", cookie), ("session-context-other", other_cookie)):
            bound = self.call(
                "/v1/harness/sessions/bind", method="POST", payload={"session_id": session_id},
                cookie=owner_cookie, origin=harness_origin,
            )
            self.assertEqual(bound[0], 200)
        now = datetime.now(timezone.utc)
        error = ErrorEntry("e" * 32, user.user_id, "0" * 32, "若 x+2=4，求 x。", "x=1", "移项错误", "open", now)
        self.domain_store.errors[error.error_id] = error
        task = ReviewTask("a" * 32, user.user_id, error.error_id, 1, now - timedelta(days=1), "ready")
        self.domain_store.review_tasks[task.task_id] = task
        self.domain_store._review_keys[(user.user_id, error.error_id, 1)] = task.task_id
        question = Question("1" * 32, "若 x+2=4，求 x。", "x=2", 10, 2.0, "公开验证题库", options=("A. 1", "B. 2"))
        self.domain_store.add_question(question)
        self.domain_store.recommendations["b" * 32] = Recommendation(
            "b" * 32, user.user_id, error.error_id, question, "同知识点", "assigned"
        )
        paper = self.notebook.create_practice_pdf(
            user_id=user.user_id, error_ids=[error.error_id], idempotency_key="context-reflow-paper"
        )

        def seed_progress(checkpoint, _get, _complete):
            code = checkpoint["review_manifest"][1]["code"]
            checkpoint["review_submissions"] = {code: {
                "candidate_id": "c" * 32, "verdict": "correct", "submitted_at": now.isoformat()
            }}
            checkpoint["review_receipts"] = {"frozen": {"status": "review_waiting"}}

        self.domain_store.mutate_practice_checkpoint(user_id=user.user_id, job_id=paper.job_id, operation=seed_progress)
        before = self.domain_store.get_job(user_id=user.user_id, job_id=paper.job_id)
        assert before is not None and before.checkpoint is not None
        old_file_id = before.checkpoint["file_id"]
        frozen_manifest = json.loads(json.dumps(before.checkpoint["review_manifest"]))
        frozen_submissions = json.loads(json.dumps(before.checkpoint["review_submissions"]))
        frozen_receipts = json.loads(json.dumps(before.checkpoint["review_receipts"]))
        generated_at = before.checkpoint["generated_at"]
        usage_before = self.domain_store.learning_usage(user_id=user.user_id)
        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        context_response = self.call(
            "/v1/internal/harness/context", method="POST",
            payload={"session_id": "session-context", "error_id": error.error_id,
                     "review_code": frozen_manifest[1]["code"]}, **internal,
        )
        self.assertEqual(context_response[0], 200)
        context = json.loads(context_response[2]["context_json"])
        self.assertEqual(context["scope"], "current_bound_account")
        self.assertEqual(context["query"], {
            "mode": "exact", "error_id": error.error_id, "review_code": frozen_manifest[1]["code"],
        })
        self.assertEqual(context["error"]["error_id"], error.error_id)
        self.assertEqual(context["review_item"]["status"], "correct")
        self.assertEqual(context["review_item"]["recommended_action"], "submit_remaining_required")
        self.assertEqual(context["review_item"]["pending_items"][0]["review_code"], frozen_manifest[0]["code"])
        self.assertEqual(set(context), {"scope", "query", "error", "review_item"})
        overview_response = self.call(
            "/v1/internal/harness/context", method="POST",
            payload={"session_id": "session-context"}, **internal,
        )
        self.assertEqual(overview_response[0], 200)
        overview = json.loads(overview_response[2]["context_json"])
        self.assertEqual(overview["query"], {"mode": "overview"})
        self.assertEqual(overview["progress"]["due_review_count"], 1)
        self.assertEqual(overview["practice_pdfs"][0]["task_id"], paper.job_id)
        self.assertEqual(overview["pagination"]["errors"]["total"], 1)
        self.assertFalse(overview["pagination"]["errors"]["has_more"])
        unrelated_error = ErrorEntry("d" * 32, user.user_id, "2" * 32, "另一题", "作答", "错因", "open", now)
        self.domain_store.errors[unrelated_error.error_id] = unrelated_error
        mismatched_context = self.call(
            "/v1/internal/harness/context", method="POST",
            payload={
                "session_id": "session-context", "error_id": unrelated_error.error_id,
                "review_code": frozen_manifest[1]["code"],
            }, **internal,
        )
        self.assertEqual((mismatched_context[0], mismatched_context[2]["error"]["code"]), (404, "not_found"))
        denied_context = self.call(
            "/v1/internal/harness/context", method="POST",
            payload={"session_id": "session-context-other", "error_id": error.error_id}, **internal,
        )
        self.assertEqual((denied_context[0], denied_context[2]["error"]["code"]), (404, "not_found"))

        reflowed = self.call(
            f"/v1/internal/harness/practice-pdfs/{paper.job_id}/reflow", method="POST",
            payload={"session_id": "session-context"}, **internal,
        )
        self.assertEqual(reflowed[0], 200)
        self.assertEqual(reflowed[2]["result"]["task_id"], paper.job_id)
        after = self.domain_store.get_job(user_id=user.user_id, job_id=paper.job_id)
        assert after is not None and after.checkpoint is not None
        self.assertNotEqual(after.checkpoint["file_id"], old_file_id)
        self.assertIn(old_file_id, self.domain_store.files)
        self.assertEqual(after.checkpoint["generated_at"], generated_at)
        self.assertEqual(after.checkpoint["review_manifest"], frozen_manifest)
        self.assertEqual(after.checkpoint["review_submissions"], frozen_submissions)
        self.assertEqual(after.checkpoint["review_receipts"], frozen_receipts)
        self.assertTrue(after.checkpoint["print_items"])
        self.assertTrue(all("image_object_key" not in item for item in after.checkpoint["print_items"]))
        self.assertEqual(self.domain_store.learning_usage(user_id=user.user_id), usage_before)
        denied_reflow = self.call(
            f"/v1/internal/harness/practice-pdfs/{paper.job_id}/reflow", method="POST",
            payload={"session_id": "session-context-other"}, **internal,
        )
        self.assertEqual((denied_reflow[0], denied_reflow[2]["error"]["code"]), (404, "not_found"))

    def test_harness_context_sections_are_bounded_pageable_and_fail_closed(self) -> None:
        cookie = self.login("13800138014")
        other_cookie = self.login("13800138015")
        bound = self.call(
            "/v1/harness/sessions/bind", method="POST", payload={"session_id": "session-paged-context"},
            cookie=cookie, origin="https://example.test",
        )
        self.assertEqual(bound[0], 200)
        user = self.auth_service.authenticate_session(cookie.split("=", 1)[1])
        other_user = self.auth_service.authenticate_session(other_cookie.split("=", 1)[1])
        assert user is not None and other_user is not None
        now = datetime.now(timezone.utc)
        error_ids = []
        for index in range(25):
            error_id = f"{index + 1:032x}"
            error_ids.append(error_id)
            self.domain_store.errors[error_id] = ErrorEntry(
                error_id, user.user_id, f"{index + 101:032x}", f"题目 {index + 1}", "作答", "错因", "open",
                now - timedelta(minutes=index),
            )
        self.domain_store.errors["f" * 32] = ErrorEntry(
            "f" * 32, other_user.user_id, "e" * 32, "其他账号题目", "作答", "错因", "open", now,
        )
        internal = {
            "origin": None,
            "client": ("127.0.0.1", 3080),
            "extra_headers": {"authorization": "Bearer test-internal-token"},
        }
        second_page = self.call(
            "/v1/internal/harness/context", method="POST",
            payload={
                "session_id": "session-paged-context", "scope": "errors", "page": 2, "page_size": 10,
            }, **internal,
        )
        self.assertEqual(second_page[0], 200)
        context = json.loads(second_page[2]["context_json"])
        self.assertEqual(set(context), {"scope", "query", "errors", "pagination"})
        self.assertEqual(context["query"], {"mode": "section", "section": "errors"})
        self.assertEqual(context["pagination"], {"page": 2, "page_size": 10, "total": 25, "has_more": True})
        self.assertEqual([item["error_id"] for item in context["errors"]], error_ids[10:20])

        overview_page = self.call(
            "/v1/internal/harness/context", method="POST",
            payload={"session_id": "session-paged-context"}, **internal,
        )
        self.assertEqual(overview_page[0], 200)
        overview_context = json.loads(overview_page[2]["context_json"])
        self.assertEqual(overview_context["pagination"]["errors"], {
            "page": 1, "page_size": 20, "total": 25, "has_more": True,
        })

        exact = self.call(
            "/v1/internal/harness/context", method="POST",
            payload={"session_id": "session-paged-context", "error_id": error_ids[-1]}, **internal,
        )
        self.assertEqual(exact[0], 200)
        exact_context = json.loads(exact[2]["context_json"])
        self.assertEqual(exact_context["error"]["error_id"], error_ids[-1])
        self.assertNotIn("errors", exact_context)

        pending_items = [{"candidate_id": f"{index + 201:032x}", "options": []} for index in range(25)]
        original_pending = self.notebook.list_pending_practice_review_links
        self.notebook.list_pending_practice_review_links = lambda *, user_id: pending_items  # type: ignore[method-assign]
        try:
            pending_page = self.call(
                "/v1/internal/harness/context", method="POST",
                payload={
                    "session_id": "session-paged-context", "scope": "pending_review_links",
                    "page": 2, "page_size": 10,
                }, **internal,
            )
        finally:
            self.notebook.list_pending_practice_review_links = original_pending  # type: ignore[method-assign]
        self.assertEqual(pending_page[0], 200)
        pending_context = json.loads(pending_page[2]["context_json"])
        self.assertEqual(set(pending_context), {"scope", "query", "pending_review_links", "pagination"})
        self.assertEqual(pending_context["pagination"], {"page": 2, "page_size": 10, "total": 25, "has_more": True})
        self.assertEqual(pending_context["pending_review_links"], pending_items[10:20])
        self.assertEqual(len(pending_items), 25)

        invalid_payloads = (
            {"session_id": "session-paged-context", "scope": "unknown"},
            {"session_id": "session-paged-context", "scope": "exact"},
            {"session_id": "session-paged-context", "error_id": error_ids[0], "page": 1},
            {"session_id": "session-paged-context", "scope": "overview", "page_size": 10},
            {"session_id": "session-paged-context", "scope": "errors", "page_size": 21},
            {"session_id": "session-paged-context", "scope": "errors", "page": True},
            {"session_id": "session-paged-context", "error_id": "A" * 32},
            {"session_id": "session-paged-context", "scope": "errors", "unexpected": True},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.call(
                    "/v1/internal/harness/context", method="POST", payload=payload, **internal,
                )
                self.assertEqual((response[0], response[2]["error"]["code"]), (400, "invalid_request"))

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
        listed_error = self.call("/v1/errors", cookie=cookie)[2]["items"][0]
        self.assertEqual((listed_error["error_id"], listed_error["review"]["stage"]), (error_id, 1))
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
        progress = self.call("/v1/progress", cookie=cookie)
        self.assertEqual(progress[2]["review_stage_counts"]["2"], 1)
        self.assertEqual((progress[2]["today_completed_review_count"], progress[2]["today_needs_correction_count"]), (1, 0))
        created_month = datetime.fromisoformat(committed[2]["created_at"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m")
        calendar = self.call(f"/v1/progress/calendar?month={created_month}", cookie=cookie)
        self.assertEqual((calendar[0], calendar[2]["total_error_count"]), (200, 1))
        self.assertEqual(calendar[2]["summary"]["new_error_count"], 1)
        self.assertEqual(calendar[2]["summary"]["completed_review_count"], 1)
        self.assertTrue(any("一元一次方程" in item["knowledge_points"] for day in calendar[2]["days"] for item in day["items"]))
        self.assertEqual(self.call("/v1/progress/calendar?month=2026-13", cookie=cookie)[0], 400)
        practice = self.call("/v1/practice-pdfs", method="POST", payload={"error_ids": [error_id]}, cookie=cookie, idempotency_key="practice-0001")
        self.assertEqual(practice[0], 201)
        practice_history = self.call("/v1/practice-pdfs", cookie=cookie)
        self.assertEqual((practice_history[0], practice_history[2]["count"]), (200, 1))
        self.assertEqual(practice_history[2]["items"][0]["download_url"], practice[2]["download_url"])
        self.assertEqual(practice_history[2]["items"][0]["plan_kind"], "daily_review")
        self.assertEqual(practice_history[2]["today_plan"]["task_id"], practice[2]["task_id"])
        self.assertEqual(practice_history[2]["today_plan"]["items"][0]["status"], "pending")
        extra = self.call("/v1/practice-pdfs", method="POST", payload={"error_ids": [error_id], "plan_kind": "practice"}, cookie=cookie, idempotency_key="practice-extra")
        self.assertEqual(extra[0], 201)
        history_with_extra = self.call("/v1/practice-pdfs", cookie=cookie)[2]
        self.assertEqual(history_with_extra["count"], 2)
        self.assertEqual(history_with_extra["today_plan"]["task_id"], practice[2]["task_id"])
        downloaded = self.call(practice[2]["download_url"], cookie=cookie)
        self.assertEqual(downloaded[0], 200)
        self.assertTrue(downloaded[2].startswith(b"%PDF-"))

        other_cookie = self.login("13900139000")
        self.assertEqual(self.call("/v1/practice-pdfs", cookie=other_cookie)[2]["count"], 0)
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

            def grade(_, *, attempt, image_path, thread_id=None, reference=None):
                resumed_threads.append(thread_id)
                self.assertTrue(image_path.is_file())
                self.assertEqual(image_path.parent.name, "model-previews")
                validation = cross_validate_reference(reference, "x=1") if reference else None
                return {"attempt_id": attempt.attempt_id, "input_version": attempt.input_version, "verdict": "incorrect", "first_error": "移项后结果错误", "cause_code": "algebra_transform", "cause_evidence": "由 x+1=2 得到 x=0", "knowledge_points": ["一元一次方程", "等式性质与移项"], "correct_solution": "x=2-1=1", "final_answer": "x=1", "prevention_cue": "移项后验算", "cross_validation": validation, "confidence": 0.97, "thread_id": "thread-e2e", "route": {"task": "math-grade-adjudication", "model": "test"}}

        self.app.model_runner = FakeModel()
        reference_id = "9" * 32
        self.domain_store.add_question(Question(
            reference_id, "若 x+1=2，求 x。", "x=1", 7, 1.0, "已验证题库",
            solution_text="x=2-1=1", version_id="8" * 32,
        ))
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
        self.assertEqual(graded[2]["diagnosis"]["cross_validation"]["status"], "consistent")
        self.assertEqual([item["item_no"] for item in self.call("/v1/intakes", cookie=cookie)[2]["items"]], [2])
        self.assertEqual(resumed_threads, [None, "thread-e2e", "thread-e2e"])
        self.assertEqual(self.call("/v1/errors", cookie=cookie)[2]["items"], [])
        committed = self.call(f"/v1/grade-results/{graded[2]['result_id']}/commit", method="POST", payload={"input_version": 1}, cookie=cookie, idempotency_key="model-commit")
        self.assertEqual((committed[0], committed[2]["question_text"]), (201, "若 x+1=2，求 x。"))
        self.assertEqual(committed[2]["question_id"], reference_id)
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
