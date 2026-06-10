#!/usr/bin/env python3
import datetime as dt
import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
TIME_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def load_admin():
    base = pathlib.Path(tempfile.mkdtemp(prefix="sms-admin-test-", dir="/private/tmp"))
    os.environ["SMS_BASE"] = str(base)
    spec = importlib.util.spec_from_file_location("sms_admin_under_test", ROOT / "admin" / "sms_admin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, base


def cleanup(base):
    shutil.rmtree(base, ignore_errors=True)


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def test_strategy_schema_and_default_minutes():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            strategy = conn.execute("SELECT * FROM send_strategies WHERE is_default = 1").fetchone()
            assert_true(strategy is not None, "default strategy exists")
            assert_true(strategy["send_interval_minutes"] == 1, "default send interval is 1 minute")
            assert_true(strategy["retry_interval_minutes"] == 10, "default retry interval is 10 minutes")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(outbound_sms)").fetchall()}
            expected = {
                "send_type",
                "strategy_id",
                "strategy_name",
                "plan_id",
                "send_interval_minutes",
                "retry_interval_minutes",
                "command_timeout_seconds",
            }
            assert_true(expected.issubset(columns), "outbound strategy snapshot columns exist")
    finally:
        cleanup(base)


def test_create_outbound_records_type_and_strategy_snapshot():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Slow', 3, 7, 2, 31, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        item_id = admin.create_outbound(
            "13000000000",
            "hello",
            admin.now_text(),
            strategy_id=strategy_id,
            send_type="plan",
            plan_id=12,
        )
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(row["send_type"] == "plan", "outbound row records plan type")
        assert_true(row["strategy_id"] == strategy_id, "outbound row records strategy id")
        assert_true(row["strategy_name"] == "Slow", "outbound row records strategy name snapshot")
        assert_true(row["send_interval_minutes"] == 3, "outbound row records send interval minutes")
        assert_true(row["retry_interval_minutes"] == 7, "outbound row records retry interval minutes")
        assert_true(row["max_retries"] == 2, "outbound row records max retries")
        assert_true(row["command_timeout_seconds"] == 31, "outbound row records command timeout")
        assert_true(row["plan_id"] == 12, "outbound row records plan id")
    finally:
        cleanup(base)


def test_destination_from_form_applies_country_code():
    admin, base = load_admin()
    try:
        assert_true(
            admin.destination_from_form({"country_code": "+86", "destination": "13000000000"}) == "+8613000000000",
            "country code is prefixed to local number",
        )
        assert_true(
            admin.destination_from_form({"country_code": "1", "destination": "5551234567"}) == "+15551234567",
            "country code accepts digits without plus sign",
        )
        assert_true(
            admin.destination_from_form({"country_code": "+86", "destination": "+447700900123"}) == "+447700900123",
            "full international destination is kept when pasted",
        )
    finally:
        cleanup(base)


def test_send_interval_allows_180_days():
    admin, base = load_admin()
    try:
        admin.init_db()
        admin.save_strategy_from_form(
            {
                "name": "Half year interval",
                "send_interval_minutes": "259200",
                "retry_interval_minutes": "10",
                "max_retries": "1",
                "command_timeout_seconds": "45",
                "active": "yes",
            }
        )
        with admin.db() as conn:
            strategy = conn.execute("SELECT * FROM send_strategies WHERE name = 'Half year interval'").fetchone()
        assert_true(strategy["send_interval_minutes"] == 259200, "send interval allows 180 days in minutes")
    finally:
        cleanup(base)


def test_delete_strategy_removes_non_default_but_keeps_snapshots():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Delete me', 3, 7, 1, 45, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        item_id = admin.create_outbound(
            "13000000000",
            "snapshot",
            admin.now_text(),
            strategy_id=strategy_id,
            send_type="normal",
        )
        admin.delete_strategy(strategy_id)
        with admin.db() as conn:
            strategy = conn.execute("SELECT * FROM send_strategies WHERE id = ?", (strategy_id,)).fetchone()
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(strategy is None, "strategy is deleted")
        assert_true(row["strategy_name"] == "Delete me", "outbound snapshot keeps deleted strategy name")
    finally:
        cleanup(base)


