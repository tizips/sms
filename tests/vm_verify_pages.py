#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.request
from pathlib import Path


BASE = Path("/htdocs/sms")
ADMIN_ENV = BASE / "conf" / "admin.env"
URL = "http://127.0.0.1:8088"


def parse_env(path: Path) -> dict:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_cookie(secret: str) -> str:
    payload = b64url(
        json.dumps(
            {"exp": int(time.time()) + 300, "nonce": secrets.token_urlsafe(12)},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    sig = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"sms_admin={payload}.{sig}"


def get(path: str, cookie: str) -> str:
    req = urllib.request.Request(URL + path, headers={"Cookie": cookie})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8")


def main() -> None:
    cfg = parse_env(ADMIN_ENV)
    cookie = make_cookie(cfg["SESSION_SECRET"])
    pages = {
        "settings": get("/settings", cookie),
        "plans": get("/plans", cookie),
        "send": get("/send", cookie),
        "outbound": get("/outbound", cookie),
        "dashboard": get("/", cookie),
    }
    checks = {
        "settings uses minute labels": "发送间隔（分钟）" in pages["settings"] and "失败重试间隔（分钟）" in pages["settings"],
        "settings hides old second labels": "发送间隔（秒）" not in pages["settings"] and "失败重试间隔（秒）" not in pages["settings"],
        "plans page renders": "新增发送计划" in pages["plans"] and "发送计划" in pages["plans"],
        "send page has strategy selector": 'name="strategy_id"' in pages["send"],
        "outbound has type and strategy columns": "<th>类型</th>" in pages["outbound"] and "<th>策略</th>" in pages["outbound"],
        "dashboard renders": "数据面板" in pages["dashboard"],
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
