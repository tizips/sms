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


def send_plan_storage_parts(destination: str, country_code: str = "", phone_number: str = "") -> tuple[str, str]:
    if country_code or phone_number:
        return normalize_country_code(country_code or "+86"), normalize_phone_number(phone_number)
    stored_country_code, stored_phone_number = split_destination_for_form(destination)
    if stored_phone_number.startswith("+"):
        return "", ""
    return stored_country_code, stored_phone_number


def create_send_plan(
    name: str,
    destination: str,
    text: str,
    scheduled_at: str,
    strategy_id: int | None,
    country_code: str = "",
    phone_number: str = "",
) -> int:
    created = now_text()
    stored_country_code, stored_phone_number = send_plan_storage_parts(destination, country_code, phone_number)
    with db() as conn:
        strategy = fetch_strategy(conn, strategy_id, active_only=True)
        plan_cur = conn.execute(
            """
            INSERT INTO send_plans
              (created_at, updated_at, name, destination, country_code, phone_number, text, scheduled_at,
               strategy_id, strategy_name, outbound_id, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (
                created,
                created,
                name,
                destination,
                stored_country_code,
                stored_phone_number,
                text,
                scheduled_at,
                strategy["id"],
                strategy["name"],
            ),
        )
        plan_id = int(plan_cur.lastrowid)
        outbound_id = insert_outbound(conn, destination, text, scheduled_at, strategy, "plan", plan_id)
        conn.execute("UPDATE send_plans SET outbound_id = ?, updated_at = ? WHERE id = ?", (outbound_id, created, plan_id))
        return plan_id


def update_send_plan(
    plan_id: int,
    destination: str,
    text: str,
    country_code: str = "",
    phone_number: str = "",
) -> None:
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("短信内容不能为空")
    segments, encoding = estimate_segments(clean_text)
    if segments > 10:
        raise ValueError(f"短信预计 {segments} 段，超过后台单次 10 段限制")
    now = now_text()
    stored_country_code, stored_phone_number = send_plan_storage_parts(destination, country_code, phone_number)
    with db() as conn:
        plan = conn.execute("SELECT * FROM send_plans WHERE id = ? AND deleted = 0", (plan_id,)).fetchone()
        if not plan:
            raise ValueError("发送计划不存在")
        if plan["archived"]:
            raise ValueError("已归档的发送计划不能修改")
        conn.execute(
            "UPDATE send_plans SET destination = ?, country_code = ?, phone_number = ?, text = ?, updated_at = ? WHERE id = ?",
            (destination, stored_country_code, stored_phone_number, clean_text, now, plan_id),
        )
        if plan["outbound_id"]:
            conn.execute(
                """
                UPDATE outbound_sms
                SET destination = ?, text = ?, updated_at = ?
                WHERE id = ? AND plan_id = ? AND send_type = 'plan' AND status = 'queued'
                """,
                (destination, clean_text, now, plan["outbound_id"], plan_id),
            )


def advance_plan_after_success(row: sqlite3.Row, submitted_at: str) -> None:
    if row["send_type"] != "plan" or not row["plan_id"]:
        return
    scheduled_dt = parse_time(row["scheduled_at"]) or parse_time(submitted_at) or dt.datetime.now().astimezone()
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
        next_scheduled = (scheduled_dt + dt.timedelta(minutes=send_interval)).strftime(TIME_FORMAT)
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
          AND (attempts > 0 OR status IN ('submitted','sent','failed','send_timeout','ambiguous','sending'))
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


INTERVAL_UNITS = (
    ("minute", "分钟", 1),
    ("hour", "小时", 60),
    ("day", "天", 24 * 60),
)
INTERVAL_UNIT_FACTORS = {key: factor for key, _label, factor in INTERVAL_UNITS}
INTERVAL_UNIT_LABELS = {key: label for key, label, _factor in INTERVAL_UNITS}


def send_interval_parts(minutes: int) -> tuple[int, str]:
    minutes = max(0, int(minutes))
    if minutes > 0 and minutes % (24 * 60) == 0:
        return minutes // (24 * 60), "day"
    if minutes > 0 and minutes % 60 == 0:
        return minutes // 60, "hour"
    return minutes, "minute"


def format_send_interval_minutes(minutes: int) -> str:
    minutes = max(0, int(minutes))
    if minutes == 0:
        return "完成后不再自动发送"
    value, unit = send_interval_parts(minutes)
    return f"每 {value} {INTERVAL_UNIT_LABELS[unit]}发送间隔"


def send_interval_minutes_from_form(form: dict) -> int:
    if "send_interval_value" in form or "send_interval_unit" in form:
        value = parse_positive_int(form.get("send_interval_value", "1"), 1, 0, SEND_INTERVAL_MAX_MINUTES)
        unit = form.get("send_interval_unit", "minute").strip()
        factor = INTERVAL_UNIT_FACTORS.get(unit, 1)
        return min(SEND_INTERVAL_MAX_MINUTES, value * factor)
    return parse_positive_int(form.get("send_interval_minutes", "1"), 1, 0, SEND_INTERVAL_MAX_MINUTES)


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
            f"({format_send_interval_minutes(row['send_interval_minutes'])} / {row['retry_interval_minutes']} 分钟重试)"
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
    send_interval = send_interval_minutes_from_form(form)
    retry_interval = parse_positive_int(form.get("retry_interval_minutes", "10"), 10, 1, RETRY_INTERVAL_MAX_MINUTES)
    max_retries = parse_positive_int(form.get("max_retries", "1"), 1, 0, 10)
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
            timeout = parse_positive_int(
                form.get("command_timeout_seconds", str(existing["command_timeout_seconds"])),
                int(existing["command_timeout_seconds"]),
                10,
                180,
            )
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
            default_timeout = int(DEFAULT_STRATEGY["command_timeout_seconds"])
            timeout = parse_positive_int(form.get("command_timeout_seconds", str(default_timeout)), default_timeout, 10, 180)
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


def dispatch_idempotency_key(row) -> str:
    return f"outbound:{row['id']}:attempt:{row['attempts']}"


def create_dispatch_job(conn, row) -> int:
    now = now_text()
    idempotency_key = dispatch_idempotency_key(row)
    conn.execute(
        """
        INSERT INTO sms_dispatch_jobs
          (idempotency_key, outbound_id, attempt_no, status, destination, text,
           created_at, updated_at, cancel_requested)
        VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, 0)
        ON CONFLICT(idempotency_key) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (
            idempotency_key,
            row["id"],
            int(row["attempts"]),
            row["destination"],
            row["text"],
            now,
            now,
        ),
    )
    job = conn.execute("SELECT * FROM sms_dispatch_jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return int(job["id"])


def request_cancel_outbound(item_id: int) -> tuple[bool, str]:
    now = now_text()
    with db() as conn:
        row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return False, "短信不存在"
        status = row["status"]
        if status in {"queued", "retry_wait"}:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'cancelled', cancel_requested = 1, cancel_requested_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued','retry_wait')
                """,
                (now, now, item_id),
            )
            return True, "已取消"
        if status in {"sending", "dispatching"}:
            conn.execute(
                """
                UPDATE outbound_sms
                SET cancel_requested = 1, cancel_requested_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('sending','dispatching')
                """,
                (now, now, item_id),
            )
            conn.execute(
                """
                UPDATE sms_dispatch_jobs
                SET cancel_requested = 1, updated_at = ?
                WHERE outbound_id = ? AND status IN ('queued','leased','sending')
                """,
                (now, item_id),
            )
            return True, "已请求尝试取消，若短信已提交则不能撤回"
        if status == "submitted":
            return False, "短信已提交，不能取消"
        if status == "sent":
            return False, "短信已发送，不能取消"
        if status == "ambiguous":
            return False, "短信状态不明，请人工处理"
        if status == "cancelled":
            return False, "短信已取消"
        if status == "failed":
            return False, "短信已失败"
    return False, "当前状态不能取消"


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


def submitted_outbox_path(command_output: str) -> Path | None:
    for line in command_output.splitlines():
        for token in line.split():
            if "/outbox/" not in token or not token.endswith(".smsbackup"):
                continue
            candidate = Path(token)
            if not candidate.is_absolute():
                continue
            if candidate.parent.resolve(strict=False) == OUTBOX.resolve(strict=False):
                return candidate
    return None


def submitted_wait_expired(row: sqlite3.Row) -> bool:
    submitted_at = parse_time(row["submitted_at"] or row["updated_at"])
    if not submitted_at:
        return False
    expires_at = submitted_at + dt.timedelta(minutes=SUBMITTED_SEND_TIMEOUT_MINUTES)
    return dt.datetime.now().astimezone() >= expires_at


def reconcile_submitted_row(row: sqlite3.Row) -> bool:
    outbox_path = submitted_outbox_path(row["command_output"] or "")
    if not outbox_path:
        return False
    filename = outbox_path.name
    sent_path = SENT / filename
    error_path = ERROR / filename
    now = now_text()
    if sent_path.exists():
        with db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'sent', updated_at = ?, last_error = ''
                WHERE id = ? AND status = 'submitted'
                """,
                (now, row["id"]),
            )
            final_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
        notify_plan_success(final_row)
        return True
    if error_path.exists():
        message = "smsd 已将消息移入 error 队列"
        with db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'failed', updated_at = ?, last_error = ?
                WHERE id = ? AND status = 'submitted'
                """,
                (now, message, row["id"]),
            )
            final_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
        notify_final_failure(final_row)
        return True
    if outbox_path.exists() and submitted_wait_expired(row):
        try:
            outbox_path.unlink()
            removed = "，已从 outbox 移除"
        except FileNotFoundError:
            removed = "，outbox 文件已不存在"
        except Exception as exc:
            removed = f"，移除 outbox 文件失败：{exc}"
        message = f"等待 smsd 发送超过 {SUBMITTED_SEND_TIMEOUT_MINUTES} 分钟{removed}，避免恢复后补发"
        with db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'send_timeout', updated_at = ?, last_error = ?
                WHERE id = ? AND status = 'submitted'
                """,
                (now, message, row["id"]),
            )
            final_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
        notify_final_failure(final_row)
        return True
    return False


