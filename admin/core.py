#!/usr/bin/env python3
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import select
import secrets
import socket
import sqlite3
import smtplib
import subprocess
import ssl
import threading
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE = Path(os.environ.get("SMS_BASE", "/htdocs/sms"))
CONF = BASE / "conf"
DATA = BASE / "data"
LOGS = BASE / "logs"
DB = DATA / "sms.sqlite"
GAMMU_DB = DATA / "gammu-smsd.sqlite"
ADMIN_ENV = CONF / "admin.env"
SMSDRC = CONF / "gammu-smsdrc"
MSMTPRC = CONF / "msmtprc"
MAIL_ENV = CONF / "sms-forward-mail.env"
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"

STATUSES = {
    "queued": ("排队中", "info"),
    "retry_wait": ("等待重试", "warn"),
    "sending": ("发送中", "warn"),
    "dispatching": ("等待 PVE 发送", "warn"),
    "submitted": ("已提交", "warn"),
    "sent": ("已发送", "ok"),
    "failed": ("失败", "bad"),
    "send_timeout": ("发送超时", "bad"),
    "ambiguous": ("状态不明", "bad"),
    "cancelled": ("已取消", "muted"),
}

SEND_TYPES = {
    "normal": ("普通发送", "info"),
    "plan": ("计划发送", "warn"),
}

LEGACY_DEFAULT_SETTINGS = {
    "send_interval_seconds": "60",
    "retry_interval_seconds": "600",
    "max_retries": "1",
    "command_timeout_seconds": "45",
}

MAIL_DEFAULT_SETTINGS = {
    "mail_enabled": "0",
    "mail_to": "",
    "mail_from": "sms-gateway@localhost",
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_security": "ssl",
}

DEFAULT_STRATEGY = {
    "name": "默认策略",
    "send_interval_minutes": "1",
    "retry_interval_minutes": "10",
    "max_retries": "1",
    "command_timeout_seconds": "45",
}

TIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"
ADMIN_TIME_DISPLAY_FORMAT = "%Y/%m/%d %H:%M:%S"
ADMIN_MINUTE_DISPLAY_FORMAT = "%Y/%m/%d %H:%M"
SEND_INTERVAL_MAX_MINUTES = 180 * 24 * 60
RETRY_INTERVAL_MAX_MINUTES = 10080
SIM_STATUS_STREAM_SECONDS = 60
SIM_STATUS_STREAM_INTERVAL_SECONDS = 1
SMSD_HEALTH_LOG_MINUTES = 10
SUBMITTED_SEND_TIMEOUT_MINUTES = 10
SPOOL = BASE / "spool"
OUTBOX = SPOOL / "outbox"
SENT = SPOOL / "sent"
ERROR = SPOOL / "error"


def now_text() -> str:
    return dt.datetime.now().astimezone().strftime(TIME_FORMAT)


def format_admin_time(value) -> str:
    if isinstance(value, dt.datetime):
        return value.strftime(ADMIN_TIME_DISPLAY_FORMAT)
    text = str(value or "").strip()
    if not text:
        return "-"
    for fmt in (TIME_FORMAT, "%Y-%m-%d %H:%M:%S", ADMIN_TIME_DISPLAY_FORMAT):
        try:
            return dt.datetime.strptime(text, fmt).strftime(ADMIN_TIME_DISPLAY_FORMAT)
        except Exception:
            pass
    if len(text) >= 19 and text[4] == "-" and text[7] == "-" and text[10] in {" ", "T"}:
        return f"{text[0:4]}/{text[5:7]}/{text[8:10]} {text[11:19]}"
    if len(text) >= 19 and text[4] == "/" and text[7] == "/" and text[10] in {" ", "T"}:
        return f"{text[0:10]} {text[11:19]}"
    return text


def format_admin_minute_time(value) -> str:
    text = format_admin_time(value)
    if len(text) >= 16 and text[4] == "/" and text[7] == "/" and text[10] in {" ", "T"}:
        return f"{text[0:10]} {text[11:16]}"
    return text


