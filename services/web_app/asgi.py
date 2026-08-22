"""Small ASGI composition for auth and the first-error notebook slice."""

from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import secrets
from typing import Any, Awaitable, Callable

from services.web_auth import AuthAsgiApp, RegistrationService
from services.web_domain import ErrorEntry, GradeCandidate, IntakeItem, Job, NotebookService, Recommendation, ReviewTask


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
    ) -> None:
        self.auth = AuthAsgiApp(auth_service, allowed_hosts=allowed_hosts, require_https=require_https, session_cookie=session_cookie)
        self.auth_service = auth_service
        self.notebook = notebook
        self.allowed_hosts = {item.lower() for item in allowed_hosts}
        self.require_https = require_https
        self.session_cookie = session_cookie
        self.max_upload_bytes = max_upload_bytes
        root = Path(__file__).resolve().parents[2]
        self.static_files = {
            "/": (root / "web" / "index.html", "text/html; charset=utf-8", False),
            "/web/app.css": (root / "web" / "app.css", "text/css; charset=utf-8", False),
            "/web/app.js": (root / "web" / "app.js", "text/javascript; charset=utf-8", False),
            "/assets/branding/favicon-v1.ico": (root / "assets" / "branding" / "favicon-v1.ico", "image/x-icon", True),
            "/assets/branding/logo-symbol-color-64-v1.png": (root / "assets" / "branding" / "logo-symbol-color-64-v1.png", "image/png", True),
            "/assets/branding/logo-symbol-color-128-v1.png": (root / "assets" / "branding" / "logo-symbol-color-128-v1.png", "image/png", True),
        }

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
            if path == "/v1/workbench" and method == "GET":
                items = await self._sync(self.notebook.store.list_errors, user_id=user.user_id)
                pending = await self._sync(self.notebook.store.pending_job_count, user_id=user.user_id)
                progress = await self._sync(self.notebook.store.progress, user_id=user.user_id)
                await self._json(send, 200, {"error_count": len(items), "pending_task_count": pending, "due_review_count": progress["due_review_count"], "recommendation_gap_count": progress["recommendation_gap_count"], "recent_errors": [self._error_entry(item) for item in items[:5]]})
            elif path == "/v1/files" and method == "POST":
                purpose, filename, content = await self._multipart(receive, headers)
                record = await self._sync(self.notebook.upload, user_id=user.user_id, purpose=purpose, original_name=filename, content=content)
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
                verdict = str(payload["verdict"])
                first_error = str(payload.get("first_error", "")).strip() or None
                evidence = str(payload.get("evidence", "")).strip() or None
                if verdict not in {"correct", "partial", "incorrect", "unclear"}:
                    raise ValueError("unsupported verdict")
                if verdict in {"partial", "incorrect"} and not first_error:
                    raise ValueError("first_error is required")
                if verdict in {"correct", "unclear"}:
                    first_error = None
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
            elif path.startswith("/v1/errors/") and path.endswith("/recommendations") and method in {"GET", "POST"}:
                error_id = path.split("/")[-2]
                if method == "POST":
                    self._key(headers)
                    recommendations, gap = await self._sync(self.notebook.store.assign_recommendations, user_id=user.user_id, error_id=error_id)
                else:
                    recommendations = await self._sync(self.notebook.store.list_recommendations, user_id=user.user_id, error_id=error_id)
                    gap = len(recommendations) < 2
                await self._json(send, 200, {"items": [self._recommendation(item) for item in recommendations], "gap": gap})
            elif path.startswith("/v1/errors/") and method == "GET":
                entry = await self._sync(self.notebook.store.get_error, user_id=user.user_id, error_id=path.rsplit("/", 1)[1])
                if not entry:
                    raise LookupError
                await self._json(send, 200, self._error_entry(entry))
            elif path == "/v1/reviews/today" and method == "GET":
                tasks = await self._sync(self.notebook.store.list_due_reviews, user_id=user.user_id)
                await self._json(send, 200, {"items": [self._review(item) for item in tasks], "count": len(tasks)})
            elif path.startswith("/v1/reviews/") and path.endswith("/complete") and method == "POST":
                payload = await self._json_body(receive)
                next_task = await self._sync(self.notebook.store.complete_review, user_id=user.user_id, task_id=path.split("/")[-2], result=str(payload["result"]), idempotency_key=self._key(headers))
                await self._json(send, 200, {"completed": True, "next_review": self._review(next_task) if next_task else None, "mastered": next_task is None})
            elif path == "/v1/progress" and method == "GET":
                progress = await self._sync(self.notebook.store.progress, user_id=user.user_id)
                await self._json(send, 200, progress)
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
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {"input_version_changed", "waiting_confirmation", "failed_final", "conflict"} else "conflict"
            await self._error(send, 422 if code == "failed_final" else 409, code)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._error(send, 400, "invalid_request")

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
        return {"intake_id": value.intake_id, "input_version": value.input_version, "status": value.status, "question_text": value.question_text, "answer_text": value.answer_text}

    @staticmethod
    def _candidate(value: GradeCandidate) -> dict[str, Any]:
        return {"result_id": value.candidate_id, "input_version": value.input_version, "verdict": value.verdict, "status": value.status, "first_error": value.first_error, "evidence": value.evidence}

    @staticmethod
    def _error_entry(value: ErrorEntry) -> dict[str, Any]:
        return {"error_id": value.error_id, "status": value.status, "question_text": value.question_text, "answer_text": value.answer_text, "first_error": value.first_error, "created_at": value.created_at.isoformat()}

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

    async def _error(self, send: Send, status: int, code: str) -> None:
        await self._json(send, status, {"error": {"code": code, "message": code, "retryable": code in {"failed_retryable", "temporarily_unavailable", "rate_limited"}, "request_id": secrets.token_hex(8)}})

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


class RequestTooLarge(ValueError):
    pass
