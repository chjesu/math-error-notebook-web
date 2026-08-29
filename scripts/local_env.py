"""Run the registration service against a private local MySQL 8 instance."""

from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RUNTIME = ROOT / ".runtime" / "local-mysql"
DATA = RUNTIME / "data"
CONFIG = RUNTIME / "my.ini"
SECRETS = RUNTIME / "secrets.json"
ROOT_CLIENT = RUNTIME / "root-client.ini"
APP_CLIENT = RUNTIME / "app-client.ini"
PID_FILE = RUNTIME / "mysqld.pid"
ERROR_LOG = RUNTIME / "mysqld.log"
READY = RUNTIME / "ready"
PORT = 3307
DATABASE = "lzlm_web_local"
APP_USER = "lzlm_app"
DEFAULT_BASEDIR = Path(r"C:\Program Files\MySQL\MySQL Server 8.4")
MIGRATIONS = (
    ROOT / "services" / "web_auth" / "migrations" / "0001_phone_registration.sql",
    ROOT / "services" / "web_domain" / "migrations" / "0002_web_domain.sql",
    ROOT / "services" / "web_auth" / "migrations" / "0003_account_simplification.sql",
    ROOT / "services" / "web_domain" / "migrations" / "0004_learning_loop.sql",
    ROOT / "services" / "web_auth" / "migrations" / "0005_auth_v040.sql",
    ROOT / "services" / "web_domain" / "migrations" / "0006_privacy.sql",
    ROOT / "services" / "web_auth" / "migrations" / "0007_auth_security.sql",
    ROOT / "services" / "web_domain" / "migrations" / "0008_privacy_recovery.sql",
    ROOT / "services" / "web_domain" / "migrations" / "0009_file_upload_idempotency.sql",
    ROOT / "services" / "web_domain" / "migrations" / "0010_codex_harness.sql",
    ROOT / "services" / "web_domain" / "migrations" / "0011_daily_learning_usage.sql",
    ROOT / "services" / "web_auth" / "migrations" / "0012_operations_admin.sql",
)
HARNESS_WEB_HOME = ROOT / "data" / "runtime" / "deepseek-harness-web-home"
HARNESS_PRODUCT_WORKSPACE = HARNESS_WEB_HOME / "math-notebook-workspace"
HARNESS_AGENT_PRESETS = ROOT / "config" / "deepseek-harness" / "agent-presets"
HARNESS_RUNTIME_PRESET = HARNESS_WEB_HOME / ".agent-presets" / "math-notebook" / "agent.cordis.yml"
HARNESS_WEB_PATCH = ROOT / "config" / "deepseek-harness" / "web-product.patch.yml"
HARNESS_WEB_STDOUT = ROOT / "data" / "runtime" / "deepseek-harness-web.stdout.log"
HARNESS_WEB_STDERR = ROOT / "data" / "runtime" / "deepseek-harness-web.stderr.log"
HARNESS_WEB_PORT = 3080


def _basedir() -> Path:
    return Path(os.environ.get("LZLM_LOCAL_MYSQL_HOME", DEFAULT_BASEDIR)).resolve()


def _binary(name: str) -> Path:
    path = _basedir() / "bin" / f"{name}.exe"
    if not path.is_file():
        raise RuntimeError(f"MySQL binary is missing: {path}")
    return path


def _slash(path: Path) -> str:
    return path.resolve().as_posix()


