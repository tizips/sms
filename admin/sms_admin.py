#!/usr/bin/env python3
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.parse
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

STATUSES = {
    "queued": ("排队中", "info"),
    "retry_wait": ("等待重试", "warn"),
    "sending": ("发送中", "warn"),
    "submitted": ("已提交", "ok"),
    "failed": ("失败", "bad"),
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

DEFAULT_STRATEGY = {
    "name": "默认策略",
    "send_interval_minutes": "1",
    "retry_interval_minutes": "10",
    "max_retries": "1",
    "command_timeout_seconds": "45",
}

TIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"
SEND_INTERVAL_MAX_MINUTES = 180 * 24 * 60
RETRY_INTERVAL_MAX_MINUTES = 10080
SIM_STATUS_STREAM_SECONDS = 60


def now_text() -> str:
    return dt.datetime.now().astimezone().strftime(TIME_FORMAT)


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


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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


def init_db() -> None:
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_received_at ON inbound_sms(received_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_status_due ON outbound_sms(status, next_attempt_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_send_plans_outbound ON send_plans(outbound_id)")
        for key, value in LEGACY_DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        ensure_column(conn, "outbound_sms", "send_type", "TEXT NOT NULL DEFAULT 'normal'")
        ensure_column(conn, "outbound_sms", "strategy_id", "INTEGER")
        ensure_column(conn, "outbound_sms", "strategy_name", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "outbound_sms", "plan_id", "INTEGER")
        ensure_column(conn, "outbound_sms", "send_interval_minutes", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "outbound_sms", "retry_interval_minutes", "INTEGER NOT NULL DEFAULT 10")
        ensure_column(conn, "outbound_sms", "command_timeout_seconds", "INTEGER NOT NULL DEFAULT 45")
        ensure_column(conn, "send_plans", "archived", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "send_plans", "deleted", "INTEGER NOT NULL DEFAULT 0")
        ensure_default_strategy(conn)
        backfill_outbound_strategy(conn)


def get_settings() -> dict:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    settings = LEGACY_DEFAULT_SETTINGS.copy()
    settings.update({row["key"]: row["value"] for row in rows})
    return settings


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


def normalize_destination(value: str) -> str:
    text = value.strip().replace(" ", "")
    if text.startswith("+"):
        body = text[1:]
    else:
        body = text
    if not body.isdigit() or len(body) < 5 or len(body) > 20:
        raise ValueError("号码格式不正确，只支持 5-20 位数字，可带 + 前缀")
    return text


def normalize_country_code(value: str) -> str:
    text = (value.strip() or "+86").replace(" ", "")
    if not text.startswith("+"):
        text = f"+{text}"
    body = text[1:]
    if not body.isdigit() or len(body) < 1 or len(body) > 4:
        raise ValueError("区号格式不正确，只支持 1-4 位数字")
    return text


def destination_from_form(form: dict) -> str:
    destination = form.get("destination", "").strip()
    if destination.replace(" ", "").startswith("+"):
        return normalize_destination(destination)
    return normalize_destination(normalize_country_code(form.get("country_code", "+86")) + destination)


def estimate_segments(text: str) -> tuple[int, str]:
    if not text:
        return 0, "empty"
    gsm7_chars = (
        "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ"
        "\u001bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    )
    if all(ch in gsm7_chars for ch in text):
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        single, multi, encoding = 70, 67, "Unicode"
    count = len(text)
    if count <= single:
        return 1, encoding
    return (count + multi - 1) // multi, encoding


def shell_quote(text: str) -> str:
    return html.escape(text, quote=True)


def status_badge(status: str) -> str:
    label, tone = STATUSES.get(status, (status, "muted"))
    return f'<span class="badge {tone}">{html.escape(label)}</span>'


def send_type_badge(send_type: str) -> str:
    label, tone = SEND_TYPES.get(send_type, (send_type or "普通发送", "muted"))
    return f'<span class="badge {tone}">{html.escape(label)}</span>'


def describe_sim_state(imsi: str = "", seen_status: bool = False) -> str:
    if imsi:
        return "已识别（IMSI 可用）"
    if seen_status:
        return "未识别（无 IMSI）"
    return "状态未知"


def describe_network_switch(signal: str = "", network_level: str = "", network_state: str = "") -> tuple[str, str]:
    # This SMS gateway must keep mobile data disabled; signal/registration is not data usage.
    return "数据关闭", "ok"


def mask_sim_identity(imsi: str = "") -> str:
    digits = "".join(ch for ch in imsi if ch.isdigit())
    if len(digits) < 10:
        return ""
    return f"{digits[:5]}******{digits[-4:]}"


def format_sms_counts(sent: str = "", received: str = "", failed: str = "") -> str:
    if not (sent or received or failed):
        return ""
    return f"已发 {sent or '0'} / 已收 {received or '0'} / 失败 {failed or '0'}"


def parse_sim_status_output(output: str) -> dict:
    info = {
        "sim_state": "状态未知",
        "sim_identity": "",
        "sms_counts": "",
        "signal": "",
        "network_level": "",
        "network_state": "",
        "network_switch": "未知",
        "network_switch_tone": "muted",
        "operator": "",
        "error": "",
    }
    imsi = ""
    network_code = ""
    for raw in output.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "signal strength":
            info["signal"] = value
        elif key == "network level":
            info["network_level"] = value
        elif key == "network state":
            info["network_state"] = value
        elif key == "name in phone":
            info["operator"] = value
        elif key == "network":
            network_code = value
        elif key == "sim imsi":
            imsi = value
    info["sim_state"] = describe_sim_state(imsi, bool(output.strip()))
    info["sim_identity"] = mask_sim_identity(imsi)
    switch, switch_tone = describe_network_switch(info["signal"], info["network_level"], info["network_state"])
    info["network_switch"] = switch
    info["network_switch_tone"] = switch_tone
    if not info["operator"]:
        info["operator"] = infer_operator(network_code, imsi) or network_code
    return info


def infer_operator(network_code: str = "", imsi: str = "") -> str:
    code = "".join(ch for ch in (network_code or imsi[:5]) if ch.isdigit())
    if code.startswith("460"):
        code = code[:5]
    operators = {
        "46000": "中国移动",
        "46002": "中国移动",
        "46004": "中国移动",
        "46007": "中国移动",
        "46008": "中国移动",
        "46001": "中国联通",
        "46006": "中国联通",
        "46009": "中国联通",
        "46010": "中国联通",
        "46003": "中国电信",
        "46005": "中国电信",
        "46011": "中国电信",
        "46012": "中国电信",
    }
    return operators.get(code, "")


def parse_smsd_monitor_output(output: str) -> dict:
    values = {}
    for raw in output.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        values[key.strip().lower()] = value.strip()

    signal = values.get("networksignal", "")
    battery = values.get("batterpercent", "")
    imsi = values.get("imsi", "")
    operator = values.get("netname", "") or values.get("netcode", "")
    sms_counts = format_sms_counts(values.get("sent", ""), values.get("received", ""), values.get("failed", ""))
    network_state = "smsd 运行中" if values else ""
    switch, switch_tone = describe_network_switch(signal, f"{signal}%" if signal else "", network_state)
    return {
        "sim_state": describe_sim_state(imsi, bool(values)),
        "sim_identity": mask_sim_identity(imsi),
        "sms_counts": sms_counts,
        "signal": signal,
        "network_level": f"{signal}%" if signal else "",
        "network_state": network_state,
        "network_switch": switch,
        "network_switch_tone": switch_tone,
        "operator": operator or infer_operator(operator, imsi),
        "battery": f"{battery}%" if battery else "",
        "error": "",
        "source_updated_at": "",
        "checked_at": now_text(),
    }


def read_smsd_monitor_status() -> dict | None:
    cmd = [
        "/usr/bin/gammu-smsd-monitor" if Path("/usr/bin/gammu-smsd-monitor").exists() else "gammu-smsd-monitor",
        "-c",
        str(SMSDRC),
        "-d",
        "0",
        "-n",
        "1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0 or not output:
        return None
    status = parse_smsd_monitor_output(output)
    if status.get("signal") or status.get("sim_state") == "已识别":
        return status
    return None


def render_status_pill(label: str, tone: str = "muted") -> str:
    safe_tone = tone if tone in {"ok", "bad", "warn", "muted", "info"} else "muted"
    return f'<span class="status-pill {safe_tone}">{html.escape(label or "未知")}</span>'


def render_network_level_value(status: dict) -> str:
    level = html.escape(str(status.get("network_level") or "-"))
    pill = render_status_pill(str(status.get("network_switch") or "未知"), str(status.get("network_switch_tone") or "muted"))
    return f'<span data-network-level-text>{level}</span>{pill}'


def render_sim_state_value(label: str, tone: str) -> str:
    safe_tone = tone if tone in {"ok", "bad", "warn", "muted", "info"} else "muted"
    escaped = html.escape(label)
    return f'<span class="status-pill {safe_tone}" data-sim-badge>{escaped}</span>'


def get_sim_status(use_cache: bool = False) -> dict:
    smsd_status = read_smsd_monitor_status()
    if smsd_status:
        return smsd_status

    cmd = ["/usr/bin/gammu" if Path("/usr/bin/gammu").exists() else "gammu", "-c", str(SMSDRC), "monitor", "1"]
    status = {
        "sim_state": "状态未知",
        "sim_identity": "",
        "sms_counts": "",
        "signal": "",
        "network_level": "",
        "network_state": "",
        "network_switch": "未知",
        "network_switch_tone": "muted",
        "operator": "",
        "battery": "",
        "error": "",
        "checked_at": now_text(),
    }
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode == 0:
            status.update(parse_sim_status_output(output))
        else:
            status["error"] = (output or f"gammu exited with {result.returncode}")[:300]
    except subprocess.TimeoutExpired:
        status["error"] = "读取 SIM 状态超时"
    except Exception as exc:
        status["error"] = str(exc)[:300]

    return status


def render_sim_status_panel() -> str:
    status = get_sim_status()
    tone = "ok" if not status.get("error") else "bad"
    sim_label = status.get("sim_state") or "未知"
    if status.get("error"):
        sim_label = "不可用"
    rows = [
        ("sim_state", "SIM 卡", render_sim_state_value(str(sim_label), tone)),
        ("sim_identity", "SIM 标识", html.escape(str(status.get("sim_identity") or "-"))),
        ("sms_counts", "短信计数", html.escape(str(status.get("sms_counts") or "-"))),
        ("signal", "信号强度", html.escape(str(status.get("signal") or "-"))),
        ("network_level", "网络强度", render_network_level_value(status)),
        ("operator", "运营商", html.escape(str(status.get("operator") or "-"))),
        ("checked_at", "检测时间", html.escape(str(status.get("checked_at") or "-"))),
    ]
    body = "".join(
        f'<tr><th>{html.escape(label)}</th><td data-sim-field="{html.escape(field)}">{value}</td></tr>'
        for field, label, value in rows
    )
    error = ""
    if status.get("error"):
        error = f'<div class="hint badtext" data-sim-error>状态读取失败：{html.escape(status["error"])}</div>'
    return (
        '<div id="sim-status-panel" class="panel band sim-status"><div class="sim-status-head"><h2>SIM 卡状态</h2>'
        '<button type="button" class="secondary sim-refresh" data-sim-refresh>刷新</button></div>'
        f'<table class="kv">{body}</table><div data-sim-error-slot>{error}</div></div>'
    )


def mail_config() -> dict:
    return parse_env(MAIL_ENV)


def send_notification(subject: str, plain: str, html_body: str = "") -> tuple[int, str]:
    cfg = mail_config()
    to_addr = cfg.get("SMS_FORWARD_TO", "")
    if not to_addr:
        return 0, "SMS_FORWARD_TO is not configured"
    from_addr = cfg.get("SMS_FORWARD_FROM", "sms-gateway@localhost")
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(plain, charset="utf-8")
    if html_body:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
    cmd = ["/usr/bin/msmtp", "-C", str(MSMTPRC), "-t"] if MSMTPRC.exists() else ["/usr/sbin/sendmail", "-t"]
    try:
        subprocess.run(cmd, input=msg.as_bytes(), timeout=30, check=True)
        return 1, ""
    except Exception as exc:
        return 0, str(exc)


def notify_final_failure(row: sqlite3.Row) -> None:
    subject = f"短信发送失败 #{row['id']} - {row['destination']}"
    plain = (
        "短信发送失败，已停止自动重试。\n\n"
        f"记录 ID: #{row['id']}\n"
        f"目标号码: {row['destination']}\n"
        f"尝试次数: {row['attempts']}\n"
        f"状态: {row['status']}\n"
        f"错误: {row['last_error']}\n\n"
        f"短信内容:\n{row['text']}\n"
    )
    html_body = (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;"
        "background:#f6f7f9;padding:24px;color:#1d2939;\">"
        "<div style=\"max-width:620px;margin:0 auto;background:#fff;border:1px solid #e4e7ec;border-radius:8px;overflow:hidden;\">"
        "<div style=\"background:#b42318;color:#fff;padding:18px 22px;font-size:20px;font-weight:700;\">短信发送失败</div>"
        "<div style=\"padding:20px 22px;font-size:14px;line-height:24px;\">"
        f"<p><b>记录 ID:</b> #{row['id']}</p>"
        f"<p><b>目标号码:</b> {shell_quote(row['destination'])}</p>"
        f"<p><b>尝试次数:</b> {row['attempts']}</p>"
        f"<p><b>错误:</b> {shell_quote(row['last_error'])}</p>"
        f"<pre style=\"white-space:pre-wrap;background:#f8fafc;border:1px solid #e4e7ec;border-radius:6px;padding:12px;\">{shell_quote(row['text'])}</pre>"
        "</div></div></div>"
    )
    ok, err = send_notification(subject, plain, html_body)
    with db() as conn:
        conn.execute(
            "UPDATE outbound_sms SET final_notified = ?, command_output = command_output || ? WHERE id = ?",
            (1 if ok else 0, f"\nnotify_error={err}" if err else "\nnotified=1", row["id"]),
        )


def due_time_from_form(value: str) -> str:
    if not value.strip():
        return now_text()
    parsed = dt.datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
    return parsed.astimezone().strftime(TIME_FORMAT)


def fetch_strategy(conn: sqlite3.Connection, strategy_id: int | None, active_only: bool = False) -> sqlite3.Row:
    if strategy_id is None:
        row = ensure_default_strategy(conn)
    else:
        query = "SELECT * FROM send_strategies WHERE id = ?"
        params: tuple = (strategy_id,)
        if active_only:
            query += " AND active = 1"
        row = conn.execute(query, params).fetchone()
    if not row:
        raise ValueError("发送策略不存在或已停用")
    return row


def list_strategies(active_only: bool = False) -> list[sqlite3.Row]:
    query = "SELECT * FROM send_strategies"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY is_default DESC, id ASC"
    with db() as conn:
        return conn.execute(query).fetchall()


def get_default_strategy() -> sqlite3.Row:
    with db() as conn:
        return ensure_default_strategy(conn)


def get_strategy(strategy_id: int | None, active_only: bool = False) -> sqlite3.Row:
    with db() as conn:
        return fetch_strategy(conn, strategy_id, active_only)


def strategy_from_form(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except Exception:
        raise ValueError("发送策略格式不正确")


def insert_outbound(
    conn: sqlite3.Connection,
    destination: str,
    text: str,
    scheduled_at: str,
    strategy: sqlite3.Row,
    send_type: str,
    plan_id: int | None = None,
) -> int:
    if send_type not in SEND_TYPES:
        raise ValueError("发送类型不正确")
    created = now_text()
    retry_minutes = int(strategy["retry_interval_minutes"])
    cur = conn.execute(
        """
        INSERT INTO outbound_sms
          (created_at, updated_at, destination, text, scheduled_at, next_attempt_at,
           status, attempts, max_retries, retry_interval_seconds, send_type, strategy_id,
           strategy_name, plan_id, send_interval_minutes, retry_interval_minutes, command_timeout_seconds)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            created,
            created,
            destination,
            text,
            scheduled_at,
            scheduled_at,
            int(strategy["max_retries"]),
            retry_minutes * 60,
            send_type,
            strategy["id"],
            strategy["name"],
            plan_id,
            int(strategy["send_interval_minutes"]),
            retry_minutes,
            int(strategy["command_timeout_seconds"]),
        ),
    )
    return int(cur.lastrowid)


def create_outbound(
    destination: str,
    text: str,
    scheduled_at: str,
    strategy_id: int | None = None,
    send_type: str = "normal",
    plan_id: int | None = None,
) -> int:
    with db() as conn:
        strategy = fetch_strategy(conn, strategy_id, active_only=True)
        return insert_outbound(conn, destination, text, scheduled_at, strategy, send_type, plan_id)


def create_immediate_outbound(destination: str, text: str, scheduled_at: str) -> int:
    created = now_text()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO outbound_sms
              (created_at, updated_at, destination, text, scheduled_at, next_attempt_at,
               status, attempts, max_retries, retry_interval_seconds, send_type, strategy_id,
               strategy_name, plan_id, send_interval_minutes, retry_interval_minutes, command_timeout_seconds)
            VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, 0, 0, 'normal', NULL, '', NULL, 0, 0, ?)
            """,
            (
                created,
                created,
                destination,
                text,
                scheduled_at,
                scheduled_at,
                int(DEFAULT_STRATEGY["command_timeout_seconds"]),
            ),
        )
        return int(cur.lastrowid)


def create_send_plan(name: str, destination: str, text: str, scheduled_at: str, strategy_id: int | None) -> int:
    created = now_text()
    with db() as conn:
        strategy = fetch_strategy(conn, strategy_id, active_only=True)
        plan_cur = conn.execute(
            """
            INSERT INTO send_plans
              (created_at, updated_at, name, destination, text, scheduled_at,
               strategy_id, strategy_name, outbound_id, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (created, created, name, destination, text, scheduled_at, strategy["id"], strategy["name"]),
        )
        plan_id = int(plan_cur.lastrowid)
        outbound_id = insert_outbound(conn, destination, text, scheduled_at, strategy, "plan", plan_id)
        conn.execute("UPDATE send_plans SET outbound_id = ?, updated_at = ? WHERE id = ?", (outbound_id, created, plan_id))
        return plan_id


def advance_plan_after_success(row: sqlite3.Row, submitted_at: str) -> None:
    if row["send_type"] != "plan" or not row["plan_id"]:
        return
    submitted_dt = parse_time(submitted_at) or dt.datetime.now().astimezone()
    with db() as conn:
        plan = conn.execute("SELECT * FROM send_plans WHERE id = ? AND active = 1", (row["plan_id"],)).fetchone()
        if not plan:
            return
        strategy = conn.execute(
            "SELECT * FROM send_strategies WHERE id = ? AND active = 1",
            (plan["strategy_id"],),
        ).fetchone()
        if not strategy:
            conn.execute("UPDATE send_plans SET active = 0, updated_at = ? WHERE id = ?", (submitted_at, plan["id"]))
            return
        send_interval = parse_positive_int(str(strategy["send_interval_minutes"]), 0, 0, SEND_INTERVAL_MAX_MINUTES)
        if send_interval <= 0:
            conn.execute("UPDATE send_plans SET active = 0, updated_at = ? WHERE id = ?", (submitted_at, plan["id"]))
            return
        next_scheduled = (submitted_dt + dt.timedelta(minutes=send_interval)).strftime(TIME_FORMAT)
        outbound_id = insert_outbound(
            conn,
            plan["destination"],
            plan["text"],
            next_scheduled,
            strategy,
            "plan",
            plan["id"],
        )
        conn.execute(
            """
            UPDATE send_plans
            SET outbound_id = ?, scheduled_at = ?, strategy_name = ?, active = 1, updated_at = ?
            WHERE id = ?
            """,
            (outbound_id, next_scheduled, strategy["name"], submitted_at, plan["id"]),
        )


def plan_has_send_history(conn: sqlite3.Connection, plan_id: int) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM outbound_sms
        WHERE plan_id = ?
          AND (attempts > 0 OR status IN ('submitted','failed','ambiguous','sending'))
        """,
        (plan_id,),
    ).fetchone()
    return bool(row and row["c"])


def delete_send_plan(plan_id: int) -> None:
    now = now_text()
    with db() as conn:
        plan = conn.execute("SELECT * FROM send_plans WHERE id = ? AND deleted = 0", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("发送计划不存在")
        if plan_has_send_history(conn, plan_id):
            raise ValueError("已有发送记录的计划不能删除，请使用归档")
        conn.execute(
            "UPDATE send_plans SET active = 0, archived = 0, deleted = 1, updated_at = ? WHERE id = ?",
            (now, plan_id),
        )
        if plan["outbound_id"]:
            conn.execute(
                "UPDATE outbound_sms SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN ('queued','retry_wait')",
                (now, plan["outbound_id"]),
            )


def archive_send_plan(plan_id: int) -> None:
    now = now_text()
    with db() as conn:
        plan = conn.execute("SELECT * FROM send_plans WHERE id = ? AND deleted = 0", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("发送计划不存在")
        conn.execute(
            "UPDATE send_plans SET active = 0, archived = 1, updated_at = ? WHERE id = ?",
            (now, plan_id),
        )
        if plan["outbound_id"]:
            conn.execute(
                "UPDATE outbound_sms SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN ('queued','retry_wait')",
                (now, plan["outbound_id"]),
            )


def strategy_options_html(selected_id: int | None = None) -> str:
    rows = list_strategies(active_only=True)
    if selected_id is None and rows:
        selected_id = next((row["id"] for row in rows if row["is_default"]), rows[0]["id"])
    options = []
    for row in rows:
        selected = " selected" if row["id"] == selected_id else ""
        suffix = " · 默认" if row["is_default"] else ""
        label = (
            f"{row['name']}{suffix} "
            f"({row['send_interval_minutes']} 分钟发送间隔 / {row['retry_interval_minutes']} 分钟重试)"
        )
        options.append(f'<option value="{row["id"]}"{selected}>{html.escape(label)}</option>')
    return "".join(options)


def save_strategy_from_form(form: dict) -> None:
    strategy_id = form.get("strategy_id", "").strip()
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("策略名称不能为空")
    if len(name) > 40:
        raise ValueError("策略名称最多 40 个字符")
    send_interval = parse_positive_int(form.get("send_interval_minutes", "1"), 1, 0, SEND_INTERVAL_MAX_MINUTES)
    retry_interval = parse_positive_int(form.get("retry_interval_minutes", "10"), 10, 1, RETRY_INTERVAL_MAX_MINUTES)
    max_retries = parse_positive_int(form.get("max_retries", "1"), 1, 0, 10)
    timeout = parse_positive_int(form.get("command_timeout_seconds", "45"), 45, 10, 180)
    active = 1 if form.get("active") == "yes" else 0
    make_default = form.get("is_default") == "yes"
    now = now_text()
    with db() as conn:
        if strategy_id:
            existing = conn.execute("SELECT * FROM send_strategies WHERE id = ?", (int(strategy_id),)).fetchone()
            if not existing:
                raise ValueError("发送策略不存在")
            if existing["is_default"] and not active:
                raise ValueError("默认策略不能停用")
            conn.execute(
                """
                UPDATE send_strategies
                SET name = ?, send_interval_minutes = ?, retry_interval_minutes = ?,
                    max_retries = ?, command_timeout_seconds = ?, active = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, send_interval, retry_interval, max_retries, timeout, active, now, existing["id"]),
            )
            saved_id = existing["id"]
        else:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries,
                   command_timeout_seconds, active, is_default, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (name, send_interval, retry_interval, max_retries, timeout, active, now, now),
            )
            saved_id = int(cur.lastrowid)
        if make_default:
            conn.execute("UPDATE send_strategies SET is_default = 0")
            conn.execute("UPDATE send_strategies SET is_default = 1, active = 1, updated_at = ? WHERE id = ?", (now, saved_id))
        ensure_default_strategy(conn)


def delete_strategy(strategy_id: int) -> None:
    with db() as conn:
        row = conn.execute("SELECT * FROM send_strategies WHERE id = ?", (strategy_id,)).fetchone()
        if not row:
            raise ValueError("发送策略不存在")
        if row["is_default"]:
            raise ValueError("默认策略不能删除")
        conn.execute("DELETE FROM send_strategies WHERE id = ?", (strategy_id,))
        ensure_default_strategy(conn)


def required_future_time_from_form(value: str) -> str:
    if not value.strip():
        raise ValueError("请选择计划发送时间")
    parsed = dt.datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M").astimezone()
    if parsed < dt.datetime.now().astimezone() - dt.timedelta(seconds=30):
        raise ValueError("计划发送时间不能早于当前时间")
    return parsed.strftime(TIME_FORMAT)


def optional_future_time_from_form(value: str) -> str:
    if not value.strip():
        return now_text()
    return required_future_time_from_form(value)


def parse_time(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(value, TIME_FORMAT)
    except Exception:
        return None


def latest_plan_submission(conn: sqlite3.Connection, row: sqlite3.Row) -> dt.datetime | None:
    if row["send_type"] != "plan" or not row["plan_id"]:
        return None
    last = conn.execute(
        """
        SELECT submitted_at
        FROM outbound_sms
        WHERE plan_id = ?
          AND id != ?
          AND status = 'submitted'
          AND submitted_at IS NOT NULL
          AND submitted_at != ''
        ORDER BY submitted_at DESC
        LIMIT 1
        """,
        (row["plan_id"], row["id"]),
    ).fetchone()
    if not last:
        return None
    return parse_time(last["submitted_at"])


def can_submit_by_interval(row: sqlite3.Row, conn: sqlite3.Connection | None = None) -> bool:
    interval = parse_positive_int(str(row["send_interval_minutes"]), 1, 0, SEND_INTERVAL_MAX_MINUTES)
    if row["send_type"] == "plan" and row["plan_id"]:
        if conn is None:
            with db() as plan_conn:
                last_dt = latest_plan_submission(plan_conn, row)
        else:
            last_dt = latest_plan_submission(conn, row)
        if not last_dt:
            return True
        return dt.datetime.now().astimezone() >= last_dt + dt.timedelta(minutes=interval)

    last = get_state("last_submission_at")
    if not last:
        return True
    last_dt = parse_time(last)
    if not last_dt:
        return True
    return dt.datetime.now().astimezone() >= last_dt + dt.timedelta(minutes=interval)


def build_sms_command(destination: str, text: str) -> list[str]:
    _segments, encoding = estimate_segments(text)
    cmd = [
        "/usr/bin/gammu-smsd-inject",
        "-c",
        str(SMSDRC),
        "TEXT",
        destination,
    ]
    if encoding == "Unicode":
        cmd.append("-unicode")
    cmd.extend(["-text", text])
    return cmd


def sms_subprocess_env() -> dict:
    env = os.environ.copy()
    env["LANG"] = "C.UTF-8"
    env["LC_ALL"] = "C.UTF-8"
    return env


def submit_sms(row: sqlite3.Row, timeout: int) -> tuple[str, str]:
    cmd = build_sms_command(row["destination"], row["text"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=sms_subprocess_env())
    except subprocess.TimeoutExpired:
        return "ambiguous", f"gammu-smsd-inject timed out after {timeout}s; no automatic retry to avoid duplicate SMS"
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode == 0:
        return "submitted", output
    return "failed", f"exit={result.returncode}; {output}"


def process_one_due(row_id: int | None = None, ignore_interval: bool = False) -> None:
    now = now_text()
    with db() as conn:
        if row_id is None:
            rows = conn.execute(
                """
                SELECT * FROM outbound_sms
                WHERE status IN ('queued', 'retry_wait')
                  AND scheduled_at <= ?
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT 50
                """,
                (now, now),
            ).fetchall()
            row = None
            for candidate in rows:
                if can_submit_by_interval(candidate, conn):
                    row = candidate
                    break
        else:
            row = conn.execute(
                """
                SELECT * FROM outbound_sms
                WHERE id = ?
                  AND status IN ('queued', 'retry_wait')
                  AND scheduled_at <= ?
                  AND next_attempt_at <= ?
                """,
                (row_id, now, now),
            ).fetchone()
            if row and not ignore_interval and not can_submit_by_interval(row, conn):
                row = None
        if not row:
            return
        updated = conn.execute(
            """
            UPDATE outbound_sms
            SET status = 'sending', attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status IN ('queued','retry_wait')
            """,
            (now, row["id"]),
        ).rowcount
        if updated != 1:
            return
    with db() as conn:
        row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
    timeout = parse_positive_int(str(row["command_timeout_seconds"]), 45, 10, 180)
    result_status, message = submit_sms(row, timeout)
    finished = now_text()
    if result_status == "submitted":
        set_state("last_submission_at", finished)
        with db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'submitted', submitted_at = ?, updated_at = ?,
                    last_error = '', command_output = ?
                WHERE id = ?
                """,
                (finished, finished, message[:2000], row["id"]),
            )
        advance_plan_after_success(row, finished)
        return
    if result_status == "ambiguous":
        set_state("last_submission_at", finished)
        with db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'ambiguous', updated_at = ?, last_error = ?, command_output = ?
                WHERE id = ?
                """,
                (finished, message, message[:2000], row["id"]),
            )
            final_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
        notify_final_failure(final_row)
        return
    attempts = int(row["attempts"])
    max_retries = int(row["max_retries"])
    retry_interval = int(row["retry_interval_minutes"])
    if attempts <= max_retries:
        next_attempt = (dt.datetime.now().astimezone() + dt.timedelta(minutes=retry_interval)).strftime(TIME_FORMAT)
        with db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'retry_wait', updated_at = ?, next_attempt_at = ?,
                    last_error = ?, command_output = ?
                WHERE id = ?
                """,
                (finished, next_attempt, message, message[:2000], row["id"]),
            )
        return
    with db() as conn:
        conn.execute(
            """
            UPDATE outbound_sms
            SET status = 'failed', updated_at = ?, last_error = ?, command_output = ?
            WHERE id = ?
            """,
            (finished, message, message[:2000], row["id"]),
        )
        final_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
    notify_final_failure(final_row)


def worker_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            process_one_due()
        except Exception as exc:
            LOGS.mkdir(parents=True, exist_ok=True)
            with (LOGS / "sms-admin-worker.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{now_text()} worker_error={exc}\n")
        stop_event.wait(5)


def format_sse_event(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def sim_status_script() -> str:
    return """
  <script>
    (function() {
      var panel = document.getElementById("sim-status-panel");
      if (!panel || !window.EventSource) return;
      var refresh = panel.querySelector("[data-sim-refresh]");
      var source = null;
      function setField(name, value) {
        var cell = panel.querySelector('[data-sim-field="' + name + '"]');
        if (cell) cell.textContent = value || "-";
      }
      function pillTone(tone) {
        return ["ok", "bad", "warn", "muted", "info"].indexOf(tone) >= 0 ? tone : "muted";
      }
      function renderSimState(status) {
        var cell = panel.querySelector('[data-sim-field="sim_state"]');
        if (!cell) return;
        var hasError = Boolean(status.error);
        var label = hasError ? "不可用" : (status.sim_state || "未知");
        cell.textContent = "";
        var badge = document.createElement("span");
        badge.setAttribute("data-sim-badge", "");
        badge.className = "status-pill " + (hasError ? "bad" : "ok");
        badge.textContent = label;
        cell.appendChild(badge);
      }
      function renderNetworkLevel(status) {
        var cell = panel.querySelector('[data-sim-field="network_level"]');
        if (!cell) return;
        cell.textContent = "";
        var value = document.createElement("span");
        value.setAttribute("data-network-level-text", "");
        value.textContent = status.network_level || "-";
        var pill = document.createElement("span");
        pill.className = "status-pill " + pillTone(status.network_switch_tone);
        pill.textContent = status.network_switch || "未知";
        cell.appendChild(value);
        cell.appendChild(pill);
      }
      function setRefresh(active) {
        if (!refresh) return;
        refresh.disabled = active;
        refresh.textContent = active ? "刷新中" : "刷新";
      }
      function closeStream() {
        if (source) source.close();
        source = null;
      }
      function render(status) {
        var hasError = Boolean(status.error);
        renderSimState(status);
        setField("sim_identity", status.sim_identity);
        setField("sms_counts", status.sms_counts);
        setField("signal", status.signal);
        renderNetworkLevel(status);
        setField("operator", status.operator);
        setField("checked_at", status.checked_at);
        var slot = panel.querySelector("[data-sim-error-slot]");
        if (slot) {
          slot.innerHTML = hasError ? '<div class="hint badtext" data-sim-error></div>' : "";
          var error = slot.querySelector("[data-sim-error]");
          if (error) error.textContent = "状态读取失败：" + status.error;
        }
      }
      function startStream() {
        closeStream();
        source = new EventSource("/api/sim-status/stream");
        setRefresh(true);
        source.addEventListener("sim-status", function(event) {
          try {
            render(JSON.parse(event.data));
          } catch (err) {}
        });
        source.addEventListener("done", function() {
          closeStream();
          setRefresh(false);
        });
        source.onerror = function() {
          closeStream();
          setRefresh(false);
        };
      }
      if (refresh) refresh.addEventListener("click", startStream);
      startStream();
    })();
  </script>"""


def page(title: str, body: str, active: str = "") -> bytes:
    nav = [
        ("/", "数据面板", "dashboard"),
        ("/inbound", "接收列表", "inbound"),
        ("/outbound", "发送列表", "outbound"),
        ("/send", "发送短信", "send"),
        ("/plans", "发送计划", "plans"),
        ("/settings", "发送策略", "settings"),
        ("/password", "修改密码", "password"),
    ]
    links = "".join(
        f'<a class="nav-link {"active" if key == active else ""}" href="{href}">{label}</a>'
        for href, label, key in nav
    )
    script = sim_status_script() if active == "dashboard" else ""
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} · SMS Admin</title>
  <style>
    :root {{
      --ink:#182230; --muted:#667085; --line:#d9e1ea; --paper:#ffffff;
      --wash:#f5f7fa; --nav:#111827; --blue:#2563eb; --green:#067647;
      --red:#b42318; --amber:#b54708; --teal:#0f766e;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--wash); color:var(--ink); font-family:"Avenir Next","Helvetica Neue","Microsoft YaHei",sans-serif; letter-spacing:0; }}
    a {{ color:inherit; text-decoration:none; }}
    .shell {{ min-height:100vh; display:grid; grid-template-columns:220px 1fr; }}
    .side {{ background:var(--nav); color:#e5e7eb; padding:22px 16px; }}
    .brand {{ font-size:18px; font-weight:800; margin-bottom:4px; }}
    .sub {{ font-size:12px; color:#9ca3af; margin-bottom:24px; }}
    .nav-link {{ display:block; padding:10px 12px; border-radius:6px; color:#cbd5e1; margin-bottom:4px; font-size:14px; }}
    .nav-link.active, .nav-link:hover {{ background:#243244; color:#fff; }}
    .main {{ min-width:0; }}
    .top {{ height:64px; display:flex; align-items:center; justify-content:space-between; padding:0 28px; background:#fff; border-bottom:1px solid var(--line); }}
    .top h1 {{ margin:0; font-size:20px; line-height:28px; }}
    .logout {{ color:var(--muted); font-size:13px; }}
    .content {{ padding:24px 28px 40px; }}
    .notice {{ border:1px solid #fedf89; background:#fffaeb; color:#7a2e0e; padding:12px 14px; border-radius:6px; margin-bottom:18px; font-size:14px; line-height:22px; }}
    .grid {{ display:grid; gap:14px; }}
    .stack {{ display:grid; gap:14px; margin-bottom:18px; }}
    .stats {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
    .panel {{ background:var(--paper); border:1px solid var(--line); border-radius:8px; }}
    .panel.wide, form.panel.wide {{ max-width:1100px; }}
    .stat {{ padding:16px; min-height:92px; }}
    .stat .label {{ color:var(--muted); font-size:12px; }}
    .stat .value {{ font-size:28px; font-weight:800; margin-top:8px; }}
    .band {{ padding:16px; margin-top:16px; }}
    .sim-status {{ margin-bottom:16px; }}
    .sim-status-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:12px; }}
    .sim-status-head h2 {{ margin:0; }}
    .sim-refresh {{ min-width:72px; padding:8px 12px; font-size:13px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
    th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #edf1f5; vertical-align:top; font-size:13px; line-height:20px; }}
    th {{ color:#475467; background:#f8fafc; font-weight:700; }}
    tr:last-child td {{ border-bottom:0; }}
    .msg {{ max-width:520px; white-space:pre-wrap; word-break:break-word; }}
    .badge {{ display:inline-block; min-width:64px; text-align:center; padding:3px 8px; border-radius:999px; font-size:12px; font-weight:700; }}
    .badge.ok {{ background:#dcfae6; color:var(--green); }}
    .badge.bad {{ background:#fee4e2; color:var(--red); }}
    .badge.warn {{ background:#fef0c7; color:var(--amber); }}
    .badge.info {{ background:#dbeafe; color:#1d4ed8; }}
    .badge.muted {{ background:#eef2f6; color:#475467; }}
    .status-pill {{ display:inline-flex; align-items:center; min-height:22px; margin-left:8px; padding:2px 9px; border-radius:999px; font-size:12px; font-weight:800; }}
    .status-pill.ok {{ background:#dcfae6; color:var(--green); }}
    .status-pill.bad {{ background:#fee4e2; color:var(--red); }}
    .status-pill.warn {{ background:#fef0c7; color:var(--amber); }}
    .status-pill.info {{ background:#dbeafe; color:#1d4ed8; }}
    .status-pill.muted {{ background:#eef2f6; color:#475467; }}
    form.panel {{ padding:18px; max-width:760px; }}
    label {{ display:block; font-size:13px; color:#344054; font-weight:700; margin:14px 0 6px; }}
    input, textarea, select {{ width:100%; border:1px solid #cfd8e3; border-radius:6px; padding:10px 11px; font:inherit; background:#fff; color:var(--ink); }}
    textarea {{ min-height:150px; resize:vertical; }}
    .row {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
    .row.three {{ grid-template-columns:1fr 1fr 1fr; }}
    .row.phone {{ grid-template-columns:140px 1fr; }}
    .hint {{ color:var(--muted); font-size:12px; line-height:18px; margin-top:6px; }}
    .badtext {{ color:var(--red); }}
    .checkline {{ display:flex; flex-wrap:wrap; gap:14px; align-items:center; margin-top:12px; }}
    .checkline label {{ display:flex; align-items:center; gap:8px; margin:0; }}
    .checkline input {{ width:auto; }}
    .actions {{ margin-top:18px; display:flex; gap:10px; align-items:center; }}
    .toolbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin:0 0 14px; }}
    .table-actions {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
    .compact-filters {{ display:flex; width:100%; justify-content:flex-end; align-items:end; flex-wrap:wrap; gap:10px; padding:12px; max-width:none; margin-bottom:16px; }}
    form.panel.wide.compact-filters {{ width:100%; max-width:none; }}
    .compact-filters .field {{ width:150px; }}
    .compact-filters .field.narrow {{ width:112px; }}
    .compact-filters label {{ margin:0 0 4px; font-size:12px; }}
    .compact-filters input, .compact-filters select {{ padding:8px 9px; min-height:36px; }}
    button, .button {{ border:0; border-radius:6px; background:var(--blue); color:#fff; padding:10px 14px; font-weight:800; cursor:pointer; font-size:14px; }}
    button.secondary, .button.secondary {{ background:#344054; }}
    button.muted {{ background:#667085; }}
    button.danger {{ background:var(--red); }}
    button:disabled {{ background:#98a2b3; cursor:not-allowed; }}
    .inline {{ display:inline; }}
    .count-pill {{ display:inline-block; min-width:30px; text-align:center; border-radius:999px; padding:3px 9px; font-size:12px; font-weight:800; }}
    .count-pill.success {{ background:#dcfae6; color:var(--green); }}
    .count-pill.failure {{ background:#fee4e2; color:var(--red); }}
    dialog.modal {{ width:min(760px, calc(100vw - 32px)); border:1px solid var(--line); border-radius:8px; padding:0; color:var(--ink); }}
    dialog.modal::backdrop {{ background:rgba(15,23,42,.45); }}
    .modal-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:16px 18px; border-bottom:1px solid var(--line); }}
    .modal-head h2 {{ margin:0; font-size:18px; }}
    .modal-body {{ padding:18px; }}
    .detail-grid {{ display:grid; grid-template-columns:140px 1fr; gap:8px 12px; font-size:13px; line-height:20px; }}
    .detail-grid b {{ color:#475467; }}
    .strategy-card {{ padding:18px; }}
    .strategy-card form {{ max-width:none; }}
    .strategy-actions {{ justify-content:space-between; }}
    .kv th {{ width:120px; }}
    .login {{ min-height:100vh; display:grid; place-items:center; padding:24px; background:#111827; }}
    .login form {{ width:100%; max-width:360px; background:#fff; border-radius:8px; padding:24px; }}
    .login h1 {{ margin:0 0 12px; font-size:22px; }}
    @media (max-width: 860px) {{
      .shell {{ grid-template-columns:1fr; }}
      .side {{ position:static; }}
      .stats, .row, .row.three, .row.phone {{ grid-template-columns:1fr; }}
      .detail-grid {{ grid-template-columns:1fr; }}
      .top {{ padding:0 16px; }}
      .content {{ padding:18px 16px 32px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside class="side">
      <div class="brand">SMS Admin</div>
      <div class="sub">Quectel EM120R-GL</div>
      {links}
    </aside>
    <main class="main">
      <header class="top"><h1>{html.escape(title)}</h1><a class="logout" href="/logout">退出</a></header>
      <div class="content">{body}</div>
    </main>
  </div>
{script}
</body>
</html>"""
    return html_doc.encode("utf-8")


def login_page(error: str = "") -> bytes:
    err = f'<div class="notice">{html.escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · SMS Admin</title>
<style>
*,*::before,*::after{{box-sizing:border-box;}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#111827;color:#182230;font-family:"Avenir Next","Microsoft YaHei",sans-serif;}}
form{{width:min(360px,calc(100vw - 32px));background:#fff;border-radius:8px;padding:24px;border:1px solid #243244;}}
h1{{margin:0 0 8px;font-size:22px;}}p{{margin:0 0 18px;color:#667085;font-size:13px;line-height:20px;}}
label{{display:block;font-weight:700;font-size:13px;margin-bottom:6px;}}input{{width:100%;border:1px solid #cfd8e3;border-radius:6px;padding:11px;font:inherit;}}
button{{width:100%;margin-top:16px;border:0;border-radius:6px;background:#2563eb;color:#fff;padding:11px;font-weight:800;cursor:pointer;}}
.notice{{border:1px solid #fedf89;background:#fffaeb;color:#7a2e0e;padding:10px 12px;border-radius:6px;margin-bottom:14px;font-size:13px;}}
</style></head><body><form method="post" action="/login"><h1>SMS Admin</h1><p>输入管理密码后进入后台。</p>{err}<label>管理密码</label><input type="password" name="password" autofocus autocomplete="current-password"><button>进入后台</button></form></body></html>""".encode("utf-8")


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "SMSAdmin/1.0"

    def log_message(self, fmt: str, *args) -> None:
        with (LOGS / "sms-admin-access.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{now_text()} {self.address_string()} {fmt % args}\n")

    @property
    def cfg(self) -> dict:
        return self.server.cfg  # type: ignore[attr-defined]

    def send_html(self, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def stream_sim_status(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for index in range(SIM_STATUS_STREAM_SECONDS):
                self.wfile.write(format_sse_event("sim-status", get_sim_status(use_cache=False)).encode("utf-8"))
                self.wfile.flush()
                if index < SIM_STATUS_STREAM_SECONDS - 1:
                    time.sleep(1)
            self.wfile.write(format_sse_event("done", {"done": True}).encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def cookie_value(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            if "=" not in part:
                continue
            key, value = part.strip().split("=", 1)
            if key == name:
                return value
        return ""

    def make_cookie(self) -> str:
        exp = int(time.time()) + 86400
        nonce = secrets.token_urlsafe(12)
        payload = b64url(json.dumps({"exp": exp, "nonce": nonce}, separators=(",", ":")).encode("utf-8"))
        sig = hmac.new(self.cfg["SESSION_SECRET"].encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
        return f"sms_admin={payload}.{sig}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400"

    def authenticated(self) -> bool:
        value = self.cookie_value("sms_admin")
        if "." not in value:
            return False
        payload, sig = value.rsplit(".", 1)
        expected = hmac.new(self.cfg["SESSION_SECRET"].encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        try:
            data = json.loads(b64url_decode(payload).decode("utf-8"))
        except Exception:
            return False
        return int(data.get("exp", 0)) >= int(time.time())

    def require_auth(self) -> bool:
        if self.path.startswith("/login"):
            return True
        if self.authenticated():
            return True
        self.redirect("/login")
        return False

    def do_GET(self) -> None:
        if not self.require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/login":
            self.send_html(login_page())
        elif path == "/logout":
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "sms_admin=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
        elif path == "/api/sim-status/stream":
            self.stream_sim_status()
        elif path == "/":
            self.send_html(self.render_dashboard())
        elif path == "/inbound":
            self.send_html(self.render_inbound())
        elif path == "/outbound":
            self.send_html(self.render_outbound())
        elif path == "/send":
            self.send_html(self.render_send())
        elif path == "/plans":
            self.send_html(self.render_plans())
        elif path == "/settings":
            self.send_html(self.render_settings())
        elif path == "/password":
            self.send_html(self.render_password())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/login":
            form = self.read_form()
            if verify_password(form.get("password", ""), self.cfg["ADMIN_PASSWORD_HASH"]):
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", self.make_cookie())
                self.end_headers()
            else:
                self.send_html(login_page("密码不正确"), 403)
            return
        if not self.require_auth():
            return
        if path == "/send":
            self.handle_send()
        elif path == "/plans":
            self.handle_plan()
        elif path == "/settings":
            self.handle_settings()
        elif path.startswith("/settings/") and path.endswith("/delete"):
            self.handle_strategy_delete(path)
        elif path == "/password":
            self.handle_password()
        elif path.startswith("/outbound/") and path.endswith("/cancel"):
            self.handle_cancel(path)
        elif path.startswith("/plans/") and path.endswith("/delete"):
            self.handle_plan_delete(path)
        elif path.startswith("/plans/") and path.endswith("/archive"):
            self.handle_plan_archive(path)
        elif path.startswith("/plans/") and path.endswith("/cancel"):
            self.handle_plan_cancel(path)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def render_dashboard(self) -> bytes:
        with db() as conn:
            inbound_count = conn.execute("SELECT COUNT(*) AS c FROM inbound_sms").fetchone()["c"]
            sent_count = conn.execute("SELECT COUNT(*) AS c FROM outbound_sms WHERE status = 'submitted'").fetchone()["c"]
            pending_count = conn.execute("SELECT COUNT(*) AS c FROM outbound_sms WHERE status IN ('queued','retry_wait','sending')").fetchone()["c"]
            risk_count = conn.execute("SELECT COUNT(*) AS c FROM outbound_sms WHERE status IN ('failed','ambiguous')").fetchone()["c"]
            recent_in = conn.execute("SELECT * FROM inbound_sms ORDER BY id DESC LIMIT 5").fetchall()
            recent_out = conn.execute("SELECT * FROM outbound_sms ORDER BY id DESC LIMIT 5").fetchall()
        service = subprocess.run(["/bin/systemctl", "is-active", "gammu-smsd"], capture_output=True, text=True, timeout=5).stdout.strip()
        service_badge = '<span class="badge ok">active</span>' if service == "active" else f'<span class="badge bad">{html.escape(service or "unknown")}</span>'
        stats = f"""
        <div class="grid stats">
          <div class="panel stat"><div class="label">接收短信</div><div class="value">{inbound_count}</div></div>
          <div class="panel stat"><div class="label">已提交发送</div><div class="value">{sent_count}</div></div>
          <div class="panel stat"><div class="label">待处理发送</div><div class="value">{pending_count}</div></div>
          <div class="panel stat"><div class="label">失败/不明</div><div class="value">{risk_count}</div></div>
        </div>
        <div class="panel band"><b>服务状态</b>：gammu-smsd {service_badge}</div>
        """
        return page("数据面板", render_sim_status_panel() + stats + self.table_inbound(recent_in, "最近接收") + self.table_outbound(recent_out, "最近发送"), "dashboard")

    def table_inbound(self, rows, title: str = "接收列表") -> str:
        body = "".join(
            f"<tr><td>#{r['id']}</td><td>{html.escape(r['received_at'])}</td><td>{html.escape(r['sender'])}</td><td class=\"msg\">{html.escape(r['text'])}</td><td>{'已转发' if r['forwarded'] else '未转发'}</td></tr>"
            for r in rows
        ) or '<tr><td colspan="5">暂无记录</td></tr>'
        return f'<h2>{html.escape(title)}</h2><table><tr><th>ID</th><th>时间</th><th>号码</th><th>内容</th><th>邮件</th></tr>{body}</table>'

    def table_outbound(self, rows, title: str = "发送列表") -> str:
        body = ""
        for r in rows:
            cancel = ""
            if r["status"] in ("queued", "retry_wait"):
                if r["send_type"] == "plan":
                    cancel = '<span class="hint">到计划列表操作</span>'
                else:
                    cancel = f'<form class="inline" method="post" action="/outbound/{r["id"]}/cancel"><button class="danger">取消</button></form>'
            plan_text = f"#{r['plan_id']}" if r["plan_id"] else "-"
            body += (
                f"<tr><td>#{r['id']}</td><td>{send_type_badge(r['send_type'])}</td>"
                f"<td>{plan_text}</td><td>{html.escape(r['created_at'])}</td><td>{html.escape(r['destination'])}</td>"
                f"<td>{html.escape(r['strategy_name'] or '-')}</td><td>{status_badge(r['status'])}</td>"
                f"<td>{r['attempts']}/{r['max_retries'] + 1}</td>"
                f"<td class=\"msg\">{html.escape(r['text'])}</td><td class=\"msg\">{html.escape(r['last_error'])}</td><td>{cancel}</td></tr>"
            )
        body = body or '<tr><td colspan="11">暂无记录</td></tr>'
        return (
            f'<h2>{html.escape(title)}</h2><table><tr><th>ID</th><th>类型</th><th>计划ID</th><th>创建</th><th>目标</th>'
            '<th>策略</th><th>状态</th><th>尝试</th><th>内容</th><th>错误</th><th></th></tr>'
            f"{body}</table>"
        )

    def render_inbound(self) -> bytes:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        sender = query.get("sender", [""])[0].strip()
        keyword = query.get("keyword", [""])[0].strip()
        forwarded = query.get("forwarded", [""])[0].strip()
        clauses = []
        args = []
        if sender:
            clauses.append("sender LIKE ?")
            args.append(f"%{sender}%")
        if keyword:
            clauses.append("text LIKE ?")
            args.append(f"%{keyword}%")
        if forwarded in ("0", "1"):
            clauses.append("forwarded = ?")
            args.append(int(forwarded))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with db() as conn:
            rows = conn.execute(f"SELECT * FROM inbound_sms {where} ORDER BY id DESC LIMIT 100", args).fetchall()
        forwarded_options = (
            '<option value="">全部邮件状态</option>'
            f'<option value="1"{" selected" if forwarded == "1" else ""}>已转发</option>'
            f'<option value="0"{" selected" if forwarded == "0" else ""}>未转发</option>'
        )
        filters = f"""
        <form class="panel wide filters compact-filters" method="get" action="/inbound">
          <div class="field"><label>号码</label><input name="sender" value="{html.escape(sender)}"></div>
          <div class="field"><label>内容</label><input name="keyword" value="{html.escape(keyword)}"></div>
          <div class="field"><label>邮件</label><select name="forwarded">{forwarded_options}</select></div>
          <button>筛选</button><a class="button secondary" href="/inbound">重置</a>
        </form>
        """
        return page("接收列表", filters + self.table_inbound(rows), "inbound")

    def render_outbound(self) -> bytes:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        page_num = parse_positive_int(query.get("page", ["1"])[0], 1, 1, 999999)
        plan_id = query.get("plan_id", [""])[0].strip()
        strategy_id = query.get("strategy_id", [""])[0].strip()
        status = query.get("status", [""])[0].strip()
        send_type = query.get("send_type", [""])[0].strip()
        clauses = []
        args = []
        if plan_id:
            clauses.append("plan_id = ?")
            args.append(plan_id)
        if strategy_id:
            clauses.append("strategy_id = ?")
            args.append(strategy_id)
        if status in STATUSES:
            clauses.append("status = ?")
            args.append(status)
        if send_type in SEND_TYPES:
            clauses.append("send_type = ?")
            args.append(send_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = 10
        offset = (page_num - 1) * limit
        with db() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM outbound_sms {where}", args).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM outbound_sms {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        status_options = '<option value="">全部状态</option>' + "".join(
            f'<option value="{html.escape(key)}"{" selected" if key == status else ""}>{html.escape(label)}</option>'
            for key, (label, _tone) in STATUSES.items()
        )
        type_options = '<option value="">全部类型</option>' + "".join(
            f'<option value="{html.escape(key)}"{" selected" if key == send_type else ""}>{html.escape(label)}</option>'
            for key, (label, _tone) in SEND_TYPES.items()
        )
        strategy_options = ['<option value="">全部策略</option>']
        for strategy in list_strategies(active_only=False):
            selected = " selected" if str(strategy["id"]) == strategy_id else ""
            strategy_options.append(f'<option value="{strategy["id"]}"{selected}>{html.escape(strategy["name"])}</option>')
        filters = f"""
        <form class="panel wide filters compact-filters" method="get" action="/outbound">
          <div class="field narrow"><label>计划ID</label><input name="plan_id" inputmode="numeric" value="{html.escape(plan_id)}"></div>
          <div class="field"><label>策略</label><select name="strategy_id">{''.join(strategy_options)}</select></div>
          <div class="field"><label>状态</label><select name="status">{status_options}</select></div>
          <div class="field"><label>类型</label><select name="send_type">{type_options}</select></div>
          <button>筛选</button><a class="button secondary" href="/outbound">重置</a>
        </form>
        """
        base_query = {}
        if plan_id:
            base_query["plan_id"] = plan_id
        if strategy_id:
            base_query["strategy_id"] = strategy_id
        if status in STATUSES:
            base_query["status"] = status
        if send_type in SEND_TYPES:
            base_query["send_type"] = send_type
        def page_link(num: int, label: str) -> str:
            params = dict(base_query)
            params["page"] = str(num)
            return f'<a class="button secondary" href="/outbound?{urllib.parse.urlencode(params)}">{html.escape(label)}</a>'
        pager_buttons = []
        if page_num > 1:
            pager_buttons.append(page_link(page_num - 1, "上一页"))
        if offset + limit < total:
            pager_buttons.append(page_link(page_num + 1, "下一页"))
        pager = (
            f'<div class="toolbar"><div class="hint">共 {total} 条，每页 10 条，第 {page_num} 页</div>'
            f'<div class="table-actions">{"".join(pager_buttons)}</div></div>'
        )
        return page("发送列表", filters + pager + self.table_outbound(rows), "outbound")

    def render_send(self, error: str = "") -> bytes:
        notice = f'<div class="notice">{html.escape(error)}</div>' if error else ""
        body = f"""
        <div class="notice">提交后会立即尝试发送。提交超时或状态不明不会自动重发。</div>
        {notice}
        <form class="panel" method="post" action="/send">
          <div class="row phone">
            <div><label>区号</label><input name="country_code" value="+86" inputmode="tel" maxlength="6" required></div>
            <div><label>目标号码</label><input name="destination" placeholder="例如 13000000000" inputmode="tel" required></div>
          </div>
          <label>短信内容</label>
          <textarea name="text" maxlength="1000" required></textarea>
          <div class="hint">中文短信按 Unicode 估算：70 字一条，多条约 67 字一段。发送前请注意分段费用。</div>
          <label><input style="width:auto;margin-right:8px;" type="checkbox" name="confirm" value="yes" required>我确认号码和内容无误，并接受可能产生的短信费用</label>
          <div class="actions"><button>立即发送</button></div>
        </form>
        """
        return page("发送短信", body, "send")

    def handle_send(self) -> None:
        form = self.read_form()
        try:
            if form.get("confirm") != "yes":
                raise ValueError("请先勾选同意费用确认")
            destination = destination_from_form(form)
            text = form.get("text", "").strip()
            if not text:
                raise ValueError("短信内容不能为空")
            segments, encoding = estimate_segments(text)
            if segments > 10:
                raise ValueError(f"短信预计 {segments} 段，超过后台单次 10 段限制")
            item_id = create_immediate_outbound(destination, text, now_text())
            process_one_due(item_id, ignore_interval=True)
        except Exception as exc:
            self.send_html(self.render_send(str(exc)), 400)
            return
        self.redirect("/outbound")

    def render_plans(self, error: str = "", saved: str = "") -> bytes:
        notice = ""
        if error:
            notice = f'<div class="notice">{html.escape(error)}</div>'
        elif saved:
            notice = '<div class="notice">发送计划已保存。</div>'
        strategy_options = strategy_options_html()
        with db() as conn:
            rows = conn.execute(
                """
                SELECT p.*, o.status AS outbound_status, o.attempts AS outbound_attempts,
                       o.max_retries AS outbound_max_retries, o.last_error AS outbound_last_error,
                       o.id AS current_outbound_id,
                       (SELECT COUNT(*) FROM outbound_sms s
                        WHERE s.plan_id = p.id AND s.status = 'submitted') AS success_count,
                       (SELECT COUNT(*) FROM outbound_sms s
                        WHERE s.plan_id = p.id AND s.status IN ('failed','ambiguous')) AS failure_count,
                       (SELECT COUNT(*) FROM outbound_sms s
                        WHERE s.plan_id = p.id
                          AND (s.attempts > 0 OR s.status IN ('submitted','failed','ambiguous','sending'))) AS history_count
                FROM send_plans p
                LEFT JOIN outbound_sms o ON o.id = p.outbound_id
                WHERE p.deleted = 0
                ORDER BY p.id DESC
                LIMIT 100
                """
            ).fetchall()
        plan_rows = ""
        detail_modals = ""
        for r in rows:
            current_status = r["outbound_status"] or "cancelled"
            if r["archived"]:
                plan_status = '<span class="badge muted">已归档</span>'
                next_time = "-"
            elif not r["active"]:
                plan_status = '<span class="badge muted">已停止</span>'
                next_time = "-"
            else:
                plan_status = status_badge(current_status)
                next_time = r["scheduled_at"]
            if r["archived"]:
                action = ""
            elif int(r["history_count"] or 0) > 0:
                action = (
                    f'<form class="inline" method="post" action="/plans/{r["id"]}/archive">'
                    '<button class="secondary">归档</button></form>'
                )
            else:
                action = (
                    f'<form class="inline" method="post" action="/plans/{r["id"]}/delete">'
                    '<button class="danger">删除</button></form>'
                )
            detail_id = f"plan-detail-{r['id']}"
            detail_button = f'<button class="muted" type="button" onclick="document.getElementById(\'{detail_id}\').showModal()">详情</button>'
            plan_rows += (
                f"<tr><td>#{r['id']}</td><td>{html.escape(r['name'])}</td><td>{html.escape(next_time)}</td>"
                f"<td>{html.escape(r['destination'])}</td><td>{html.escape(r['strategy_name'])}</td>"
                f"<td>{plan_status}</td>"
                f"<td><span class=\"count-pill failure\">{int(r['failure_count'] or 0)}</span></td>"
                f"<td><span class=\"count-pill success\">{int(r['success_count'] or 0)}</span></td>"
                f"<td><div class=\"table-actions\">{detail_button}{action}</div></td></tr>"
            )
            detail_modals += f"""
            <dialog class="modal" id="{detail_id}">
              <div class="modal-head"><h2>计划详情 #{r['id']}</h2><form method="dialog"><button class="muted">关闭</button></form></div>
              <div class="modal-body">
                <div class="detail-grid">
                  <b>名称</b><span>{html.escape(r['name'])}</span>
                  <b>目标</b><span>{html.escape(r['destination'])}</span>
                  <b>策略</b><span>{html.escape(r['strategy_name'])}</span>
                  <b>下次发送</b><span>{html.escape(next_time)}</span>
                  <b>当前发送ID</b><span>#{html.escape(str(r['current_outbound_id'] or '-'))}</span>
                  <b>状态</b><span>{plan_status}</span>
                  <b>成功次数</b><span><span class="count-pill success">{int(r['success_count'] or 0)}</span></span>
                  <b>失败次数</b><span><span class="count-pill failure">{int(r['failure_count'] or 0)}</span></span>
                  <b>短信内容</b><span class="msg">{html.escape(r['text'])}</span>
                  <b>最近错误</b><span class="msg">{html.escape(r['outbound_last_error'] or '-')}</span>
                </div>
              </div>
            </dialog>
            """
        plan_rows = plan_rows or '<tr><td colspan="9">暂无计划</td></tr>'
        create_modal = f"""
        <dialog class="modal" id="plan-create">
          <div class="modal-head"><h2>新增发送计划</h2><form method="dialog"><button class="muted">关闭</button></form></div>
          <form method="post" action="/plans">
            <div class="modal-body">
              <div class="row">
                <div><label>计划名称</label><input name="name" maxlength="40" placeholder="例如 账单提醒"></div>
                <div><label>发送策略</label><select name="strategy_id" required>{strategy_options}</select></div>
              </div>
              <div class="row phone">
                <div><label>区号</label><input name="country_code" value="+86" inputmode="tel" maxlength="6" required></div>
                <div><label>目标号码</label><input name="destination" placeholder="例如 13000000000" inputmode="tel" required></div>
              </div>
              <label>计划发送时间</label>
              <input type="datetime-local" name="scheduled_at">
              <label>短信内容</label>
              <textarea name="text" maxlength="1000" required></textarea>
              <div class="hint">计划会立即生成一条发送队列记录；发送成功后按策略发送间隔准备下一次。发送间隔为 0 时只发送本次。</div>
              <label><input style="width:auto;margin-right:8px;" type="checkbox" name="confirm" value="yes" required>我确认号码、时间和内容无误，并接受可能产生的短信费用</label>
              <div class="actions"><button>保存发送计划</button></div>
            </div>
          </form>
        </dialog>
        """
        body = f"""
        {notice}
        <div class="toolbar"><h2>发送计划</h2><button type="button" onclick="document.getElementById('plan-create').showModal()">新增发送计划</button></div>
        {create_modal}
        <table><tr><th>ID</th><th>名称</th><th>下次发送</th><th>目标</th><th>策略</th><th>状态</th><th>失败次数</th><th>成功次数</th><th>操作</th></tr>{plan_rows}</table>
        {detail_modals}
        """
        return page("发送计划", body, "plans")

    def handle_plan(self) -> None:
        form = self.read_form()
        try:
            if form.get("confirm") != "yes":
                raise ValueError("请先勾选同意费用确认")
            destination = destination_from_form(form)
            text = form.get("text", "").strip()
            if not text:
                raise ValueError("短信内容不能为空")
            segments, encoding = estimate_segments(text)
            if segments > 10:
                raise ValueError(f"短信预计 {segments} 段，超过后台单次 10 段限制")
            scheduled_at = optional_future_time_from_form(form.get("scheduled_at", ""))
            strategy_id = strategy_from_form(form.get("strategy_id", ""))
            name = form.get("name", "").strip() or f"{destination} {scheduled_at}"
            create_send_plan(name[:40], destination, text, scheduled_at, strategy_id)
        except Exception as exc:
            self.send_html(self.render_plans(str(exc)), 400)
            return
        self.redirect("/plans")

    def render_settings(self, saved: str = "", error: str = "") -> bytes:
        notice = ""
        if error:
            notice = f'<div class="notice">{html.escape(error)}</div>'
        elif saved:
            notice = '<div class="notice">策略已保存。</div>'
        strategy_cards = ""
        for row in list_strategies(active_only=False):
            active_checked = " checked" if row["active"] else ""
            default_checked = " checked" if row["is_default"] else ""
            save_form_id = f"strategy-save-{row['id']}"
            delete_form_id = f"strategy-delete-{row['id']}"
            delete_form = ""
            delete_button = '<span class="hint">默认策略不能删除</span>'
            if not row["is_default"]:
                delete_form = (
                    f'<form id="{delete_form_id}" method="post" action="/settings/{row["id"]}/delete"></form>'
                )
                delete_button = f'<button class="danger" form="{delete_form_id}">删除策略</button>'
            strategy_cards += f"""
            <section class="panel wide strategy-card">
              <form id="{save_form_id}" method="post" action="/settings">
                <input type="hidden" name="strategy_id" value="{row['id']}">
                <h2>{html.escape(row['name'])}</h2>
                <div class="row">
                  <div><label>策略名称</label><input name="name" maxlength="40" value="{html.escape(row['name'])}" required></div>
                  <div><label>提交命令超时（秒）</label><input name="command_timeout_seconds" type="number" min="10" max="180" value="{row['command_timeout_seconds']}"></div>
                </div>
                <div class="row three">
                  <div><label>发送间隔（分钟，最多 180 天）</label><input name="send_interval_minutes" type="number" min="0" max="{SEND_INTERVAL_MAX_MINUTES}" value="{row['send_interval_minutes']}"></div>
                  <div><label>失败重试间隔（分钟）</label><input name="retry_interval_minutes" type="number" min="1" max="{RETRY_INTERVAL_MAX_MINUTES}" value="{row['retry_interval_minutes']}"></div>
                  <div><label>最大重试次数</label><input name="max_retries" type="number" min="0" max="10" value="{row['max_retries']}"></div>
                </div>
                <div class="checkline">
                  <label><input type="checkbox" name="active" value="yes"{active_checked}>启用</label>
                  <label><input type="checkbox" name="is_default" value="yes"{default_checked}>默认策略</label>
                </div>
              </form>
              {delete_form}
              <div class="actions strategy-actions">{delete_button}<button form="{save_form_id}">保存策略</button></div>
            </section>
            """
        body = f"""
        <div class="notice">保守策略：发送间隔和失败重试间隔都按分钟使用；只有 gammu-smsd-inject 明确返回失败才会重试，超时/状态不明不会重试。</div>
        {notice}
        <div class="stack">{strategy_cards}</div>
        <form class="panel wide" method="post" action="/settings">
          <h2>新增策略</h2>
          <div class="row">
            <div><label>策略名称</label><input name="name" maxlength="40" required></div>
            <div><label>提交命令超时（秒）</label><input name="command_timeout_seconds" type="number" min="10" max="180" value="{DEFAULT_STRATEGY['command_timeout_seconds']}"></div>
          </div>
          <div class="row three">
            <div><label>发送间隔（分钟，最多 180 天）</label><input name="send_interval_minutes" type="number" min="0" max="{SEND_INTERVAL_MAX_MINUTES}" value="{DEFAULT_STRATEGY['send_interval_minutes']}"></div>
            <div><label>失败重试间隔（分钟）</label><input name="retry_interval_minutes" type="number" min="1" max="{RETRY_INTERVAL_MAX_MINUTES}" value="{DEFAULT_STRATEGY['retry_interval_minutes']}"></div>
            <div><label>最大重试次数</label><input name="max_retries" type="number" min="0" max="10" value="{DEFAULT_STRATEGY['max_retries']}"></div>
          </div>
          <div class="checkline">
            <label><input type="checkbox" name="active" value="yes" checked>启用</label>
            <label><input type="checkbox" name="is_default" value="yes">默认策略</label>
          </div>
          <div class="actions"><button>新增策略</button></div>
        </form>
        """
        return page("发送策略", body, "settings")

    def handle_settings(self) -> None:
        form = self.read_form()
        try:
            save_strategy_from_form(form)
        except Exception as exc:
            self.send_html(self.render_settings(error=str(exc)), 400)
            return
        self.send_html(self.render_settings("1"))

    def handle_strategy_delete(self, path: str) -> None:
        try:
            strategy_id = int(path.split("/")[2])
            delete_strategy(strategy_id)
        except Exception as exc:
            self.send_html(self.render_settings(error=str(exc)), 400)
            return
        self.send_html(self.render_settings("1"))

    def render_password(self, message: str = "", error: str = "") -> bytes:
        notice = ""
        if error:
            notice = f'<div class="notice">{html.escape(error)}</div>'
        elif message:
            notice = f'<div class="notice">{html.escape(message)}</div>'
        body = f"""
        <div class="notice">修改成功后会立即轮换会话密钥，当前登录状态失效，需要使用新密码重新登录。</div>
        {notice}
        <form class="panel" method="post" action="/password">
          <label>当前密码</label>
          <input type="password" name="current_password" autocomplete="current-password" required>
          <label>新密码</label>
          <input type="password" name="new_password" autocomplete="new-password" minlength="6" required>
          <div class="hint">至少 6 个字符。建议使用密码管理器生成随机密码。</div>
          <label>再次输入新密码</label>
          <input type="password" name="confirm_password" autocomplete="new-password" minlength="6" required>
          <div class="actions"><button>保存新密码</button></div>
        </form>
        """
        return page("修改密码", body, "password")

    def handle_password(self) -> None:
        form = self.read_form()
        current = form.get("current_password", "")
        new_password = form.get("new_password", "")
        confirm = form.get("confirm_password", "")
        if not verify_password(current, self.cfg["ADMIN_PASSWORD_HASH"]):
            self.send_html(self.render_password(error="当前密码不正确"), 403)
            return
        if len(new_password) < 6:
            self.send_html(self.render_password(error="新密码至少需要 6 个字符"), 400)
            return
        if new_password != confirm:
            self.send_html(self.render_password(error="两次输入的新密码不一致"), 400)
            return
        if current == new_password:
            self.send_html(self.render_password(error="新密码不能和当前密码相同"), 400)
            return

        new_cfg = dict(self.cfg)
        new_cfg["ADMIN_PASSWORD_HASH"] = hash_password(new_password)
        new_cfg["SESSION_SECRET"] = secrets.token_urlsafe(32)
        write_admin_env(new_cfg)
        self.server.cfg = new_cfg  # type: ignore[attr-defined]
        self.send_html(
            login_page("密码已修改，请使用新密码重新登录。"),
            200,
            {"Set-Cookie": "sms_admin=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"},
        )

    def handle_cancel(self, path: str) -> None:
        try:
            item_id = int(path.split("/")[2])
        except Exception:
            self.send_error(400)
            return
        with db() as conn:
            conn.execute(
                "UPDATE outbound_sms SET status = 'cancelled', updated_at = ? WHERE id = ? AND status IN ('queued','retry_wait')",
                (now_text(), item_id),
            )
        self.redirect("/outbound")

    def handle_plan_delete(self, path: str) -> None:
        try:
            plan_id = int(path.split("/")[2])
            delete_send_plan(plan_id)
        except Exception as exc:
            self.send_html(self.render_plans(str(exc)), 400)
            return
        self.redirect("/plans")

    def handle_plan_archive(self, path: str) -> None:
        try:
            plan_id = int(path.split("/")[2])
            archive_send_plan(plan_id)
        except Exception as exc:
            self.send_html(self.render_plans(str(exc)), 400)
            return
        self.redirect("/plans")

    def handle_plan_cancel(self, path: str) -> None:
        try:
            plan_id = int(path.split("/")[2])
        except Exception:
            self.send_error(400)
            return
        now = now_text()
        with db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            if not plan:
                self.send_error(404)
                return
            if plan["outbound_id"]:
                conn.execute(
                    """
                    UPDATE outbound_sms
                    SET status = 'cancelled', updated_at = ?
                    WHERE id = ? AND status IN ('queued','retry_wait')
                    """,
                    (now, plan["outbound_id"]),
                )
            conn.execute("UPDATE send_plans SET active = 0, updated_at = ? WHERE id = ?", (now, plan_id))
        self.redirect("/plans")


def run() -> None:
    cfg = parse_env(ADMIN_ENV)
    required = ["ADMIN_PASSWORD_HASH", "SESSION_SECRET"]
    missing = [key for key in required if not cfg.get(key)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in {ADMIN_ENV}")
    init_db()
    stop_event = threading.Event()
    worker = threading.Thread(target=worker_loop, args=(stop_event,), daemon=True)
    worker.start()
    host = cfg.get("BIND_HOST", "0.0.0.0")
    port = int(cfg.get("PORT", "8088"))
    server = ThreadingHTTPServer((host, port), AdminHandler)
    server.cfg = cfg  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    finally:
        stop_event.set()


if __name__ == "__main__":
    run()