def test_delete_default_strategy_is_blocked():
    admin, base = load_admin()
    try:
        admin.init_db()
        default_strategy = admin.get_default_strategy()
        try:
            admin.delete_strategy(default_strategy["id"])
        except ValueError as exc:
            assert_true("默认策略不能删除" in str(exc), "default delete reports clear error")
        else:
            raise AssertionError("default strategy delete should fail")
    finally:
        cleanup(base)


def test_dashboard_omits_cost_protection_notice():
    admin, base = load_admin()
    try:
        admin.init_db()

        class Dummy:
            table_inbound = admin.AdminHandler.table_inbound
            table_outbound = admin.AdminHandler.table_outbound

        class RunResult:
            stdout = "active"

        old_run = admin.subprocess.run
        old_get_sim_status = admin.get_sim_status
        try:
            admin.subprocess.run = lambda *args, **kwargs: RunResult()
            admin.get_sim_status = lambda: {
                "sim_state": "已识别",
                "signal": "80%",
                "network_level": "80%",
                "network_state": "接收可用 / 发送可用",
                "operator": "Test",
                "checked_at": admin.now_text(),
                "error": "",
            }
            body = admin.AdminHandler.render_dashboard(Dummy()).decode("utf-8")
        finally:
            admin.subprocess.run = old_run
            admin.get_sim_status = old_get_sim_status

        assert_true("费用保护" not in body, "dashboard omits cost protection notice")
        assert_true('class="panel band sim-status"' in body, "SIM status panel has dashboard spacing class")
    finally:
        cleanup(base)


def test_login_page_uses_border_box_sizing():
    admin, base = load_admin()
    try:
        body = admin.login_page().decode("utf-8")
        assert_true("box-sizing:border-box" in body, "login page uses border-box sizing")
        assert_true('input type="password"' in body, "login page renders password input")
    finally:
        cleanup(base)


def test_send_form_uses_default_strategy_and_country_code_field():
    admin, base = load_admin()
    try:
        admin.init_db()
        body = admin.AdminHandler.render_send(object()).decode("utf-8")
        form = body[body.index('<form class="panel" method="post" action="/send">') : body.index("</form>", body.index("/send"))]

        assert_true("strategy_id" not in form, "normal send form does not expose strategy selector")
        assert_true("<label>发送策略</label>" not in form, "normal send form hides strategy label")
        assert_true("scheduled_at" not in form, "normal send form does not expose scheduled time")
        assert_true('name="country_code"' in form, "normal send form has country code field")
        assert_true('value="+86"' in form, "normal send country code defaults to +86")
    finally:
        cleanup(base)


def test_plan_form_country_code_and_optional_time():
    admin, base = load_admin()
    try:
        admin.init_db()
        body = admin.AdminHandler.render_plans(object()).decode("utf-8")
        dialog = body[body.index('id="plan-create"') : body.index("</dialog>", body.index('id="plan-create"'))]
        form = dialog[dialog.index('<form method="post" action="/plans">') : dialog.index("</form>", dialog.index("/plans"))]

        assert_true('name="country_code"' in form, "plan form has country code field")
        assert_true('value="+86"' in form, "plan country code defaults to +86")
        assert_true('<input type="datetime-local" name="scheduled_at">' in form, "plan scheduled time is optional")
    finally:
        cleanup(base)


def test_optional_future_time_allows_blank_for_immediate_send():
    admin, base = load_admin()
    try:
        before = dt.datetime.now().astimezone()
        scheduled_at = admin.optional_future_time_from_form("")
        parsed = dt.datetime.strptime(scheduled_at, TIME_FORMAT)
        assert_true((parsed - before).total_seconds() < 5, "blank plan time is scheduled immediately")
    finally:
        cleanup(base)