def reconcile_submitted_outbounds(limit: int = 100) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM outbound_sms
            WHERE status = 'submitted'
            ORDER BY submitted_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    for row in rows:
        reconcile_submitted_row(row)


def reconcile_dispatch_row(job) -> bool:
    result_status = job["result_status"] or job["status"]
    message = job["result_message"] or ""
    now = now_text()
    submitted_followup: tuple[int, str] | None = None
    with db() as conn:
        row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (job["outbound_id"],)).fetchone()
        if not row or row["status"] not in {"sending", "dispatching"}:
            return False

        if result_status in {"submitted", "sent"}:
            submitted_at = job["submitted_at"] or job["completed_at"] or now
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = ?, submitted_at = ?, updated_at = ?, last_error = '', command_output = ?
                WHERE id = ? AND status IN ('sending','dispatching')
                """,
                (result_status, submitted_at, now, message[:2000], row["id"]),
            )
            submitted_followup = (row["id"], submitted_at)

        if result_status in {"cancelled", "skipped"}:
            if row["cancel_requested"]:
                conn.execute(
                    """
                    UPDATE outbound_sms
                    SET status = 'cancelled', updated_at = ?, last_error = ?
                    WHERE id = ? AND status IN ('sending','dispatching')
                    """,
                    (now, message[:2000], row["id"]),
                )
            return True

        if result_status == "ambiguous":
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'ambiguous', updated_at = ?, last_error = ?, command_output = ?
                WHERE id = ? AND status IN ('sending','dispatching')
                """,
                (now, message[:2000], message[:2000], row["id"]),
            )
            final_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
            notify_final_failure(final_row)
            return True

        if result_status == "failed":
            attempts = int(row["attempts"])
            max_retries = int(row["max_retries"])
            retry_interval = int(row["retry_interval_minutes"])
            if attempts <= max_retries:
                next_attempt = (dt.datetime.now().astimezone() + dt.timedelta(minutes=retry_interval)).strftime(TIME_FORMAT)
                conn.execute(
                    """
                    UPDATE outbound_sms
                    SET status = 'retry_wait', updated_at = ?, next_attempt_at = ?,
                        last_error = ?, command_output = ?
                    WHERE id = ? AND status IN ('sending','dispatching')
                    """,
                    (now, next_attempt, message[:2000], message[:2000], row["id"]),
                )
                return True
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'failed', updated_at = ?, last_error = ?, command_output = ?
                WHERE id = ? AND status IN ('sending','dispatching')
                """,
                (now, message[:2000], message[:2000], row["id"]),
            )
            final_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row["id"],)).fetchone()
            notify_final_failure(final_row)
            return True
    if submitted_followup:
        row_id, submitted_at = submitted_followup
        set_state("last_submission_at", submitted_at)
        with db() as conn:
            updated = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (row_id,)).fetchone()
        if updated["status"] == "sent":
            notify_plan_success(updated)
        advance_plan_after_success(updated, submitted_at)
        return True
    return False


def reconcile_dispatch_jobs(limit: int = 100) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sms_dispatch_jobs
            WHERE status IN ('submitted','sent','failed','ambiguous','cancelled','skipped')
            ORDER BY completed_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    for row in rows:
        reconcile_dispatch_row(row)


def sms_submission_blocked_message() -> str:
    health = read_smsd_health_status()
    if not health or health.get("sim_state_tone") not in {"bad", "warn"}:
        return ""
    state = health.get("sim_state") or "SIM 状态不可用"
    network_state = health.get("network_state") or "-"
    return f"SIM 当前不可发送：{state} / {network_state}，未写入 smsd 发送队列"


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
    if use_pve_dispatch():
        dispatched = now_text()
        with db() as conn:
            job_id = create_dispatch_job(conn, row)
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'dispatching', dispatch_job_id = ?, updated_at = ?, last_error = '', command_output = ''
                WHERE id = ? AND status = 'sending'
                """,
                (job_id, dispatched, row["id"]),
            )
        return
    timeout = parse_positive_int(str(row["command_timeout_seconds"]), 45, 10, 180)
    blocked_message = sms_submission_blocked_message()
    if blocked_message:
        result_status, message = "failed", blocked_message
    else:
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
    while not stop_event.wait(worker_interval_seconds()):
        try:
            forward_pending_inbound_sms()
            reconcile_dispatch_jobs()
            reconcile_submitted_outbounds()
            process_one_due()
        except Exception as exc:
            log_worker(f"worker_error={exc}")


