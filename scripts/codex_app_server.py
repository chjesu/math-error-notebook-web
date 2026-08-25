"""Minimal synchronous client for the official Codex app-server protocol."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import tempfile
from threading import Event, Thread
import time
from typing import Any


EventCallback = Callable[[dict[str, Any]], None]


class AppServerError(RuntimeError):
    """A protocol or turn failure without prompt or stderr disclosure."""

    def __init__(self, message: str, *, category: str = "cli_error", turn_started: bool = False) -> None:
        self.category = category
        self.turn_started = turn_started
        self.retryable = category in {"network", "timeout", "rate_limit"} and not turn_started
        self.public_code = {
            "certificate": "model_network_error", "network": "model_network_error",
            "timeout": "model_network_error", "rate_limit": "model_rate_limited",
            "authentication": "model_authentication_error",
            "interrupted": "model_interrupted",
        }.get(category, "model_unavailable")
        super().__init__(message)


def _classify_error(diagnostic: str, *, timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    value = diagnostic.casefold()
    if any(marker in value for marker in ("unknownissuer", "invalid peer certificate", "certificate verify")):
        return "certificate"
    if any(marker in value for marker in ("unauthorized", "not logged in", "authentication failed")):
        return "authentication"
    if any(marker in value for marker in ("rate limit", "too many requests", "status 429")):
        return "rate_limit"
    if any(marker in value for marker in ("timed out", "timeout", "stream disconnected")):
        return "timeout"
    if any(marker in value for marker in ("failed to connect", "error sending request", "connection reset", "connection refused", "dns error", "network is unreachable")):
        return "network"
    return "cli_error"


def _put_lines(stream: Any, target: Queue[str | None]) -> None:
    try:
        for line in stream:
            target.put(line)
    finally:
        target.put(None)


def _drain_stderr(stream: Any, target: list[str]) -> None:
    for line in stream:
        target.append(line[-1000:])
        del target[:-8]


def _safe_event(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return None
    if method == "item/agentMessage/delta":
        return {"type": "agent_message_delta", "delta": str(params.get("delta", ""))}
    if method in {"item/started", "item/completed"}:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        return {"type": method.replace("/", "_"), "item_type": item.get("type")}
    if method in {"turn/started", "turn/completed"}:
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        return {"type": method.replace("/", "_"), "status": turn.get("status")}
    if method == "thread/compacted":
        return {"type": "thread_compacted"}
    if method == "thread/tokenUsage/updated":
        usage = params.get("tokenUsage") if isinstance(params.get("tokenUsage"), dict) else {}
        return {"type": "token_usage", "total_tokens": usage.get("totalTokens")}
    if method in {"error", "warning"}:
        return {"type": method, "message": "Codex 会话运行时报告了问题。"}
    return None


def run_turn(
    *,
    route: dict[str, Any],
    prompt: str,
    output_path: Path,
    images: list[Path] | None = None,
    thread_id: str | None = None,
    event_callback: EventCallback | None = None,
    cancel_event: Event | None = None,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    """Run one structured turn and return the durable Codex thread id."""
    executable = shutil.which("codex")
    if not executable:
        raise AppServerError("codex CLI is not installed or not on PATH")
    schema = json.loads(Path(route["schema"]).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="web-codex-app-server-", ignore_cleanup_errors=True) as isolated:
        process = subprocess.Popen(
            [executable, "app-server", "-c", "mcp_servers={}", "-c", "features.shell_tool=false"],
            cwd=isolated,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=_codex_environment(),
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise AppServerError("codex app-server did not expose stdio")
        messages: Queue[str | None] = Queue()
        stderr_tail: list[str] = []
        Thread(target=_put_lines, args=(process.stdout, messages), daemon=True).start()
        Thread(target=_drain_stderr, args=(process.stderr, stderr_tail), daemon=True).start()

        def send(message: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()

        send({
            "method": "initialize", "id": 0,
            "params": {"clientInfo": {"name": "lzl_math_error_notebook", "title": "李兆霖数学错题本", "version": "0.5.0"}},
        })
        send({"method": "initialized", "params": {}})
        thread_method = "thread/resume" if thread_id else "thread/start"
        thread_params: dict[str, Any] = {
            "model": route["model"], "approvalPolicy": "never", "sandbox": "read-only", "cwd": isolated,
        }
        if thread_id:
            thread_params["threadId"] = thread_id
        else:
            thread_params["ephemeral"] = False
        send({"method": thread_method, "id": 1, "params": thread_params})

        resolved_thread = thread_id
        turn_started = False
        resolved_turn: str | None = None
        interrupt_sent = False
        final_text = ""
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError("codex app-server turn timed out", category="timeout", turn_started=turn_started)
                try:
                    line = messages.get(timeout=min(remaining, 1.0))
                except Empty:
                    if process.poll() is not None:
                        raise AppServerError(
                            "codex app-server exited before completing the turn",
                            category=_classify_error("".join(stderr_tail)), turn_started=turn_started,
                        )
                    continue
                if line is None:
                    raise AppServerError(
                        "codex app-server closed its output",
                        category=_classify_error("".join(stderr_tail)), turn_started=turn_started,
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    send({"id": message["id"], "result": {"decision": "decline"}})
                    continue
                if message.get("id") in {0, 1, 2, 3} and message.get("error"):
                    error = message.get("error") if isinstance(message.get("error"), dict) else {}
                    raise AppServerError(
                        "codex app-server rejected a protocol request",
                        category=_classify_error(str(error.get("message", ""))), turn_started=turn_started,
                    )
                if message.get("id") == 1:
                    result = message.get("result") if isinstance(message.get("result"), dict) else {}
                    thread = result.get("thread") if isinstance(result.get("thread"), dict) else {}
                    resolved_thread = thread.get("id") or resolved_thread
                    if not isinstance(resolved_thread, str) or not resolved_thread:
                        raise AppServerError("codex app-server omitted the thread id")
                    turn_input: list[dict[str, str]] = [{"type": "text", "text": prompt}]
                    turn_input.extend({"type": "localImage", "path": str(image)} for image in images or [])
                    send({
                        "method": "turn/start", "id": 2,
                        "params": {
                            "threadId": resolved_thread,
                            "input": turn_input,
                            "model": route["model"], "effort": route["reasoning_effort"],
                            "approvalPolicy": "never", "outputSchema": schema,
                        },
                    })
                    continue
                if message.get("id") == 2:
                    turn_started = True
                    result = message.get("result") if isinstance(message.get("result"), dict) else {}
                    turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
                    resolved_turn = turn.get("id") if isinstance(turn.get("id"), str) else resolved_turn
                    if cancel_event is not None and cancel_event.is_set() and resolved_turn and not interrupt_sent:
                        send({"method": "turn/interrupt", "id": 3, "params": {"threadId": resolved_thread, "turnId": resolved_turn}})
                        interrupt_sent = True
                    continue
                if message.get("method") == "turn/started":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    resolved_turn = turn.get("id") if isinstance(turn.get("id"), str) else resolved_turn
                if cancel_event is not None and cancel_event.is_set() and resolved_turn and not interrupt_sent:
                    send({"method": "turn/interrupt", "id": 3, "params": {"threadId": resolved_thread, "turnId": resolved_turn}})
                    interrupt_sent = True
                event = _safe_event(message)
                if event and event_callback:
                    try:
                        event_callback(event)
                    except Exception:
                        pass
                if message.get("method") == "item/completed":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                        final_text = item["text"]
                if message.get("method") == "turn/completed":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    if turn.get("status") != "completed":
                        if interrupt_sent or (cancel_event is not None and cancel_event.is_set()):
                            raise AppServerError(
                                "codex app-server turn was interrupted",
                                category="interrupted", turn_started=turn_started,
                            )
                        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                        raise AppServerError(
                            "codex app-server turn did not complete",
                            category=_classify_error(str(error.get("message", ""))), turn_started=turn_started,
                        )
                    if not turn_started or not final_text:
                        raise AppServerError("codex app-server omitted the final structured message")
                    parsed = json.loads(final_text)
                    output_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
                    return {"thread_id": resolved_thread, "result": parsed}
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()


def list_thread_items(
    *,
    thread_id: str,
    limit: int = 200,
    sort_direction: str = "desc",
    cursor: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Read persisted thread items without exposing the thread to the browser."""
    if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 128:
        raise ValueError("invalid Codex thread id")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("invalid Codex history limit")
    if sort_direction not in {"asc", "desc"}:
        raise ValueError("invalid Codex history sort direction")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048):
        raise ValueError("invalid Codex history cursor")
    executable = shutil.which("codex")
    if not executable:
        raise AppServerError("codex CLI is not installed or not on PATH")
    with tempfile.TemporaryDirectory(prefix="web-codex-app-server-", ignore_cleanup_errors=True) as isolated:
        process = subprocess.Popen(
            [executable, "app-server", "-c", "mcp_servers={}", "-c", "features.shell_tool=false"],
            cwd=isolated,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=_codex_environment(),
            bufsize=1,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise AppServerError("codex app-server did not expose stdio")
        messages: Queue[str | None] = Queue()
        stderr_tail: list[str] = []
        Thread(target=_put_lines, args=(process.stdout, messages), daemon=True).start()
        Thread(target=_drain_stderr, args=(process.stderr, stderr_tail), daemon=True).start()

        def send(message: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()

        send({
            "method": "initialize", "id": 0,
            "params": {
                "clientInfo": {"name": "lzl_math_error_notebook", "title": "李兆霖数学错题本", "version": "0.5.0"},
                "capabilities": {"experimentalApi": True},
            },
        })
        send({"method": "initialized", "params": {}})
        params: dict[str, Any] = {
            "threadId": thread_id, "limit": limit, "sortDirection": sort_direction,
        }
        if cursor is not None:
            params["cursor"] = cursor
        send({"method": "thread/items/list", "id": 1, "params": params})
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError("codex app-server history request timed out", category="timeout")
                try:
                    line = messages.get(timeout=min(remaining, 1.0))
                except Empty:
                    if process.poll() is not None:
                        raise AppServerError(
                            "codex app-server exited before returning history",
                            category=_classify_error("".join(stderr_tail)),
                        )
                    continue
                if line is None:
                    raise AppServerError(
                        "codex app-server closed its output",
                        category=_classify_error("".join(stderr_tail)),
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    send({"id": message["id"], "result": {"decision": "decline"}})
                    continue
                if message.get("id") == 1:
                    if message.get("error"):
                        error = message.get("error") if isinstance(message.get("error"), dict) else {}
                        raise AppServerError(
                            "codex app-server rejected the history request",
                            category=_classify_error(str(error.get("message", ""))),
                        )
                    result = message.get("result") if isinstance(message.get("result"), dict) else {}
                    entries = result.get("data") if isinstance(result.get("data"), list) else []
                    entries = [entry for entry in entries if isinstance(entry, dict)]
                    next_cursor = result.get("nextCursor")
                    return {
                        "items": entries,
                        "next_cursor": next_cursor if isinstance(next_cursor, str) and next_cursor else None,
                    }
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()


def compact_thread(*, thread_id: str, timeout_seconds: float = 180.0) -> dict[str, Any]:
    """Ask the official app-server to compact one persisted thread."""
    if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 128:
        raise ValueError("invalid Codex thread id")
    executable = shutil.which("codex")
    if not executable:
        raise AppServerError("codex CLI is not installed or not on PATH")
    with tempfile.TemporaryDirectory(prefix="web-codex-app-server-", ignore_cleanup_errors=True) as isolated:
        process = subprocess.Popen(
            [executable, "app-server", "-c", "mcp_servers={}", "-c", "features.shell_tool=false"],
            cwd=isolated, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=_codex_environment(), bufsize=1,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise AppServerError("codex app-server did not expose stdio")
        messages: Queue[str | None] = Queue()
        stderr_tail: list[str] = []
        Thread(target=_put_lines, args=(process.stdout, messages), daemon=True).start()
        Thread(target=_drain_stderr, args=(process.stderr, stderr_tail), daemon=True).start()

        def send(message: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()

        send({"method": "initialize", "id": 0, "params": {
            "clientInfo": {"name": "lzl_math_error_notebook", "title": "李兆霖数学错题本", "version": "0.5.0"},
        }})
        send({"method": "initialized", "params": {}})
        send({"method": "thread/resume", "id": 1, "params": {
            "threadId": thread_id, "approvalPolicy": "never", "sandbox": "read-only", "cwd": isolated,
        }})
        compact_requested = False
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError("codex app-server compaction timed out", category="timeout")
                try:
                    line = messages.get(timeout=min(remaining, 1.0))
                except Empty:
                    if process.poll() is not None:
                        raise AppServerError(
                            "codex app-server exited before compacting the thread",
                            category=_classify_error("".join(stderr_tail)),
                        )
                    continue
                if line is None:
                    raise AppServerError(
                        "codex app-server closed its output",
                        category=_classify_error("".join(stderr_tail)),
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    send({"id": message["id"], "result": {"decision": "decline"}})
                    continue
                if message.get("id") in {0, 1, 2} and message.get("error"):
                    error = message.get("error") if isinstance(message.get("error"), dict) else {}
                    raise AppServerError(
                        "codex app-server rejected the compaction request",
                        category=_classify_error(str(error.get("message", ""))),
                    )
                if message.get("id") == 1 and not compact_requested:
                    send({"method": "thread/compact/start", "id": 2, "params": {"threadId": thread_id}})
                    compact_requested = True
                    continue
                if message.get("method") == "thread/compacted":
                    return {"thread_id": thread_id, "status": "completed"}
                if message.get("method") == "item/completed":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    if item.get("type") == "contextCompaction":
                        return {"thread_id": thread_id, "status": "completed"}
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()


def _codex_environment() -> dict[str, str]:
    from scripts.codex_task_router import codex_environment

    return codex_environment()