def test_strategy_actions_show_delete_left_and_save_right():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Delete me', 3, 7, 1, 45, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)

        body = admin.AdminHandler.render_settings(object()).decode("utf-8")
        default_start = body.index("<h2>默认策略</h2>")
        default_section = body[default_start : body.index("</section>", default_start)]
        start = body.index("<h2>Delete me</h2>")
        section = body[start : body.index("</section>", start)]

        assert_true(default_section.count("默认策略不能删除") == 1, "default strategy hint appears once")
        assert_true('<div class="actions strategy-actions">' in section, "strategy actions use shared row")
        assert_true(section.index("删除策略") < section.index("保存策略"), "delete button is left of save button")
        assert_true(
            section.index("</form>") < section.index(f'<form id="strategy-delete-{strategy_id}"'),
            "delete form is outside save form",
        )
    finally:
        cleanup(base)


def test_parse_sim_status_output():
    admin, base = load_admin()
    try:
        output = """
Phone information
Network state        : home network
Network              : 460 00
Name in phone        : China Mobile
Signal strength      : -67 dBm
Network level        : 78 percent
SIM IMSI             : 460001234567890
"""
        info = admin.parse_sim_status_output(output)
        assert_true(info["sim_state"] == "已识别", "SIM IMSI marks SIM as identified")
        assert_true(info["signal"] == "-67 dBm", "signal strength is parsed")
        assert_true(info["network_level"] == "78 percent", "network level is parsed")
        assert_true(info["network_state"] == "home network", "network state is parsed")
        assert_true(info["operator"] == "China Mobile", "operator is parsed")
    finally:
        cleanup(base)


def test_read_smsd_phone_status_prefers_status_database():
    admin, base = load_admin()
    try:
        admin.init_db()
        conn = admin.sqlite3.connect(admin.GAMMU_DB)
        with conn:
            conn.execute(
                """
                CREATE TABLE phones (
                  ID TEXT NOT NULL,
                  UpdatedInDB NUMERIC NOT NULL,
                  InsertIntoDB NUMERIC NOT NULL,
                  TimeOut NUMERIC NOT NULL,
                  Send TEXT NOT NULL,
                  Receive TEXT NOT NULL,
                  IMEI TEXT NOT NULL PRIMARY KEY,
                  IMSI TEXT NOT NULL,
                  NetCode TEXT,
                  NetName TEXT,
                  Client TEXT NOT NULL,
                  Battery INTEGER NOT NULL,
                  Signal INTEGER NOT NULL,
                  Sent INTEGER NOT NULL,
                  Received INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO phones
                  (ID, UpdatedInDB, InsertIntoDB, TimeOut, Send, Receive, IMEI, IMSI,
                   NetCode, NetName, Client, Battery, Signal, Sent, Received)
                VALUES ('modem', '2026-06-10 11:11:11', '2026-06-10 11:00:00',
                        '2026-06-10 11:12:00', 'yes', 'yes', 'imei', 'imsi',
                        '46011', 'China Telecom', 'Gammu', 90, 78, 0, 0)
                """
            )
        status = admin.read_smsd_phone_status()
        assert_true(status["sim_state"] == "已识别", "smsd phone row marks SIM as identified")
        assert_true(status["signal"] == "78%", "smsd phone row provides signal percent")
        assert_true(status["battery"] == "90%", "smsd phone row provides battery percent")
        assert_true(status["operator"] == "China Telecom", "smsd phone row provides operator")
        assert_true(status["checked_at"] != "2026-06-10 11:11:11", "SIM checked time is current detection time")
    finally:
        cleanup(base)


def test_sms_command_uses_unicode_for_chinese_text():
    admin, base = load_admin()
    try:
        cmd = admin.build_sms_command("13000000000", "中文测试")
        assert_true("-unicode" in cmd, "Chinese SMS command uses gammu unicode mode")
        assert_true(cmd.index("-unicode") < cmd.index("-text"), "unicode flag is before text parameter")
    finally:
        cleanup(base)


def test_sms_command_keeps_gsm7_without_unicode_flag():
    admin, base = load_admin()
    try:
        cmd = admin.build_sms_command("13000000000", "hello")
        assert_true("-unicode" not in cmd, "GSM-7 SMS command does not force unicode")
    finally:
        cleanup(base)


