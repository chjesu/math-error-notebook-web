"""Run the registration service against a private local MySQL 8 instance."""

from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
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
)


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
        _run_sql(
            migration.read_text(encoding="utf-8"),
            root=True,
            database=True,
            label=f"apply {name}",
        )
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


async def _asgi_call(app: Any, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, str], dict]:
    body = json.dumps(payload).encode("utf-8")
    requests = [{"type": "http.request", "body": body, "more_body": False}]
    responses: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return requests.pop(0)

    async def send(message: dict[str, Any]) -> None:
        responses.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "scheme": "https",
            "client": ("198.51.100.10", 12345),
            "headers": [
                (b"host", b"local.test"),
                (b"content-type", b"application/json"),
                (b"x-device-id", b"local-smoke-device"),
            ],
        },
        receive,
        send,
    )
    started, finished = responses
    headers = {key.decode("ascii"): value.decode("ascii") for key, value in started["headers"]}
    return started["status"], headers, json.loads(finished["body"])


def _service():
    from services.web_auth import (
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
    )
    return service, sender


def smoke() -> dict[str, Any]:
    from services.web_auth import AuthAsgiApp, AuthConfig
    from services.web_auth.registration import SendCodeStatus

    start()
    _clear_test_data()
    service, sender = _service()
    app = AuthAsgiApp(service, allowed_hosts={"local.test"})
    phone = f"139{secrets.randbelow(100_000_000):08d}"
    requested = asyncio.run(_asgi_call(app, "/v1/auth/otp/request", {"phone": phone}))
    if requested[0] != 202 or len(sender.deliveries) != 1:
        raise RuntimeError("OTP request smoke test failed")
    verified = asyncio.run(
        _asgi_call(
            app,
            "/v1/auth/otp/verify",
            {
                "challenge_token": requested[2]["challenge_token"],
                "phone": phone,
                "code": sender.deliveries[0][1],
            },
        )
    )
    if verified[0] != 200 or "set-cookie" not in verified[1]:
        raise RuntimeError("OTP verification smoke test failed")
    replay = asyncio.run(
        _asgi_call(
            app,
            "/v1/auth/otp/verify",
            {
                "challenge_token": requested[2]["challenge_token"],
                "phone": phone,
                "code": sender.deliveries[0][1],
            },
        )
    )
    if replay[0] != 400:
        raise RuntimeError("OTP replay protection smoke test failed")

    _clear_test_data()
    concurrent_service, concurrent_sender = _service()
    concurrent_phone = f"138{secrets.randbelow(100_000_000):08d}"

    def request_once(_: int):
        return concurrent_service.request_code(
            phone=concurrent_phone,
            ip_address="198.51.100.20",
            device_id="local-concurrent-device",
        ).status

    with ThreadPoolExecutor(max_workers=20) as executor:
        concurrent_statuses = list(executor.map(request_once, range(50)))
    if concurrent_statuses.count(SendCodeStatus.ACCEPTED) != 1 or len(concurrent_sender.deliveries) != 1:
        raise RuntimeError("concurrent SMS reservation smoke test failed")

    _clear_test_data()
    ip_service, ip_sender = _service()
    ip_service.config = AuthConfig(captcha_after_phone_day=99, captcha_after_ip_hour=99)
    first_number = secrets.randbelow(99_999_989)
    ip_statuses = [
        ip_service.request_code(
            phone=f"137{first_number + index:08d}",
            ip_address="198.51.100.30",
            device_id=f"local-ip-limit-device-{index}",
        ).status
        for index in range(11)
    ]
    if ip_statuses[:10] != [SendCodeStatus.ACCEPTED] * 10 or ip_statuses[10] is not SendCodeStatus.RETRY_LATER:
        raise RuntimeError("IP minute limit smoke test failed")
    if len(ip_sender.deliveries) != 10:
        raise RuntimeError("IP minute limit called the SMS sender too many times")
    return {
        "status": "ok",
        "mysql": f"127.0.0.1:{PORT}/{DATABASE}",
        "otp_request": requested[0],
        "otp_verify": verified[0],
        "otp_replay": replay[0],
        "secure_cookie": "Secure" in verified[1]["set-cookie"],
        "concurrent_requests": 50,
        "concurrent_provider_sends": len(concurrent_sender.deliveries),
        "ip_minute_requests": 11,
        "ip_minute_provider_sends": len(ip_sender.deliveries),
    }


class ConsoleSmsSender:
    """Local-only sender: show the simulated OTP in the foreground terminal."""

    def send_verification(self, phone: str, code: str, ttl_seconds: int) -> str:
        print(f"[LOCAL SMS] {phone[:3]}****{phone[-4:]} code={code} ttl={ttl_seconds}s", flush=True)
        return f"local-{secrets.token_hex(8)}"


def serve(host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("local simulation may only bind to localhost")
    start()
    from services.web_auth import (
        AuthAsgiApp,
        InMemoryCaptchaVerifier,
        MySqlRegistrationStore,
        RegistrationService,
    )
    import uvicorn

    service = RegistrationService(
        store=MySqlRegistrationStore(_connection_factory()),
        sms_sender=ConsoleSmsSender(),
        captcha_verifier=InMemoryCaptchaVerifier({"local-captcha"}),
        secret_pepper=base64.b64decode(_load_secrets()["auth_pepper_b64"]),
    )
    app = AuthAsgiApp(
        service,
        allowed_hosts={"127.0.0.1", "localhost"},
        require_https=False,
    )
    uvicorn.run(app, host=host, port=port, access_log=False)


def doctor() -> dict[str, Any]:
    ready_migrations = set(READY.read_text(encoding="utf-8").splitlines()) if READY.is_file() else set()
    checks = {
        "mysqld": _binary("mysqld").is_file(),
        "mysql": _binary("mysql").is_file(),
        "migration": all(migration.is_file() for migration in MIGRATIONS),
        "initialized": DATA.is_dir() and any(DATA.iterdir()),
        "schema_ready": {migration.name for migration in MIGRATIONS} <= ready_migrations,
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
            serve(args.host, args.port)
            return 0
        else:
            result = doctor()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result.get("status") == "ok" else 2
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
