"""Small ASGI composition for auth and the first-error notebook slice."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import string
from threading import Event, Lock
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

from services.web_auth import AuthAsgiApp, RegistrationService
from services.web_domain import (
    ErrorEntry,
    GradeCandidate,
    IntakeItem,
    Job,
    NotebookService,
    Recommendation,
    ReviewTask,
    IntakeBatch,
    IntakeBatchEngine,
    cross_validate_reference,
    reference_adjudication_from_evidence,
    reference_conflict_resolved,
)
from .codex_model import ModelUnavailableError
from services.web_domain.practice_review import decode_review_qr_codes, review_locator
from .gateway import LoopbackHarnessGateway
from .intake_pipeline import NotebookIntakeBatchProcessor


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
        harness_internal_token: str | None = None,
        harness_upstream: tuple[str, int] | None = None,
        intake_batch_engine: Any | None = None,
    ) -> None:
        self.auth = AuthAsgiApp(auth_service, allowed_hosts=allowed_hosts, require_https=require_https, session_cookie=session_cookie)
        self.auth_service = auth_service
        self.notebook = notebook
        self.allowed_hosts = {item.lower() for item in allowed_hosts}
        self.require_https = require_https
        self.session_cookie = session_cookie
        self.max_upload_bytes = max_upload_bytes
        self.model_runner = model_runner
        self.harness_internal_token = harness_internal_token
        self.harness_gateway = LoopbackHarnessGateway(*harness_upstream) if harness_upstream is not None else None
        self.intake_batch_engine = intake_batch_engine
        self._harness_sessions: dict[str, str] = {}
        self._harness_sessions_lock = Lock()
        self._harness_processes: set[tuple[str, str]] = set()
        self._harness_processes_lock = Lock()
        self._turn_cancellations: dict[tuple[str, str], Event] = {}
        self._turn_cancellations_lock = Lock()
        self._sse_connections_total = 0
        self._sse_connections_by_user: dict[str, int] = {}
        self._sse_connections_by_batch: dict[tuple[str, str], int] = {}
        self._sse_connections_lock = Lock()
        root = Path(__file__).resolve().parents[2]
        self.static_files = {
            "/": (root / "web" / "index.html", "text/html; charset=utf-8", False),
            "/login": (root / "web" / "login.html", "text/html; charset=utf-8", False),
            "/register": (root / "web" / "register.html", "text/html; charset=utf-8", False),
            "/legal/terms": (root / "web" / "terms.html", "text/html; charset=utf-8", False),
            "/legal/privacy": (root / "web" / "privacy.html", "text/html; charset=utf-8", False),
            "/errors": (root / "web" / "errors.html", "text/html; charset=utf-8", False),
            "/practice": (root / "web" / "practice.html", "text/html; charset=utf-8", False),
            "/progress": (root / "web" / "progress.html", "text/html; charset=utf-8", False),
            "/settings": (root / "web" / "settings.html", "text/html; charset=utf-8", False),
            "/web/app.css": (root / "web" / "app.css", "text/css; charset=utf-8", False),
            "/web/app.js": (root / "web" / "app.js", "text/javascript; charset=utf-8", False),
            "/web/auth.js": (root / "web" / "auth.js", "text/javascript; charset=utf-8", False),
            "/web/nav-icons.svg": (root / "web" / "nav-icons.svg", "image/svg+xml", False),
            "/web/vendor/katex/katex.min.js": (root / "web" / "vendor" / "katex" / "katex.min.js", "text/javascript; charset=utf-8", False),
            "/web/vendor/katex/auto-render.min.js": (root / "web" / "vendor" / "katex" / "auto-render.min.js", "text/javascript; charset=utf-8", False),
            "/manifest.webmanifest": (root / "web" / "manifest.webmanifest", "application/manifest+json", False),
            "/assets/branding/favicon-v1.ico": (root / "assets" / "branding" / "favicon-v1.ico", "image/x-icon", True),
            "/assets/branding/logo-symbol-color-64-v1.png": (root / "assets" / "branding" / "logo-symbol-color-64-v1.png", "image/png", True),
            "/assets/branding/logo-symbol-color-128-v1.png": (root / "assets" / "branding" / "logo-symbol-color-128-v1.png", "image/png", True),
        }
        self.public_static_paths = {
            "/login", "/register", "/legal/terms", "/legal/privacy",
            "/web/app.css", "/web/auth.js",
            "/manifest.webmanifest",
            "/assets/branding/favicon-v1.ico", "/assets/branding/logo-symbol-color-64-v1.png",
            "/assets/branding/logo-symbol-color-128-v1.png",
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

    def resume_pending_batches(self) -> bool:
        """Start the continuous scanner; MySQL decides which pending or expired work is due."""
        if self.model_runner is None and self.intake_batch_engine is None:
            return False
        self._ensure_intake_batch_engine().start()
        return True

    def stop_pending_batches(self) -> None:
        """Stop local scanner threads during an orderly process shutdown."""
        if self.intake_batch_engine is not None:
            self.intake_batch_engine.stop()

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") == "websocket":
            await self._websocket(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        host = headers.get("host", "").split(":", 1)[0].lower()
        if host not in self.allowed_hosts:
            await self._error(send, 400, "invalid_host")
            return
        if self.require_https and scope.get("scheme") != "https":
            await self._error(send, 400, "https_required")
            return
        # Retired admin URLs must not offer a login or expose historical data.
        if path in {"/admin", "/v1/admin", "/web/admin.html", "/web/admin.js"} or path.startswith(("/admin/", "/v1/admin/")):
            await self._error(send, 404, "not_found")
            return
        method = str(scope.get("method", "")).upper()
        safe_methods = {"GET", "HEAD", "OPTIONS"}
        if method not in safe_methods | {"POST", "PATCH", "PUT", "DELETE"}:
            await self._error(send, 405, "method_not_allowed")
            return
        if path == "/v1/internal/harness/intakes/process" and method == "POST":
            await self._internal_harness_process(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/grading-references" and method == "POST":
            await self._internal_harness_grading_references(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/intake-batches" and method == "POST":
            await self._internal_harness_batch_create(scope, receive, send, headers)
            return
        if path.startswith("/v1/internal/harness/intake-batches/") and method == "GET":
            await self._internal_harness_batch_get(scope, send, headers)
            return
        if path.startswith("/v1/internal/harness/grade-results/") and path.endswith("/commit") and method == "POST":
            await self._internal_harness_commit(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/reference-conflicts/adjudicate" and method == "POST":
            await self._internal_harness_adjudicate(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/practice-reviews/adjudicate" and method == "POST":
            await self._internal_harness_adjudicate_practice_review(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/reference-conflicts/recheck" and method == "POST":
            await self._internal_harness_recheck(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/question-bank/reference" and method == "POST":
            await self._internal_harness_question_reference(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/context" and method == "POST":
            await self._internal_harness_context(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/practice-reviews/retry" and method == "POST":
            await self._internal_harness_retry_practice_review(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/reviews/defer" and method == "POST":
            await self._internal_harness_defer_review(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/reviews/resume" and method == "POST":
            await self._internal_harness_resume_review(scope, receive, send, headers)
            return
        if path.startswith("/v1/internal/harness/practice-pdfs/") and path.endswith("/reflow") and method == "POST":
            await self._internal_harness_reflow_practice_pdf(scope, receive, send, headers)
            return
        if path == "/v1/internal/harness/errors/remove" and method == "POST":
            await self._internal_harness_remove_error(scope, receive, send, headers)
            return
        expected_origin = f"{scope.get('scheme', 'https')}://{headers.get('host', '')}"
        supplied_origin = headers.get("origin")
        if (method not in safe_methods or supplied_origin is not None) and supplied_origin != expected_origin:
            await self._error(send, 403, "forbidden")
            return
        if path == "/manifest.webmanifest":
            if method != "GET":
                await self._error(send, 405, "method_not_allowed")
                return
            await self._asset(send, *self.static_files[path])
            return
        if path == "/healthz" or path.startswith("/v1/auth/") or path in {"/v1/session", "/v1/sessions"}:
            await self.auth(scope, receive, send)
            return
        static = self.static_files.get(path)
        if method == "GET" and static and path in self.public_static_paths:
            await self._asset(send, *static)
            return
        token = self._cookie(headers.get("cookie", ""), self.session_cookie)
        user = await asyncio.to_thread(self.auth_service.authenticate_session, token or "")
        if user is None:
            if method == "GET" and (static is not None or self._is_harness_http_path(path)):
                await self._redirect(send, "/login")
                return
            await self._error(send, 401, "authentication_required")
            return
        if method == "GET" and static and path != "/":
            await self._asset(send, *static)
            return
        if self._is_harness_http_path(path):
            if self.harness_gateway is None:
                if path == "/" and method == "GET":
                    await self._asset(send, *self.static_files["/"])
                else:
                    await self._error(send, 503, "harness_unavailable")
                return
            await self.harness_gateway.http(scope, receive, send, self._harness_target(path))
            return
        try:
            if path == "/v1/harness/sessions/bind" and method == "POST":
                payload = await self._json_body(receive)
                session_id = self._harness_session_id(payload)
                await self._sync(self.notebook.store.bind_model_session, user_id=user.user_id, session_id=session_id)
                with self._harness_sessions_lock:
                    existing_user = self._harness_sessions.get(session_id)
                    if existing_user is not None and existing_user != user.user_id:
                        raise PermissionError("model session already belongs to another user")
                    self._harness_sessions[session_id] = user.user_id
                await self._json(
                    send,
                    200,
                    {"status": "bound"},
                )
            elif path == "/v1/harness/sessions/usage" and method == "POST":
                payload = await self._json_body(receive)
                expected = {
                    "session_id", "uncached_input_tokens", "output_tokens",
                    "cache_read_tokens", "cache_write_tokens",
                }
                if set(payload) != expected:
                    raise ValueError("invalid model usage request")
                session_id = self._harness_session_id(payload)
                if await self._harness_user_id(session_id) != user.user_id:
                    raise PermissionError("unbound model session")
                await self._sync(
                    self.notebook.store.record_model_session_usage,
                    user_id=user.user_id,
                    session_id=session_id,
                    uncached_input_tokens=payload["uncached_input_tokens"],
                    output_tokens=payload["output_tokens"],
                    cache_read_tokens=payload["cache_read_tokens"],
                    cache_write_tokens=payload["cache_write_tokens"],
                )
                await self._json(
                    send,
                    200,
                    {"status": "recorded"},
                )
            elif path == "/v1/intakes" and method == "GET":
                intakes = await self._sync(self.notebook.store.list_pending_intakes, user_id=user.user_id)
                await self._json(send, 200, {"items": [self._intake_with_attachment(user.user_id, item) for item in intakes]})
            elif path.startswith("/v1/intakes/") and path.endswith("/source") and method == "GET":
                intake_id = path.split("/")[3]
                _, media_type, content = await self._sync(
                    self.notebook.read_intake_source, user_id=user.user_id, intake_id=intake_id,
                )
                await self._inline_bytes(send, 200, content, media_type)
            elif path.startswith("/v1/question-assets/") and method == "GET":
                filename = path.rsplit("/", 1)[1]
                media_type, content = await self._sync(
                    self.notebook.read_question_asset, user_id=user.user_id, filename=filename,
                )
                await self._inline_bytes(send, 200, content, media_type)
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
                        items = self._history_items(user.user_id, conversation_id, page["items"])
                        history = {
                            "items": items,
                            "next_cursor": self._encode_history_cursor(conversation_id, page.get("next_cursor")),
                        }
                        if items or page.get("next_cursor"):
                            break
                await self._json(send, 200, history)
            elif path == "/v1/conversations/latest" and method == "DELETE":
                with self._turn_cancellations_lock:
                    cancellations = [event for (owner, _), event in self._turn_cancellations.items() if owner == user.user_id]
                for cancellation in cancellations:
                    cancellation.set()
                await self._sync(self.notebook.clear_conversation, user_id=user.user_id)
                await send({"type": "http.response.start", "status": 204, "headers": [(b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b""})
            elif path == "/v1/harness/navigation-status" and method == "GET":
                items = await self._sync(self.notebook.store.list_errors, user_id=user.user_id)
                reviews = await self._sync(self.notebook.store.list_active_reviews, user_id=user.user_id)
                review_by_error = {item.error_id: item for item in reviews}
                error_state = [
                    [item.error_id, item.status, item.created_at.isoformat(), review.stage, review.status, review.due_at.isoformat()]
                    if (review := review_by_error.get(item.error_id)) else [item.error_id, item.status, item.created_at.isoformat()]
                    for item in items
                ]
                papers = await self._sync(self.notebook.list_practice_pdfs, user_id=user.user_id)
                paper_state = [
                    [item.get("task_id"), item.get("filename"), item.get("byte_size"), item.get("generated_at"),
                     (item.get("progress") or {}).get("answered_count"), (item.get("progress") or {}).get("required_count"),
                     (item.get("progress") or {}).get("needs_correction_count")]
                    for item in papers
                ]
                progress = await self._sync(self.notebook.progress, user_id=user.user_id)
                await self._json(send, 200, {
                    "scope": hashlib.sha256(f"navigation-status:{user.user_id}".encode("utf-8")).hexdigest()[:24],
                    "errors": {"count": len(items), "revision": hashlib.sha256(json.dumps(error_state, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]},
                    "practice": {"count": len(papers), "revision": hashlib.sha256(json.dumps(paper_state, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]},
                    "progress": {"due_count": int(progress["due_review_count"]), "needs_correction_count": int(progress["today_needs_correction_count"]),
                                 "deferred_count": int(progress["deferred_review_count"])},
                })
            elif path == "/v1/workbench" and method == "GET":
                items = await self._sync(self.notebook.store.list_errors, user_id=user.user_id)
                pending = await self._sync(self.notebook.store.pending_job_count, user_id=user.user_id)
                progress = await self._sync(self.notebook.progress, user_id=user.user_id)
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
            elif path == "/v1/intake/batches" and method == "POST":
                payload = await self._json_body(receive)
                if set(payload) != {"file_ids"} or not isinstance(payload["file_ids"], list):
                    raise ValueError("invalid intake batch request")
                engine = self._ensure_intake_batch_engine()
                batch, _ = await self._sync(
                    self.notebook.store.batch_repository.create_batch,
                    user_id=user.user_id,
                    file_ids=payload["file_ids"],
                    idempotency_key=self._key(headers),
                )
                engine.start()
                await self._json(send, 202, self._batch(batch))
            elif path == "/v1/intake/batches/active" and method == "GET":
                query = parse_qs(scope.get("query_string", b"").decode("ascii"), keep_blank_values=True, strict_parsing=True)
                if set(query) - {"updated_after"} or any(len(values) != 1 for values in query.values()):
                    raise ValueError("invalid recovery cursor")
                recovery_time = await self._sync(self.notebook.store.batch_repository.recovery_cursor)
                if "updated_after" in query:
                    raw_updated_after = query["updated_after"][0]
                    if re.fullmatch(r"0|[1-9][0-9]{0,12}", raw_updated_after) is None:
                        raise ValueError("invalid recovery cursor")
                    cursor_millis = int(raw_updated_after)
                    if cursor_millis > int(recovery_time.timestamp() * 1_000):
                        raise ValueError("invalid recovery cursor")
                    try:
                        updated_after = datetime.fromtimestamp(cursor_millis / 1000, timezone.utc)
                    except (OverflowError, OSError) as exc:
                        raise ValueError("invalid recovery cursor") from exc
                    batch = await self._sync(
                        self.notebook.store.batch_repository.find_recoverable_batch,
                        user_id=user.user_id,
                        updated_after=updated_after,
                    )
                else:
                    batch = await self._sync(
                        self.notebook.store.batch_repository.find_active_batch,
                        user_id=user.user_id,
                    )
                recovery_cursor = str(int(recovery_time.timestamp() * 1_000))
                await self._json(send, 200, {
                    "batch": self._batch(batch) if batch else None,
                    "recovery_cursor": recovery_cursor,
                })
            elif path.startswith("/v1/intake/batches/") and path.endswith("/events") and method == "GET":
                batch_id = path.split("/")[-2]
                await self._batch_events(receive, send, headers, user.user_id, batch_id)
            elif path.startswith("/v1/intake/batches/") and method == "GET":
                batch_id = path.rsplit("/", 1)[1]
                batch = await self._sync(
                    self.notebook.store.batch_repository.get_batch,
                    user_id=user.user_id,
                    batch_id=batch_id,
                )
                if batch is None:
                    raise LookupError
                await self._json(send, 200, self._batch(batch))
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
                thread_owner_id, thread_id = await self._sync(self._intake_thread, user.user_id, intake)
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
                    conversation_id=thread_owner_id,
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
                _, thread_id = await self._sync(self._intake_thread, user.user_id, intake)
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
                attempt = await self._sync(self.notebook.store.get_attempt, user_id=user.user_id, attempt_id=attempt_id)
                if not attempt:
                    raise LookupError
                await self._sync(self.notebook.store.reserve_grade_batch, user_id=user.user_id, intake_ids=[attempt.intake_id])
                counted = False
                try:
                    candidate = await self._sync(
                        self.notebook.store.record_grade_candidate,
                        user_id=user.user_id,
                        attempt_id=attempt_id,
                        input_version=int(payload["input_version"]),
                        verdict=verdict,
                        first_error=first_error,
                        evidence=evidence,
                    )
                    counted = candidate.verdict != "unclear"
                finally:
                    await self._sync(self.notebook.store.finish_grade_usage, user_id=user.user_id, intake_id=attempt.intake_id, counted=counted)
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
                intake = await self._sync(self.notebook.store.get_intake, user_id=user.user_id, intake_id=attempt.intake_id)
                if not intake:
                    raise LookupError
                file_record = await self._sync(self.notebook.store.get_file, user_id=user.user_id, file_id=intake.file_id)
                if not file_record or file_record.media_type not in {"image/png", "image/jpeg"}:
                    raise ModelUnavailableError("grading requires the original PNG or JPEG")
                await self._sync(self.notebook.store.reserve_grade_batch, user_id=user.user_id, intake_ids=[intake.intake_id])
                counted = False
                try:
                    reference = await self._sync(
                        self.notebook.store.find_verified_question,
                        question_text=attempt.question_text,
                    )
                    thread_owner_id, thread_id = await self._sync(self._intake_thread, user.user_id, intake)
                    with self.notebook.files.model_preview(file_record.object_key, file_record.content_sha256) as image_path:
                        result = await self._sync(
                            self.model_runner.grade, attempt=attempt, image_path=image_path, thread_id=thread_id, reference=reference,
                        )
                    await self._sync(
                        self.notebook.store.save_codex_thread,
                        user_id=user.user_id,
                        conversation_id=thread_owner_id,
                        thread_id=result["thread_id"],
                    )
                    if result.get("attempt_id") != attempt.attempt_id or result.get("input_version") != attempt.input_version:
                        raise ModelUnavailableError("model response does not match the frozen attempt")
                    verdict, first_error, evidence = self._grade_values(
                        result,
                        evidence_key="cause_evidence",
                        cross_validation=result.get("cross_validation"),
                    )
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
                    counted = candidate.verdict != "unclear"
                    validation = self._diagnosis(candidate.evidence).get("cross_validation")
                    if isinstance(validation, dict) and validation.get("status") == "consistent":
                        await self._sync(
                            self.notebook.store.link_attempt_question,
                            user_id=user.user_id,
                            attempt_id=attempt_id,
                            question_id=str(validation["question_id"]),
                        )
                finally:
                    await self._sync(self.notebook.store.finish_grade_usage, user_id=user.user_id, intake_id=intake.intake_id, counted=counted)
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
                reviews = await self._sync(self.notebook.store.list_active_reviews, user_id=user.user_id)
                review_by_error = {item.error_id: item for item in reviews}
                selection_scope = hashlib.sha256(f"review-selection:{user.user_id}".encode("utf-8")).hexdigest()[:24]
                await self._json(send, 200, {"selection_scope": selection_scope, "items": [self._error_entry(item) | {"review": self._review(review_by_error[item.error_id]) if item.error_id in review_by_error else None} for item in items]})
            elif path == "/v1/learning-usage" and method == "GET":
                await self._json(send, 200, await self._sync(self.notebook.store.learning_usage, user_id=user.user_id))
            elif path.startswith("/v1/errors/") and path.endswith("/master") and method == "POST":
                entry = await self._sync(self.notebook.store.set_error_status, user_id=user.user_id, error_id=path.split("/")[-2], status="mastered")
                await self._json(send, 200, self._error_entry(entry))
            elif path.startswith("/v1/errors/") and path.endswith("/recommendations") and method in {"GET", "POST"}:
                error_id = path.split("/")[-2]
                if method == "POST":
                    self._key(headers)
                    query = self._query(scope, allowed={"limit"})
                    limit = int(query.get("limit", "2"))
                    if limit not in {1, 2}:
                        raise ValueError("invalid recommendation limit")
                    recommendations, gap = await self._sync(self.notebook.store.assign_recommendations, user_id=user.user_id, error_id=error_id, limit=limit)
                else:
                    recommendations = await self._sync(self.notebook.store.list_recommendations, user_id=user.user_id, error_id=error_id)
                    gap = len(recommendations) < 2
                usage = await self._sync(self.notebook.store.learning_usage, user_id=user.user_id)
                await self._json(send, 200, {"items": [self._recommendation(item) for item in recommendations], "gap": gap, "usage": usage})
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
            elif path == "/v1/practice-review-links" and method == "GET":
                items = await self._sync(self.notebook.list_pending_practice_review_links, user_id=user.user_id)
                await self._json(send, 200, {"items": items, "count": len(items)})
            elif path.startswith("/v1/practice-review-links/") and method == "POST":
                candidate_id = path.rsplit("/", 1)[1]
                payload = await self._json_body(receive)
                if set(payload) != {"input_version", "code"} or not isinstance(payload["code"], str) or len(payload["code"]) > 180:
                    raise ValueError("invalid review selection")
                candidate = await self._sync(
                    self.notebook.link_pending_practice_review, user_id=user.user_id,
                    candidate_id=candidate_id, input_version=int(payload["input_version"]), code=payload["code"],
                )
                receipt = await self._commit_candidate_receipt(user.user_id, candidate)
                await self._json(send, 200, {"receipt": receipt})
            elif path.startswith("/v1/reviews/") and path.endswith("/complete") and method == "POST":
                payload = await self._json_body(receive)
                next_task = await self._sync(self.notebook.store.complete_review, user_id=user.user_id, task_id=path.split("/")[-2], result=str(payload["result"]), idempotency_key=self._key(headers))
                await self._json(send, 200, {"completed": True, "next_review": self._review(next_task) if next_task else None, "mastered": next_task is None})
            elif path.startswith("/v1/reviews/") and path.endswith("/defer") and method == "POST":
                payload = await self._json_body(receive)
                if set(payload) != {"days", "reason"}:
                    raise ValueError("invalid review deferral")
                task = await self._sync(self.notebook.store.defer_review, user_id=user.user_id, task_id=path.split("/")[-2],
                                        days=payload["days"], reason=payload["reason"], idempotency_key=self._key(headers))
                await self._json(send, 200, {"review": self._review(task), "message": "已标记为待补知识；延期期间不计完成，也不推进复习阶段。"})
            elif path.startswith("/v1/reviews/") and path.endswith("/resume") and method == "POST":
                payload = await self._json_body(receive)
                if payload:
                    raise ValueError("invalid review resume")
                task = await self._sync(self.notebook.store.resume_review, user_id=user.user_id, task_id=path.split("/")[-2], idempotency_key=self._key(headers))
                await self._json(send, 200, {"review": self._review(task), "message": "已恢复学习，该题重新进入当前待复习任务。"})
            elif path == "/v1/progress/calendar" and method == "GET":
                month = self._query(scope, allowed={"month"}).get("month", "")
                calendar = await self._sync(self.notebook.review_calendar, user_id=user.user_id, month=month)
                await self._json(send, 200, calendar)
            elif path == "/v1/progress" and method == "GET":
                progress = await self._sync(self.notebook.progress, user_id=user.user_id)
                await self._json(send, 200, progress)
            elif path == "/v1/bank/status" and method == "GET":
                await self._json(send, 200, await self._sync(self.notebook.store.bank_status))
            elif path == "/v1/practice-pdfs" and method == "GET":
                items = await self._sync(self.notebook.list_practice_pdfs, user_id=user.user_id)
                plan = await self._sync(self.notebook.today_practice_plan, user_id=user.user_id, papers=items)
                await self._json(send, 200, {"items": [item | {"download_url": f"/v1/practice-pdfs/{item['task_id']}/download"} for item in items], "count": len(items), "today_plan": plan})
            elif path == "/v1/practice-pdfs" and method == "POST":
                payload = await self._json_body(receive)
                error_ids = payload.get("error_ids")
                include_answers = payload.get("include_answers", False)
                plan_kind = payload.get("plan_kind", "daily_review")
                if not isinstance(error_ids, list) or not all(isinstance(item, str) for item in error_ids) or not 1 <= len(error_ids) <= 12 or not isinstance(include_answers, bool) or plan_kind not in {"daily_review", "practice"}:
                    raise ValueError("invalid practice request")
                job = await self._sync(self.notebook.create_practice_pdf, user_id=user.user_id, error_ids=list(dict.fromkeys(error_ids)), idempotency_key=self._key(headers), include_answers=include_answers, plan_kind=plan_kind)
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
            code = str(exc) if str(exc) in {
                "input_version_changed", "waiting_confirmation", "failed_final", "reference_conflict",
                "conflict", "idempotency_conflict", "daily_grade_limit", "daily_recommendation_limit",
                "batch_limit_reached", "batch_rate_limited",
            } else "conflict"
            limited = code.startswith("daily_") or code in {"batch_limit_reached", "batch_rate_limited"}
            await self._error(send, 429 if limited else 422 if code == "failed_final" else 409, code)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    def _ensure_intake_batch_engine(self) -> Any:
        if self.intake_batch_engine is not None:
            return self.intake_batch_engine
        if self.model_runner is None:
            raise ModelUnavailableError("local model processing is disabled")
        self.intake_batch_engine = IntakeBatchEngine(
            self.notebook.store.batch_repository,
            NotebookIntakeBatchProcessor(self.notebook, self.model_runner),
        )
        return self.intake_batch_engine

    async def _batch_events(
        self,
        receive: Receive,
        send: Send,
        headers: dict[str, str],
        user_id: str,
        batch_id: str,
    ) -> None:
        repository = self.notebook.store.batch_repository
        current = await self._sync(repository.get_batch, user_id=user_id, batch_id=batch_id)
        if current is None:
            await self._error(send, 404, "not_found")
            return
        raw_cursor = headers.get("last-event-id", "")
        if raw_cursor and re.fullmatch(r"0|[1-9][0-9]{0,18}", raw_cursor) is None:
            await self._error(send, 400, "invalid_event_cursor")
            return
        cursor = int(raw_cursor) if raw_cursor else 0
        if cursor > current.last_event_id:
            await self._error(send, 400, "invalid_event_cursor")
            return
        if not self._claim_sse_connection(user_id=user_id, batch_id=batch_id):
            await self._error(send, 429, "rate_limited")
            return
        disconnect = asyncio.create_task(self._wait_for_disconnect(receive))
        try:
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"x-accel-buffering", b"no"),
                ],
            })
            while True:
                if disconnect.done():
                    return
                events = await self._sync(
                    repository.list_events,
                    user_id=user_id,
                    batch_id=batch_id,
                    after_sequence=cursor,
                )
                for event in events:
                    payload = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
                    body = f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n".encode("utf-8")
                    await send({"type": "http.response.body", "body": body, "more_body": True})
                    cursor = event.sequence
                current = await self._sync(repository.get_batch, user_id=user_id, batch_id=batch_id)
                if current is None:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                if current.terminal and cursor >= current.last_event_id:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                if disconnect in (await asyncio.wait({disconnect}, timeout=1.0))[0]:
                    return
        finally:
            disconnect.cancel()
            try:
                await disconnect
            except asyncio.CancelledError:
                pass
            self._release_sse_connection(user_id=user_id, batch_id=batch_id)

    def _claim_sse_connection(self, *, user_id: str, batch_id: str) -> bool:
        key = (user_id, batch_id)
        with self._sse_connections_lock:
            if (
                self._sse_connections_total >= 200
                or self._sse_connections_by_user.get(user_id, 0) >= 4
                or self._sse_connections_by_batch.get(key, 0) >= 2
            ):
                return False
            self._sse_connections_total += 1
            self._sse_connections_by_user[user_id] = self._sse_connections_by_user.get(user_id, 0) + 1
            self._sse_connections_by_batch[key] = self._sse_connections_by_batch.get(key, 0) + 1
            return True

    def _release_sse_connection(self, *, user_id: str, batch_id: str) -> None:
        key = (user_id, batch_id)
        with self._sse_connections_lock:
            self._sse_connections_total = max(0, self._sse_connections_total - 1)
            for counters, counter_key in (
                (self._sse_connections_by_user, user_id),
                (self._sse_connections_by_batch, key),
            ):
                remaining = counters.get(counter_key, 0) - 1
                if remaining > 0:
                    counters[counter_key] = remaining
                else:
                    counters.pop(counter_key, None)

    @staticmethod
    async def _wait_for_disconnect(receive: Receive) -> None:
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return

    @staticmethod
    def _batch(value: IntakeBatch) -> dict[str, Any]:
        return {
            "schema": "intake-batch/v1",
            "batch_id": value.batch_id,
            "status": value.status,
            "current_stage": None if value.terminal else value.status,
            "total_files": value.total_files,
            "completed_files": value.completed_files,
            "total_items": value.total_items,
            "completed_items": value.completed_items,
            "stage_completed_items": value.stage_completed_items,
            "terminal": value.terminal,
            "last_event_id": value.last_event_id,
            "error_code": value.error_code,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
            "events_url": f"/v1/intake/batches/{value.batch_id}/events",
        }

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
        thread_owner_id, thread_id = self._intake_thread(user_id, intake)
        if stage == "intake":
            if payload["attempt_id"] is not None or payload["candidate_id"] is not None:
                raise ValueError("invalid intake chat context")
            result = self.model_runner.chat_turn(
                conversation_id=intake_id, stage="intake", resource_id=intake_id,
                input_version=intake.input_version, user_message=message,
                context={"question_text": intake.question_text, "answer_text": intake.answer_text, "status": intake.status},
                thread_id=thread_id, event_callback=event_callback, cancel_event=cancel_event,
            )
            self.notebook.store.save_codex_thread(user_id=user_id, conversation_id=thread_owner_id, thread_id=result["thread_id"])
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
        self.notebook.store.save_codex_thread(user_id=user_id, conversation_id=thread_owner_id, thread_id=result["thread_id"])
        if result.get("action") == "revise_grade":
            prior_validation = self._diagnosis(candidate.evidence).get("cross_validation") if candidate else None
            verdict, first_error, evidence = self._grade_values(
                result,
                evidence_key="cause_evidence",
                cross_validation=prior_validation if isinstance(prior_validation, dict) else None,
            )
            self.notebook.store.reserve_grade_batch(user_id=user_id, intake_ids=[intake.intake_id])
            counted = False
            try:
                candidate = self.notebook.store.record_grade_candidate(
                    user_id=user_id, attempt_id=attempt_id, input_version=attempt.input_version,
                    verdict=verdict, first_error=first_error, evidence=evidence, confidence=float(result["confidence"]),
                )
                counted = candidate.verdict != "unclear"
            finally:
                self.notebook.store.finish_grade_usage(user_id=user_id, intake_id=intake.intake_id, counted=counted)
        return {
            "assistant_message": result["assistant_message"], "action": result["action"],
            "stage": "grade", "candidate": self._candidate(candidate) if candidate else None, "model": result.get("route"),
        }

    def _intake_thread(self, user_id: str, intake: IntakeItem) -> tuple[str, str | None]:
        """Resolve one parent Harness thread for every question split from the same file."""
        direct = self.notebook.store.get_codex_thread(user_id=user_id, conversation_id=intake.intake_id)
        if direct:
            return intake.intake_id, direct
        for sibling in self.notebook.store.get_file_intakes(user_id=user_id, file_id=intake.file_id):
            thread_id = self.notebook.store.get_codex_thread(user_id=user_id, conversation_id=sibling.intake_id)
            if thread_id:
                return sibling.intake_id, thread_id
        return intake.intake_id, None

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

    async def _read(self, receive: Receive, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_upload_bytes if max_bytes is None else max_bytes
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                raise ValueError("invalid ASGI message")
            body.extend(message.get("body", b""))
            if len(body) > limit:
                raise RequestTooLarge
            if not message.get("more_body", False):
                return bytes(body)

    async def _json_body(self, receive: Receive, *, max_bytes: int | None = None) -> dict[str, Any]:
        value = json.loads((await self._read(receive, max_bytes=max_bytes)).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value

    @staticmethod
    def _query(scope: dict[str, Any], *, allowed: set[str] | None = None) -> dict[str, str]:
        raw = scope.get("query_string", b"")
        if not isinstance(raw, bytes) or len(raw) > 4096:
            raise ValueError("invalid query")
        values = parse_qs(raw.decode("ascii"), keep_blank_values=True, max_num_fields=4)
        if set(values) - (allowed or {"cursor"}) or any(len(items) != 1 for items in values.values()):
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

    def _attachment(self, user_id: str, value: IntakeItem) -> dict[str, Any] | None:
        record = self.notebook.store.get_file(user_id=user_id, file_id=value.file_id)
        if not record or record.purpose != "question_image" or record.media_type not in {"image/jpeg", "image/png"}:
            return None
        return {
            "attachment_id": record.file_id,
            "name": record.original_name,
            "media_type": record.media_type,
            "preview_url": f"/v1/intakes/{value.intake_id}/source",
        }

    def _intake_with_attachment(self, user_id: str, value: IntakeItem) -> dict[str, Any]:
        payload = self._intake(value)
        payload["attachment"] = self._attachment(user_id, value)
        return payload

    def _history_items(self, user_id: str, conversation_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        intake = self.notebook.store.get_intake(user_id=user_id, intake_id=conversation_id)
        attachment = self._attachment(user_id, intake) if intake else None
        if not attachment:
            return items
        result = [dict(item) for item in items]
        for item in result:
            if item.get("role") == "user":
                item["attachments"] = [attachment]
                return result
        return [{"role": "user", "text": "请整理这 1 个文件", "attachments": [attachment]}, *result[:19]]

    @staticmethod
    def _candidate(value: GradeCandidate) -> dict[str, Any]:
        return {"result_id": value.candidate_id, "input_version": value.input_version, "verdict": value.verdict, "status": value.status, "first_error": value.first_error, "diagnosis": NotebookAsgiApp._diagnosis(value.evidence)}

    @staticmethod
    def _error_entry(value: ErrorEntry) -> dict[str, Any]:
        return {"error_id": value.error_id, "question_id": value.question_id, "status": value.status, "question_text": value.question_text, "answer_text": value.answer_text, "first_error": value.first_error, "diagnosis": NotebookAsgiApp._diagnosis(value.evidence), "created_at": value.created_at.isoformat()}

    @staticmethod
    def _diagnosis(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {"correct_solution": value}
        return payload if isinstance(payload, dict) and payload.get("schema") == "math-error-diagnosis/v1" else {"correct_solution": value}

    async def _internal_harness_batch_create(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            engine = self._ensure_intake_batch_engine()
            payload = await self._json_body(receive, max_bytes=self.max_upload_bytes * 7)
            if set(payload) != {"session_id", "attachments"}:
                raise ValueError("invalid Harness batch request")
            session_id = self._harness_session_id(payload)
            with self._harness_sessions_lock:
                user_id = self._harness_sessions.get(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            attachments = payload["attachments"]
            if not isinstance(attachments, list) or not 1 <= len(attachments) <= 5:
                raise ValueError("invalid Harness attachments")
            file_ids: list[str] = []
            digests: list[str] = []
            total_bytes = 0
            for attachment in attachments:
                if not isinstance(attachment, dict) or set(attachment) != {"attachment_id", "name", "media_type", "data"}:
                    raise ValueError("invalid Harness attachment")
                if not all(isinstance(attachment[key], str) for key in attachment):
                    raise ValueError("invalid Harness attachment")
                digest_match = re.fullmatch(r"sha256:([0-9a-f]{64})", attachment["attachment_id"])
                if (
                    digest_match is None
                    or attachment["media_type"] not in {"image/png", "image/jpeg"}
                    or not 1 <= len(attachment["name"]) <= 255
                ):
                    raise ValueError("invalid Harness attachment")
                try:
                    content = base64.b64decode(attachment["data"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("invalid Harness attachment") from exc
                total_bytes += len(content)
                digest = digest_match.group(1)
                if (
                    not content
                    or len(content) > self.max_upload_bytes
                    or total_bytes > self.max_upload_bytes * 5
                    or hashlib.sha256(content).hexdigest() != digest
                ):
                    raise ValueError("invalid Harness attachment")
                record = await self._sync(
                    self.notebook.upload,
                    user_id=user_id,
                    purpose="question_image",
                    original_name=attachment["name"],
                    content=content,
                    idempotency_key=f"harness-file-{digest[:51]}",
                )
                file_ids.append(record.file_id)
                digests.append(digest)
            request_hash = hashlib.sha256(":".join(digests).encode("ascii")).hexdigest()
            batch, _ = await self._sync(
                self.notebook.store.batch_repository.create_batch,
                user_id=user_id,
                file_ids=file_ids,
                idempotency_key=f"harness-batch-{request_hash[:50]}",
            )
            engine.start()
            await self._json(send, 202, self._batch(batch))
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except ModelUnavailableError as exc:
            await self._error(send, 503, exc.code)
        except LookupError:
            await self._error(send, 404, "not_found")
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {
                "idempotency_conflict", "batch_limit_reached", "batch_rate_limited",
            } else "conflict"
            await self._error(send, 429 if code in {"batch_limit_reached", "batch_rate_limited"} else 409, code)
        except RequestTooLarge:
            await self._error(send, 413, "request_too_large")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_batch_get(
        self,
        scope: dict[str, Any],
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            session_id = self._harness_session_id({"session_id": headers.get("x-lzlm-session-id")})
            with self._harness_sessions_lock:
                user_id = self._harness_sessions.get(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            batch_id = str(scope.get("path", "")).rsplit("/", 1)[1]
            batch = await self._sync(
                self.notebook.store.batch_repository.get_batch,
                user_id=user_id,
                batch_id=batch_id,
            )
            if batch is None:
                raise LookupError
            response = self._batch(batch)
            if batch.status == "completed":
                response["results"] = await self._harness_batch_results(user_id, batch_id)
                response["usage"] = await self._sync(self.notebook.store.learning_usage, user_id=user_id)
            await self._json(send, 200, response)
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except LookupError:
            await self._error(send, 404, "not_found")
        except (KeyError, TypeError, ValueError):
            await self._error(send, 400, "invalid_request")

    async def _harness_batch_results(self, user_id: str, batch_id: str) -> list[dict[str, Any]]:
        repository = self.notebook.store.batch_repository
        snapshots = await self._sync(repository.list_files, batch_id=batch_id)
        file_ordinals = {snapshot.file_id: snapshot.ordinal for snapshot in snapshots}
        operations = await self._sync(
            repository.list_operations, user_id=user_id, batch_id=batch_id, stage="grading",
        )
        results = []
        for operation in operations:
            intake_id = str(operation.result["intake_id"])
            candidate_id = str(operation.result["candidate_id"])
            intake = await self._sync(self.notebook.store.get_intake, user_id=user_id, intake_id=intake_id)
            candidate = await self._sync(
                self.notebook.store.get_grade_candidate, user_id=user_id, candidate_id=candidate_id,
            )
            if intake is None or candidate is None:
                raise LookupError("batch result not found")
            diagnosis = self._diagnosis(candidate.evidence)
            error_id = str(operation.result.get("error_id") or "")
            if candidate.verdict in {"correct"}:
                receipt_status, receipt_message = "not_saved_correct", "本题作答正确，未计入错题本。"
            elif candidate.verdict == "unclear":
                receipt_status, receipt_message = "needs_review", "证据不足，需要补充或人工确认。"
            elif error_id:
                receipt_status, receipt_message = "saved", "已计入错题本并安排首次复习。"
            else:
                receipt_status, receipt_message = "needs_review", "参考证据存在冲突，等待复核。"
            reference_review = None
            validation = diagnosis.get("cross_validation")
            if isinstance(validation, dict) and validation.get("status") == "conflict":
                reference = await self._sync(
                    self.notebook.store.find_verified_question, question_text=intake.question_text,
                )
                if reference is not None:
                    reference_review = {
                        "source_title": reference.source_title,
                        "version_no": reference.version_no,
                        "independent_answer": str(diagnosis.get("final_answer") or ""),
                        "reference_answer": reference.answer_text,
                        "reference_solution": reference.solution_text or "",
                    }
            results.append({
                "attachment_index": file_ordinals.get(intake.file_id, 1),
                "item_no": intake.item_no,
                "candidate_id": candidate.candidate_id,
                "input_version": candidate.input_version,
                "verdict": candidate.verdict,
                "question_text": intake.question_text,
                "answer_text": intake.answer_text,
                "first_error": candidate.first_error or "",
                "cause_code": str(diagnosis.get("cause_code") or ""),
                "cause_evidence": str(diagnosis.get("cause_evidence") or ""),
                "knowledge_points": diagnosis.get("knowledge_points") if isinstance(diagnosis.get("knowledge_points"), list) else [],
                "correct_solution": str(diagnosis.get("correct_solution") or ""),
                "final_answer": str(diagnosis.get("final_answer") or ""),
                "prevention_cue": str(diagnosis.get("prevention_cue") or ""),
                "receipt_status": receipt_status,
                "receipt_message": receipt_message,
                "error_id": error_id,
                "reference_review": reference_review,
            })
        return results

    async def _internal_harness_grading_references(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        """Resolve only exact, current references before the model grades frozen OCR."""
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive, max_bytes=self.max_upload_bytes * 2)
            if set(payload) != {"session_id", "attachment", "items"}:
                raise ValueError("invalid grading reference request")
            session_id = self._harness_session_id(payload)
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            _, _, _, content, _ = self._harness_attachment(payload["attachment"])
            raw_items = payload["items"]
            if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 20:
                raise ValueError("invalid grading reference items")
            locators = []
            for index, item in enumerate(raw_items, 1):
                if (
                    not isinstance(item, dict)
                    or set(item) != {"item_no", "question_text", "review"}
                    or item.get("item_no") != index
                    or isinstance(item.get("item_no"), bool)
                    or not isinstance(item.get("question_text"), str)
                    or not 1 <= len(item["question_text"].strip()) <= 12000
                ):
                    raise ValueError("invalid grading reference items")
                locators.append(review_locator(item.get("review")))
            qr_codes = decode_review_qr_codes(content)
            if len(qr_codes) == len(locators):
                locators = [{"code": code} for code in qr_codes]
            results = []
            for item, locator in zip(raw_items, locators, strict=True):
                context = None
                if locator.get("code") or locator.get("question_id"):
                    context = await self._sync(
                        self.notebook.resolve_practice_review,
                        user_id=user_id,
                        question_text=item["question_text"].strip(),
                        locator=locator,
                        review_mode=True,
                    )
                question_id = str((context or {}).get("question_id") or "")
                if not question_id and re.fullmatch(r"[0-9a-f]{32}", str(locator.get("question_id") or "")):
                    question_id = str(locator["question_id"])
                reference = await self._sync(
                    self.notebook.store.get_verified_question,
                    question_id=question_id,
                ) if question_id else None
                result = {"item_no": item["item_no"], "grading_strategy": "independent", "reference": None}
                if reference is not None:
                    result.update(grading_strategy="verified_reference", reference={
                        "question_id": reference.question_id,
                        "version_no": reference.version_no,
                        "question_text": reference.stem_text,
                        "reference_answer": reference.answer_text,
                        "reference_solution": reference.solution_text or "",
                        "source_title": reference.source_title,
                    })
                results.append(result)
            await self._json(send, 200, {"items": results})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RequestTooLarge:
            await self._error(send, 413, "request_too_large")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_process(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        user_id: str | None = None
        process_key: tuple[str, str] | None = None
        process_claimed = False
        reserved_intakes: list[str] = []
        completed_intakes: set[str] = set()
        try:
            payload = await self._json_body(receive, max_bytes=self.max_upload_bytes * 2)
            if not {"session_id", "attachment", "items"} <= set(payload) or set(payload) - {"session_id", "attachment", "items", "review_mode", "correction_mode"}:
                raise ValueError("invalid Harness processing request")
            if not isinstance(payload.get("review_mode", False), bool) or not isinstance(payload.get("correction_mode", False), bool):
                raise ValueError("invalid review mode")
            session_id = self._harness_session_id(payload)
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            attachment_id, name, media_type, content, digest = self._harness_attachment(payload["attachment"])
            raw_items = payload["items"]
            required = {
                "item_no", "question_text", "answer_text", "verdict", "first_error", "cause_code",
                "cause_evidence", "knowledge_points", "correct_solution", "final_answer", "prevention_cue", "confidence",
            }
            if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 20:
                raise ValueError("invalid Harness result items")
            for index, item in enumerate(raw_items, 1):
                if (
                    not isinstance(item, dict)
                    or not required <= set(item) or set(item) - required - {"review", "grading_strategy"}
                    or item.get("item_no") != index
                    or isinstance(item.get("item_no"), bool)
                ):
                    raise ValueError("invalid Harness result items")
                string_fields = required - {"item_no", "knowledge_points", "confidence"}
                if any(not isinstance(item.get(key), str) for key in string_fields):
                    raise ValueError("invalid Harness result items")
                confidence = item.get("confidence")
                review_locator(item.get("review"))
                if item.get("grading_strategy", "independent") not in {"verified_reference", "independent"}:
                    raise ValueError("invalid grading strategy")
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
                    raise ValueError("invalid Harness confidence")

            process_key = (user_id, digest)
            with self._harness_processes_lock:
                if process_key in self._harness_processes:
                    raise RuntimeError("conflict")
                self._harness_processes.add(process_key)
                process_claimed = True
            record = await self._sync(
                self.notebook.upload,
                user_id=user_id,
                purpose="question_image",
                original_name=name,
                content=content,
                idempotency_key=f"harness-file-{digest[:51]}",
            )
            review_locators = [review_locator(item.get("review")) for item in raw_items]
            qr_codes = decode_review_qr_codes(content)
            if len(qr_codes) == len(review_locators):
                review_locators = [{"code": code} for code in qr_codes]
            exact_review_contexts = []
            for item, locator in zip(raw_items, review_locators, strict=True):
                context = None
                if locator.get("code") or locator.get("question_id"):
                    context = await self._sync(
                        self.notebook.resolve_practice_review,
                        user_id=user_id,
                        question_text=str(item["question_text"]).strip(),
                        locator=locator,
                        review_mode=True,
                    )
                exact_review_contexts.append(context if context and context.get("status") == "matched" else None)
            batch_jobs = {context["job_id"] for context in exact_review_contexts if context}
            if len(batch_jobs) == 1:
                batch_job = next(iter(batch_jobs))
                for index, item in enumerate(raw_items):
                    if exact_review_contexts[index] is not None:
                        continue
                    locator = dict(review_locators[index])
                    locator.setdefault("pdf_id", batch_job)
                    context = await self._sync(
                        self.notebook.resolve_practice_review,
                        user_id=user_id,
                        question_text=str(item["question_text"]).strip(),
                        locator=locator,
                        review_mode=True,
                    )
                    if context and context.get("status") == "matched":
                        exact_review_contexts[index] = context
            intakes = await self._sync(self.notebook.store.get_file_intakes, user_id=user_id, file_id=record.file_id)
            if intakes:
                primary = intakes[0]
            else:
                primary, _ = await self._sync(
                    self.notebook.store.create_intake,
                    user_id=user_id,
                    file_id=record.file_id,
                    idempotency_key=f"harness-intake-{digest[:49]}",
                )
            extracted = [
                {
                    "item_no": index,
                    "question_text": str((exact_review_contexts[index - 1] or {}).get("stem_text") or item["question_text"]).strip(),
                    "answer_text": str(item["answer_text"]).strip(),
                }
                for index, item in enumerate(raw_items, 1)
            ]
            if primary.status == "extracting":
                intakes = await self._sync(
                    self.notebook.store.save_extraction_candidates,
                    user_id=user_id,
                    intake_id=primary.intake_id,
                    items=extracted,
                    evidence={"source": "deepseek_harness_tool", "attachment_id": attachment_id},
                )
            else:
                existing = [(item.item_no, item.question_text, item.answer_text) for item in intakes]
                requested = [(item["item_no"], item["question_text"], item["answer_text"]) for item in extracted]
                if existing != requested:
                    if all(item.status == "waiting_confirmation" for item in intakes):
                        intakes = await self._sync(
                            self.notebook.store.save_extraction_candidates,
                            user_id=user_id,
                            intake_id=primary.intake_id,
                            items=extracted,
                            evidence={"source": "deepseek_harness_tool", "attachment_id": attachment_id},
                            replace_existing=True,
                        )
                    elif all(item.status == "confirmed" for item in intakes):
                        pass  # Same image: the first completed extraction is authoritative.
                    else:
                        raise RuntimeError("conflict")

            frozen_candidates: dict[str, GradeCandidate] = {}
            if any(intake.status == "confirmed" for intake in intakes):
                for candidate in await self._sync(self.notebook.store.list_latest_grade_candidates, user_id=user_id):
                    attempt = await self._sync(self.notebook.store.get_attempt, user_id=user_id, attempt_id=candidate.attempt_id)
                    if attempt is not None:
                        frozen_candidates[attempt.intake_id] = candidate
                if any(intake.status != "confirmed" or intake.intake_id not in frozen_candidates for intake in intakes):
                    raise RuntimeError("conflict")

            if not frozen_candidates:
                reserved_intakes = [intake.intake_id for intake in intakes]
                await self._sync(self.notebook.store.reserve_grade_batch, user_id=user_id, intake_ids=reserved_intakes)
            results = []
            processing_items = [(intake, None, None, None) for intake in intakes] if frozen_candidates else zip(intakes, raw_items, exact_review_contexts, review_locators, strict=True)
            for intake, item, exact_context, exact_locator in processing_items:
                exact_question_id = str((exact_context or {}).get("question_id") or "")
                if not exact_question_id and re.fullmatch(r"[0-9a-f]{32}", str((exact_locator or {}).get("question_id") or "")):
                    exact_question_id = str(exact_locator["question_id"])
                reference = (
                    await self._sync(self.notebook.store.get_verified_question, question_id=exact_question_id)
                    if exact_question_id else
                    await self._sync(self.notebook.store.find_verified_question, question_text=intake.question_text)
                )
                if frozen_candidates:
                    candidate = frozen_candidates[intake.intake_id]
                    final_answer = str(self._diagnosis(candidate.evidence).get("final_answer") or "")
                else:
                    assert item is not None
                    grading_strategy = item.get("grading_strategy", "independent")
                    if grading_strategy == "verified_reference" and (not exact_question_id or reference is None):
                        raise RuntimeError("reference_changed")
                    attempt_id, _ = await self._sync(
                        self.notebook.store.confirm_intake,
                        user_id=user_id,
                        intake_id=intake.intake_id,
                        expected_version=intake.input_version,
                        idempotency_key=f"harness-grade-{intake.intake_id}-{intake.input_version}",
                    )
                    final_answer = str(item["final_answer"]).strip()
                    validation = cross_validate_reference(reference, final_answer) if reference is not None and final_answer else None
                    if grading_strategy == "verified_reference" and (
                        not isinstance(validation, dict) or validation.get("status") != "consistent"
                    ):
                        raise RuntimeError("reference_changed")
                    verdict, first_error, evidence = self._grade_values(
                        item,
                        evidence_key="cause_evidence",
                        cross_validation=validation,
                    )
                    diagnosis = self._diagnosis(evidence)
                    diagnosis["grading_strategy"] = grading_strategy
                    evidence = json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":"))
                    context = exact_context or await self._sync(self.notebook.store.practice_review_context, user_id=user_id, attempt_id=attempt_id)
                    if not context or context.get("status") == "unmatched":
                        context = await self._sync(
                            self.notebook.resolve_practice_review, user_id=user_id, question_text=intake.question_text,
                            locator=exact_locator, review_mode=bool(context) or payload.get("review_mode", False),
                        )
                    if (not context or context.get("status") == "unmatched") and isinstance(validation, dict) and validation.get("status") == "consistent":
                        question_id = str(validation["question_id"])
                        owned = await self._sync(self.notebook.has_practice_review_identity, user_id=user_id, question_id=question_id)
                        if owned:
                            context = await self._sync(
                                self.notebook.resolve_practice_review, user_id=user_id, question_text=reference.stem_text,
                                locator={"question_id": question_id, "kind": "recommendation"}, review_mode=True,
                            )
                    if context:
                        if payload.get("correction_mode"):
                            context = dict(context) | {"correction": True}
                        diagnosis = self._diagnosis(evidence)
                        diagnosis["practice_review"] = context
                        evidence = json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":"))
                    candidate = await self._sync(
                        self.notebook.store.record_grade_candidate,
                        user_id=user_id,
                        attempt_id=attempt_id,
                        input_version=intake.input_version,
                        verdict=verdict,
                        first_error=first_error,
                        evidence=evidence,
                        confidence=float(item["confidence"]),
                    )
                    await self._sync(
                        self.notebook.store.finish_grade_usage,
                        user_id=user_id,
                        intake_id=intake.intake_id,
                        counted=candidate.verdict != "unclear" and not (context and context.get("required") is False),
                    )
                    completed_intakes.add(intake.intake_id)
                    linked_question_id = exact_question_id if reference is not None else ""
                    if not linked_question_id and isinstance(validation, dict) and validation.get("status") == "consistent":
                        linked_question_id = str(validation["question_id"])
                    if linked_question_id:
                        await self._sync(
                            self.notebook.store.link_attempt_question,
                            user_id=user_id,
                            attempt_id=attempt_id,
                            question_id=linked_question_id,
                        )
                receipt = await self._commit_candidate_receipt(user_id, candidate)
                candidate = await self._sync(self.notebook.store.get_grade_candidate, user_id=user_id, candidate_id=receipt["candidate_id"])
                diagnosis = self._diagnosis(candidate.evidence)
                review = diagnosis.get("practice_review") if isinstance(diagnosis.get("practice_review"), dict) else {}
                result_item = {
                    "item_no": intake.item_no,
                    "candidate_id": candidate.candidate_id,
                    "input_version": candidate.input_version,
                    "verdict": candidate.verdict,
                    "question_text": intake.question_text,
                    "answer_text": intake.answer_text,
                    "first_error": candidate.first_error or "",
                    "cause_code": str(diagnosis.get("cause_code") or ""),
                    "cause_evidence": str(diagnosis.get("cause_evidence") or ""),
                    "knowledge_points": diagnosis.get("knowledge_points") if isinstance(diagnosis.get("knowledge_points"), list) else [],
                    "correct_solution": str(diagnosis.get("correct_solution") or ""),
                    "final_answer": str(diagnosis.get("final_answer") or ""),
                    "prevention_cue": str(diagnosis.get("prevention_cue") or ""),
                    "receipt_status": receipt["status"],
                    "receipt_message": receipt["message"],
                    "error_id": str(receipt.get("error_id") or ""),
                    "review_association": {
                        "status": str(review.get("status") or "not_review"),
                        "pdf_id": str(review.get("job_id") or ""),
                        "review_code": str(review.get("code") or ""),
                        "error_id": str(review.get("error_id") or receipt.get("error_id") or ""),
                        "question_id": str(review.get("question_id") or ""),
                        "stage": int(review.get("stage") or 0),
                        "kind": str(review.get("kind") or ""),
                    },
                    "reference_review": None,
                }
                if reference is not None and receipt["reference_status"] == "conflict":
                    result_item["reference_review"] = {
                        "source_title": reference.source_title,
                        "version_no": reference.version_no,
                        "independent_answer": final_answer,
                        "reference_answer": reference.answer_text,
                        "reference_solution": reference.solution_text or "",
                    }
                results.append(result_item)
            # Multiple required questions can share one photo. Earlier items
            # must reflect a group completed by a later item in this same call.
            for result_item in results:
                if result_item["receipt_status"] != "review_waiting":
                    continue
                candidate = await self._sync(self.notebook.store.get_grade_candidate, user_id=user_id, candidate_id=result_item["candidate_id"])
                receipt = await self._commit_candidate_receipt(user_id, candidate)
                result_item.update(receipt_status=receipt["status"], receipt_message=receipt["message"], error_id=str(receipt.get("error_id") or ""))
            pending = {
                item["candidate_id"]: item["options"]
                for item in await self._sync(self.notebook.list_pending_practice_review_links, user_id=user_id)
            }
            for result_item in results:
                result_item["review_match_candidates"] = pending.get(result_item["candidate_id"], [])
            usage = await self._sync(self.notebook.store.learning_usage, user_id=user_id)
            await self._json(send, 200, {"results": results, "usage": usage})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {"input_version_changed", "reference_conflict", "reference_changed", "conflict", "daily_grade_limit"} else "conflict"
            await self._error(send, 429 if code == "daily_grade_limit" else 409, code)
        except RequestTooLarge:
            await self._error(send, 413, "request_too_large")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")
        finally:
            try:
                for intake_id in reserved_intakes if user_id is not None else []:
                    if intake_id not in completed_intakes:
                        await self._sync(self.notebook.store.finish_grade_usage, user_id=user_id, intake_id=intake_id, counted=False)
            finally:
                if process_claimed and process_key is not None:
                    with self._harness_processes_lock:
                        self._harness_processes.discard(process_key)

    async def _internal_harness_commit(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            candidate_id = str(scope.get("path", "")).split("/")[-2]
            if re.fullmatch(r"[0-9a-f]{32}", candidate_id) is None:
                raise ValueError("invalid candidate id")
            payload = await self._json_body(receive)
            if not {"session_id", "input_version"} <= set(payload) or set(payload) - {"session_id", "input_version", "review"}:
                raise ValueError("invalid receipt request")
            session_id = self._harness_session_id(payload)
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            candidate = await self._sync(
                self.notebook.store.get_grade_candidate,
                user_id=user_id,
                candidate_id=candidate_id,
            )
            if candidate is None:
                raise LookupError("grade candidate not found")
            if candidate.input_version != int(payload["input_version"]):
                raise RuntimeError("input_version_changed")
            candidate = await self._sync(self.notebook.prepare_review_candidate, user_id=user_id,
                candidate=candidate, locator=payload.get("review"))
            receipt = await self._commit_candidate_receipt(user_id, candidate)
            await self._json(send, 200, {"receipt": receipt})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {"input_version_changed", "reference_conflict"} else "conflict"
            await self._error(send, 409, code)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_adjudicate(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "items"}:
                raise ValueError("invalid reference adjudication request")
            session_id = self._harness_session_id(payload)
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            items = payload["items"]
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise ValueError("invalid reference adjudication items")
            results = []
            for item in items:
                if (
                    not isinstance(item, dict)
                    or not {"candidate_id", "input_version", "status", "rationale"} <= set(item)
                    or set(item) - {"candidate_id", "input_version", "status", "rationale", "authoritative_grade"}
                ):
                    raise ValueError("invalid reference adjudication item")
                candidate_id = item["candidate_id"]
                input_version = item["input_version"]
                status = item["status"]
                rationale = item["rationale"]
                if (
                    not isinstance(candidate_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", candidate_id) is None
                    or not isinstance(input_version, int)
                    or isinstance(input_version, bool)
                    or input_version < 1
                    or status not in {"consistent", "conflict", "uncertain"}
                    or not isinstance(rationale, str)
                    or not 20 <= len(rationale.strip()) <= 4000
                ):
                    raise ValueError("invalid reference adjudication item")
                candidate = await self._sync(
                    self.notebook.store.get_grade_candidate,
                    user_id=user_id,
                    candidate_id=candidate_id,
                )
                if candidate is None:
                    raise LookupError("grade candidate not found")
                if candidate.input_version != input_version:
                    raise RuntimeError("input_version_changed")
                diagnosis = self._diagnosis(candidate.evidence)
                validation = diagnosis.get("cross_validation")
                if not isinstance(validation, dict) or validation.get("status") != "conflict":
                    raise RuntimeError("reference_conflict")
                authoritative_grade = item.get("authoritative_grade")
                if status == "uncertain":
                    if authoritative_grade is not None:
                        raise ValueError("uncertain adjudication cannot replace grade")
                    results.append({
                        "candidate_id": candidate_id,
                        "input_version": input_version,
                        "status": "needs_review",
                        "receipt_message": "错题本记录检查：第二阶段复核仍无法确认两份答案是否一致，尚未计入错题本。",
                    })
                    continue
                if status == "consistent" and authoritative_grade is not None:
                    raise ValueError("consistent adjudication cannot replace grade")
                grade_keys = {
                    "verdict", "first_error", "cause_code", "cause_evidence",
                    "knowledge_points", "prevention_cue", "confidence",
                }
                if status == "conflict" and (
                    not isinstance(authoritative_grade, dict)
                    or set(authoritative_grade) != grade_keys
                ):
                    raise ValueError("authoritative grade is required for a reference conflict")
                attempt = await self._sync(
                    self.notebook.store.get_attempt,
                    user_id=user_id,
                    attempt_id=candidate.attempt_id,
                )
                reference = await self._sync(
                    self.notebook.store.find_verified_question,
                    question_text=attempt.question_text if attempt is not None else "",
                )
                fresh_validation = cross_validate_reference(reference, str(diagnosis.get("final_answer") or "")) if reference is not None else None
                if (
                    attempt is None
                    or fresh_validation is None
                    or fresh_validation.get("question_id") != validation.get("question_id")
                    or fresh_validation.get("version_id") != validation.get("version_id")
                    or fresh_validation.get("reference_answer_sha256") != validation.get("reference_answer_sha256")
                    or fresh_validation.get("independent_answer_sha256") != validation.get("independent_answer_sha256")
                ):
                    raise RuntimeError("reference_conflict")
                adjudication_status = "consistent"
                revised_verdict = candidate.verdict
                revised_first_error = candidate.first_error
                revised_evidence = diagnosis
                revised_confidence = None
                if status == "conflict":
                    assert isinstance(authoritative_grade, dict)
                    revised_confidence = authoritative_grade["confidence"]
                    if (
                        not isinstance(revised_confidence, (int, float))
                        or isinstance(revised_confidence, bool)
                        or not 0 <= float(revised_confidence) <= 1
                    ):
                        raise ValueError("invalid authoritative confidence")
                    revised_payload = dict(authoritative_grade)
                    revised_payload["correct_solution"] = reference.solution_text or reference.answer_text
                    revised_payload["final_answer"] = reference.answer_text
                    revised_verdict, revised_first_error, encoded = self._grade_values(
                        revised_payload,
                        evidence_key="cause_evidence",
                        cross_validation=validation,
                    )
                    revised_evidence = self._diagnosis(encoded)
                    if diagnosis.get("practice_review"):
                        revised_evidence["practice_review"] = diagnosis["practice_review"]
                    adjudication_status = "reference_preferred"
                revised_evidence["reference_adjudication"] = {
                    "schema": "question-bank-reference-adjudication/v1",
                    "status": adjudication_status,
                    "rationale": rationale.strip(),
                    "reference_answer_sha256": validation["reference_answer_sha256"],
                    "independent_answer_sha256": validation["independent_answer_sha256"],
                    "independent_verdict": candidate.verdict,
                    "independent_final_answer": str(diagnosis.get("final_answer") or ""),
                }
                revised = await self._sync(
                    self.notebook.store.record_grade_candidate,
                    user_id=user_id,
                    attempt_id=candidate.attempt_id,
                    input_version=candidate.input_version,
                    verdict=revised_verdict,
                    first_error=revised_first_error,
                    evidence=json.dumps(revised_evidence, ensure_ascii=False, separators=(",", ":")),
                    confidence=float(revised_confidence) if revised_confidence is not None else None,
                )
                await self._sync(
                    self.notebook.store.link_attempt_question,
                    user_id=user_id,
                    attempt_id=candidate.attempt_id,
                    question_id=str(validation["question_id"]),
                )
                review_context = revised_evidence.get("practice_review")
                if isinstance(review_context, dict) and review_context.get("status") == "unmatched":
                    receipt = self._grade_receipt(revised) | {
                        "candidate_id": revised.candidate_id,
                        "input_version": revised.input_version,
                        "status": "review_unmatched",
                        "review_status": "waiting_match",
                        "message": "题库答案语义复核已完成；对应 PDF 仍需从服务端候选中确认，尚未推进复习阶段，未重复入本。",
                    }
                else:
                    receipt = await self._commit_candidate_receipt(user_id, revised)
                results.append({
                    "candidate_id": receipt["candidate_id"],
                    "input_version": input_version,
                    "question_text": attempt.question_text,
                    "status": receipt["status"],
                    "receipt_message": receipt["message"],
                    "error_id": str(receipt.get("error_id") or ""),
                })
            pending = {
                item["candidate_id"]: item["options"]
                for item in await self._sync(self.notebook.list_pending_practice_review_links, user_id=user_id)
            }
            for result in results:
                result["review_match_candidates"] = pending.get(result["candidate_id"], [])
            await self._json(send, 200, {"results": results})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {"input_version_changed", "reference_conflict", "conflict"} else "conflict"
            await self._error(send, 409, code)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_recheck(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "question_text"}:
                raise ValueError("invalid reference recheck request")
            session_id = self._harness_session_id(payload)
            question_text = payload["question_text"]
            if not isinstance(question_text, str) or not 1 <= len(question_text.strip()) <= 200000:
                raise ValueError("invalid reference recheck request")
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            candidate = await self._sync(
                self.notebook.store.find_reference_conflict_candidate,
                user_id=user_id,
                question_text=question_text.strip(),
            )
            if candidate is None:
                raise LookupError("reference conflict not found")
            attempt = await self._sync(
                self.notebook.store.get_attempt,
                user_id=user_id,
                attempt_id=candidate.attempt_id,
            )
            diagnosis = self._diagnosis(candidate.evidence)
            final_answer = str(diagnosis.get("final_answer") or "").strip()
            reference = await self._sync(
                self.notebook.store.find_verified_question,
                question_text=attempt.question_text if attempt is not None else "",
            )
            if attempt is None or reference is None or not final_answer:
                raise RuntimeError("reference_conflict")
            validation = cross_validate_reference(reference, final_answer)
            diagnosis["cross_validation"] = validation
            diagnosis.pop("reference_adjudication", None)
            revised = await self._sync(
                self.notebook.store.record_grade_candidate,
                user_id=user_id,
                attempt_id=candidate.attempt_id,
                input_version=candidate.input_version,
                verdict=candidate.verdict,
                first_error=candidate.first_error,
                evidence=json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":")),
            )
            if validation["status"] == "consistent":
                await self._sync(
                    self.notebook.store.link_attempt_question,
                    user_id=user_id,
                    attempt_id=candidate.attempt_id,
                    question_id=str(validation["question_id"]),
                )
            receipt = await self._commit_candidate_receipt(user_id, revised)
            review = None
            if receipt["reference_status"] == "conflict":
                review = {
                    "source_title": reference.source_title,
                    "version_no": reference.version_no,
                    "independent_answer": final_answer,
                    "reference_answer": reference.answer_text,
                    "reference_solution": reference.solution_text or "",
                }
            await self._json(send, 200, {"result": {
                "candidate_id": receipt["candidate_id"],
                "input_version": revised.input_version,
                "question_text": attempt.question_text,
                "receipt_status": receipt["status"],
                "receipt_message": receipt["message"],
                "error_id": str(receipt.get("error_id") or ""),
                "reference_review": review,
            }})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {"input_version_changed", "reference_conflict", "conflict"} else "conflict"
            await self._error(send, 409, code)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_adjudicate_practice_review(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "items"}:
                raise ValueError("invalid practice review adjudication request")
            session_id = self._harness_session_id(payload)
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            items = payload["items"]
            if not isinstance(items, list) or not 1 <= len(items) <= 20:
                raise ValueError("invalid practice review adjudication items")
            if len({item.get("candidate_id") for item in items if isinstance(item, dict)}) != len(items):
                raise ValueError("duplicate practice review adjudication item")
            pending = {
                item["candidate_id"]: item
                for item in await self._sync(self.notebook.list_pending_practice_review_links, user_id=user_id)
            }
            validated = []
            for item in items:
                if not isinstance(item, dict) or set(item) != {"candidate_id", "input_version", "status", "code", "rationale"}:
                    raise ValueError("invalid practice review adjudication item")
                candidate_id = item["candidate_id"]
                input_version = item["input_version"]
                status = item["status"]
                code = item["code"]
                rationale = item["rationale"]
                if (
                    not isinstance(candidate_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", candidate_id) is None
                    or not isinstance(input_version, int)
                    or isinstance(input_version, bool)
                    or input_version < 1
                    or status not in {"matched", "uncertain"}
                    or not isinstance(code, str)
                    or len(code) > 180
                    or not isinstance(rationale, str)
                    or not 20 <= len(rationale.strip()) <= 4000
                    or (status == "matched" and re.fullmatch(r"R[0-9a-f]{12}-\d{2}(?:-[0-9A-F]{6})?", code) is None)
                    or (status == "uncertain" and code != "")
                ):
                    raise ValueError("invalid practice review adjudication item")
                candidate = await self._sync(
                    self.notebook.store.get_grade_candidate, user_id=user_id, candidate_id=candidate_id,
                )
                if candidate is None:
                    raise LookupError("grade candidate not found")
                if candidate.input_version != input_version:
                    raise RuntimeError("input_version_changed")
                current = pending.get(candidate_id)
                if current is None:
                    raise RuntimeError("conflict")
                if status == "matched" and not any(option["code"] == code for option in current["options"]):
                    raise ValueError("invalid practice review selection")
                validated.append((candidate_id, input_version, status, code))

            results = []
            for candidate_id, input_version, status, code in validated:
                if status == "uncertain":
                    results.append({
                        "candidate_id": candidate_id,
                        "input_version": input_version,
                        "status": "review_unmatched",
                        "receipt_message": "语义核对后仍无法唯一确认对应 PDF；判题结果已保留，未重复入本或推进阶段。",
                        "error_id": "",
                    })
                    continue
                linked = await self._sync(
                    self.notebook.link_pending_practice_review,
                    user_id=user_id,
                    candidate_id=candidate_id,
                    input_version=input_version,
                    code=code,
                )
                receipt = await self._commit_candidate_receipt(user_id, linked)
                results.append({
                    "candidate_id": receipt["candidate_id"],
                    "input_version": input_version,
                    "status": receipt["status"],
                    "receipt_message": receipt["message"],
                    "error_id": str(receipt.get("error_id") or ""),
                })
            await self._json(send, 200, {"results": results})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {"input_version_changed", "conflict"} else "conflict"
            await self._error(send, 409, code)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_question_reference(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "question_id"}:
                raise ValueError("invalid question reference request")
            session_id = self._harness_session_id(payload)
            question_id = payload["question_id"]
            if not isinstance(question_id, str) or re.fullmatch(r"[0-9a-f]{32}", question_id) is None:
                raise ValueError("invalid question reference request")
            if await self._harness_user_id(session_id) is None:
                raise PermissionError("unbound harness session")
            reference = await self._sync(
                self.notebook.store.get_verified_question,
                question_id=question_id,
            )
            if reference is None:
                raise LookupError("verified question not found")
            await self._json(send, 200, {"result": {
                "question_id": reference.question_id,
                "version_no": reference.version_no,
                "question_text": reference.stem_text,
                "reference_answer": reference.answer_text,
                "reference_solution": reference.solution_text or "",
                "source_title": reference.source_title,
            }})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_context(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if "session_id" not in payload or set(payload) - {"session_id", "error_id", "review_code"}:
                raise ValueError("invalid context request")
            session_id = self._harness_session_id(payload)
            error_id = payload.get("error_id")
            review_code = payload.get("review_code")
            if error_id is not None and (not isinstance(error_id, str) or re.fullmatch(r"[0-9a-f]{32}", error_id) is None):
                raise ValueError("invalid context request")
            if review_code is not None and (not isinstance(review_code, str) or re.fullmatch(r"R[0-9a-f]{12}-\d{2}(?:-[0-9A-F]{6})?", review_code, re.IGNORECASE) is None):
                raise ValueError("invalid context request")
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            errors = await self._sync(self.notebook.store.list_errors, user_id=user_id)
            exact_error = None
            if error_id is not None:
                entry = await self._sync(self.notebook.store.get_error, user_id=user_id, error_id=error_id)
                if entry is None:
                    raise LookupError("error not found")
                exact_error = self._error_entry(entry)
            due_tasks = await self._sync(self.notebook.store.list_due_reviews, user_id=user_id)
            due_reviews = []
            for task in due_tasks[:20]:
                entry = await self._sync(self.notebook.store.get_error, user_id=user_id, error_id=task.error_id)
                due_reviews.append(self._review(task) | {
                    "question_text": entry.question_text if entry else "",
                    "first_error": entry.first_error if entry else None,
                })
            active_tasks = await self._sync(self.notebook.store.list_active_reviews, user_id=user_id)
            active_reviews = []
            for task in active_tasks[:20]:
                entry = await self._sync(self.notebook.store.get_error, user_id=user_id, error_id=task.error_id)
                active_reviews.append(self._review(task) | {
                    "question_text": entry.question_text if entry else "",
                    "first_error": entry.first_error if entry else None,
                })
            papers = await self._sync(self.notebook.list_practice_pdfs, user_id=user_id)
            plan = await self._sync(self.notebook.today_practice_plan, user_id=user_id, papers=papers)
            pending = await self._sync(self.notebook.list_pending_practice_review_links, user_id=user_id)
            review_item = await self._sync(self.notebook.inspect_review_code, user_id=user_id, code=review_code) if review_code else None
            if review_code and review_item is None:
                raise LookupError("review code not found")
            context = {
                "scope": "current_bound_account",
                "progress": await self._sync(self.notebook.progress, user_id=user_id),
                "errors": [self._error_entry(item) for item in errors[:20]],
                "due_reviews": due_reviews,
                "active_reviews": active_reviews,
                "practice_pdfs": [item | {"download_url": f"/v1/practice-pdfs/{item['task_id']}/download"} for item in papers[:20]],
                "today_plan": plan,
                "pending_review_links": pending[:20],
                "error": exact_error,
                "review_item": review_item,
            }
            await self._json(send, 200, {"context_json": json.dumps(context, ensure_ascii=False, separators=(",", ":"))})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_retry_practice_review(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "review_code"}:
                raise ValueError("invalid review retry request")
            session_id = self._harness_session_id(payload)
            review_code = payload["review_code"]
            if not isinstance(review_code, str) or re.fullmatch(
                r"R[0-9a-f]{12}-\d{2}(?:-[0-9A-F]{6})?", review_code, re.IGNORECASE
            ) is None:
                raise ValueError("invalid review retry request")
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            state = await self._sync(self.notebook.inspect_review_code, user_id=user_id, code=review_code)
            if state is None:
                raise LookupError("review code not found")
            if state.get("recommended_action") != "retry_group_confirmation":
                raise RuntimeError("review_retry_not_needed")
            candidate = await self._sync(self.notebook.review_candidate_for_code, user_id=user_id, code=review_code)
            if candidate is None:
                raise LookupError("grade candidate not found")
            receipt = await self._commit_candidate_receipt(user_id, candidate)
            state = await self._sync(self.notebook.inspect_review_code, user_id=user_id, code=review_code)
            await self._json(send, 200, {
                "result_json": json.dumps({"receipt": receipt, "review_item": state}, ensure_ascii=False, separators=(",", ":"))
            })
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError:
            await self._error(send, 409, "review_retry_not_needed")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_defer_review(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "task_id", "days", "reason", "idempotency_key"}:
                raise ValueError("invalid review deferral")
            session_id = self._harness_session_id(payload)
            task_id = payload["task_id"]
            if not isinstance(task_id, str) or re.fullmatch(r"[0-9a-f]{32}", task_id) is None:
                raise ValueError("invalid review deferral")
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            task = await self._sync(self.notebook.store.defer_review, user_id=user_id, task_id=task_id,
                                    days=payload["days"], reason=payload["reason"], idempotency_key=payload["idempotency_key"])
            await self._json(send, 200, {"receipt": {"status": "deferred", "review": self._review(task),
                "message": "已标记为待补知识；延期期间不计完成、不推进阶段，到期后自动恢复为待复习。"}})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError:
            await self._error(send, 409, "conflict")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_resume_review(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "task_id", "idempotency_key"}:
                raise ValueError("invalid review resume")
            session_id = self._harness_session_id(payload)
            task_id = payload["task_id"]
            if not isinstance(task_id, str) or re.fullmatch(r"[0-9a-f]{32}", task_id) is None:
                raise ValueError("invalid review resume")
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            task = await self._sync(self.notebook.store.resume_review, user_id=user_id, task_id=task_id,
                                    idempotency_key=payload["idempotency_key"])
            await self._json(send, 200, {"receipt": {"status": "resumed", "review": self._review(task),
                "message": "已恢复学习，该题重新进入当前待复习任务。"}})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError:
            await self._error(send, 409, "conflict")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_reflow_practice_pdf(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id"}:
                raise ValueError("invalid PDF reflow request")
            session_id = self._harness_session_id(payload)
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            job_id = scope.get("path", "").split("/")[-2]
            if re.fullmatch(r"[0-9a-f]{32}", str(job_id)) is None:
                raise ValueError("invalid PDF reflow request")
            job = await self._sync(self.notebook.reflow_practice_pdf, user_id=user_id, job_id=job_id)
            record = await self._sync(self.notebook.store.get_file, user_id=user_id, file_id=str((job.checkpoint or {}).get("file_id", "")))
            if record is None:
                raise LookupError("practice PDF not found")
            await self._json(send, 200, {"result": {
                "task_id": job.job_id,
                "filename": record.original_name,
                "byte_size": record.byte_size,
                "generated_at": str((job.checkpoint or {}).get("generated_at", "")),
                "download_url": f"/v1/practice-pdfs/{job.job_id}/download",
                "message": "PDF 已按冻结题单重新排版；题目、推荐额度和判题进度均未改变。",
            }})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except RuntimeError as exc:
            code = str(exc) if str(exc) == "reflow_snapshot_unavailable" else "conflict"
            await self._error(send, 409, code)
        except OSError:
            await self._error(send, 503, "failed_retryable")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    async def _internal_harness_remove_error(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
        headers: dict[str, str],
    ) -> None:
        if not self._internal_harness_allowed(scope, headers):
            await self._error(send, 403, "forbidden")
            return
        try:
            payload = await self._json_body(receive)
            if set(payload) != {"session_id", "error_id", "confirmation_text"}:
                raise ValueError("invalid error removal request")
            session_id = self._harness_session_id(payload)
            error_id = payload["error_id"]
            confirmation_text = payload["confirmation_text"]
            if (
                not isinstance(error_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", error_id) is None
                or not isinstance(confirmation_text, str)
            ):
                raise ValueError("invalid error removal request")
            compact_confirmation = re.sub(r"\s+", "", confirmation_text).casefold()
            if "确认移除错题" not in compact_confirmation or error_id.casefold() not in compact_confirmation:
                raise PermissionError("explicit removal confirmation required")
            user_id = await self._harness_user_id(session_id)
            if user_id is None:
                raise PermissionError("unbound harness session")
            entry = await self._sync(
                self.notebook.store.set_error_status,
                user_id=user_id,
                error_id=error_id,
                status="removed",
            )
            await self._json(send, 200, {"receipt": {
                "schema": "math-notebook-removal-receipt/v1",
                "status": "removed",
                "error_id": entry.error_id,
                "message": "该题已从错题本移除，相关待复习任务和未使用推荐题已同步取消。",
            }})
        except LookupError:
            await self._error(send, 404, "not_found")
        except PermissionError:
            await self._error(send, 403, "forbidden")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

    def _internal_harness_allowed(self, scope: dict[str, Any], headers: dict[str, str]) -> bool:
        client = scope.get("client")
        client_host = str(client[0]) if isinstance(client, (tuple, list)) and client else ""
        supplied = headers.get("authorization", "")
        expected = f"Bearer {self.harness_internal_token}" if self.harness_internal_token else ""
        return client_host in {"127.0.0.1", "::1"} and bool(expected) and hmac.compare_digest(supplied, expected)

    async def _harness_user_id(self, session_id: str) -> str | None:
        with self._harness_sessions_lock:
            user_id = self._harness_sessions.get(session_id)
        if user_id is None:
            user_id = await self._sync(self.notebook.store.model_session_user, session_id=session_id)
            if user_id is not None:
                with self._harness_sessions_lock:
                    self._harness_sessions[session_id] = user_id
        return user_id

    async def _commit_candidate_receipt(self, user_id: str, candidate: GradeCandidate) -> dict[str, Any]:
        candidate = await self._sync(self.notebook.prepare_review_candidate, user_id=user_id, candidate=candidate)
        receipt = self._grade_receipt(candidate) | {"candidate_id": candidate.candidate_id, "input_version": candidate.input_version}
        adjudication = reference_adjudication_from_evidence(candidate.evidence)
        reference_preferred = bool(adjudication and adjudication.get("status") == "reference_preferred")
        reference_prefix = (
            f"题库第 {receipt['reference_version_no']} 版为已验证当前版本；"
            "第二阶段复核确认独立推导与题库不一致，已按题库答案与解析重新判题；"
            if receipt["reference_status"] == "consistent" and reference_preferred
            else ""
        )
        if receipt["reference_status"] == "consistent" and reference_preferred:
            receipt["message"] = reference_prefix + receipt["message"]
        if self._diagnosis(candidate.evidence).get("practice_review"):
            if receipt["reference_status"] == "conflict":
                receipt["message"] = "复习判题与题库参考答案正在等待语义复核，尚未推进复习阶段，未重复入本。"
                return receipt
            try:
                result = receipt | await self._sync(self.notebook.commit_practice_review, user_id=user_id, candidate=candidate)
                if reference_preferred:
                    result["message"] = reference_prefix + result["message"]
                return result
            except Exception:
                # The frozen grade already exists. Report a retryable receipt,
                # never claim success or ask the student to upload it again.
                result = receipt | {"status": "review_retryable", "review_status": "retryable",
                    "message": "复习判题结果已保留，但复习记录暂未写入。可直接在会话重试确认，无需重新上传图片。"}
                if reference_preferred:
                    result["message"] = reference_prefix + result["message"]
                return result
        if candidate.verdict not in {"partial", "incorrect"} or receipt["status"] != "pending":
            return receipt
        entry = await self._sync(
            self.notebook.store.commit_grade,
            user_id=user_id,
            candidate_id=candidate.candidate_id,
            expected_version=candidate.input_version,
        )
        already_saved = candidate.status == "committed"
        receipt |= {
            "status": "already_saved" if already_saved else "saved",
            "error_id": entry.error_id,
            "review_status": "scheduled",
        }
        receipt["message"] = (
            "错题本记录检查：该题已经在错题本中，无需重复保存。"
            if already_saved
            else f"错题本记录检查：已计入错题本，已保存 {receipt['knowledge_point_count']} 个知识点，并已安排首次复习。"
        )
        if receipt["reference_status"] == "consistent" and reference_preferred:
            receipt["message"] = reference_prefix + receipt["message"]
        elif receipt["reference_status"] == "consistent":
            prefix = (
                f"题库第 {receipt['reference_version_no']} 版答案与解析经第二阶段语义复核一致；"
                if adjudication and adjudication.get("status") == "consistent"
                else f"题库第 {receipt['reference_version_no']} 版参考答案确定性校验一致；"
            )
            receipt["message"] = prefix + receipt["message"]
        return receipt

    @staticmethod
    def _harness_session_id(payload: dict[str, Any]) -> str:
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", session_id) is None:
            raise ValueError("invalid harness session id")
        return session_id

    def _harness_attachment(self, attachment: Any) -> tuple[str, str, str, bytes, str]:
        if not isinstance(attachment, dict) or set(attachment) != {"attachment_id", "name", "media_type", "data"}:
            raise ValueError("invalid Harness attachment")
        if not all(isinstance(attachment.get(key), str) for key in ("attachment_id", "name", "media_type", "data")):
            raise ValueError("invalid Harness attachment")
        attachment_id = attachment["attachment_id"]
        digest_match = re.fullmatch(r"sha256:([0-9a-f]{64})", attachment_id)
        name = attachment["name"]
        media_type = attachment["media_type"]
        if digest_match is None or media_type not in {"image/png", "image/jpeg"} or not 1 <= len(name) <= 255:
            raise ValueError("invalid Harness attachment")
        try:
            content = base64.b64decode(attachment["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid Harness attachment") from exc
        digest = digest_match.group(1)
        if not content or len(content) > self.max_upload_bytes or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("invalid Harness attachment")
        return attachment_id, name, media_type, content, digest

    async def _websocket(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        connection = await receive()
        if connection.get("type") != "websocket.connect":
            return
        path = str(scope.get("path", ""))
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", ())
        }
        host = headers.get("host", "").split(":", 1)[0].lower()
        scheme = str(scope.get("scheme", "ws"))
        if host not in self.allowed_hosts:
            await send({"type": "websocket.close", "code": 1008, "reason": "invalid host"})
            return
        if self.require_https and scheme != "wss":
            await send({"type": "websocket.close", "code": 1008, "reason": "secure transport required"})
            return
        browser_scheme = "https" if scheme == "wss" else "http"
        if headers.get("origin") != f"{browser_scheme}://{headers.get('host', '')}":
            await send({"type": "websocket.close", "code": 1008, "reason": "forbidden origin"})
            return
        token = self._cookie(headers.get("cookie", ""), self.session_cookie)
        user = await asyncio.to_thread(self.auth_service.authenticate_session, token or "")
        if user is None:
            await send({"type": "websocket.close", "code": 4401, "reason": "authentication required"})
            return
        if self.harness_gateway is None:
            await send({"type": "websocket.close", "code": 1013, "reason": "Harness unavailable"})
            return
        await self.harness_gateway.websocket(scope, receive, send, path)

    @staticmethod
    def _is_harness_http_path(path: str) -> bool:
        if path in {"/", "/harness", "/harness/", "/api", "/favicon.svg"}:
            return True
        if path.startswith(("/harness/", "/api/", "/plugins/")):
            return True
        return path.startswith("/assets/") and not path.startswith("/assets/branding/")

    @staticmethod
    def _harness_target(path: str) -> str:
        if path in {"/harness", "/harness/"}:
            return "/"
        if path.startswith("/harness/"):
            return path[len("/harness"):]
        return path

    @staticmethod
    def _grade_receipt(candidate: GradeCandidate) -> dict[str, Any]:
        diagnosis = NotebookAsgiApp._diagnosis(candidate.evidence)
        points = diagnosis.get("knowledge_points")
        count = len(points) if isinstance(points, list) else 0
        validation = diagnosis.get("cross_validation")
        reference = {"reference_status": "not_found"}
        resolved = reference_conflict_resolved(candidate.evidence)
        if isinstance(validation, dict):
            reference = {
                "reference_status": "consistent" if resolved else str(validation["status"]),
                "reference_question_id": str(validation["question_id"]),
                "reference_version_no": int(validation["version_no"]),
            }
        if reference["reference_status"] == "conflict":
            return {
                "schema": "math-notebook-entry-receipt/v1",
                "status": "needs_review",
                "knowledge_point_count": count,
                "review_status": "not_scheduled",
                "message": "错题本记录检查：独立解答与题库参考答案未通过确定性一致性校验，正在等待第二阶段语义复核。",
            } | reference
        if candidate.verdict == "correct":
            return {
                "schema": "math-notebook-entry-receipt/v1",
                "status": "not_saved_correct",
                "knowledge_point_count": count,
                "review_status": "not_scheduled",
                "message": "错题本记录检查：本题判定正确，未计入错题本。",
            } | reference
        if candidate.verdict == "unclear":
            return {
                "schema": "math-notebook-entry-receipt/v1",
                "status": "needs_review",
                "knowledge_point_count": count,
                "review_status": "not_scheduled",
                "message": "错题本记录检查：证据不足，尚未计入错题本，请补充或修正后重试。",
            } | reference
        return {
            "schema": "math-notebook-entry-receipt/v1",
            "status": "pending",
            "knowledge_point_count": count,
            "review_status": "not_scheduled",
            "message": "错题本记录检查：正在确认入本结果。",
        } | reference

    @staticmethod
    def _grade_values(
        payload: dict[str, Any],
        *,
        evidence_key: str,
        cross_validation: dict[str, Any] | None = None,
    ) -> tuple[str, str | None, str]:
        verdict = str(payload["verdict"])
        first_error = str(payload.get("first_error") or "").strip() or None
        if verdict not in {"correct", "partial", "incorrect", "unclear"}:
            raise ValueError("unsupported verdict")
        if verdict in {"correct", "unclear"}:
            first_error = None
        cause_code = str(payload.get("cause_code") or "").strip()
        cause_evidence = str(payload.get(evidence_key) or "").strip()
        raw_knowledge_points = payload.get("knowledge_points", [])
        if not isinstance(raw_knowledge_points, list) or len(raw_knowledge_points) > 8:
            raise ValueError("invalid knowledge points")
        knowledge_points = []
        for item in raw_knowledge_points:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 200:
                raise ValueError("invalid knowledge points")
            knowledge_points.append(item.strip())
        correct_solution = str(payload.get("correct_solution") or "").strip()
        final_answer = str(payload.get("final_answer") or "").strip()
        prevention_cue = str(payload.get("prevention_cue") or "").strip()
        allowed_causes = {"knowledge_gap", "concept_confusion", "formula_condition", "method_choice", "reasoning_gap", "algebra_transform", "calculation", "misreading", "incomplete_cases", "expression", "careless", "unclear"}
        if any(len(value) > 12000 for value in (first_error or "", cause_evidence, correct_solution, final_answer, prevention_cue)):
            raise ValueError("grade field is too long")
        if verdict in {"partial", "incorrect"} and (not first_error or cause_code not in allowed_causes or not cause_evidence or not knowledge_points or not correct_solution or not final_answer):
            raise ValueError("complete diagnosis is required")
        if cause_code == "careless" and not cause_evidence:
            raise ValueError("careless requires direct evidence")
        diagnosis: dict[str, Any] = {"schema": "math-error-diagnosis/v1", "cause_code": cause_code or None, "cause_evidence": cause_evidence or None, "knowledge_points": knowledge_points, "correct_solution": correct_solution or None, "final_answer": final_answer or None, "prevention_cue": prevention_cue or None}
        if cross_validation is not None:
            expected = {
                "schema", "status", "question_id", "version_id", "version_no", "source_title",
                "match_score", "reference_answer_sha256", "independent_answer_sha256",
            }
            if set(cross_validation) != expected or cross_validation.get("schema") != "question-bank-cross-validation/v1":
                raise ValueError("invalid cross validation")
            if cross_validation.get("status") not in {"consistent", "conflict"}:
                raise ValueError("invalid cross validation")
            if any(re.fullmatch(r"[0-9a-f]{32}", str(cross_validation.get(key, ""))) is None for key in ("question_id", "version_id")):
                raise ValueError("invalid cross validation")
            version_no, score = cross_validation.get("version_no"), cross_validation.get("match_score")
            if not isinstance(version_no, int) or isinstance(version_no, bool) or version_no < 1:
                raise ValueError("invalid cross validation")
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.92 <= float(score) <= 1:
                raise ValueError("invalid cross validation")
            if not isinstance(cross_validation.get("source_title"), str) or not 1 <= len(cross_validation["source_title"]) <= 255:
                raise ValueError("invalid cross validation")
            if any(re.fullmatch(r"[0-9a-f]{64}", str(cross_validation.get(key, ""))) is None for key in ("reference_answer_sha256", "independent_answer_sha256")):
                raise ValueError("invalid cross validation")
            diagnosis["cross_validation"] = dict(cross_validation)
        evidence = json.dumps(diagnosis, ensure_ascii=False, separators=(",", ":"))
        return verdict, first_error, evidence

    @staticmethod
    def _recommendation(value: Recommendation) -> dict[str, Any]:
        return {"recommendation_id": value.recommendation_id, "question_id": value.question.question_id, "stem_text": value.question.stem_text, "grade": value.question.grade, "difficulty": value.question.difficulty, "source": value.question.source_title, "reason": value.reason, "status": value.status, "options": list(value.question.options) if value.question.options else None}

    @staticmethod
    def _review(value: ReviewTask) -> dict[str, Any]:
        deferred = value.deferred_from is not None and value.due_at > datetime.now(timezone.utc)
        return {"review_id": value.task_id, "error_id": value.error_id, "stage": value.stage, "due_at": value.due_at.isoformat(), "status": value.status,
                "deferred": deferred, "original_due_at": value.deferred_from.isoformat() if value.deferred_from else None,
                "defer_reason": value.defer_reason}

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
        await self._json(send, status, {"error": {"code": code, "message": code, "retryable": code in {"failed_retryable", "temporarily_unavailable", "model_unavailable", "model_network_error", "model_rate_limited", "rate_limited", "batch_limit_reached", "batch_rate_limited"}, "request_id": secrets.token_hex(8)}})

    @staticmethod
    async def _redirect(send: Send, location: str) -> None:
        await send({
            "type": "http.response.start",
            "status": 303,
            "headers": [
                (b"location", location.encode("ascii")),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _json(
        send: Send,
        status: int,
        payload: dict[str, Any],
        *,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response_headers = [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
        response_headers.extend(extra_headers or ())
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _bytes(send: Send, status: int, body: bytes, media_type: str, filename: str) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", media_type.encode("ascii")), (b"content-length", str(len(body)).encode("ascii")), (b"content-disposition", f'attachment; filename="{filename}"'.encode("ascii")), (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _inline_bytes(send: Send, status: int, body: bytes, media_type: str) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", media_type.encode("ascii")), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"private,no-store"), (b"x-content-type-options", b"nosniff")]})
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