def test_retry_wait_uses_minutes():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Retry two minutes', 0, 2, 1, 31, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        item_id = admin.create_outbound(
            "13000000000",
            "retry test",
            admin.now_text(),
            strategy_id=strategy_id,
            send_type="normal",
        )
        admin.submit_sms = lambda row, timeout: ("failed", "forced failure")
        before = dt.datetime.now().astimezone()
        admin.process_one_due()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        next_attempt = dt.datetime.strptime(row["next_attempt_at"], TIME_FORMAT)
        assert_true(row["status"] == "retry_wait", "failed row waits for retry")
        assert_true((next_attempt - before).total_seconds() >= 115, "retry is delayed by minutes, not seconds")
    finally:
        cleanup(base)


def test_normal_send_is_unbound_and_ignores_strategy_interval():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            conn.execute("UPDATE send_strategies SET send_interval_minutes = 1440 WHERE is_default = 1")

        item_id = admin.create_immediate_outbound("13000000000", "send now", admin.now_text())
        admin.set_state("last_submission_at", admin.now_text())
        admin.submit_sms = lambda row, timeout: ("submitted", "ok")
        admin.process_one_due()

        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(row["status"] == "submitted", "normal send ignores strategy/global interval")
        assert_true(row["strategy_id"] is None, "normal send is not bound to a strategy id")
        assert_true(row["strategy_name"] == "", "normal send is not bound to a strategy name")
        assert_true(row["send_interval_minutes"] == 0, "normal send records no send interval")
        assert_true(row["max_retries"] == 0, "normal send does not retry automatically")
    finally:
        cleanup(base)


def test_planned_send_is_not_blocked_by_global_submission_interval():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Daily plan', 1440, 10, 1, 31, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        plan_id = admin.create_send_plan("Due now", "13000000000", "planned now", admin.now_text(), strategy_id)
        with admin.db() as conn:
            original_outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]
        admin.set_state("last_submission_at", admin.now_text())
        admin.submit_sms = lambda row, timeout: ("submitted", "ok")
        admin.process_one_due()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (original_outbound_id,)).fetchone()
        assert_true(row["status"] == "submitted", "planned send ignores unrelated global last submission")
        assert_true(row["attempts"] == 1, "planned send was attempted once")
    finally:
        cleanup(base)


def test_planned_send_checks_only_same_plan_interval():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Daily plan', 1440, 10, 1, 31, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        plan_id = admin.create_send_plan("Due now", "13000000000", "planned now", admin.now_text(), strategy_id)
        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            conn.execute(
                """
                INSERT INTO outbound_sms
                  (created_at, updated_at, destination, text, scheduled_at, next_attempt_at,
                   status, attempts, max_retries, retry_interval_seconds, submitted_at, send_type,
                   strategy_id, strategy_name, plan_id, send_interval_minutes, retry_interval_minutes,
                   command_timeout_seconds)
                VALUES (?, ?, '13000000000', 'previous same plan', ?, ?, 'submitted', 1, 1, 600,
                        ?, 'plan', ?, 'Daily plan', ?, 1440, 10, 31)
                """,
                (admin.now_text(), admin.now_text(), admin.now_text(), admin.now_text(), admin.now_text(), strategy_id, plan_id),
            )
        admin.submit_sms = lambda row, timeout: (_ for _ in ()).throw(AssertionError("same plan interval should block submit"))
        admin.process_one_due()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (plan["outbound_id"],)).fetchone()
        assert_true(row["status"] == "queued", "same plan recent submission keeps item queued")
        assert_true(row["attempts"] == 0, "blocked planned send is not attempted")
    finally:
        cleanup(base)