def worker_interval_seconds(now: dt.datetime | None = None) -> float:
    current = now or dt.datetime.now().astimezone()
    elapsed = current.second + (current.microsecond / 1_000_000)
    if elapsed == 0:
        return 0.0
    return 60.0 - elapsed


def inbound_notification_loop(stop_event: threading.Event) -> None:
    url = sms_redis_url()
    if not url:
        return
    channel = inbound_channel()
    while not stop_event.is_set():
        sock = None
        try:
            sock, reader = redis_connect(url, timeout=5)
            sock.sendall(redis_command_bytes("SUBSCRIBE", channel))
            redis_read_value(reader)
            sock.settimeout(None)
            log_worker(f"redis_subscribed channel={channel}")
            while not stop_event.is_set():
                readable, _, _ = select.select([sock], [], [], 5)
                if not readable:
                    continue
                message = redis_read_value(reader)
                if not isinstance(message, list) or len(message) < 3 or message[0] != "message":
                    continue
                row_id = parse_inbound_notification_payload(str(message[2]))
                if row_id:
                    forwarded = forward_inbound_sms_by_id(row_id)
                    log_worker(f"redis_inbound row_id={row_id} forwarded={1 if forwarded else 0}")
        except Exception as exc:
            log_worker(f"redis_listener_error={exc}")
            stop_event.wait(5)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass


def start_inbound_notification_listener(stop_event: threading.Event) -> threading.Thread | None:
    if not sms_redis_url():
        return None
    thread = threading.Thread(target=inbound_notification_loop, args=(stop_event,), daemon=True)
    thread.start()
    return thread
