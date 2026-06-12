#!/bin/sh
set -eu

SMS_BASE="${SMS_BASE:-/htdocs/sms}"
export SMS_BASE

mkdir -p \
  "$SMS_BASE/conf" \
  "$SMS_BASE/data" \
  "$SMS_BASE/logs" \
  "$SMS_BASE/spool/inbox" \
  "$SMS_BASE/spool/outbox" \
  "$SMS_BASE/spool/sent" \
  "$SMS_BASE/spool/error"

if [ ! -s "$SMS_BASE/conf/admin.env" ]; then
  python3 - <<'PY'
import base64
import hashlib
import os
import secrets
from pathlib import Path

base = Path(os.environ.get("SMS_BASE", "/htdocs/sms"))
conf = base / "conf"
conf.mkdir(parents=True, exist_ok=True)

password_hash = os.environ.get("ADMIN_PASSWORD_HASH", "").strip()
password = os.environ.get("ADMIN_PASSWORD", "").strip()
if not password_hash:
    if not password:
        raise SystemExit("ADMIN_PASSWORD or ADMIN_PASSWORD_HASH is required when conf/admin.env does not exist")
    rounds = 260000
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    password_hash = "pbkdf2_sha256$%d$%s$%s" % (
        rounds,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )

session_secret = os.environ.get("SESSION_SECRET", "").strip() or secrets.token_urlsafe(32)
bind_host = os.environ.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
port = os.environ.get("PORT", "8088").strip() or "8088"
admin_env = conf / "admin.env"
tmp = conf / "admin.env.tmp"
tmp.write_text(
    "\n".join(
        [
            f"ADMIN_PASSWORD_HASH={password_hash}",
            f"SESSION_SECRET={session_secret}",
            f"BIND_HOST={bind_host}",
            f"PORT={port}",
            "",
        ]
    ),
    encoding="utf-8",
)
os.chmod(tmp, 0o600)
os.replace(tmp, admin_env)
os.chmod(admin_env, 0o600)
PY
fi

exec "$@"