def test_successful_plan_creates_next_outbound_from_strategy_interval():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Every three minutes', 3, 10, 1, 31, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        plan_id = admin.create_send_plan("Recurring", "13000000000", "planned again", admin.now_text(), strategy_id)
        with admin.db() as conn:
            original_plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            original_outbound_id = original_plan["outbound_id"]

        admin.submit_sms = lambda row, timeout: ("submitted", "ok")
        admin.process_one_due()

        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            sent = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (original_outbound_id,)).fetchone()
            next_row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (plan["outbound_id"],)).fetchone()
            plan_count = conn.execute("SELECT COUNT(*) AS c FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["c"]
        submitted_at = dt.datetime.strptime(sent["submitted_at"], TIME_FORMAT)
        next_scheduled = dt.datetime.strptime(next_row["scheduled_at"], TIME_FORMAT)
        assert_true(sent["status"] == "submitted", "original plan outbound is submitted")
        assert_true(plan_count == 1, "recurring send stays inside the same plan")
        assert_true(plan["outbound_id"] != original_outbound_id, "plan points at the next outbound row")
        assert_true(plan["scheduled_at"] == next_row["scheduled_at"], "plan scheduled time moves to the next send")
        assert_true(next_row["status"] == "queued", "next planned send is queued")
        assert_true(next_row["send_type"] == "plan", "next planned send keeps plan type")
        assert_true(next_row["plan_id"] == plan_id, "next planned send links to the same plan")
        assert_true(next_scheduled - submitted_at == dt.timedelta(minutes=3), "next send uses strategy send interval")
    finally:
        cleanup(base)


def test_successful_plan_with_zero_strategy_interval_does_not_create_next_outbound():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Zero interval', 0, 10, 1, 31, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        plan_id = admin.create_send_plan("Zero guard", "13000000000", "guard", admin.now_text(), strategy_id)
        with admin.db() as conn:
            original_outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]

        admin.submit_sms = lambda row, timeout: ("submitted", "ok")
        admin.process_one_due()

        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            sent = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (original_outbound_id,)).fetchone()
            queued_count = conn.execute(
                "SELECT COUNT(*) AS c FROM outbound_sms WHERE plan_id = ? AND status IN ('queued','retry_wait')",
                (plan_id,),
            ).fetchone()["c"]
        assert_true(sent["status"] == "submitted", "zero interval plan sends the current outbound")
        assert_true(plan["outbound_id"] == original_outbound_id, "zero interval plan keeps the submitted outbound")
        assert_true(plan["active"] == 0, "zero interval plan is marked inactive after success")
        assert_true(queued_count == 0, "zero interval plan does not queue another send")
    finally:
        cleanup(base)


def test_delete_send_plan_soft_deletes_only_without_send_history():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        plan_id = admin.create_send_plan("Delete plan", "13000000000", "delete me", admin.now_text(), strategy["id"])
        with admin.db() as conn:
            outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]

        admin.delete_send_plan(plan_id)

        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            outbound = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (outbound_id,)).fetchone()
        assert_true(plan["deleted"] == 1, "plan is soft deleted")
        assert_true(plan["active"] == 0, "deleted plan is inactive")
        assert_true(outbound["status"] == "cancelled", "current queued outbound is cancelled")
    finally:
        cleanup(base)


def test_archive_send_plan_keeps_plan_visible_after_send_history():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        plan_id = admin.create_send_plan("Archive plan", "13000000000", "archive me", admin.now_text(), strategy["id"])
        with admin.db() as conn:
            outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]
            conn.execute(
                "UPDATE outbound_sms SET status = 'submitted', attempts = 1, submitted_at = ?, updated_at = ? WHERE id = ?",
                (admin.now_text(), admin.now_text(), outbound_id),
            )

        try:
            admin.delete_send_plan(plan_id)
        except ValueError as exc:
            assert_true("已有发送记录" in str(exc), "delete is blocked once plan has send history")
        else:
            raise AssertionError("delete should be blocked for plans with send history")

        admin.archive_send_plan(plan_id)
        body = admin.AdminHandler.render_plans(object()).decode("utf-8")

        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
        assert_true(plan["archived"] == 1, "plan is archived")
        assert_true(plan["deleted"] == 0, "archived plan is not deleted")
        assert_true(plan["active"] == 0, "archived plan is inactive")
        assert_true("Archive plan" in body, "archived plan remains visible")
        assert_true("已归档" in body, "archived plan is labelled")
        table = body[body.index("<h2>发送计划</h2>") : body.index("</table>", body.index("<h2>发送计划</h2>"))]
        row = table[table.index("Archive plan") : table.index("</tr>", table.index("Archive plan"))]
        assert_true('<span class="badge muted">已归档</span>' in row, "archived plan keeps status label")
        assert_true('<span class="hint">已归档</span>' not in row, "archived plan action does not repeat archived text")
    finally:
        cleanup(base)


