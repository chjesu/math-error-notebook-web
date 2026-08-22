"""Small ASGI composition for auth and the first-error notebook slice."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
import json
import secrets
from typing import Any, Awaitable, Callable

from services.web_auth import AuthAsgiApp, RegistrationService
from services.web_domain import ErrorEntry, GradeCandidate, IntakeItem, Job, NotebookService


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
        token = self._cookie(headers.get("cookie", ""), self.session_cookie)
        user = self.auth_service.authenticate_session(token or "")
        if user is None:
            await self._error(send, 401, "authentication_required")
            return
        method = str(scope.get("method", ""))
        if method in {"POST", "PATCH", "PUT", "DELETE"}:
            expected_origin = f"{scope.get('scheme', 'https')}://{headers.get('host', '')}"
            if headers.get("origin") != expected_origin:
                await self._error(send, 403, "forbidden")
                return
        try:
            if path == "/v1/workbench" and method == "GET":
                items = self.notebook.store.list_errors(user_id=user.user_id)
                pending = sum(job.user_id == user.user_id and job.status not in {"completed", "cancelled", "failed_final"} for job in self.notebook.store.jobs.values()) if hasattr(self.notebook.store, "jobs") else 0
                await self._json(send, 200, {"error_count": len(items), "pending_task_count": pending, "recent_errors": [self._error_entry(item) for item in items[:5]]})
            elif path == "/v1/files" and method == "POST":
                purpose, filename, content = await self._multipart(receive, headers)
                record = self.notebook.upload(user_id=user.user_id, purpose=purpose, original_name=filename, content=content)
                await self._json(send, 201, {"file_id": record.file_id, "status": record.status, "content_sha256": record.content_sha256})
            elif path == "/v1/intakes" and method == "POST":
                payload = await self._json_body(receive)
                intake, job = self.notebook.store.create_intake(user_id=user.user_id, file_id=str(payload["file_id"]), idempotency_key=self._key(headers))
                await self._json(send, 202, {"resource_id": intake.intake_id, "task_id": job.job_id})
            elif path.startswith("/v1/tasks/") and method == "GET":
                job = self.notebook.store.get_job(user_id=user.user_id, job_id=path.rsplit("/", 1)[1])
                if not job:
                    raise LookupError
                await self._json(send, 200, self._job(job))
            elif path.startswith("/v1/intakes/") and path.endswith("/confirm") and method == "POST":
                intake_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                attempt_id, job = self.notebook.store.confirm_intake(user_id=user.user_id, intake_id=intake_id, expected_version=int(payload["input_version"]), idempotency_key=self._key(headers))
                await self._json(send, 202, {"resource_id": attempt_id, "task_id": job.job_id})
            elif path.startswith("/v1/intakes/") and method == "PATCH":
                intake_id = path.rsplit("/", 1)[1]
                payload = await self._json_body(receive)
                intake = self.notebook.store.revise_intake(user_id=user.user_id, intake_id=intake_id, expected_version=int(payload["input_version"]), question_text=str(payload["question_text"]), answer_text=str(payload["answer_text"]))
                await self._json(send, 200, self._intake(intake))
            elif path.startswith("/v1/grade-results/") and path.endswith("/commit") and method == "POST":
                candidate_id = path.split("/")[-2]
                payload = await self._json_body(receive)
                entry = self.notebook.store.commit_grade(user_id=user.user_id, candidate_id=candidate_id, expected_version=int(payload["input_version"]))
                await self._json(send, 201, self._error_entry(entry))
            elif path.startswith("/v1/grade-results/") and method == "GET":
                candidate = self.notebook.store.get_grade_candidate(user_id=user.user_id, candidate_id=path.rsplit("/", 1)[1])
                if not candidate:
                    raise LookupError
                await self._json(send, 200, self._candidate(candidate))
            elif path == "/v1/errors" and method == "GET":
                await self._json(send, 200, {"items": [self._error_entry(item) for item in self.notebook.store.list_errors(user_id=user.user_id)]})
            elif path.startswith("/v1/errors/") and method == "GET":
                entry = self.notebook.store.get_error(user_id=user.user_id, error_id=path.rsplit("/", 1)[1])
                if not entry:
                    raise LookupError
                await self._json(send, 200, self._error_entry(entry))
            else:
                await self._error(send, 404, "not_found")
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
            body.extend(message.get("body", b""))
            if len(body) > self.max_upload_bytes:
                raise ValueError("request_too_large")
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
        return purpose, filename, content

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

    async def _error(self, send: Send, status: int, code: str) -> None:
        await self._json(send, status, {"error": {"code": code, "message": code, "retryable": code in {"failed_retryable", "temporarily_unavailable", "rate_limited"}, "request_id": secrets.token_hex(8)}})

    @staticmethod
    async def _json(send: Send, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode("ascii")), (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})