def _write_config() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(
        "\n".join(
            (
                "[mysqld]",
                f'basedir="{_slash(_basedir())}"',
                f'datadir="{_slash(DATA)}"',
                f'pid-file="{_slash(PID_FILE)}"',
                f'log-error="{_slash(ERROR_LOG)}"',
                f"port={PORT}",
                "bind-address=127.0.0.1",
                "mysqlx=0",
                "local-infile=OFF",
                "secure-file-priv=NULL",
                "character-set-server=utf8mb4",
                "collation-server=utf8mb4_0900_ai_ci",
                "max-connections=100",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )


def _load_secrets() -> dict[str, str]:
    if not SECRETS.is_file():
        raise RuntimeError("local secrets are missing; run local_env.py init")
    value = json.loads(SECRETS.read_text(encoding="utf-8"))
    required = {"root_password", "app_password", "auth_pepper_b64"}
    if not isinstance(value, dict) or not required <= value.keys():
        raise RuntimeError("local secrets file is invalid")
    return {key: str(value[key]) for key in required}


def _write_client_configs() -> None:
    values = _load_secrets()
    ROOT_CLIENT.write_text(
        f"[client]\npassword={values['root_password']}\n",
        encoding="utf-8",
        newline="\n",
    )
    APP_CLIENT.write_text(
        f"[client]\npassword={values['app_password']}\n",
        encoding="utf-8",
        newline="\n",
    )


def _client_args(*, root: bool, database: bool = False, password: bool = True) -> list[str]:
    args = [str(_binary("mysql"))]
    if password:
        args.append(f"--defaults-extra-file={ROOT_CLIENT if root else APP_CLIENT}")
    args.extend(
        [
            "--protocol=TCP",
            "--host=127.0.0.1",
            f"--port={PORT}",
            "--default-character-set=utf8mb4",
            "--batch",
            "--skip-column-names",
            f"--user={'root' if root else APP_USER}",
        ]
    )
    if database:
        args.append(DATABASE)
    return args


def _run_sql(
    sql: str,
    *,
    root: bool,
    database: bool = False,
    password: bool = True,
    label: str = "MySQL command",
) -> str:
    result = subprocess.run(
        _client_args(root=root, database=database, password=password),
        input=sql,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode:
        detail = " ".join(result.stderr.strip().splitlines()[-2:])
        for secret in _load_secrets().values():
            detail = detail.replace(secret, "<redacted>")
        raise RuntimeError(f"{label} failed: {detail or 'unknown MySQL error'}")
    return result.stdout.strip()


def _ensure_root_password() -> None:
    try:
        _run_sql("SELECT 1;", root=True, label="root password check")
        return
    except RuntimeError:
        pass
    _run_sql(
        f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{_load_secrets()['root_password']}';",
        root=True,
        password=False,
        label="initial root password setup",
    )


def _bootstrap_local_database() -> None:
    values = _load_secrets()
    _ensure_root_password()
    bootstrap = (
        f"DROP DATABASE IF EXISTS `{DATABASE}`;\n"
        f"DROP USER IF EXISTS '{APP_USER}'@'127.0.0.1';\n"
        f"CREATE DATABASE `{DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n"
        f"CREATE USER '{APP_USER}'@'127.0.0.1' IDENTIFIED BY '{values['app_password']}';\n"
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{DATABASE}`.* TO '{APP_USER}'@'127.0.0.1';\n"
        "FLUSH PRIVILEGES;\n"
    )
    _run_sql(bootstrap, root=True, label="local database bootstrap")
    _apply_migrations()


def _apply_migrations() -> None:
    _run_sql(
        "CREATE TABLE IF NOT EXISTS web_schema_migrations ("
        "name VARCHAR(128) CHARACTER SET ascii PRIMARY KEY, sha256 CHAR(64) CHARACTER SET ascii NULL, "
        "applied_at DATETIME(6) NOT NULL"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;",
        root=True,
        database=True,
        label="migration ledger setup",
    )
    has_hash = _run_sql(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() "
        "AND table_name='web_schema_migrations' AND column_name='sha256';",
        root=True,
        database=True,
        label="migration ledger shape check",
    )
    if has_hash.strip() == "0":
        _run_sql(
            "ALTER TABLE web_schema_migrations ADD COLUMN sha256 CHAR(64) CHARACTER SET ascii NULL AFTER name;",
            root=True,
            database=True,
            label="migration ledger hash upgrade",
        )
    ledger_output = _run_sql(
        "SELECT name,COALESCE(sha256,'') FROM web_schema_migrations ORDER BY name;",
        root=True,
        database=True,
        label="migration ledger read",
    )
    existing = {
        parts[0]: parts[1] if len(parts) > 1 else ""
        for line in ledger_output.splitlines()
        if line.strip()
        for parts in [line.split("\t", 1)]
    }
    for migration in MIGRATIONS:
        name = migration.name
        digest = hashlib.sha256(migration.read_bytes()).hexdigest()
        if name in existing:
            if name == "0002_web_domain.sql" and not existing[name]:
                shape = _run_sql(
                    "SELECT SUM(table_name='intake_items'),SUM(table_name='web_tenants') "
                    "FROM information_schema.tables WHERE table_schema=DATABASE() "
                    "AND table_name IN ('intake_items','web_tenants');",
                    root=True,
                    database=True,
                    label="legacy domain schema check",
                )
                if shape.strip() != "1\t0":
                    raise RuntimeError(
                        "0002 ledger exists with the retired domain schema; rebuild the local database from a backup"
                    )
            if existing[name] and existing[name] != digest:
                raise RuntimeError(f"applied migration hash mismatch: {name}")
            if not existing[name]:
                _run_sql(
                    f"UPDATE web_schema_migrations SET sha256='{digest}' WHERE name='{name}' AND sha256 IS NULL;",
                    root=True,
                    database=True,
                    label=f"backfill {name} hash",
                )
            continue
        if name == "0001_phone_registration.sql":
            legacy = _run_sql(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name='web_users';",
                root=True,
                database=True,
                label="legacy schema check",
            )
            if legacy.strip() == "1":
                _run_sql(
                    f"INSERT INTO web_schema_migrations (name, sha256, applied_at) VALUES ('{name}', '{digest}', UTC_TIMESTAMP(6));",
                    root=True,
                    database=True,
                    label=f"record existing {name}",
                )
                existing[name] = digest
                continue
        migration_sql = migration.read_text(encoding="utf-8")
        if name == "0005_auth_v040.sql":
            partial = _run_sql(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
                "AND table_name IN ('auth_password_credentials','auth_agreement_acceptances');",
                root=True,
                database=True,
                label="0005 partial schema check",
            )
            if partial.strip() != "0":
                migration_sql = (migration.with_name("0007_auth_security.sql")).read_text(encoding="utf-8")
        elif name == "0008_privacy_recovery.sql":
            applied_columns = _run_sql(
                "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() "
                "AND table_name='account_deletions' "
                "AND column_name IN ('status','updated_at','last_error_code');",
                root=True,
                database=True,
                label="0008 recovery schema check",
            )
            if applied_columns.strip() == "3":
                migration_sql = "SELECT 1;"
            elif applied_columns.strip() != "0":
                raise RuntimeError("0008 privacy recovery schema is partially applied")
        _run_sql(migration_sql, root=True, database=True, label=f"apply {name}")
        _run_sql(
            f"INSERT INTO web_schema_migrations (name, sha256, applied_at) VALUES ('{name}', '{digest}', UTC_TIMESTAMP(6));",
            root=True,
            database=True,
            label=f"record {name}",
        )
        existing[name] = digest
    READY.write_text("\n".join(migration.name for migration in MIGRATIONS) + "\n", encoding="utf-8")


def _is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_running(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_running():
            return
        time.sleep(0.2)
    tail = ""
    if ERROR_LOG.is_file():
        tail = "\n".join(ERROR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
    raise RuntimeError(f"local MySQL did not start\n{tail}")


def start() -> None:
    if _is_running():
        return
    if not DATA.is_dir() or not CONFIG.is_file():
        raise RuntimeError("local MySQL is not initialized; run local_env.py init")
    creationflags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= int(getattr(subprocess, name, 0))
    subprocess.Popen(
        [str(_binary("mysqld")), f"--defaults-file={CONFIG}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    _wait_running()


def stop() -> None:
    if not _is_running():
        return
    result = subprocess.run(
        [
            str(_binary("mysqladmin")),
            f"--defaults-extra-file={ROOT_CLIENT}",
            "--protocol=TCP",
            "--host=127.0.0.1",
            f"--port={PORT}",
            "--user=root",
            "shutdown",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("local MySQL shutdown failed")


def init() -> None:
    _binary("mysqld")
    _binary("mysql")
    if DATA.is_dir() and any(DATA.iterdir()):
        if not SECRETS.is_file():
            raise RuntimeError("data exists without its local secrets; do not overwrite it")
        _write_config()
        _write_client_configs()
        start()
        _apply_migrations()
        return

    _write_config()
    DATA.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(_binary("mysqld")), f"--defaults-file={CONFIG}", "--initialize-insecure", "--console"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"MySQL initialization failed\n{result.stdout[-2000:]}")

    values = {
        "root_password": secrets.token_urlsafe(30),
        "app_password": secrets.token_urlsafe(30),
        "auth_pepper_b64": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
    }
    SECRETS.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_client_configs()
    start()
    _bootstrap_local_database()


def _connection_factory():
    values = _load_secrets()
    import pymysql

    def connect():
        return pymysql.connect(
            host="127.0.0.1",
            port=PORT,
            user=APP_USER,
            password=values["app_password"],
            database=DATABASE,
            charset="utf8mb4",
            autocommit=False,
            connect_timeout=3,
            read_timeout=3,
            write_timeout=3,
        )

    return connect


def _clear_test_data() -> None:
    connection = _connection_factory()()
    cursor = connection.cursor()
    try:
        for table in (
            "operations_audit_events",
            "admin_operators",
            "account_deletions",
            "file_upload_idempotency",
            "daily_learning_usage",
            "review_attempts",
            "recommendations",
            "review_tasks",
            "domain_audit_events",
            "error_notebook_entries",
            "grade_candidates",
            "attempts",
            "web_jobs",
            "intake_items",
            "web_files",
            "auth_agreement_acceptances",
            "auth_password_credentials",
            "auth_sessions",
            "auth_sms_challenges",
            "auth_sms_send_events",
            "auth_rate_limit_buckets",
            "auth_send_cooldowns",
            "auth_audit_events",
            "web_users",
        ):
            cursor.execute(f"DELETE FROM `{table}`")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _existing_user_count() -> int:
    return int(_run_sql("SELECT COUNT(*) FROM web_users;", root=False, database=True, label="local user count"))


def grant_admin(phone_last4: str, role: str) -> dict[str, str]:
    """Grant a local-only operator role without accepting or displaying a full phone number."""

    if not re.fullmatch(r"\d{4}", phone_last4):
        raise ValueError("phone_last4 must contain exactly four digits")
    if role not in {"operations", "reviewer", "security", "administrator"}:
        raise ValueError("invalid operator role")
    start()
    _apply_migrations()
    connection = _connection_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT id FROM web_users WHERE phone_last4=%s AND status='active' ORDER BY created_at",
            (phone_last4,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise ValueError("phone_last4 must match exactly one active local account")
        user_id = str(rows[0][0])
        connection.begin()
        cursor.execute(
            "INSERT INTO admin_operators (user_id,role,status,granted_by,created_at,updated_at) "
            "VALUES (%s,%s,'active',NULL,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) "
            "ON DUPLICATE KEY UPDATE role=VALUES(role),status='active',updated_at=UTC_TIMESTAMP(6)",
            (user_id, role),
        )
        connection.commit()
        return {"status": "ok", "operator": f"用户 ····{phone_last4}", "role": role}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _clear_smoke_auth(service: Any, phones: list[str], user_ids: list[str]) -> None:
    """Remove only identities created by smoke; never touch existing local users."""

    phone_hashes = list(dict.fromkeys(service._hash("phone", phone) for phone in phones))
    cooldown_hashes = [service._hash("cooldown", phone_hash) for phone_hash in phone_hashes]
    users = list(dict.fromkeys(user_ids))
    connection = _connection_factory()()
    cursor = connection.cursor()
    try:
        connection.begin()
        if users:
            placeholders = ",".join(["%s"] * len(users))
            cursor.execute(f"DELETE FROM operations_audit_events WHERE operator_user_id IN ({placeholders})", tuple(users))
            cursor.execute(f"DELETE FROM admin_operators WHERE user_id IN ({placeholders}) OR granted_by IN ({placeholders})", tuple(users + users))
            for table in ("account_deletions", "auth_agreement_acceptances", "auth_password_credentials", "auth_sessions"):
                cursor.execute(f"DELETE FROM `{table}` WHERE user_id IN ({placeholders})", tuple(users))
            cursor.execute(f"DELETE FROM web_users WHERE id IN ({placeholders})", tuple(users))
        if phone_hashes:
            placeholders = ",".join(["%s"] * len(phone_hashes))
            cursor.execute(f"DELETE FROM auth_sms_challenges WHERE phone_lookup_hash IN ({placeholders})", tuple(phone_hashes))
            cursor.execute(f"DELETE FROM auth_sms_send_events WHERE phone_lookup_hash IN ({placeholders})", tuple(phone_hashes))
            cursor.execute(f"DELETE FROM auth_audit_events WHERE phone_lookup_hash IN ({placeholders})", tuple(phone_hashes))
            cursor.execute(f"DELETE FROM auth_rate_limit_buckets WHERE dimension='phone' AND subject_hash IN ({placeholders})", tuple(phone_hashes))
        if cooldown_hashes:
            placeholders = ",".join(["%s"] * len(cooldown_hashes))
            cursor.execute(f"DELETE FROM auth_send_cooldowns WHERE phone_lookup_hash IN ({placeholders})", tuple(cooldown_hashes))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _live_schema_ready() -> bool:
    """Verify the live ledger and required v0.4 tables, not only the ready file."""

    if not _is_running():
        return False
    try:
        rows = _run_sql(
            "SELECT name,COALESCE(sha256,'') FROM web_schema_migrations ORDER BY name;",
            root=False,
            database=True,
            label="live migration ledger check",
        )
        applied = {
            parts[0]: parts[1] if len(parts) > 1 else ""
            for line in rows.splitlines()
            if line.strip()
            for parts in [line.split("\t", 1)]
        }
        for migration in MIGRATIONS:
            if applied.get(migration.name) != hashlib.sha256(migration.read_bytes()).hexdigest():
                return False
        required = _run_sql(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
            "AND table_name IN ('web_users','auth_password_credentials',"
            "'auth_agreement_acceptances','web_jobs','account_deletions','file_upload_idempotency',"
            "'admin_operators','operations_audit_events');",
            root=False,
            database=True,
            label="live schema table check",
        )
        recovery_columns = _run_sql(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() "
            "AND table_name='account_deletions' AND column_name IN ('status','updated_at','last_error_code');",
            root=False,
            database=True,
            label="live privacy recovery schema check",
        )
        return required.strip() == "8" and recovery_columns.strip() == "3"
    except RuntimeError:
        return False


async def _asgi_call(
    app: Any,
    path: str,
    payload: dict[str, Any],
    *,
    cookie: str | None = None,
    idempotency_key: str | None = None,
    method: str = "POST",
    origin: str = "https://local.test",
    extra_headers: dict[str, str] | None = None,
    client: tuple[str, int] = ("198.51.100.10", 12345),
) -> tuple[int, dict[str, str], Any]:
    body = json.dumps(payload).encode("utf-8")
    requests = [{"type": "http.request", "body": body, "more_body": False}]
    responses: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return requests.pop(0)

    async def send(message: dict[str, Any]) -> None:
        responses.append(message)

    request_headers = [
        (b"host", b"local.test"),
        (b"content-type", b"application/json"),
        (b"x-device-id", b"local-smoke-device"),
    ]
    if cookie:
        request_headers.extend([(b"cookie", cookie.split(";", 1)[0].encode("ascii")), (b"origin", origin.encode("ascii"))])
    if idempotency_key:
        request_headers.append((b"idempotency-key", idempotency_key.encode("ascii")))
    for key, value in (extra_headers or {}).items():
        request_headers.append((key.encode("ascii"), value.encode("ascii")))
    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "scheme": "https",
            "client": client,
            "headers": request_headers,
        },
        receive,
        send,
    )
    started, finished = responses
    headers = {key.decode("ascii"): value.decode("ascii") for key, value in started["headers"]}
    response_body = finished.get("body", b"")
    if not response_body:
        parsed: Any = None
    elif headers.get("content-type", "").startswith("application/json"):
        parsed = json.loads(response_body)
    else:
        parsed = response_body
    return started["status"], headers, parsed


def _service(*, cooldown_seconds: int = 60):
    from services.web_auth import (
        AuthConfig,
        InMemoryCaptchaVerifier,
        MySqlRegistrationStore,
        RecordingSmsSender,
        RegistrationService,
    )

    sender = RecordingSmsSender()
    service = RegistrationService(
        store=MySqlRegistrationStore(_connection_factory()),
        sms_sender=sender,
        captcha_verifier=InMemoryCaptchaVerifier({"local-captcha"}),
        secret_pepper=base64.b64decode(_load_secrets()["auth_pepper_b64"]),
        config=AuthConfig(resend_cooldown_seconds=cooldown_seconds, captcha_after_phone_day=99, captcha_after_ip_hour=99),
    )
    return service, sender


def _domain_smoke(service: Any, sender: Any, session_cookie: str, phone: str) -> dict[str, int]:
    from services.web_app import NotebookAsgiApp
    from services.web_domain import MySqlDomainStore, NotebookService

    token = session_cookie.split(";", 1)[0].split("=", 1)[1]
    user = service.authenticate_session(token)
    if user is None:
        raise RuntimeError("domain smoke session authentication failed")
    store = MySqlDomainStore(_connection_factory())
    with tempfile.TemporaryDirectory(prefix="domain-smoke-", dir=RUNTIME) as directory:
        notebook = NotebookService(store, Path(directory))
        upload_content = b"\x89PNG\r\n\x1a\nlocal-smoke"
        uploaded = notebook.upload(user_id=user.user_id, purpose="question_image", original_name="question.png", content=upload_content, idempotency_key="smoke-upload")
        replayed_upload = notebook.upload(user_id=user.user_id, purpose="question_image", original_name="question.png", content=upload_content, idempotency_key="smoke-upload")
        if replayed_upload.file_id != uploaded.file_id:
            raise RuntimeError("file upload idempotency replay failed")
        try:
            notebook.upload(user_id=user.user_id, purpose="question_image", original_name="question.png", content=b"\x89PNG\r\n\x1a\nchanged-smoke", idempotency_key="smoke-upload")
        except RuntimeError as exc:
            if str(exc) != "conflict":
                raise
        else:
            raise RuntimeError("file upload idempotency conflict was not rejected")
        harness_token = "local-smoke-harness-token"
        app = NotebookAsgiApp(service, notebook, allowed_hosts={"local.test"}, harness_internal_token=harness_token)
        created = asyncio.run(_asgi_call(app, "/v1/intakes", {"file_id": uploaded.file_id}, cookie=session_cookie, idempotency_key="smoke-extract"))
        if created[0] != 202:
            raise RuntimeError("domain intake API smoke test failed")
        intake_id = created[2]["resource_id"]
        manual = asyncio.run(_asgi_call(app, f"/v1/intakes/{intake_id}/manual-candidate", {"question_text": "解方程 x+1=2", "answer_text": "x=0"}, cookie=session_cookie))
        repeated = asyncio.run(_asgi_call(app, f"/v1/intakes/{intake_id}/manual-candidate", {"question_text": "解方程 x+1=2", "answer_text": "x=0"}, cookie=session_cookie))
        if manual[0] != 201 or repeated[2] != manual[2]:
            raise RuntimeError("domain manual intake API smoke test failed")
        confirmed = asyncio.run(_asgi_call(app, f"/v1/intakes/{intake_id}/confirm", {"input_version": 1}, cookie=session_cookie, idempotency_key="smoke-grade"))
        if confirmed[0] != 202:
            raise RuntimeError("domain manual intake confirmation API smoke test failed")
        graded = asyncio.run(_asgi_call(app, f"/v1/attempts/{confirmed[2]['resource_id']}/manual-grade", {
            "input_version": 1,
            "verdict": "incorrect",
            "first_error": "移项符号错误",
            "cause_code": "algebra_transform",
            "evidence": "把常数项移到等号右侧时没有变号",
            "knowledge_points": ["一元一次方程", "等式性质与移项"],
            "correct_solution": "x+1=2，所以 x=1",
            "final_answer": "x=1",
            "prevention_cue": "移项后立即检查符号",
        }, cookie=session_cookie))
        if graded[0] != 201:
            raise RuntimeError("domain manual grade candidate API smoke test failed")
        bound = asyncio.run(_asgi_call(
            app,
            "/v1/harness/sessions/bind",
            {"session_id": "local-smoke-session"},
            cookie=session_cookie,
            origin="http://local.test:3080",
        ))
        if bound[0] != 200:
            raise RuntimeError("Harness session binding smoke test failed")
        internal = {
            "extra_headers": {"authorization": f"Bearer {harness_token}"},
            "client": ("127.0.0.1", 3080),
        }
        committed = asyncio.run(_asgi_call(
            app,
            f"/v1/internal/harness/grade-results/{graded[2]['result_id']}/commit",
            {"session_id": "local-smoke-session", "input_version": 1},
            **internal,
        ))
        replayed_commit = asyncio.run(_asgi_call(
            app,
            f"/v1/internal/harness/grade-results/{graded[2]['result_id']}/commit",
            {"session_id": "local-smoke-session", "input_version": 1},
            **internal,
        ))
        receipt = committed[2].get("receipt", {}) if committed[0] == 200 else {}
        replayed_receipt = replayed_commit[2].get("receipt", {}) if replayed_commit[0] == 200 else {}
        if (
            receipt.get("status") != "saved"
            or receipt.get("knowledge_point_count") != 2
            or receipt.get("review_status") != "scheduled"
            or replayed_receipt.get("status") != "already_saved"
            or replayed_receipt.get("error_id") != receipt.get("error_id")
        ):
            raise RuntimeError("Harness notebook receipt smoke test failed")
        error_id = receipt["error_id"]
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        source_id = hashlib.sha256(f"{user.user_id}:source".encode("ascii")).hexdigest()[:32]
        question_id = hashlib.sha256(f"{user.user_id}:question".encode("ascii")).hexdigest()[:32]
        version_id = hashlib.sha256(f"{user.user_id}:version".encode("ascii")).hexdigest()[:32]
        verification_id = hashlib.sha256(f"{user.user_id}:verification".encode("ascii")).hexdigest()[:32]
        connection = _connection_factory()()
        cursor = connection.cursor()
        try:
            connection.begin()
            cursor.execute("INSERT INTO question_sources (id,title,license_status,content_sha256,created_at) VALUES (%s,'本地验证题库','open',%s,%s)", (source_id, secrets.token_hex(32), now))
            cursor.execute("INSERT INTO questions (id,source_id,canonical_sha256,grade,difficulty,status,current_version_no,created_at,updated_at) VALUES (%s,%s,%s,10,2.0,'verified',1,%s,%s)", (question_id, source_id, secrets.token_hex(32), now, now))
            cursor.execute("INSERT INTO question_versions (id,question_id,version_no,stem_text,answer_text,content_sha256,created_at) VALUES (%s,%s,1,'解方程 x+2=4','x=2',%s,%s)", (version_id, question_id, secrets.token_hex(32), now))
            cursor.execute("INSERT INTO question_verifications (id,question_version_id,verdict,method,evidence_sha256,verified_at) VALUES (%s,%s,'verified','human',%s,%s)", (verification_id, version_id, secrets.token_hex(32), now))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
            connection.close()
        recommendations, _ = store.assign_recommendations(user_id=user.user_id, error_id=error_id)
        reviews = store.list_due_reviews(user_id=user.user_id)
        if not recommendations or len(reviews) != 1:
            raise RuntimeError("domain recommendation or review smoke test failed")
        store.complete_review(user_id=user.user_id, task_id=reviews[0].task_id, result="correct", idempotency_key="smoke-review")
        pdf_job = notebook.create_practice_pdf(user_id=user.user_id, error_ids=[error_id], idempotency_key="smoke-pdf")
        _, pdf = notebook.download_practice_pdf(user_id=user.user_id, job_id=pdf_job.job_id)
        if not pdf.startswith(b"%PDF-"):
            raise RuntimeError("domain PDF smoke test failed")
        export_otp = asyncio.run(_asgi_call(app, "/v1/auth/sensitive/otp/request", {"phone": phone, "action": "export"}, cookie=session_cookie))
        if export_otp[0] != 202:
            raise RuntimeError("export sensitive OTP smoke test failed")
        exported = asyncio.run(_asgi_call(app, "/v1/exports", {"phone": phone, "challenge_token": export_otp[2]["challenge_token"], "code": sender.deliveries[-1][1]}, cookie=session_cookie, idempotency_key="smoke-export"))
        if exported[0] != 201 or not exported[2].get("download_url"):
            raise RuntimeError("personal export smoke test failed")
        downloaded = asyncio.run(_asgi_call(app, exported[2]["download_url"], {}, cookie=session_cookie, method="GET"))
        if downloaded[0] != 200 or "errors" not in downloaded[2].get("data", {}):
            raise RuntimeError("personal export download smoke test failed")
        delete_otp = asyncio.run(_asgi_call(app, "/v1/auth/sensitive/otp/request", {"phone": phone, "action": "delete"}, cookie=session_cookie))
        if delete_otp[0] != 202:
            code = (delete_otp[2] or {}).get("error", {}).get("code", "unknown")
            raise RuntimeError(f"delete sensitive OTP smoke test failed: {delete_otp[0]} {code}")
        deleted = asyncio.run(_asgi_call(app, "/v1/account", {"phone": phone, "challenge_token": delete_otp[2]["challenge_token"], "code": sender.deliveries[-1][1], "confirmation": "DELETE"}, cookie=session_cookie, method="DELETE"))
        if deleted[0] != 204 or service.authenticate_session(token) is not None:
            raise RuntimeError("account deletion smoke test failed")
        return {"manual_api": 1, "upload_idempotency": 1, "recommendations": len(recommendations), "reviews": len(reviews), "pdf_bytes": len(pdf), "export": 1, "deletion": 1}


def _clear_domain_smoke_data(user_id: str) -> None:
    source_id = hashlib.sha256(f"{user_id}:source".encode("ascii")).hexdigest()[:32]
    question_id = hashlib.sha256(f"{user_id}:question".encode("ascii")).hexdigest()[:32]
    version_id = hashlib.sha256(f"{user_id}:version".encode("ascii")).hexdigest()[:32]
    verification_id = hashlib.sha256(f"{user_id}:verification".encode("ascii")).hexdigest()[:32]
    connection = _connection_factory()()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM account_deletions WHERE user_id=%s", (user_id,))
        for table in ("file_upload_idempotency", "daily_learning_usage", "review_attempts", "recommendations", "review_tasks", "domain_audit_events", "error_notebook_entries", "grade_candidates", "attempts", "web_jobs", "intake_items", "web_files"):
            cursor.execute(f"DELETE FROM `{table}` WHERE user_id=%s", (user_id,))
        cursor.execute("DELETE FROM question_verifications WHERE id=%s AND question_version_id=%s", (verification_id, version_id))
        cursor.execute("DELETE FROM question_versions WHERE id=%s AND question_id=%s", (version_id, question_id))
        cursor.execute("DELETE FROM questions WHERE id=%s AND source_id=%s", (question_id, source_id))
        cursor.execute("DELETE FROM question_sources WHERE id=%s", (source_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def smoke() -> dict[str, Any]:
    from services.web_auth import AuthAsgiApp, AuthConfig
    from services.web_auth.registration import SendCodeStatus

    start()
    preserved_user_count = _existing_user_count()
    service, sender = _service(cooldown_seconds=0)
    app = AuthAsgiApp(service, allowed_hosts={"local.test"})
    phone = f"139{secrets.randbelow(100_000_000):08d}"
    requested = asyncio.run(_asgi_call(app, "/v1/auth/register/otp/request", {"phone": phone}))
    if requested[0] != 202 or len(sender.deliveries) != 1:
        raise RuntimeError("OTP request smoke test failed")
    verified = asyncio.run(
        _asgi_call(
            app,
            "/v1/auth/register/complete",
            {
                "challenge_token": requested[2]["challenge_token"],
                "phone": phone,
                "code": sender.deliveries[0][1],
                "password": "local-smoke-pass1",
                "terms_version": "2026-08-23",
                "privacy_version": "2026-08-23",
            },
        )
    )
    if verified[0] != 200 or "set-cookie" not in verified[1]:
        raise RuntimeError("OTP verification smoke test failed")
    login_requested = asyncio.run(_asgi_call(app, "/v1/auth/login/otp/request", {"phone": phone}))
    login = asyncio.run(
        _asgi_call(
            app,
            "/v1/auth/login/otp/verify",
            {
                "challenge_token": login_requested[2]["challenge_token"],
                "phone": phone,
                "code": sender.deliveries[-1][1],
                "terms_version": "2026-08-23",
                "privacy_version": "2026-08-23",
            },
        )
    )
    if login[0] != 200 or "set-cookie" not in login[1]:
        raise RuntimeError("OTP login smoke test failed")
    replay = asyncio.run(
        _asgi_call(
            app,
            "/v1/auth/login/otp/verify",
            {"challenge_token": login_requested[2]["challenge_token"], "phone": phone, "code": sender.deliveries[-1][1], "terms_version": "2026-08-23", "privacy_version": "2026-08-23"},
        )
    )
    if replay[0] != 400:
        raise RuntimeError("OTP replay protection smoke test failed")
    smoke_user = service.authenticate_session(login[1]["set-cookie"].split(";", 1)[0].split("=", 1)[1])
    if smoke_user is None:
        raise RuntimeError("domain smoke user lookup failed")
    try:
        domain = _domain_smoke(service, sender, login[1]["set-cookie"], phone)
    finally:
        _clear_domain_smoke_data(smoke_user.user_id)
        _clear_smoke_auth(service, [phone], [smoke_user.user_id])

    concurrent_service, concurrent_sender = _service()
    concurrent_phone = f"138{secrets.randbelow(100_000_000):08d}"

    def request_once(_: int):
        return concurrent_service.request_code(
            purpose="register",
            phone=concurrent_phone,
            ip_address="198.51.100.20",
            device_id="local-concurrent-device",
        ).status

    with ThreadPoolExecutor(max_workers=20) as executor:
        concurrent_statuses = list(executor.map(request_once, range(50)))
    if concurrent_statuses.count(SendCodeStatus.ACCEPTED) != 1 or len(concurrent_sender.deliveries) != 1:
        raise RuntimeError("concurrent SMS reservation smoke test failed")
    _clear_smoke_auth(concurrent_service, [concurrent_phone], [])

    ip_service, ip_sender = _service()
    ip_service.config = AuthConfig(captcha_after_phone_day=99, captcha_after_ip_hour=99)
    first_number = secrets.randbelow(99_999_989)
    ip_phones = [f"137{first_number + index:08d}" for index in range(11)]
    ip_statuses = [
        ip_service.request_code(
            purpose="register",
            phone=phone,
            ip_address="198.51.100.30",
            device_id=f"local-ip-limit-device-{index}",
        ).status
        for index, phone in enumerate(ip_phones)
    ]
    if ip_statuses[:10] != [SendCodeStatus.ACCEPTED] * 10 or ip_statuses[10] is not SendCodeStatus.RETRY_LATER:
        raise RuntimeError("IP minute limit smoke test failed")
    if len(ip_sender.deliveries) != 10:
        raise RuntimeError("IP minute limit called the SMS sender too many times")
    _clear_smoke_auth(ip_service, ip_phones, [])
    return {
        "status": "ok",
        "mysql": f"127.0.0.1:{PORT}/{DATABASE}",
        "otp_request": requested[0],
        "otp_verify": verified[0],
        "otp_login": login[0],
        "otp_replay": replay[0],
        "secure_cookie": "Secure" in verified[1]["set-cookie"],
        "concurrent_requests": 50,
        "concurrent_provider_sends": len(concurrent_sender.deliveries),
        "ip_minute_requests": 11,
        "ip_minute_provider_sends": len(ip_sender.deliveries),
        "domain_recommendations": domain["recommendations"],
        "domain_reviews": domain["reviews"],
        "domain_manual_api": domain["manual_api"],
        "domain_upload_idempotency": domain["upload_idempotency"],
        "domain_pdf_bytes": domain["pdf_bytes"],
        "domain_export": domain["export"],
        "account_deletion": domain["deletion"],
        "preserved_user_count": preserved_user_count,
    }


class ConsoleSmsSender:
    """Local-only sender that keeps simulated deliveries in process memory."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, str, int]] = []

    def send_verification(self, phone: str, code: str, ttl_seconds: int) -> str:
        self.deliveries.append((phone, code, ttl_seconds))
        print(f"[LOCAL SMS] {phone[:3]}****{phone[-4:]} simulated code issued", flush=True)
        return f"local-{secrets.token_hex(8)}"


class LocalOtpDisclosureApp:
    """Add a simulated OTP to localhost-only request responses."""

    OTP_PATHS = {
        "/v1/auth/register/otp/request",
        "/v1/auth/login/otp/request",
        "/v1/auth/sensitive/otp/request",
    }

    def __init__(self, app: Any, sender: Any) -> None:
        self.app = app
        self.sender = sender

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") not in self.OTP_PATHS:
            await self.app(scope, receive, send)
            return

        delivery_count = len(self.sender.deliveries)
        messages: list[dict[str, Any]] = []

        async def capture(message: dict[str, Any]) -> None:
            messages.append(message)

        await self.app(scope, receive, capture)
        start_message = next((message for message in messages if message["type"] == "http.response.start"), None)
        if start_message is None or start_message.get("status") != 202 or len(self.sender.deliveries) != delivery_count + 1:
            for message in messages:
                await send(message)
            return

        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        payload = json.loads(body)
        payload["local_test_code"] = self.sender.deliveries[-1][1]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = [(key, value) for key, value in start_message["headers"] if key.lower() != b"content-length"]
        headers.append((b"content-length", str(len(encoded)).encode("ascii")))
        await send({**start_message, "headers": headers})
        await send({"type": "http.response.body", "body": encoded})


def _harness_web_command() -> list[str]:
    executable = shutil.which("node")
    entry = ROOT / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    if not executable or not entry.is_file():
        raise RuntimeError("DeepSeek Harness Web is not installed; run npm ci")
    if not HARNESS_WEB_PATCH.is_file():
        raise RuntimeError(f"DeepSeek Harness Web patch is missing: {HARNESS_WEB_PATCH}")
    return [
        executable,
        str(entry),
        "--profile", "web",
        "--patch", str(HARNESS_WEB_PATCH),
        "--host", "127.0.0.1",
        "--port", str(HARNESS_WEB_PORT),
        "--no-open",
    ]


def _start_harness_web(internal_token: str) -> subprocess.Popen[Any]:
    HARNESS_WEB_HOME.mkdir(parents=True, exist_ok=True)
    HARNESS_PRODUCT_WORKSPACE.mkdir(parents=True, exist_ok=True)
    HARNESS_RUNTIME_PRESET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HARNESS_AGENT_PRESETS / "math-notebook" / "agent.cordis.yml", HARNESS_RUNTIME_PRESET)
    HARNESS_WEB_STDOUT.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({
        "DSH_HOME": str(HARNESS_WEB_HOME),
        "LZLM_HARNESS_WORKSPACE_ROOT": str(HARNESS_PRODUCT_WORKSPACE),
        "LZLM_HARNESS_INTERNAL_TOKEN": internal_token,
        "LZLM_PRODUCT_ORIGIN": "http://127.0.0.1:8000",
        "DSH_PERMISSION_MODE": "read-only",
        "DSH_TELEMETRY_DISABLED": "1",
    })
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with HARNESS_WEB_STDOUT.open("a", encoding="utf-8") as stdout, HARNESS_WEB_STDERR.open("a", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            _harness_web_command(),
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"DeepSeek Harness Web exited during startup; see {HARNESS_WEB_STDERR}")
        try:
            with socket.create_connection(("127.0.0.1", HARNESS_WEB_PORT), timeout=0.2):
                return process
        except OSError:
            time.sleep(0.1)
    process.terminate()
    raise RuntimeError(f"DeepSeek Harness Web did not become ready; see {HARNESS_WEB_STDERR}")


def _stop_harness_web(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def serve(
    host: str,
    port: int,
    *,
    enable_codex_model: bool = False,
    enable_harness_model: bool = False,
    enable_harness_ui: bool = False,
) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("local simulation may only bind to localhost")
    start()
    from services.web_app import CodexNotebookModel, HarnessRuntimeAdapter, NotebookAsgiApp
    from services.web_auth import (
        AuthConfig,
        InMemoryCaptchaVerifier,
        MySqlRegistrationStore,
        RegistrationService,
    )
    from services.web_domain import MySqlDomainStore, NotebookService
    from services.web_ops import MySqlOperationsStore, OperationsService
    import uvicorn

    sender = ConsoleSmsSender()
    service = RegistrationService(
        store=MySqlRegistrationStore(_connection_factory()),
        sms_sender=sender,
        captcha_verifier=InMemoryCaptchaVerifier({"local-captcha"}),
        secret_pepper=base64.b64decode(_load_secrets()["auth_pepper_b64"]),
        config=AuthConfig(captcha_after_phone_day=99, captcha_after_ip_hour=99),
    )
    notebook = NotebookService(MySqlDomainStore(_connection_factory()), RUNTIME / "quarantine")
    operations = OperationsService(MySqlOperationsStore(_connection_factory()))
    harness_internal_token = secrets.token_urlsafe(32) if enable_harness_ui else None
    if enable_codex_model and enable_harness_model:
        raise RuntimeError("choose either the Harness model or the legacy Codex model")
    if enable_harness_model:
        harness = HarnessRuntimeAdapter.from_environment(ROOT)
        model_runner = CodexNotebookModel(
            RUNTIME / "model-candidates",
            review=harness.run_structured_turn,
            harness_review=harness.run_structured_turn,
            conversation_review=harness.run_conversation_turn,
            history_reader=harness.read_history,
            compactor=harness.compact,
        )
    else:
        model_runner = CodexNotebookModel(RUNTIME / "model-candidates") if enable_codex_model else None
    app = NotebookAsgiApp(
        service,
        notebook,
        allowed_hosts={"127.0.0.1", "localhost"},
        require_https=False,
        model_runner=model_runner,
        harness_internal_token=harness_internal_token,
        operations=operations,
    )
    app.resume_pending_deletions()
    harness_web = _start_harness_web(harness_internal_token) if harness_internal_token else None
    try:
        uvicorn.run(LocalOtpDisclosureApp(app, sender), host=host, port=port, access_log=False)
    finally:
        _stop_harness_web(harness_web)


def doctor() -> dict[str, Any]:
    ready_migrations = set(READY.read_text(encoding="utf-8").splitlines()) if READY.is_file() else set()
    checks = {
        "mysqld": _binary("mysqld").is_file(),
        "mysql": _binary("mysql").is_file(),
        "migration": all(migration.is_file() for migration in MIGRATIONS),
        "initialized": DATA.is_dir() and any(DATA.iterdir()),
        "schema_ready": {migration.name for migration in MIGRATIONS} <= ready_migrations and _live_schema_ready(),
        "secrets": SECRETS.is_file(),
        "client_configs": ROOT_CLIENT.is_file() and APP_CLIENT.is_file(),
        "running": _is_running(),
    }
    try:
        import pymysql  # noqa: F401
        import uvicorn  # noqa: F401
        checks["python_dependencies"] = True
    except ImportError:
        checks["python_dependencies"] = False
    return {"status": "ok" if all(checks.values()) else "incomplete", "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the localhost-only Web registration environment")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "init", "migrate", "start", "stop", "status", "smoke"):
        sub.add_parser(name)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--enable-codex-model", action="store_true")
    serve_parser.add_argument("--enable-harness-model", action="store_true")
    serve_parser.add_argument("--enable-harness-ui", action="store_true")
    grant_admin_parser = sub.add_parser("grant-admin")
    grant_admin_parser.add_argument("--phone-last4", required=True)
    grant_admin_parser.add_argument("--role", choices=("operations", "reviewer", "security", "administrator"), default="administrator")
    args = parser.parse_args()
    try:
        if args.command == "init":
            init()
            result: Any = doctor()
        elif args.command == "migrate":
            start()
            _apply_migrations()
            result = doctor()
        elif args.command == "start":
            start()
            result = doctor()
        elif args.command == "stop":
            stop()
            result = {"status": "ok", "running": _is_running()}
        elif args.command == "status":
            result = doctor()
        elif args.command == "smoke":
            result = smoke()
        elif args.command == "serve":
            serve(
                args.host,
                args.port,
                enable_codex_model=args.enable_codex_model,
                enable_harness_model=args.enable_harness_model,
                enable_harness_ui=args.enable_harness_ui,
            )
            return 0
        elif args.command == "grant-admin":
            result = grant_admin(args.phone_last4, args.role)
        else:
            result = doctor()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result.get("status") == "ok" else 2
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