def test_plan_list_uses_modal_counts_next_time_and_archive_action_for_history():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        plan_id = admin.create_send_plan("Visible plan", "13000000000", "plan body", admin.now_text(), strategy["id"])
        with admin.db() as conn:
            current_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]
            conn.execute(
                """
                INSERT INTO outbound_sms
                  (created_at, updated_at, destination, text, scheduled_at, next_attempt_at,
                   status, attempts, max_retries, retry_interval_seconds, submitted_at, send_type,
                   strategy_id, strategy_name, plan_id, send_interval_minutes, retry_interval_minutes,
                   command_timeout_seconds)
                VALUES (?, ?, '13000000000', 'success', ?, ?, 'submitted', 1, 1, 600,
                        ?, 'plan', ?, '默认策略', ?, 1, 10, 45)
                """,
                (admin.now_text(), admin.now_text(), admin.now_text(), admin.now_text(), admin.now_text(), strategy["id"], plan_id),
            )
            conn.execute(
                """
                INSERT INTO outbound_sms
                  (created_at, updated_at, destination, text, scheduled_at, next_attempt_at,
                   status, attempts, max_retries, retry_interval_seconds, send_type,
                   strategy_id, strategy_name, plan_id, send_interval_minutes, retry_interval_minutes,
                   command_timeout_seconds, last_error)
                VALUES (?, ?, '13000000000', 'failure', ?, ?, 'failed', 2, 1, 600,
                        'plan', ?, '默认策略', ?, 1, 10, 45, 'bad')
                """,
                (admin.now_text(), admin.now_text(), admin.now_text(), admin.now_text(), strategy["id"], plan_id),
            )
        body = admin.AdminHandler.render_plans(object()).decode("utf-8")
        table = body[body.index("<h2>发送计划</h2>") : body.index("</table>", body.index("<h2>发送计划</h2>"))]

        assert_true("plan-create" in body, "new plan form is shown in a modal dialog")
        assert_true(f"plan-detail-{plan_id}" in body, "plan detail modal is present")
        assert_true(f'/plans/{plan_id}/archive' in body, "plan with send history has archive action")
        assert_true(f'/plans/{plan_id}/delete' not in body, "plan with send history does not show delete action")
        assert_true("<th>失败次数</th>" in table, "plan list shows failure count")
        assert_true("<th>成功次数</th>" in table, "plan list shows success count")
        assert_true("<th>下次发送</th>" in table, "plan list shows next send time")
        assert_true("<th>内容</th>" not in table, "plan list hides content column")
        assert_true("<th>错误</th>" not in table, "plan list hides error column")
        assert_true('class="count-pill failure">1</span>' in body, "failure count is red")
        assert_true('class="count-pill success">1</span>' in body, "success count is green")
        assert_true(str(current_id) in body, "current outbound id still appears in detail context")
    finally:
        cleanup(base)


def test_plan_list_shows_delete_for_plan_without_send_history_and_hides_deleted():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        plan_id = admin.create_send_plan("Hidden plan", "13000000000", "hidden", admin.now_text(), strategy["id"])
        body_before = admin.AdminHandler.render_plans(object()).decode("utf-8")
        assert_true(f'/plans/{plan_id}/delete' in body_before, "plan without send history shows delete action")
        assert_true(f'/plans/{plan_id}/archive' not in body_before, "plan without send history does not show archive action")
        admin.delete_send_plan(plan_id)
        body = admin.AdminHandler.render_plans(object()).decode("utf-8")
        assert_true("Hidden plan" not in body, "deleted plan is hidden from the plan list")
    finally:
        cleanup(base)


