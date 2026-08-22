"""Build a bounded, explicit, secret-checked Codex review packet."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
MAX_TOTAL_BYTES = 240_000
FORBIDDEN_PARTS = {".git", ".runtime", ".venv", "data", "__pycache__"}
LITERAL_SECRET = re.compile(
    r"(?i)(?:password|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*['\"](?!<|runtime-|test-)[^'\"]{6,}['\"]"
)


def build(task: str, paths: list[Path]) -> dict:
    files = []
    total = 0
    for raw in paths:
        candidate = (ROOT / raw).resolve()
        try:
            relative = candidate.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise ValueError(f"file is outside project root: {raw}") from exc
        if set(relative.parts) & FORBIDDEN_PARTS or not candidate.is_file():
            raise ValueError(f"file is not eligible for review: {raw}")
        content = candidate.read_text(encoding="utf-8")
        if LITERAL_SECRET.search(content):
            raise ValueError(f"possible literal secret found: {relative.as_posix()}")
        encoded = content.encode("utf-8")
        total += len(encoded)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("review packet exceeds 240 KB")
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "content": content,
            }
        )
    return {"schema": "web-review-packet/v1", "task": task, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a frozen Codex review packet")
    parser.add_argument("--task", required=True)
    parser.add_argument("--file", action="append", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        packet = build(args.task, args.file)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        result = {
            "status": "ok",
            "files": len(packet["files"]),
            "out": str(args.out.resolve()),
            "sha256": hashlib.sha256(args.out.read_bytes()).hexdigest(),
        }
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")) if args.json else result)
        return 0
    except (ValueError, OSError, UnicodeError) as exc:
        result = {"status": "error", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")) if args.json else result)
        return 2


if __name__ == "__main__":
    sys.exit(main())

