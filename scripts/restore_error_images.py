"""Restore verified original diagrams to existing error-notebook question text.

The manifest contains only owned error ids plus either existing content-addressed
question assets or already-prepared diagram image files.  The script never crops,
redraws, OCRs, or infers a diagram; preparation and visual verification happen
before this bounded database mutation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import local_env  # noqa: E402
from services.web_files import LocalFsStorageAdapter  # noqa: E402


ID_RE = re.compile(r"[0-9a-f]{32}")
ASSET_RE = re.compile(r"bank-assets/([0-9a-f]{64})\.(png|jpg|jpeg)")
IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\((bank-assets/[0-9a-f]{64}\.(?:png|jpg|jpeg))\)")


def _image_asset(path: Path) -> tuple[str, bytes]:
    resolved = path.resolve()
    if ROOT not in resolved.parents or not resolved.is_file():
        raise ValueError(f"asset file must be inside the project workspace: {path}")
    content = resolved.read_bytes()
    if len(content) > 8 * 1024 * 1024:
        raise ValueError(f"asset file is too large: {path}")
    with Image.open(resolved) as image:
        image.verify()
        image_format = (image.format or "").upper()
    extension = {"PNG": "png", "JPEG": "jpg"}.get(image_format)
    if extension is None:
        raise ValueError(f"asset file must be PNG or JPEG: {path}")
    digest = hashlib.sha256(content).hexdigest()
    return f"bank-assets/{digest}.{extension}", content


def prepare_manifest(payload: dict, storage: LocalFsStorageAdapter) -> tuple[str, list[dict]]:
    user_id = str(payload.get("user_id") or "")
    entries = payload.get("entries")
    if ID_RE.fullmatch(user_id) is None or not isinstance(entries, list) or not entries:
        raise ValueError("manifest requires one user_id and a non-empty entries list")
    prepared = []
    seen_errors: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("each manifest entry must be an object")
        error_id = str(item.get("error_id") or "")
        if ID_RE.fullmatch(error_id) is None or error_id in seen_errors:
            raise ValueError("manifest error ids must be unique 32-character lowercase hex")
        seen_errors.add(error_id)
        references: list[str] = []
        assets: dict[str, bytes] = {}
        for reference in item.get("existing_refs") or []:
            match = ASSET_RE.fullmatch(str(reference))
            if match is None:
                raise ValueError(f"invalid existing asset reference for {error_id}")
            content = storage.read_bytes(str(reference))
            if hashlib.sha256(content).hexdigest() != match.group(1):
                raise ValueError(f"existing asset hash mismatch for {error_id}")
            references.append(str(reference))
        for filename in item.get("asset_files") or []:
            reference, content = _image_asset(ROOT / str(filename))
            references.append(reference)
            assets[reference] = content
        references = list(dict.fromkeys(references))
        if not references:
            raise ValueError(f"no verified image assets supplied for {error_id}")
        prepared.append({"error_id": error_id, "references": references, "assets": assets})
    return user_id, prepared


def restore(*, manifest: Path, apply: bool) -> dict:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    storage = LocalFsStorageAdapter(local_env.RUNTIME / "quarantine")
    user_id, entries = prepare_manifest(payload, storage)
    connection = local_env._connection_factory()()
    cursor = connection.cursor()
    originals: list[dict] = []
    changed: list[dict] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        connection.begin()
        for entry in entries:
            cursor.execute(
                "SELECT question_text,status FROM error_notebook_entries WHERE id=%s AND user_id=%s FOR UPDATE",
                (entry["error_id"], user_id),
            )
            row = cursor.fetchone()
            if not row or str(row[1]) == "removed":
                raise LookupError(f"active owned error not found: {entry['error_id']}")
            original = str(row[0])
            existing = set(IMAGE_MARKDOWN_RE.findall(original))
            additions = [reference for reference in entry["references"] if reference not in existing]
            originals.append({"error_id": entry["error_id"], "question_text": original})
            if additions:
                updated = original.rstrip() + "\n" + "\n".join(f"![原题图]({reference})" for reference in additions)
                changed.append({"error_id": entry["error_id"], "references": additions})
                if apply:
                    cursor.execute(
                        "UPDATE error_notebook_entries SET question_text=%s,updated_at=%s WHERE id=%s AND user_id=%s",
                        (updated, now, entry["error_id"], user_id),
                    )
                    cursor.execute(
                        "INSERT INTO domain_audit_events (user_id,event_type,resource_type,resource_id,metadata_json,occurred_at) VALUES (%s,'error.diagram_restored','error',%s,%s,%s)",
                        (user_id, entry["error_id"], json.dumps({"references": additions}, separators=(",", ":")), now),
                    )
        if apply:
            for entry in entries:
                for reference, content in entry["assets"].items():
                    storage.save_bytes(reference, content, "image/png" if reference.endswith(".png") else "image/jpeg")
            backup_dir = local_env.RUNTIME / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"error-diagrams-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
            backup_path.write_text(json.dumps({"user_id": user_id, "entries": originals}, ensure_ascii=False, indent=2), encoding="utf-8")
            connection.commit()
        else:
            connection.rollback()
            backup_path = None
        return {
            "status": "applied" if apply else "dry_run",
            "checked": len(entries),
            "changed": len(changed),
            "unchanged": len(entries) - len(changed),
            "restored_assets": sum(len(item["references"]) for item in changed),
            "backup": str(backup_path) if backup_path else None,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore verified diagrams for existing local error records")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write assets and update MySQL; otherwise only validate")
    args = parser.parse_args()
    try:
        print(json.dumps(restore(manifest=args.manifest.resolve(), apply=args.apply), ensure_ascii=False))
        return 0
    except (OSError, ValueError, LookupError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