def test_outbound_page_uses_send_list_title_and_paginates_ten_per_page():
    admin, base = load_admin()
    try:
        admin.init_db()
        for i in range(12):
            admin.create_immediate_outbound(f"130000000{i:02d}", f"bulk {i}", admin.now_text())

        class Dummy:
            path = "/outbound?page=2"
            table_outbound = admin.AdminHandler.table_outbound

        body = admin.AdminHandler.render_outbound(Dummy()).decode("utf-8")
        table = body[body.index("<h2>发送列表</h2>") : body.index("</table>", body.index("<h2>发送列表</h2>"))]

        assert_true("<h2>最近发送</h2>" not in body, "outbound page title is send list")
        assert_true(table.count("<tr><td>#") == 2, "outbound page shows ten rows per page")
        assert_true("page=1" in body, "pagination keeps previous page link")
    finally:
        cleanup(base)


def test_outbound_page_filters_by_plan_strategy_status_and_type():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Filter strategy', 5, 10, 1, 31, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)
        plan_id = admin.create_send_plan("Filter plan", "13000000000", "planned filter hit", admin.now_text(), strategy_id)
        admin.create_immediate_outbound("13000000001", "normal miss", admin.now_text())
        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            conn.execute("UPDATE outbound_sms SET status = 'submitted', submitted_at = ?, updated_at = ? WHERE id = ?", (admin.now_text(), admin.now_text(), plan["outbound_id"]))

        class Dummy:
            path = f"/outbound?plan_id={plan_id}&strategy_id={strategy_id}&status=submitted&send_type=plan"
            table_outbound = admin.AdminHandler.table_outbound

        body = admin.AdminHandler.render_outbound(Dummy()).decode("utf-8")
        table = body[body.index("<h2>发送列表</h2>") : body.index("</table>", body.index("<h2>发送列表</h2>"))]

        assert_true('name="plan_id"' in body, "outbound filters include plan id")
        assert_true('name="strategy_id"' in body, "outbound filters include strategy")
        assert_true('name="status"' in body, "outbound filters include status")
        assert_true('name="send_type"' in body, "outbound filters include type")
        assert_true("planned filter hit" in table, "filtered plan row is shown")
        assert_true("normal miss" not in table, "non-matching normal row is hidden")
    finally:
        cleanup(base)


def test_outbound_filters_are_compact_right_aligned_and_plan_rows_do_not_cancel_there():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        plan_id = admin.create_send_plan("No list cancel", "13000000000", "plan queued", admin.now_text(), strategy["id"])
        normal_id = admin.create_immediate_outbound("13000000001", "normal queued", admin.now_text())

        class Dummy:
            path = "/outbound"
            table_outbound = admin.AdminHandler.table_outbound

        body = admin.AdminHandler.render_outbound(Dummy()).decode("utf-8")

        assert_true('class="panel wide filters compact-filters"' in body, "outbound filters use compact one-line class")
        assert_true(".compact-filters { display:flex; width:100%; justify-content:flex-end;" in body, "compact filters span full width and align right")
        assert_true("form.panel.wide.compact-filters { width:100%; max-width:none;" in body, "compact filters override wide panel max width")
        assert_true(f'/outbound/{normal_id}/cancel' in body, "normal queued send can still be cancelled from outbound list")
        with admin.db() as conn:
            plan_outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]
        assert_true(f'/outbound/{plan_outbound_id}/cancel' not in body, "planned queued send cannot be cancelled from outbound list")
        assert_true("计划列表操作" in body, "planned queued row directs cancellation to plan list")
    finally:
        cleanup(base)


