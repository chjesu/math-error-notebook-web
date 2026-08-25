"""Exercise the localhost Web-to-Codex app-server stream with synthetic data."""

from __future__ import annotations

from io import BytesIO
from http.cookies import SimpleCookie
import json
import secrets
import time

from PIL import Image, ImageDraw
import httpx


BASE = "http://127.0.0.1:8000"
ORIGIN_HEADERS = {"Origin": BASE, "X-Device-ID": "harness-smoke-device"}


def main() -> int:
    phone = "166" + f"{secrets.randbelow(100_000_000):08d}"
    password = "Aa9!" + secrets.token_urlsafe(12)
    session = httpx.Client()
    requested = session.post(f"{BASE}/v1/auth/register/otp/request", json={"phone": phone}, headers=ORIGIN_HEADERS, timeout=10)
    requested.raise_for_status()
    challenge = requested.json()
    completed = session.post(
        f"{BASE}/v1/auth/register/complete",
        json={
            "phone": phone, "challenge_token": challenge["challenge_token"],
            "code": challenge["local_test_code"], "password": password,
            "terms_version": "2026-08-23", "privacy_version": "2026-08-23",
        },
        headers=ORIGIN_HEADERS,
        timeout=10,
    )
    completed.raise_for_status()
    cookie = SimpleCookie()
    cookie.load(completed.headers["set-cookie"])
    session.cookies.clear()
    session.cookies.set("__Host-lzlm_session", cookie["__Host-lzlm_session"].value, domain="127.0.0.1", path="/")

    image = Image.new("RGB", (900, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((60, 80), "1. x + 1 = 2, find x.", fill="black")
    draw.text((60, 160), "Student answer: x = 0", fill="black")
    content = BytesIO()
    image.save(content, format="PNG")
    uploaded = session.post(
        f"{BASE}/v1/files",
        data={"purpose": "question_image"},
        files={"file": ("harness-smoke.png", content.getvalue(), "image/png")},
        headers=ORIGIN_HEADERS | {"Idempotency-Key": secrets.token_hex(8)},
        timeout=15,
    )
    uploaded.raise_for_status()
    intake = session.post(
        f"{BASE}/v1/intakes", json={"file_id": uploaded.json()["file_id"]},
        headers=ORIGIN_HEADERS | {"Idempotency-Key": secrets.token_hex(8)}, timeout=10,
    )
    intake.raise_for_status()
    intake_id = intake.json()["resource_id"]
    manual = session.post(
        f"{BASE}/v1/intakes/{intake_id}/manual-candidate",
        json={"question_text": "已知 x+1=2，求 x。", "answer_text": "x=0"},
        headers=ORIGIN_HEADERS,
        timeout=10,
    )
    manual.raise_for_status()
    with session.stream(
        "POST", f"{BASE}/v1/intakes/{intake_id}/chat-turn-stream",
        json={
            "message": "请检查题干与作答候选是否完整。", "stage": "intake",
            "input_version": manual.json()["input_version"], "attempt_id": None, "candidate_id": None,
        },
        headers=ORIGIN_HEADERS,
        timeout=httpx.Timeout(180, connect=10),
    ) as stream:
        stream.raise_for_status()
        events = [json.loads(line) for line in stream.iter_lines() if line]
    runtime = [item.get("event", {}).get("type") for item in events if item.get("type") == "runtime"]
    if not {"request_started", "turn_started", "agent_message_delta"} <= set(runtime) or events[-1].get("type") != "result":
        raise RuntimeError("incomplete app-server event stream")
    print(json.dumps({
        "status": "ok", "elapsed_seconds": round(time.monotonic() - started, 2),
        "runtime_event_count": len(runtime), "final_action": events[-1]["data"]["action"],
    }, ensure_ascii=False))
    return 0


started = time.monotonic()


if __name__ == "__main__":
    raise SystemExit(main())