def format_signal_strength(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    lower = text.lower()
    if "%" in text or "dbm" in lower or "asu" in lower or "percent" in lower:
        return text
    number = text[1:] if text.startswith("-") else text
    if number.isdigit():
        return f"{text}%"
    return text


def monitor_signal_percent_to_dbm(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value or "").strip().rstrip("%")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    percent = int(round(float(text)))
    if percent <= 0 or percent > 100 or percent == 255:
        return None
    rssi = int((percent * 31 + 50) // 100)
    return -113 + (2 * rssi)


def parse_signal_dbm(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        dbm = int(round(value))
        return dbm if -140 <= dbm <= -20 else None
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm\b", text, re.I)
    if not match:
        return None
    dbm = int(round(float(match.group(1))))
    return dbm if -140 <= dbm <= -20 else None


def signal_dbm_from_status(status: dict) -> int | None:
    dbm = parse_signal_dbm(status.get("signal_dbm"))
    if dbm is not None:
        return dbm
    return parse_signal_dbm(status.get("signal"))


def signal_quality_for_dbm(dbm: int | None) -> tuple[str, str]:
    if dbm is None:
        return "", ""
    if dbm >= -79:
        return "极强/强", "ok"
    if dbm >= -89:
        return "良好", "good"
    if dbm >= -99:
        return "一般", "warn"
    return "较弱", "bad"


def parse_env(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def use_pve_dispatch() -> bool:
    return os.environ.get("SMS_DISPATCH_MODE", "").strip().lower() == "pve"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_password(password: str, encoded: str) -> bool:
    try:
        alg, rounds_text, salt_text, digest_text = encoded.split("$", 3)
        if alg != "pbkdf2_sha256":
            return False
        rounds = int(rounds_text)
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected = base64.b64decode(digest_text.encode("ascii"))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


def hash_password(password: str) -> str:
    rounds = 260000
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2_sha256$%d$%s$%s" % (
        rounds,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def write_admin_env(cfg: dict) -> None:
    bind_host = cfg.get("BIND_HOST", "0.0.0.0")
    port = cfg.get("PORT", "8088")
    content = "\n".join(
        [
            f"ADMIN_PASSWORD_HASH={cfg['ADMIN_PASSWORD_HASH']}",
            f"SESSION_SECRET={cfg['SESSION_SECRET']}",
            f"BIND_HOST={bind_host}",
            f"PORT={port}",
            "",
        ]
    )
    tmp = ADMIN_ENV.with_name("admin.env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, ADMIN_ENV)
    os.chmod(ADMIN_ENV, 0o600)


def database_url() -> str:
    return os.environ.get("SMS_DATABASE_URL", "").strip() or os.environ.get("DATABASE_URL", "").strip()


def using_postgres() -> bool:
    return bool(database_url())


def translate_placeholders(sql: str) -> str:
    return sql.replace("?", "%s")


def add_returning_id(sql: str) -> str:
    upper = " ".join(sql.upper().split())
    insert_tables = (
        "INSERT INTO INBOUND_SMS",
        "INSERT INTO OUTBOUND_SMS",
        "INSERT INTO SEND_STRATEGIES",
        "INSERT INTO SEND_PLANS",
        "INSERT INTO SMS_DISPATCH_JOBS",
    )
    if upper.startswith(insert_tables) and " RETURNING " not in upper:
        return sql.rstrip().rstrip(";") + " RETURNING id"
    return sql


class PgResult:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.lastrowid = None
        if cursor.description:
            first = cursor.fetchone()
            if first is not None:
                self._first = first
                if "id" in first:
                    self.lastrowid = first["id"]
            else:
                self._first = None
        else:
            self._first = None

    def fetchone(self):
        if self._first is not None:
            first = self._first
            self._first = None
            return first
        return self.cursor.fetchone()

    def fetchall(self):
        if self._first is not None:
            first = self._first
            self._first = None
            return [first] + self.cursor.fetchall()
        return self.cursor.fetchall()


class PgConnection:
    def __init__(self, url: str):
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:
            raise RuntimeError("python3-psycopg2 is required when SMS_DATABASE_URL is set") from exc
        self._psycopg2 = psycopg2
        self._extras = psycopg2.extras
        self.conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self.conn.cursor()
        cur.execute(translate_placeholders(add_returning_id(sql)), tuple(params or ()))
        return PgResult(cur)

    def cursor(self):
        return self.conn.cursor()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()


def db():
    if using_postgres():
        return PgConnection(database_url())
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_postgres_migration_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def run_postgres_migrations(conn) -> None:
    ensure_postgres_migration_table(conn)
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in applied:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))


def seconds_value_to_minutes(value: str, default_seconds: int, minimum_minutes: int) -> int:
    try:
        seconds = int(value)
    except Exception:
        seconds = default_seconds
    seconds = max(0, seconds)
    if seconds == 0:
        return 0 if minimum_minutes == 0 else minimum_minutes
    return max(minimum_minutes, (seconds + 59) // 60)


def legacy_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def ensure_default_strategy(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM send_strategies WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
    if row:
        return row
    existing = conn.execute("SELECT * FROM send_strategies ORDER BY id LIMIT 1").fetchone()
    if existing:
        conn.execute("UPDATE send_strategies SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END", (existing["id"],))
        return conn.execute("SELECT * FROM send_strategies WHERE id = ?", (existing["id"],)).fetchone()

    send_minutes = seconds_value_to_minutes(
        legacy_setting(conn, "send_interval_seconds", LEGACY_DEFAULT_SETTINGS["send_interval_seconds"]),
        int(LEGACY_DEFAULT_SETTINGS["send_interval_seconds"]),
        0,
    )
    retry_minutes = seconds_value_to_minutes(
        legacy_setting(conn, "retry_interval_seconds", LEGACY_DEFAULT_SETTINGS["retry_interval_seconds"]),
        int(LEGACY_DEFAULT_SETTINGS["retry_interval_seconds"]),
        1,
    )
    max_retries = parse_positive_int(
        legacy_setting(conn, "max_retries", LEGACY_DEFAULT_SETTINGS["max_retries"]),
        int(DEFAULT_STRATEGY["max_retries"]),
        0,
        10,
    )
    timeout = parse_positive_int(
        legacy_setting(conn, "command_timeout_seconds", LEGACY_DEFAULT_SETTINGS["command_timeout_seconds"]),
        int(DEFAULT_STRATEGY["command_timeout_seconds"]),
        10,
        180,
    )
    created = now_text()
    cur = conn.execute(
        """
        INSERT INTO send_strategies
          (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
           active, is_default, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
        """,
        (DEFAULT_STRATEGY["name"], send_minutes, retry_minutes, max_retries, timeout, created, created),
    )
    return conn.execute("SELECT * FROM send_strategies WHERE id = ?", (cur.lastrowid,)).fetchone()


def backfill_outbound_strategy(conn: sqlite3.Connection) -> None:
    default_strategy = ensure_default_strategy(conn)
    rows = conn.execute(
        """
        SELECT id, strategy_id, strategy_name, retry_interval_seconds
        FROM outbound_sms
        WHERE strategy_id IS NULL OR strategy_name = ''
        """
    ).fetchall()
    for row in rows:
        retry_minutes = seconds_value_to_minutes(
            str(row["retry_interval_seconds"]),
            int(LEGACY_DEFAULT_SETTINGS["retry_interval_seconds"]),
            1,
        )
        conn.execute(
            """
            UPDATE outbound_sms
            SET strategy_id = ?, strategy_name = ?, send_type = COALESCE(NULLIF(send_type, ''), 'normal'),
                send_interval_minutes = ?, retry_interval_minutes = ?, command_timeout_seconds = ?
            WHERE id = ?
            """,
            (
                default_strategy["id"],
                default_strategy["name"],
                default_strategy["send_interval_minutes"],
                retry_minutes,
                default_strategy["command_timeout_seconds"],
                row["id"],
            ),
        )


def ensure_mail_settings(conn: sqlite3.Connection) -> None:
    for key, value in MAIL_DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )


def init_db() -> None:
    if using_postgres():
        with db() as conn:
            run_postgres_migrations(conn)
            for key, value in LEGACY_DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                    (key, value),
                )
            ensure_mail_settings(conn)
            ensure_default_strategy(conn)
            backfill_outbound_strategy(conn)
        return

    DATA.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_sms (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              received_at TEXT NOT NULL,
              sender TEXT NOT NULL DEFAULT '',
              text TEXT NOT NULL DEFAULT '',
              raw_ids TEXT NOT NULL DEFAULT '',
              phone_id TEXT NOT NULL DEFAULT '',
              sms_messages INTEGER NOT NULL DEFAULT 0,
              decoded_parts INTEGER NOT NULL DEFAULT 0,
              forwarded INTEGER NOT NULL DEFAULT 0,
              forward_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbound_sms (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              destination TEXT NOT NULL,
              text TEXT NOT NULL,
              scheduled_at TEXT NOT NULL,
              next_attempt_at TEXT NOT NULL,
              status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              max_retries INTEGER NOT NULL DEFAULT 0,
              retry_interval_seconds INTEGER NOT NULL DEFAULT 600,
              submitted_at TEXT,
              last_error TEXT NOT NULL DEFAULT '',
              command_output TEXT NOT NULL DEFAULT '',
              final_notified INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_strategies (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              send_interval_minutes INTEGER NOT NULL DEFAULT 1,
              retry_interval_minutes INTEGER NOT NULL DEFAULT 10,
              max_retries INTEGER NOT NULL DEFAULT 1,
              command_timeout_seconds INTEGER NOT NULL DEFAULT 45,
              active INTEGER NOT NULL DEFAULT 1,
              is_default INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS send_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              name TEXT NOT NULL,
              destination TEXT NOT NULL,
              country_code TEXT NOT NULL DEFAULT '',
              phone_number TEXT NOT NULL DEFAULT '',
              text TEXT NOT NULL,
              scheduled_at TEXT NOT NULL,
              strategy_id INTEGER NOT NULL,
              strategy_name TEXT NOT NULL,
              outbound_id INTEGER,
              active INTEGER NOT NULL DEFAULT 1,
              archived INTEGER NOT NULL DEFAULT 0,
              deleted INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sms_dispatch_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              idempotency_key TEXT NOT NULL UNIQUE,
              outbound_id INTEGER NOT NULL,
              attempt_no INTEGER NOT NULL,
              status TEXT NOT NULL,
              destination TEXT NOT NULL DEFAULT '',
              text TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              lease_until TEXT,
              cancel_requested INTEGER NOT NULL DEFAULT 0,
              checked_outbound_status TEXT NOT NULL DEFAULT '',
              checked_at TEXT,
              result_status TEXT NOT NULL DEFAULT '',
              result_message TEXT NOT NULL DEFAULT '',
              submitted_at TEXT,
              completed_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_received_at ON inbound_sms(received_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_status_due ON outbound_sms(status, next_attempt_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_plans_outbound ON send_plans(outbound_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_status ON sms_dispatch_jobs(status, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dispatch_outbound ON sms_dispatch_jobs(outbound_id)")
        for key, value in LEGACY_DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        ensure_mail_settings(conn)
        ensure_column(conn, "outbound_sms", "send_type", "TEXT NOT NULL DEFAULT 'normal'")
        ensure_column(conn, "outbound_sms", "strategy_id", "INTEGER")
        ensure_column(conn, "outbound_sms", "strategy_name", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "outbound_sms", "plan_id", "INTEGER")
        ensure_column(conn, "outbound_sms", "send_interval_minutes", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "outbound_sms", "retry_interval_minutes", "INTEGER NOT NULL DEFAULT 10")
        ensure_column(conn, "outbound_sms", "command_timeout_seconds", "INTEGER NOT NULL DEFAULT 45")
        ensure_column(conn, "outbound_sms", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "outbound_sms", "cancel_requested_at", "TEXT")
        ensure_column(conn, "outbound_sms", "dispatch_job_id", "INTEGER")
        ensure_column(conn, "send_plans", "archived", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "send_plans", "deleted", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "send_plans", "country_code", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "send_plans", "phone_number", "TEXT NOT NULL DEFAULT ''")
        ensure_default_strategy(conn)
        backfill_outbound_strategy(conn)


def get_settings() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    settings = LEGACY_DEFAULT_SETTINGS.copy()
    settings.update({row["key"]: row["value"] for row in rows})
    return settings


def get_mail_settings() -> dict:
    keys = tuple(MAIL_DEFAULT_SETTINGS.keys())
    placeholders = ",".join("?" for _ in keys)
    with db() as conn:
        rows = conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({placeholders})",
            keys,
        ).fetchall()
    settings = MAIL_DEFAULT_SETTINGS.copy()
    settings.update({row["key"]: row["value"] for row in rows if row["key"] in MAIL_DEFAULT_SETTINGS})
    return settings


def save_mail_settings(form: dict) -> None:
    existing = get_mail_settings()
    enabled = "1" if str(form.get("mail_enabled", "")).lower() in {"1", "yes", "true", "on"} else "0"
    values = {
        "mail_enabled": enabled,
        "mail_to": form.get("mail_to", "").strip(),
        "mail_from": form.get("mail_from", "").strip() or "sms-gateway@localhost",
        "smtp_host": form.get("smtp_host", "").strip(),
        "smtp_port": str(parse_positive_int(form.get("smtp_port", "465"), 465, 1, 65535)),
        "smtp_user": form.get("smtp_user", "").strip(),
        "smtp_password": form.get("smtp_password", ""),
        "smtp_security": form.get("smtp_security", "ssl").strip().lower(),
    }
    if values["smtp_security"] not in {"ssl", "starttls", "none"}:
        raise ValueError("SMTP 加密方式不正确")
    if not values["smtp_password"]:
        values["smtp_password"] = existing.get("smtp_password", "")
    if enabled == "1":
        missing = []
        if not values["mail_to"]:
            missing.append("收件人")
        if not values["mail_from"]:
            missing.append("发件人")
        if not values["smtp_host"]:
            missing.append("SMTP 主机")
        if missing:
            raise ValueError("启用邮件时必须填写：" + "、".join(missing))
    with db() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def set_settings(values: dict) -> None:
    with db() as conn:
        for key in LEGACY_DEFAULT_SETTINGS:
            if key in values:
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(values[key])),
                )


def get_state(key: str) -> str:
    with db() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def set_state(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def parse_positive_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        num = int(value)
    except Exception:
        return default
    return max(minimum, min(maximum, num))