def test_inbound_page_filters_by_sender_keyword_and_forward_status():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            conn.execute(
                """
                INSERT INTO inbound_sms
                  (received_at, sender, text, raw_ids, phone_id, sms_messages, decoded_parts, forwarded)
                VALUES (?, '+8613000000000', 'hello invoice', '', '', 1, 1, 1)
                """,
                (admin.now_text(),),
            )
            conn.execute(
                """
                INSERT INTO inbound_sms
                  (received_at, sender, text, raw_ids, phone_id, sms_messages, decoded_parts, forwarded)
                VALUES (?, '+8613999999999', 'other text', '', '', 1, 1, 0)
                """,
                (admin.now_text(),),
            )

        class Dummy:
            path = "/inbound?sender=130000&keyword=invoice&forwarded=1"
            table_inbound = admin.AdminHandler.table_inbound

        body = admin.AdminHandler.render_inbound(Dummy()).decode("utf-8")
        table = body[body.index("<h2>接收列表</h2>") : body.index("</table>", body.index("<h2>接收列表</h2>"))]

        assert_true('name="sender"' in body, "inbound filters include sender")
        assert_true('name="keyword"' in body, "inbound filters include keyword")
        assert_true('name="forwarded"' in body, "inbound filters include forward status")
        assert_true(".compact-filters { display:flex; width:100%;" in body, "inbound filters span full width")
        assert_true("form.panel.wide.compact-filters { width:100%; max-width:none;" in body, "inbound filters override wide panel max width")
        assert_true("hello invoice" in table, "matching inbound row is shown")
        assert_true("other text" not in table, "non-matching inbound row is hidden")
    finally:
        cleanup(base)


def test_create_send_plan_creates_planned_outbound():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        scheduled_at = (dt.datetime.now().astimezone() + dt.timedelta(minutes=20)).strftime(TIME_FORMAT)
        plan_id = admin.create_send_plan("Morning check", "13000000000", "planned hello", scheduled_at, strategy["id"])
        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            outbound = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (plan["outbound_id"],)).fetchone()
        assert_true(plan["strategy_id"] == strategy["id"], "plan records strategy id")
        assert_true(outbound["send_type"] == "plan", "planned outbound uses plan type")
        assert_true(outbound["plan_id"] == plan_id, "planned outbound links back to plan")
        assert_true(outbound["scheduled_at"] == scheduled_at, "planned outbound keeps scheduled time")
    finally:
        cleanup(base)


def main():
    tests = [
        test_strategy_schema_and_default_minutes,
        test_create_outbound_records_type_and_strategy_snapshot,
        test_destination_from_form_applies_country_code,
        test_send_interval_allows_180_days,
        test_delete_strategy_removes_non_default_but_keeps_snapshots,
        test_delete_default_strategy_is_blocked,
        test_dashboard_omits_cost_protection_notice,
        test_login_page_uses_border_box_sizing,
        test_send_form_uses_default_strategy_and_country_code_field,
        test_plan_form_country_code_and_optional_time,
        test_optional_future_time_allows_blank_for_immediate_send,
        test_strategy_actions_show_delete_left_and_save_right,
        test_parse_sim_status_output,
        test_read_smsd_phone_status_prefers_status_database,
        test_sms_command_uses_unicode_for_chinese_text,
        test_sms_command_keeps_gsm7_without_unicode_flag,
        test_retry_wait_uses_minutes,
        test_normal_send_is_unbound_and_ignores_strategy_interval,
        test_planned_send_is_not_blocked_by_global_submission_interval,
        test_planned_send_checks_only_same_plan_interval,
        test_successful_plan_creates_next_outbound_from_strategy_interval,
        test_successful_plan_with_zero_strategy_interval_does_not_create_next_outbound,
        test_delete_send_plan_soft_deletes_only_without_send_history,
        test_archive_send_plan_keeps_plan_visible_after_send_history,
        test_plan_list_uses_modal_counts_next_time_and_archive_action_for_history,
        test_plan_list_shows_delete_for_plan_without_send_history_and_hides_deleted,
        test_outbound_page_uses_send_list_title_and_paginates_ten_per_page,
        test_outbound_page_filters_by_plan_strategy_status_and_type,
        test_outbound_filters_are_compact_right_aligned_and_plan_rows_do_not_cancel_there,
        test_inbound_page_filters_by_sender_keyword_and_forward_status,
        test_create_send_plan_creates_planned_outbound,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
