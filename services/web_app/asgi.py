"""Small ASGI composition for auth and the first-error notebook slice."""

from __future__ import annotations

import asyncio
import base64
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import secrets
import string
from threading import Event, Lock
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

from services.web_auth import AuthAsgiApp, RegistrationService
from services.web_domain import ErrorEntry, GradeCandidate, IntakeItem, Job, NotebookService, Recommendation, ReviewTask
from .codex_model import ModelUnavailableError


Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class NotebookAsgiApp:
    def __init__(
        self,
        auth_service: RegistrationService,
        notebook: NotebookService,
        *,
        allowed_hosts: set[str],
        require_https: bool = True,
        session_cookie: str = "__Host-lzlm_session",
        max_upload_bytes: int = 26 * 1024 * 1024,
        model_runner: Any | None = None,
    ) -> None:
        self.auth = AuthAsgiApp(auth_service, allowed_hosts=allowed_hosts, require_https=require_https, session_cookie=session_cookie)
        self.auth_service = auth_service
        self.notebook = notebook
        self.allowed_hosts = {item.lower() for item in allowed_hosts}
        self.require_https = require_https
        self.session_cookie = session_cookie
        self.max_upload_bytes = max_upload_bytes
        self.model_runner = model_runner
        self._turn_cancellations: dict[tuple[str, str], Event] = {}
        self._turn_cancellations_lock = Lock()
        root = Path(__file__).resolve().parents[2]
        self.static_files = {
            "/": (root / "web" / "index.html", "text/html; charset=utf-8", False),
            "/login": (root / "web" / "login.html", "text/html; charset=utf-8", False),
            "/register": (root / "web" / "register.html", "text/html; charset=utf-8", False),
            "/legal/terms": (root / "web" / "terms.html", "text/html; charset=utf-8", False),
            "/legal/privacy": (root / "web" / "privacy.html", "text/html; charset=utf-8", False),
            "/errors": (root / "web" / "errors.html", "text/html; charset=utf-8", False),
            "/reviews": (root / "web" / "reviews.html", "text/html; charset=utf-8", False),
            "/practice": (root / "web" / "practice.html", "text/html; charset=utf-8", False),
            "/progress": (root / "web" / "progress.html", "text/html; charset=utf-8", False),
            "/settings": (root / "web" / "settings.html", "text/html; charset=utf-8", False),
            "/web/app.css": (root / "web" / "app.css", "text/css; charset=utf-8", False),
            "/web/app.js": (root / "web" / "app.js", "text/javascript; charset=utf-8", False),
            "/web/auth.js": (root / "web" / "auth.js", "text/javascript; charset=utf-8", False),
            "/web/nav-icons.svg": (root / "web" / "nav-icons.svg", "image/svg+xml", False),
            "/web/vendor/katex/katex.min.js": (root / "web" / "vendor" / "katex" / "katex.min.js", "text/javascript; charset=utf-8", False),
            "/web/vendor/katex/auto-render.min.js": (root / "web" / "vendor" / "katex" / "auto-render.min.js", "text/javascript; charset=utf-8", False),
            "/assets/branding/favicon-v1.ico": (root / "assets" / "branding" / "favicon-v1.ico", "image/x-icon", True),
            "/assets/branding/logo-symbol-color-64-v1.png": (root / "assets" / "branding" / "logo-symbol-color-64-v1.png", "image/png", True),
            "/assets/branding/logo-symbol-color-128-v1.png": (root / "assets" / "branding" / "logo-symbol-color-128-v1.png", "image/png", True),
        }

    def resume_pending_deletions(self) -> int:
        """Complete only accounts whose authoritative auth state is disabled."""
        resumed = 0
        for user_id in self.notebook.pending_deletion_user_ids():
            try:
                if not self.auth_service.deactivate_account(user_id):
                    self.notebook.record_deletion_error(user_id=user_id, code="auth_deactivation_failed")
                    continue
            except Exception:
                self.notebook.record_deletion_error(user_id=user_id, code="auth_deactivation_failed")
                continue
            try:
                self.notebook.complete_user_deletion(user_id=user_id)
                resumed += 1
            except Exception:
                pass  # The domain service preserves its specific retryable error code.
        return resumed

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if path == "/healthz" or path.startswith("/v1/auth/") or path in {"/v1/session", "/v1/sessions"}:
            await self.auth(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        host = headers.get("host", "").split(":", 1)[0].lower()
        if host not in self.allowed_hosts:
            await self._error(send, 400, "invalid_host")
            return
        if self.require_https and scope.get("scheme") != "https":
            await self._error(send, 400, "https_required")
            return
        method = str(scope.get("method", ""))
        static = self.static_files.get(path)
        if method == "GET" and static:
            await self._asset(send, *static)
            return
        token = self._cookie(headers.get("cookie", ""), self.session_cookie)
        user = await asyncio.to_thread(self.auth_service.authenticate_session, token or "")
        if user is None:
            await self._error(send, 401, "authentication_required")
            return
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            expected_origin = f"{scope.get('scheme', 'https')}://{headers.get('host', '')}"
            if headers.get("origin") != expected_origin:
                await self._error(send, 403, "forbidden")
                return
        try:
            if path == "/v1/intakes" and method == "GET":
                intakes = await self._sync(self.notebook.store.list_pending_intakes, user_id=user.user_id)
                await self._json(send, 200, {"items": [self._intake(item) for item in intakes]})
            elif path == "/v1/conversations/latest/messages" and method == "GET":
                if self.model_runner is None:
                    raise ModelUnavailableError("local model processing is disabled")
                query = self._query(scope)
                cursor_token = query.get("cursor")
                history = {"items": [], "next_cursor": None}
                if cursor_token:
                    conversation_id, upstream_cursor = self._decode_history_cursor(cursor_token)
                    thread_id = await self._sync(
                        self.notebook.store.get_codex_thread,
                        user_id=user.user_id, conversation_id=conversation_id,
                    )
                    if not thread_id:
                        raise LookupError
                    page = await self._sync(
                        self.model_runner.history, thread_id=thread_id, cursor=upstream_cursor, limit=20,
                    )
                    history = {
                        "items": page["items"],
                        "next_cursor": self._encode_history_cursor(conversation_id, page.get("next_cursor")),
                    }
                else:
                    mappings = await self._sync(self.notebook.store.list_recent_codex_threads, user_id=user.user_id, limit=20)
                    for conversation_id, thread_id in mappings:
                        page = await self._sync(self.model_runner.history, thread_id=thread_id, cursor=None, limit=20)
                        history = {
                            "items": page["items"],
                            "next_cursor": self._encode_history_cursor(conversation_id, page.get("next_cursor")),
                        }
                        if page["items"] or page.get("next_cursor"):
                            break
                await self._json(send, 200, history)
            elif path == "/v1/workbench" and method == "GET":
                items = await self._sync(self.notebook.store.list_errors, user_id=user.user_id)
                pending = await self._sync(self.notebook.store.pending_job_count, user_id=user.user_id)
                progress = await self._sync(self.notebook.store.progress, user_id=user.user_id)
                await self._json(send, 200, {"error_count": len(items), "pending_task_count": pending, "due_review_count": progress["due_review_count"], "recommendation_gap_count": progress["recommendation_gap_count"], "recent_errors": [self._error_entry(item) for item in items[:5]]})
            elif path == "/v1/exports" and method == "POST":
                key = self._key(headers)
                payload = await self._json_body(receive)
                verified = await self._verify_sensitive(scope, headers, token or "", payload, "export")
                if verified.user_id != user.user_id:
                    raise SensitiveVerificationError
                job = await self._sync(self.notebook.create_export, user_id=user.user_id, idempotency_key=key)
                await self._json(send, 201, self._export_created(job))
            elif path.startswith("/v1/exports/") and path.endswith("/download") and method == "GET":
                job_id = path.split("/")[-2]
                filename, content = await self._sync(self.notebook.download_export, user_id=user.user_id, job_id=job_id)
                await self._bytes(send, 200, content, "application/json", filename)
            elif path.startswith("/v1/exports/") and method == "GET":
                job = await self._sync(self.notebook.store.get_job, user_id=user.user_id, job_id=path.rsplit("/", 1)[1])
                if not job or job.job_type != "export":
                    raise LookupError
                await self._json(send, 200, self._export_job(job))
            elif path == "/v1/account" and method == "DELETE":
                payload = await self._json_body(receive)
                if set(payload) != {"phone", "challenge_token", "code", "confirmation"} or payload["confirmation"] != "DELETE":
                    raise ValueError("invalid deletion confirmation")
                verified = await self._verify_sensitive(scope, headers, token or "", payload, "delete")
                if verified.user_id != user.user_id:
                    raise SensitiveVerificationError
                try:
                    await self._sync(self.notebook.prepare_user_deletion, user_id=user.user_id)
                except Exception:
                    await self._error(send, 503, "failed_retryable")
                    return
                try:
                    deactivated = await self._sync(self.auth_service.deactivate_account, user.user_id)
                except Exception:
                    deactivated = False
                if not deactivated:
                    try:
                        await self._sync(self.notebook.record_deletion_error, user_id=user.user_id, code="auth_deactivation_failed")
                    except Exception:
                        pass
                    await self._error(send, 503, "failed_retryable")
                    return
                try:
                    await self._sync(self.notebook.complete_user_deletion, user_id=user.user_id)
                except Exception:
                    await self._error(send, 503, "failed_retryable")
                    return
                expired = f"{self.session_cookie}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax".encode("ascii")
                await send({"type": "http.response.start", "status": 204, "headers": [(b"set-cookie", expired), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b""})
            elif path == "/v1/files" and method == "POST":
                key = self._key(headers)
                purpose, filename, content = await self._multipart(receive, headers)
                record = await self._sync(self.notebook.upload, user_id=user.user_id, purpose=purpose, original_name=filename, content=content, idempotency_key=key)
                await self._json(send, 201, {"file_id": record.file_id, "status": record.status, "content_sha256": record.content_sha256})
            elif path == "/v1/intakes" and method == "POST":
                payload = await self._json_body(receive)
                intake, job = await self._sync(self.notebook.store.create_intake, user_id=user.user_id, file_id=str(payload["file_id"]), idempotency_key=self._key(headers))
                await self._json(send, 202, {"resource_id": intake.intake_id, "task_id": job.job_id})
            elif path.startswith("/v1/tasks/") and method == "GET":
                job = await self._sync(self.notebook.store.get_job, user_id=user.user_id, job_id=path.rsplit("/", 1)[1])
                if not job:
                    raise LookupError
                await self._json(send, 200, self._job(job))
            elif path.startswith("/v1/intakes/") and path.endswith("/manual-candidate") and method == "POST":
                intake_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                intake = await self._sync(
                    self.notebook.store.save_extraction_candidate,
                    user_id=user.user_id,
                    intake_id=intake_id,
                    question_text=str(payload["question_text"]),
                    answer_text=str(payload.get("answer_text", "")),
                    evidence={"source": "user_manual"},
                )
                await self._json(send, 201, self._intake(intake))
            elif path.startswith("/v1/intakes/") and path.endswith("/model-candidate") and method == "POST":
                if self.model_runner is None:
                    raise ModelUnavailableError("local model processing is disabled")
                intake_id = path.split("/")[-2]
                raw_payload = await self._read(receive)
                payload = json.loads(raw_payload.decode("utf-8")) if raw_payload else {}
                if not isinstance(payload, dict) or set(payload) - {"refresh"} or not isinstance(payload.get("refresh", False), bool):
                    raise ValueError("invalid model candidate request")
                refresh = payload.get("refresh", False)
                intake = await self._sync(self.notebook.store.get_intake, user_id=user.user_id, intake_id=intake_id)
                if not intake:
                    raise LookupError
                if intake.status == "waiting_confirmation" and not refresh:
                    intakes = await self._sync(self.notebook.store.get_file_intakes, user_id=user.user_id, file_id=intake.file_id)
                    payloads = [self._intake(value) for value in intakes]
                    await self._json(send, 200, payloads[0] | {"items": payloads, "model_status": "existing"})
                    return
                if intake.status not in ({"extracting", "waiting_confirmation"} if refresh else {"extracting"}):
                    raise RuntimeError("conflict")
                file_record = await self._sync(self.notebook.store.get_file, user_id=user.user_id, file_id=intake.file_id)
                if not file_record:
                    raise LookupError
                if file_record.media_type not in {"image/png", "image/jpeg"}:
                    raise ModelUnavailableError("automatic extraction currently supports PNG and JPEG")
                thread_id = await self._sync(
                    self.notebook.store.get_codex_thread,
                    user_id=user.user_id,
                    conversation_id=intake_id,
                )
                with self.notebook.files.model_preview(file_record.object_key, file_record.content_sha256) as image_path:
                    result = await self._sync(
                        self.model_runner.extract,
                        intake=intake,
                        file_record=file_record,
                        image_path=image_path,
                        thread_id=thread_id,
                    )
                await self._sync(
                    self.notebook.store.save_codex_thread,
                    user_id=user.user_id,
                    conversation_id=intake_id,
                    thread_id=result["thread_id"],
                )
                if result.get("intake_id") != intake.intake_id or result.get("input_version") != intake.input_version:
                    raise ModelUnavailableError("model response does not match the frozen intake")
                model_items = result.get("items")
                if not isinstance(model_items, list):
                    model_items = [{
                        "item_no": 1,
                        "question_text": str(result.get("question_text", "")).strip(),
                        "answer_text": str(result.get("answer_text", "")).strip(),
                    }]
                readable_items = [
                    item for item in model_items
                    if isinstance(item, dict) and str(item.get("question_text", "")).strip()
                ]
                candidates = [
                    {"item_no": index, "question_text": str(item["question_text"]).strip(), "answer_text": str(item.get("answer_text", "")).strip()}
                    for index, item in enumerate(readable_items, 1)
                ]
                if candidates:
                    intakes = await self._sync(
                        self.notebook.store.save_extraction_candidates,
                        user_id=user.user_id,
                        intake_id=intake_id,
                        items=candidates,
                        evidence={"source": "codex_app_server", "route": result.get("route"), "confidence": result.get("confidence")},
                        replace_existing=refresh,
                    )
                    payloads = [self._intake(value) for value in intakes]
                    await self._json(send, 201, payloads[0] | {
                        "items": payloads, "model_status": result.get("status"), "model": result.get("route"),
                    })
                else:
                    await self._json(send, 200, self._intake(intake) | {"items": [], "model_status": "unclear", "model": result.get("route")})
            elif path.startswith("/v1/intakes/") and path.endswith(("/chat-turn", "/chat-turn-stream")) and method == "POST":
                if self.model_runner is None:
                    raise ModelUnavailableError("local model processing is disabled")
                intake_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                if path.endswith("/chat-turn-stream"):
                    await self._stream_chat_turn(send, user.user_id, intake_id, payload)
                else:
                    result = await self._sync(self._chat_turn_core, user.user_id, intake_id, payload)
                    await self._json(send, 200, result)
            elif path.startswith("/v1/intakes/") and path.endswith("/conversation/stop") and method == "POST":
                intake_id = path.split("/")[-3]
                intake = await self._sync(self.notebook.store.get_intake, user_id=user.user_id, intake_id=intake_id)
                if not intake:
                    raise LookupError
                with self._turn_cancellations_lock:
                    cancellation = self._turn_cancellations.get((user.user_id, intake_id))
                    if cancellation is not None:
                        cancellation.set()
                await self._json(send, 200, {"status": "interrupt_requested" if cancellation else "idle"})
            elif path.startswith("/v1/intakes/") and path.endswith("/conversation/compact") and method == "POST":
                if self.model_runner is None:
                    raise ModelUnavailableError("local model processing is disabled")
                intake_id = path.split("/")[-3]
                intake = await self._sync(self.notebook.store.get_intake, user_id=user.user_id, intake_id=intake_id)
                if not intake:
                    raise LookupError
                with self._turn_cancellations_lock:
                    if (user.user_id, intake_id) in self._turn_cancellations:
                        raise RuntimeError("conflict")
                thread_id = await self._sync(
                    self.notebook.store.get_codex_thread, user_id=user.user_id, conversation_id=intake_id,
                )
                if not thread_id:
                    raise LookupError
                result = await self._sync(self.model_runner.compact, thread_id=thread_id)
                await self._json(send, 200, result)
            elif path.startswith("/v1/intakes/") and path.endswith("/confirm") and method == "POST":
                intake_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                attempt_id, job = await self._sync(self.notebook.store.confirm_intake, user_id=user.user_id, intake_id=intake_id, expected_version=int(payload["input_version"]), idempotency_key=self._key(headers))
                await self._json(send, 202, {"resource_id": attempt_id, "task_id": job.job_id})
            elif path.startswith("/v1/intakes/") and method == "PATCH":
                intake_id = path.rsplit("/", 1)[1]
                payload = await self._json_body(receive)
                intake = await self._sync(self.notebook.store.revise_intake, user_id=user.user_id, intake_id=intake_id, expected_version=int(payload["input_version"]), question_text=str(payload["question_text"]), answer_text=str(payload["answer_text"]))
                await self._json(send, 200, self._intake(intake))
            elif path.startswith("/v1/attempts/") and path.endswith("/manual-grade") and method == "POST":
                attempt_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                verdict, first_error, evidence = self._grade_values(payload, evidence_key="evidence")
                candidate = await self._sync(
                    self.notebook.store.record_grade_candidate,
                    user_id=user.user_id,
                    attempt_id=attempt_id,
                    input_version=int(payload["input_version"]),
                    verdict=verdict,
                    first_error=first_error,
                    evidence=evidence,
                )
                await self._json(send, 201, self._candidate(candidate))
            elif path.startswith("/v1/attempts/") and path.endswith("/model-grade") and method == "POST":
                if self.model_runner is None:
                    raise ModelUnavailableError("local model processing is disabled")
                attempt_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                if set(payload) != {"input_version"}:
                    raise ValueError("invalid model grade request")
                attempt = await self._sync(self.notebook.store.get_attempt, user_id=user.user_id, attempt_id=attempt_id)
                if not attempt:
                    raise LookupError
                if int(payload["input_version"]) != attempt.input_version:
                    raise RuntimeError("input_version_changed")
                thread_id = await self._sync(
                    self.notebook.store.get_codex_thread,
                    user_id=user.user_id,
                    conversation_id=attempt.intake_id,
                )
                result = await self._sync(self.model_runner.grade, attempt=attempt, thread_id=thread_id)
                await self._sync(
                    self.notebook.store.save_codex_thread,
                    user_id=user.user_id,
                    conversation_id=attempt.intake_id,
                    thread_id=result["thread_id"],
                )
                if result.get("attempt_id") != attempt.attempt_id or result.get("input_version") != attempt.input_version:
                    raise ModelUnavailableError("model response does not match the frozen attempt")
                verdict, first_error, evidence = self._grade_values(result, evidence_key="cause_evidence")
                confidence = result.get("confidence")
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
                    raise ModelUnavailableError("model returned invalid confidence")
                candidate = await self._sync(
                    self.notebook.store.record_grade_candidate,
                    user_id=user.user_id,
                    attempt_id=attempt_id,
                    input_version=attempt.input_version,
                    verdict=verdict,
                    first_error=first_error,
                    evidence=evidence,
                    confidence=float(confidence),
                )
                await self._json(send, 201, self._candidate(candidate) | {"model": result.get("route")})
            elif path.startswith("/v1/grade-results/") and path.endswith("/commit") and method == "POST":
                candidate_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                entry = await self._sync(self.notebook.store.commit_grade, user_id=user.user_id, candidate_id=candidate_id, expected_version=int(payload["input_version"]))
                await self._json(send, 201, self._error_entry(entry))
            elif path.startswith("/v1/grade-results/") and method == "GET":
                candidate = await self._sync(self.notebook.store.get_grade_candidate, user_id=user.user_id, candidate_id=path.rsplit("/", 1)[1])
                if not candidate:
                    raise LookupError
                await self._json(send, 200, self._candidate(candidate))
            elif path == "/v1/errors" and method == "GET":
                items = await self._sync(self.notebook.store.list_errors, user_id=user.user_id)
                await self._json(send, 200, {"items": [self._error_entry(item) for item in items]})
            elif path.startswith("/v1/errors/") and path.endswith("/master") and method == "POST":
                entry = await self._sync(self.notebook.store.set_error_status, user_id=user.user_id, error_id=path.split("/")[-2], status="mastered")
                await self._json(send, 200, self._error_entry(entry))
            elif path.startswith("/v1/errors/") and path.endswith("/recommendations") and method in {"GET", "POST"}:
                error_id = path.split("/")[-2]
                if method == "POST":
                    self._key(headers)
                    recommendations, gap = await self._sync(self.notebook.store.assign_recommendations, user_id=user.user_id, error_id=error_id)
                else:
                    recommendations = await self._sync(self.notebook.store.list_recommendations, user_id=user.user_id, error_id=error_id)
                    gap = len(recommendations) < 2
                await self._json(send, 200, {"items": [self._recommendation(item) for item in recommendations], "gap": gap})
            elif path.startswith("/v1/errors/") and method == "DELETE":
                entry = await self._sync(self.notebook.store.set_error_status, user_id=user.user_id, error_id=path.rsplit("/", 1)[1], status="removed")
                await self._json(send, 200, self._error_entry(entry))
            elif path.startswith("/v1/errors/") and method == "GET":
                entry = await self._sync(self.notebook.store.get_error, user_id=user.user_id, error_id=path.rsplit("/", 1)[1])
                if not entry:
                    raise LookupError
                await self._json(send, 200, self._error_entry(entry))
            elif path == "/v1/reviews/today" and method == "GET":
                tasks = await self._sync(self.notebook.store.list_due_reviews, user_id=user.user_id)
                items = []
                for task in tasks:
                    entry = await self._sync(self.notebook.store.get_error, user_id=user.user_id, error_id=task.error_id)
                    recommendations = await self._sync(self.notebook.store.list_recommendations, user_id=user.user_id, error_id=task.error_id)
                    items.append(self._review(task) | {"question_text": entry.question_text if entry else "", "first_error": entry.first_error if entry else None, "recommendations": [self._recommendation(item) for item in recommendations[:2]]})
                await self._json(send, 200, {"items": items, "count": len(items)})
            elif path.startswith("/v1/reviews/") and path.endswith("/complete") and method == "POST":
                payload = await self._json_body(receive)
                next_task = await self._sync(self.notebook.store.complete_review, user_id=user.user_id, task_id=path.split("/")[-2], result=str(payload["result"]), idempotency_key=self._key(headers))
                await self._json(send, 200, {"completed": True, "next_review": self._review(next_task) if next_task else None, "mastered": next_task is None})
            elif path == "/v1/progress" and method == "GET":
                progress = await self._sync(self.notebook.store.progress, user_id=user.user_id)
                await self._json(send, 200, progress)
            elif path == "/v1/bank/status" and method == "GET":
                await self._json(send, 200, await self._sync(self.notebook.store.bank_status))
            elif path == "/v1/practice-pdfs" and method == "POST":
                payload = await self._json_body(receive)
                error_ids = payload.get("error_ids")
                include_answers = payload.get("include_answers", False)
                if not isinstance(error_ids, list) or not all(isinstance(item, str) for item in error_ids) or not 1 <= len(error_ids) <= 12 or not isinstance(include_answers, bool):
                    raise ValueError("invalid practice request")
                job = await self._sync(self.notebook.create_practice_pdf, user_id=user.user_id, error_ids=list(dict.fromkeys(error_ids)), idempotency_key=self._key(headers), include_answers=include_answers)
                await self._json(send, 201, self._practice_job(job))
            elif path.startswith("/v1/practice-pdfs/") and path.endswith("/download") and method == "GET":
                job_id = path.split("/")[-2]
                filename, content = await self._sync(self.notebook.download_practice_pdf, user_id=user.user_id, job_id=job_id)
                await self._bytes(send, 200, content, "application/pdf", filename)
            elif path.startswith("/v1/practice-pdfs/") and method == "GET":
                job = await self._sync(self.notebook.store.get_job, user_id=user.user_id, job_id=path.rsplit("/", 1)[1])
                if not job or job.job_type != "practice_pdf":
                    raise LookupError
                await self._json(send, 200, self._practice_job(job))
            else:
                await self._error(send, 404, "not_found")
        except RequestTooLarge:
            await self._error(send, 413, "request_too_large")
        except LookupError:
            await self._error(send, 404, "not_found")
        except SensitiveVerificationError:
            await self._error(send, 403, "sensitive_verification_failed")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except ModelUnavailableError as exc:
            await self._error(send, 503, exc.code)
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {"input_version_changed", "waiting_confirmation", "failed_final", "conflict"} else "conflict"
            await self._error(send, 422 if code == "failed_final" else 409, code)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    def _chat_turn_core(
        self,
        user_id: str,
        intake_id: str,
        payload: dict[str, Any],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        if set(payload) != {"message", "stage", "input_version", "attempt_id", "candidate_id"}:
            raise ValueError("invalid chat turn request")
        message, stage = payload["message"], payload["stage"]
        if not isinstance(message, str) or not message.strip() or len(message) > 12_000 or stage not in {"intake", "grade"}:
            raise ValueError("invalid chat turn request")
        intake = self.notebook.store.get_intake(user_id=user_id, intake_id=intake_id)
        if not intake:
            raise LookupError
        if int(payload["input_version"]) != intake.input_version:
            raise RuntimeError("input_version_changed")
        thread_id = self.notebook.store.get_codex_thread(user_id=user_id, conversation_id=intake_id)
        if stage == "intake":
            if payload["attempt_id"] is not None or payload["candidate_id"] is not None:
                raise ValueError("invalid intake chat context")
            result = self.model_runner.chat_turn(
                conversation_id=intake_id, stage="intake", resource_id=intake_id,
                input_version=intake.input_version, user_message=message,
                context={"question_text": intake.question_text, "answer_text": intake.answer_text, "status": intake.status},
                thread_id=thread_id, event_callback=event_callback, cancel_event=cancel_event,
            )
            self.notebook.store.save_codex_thread(user_id=user_id, conversation_id=intake_id, thread_id=result["thread_id"])
            if result.get("action") == "revise_intake":
                if intake.status == "extracting":
                    intake = self.notebook.store.save_extraction_candidate(
                        user_id=user_id, intake_id=intake_id,
                        question_text=str(result.get("question_text") or ""),
                        answer_text=str(result.get("answer_text") or ""),
                        evidence={"source": "codex_app_server", "route": result.get("route"), "confidence": result.get("confidence")},
                    )
                else:
                    intake = self.notebook.store.revise_intake(
                        user_id=user_id, intake_id=intake_id, expected_version=intake.input_version,
                        question_text=str(result.get("question_text") or ""), answer_text=str(result.get("answer_text") or ""),
                    )
            return {
                "assistant_message": result["assistant_message"], "action": result["action"],
                "stage": "intake", "intake": self._intake(intake), "model": result.get("route"),
            }
        attempt_id, candidate_id = payload["attempt_id"], payload["candidate_id"]
        if not isinstance(attempt_id, str) or candidate_id is not None and not isinstance(candidate_id, str):
            raise ValueError("invalid grade chat context")
        attempt = self.notebook.store.get_attempt(user_id=user_id, attempt_id=attempt_id)
        if not attempt or attempt.intake_id != intake_id or attempt.input_version != intake.input_version:
            raise LookupError
        candidate = None
        if candidate_id:
            candidate = self.notebook.store.get_grade_candidate(user_id=user_id, candidate_id=candidate_id)
            if not candidate or candidate.attempt_id != attempt_id:
                raise LookupError
        result = self.model_runner.chat_turn(
            conversation_id=intake_id, stage="grade", resource_id=attempt_id,
            input_version=attempt.input_version, user_message=message,
            context={"question_text": attempt.question_text, "answer_text": attempt.answer_text, "candidate": self._candidate(candidate) if candidate else None},
            thread_id=thread_id, event_callback=event_callback, cancel_event=cancel_event,
        )
        self.notebook.store.save_codex_thread(user_id=user_id, conversation_id=intake_id, thread_id=result["thread_id"])
        if result.get("action") == "revise_grade":
            verdict, first_error, evidence = self._grade_values(result, evidence_key="cause_evidence")
            candidate = self.notebook.store.record_grade_candidate(
                user_id=user_id, attempt_id=attempt_id, input_version=attempt.input_version,
                verdict=verdict, first_error=first_error, evidence=evidence, confidence=float(result["confidence"]),
            )
        return {
            "assistant_message": result["assistant_message"], "action": result["action"],
            "stage": "grade", "candidate": self._candidate(candidate) if candidate else None, "model": result.get("route"),
        }

    async def _stream_chat_turn(self, send: Send, user_id: str, intake_id: str, payload: dict[str, Any]) -> None:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        key = (user_id, intake_id)
        cancellation = Event()
        with self._turn_cancellations_lock:
            if key in self._turn_cancellations:
                raise ModelUnavailableError("conversation turn is already running")
            self._turn_cancellations[key] = cancellation

        def notify(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "runtime", "event": event})

        task = asyncio.create_task(asyncio.to_thread(self._chat_turn_core, user_id, intake_id, payload, notify, cancellation))

        def release(_: asyncio.Task[Any]) -> None:
            with self._turn_cancellations_lock:
                if self._turn_cancellations.get(key) is cancellation:
                    self._turn_cancellations.pop(key, None)

        task.add_done_callback(release)
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/x-ndjson; charset=utf-8"),
            (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
        ]})
        await self._ndjson(send, {"type": "runtime", "event": {"type": "request_started"}}, more=True)
        while not task.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            await self._ndjson(send, event, more=True)
        try:
            result = task.result()
        except Exception as exc:
            code = getattr(exc, "code", None) or (str(exc) if str(exc) in {"input_version_changed"} else "model_unavailable")
            await self._ndjson(send, {"type": "error", "error": {"code": code}}, more=False)
            return
        await self._ndjson(send, {"type": "result", "data": result}, more=False)

    @staticmethod
    async def _ndjson(send: Send, payload: dict[str, Any], *, more: bool) -> None:
        body = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        await send({"type": "http.response.body", "body": body, "more_body": more})

    async def _read(self, receive: Receive) -> bytes:
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                raise ValueError("invalid ASGI message")
            body.extend(message.get("body", b""))
            if len(body) > self.max_upload_bytes:
                raise RequestTooLarge
            if not message.get("more_body", False):
                return bytes(body)

    async def _json_body(self, receive: Receive) -> dict[str, Any]:
        value = json.loads((await self._read(receive)).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value

    @staticmethod
    def _query(scope: dict[str, Any]) -> dict[str, str]:
        raw = scope.get("query_string", b"")
        if not isinstance(raw, bytes) or len(raw) > 4096:
            raise ValueError("invalid query")
        values = parse_qs(raw.decode("ascii"), keep_blank_values=True, max_num_fields=4)
        if set(values) - {"cursor"} or any(len(items) != 1 for items in values.values()):
            raise ValueError("invalid query")
        return {key: items[0] for key, items in values.items()}

    @staticmethod
    def _encode_history_cursor(conversation_id: str, upstream_cursor: Any) -> str | None:
        if not isinstance(upstream_cursor, str) or not upstream_cursor:
            return None
        packet = json.dumps(
            {"conversation_id": conversation_id, "cursor": upstream_cursor},
            ensure_ascii=True, separators=(",", ":"),
        ).encode("ascii")
        return base64.urlsafe_b64encode(packet).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_history_cursor(token: str) -> tuple[str, str]:
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ValueError("invalid history cursor")
        try:
            padding = "=" * (-len(token) % 4)
            value = json.loads(base64.b64decode(token + padding, altchars=b"-_", validate=True).decode("ascii"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid history cursor") from exc
        conversation_id = value.get("conversation_id") if isinstance(value, dict) else None
        cursor = value.get("cursor") if isinstance(value, dict) else None
        if (
            not isinstance(conversation_id, str) or len(conversation_id) != 32
            or any(character not in string.hexdigits for character in conversation_id)
            or not isinstance(cursor, str) or not cursor or len(cursor) > 2048
        ):
            raise ValueError("invalid history cursor")
        return conversation_id, cursor

    async def _multipart(self, receive: Receive, headers: dict[str, str]) -> tuple[str, str, bytes]:
        content_type = headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise ValueError("multipart required")
        message = BytesParser(policy=policy.default).parsebytes(f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + await self._read(receive))
        purpose = filename = None
        content = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "purpose":
                purpose = part.get_content().strip()
            elif name == "file":
                filename = part.get_filename()
                content = part.get_payload(decode=True)
        if not purpose or not filename or content is None:
            raise ValueError("missing multipart field")
        if purpose not in {"exam", "answer_photo", "question_image"}:
            raise ValueError("unsupported public upload purpose")
        return purpose, filename, content

    async def _verify_sensitive(self, scope: dict[str, Any], headers: dict[str, str], session_token: str, payload: dict[str, Any], action: str) -> Any:
        if set(payload) - {"phone", "challenge_token", "code", "confirmation"} or not all(isinstance(payload.get(key), str) for key in {"phone", "challenge_token", "code"}):
            raise ValueError("invalid sensitive request")
        client = scope.get("client")
        device_id = headers.get("x-device-id")
        if not client or not client[0] or not device_id:
            raise ValueError("client context required")
        verified = await self._sync(
            self.auth_service.verify_sensitive,
            session_token,
            payload["phone"],
            payload["challenge_token"],
            payload["code"],
            action,
            ip_address=str(client[0]),
            device_id=device_id,
        )
        if verified is None:
            raise SensitiveVerificationError
        return verified

    @staticmethod
    async def _sync(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)

    @staticmethod
    def _cookie(header: str, name: str) -> str | None:
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                return value
        return None

    @staticmethod
    def _key(headers: dict[str, str]) -> str:
        value = headers.get("idempotency-key", "").strip()
        if not 8 <= len(value) <= 64:
            raise ValueError("invalid idempotency key")
        return value

    @staticmethod
    def _job(value: Job) -> dict[str, Any]:
        return {"task_id": value.job_id, "type": value.job_type, "status": value.status, "checkpoint": value.checkpoint, "last_error_code": value.last_error_code}

    @staticmethod
    def _intake(value: IntakeItem) -> dict[str, Any]:
        return {"intake_id": value.intake_id, "item_no": value.item_no, "input_version": value.input_version, "status": value.status, "question_text": value.question_text, "answer_text": value.answer_text}

    @staticmethod
    def _candidate(value: GradeCandidate) -> dict[str, Any]:
        return {"result_id": value.candidate_id, "input_version": value.input_version, "verdict": value.verdict, "status": value.status, "first_error": value.first_error, "diagnosis": NotebookAsgiApp._diagnosis(value.evidence)}

    @staticmethod
    def _error_entry(value: ErrorEntry) -> dict[str, Any]:
        return {"error_id": value.error_id, "status": value.status, "question_text": value.question_text, "answer_text": value.answer_text, "first_error": value.first_error, "diagnosis": NotebookAsgiApp._diagnosis(value.evidence), "created_at": value.created_at.isoformat()}

    @staticmethod
    def _diagnosis(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {"correct_solution": value}
        return payload if isinstance(payload, dict) and payload.get("schema") == "math-error-diagnosis/v1" else {"correct_solution": value}

    @staticmethod
    def _grade_values(payload: dict[str, Any], *, evidence_key: str) -> tuple[str, str | None, str]:
        verdict = str(payload["verdict"])
        first_error = str(payload.get("first_error") or "").strip() or None
        if verdict not in {"correct", "partial", "incorrect", "unclear"}:
            raise ValueError("unsupported verdict")
        if verdict in {"correct", "unclear"}:
            first_error = None
        cause_code = str(payload.get("cause_code") or "").strip()
        cause_evidence = str(payload.get(evidence_key) or "").strip()
        correct_solution = str(payload.get("correct_solution") or "").strip()
        final_answer = str(payload.get("final_answer") or "").strip()
        prevention_cue = str(payload.get("prevention_cue") or "").strip()
        allowed_causes = {"knowledge_gap", "concept_confusion", "formula_condition", "method_choice", "reasoning_gap", "algebra_transform", "calculation", "misreading", "incomplete_cases", "expression", "careless", "unclear"}
        if any(len(value) > 12000 for value in (first_error or "", cause_evidence, correct_solution, final_answer, prevention_cue)):
            raise ValueError("grade field is too long")
        if verdict in {"partial", "incorrect"} and (not first_error or cause_code not in allowed_causes or not cause_evidence or not correct_solution or not final_answer):
            raise ValueError("complete diagnosis is required")
        if cause_code == "careless" and not cause_evidence:
            raise ValueError("careless requires direct evidence")
        evidence = json.dumps({"schema": "math-error-diagnosis/v1", "cause_code": cause_code or None, "cause_evidence": cause_evidence or None, "correct_solution": correct_solution or None, "final_answer": final_answer or None, "prevention_cue": prevention_cue or None}, ensure_ascii=False, separators=(",", ":"))
        return verdict, first_error, evidence

    @staticmethod
    def _recommendation(value: Recommendation) -> dict[str, Any]:
        return {"recommendation_id": value.recommendation_id, "question_id": value.question.question_id, "stem_text": value.question.stem_text, "grade": value.question.grade, "difficulty": value.question.difficulty, "source": value.question.source_title, "reason": value.reason, "status": value.status}

    @staticmethod
    def _review(value: ReviewTask) -> dict[str, Any]:
        return {"review_id": value.task_id, "error_id": value.error_id, "stage": value.stage, "due_at": value.due_at.isoformat(), "status": value.status}

    def _practice_job(self, value: Job) -> dict[str, Any]:
        payload = self._job(value)
        if value.status == "completed":
            payload["download_url"] = f"/v1/practice-pdfs/{value.job_id}/download"
        return payload

    @staticmethod
    def _export_job(value: Job) -> dict[str, Any]:
        if not value.checkpoint or "expires_at" not in value.checkpoint:
            raise LookupError("export not found")
        payload = {"job_id": value.job_id, "status": value.status, "expires_at": value.checkpoint["expires_at"]}
        if value.status == "completed":
            payload["download_url"] = f"/v1/exports/{value.job_id}/download"
        return payload

    @staticmethod
    def _export_created(value: Job) -> dict[str, Any]:
        payload = NotebookAsgiApp._export_job(value)
        if "download_url" not in payload:
            raise RuntimeError("export_not_completed")
        return {key: payload[key] for key in ("job_id", "download_url", "expires_at")}

    async def _error(self, send: Send, status: int, code: str) -> None:
        await self._json(send, status, {"error": {"code": code, "message": code, "retryable": code in {"failed_retryable", "temporarily_unavailable", "model_unavailable", "model_network_error", "model_rate_limited", "rate_limited"}, "request_id": secrets.token_hex(8)}})

    @staticmethod
    async def _json(send: Send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _bytes(send: Send, status: int, body: bytes, media_type: str, filename: str) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", media_type.encode("ascii")), (b"content-length", str(len(body)).encode("ascii")), (b"content-disposition", f'attachment; filename="{filename}"'.encode("ascii")), (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _asset(send: Send, path: Path, media_type: str, immutable: bool) -> None:
        if not path.is_file():
            await NotebookAsgiApp._json(send, 404, {"error": {"code": "not_found", "message": "not_found", "retryable": False, "request_id": secrets.token_hex(8)}})
            return
        body = await asyncio.to_thread(path.read_bytes)
        cache = b"public,max-age=31536000,immutable" if immutable else b"no-cache"
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", media_type.encode("ascii")), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", cache), (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})


class SensitiveVerificationError(RuntimeError):
    pass


class RequestTooLarge(ValueError):
    pass
