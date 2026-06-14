#!/usr/bin/env python3
import datetime as dt
import importlib.machinery
import importlib.util
import json
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
    spec = importlib.util.spec_from_file_location("sms_admin_under_test", ROOT / "admin" / "main.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, base


def load_pve_agent():
    loader = importlib.machinery.SourceFileLoader("pve_radio_agent_under_test", str(ROOT / "bin" / "pve-radio-agent"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_pve_watchdog():
    loader = importlib.machinery.SourceFileLoader("pve_sim_watchdog_under_test", str(ROOT / "bin" / "pve-sim-watchdog"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_sms_hook():
    loader = importlib.machinery.SourceFileLoader("sms_received_hook_under_test", str(ROOT / "bin" / "sms-received-hook"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def cleanup(base):
    shutil.rmtree(base, ignore_errors=True)


def assert_true(value, message):
    if not value:
        raise AssertionError(message)


def test_postgres_initial_migration_contains_dispatch_and_cancel_fields():
    sql = (ROOT / "migrations" / "0001_initial_schema.sql").read_text(encoding="utf-8")
    assert_true("CREATE TABLE IF NOT EXISTS sms_dispatch_jobs" in sql, "migration creates dispatch jobs")
    assert_true("idempotency_key TEXT NOT NULL UNIQUE" in sql, "dispatch jobs enforce idempotency")
    assert_true("cancel_requested INTEGER NOT NULL DEFAULT 0" in sql, "migration stores cancel requests")
    assert_true("dispatch_job_id BIGINT" in sql, "outbound rows can reference dispatch jobs")


def test_postgres_migration_adds_send_plan_phone_parts_to_existing_tables():
    migration = ROOT / "migrations" / "0002_send_plan_phone_parts.sql"
    assert_true(migration.exists(), "follow-up migration exists for already-applied databases")
    sql = migration.read_text(encoding="utf-8")
    assert_true("ALTER TABLE send_plans" in sql, "migration alters existing send_plans table")
    assert_true("ADD COLUMN IF NOT EXISTS country_code" in sql, "migration adds country code to existing plans")
    assert_true("ADD COLUMN IF NOT EXISTS phone_number" in sql, "migration adds phone number to existing plans")


def test_dockerfile_sets_admin_container_timezone():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert_true("TZ=Asia/Shanghai" in dockerfile, "admin container declares Shanghai timezone")
    assert_true("tzdata" in dockerfile, "admin image installs timezone database")
    assert_true("/etc/localtime" in dockerfile, "admin image links localtime for Python astimezone")


def test_pve_agent_parses_unread_cmgl_messages():
    agent = load_pve_agent()
    output = """
+CMGL: 8,"REC UNREAD","+8613000000000","","26/06/11,10:30:00+32"
77ED4FE16D4B8BD5

OK
"""
    rows = agent.parse_cmgl_messages(output)
    assert_true(len(rows) == 1, "one unread CMGL message is parsed")
    assert_true(rows[0]["index"] == 8, "message index is parsed")
    assert_true(rows[0]["sender"] == "+8613000000000", "sender is parsed")
    assert_true(rows[0]["text"] == "短信测试", "UCS2 message body is decoded")
    assert_true(rows[0]["raw_ids"] == "pve-cmgl-8", "raw id is stable")


def test_pve_agent_dispatch_uses_gammu_smsd_inject():
    agent = load_pve_agent()
    cmd = agent.build_gammu_sms_command("+8613000000000", "中文测试")
    assert_true("gammu-smsd-inject" in cmd[0], "PVE dispatch uses gammu-smsd-inject")
    assert_true("TEXT" in cmd, "PVE dispatch injects text SMS")
    assert_true("-unicode" in cmd, "Unicode text is submitted as unicode")
    assert_true("AT+CMGS" not in " ".join(cmd), "PVE dispatch does not use direct AT sending")


def test_pve_agent_send_sms_gammu_marks_sent_when_smsd_moves_spool_file():
    agent = load_pve_agent()
    base = pathlib.Path(tempfile.mkdtemp(prefix="pve-agent-spool-", dir="/private/tmp"))
    outbox = base / "outbox"
    sent = base / "sent"
    error = base / "error"
    outbox.mkdir()
    sent.mkdir()
    error.mkdir()
    filename = "OUTC20260611_194500_00_+8613000000000_sms0.smsbackup"
    outbox_file = outbox / filename
    sent_file = sent / filename
    outbox_file.write_text("queued sms", encoding="utf-8")
    sent_file.write_text("sent sms", encoding="utf-8")

    class RunResult:
        returncode = 0
        stdout = f"Written message with ID {outbox_file}\n"
        stderr = ""

    sentinel = object()
    saved = {
        name: getattr(agent, name, sentinel)
        for name in ("OUTBOX_PATH", "SENT_PATH", "ERROR_PATH", "SENT_WAIT_SECONDS")
    }
    old_command = agent.build_gammu_sms_command
    old_run = agent.subprocess.run
    try:
        agent.OUTBOX_PATH = outbox
        agent.SENT_PATH = sent
        agent.ERROR_PATH = error
        agent.SENT_WAIT_SECONDS = 0
        agent.build_gammu_sms_command = lambda destination, text: ["gammu-smsd-inject"]
        agent.subprocess.run = lambda cmd, **kwargs: RunResult()
        status, message = agent.send_sms_gammu("+8613000000000", "sent by PVE")
    finally:
        agent.build_gammu_sms_command = old_command
        agent.subprocess.run = old_run
        for name, value in saved.items():
            if value is sentinel:
                try:
                    delattr(agent, name)
                except AttributeError:
                    pass
            else:
                setattr(agent, name, value)
        cleanup(base)

    assert_true(status == "sent", "PVE agent reports sent after smsd moves the spool file to sent")
    assert_true(str(outbox_file) in message, "PVE agent keeps gammu output for audit")


def test_pve_agent_status_reports_failed_smsd_over_stale_monitor_memory():
    agent = load_pve_agent()
    calls = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(returncode=3, stdout="failed\n")
        if cmd and cmd[0] == "journalctl":
            return RunResult(
                stdout="Jun 11 19:21:02 pve gammu-smsd[56074]: Can't open device: Error opening device. Unknown, busy or no permissions. (DEVICEOPENERROR[2])\n"
            )
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 460115033554699\nNetworkSignal: 75\nSent: 1\nReceived: 2\nFailed: 0\n")
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    try:
        agent.subprocess.run = fake_run
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run

    assert_true(calls and calls[0][:3] == ["systemctl", "is-active", "gammu-smsd.service"], "PVE agent checks smsd service before trusting monitor memory")
    assert_true(status["sim_state_tone"] == "bad", "failed smsd is reported as bad status")
    assert_true(status["sim_identity"] == "", "stale IMSI is not returned when smsd is failed")
    assert_true(status["registration_state"] != "smsd 运行中", "failed smsd does not report running state")
    assert_true(status["error"], "failed smsd includes an operator-visible error")


def test_pve_agent_status_reports_active_smsd_init_errors_over_empty_monitor_identity():
    agent = load_pve_agent()
    calls = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: \nNetworkSignal: 0\nSent: 0\nReceived: 0\nFailed: 0\n")
        if cmd and cmd[0] == "journalctl":
            return RunResult(stdout="Jun 11 19:34:52 pve gammu-smsd[134478]: Error at init connection: Unknown error. (UNKNOWN[27])\n")
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    try:
        agent.subprocess.run = fake_run
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run

    assert_true(any(cmd and cmd[0] == "journalctl" for cmd in calls), "PVE agent checks recent smsd logs when monitor has no IMSI")
    assert_true(status["sim_state_tone"] == "bad", "active smsd init errors are reported as bad status")
    assert_true(status["sim_identity"] == "", "empty monitor IMSI stays empty")
    assert_true(status["registration_state"] == "smsd 串口通信失败", "recent init errors override empty monitor running state")
    assert_true(status["error"] == "smsd 串口通信失败", "recent init errors are surfaced")


def test_pve_agent_status_checks_at_presence_over_stale_monitor_imsi():
    agent = load_pve_agent()
    commands = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    class FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeAT:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def command(self, command, **kwargs):
            commands.append(command)
            responses = {
                "AT": "OK",
                "ATE0": "OK",
                "AT+CPIN?": "+CME ERROR: 13",
                "AT+QSIMSTAT?": "+QSIMSTAT: 0,0\n\nOK",
            }
            return responses.get(command, "OK")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 460115033554699\nNetworkSignal: 100\nSent: 0\nReceived: 0\nFailed: 0\n")
        if cmd and cmd[0] == "journalctl":
            return RunResult(stdout="")
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    old_lock = agent.RadioLock
    old_at = agent.ATSession
    try:
        agent.subprocess.run = fake_run
        agent.RadioLock = FakeLock
        agent.ATSession = FakeAT
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run
        agent.RadioLock = old_lock
        agent.ATSession = old_at

    assert_true("AT+CPIN?" in commands and "AT+QSIMSTAT?" in commands, "PVE agent checks AT SIM presence")
    assert_true(status["sim_state"] == "未识别（无法访问 SIM 卡）", "AT no-SIM overrides stale monitor IMSI")
    assert_true(status["sim_state_tone"] == "bad", "AT no-SIM uses bad tone")
    assert_true(status["sim_identity"] == "", "stale monitor IMSI is cleared")
    assert_true(status["error"] == "smsd 无法访问 SIM 卡", "AT no-SIM error is surfaced")


def test_pve_agent_status_checks_mbim_presence_over_stale_monitor_imsi():
    agent = load_pve_agent()
    mbim_calls = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    class FailingAT:
        def __enter__(self):
            raise OSError("AT port busy")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 460115033554699\nNetworkSignal: 100\nSent: 0\nReceived: 0\nFailed: 0\n")
        if cmd and cmd[0] == "mbimcli":
            mbim_calls.append(cmd)
            return RunResult(stdout="Ready state: 'sim-not-inserted'\nSIM ICCID: 'unknown'\n")
        if cmd and cmd[0] == "journalctl":
            return RunResult(stdout="")
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    old_at = agent.ATSession
    try:
        agent.subprocess.run = fake_run
        agent.ATSession = FailingAT
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run
        agent.ATSession = old_at

    assert_true(mbim_calls, "PVE agent checks MBIM subscriber readiness")
    assert_true(status["sim_state"] == "未识别（无法访问 SIM 卡）", "MBIM no-SIM overrides stale monitor IMSI")
    assert_true(status["sim_identity"] == "", "MBIM no-SIM clears stale monitor IMSI")


def test_pve_agent_status_checks_mbim_presence_over_empty_monitor_identity():
    agent = load_pve_agent()

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: \nNetworkSignal: 0\nSent: 0\nReceived: 0\nFailed: 0\n")
        if cmd and cmd[0] == "mbimcli":
            return RunResult(stdout="Ready state: 'sim-not-inserted'\nSIM ICCID: 'unknown'\n")
        if cmd and cmd[0] == "journalctl":
            return RunResult(stdout="Error at init connection: Unknown error. (UNKNOWN[27])\n")
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    try:
        agent.subprocess.run = fake_run
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run

    assert_true(status["sim_state"] == "未识别（无法访问 SIM 卡）", "MBIM no-SIM overrides empty monitor and smsd log errors")
    assert_true(status["registration_state"] == "smsd 无法访问 SIM 卡", "empty monitor plus MBIM no-SIM uses SIM-specific state")


def test_pve_agent_status_reads_phone_number_from_mbim():
    agent = load_pve_agent()

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 460115033554699\nNetworkSignal: 100\nSent: 0\nReceived: 0\nFailed: 0\n")
        if cmd and cmd[0] == "mbimcli":
            return RunResult(
                stdout="""
[/dev/wwan0mbim0] Subscriber ready status retrieved:
             Ready state: 'initialized'
            Subscriber ID: '460115033554699'
                SIM ICCID: '89861121234567890123'
       Telephone numbers: (1) '+8613800138000'
"""
            )
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    try:
        agent.subprocess.run = fake_run
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run

    assert_true(status["phone_number"] == "+8613800138000", "PVE status exposes SIM phone number from MBIM")
    assert_true(status["country_code"] == "86", "PVE status exposes country code from MBIM")
    assert_true(status["phone_number_e164"] == "+86 - 13800138000", "PVE status exposes formatted phone number from MBIM")


def test_pve_agent_status_builds_e164_for_uk_local_mbim_number():
    agent = load_pve_agent()

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 234102356147404\nNetworkSignal: 72\nSent: 0\nReceived: 5\nFailed: 0\n")
        if cmd and cmd[0] == "mbimcli":
            return RunResult(
                stdout="""
[/dev/wwan0mbim0] Subscriber ready status retrieved:
             Ready state: 'initialized'
            Subscriber ID: '234102356147404'
                SIM ICCID: '8944110069285243963F'
       Telephone numbers: (1) '07922675277'
"""
            )
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    try:
        agent.subprocess.run = fake_run
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run

    assert_true(status["phone_number"] == "07922675277", "PVE status keeps the local MBIM phone number")
    assert_true(status["country_code"] == "44", "PVE status infers UK country code from MBIM")
    assert_true(status["phone_number_e164"] == "+44 - 7922675277", "PVE status exposes formatted UK phone number")
    assert_true(status["operator"] == "O2 UK", "PVE status infers UK O2 operator from IMSI")


def test_pve_agent_reuses_static_identity_without_rereading_mbim_number():
    agent = load_pve_agent()
    mbim_number_reads = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 234102356147404\nNetworkSignal: 100\nSent: 1\nReceived: 2\nFailed: 0\n")
        return RunResult()

    def fake_read_mbim_number_info():
        mbim_number_reads.append(1)
        if len(mbim_number_reads) > 1:
            raise AssertionError("MBIM number info should be cached after first successful read")
        return "07922675277", "44", "+44 - 7922675277"

    old_run = agent.subprocess.run
    old_read_number = agent.read_mbim_number_info
    old_presence = agent.read_sim_presence
    old_wwan = agent.read_wwan_state
    old_signal = agent.read_at_signal_dbm
    old_cache = getattr(agent, "STATIC_SIM_CACHE", {}).copy()
    try:
        agent.STATIC_SIM_CACHE.clear()
        agent.subprocess.run = fake_run
        agent.read_mbim_number_info = fake_read_mbim_number_info
        agent.read_sim_presence = lambda: "ready"
        agent.read_wwan_state = lambda: "DOWN"
        agent.read_at_signal_dbm = lambda: -51
        first = agent.query_status()
        second = agent.query_status()
    finally:
        agent.subprocess.run = old_run
        agent.read_mbim_number_info = old_read_number
        agent.read_sim_presence = old_presence
        agent.read_wwan_state = old_wwan
        agent.read_at_signal_dbm = old_signal
        agent.STATIC_SIM_CACHE.clear()
        agent.STATIC_SIM_CACHE.update(old_cache)

    assert_true(len(mbim_number_reads) == 1, "PVE agent reads MBIM number info only once")
    assert_true(first["phone_number_e164"] == "+44 - 7922675277", "first status stores formatted phone number")
    assert_true(second["phone_number_e164"] == "+44 - 7922675277", "second status reuses cached formatted phone number")
    assert_true(second["operator"] == "O2 UK", "operator remains available from static identity")


def test_pve_agent_status_converts_monitor_signal_to_dbm_without_at_query():
    agent = load_pve_agent()
    commands = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    class FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeAT:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def command(self, command, **kwargs):
            commands.append(command)
            raise AssertionError("monitor signal should avoid slow AT signal query")
            responses = {
                "AT": "OK",
                "ATE0": "OK",
                "AT+CSQ": "+CSQ: 23,99\n\nOK",
                'AT+QENG="servingcell"': '+QENG: "servingcell","SEARCH"\n\nOK',
            }
            return responses.get(command, "OK")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 234102356147404\nNetworkSignal: 100\nSent: 0\nReceived: 5\nFailed: 0\n")
        if cmd and cmd[0] == "mbimcli":
            return RunResult(
                stdout="""
[/dev/wwan0mbim0] Subscriber ready status retrieved:
             Ready state: 'initialized'
            Subscriber ID: '234102356147404'
                SIM ICCID: '8944110069285243963F'
       Telephone numbers: (1) '07922675277'
"""
            )
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    old_lock = agent.RadioLock
    old_at = agent.ATSession
    try:
        agent.subprocess.run = fake_run
        agent.RadioLock = FakeLock
        agent.ATSession = FakeAT
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run
        agent.RadioLock = old_lock
        agent.ATSession = old_at

    assert_true("AT+CSQ" not in commands, "PVE agent avoids slow AT signal quality when monitor signal is available")
    assert_true(status["signal"] == "-51 dBm", "PVE status converts monitor percentage to dBm")
    assert_true(status["signal_dbm"] == -51, "PVE status exposes numeric monitor-derived dBm")


def test_pve_agent_status_does_not_convert_unknown_signal_to_dbm():
    agent = load_pve_agent()

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    class FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeAT:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def command(self, command, **kwargs):
            responses = {
                "AT": "OK",
                "ATE0": "OK",
                "AT+CSQ": "+CSQ: 99,99\n\nOK",
                'AT+QENG="servingcell"': '+QENG: "servingcell","SEARCH"\n\nOK',
            }
            return responses.get(command, "OK")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(stdout="active\n")
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="IMSI: 234102356147404\nNetworkSignal: 0\nSent: 0\nReceived: 5\nFailed: 0\n")
        if cmd and cmd[0] == "mbimcli":
            return RunResult(stdout="Ready state: 'initialized'\nSubscriber ID: '234102356147404'\n")
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    old_lock = agent.RadioLock
    old_at = agent.ATSession
    try:
        agent.subprocess.run = fake_run
        agent.RadioLock = FakeLock
        agent.ATSession = FakeAT
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run
        agent.RadioLock = old_lock
        agent.ATSession = old_at

    assert_true(status["signal"] == "", "unknown monitor and AT signal is hidden instead of shown as 0 dBm")
    assert_true(status["signal_dbm"] == "", "unknown signal has no numeric dBm")


def test_pve_agent_status_checks_at_presence_when_smsd_is_inactive():
    agent = load_pve_agent()
    commands = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    class FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeAT:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def command(self, command, **kwargs):
            commands.append(command)
            return {
                "AT": "OK",
                "ATE0": "OK",
                "AT+CPIN?": "+CME ERROR: 13",
                "AT+QSIMSTAT?": "+QSIMSTAT: 0,0\n\nOK",
            }.get(command, "OK")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "is-active", "gammu-smsd.service"]:
            return RunResult(returncode=3, stdout="inactive\n")
        if cmd and cmd[0] == "journalctl":
            return RunResult(stdout="Jun 11 19:53:42 pve gammu-smsd[137320]: Error at init connection: Unknown error. (UNKNOWN[27])\n")
        if cmd[:4] == ["ip", "-brief", "link", "show"]:
            return RunResult(stdout="wwan0            DOWN\n")
        return RunResult()

    old_run = agent.subprocess.run
    old_lock = agent.RadioLock
    old_at = agent.ATSession
    try:
        agent.subprocess.run = fake_run
        agent.RadioLock = FakeLock
        agent.ATSession = FakeAT
        status = agent.query_status()
    finally:
        agent.subprocess.run = old_run
        agent.RadioLock = old_lock
        agent.ATSession = old_at

    assert_true("AT+CPIN?" in commands and "AT+QSIMSTAT?" in commands, "inactive smsd status checks AT SIM presence")
    assert_true(status["sim_state"] == "未识别（无法访问 SIM 卡）", "inactive smsd plus AT no-SIM reports no SIM")
    assert_true(status["registration_state"] == "smsd 无法访问 SIM 卡", "inactive smsd no-SIM uses SIM-specific state")


def test_pve_watchdog_treats_empty_monitor_imsi_as_not_ready():
    watchdog = load_pve_watchdog()

    class RunResult:
        returncode = 0
        stdout = "PhoneID: pve-quectel\nIMEI: \nIMSI: \nNetworkSignal: 0\n"
        stderr = ""

    old_exists = watchdog.os.path.exists
    old_run = watchdog.subprocess.run
    try:
        watchdog.os.path.exists = lambda path: True
        watchdog.subprocess.run = lambda cmd, **kwargs: RunResult()
        ready, details = watchdog.smsd_monitor_status()
    finally:
        watchdog.os.path.exists = old_exists
        watchdog.subprocess.run = old_run

    assert_true(not ready, "empty monitor IMSI is not SIM ready")
    assert_true("monitor_ready=False" in details, "watchdog details explain monitor is not ready")


def test_pve_watchdog_prefers_mbim_presence_over_stale_monitor_imsi():
    watchdog = load_pve_watchdog()
    calls = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd and "gammu-smsd-monitor" in cmd[0]:
            return RunResult(stdout="PhoneID: pve-quectel\nIMEI: 015930000049750\nIMSI: 460115033554699\nNetworkSignal: 75\n")
        if cmd and cmd[0] == "mbimcli":
            return RunResult(stdout="Ready state: 'sim-not-inserted'\nSIM ICCID: 'unknown'\n")
        return RunResult()

    old_exists = watchdog.os.path.exists
    old_run = watchdog.subprocess.run
    try:
        watchdog.os.path.exists = lambda path: True
        watchdog.subprocess.run = fake_run
        ready, details = watchdog.sim_is_ready()
    finally:
        watchdog.os.path.exists = old_exists
        watchdog.subprocess.run = old_run

    assert_true(any(cmd and cmd[0] == "mbimcli" for cmd in calls), "watchdog checks MBIM subscriber readiness")
    assert_true(not ready, "MBIM no-SIM overrides stale monitor IMSI")
    assert_true("sim-not-inserted" in details, "watchdog details include MBIM no-SIM state")


def test_pve_watchdog_recovery_reenables_mbim_software_radio():
    watchdog = load_pve_watchdog()
    calls = []

    class RunResult:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    class FakeLock:
        def __enter__(self):
            calls.append(["lock"])
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append(["unlock"])
            return False

    old_run = watchdog.subprocess.run
    old_at = watchdog.at_command
    old_sleep = watchdog.time.sleep
    old_lock = watchdog.RadioLock
    old_ready = watchdog.sim_is_ready
    old_force = watchdog.force_wwan_down
    old_log = watchdog.log
    try:
        watchdog.subprocess.run = lambda cmd, **kwargs: calls.append(cmd) or RunResult(stdout="ok")
        watchdog.at_command = lambda command, timeout=8: calls.append(["at", command, timeout]) or "OK"
        watchdog.time.sleep = lambda seconds: calls.append(["sleep", seconds])
        watchdog.RadioLock = FakeLock
        watchdog.sim_is_ready = lambda: (True, "mbim_ready_state=initialized")
        watchdog.force_wwan_down = lambda: calls.append(["force_wwan_down"])
        watchdog.log = lambda message: calls.append(["log", message])
        watchdog.recover_sim()
    finally:
        watchdog.subprocess.run = old_run
        watchdog.at_command = old_at
        watchdog.time.sleep = old_sleep
        watchdog.RadioLock = old_lock
        watchdog.sim_is_ready = old_ready
        watchdog.force_wwan_down = old_force
        watchdog.log = old_log

    assert_true(
        ["mbimcli", "-p", "-d", watchdog.MBIM_DEV, "--set-radio-state=on"] in calls,
        "watchdog recovery explicitly re-enables MBIM software radio",
    )
    assert_true(
        any(call[:3] == ["systemctl", "restart", "quectel-radio-unlock.service"] for call in calls),
        "watchdog reruns the FCC/radio unlock service during recovery",
    )


def test_pve_watchdog_main_does_not_force_wwan_down_when_sim_ready():
    watchdog = load_pve_watchdog()
    calls = []

    old_sim_is_ready = watchdog.sim_is_ready
    old_force = watchdog.force_wwan_down
    old_recover = watchdog.recover_sim
    old_log = watchdog.log
    try:
        watchdog.sim_is_ready = lambda: (True, "monitor_ready=True")
        watchdog.force_wwan_down = lambda: calls.append("force_wwan_down")
        watchdog.recover_sim = lambda: calls.append("recover_sim")
        watchdog.log = lambda message: calls.append(("log", message))
        result = watchdog.main()
    finally:
        watchdog.sim_is_ready = old_sim_is_ready
        watchdog.force_wwan_down = old_force
        watchdog.recover_sim = old_recover
        watchdog.log = old_log

    assert_true(result == 0, "ready watchdog exits successfully")
    assert_true("force_wwan_down" not in calls, "ready watchdog does not force wwan0 down")
    assert_true("recover_sim" not in calls, "ready watchdog does not run recovery")


def test_sms_received_hook_can_disable_local_mail_forwarding():
    hook = load_sms_hook()
    previous = os.environ.get("SMS_FORWARD_IN_HOOK")
    try:
        os.environ["SMS_FORWARD_IN_HOOK"] = "0"
        assert_true(not hook.should_forward_in_hook(), "PVE hook can disable local mail forwarding")
        os.environ["SMS_FORWARD_IN_HOOK"] = "1"
        assert_true(hook.should_forward_in_hook(), "legacy hook can keep local mail forwarding")
    finally:
        if previous is None:
            os.environ.pop("SMS_FORWARD_IN_HOOK", None)
        else:
            os.environ["SMS_FORWARD_IN_HOOK"] = previous


def test_sms_received_hook_publishes_inbound_notification_when_configured():
    hook = load_sms_hook()
    previous_url = os.environ.get("SMS_REDIS_URL")
    previous_channel = os.environ.get("SMS_INBOUND_CHANNEL")
    calls = []
    old_publish = hook.redis_publish
    try:
        os.environ["SMS_REDIS_URL"] = "redis://127.0.0.1:6379/0"
        os.environ["SMS_INBOUND_CHANNEL"] = "sms:inbound"
        hook.redis_publish = lambda url, channel, payload: calls.append((url, channel, payload))
        hook.publish_inbound_notification(42)
        assert_true(calls, "hook publishes inbound notification when Redis URL is configured")
        assert_true(calls[0][0] == "redis://127.0.0.1:6379/0", "hook uses configured Redis URL")
        assert_true(calls[0][1] == "sms:inbound", "hook publishes to inbound channel")
        assert_true(json.loads(calls[0][2])["inbound_id"] == 42, "hook payload contains inbound id")
    finally:
        hook.redis_publish = old_publish
        if previous_url is None:
            os.environ.pop("SMS_REDIS_URL", None)
        else:
            os.environ["SMS_REDIS_URL"] = previous_url
        if previous_channel is None:
            os.environ.pop("SMS_INBOUND_CHANNEL", None)
        else:
            os.environ["SMS_INBOUND_CHANNEL"] = previous_channel


def write_gammu_backup(path: pathlib.Path, ref: str, total: int, seq: int, text: str, timestamp: str = "20260611T130646") -> None:
    encoded = text.encode("utf-16-be").hex().upper()
    path.write_text(
        f"""; This file format was designed for Gammu and is compatible with Gammu+
; Saved {timestamp} (Thu Jun 11 13:06:46 2026)

[SMSBackup000]
UDH = 050003{ref}{total:02X}{seq:02X}
DateTime = {timestamp}
Number = "13003630212"
Text00 = {encoded}
Coding = Unicode_No_Compression
Length = {len(text)}
""",
        encoding="utf-8",
    )


def load_sms_hook_with_base(base: pathlib.Path, wait_seconds: str = "45"):
    old_base = os.environ.get("SMS_BASE")
    old_wait = os.environ.get("SMS_MULTIPART_WAIT_SECONDS")
    try:
        os.environ["SMS_BASE"] = str(base)
        os.environ["SMS_MULTIPART_WAIT_SECONDS"] = wait_seconds
        return load_sms_hook()
    finally:
        if old_base is None:
            os.environ.pop("SMS_BASE", None)
        else:
            os.environ["SMS_BASE"] = old_base
        if old_wait is None:
            os.environ.pop("SMS_MULTIPART_WAIT_SECONDS", None)
        else:
            os.environ["SMS_MULTIPART_WAIT_SECONDS"] = old_wait


def test_sms_received_hook_defers_and_combines_multipart_within_short_window():
    base = pathlib.Path(tempfile.mkdtemp(prefix="sms-hook-multipart-", dir="/private/tmp"))
    try:
        inbox = base / "spool" / "inbox"
        inbox.mkdir(parents=True)
        first = inbox / "IN20260611_130646_00_13003630212_00.txt"
        second = inbox / "IN20260611_130645_00_13003630212_00.txt"
        timestamp = dt.datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        write_gammu_backup(first, "0C", 2, 1, "Ollama 对系统要求很低\n", timestamp)
        write_gammu_backup(second, "0C", 2, 2, "ux Kernel", timestamp)
        hook = load_sms_hook_with_base(base, "45")
        flushes = []
        old_spawn = hook.spawn_multipart_flush
        try:
            hook.spawn_multipart_flush = lambda key, deadline: flushes.append((key, deadline))
            first_record = hook.prepare_inbound_record(
                "2026-06-11 13:07:08 +0800",
                "13003630212",
                "Ollama 对系统要求很低\n",
                [first.name],
                "pve-quectel",
                1,
                1,
            )
            second_record = hook.prepare_inbound_record(
                "2026-06-11 13:07:20 +0800",
                "13003630212",
                "ux Kernel",
                [second.name],
                "pve-quectel",
                1,
                1,
            )
        finally:
            hook.spawn_multipart_flush = old_spawn

        assert_true(first_record is None, "first multipart part is deferred inside the short window")
        assert_true(flushes, "deferred multipart part schedules a bounded flush")
        assert_true(second_record is not None, "second multipart part completes the deferred record")
        assert_true(second_record["text"] == "Ollama 对系统要求很低\nux Kernel", "multipart text is combined in sequence")
        assert_true(second_record["raw_ids"] == f"{first.name} {second.name}", "combined record keeps both raw ids")
        assert_true(second_record["sms_messages"] == 2, "combined record stores expected part count")
        assert_true(second_record["decoded_parts"] == 2, "combined record stores decoded part count")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_sms_received_hook_flushes_incomplete_multipart_after_short_window():
    base = pathlib.Path(tempfile.mkdtemp(prefix="sms-hook-timeout-", dir="/private/tmp"))
    try:
        inbox = base / "spool" / "inbox"
        inbox.mkdir(parents=True)
        first = inbox / "IN20260611_130646_00_13003630212_00.txt"
        write_gammu_backup(first, "0D", 2, 1, "验证码 123456", "20200101T000000")
        hook = load_sms_hook_with_base(base, "45")
        record = hook.prepare_inbound_record(
            "2026-06-11 13:07:08 +0800",
            "13003630212",
            "验证码 123456",
            [first.name],
            "pve-quectel",
            1,
            1,
        )

        assert_true(record is not None, "expired multipart window flushes available text")
        assert_true("验证码 123456" in record["text"], "flushed multipart keeps available code text")
        assert_true("分片未齐" in record["text"], "flushed multipart is marked incomplete")
        assert_true(record["sms_messages"] == 2, "flushed multipart records expected total parts")
        assert_true(record["decoded_parts"] == 1, "flushed multipart records available decoded parts")
    finally:
        shutil.rmtree(base, ignore_errors=True)


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


def test_split_destination_for_form_separates_known_country_codes():
    admin, base = load_admin()
    try:
        country_code, phone_number = admin.split_destination_for_form("+447922675277")
        assert_true(country_code == "+44", "UK destination country code is split for editing")
        assert_true(phone_number == "7922675277", "UK destination local number is split for editing")

        country_code, phone_number = admin.split_destination_for_form("13000000000")
        assert_true(country_code == "+86", "local destinations keep the default country code")
        assert_true(phone_number == "13000000000", "local destinations keep the original phone number")
    finally:
        cleanup(base)


def test_destination_parts_from_form_keep_storage_fields_separate():
    admin, base = load_admin()
    try:
        country_code, phone_number, destination = admin.destination_parts_from_form(
            {"country_code": "+44", "destination": "07922675277"}
        )
        assert_true(country_code == "+44", "form country code is normalized for storage")
        assert_true(phone_number == "07922675277", "form phone number is stored separately")
        assert_true(destination == "+4407922675277", "destination is still combined for sending")
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
                "sim_state": "已识别（IMSI 可用）",
                "sim_identity": "46011******4699",
                "sms_counts": "已发 0 / 已收 3 / 失败 0",
                "signal": "80",
                "network_level": "80%",
                "network_switch": "数据关闭",
                "network_switch_tone": "ok",
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
        assert_true('id="sim-status-panel"' in body, "SIM status panel has live update root")
        sim_row = body[body.index("<tr><th>SIM 卡</th>") : body.index("</tr>", body.index("<tr><th>SIM 卡</th>"))]
        sim_header = body[body.index('class="sim-status-head"') : body.index("</div>", body.index('class="sim-status-head"'))]
        assert_true("data-sim-badge" in sim_row, "SIM status badge is inside the SIM card row")
        assert_true("data-sim-state-text" not in body, "SIM card row shows only the status badge without duplicate text")
        assert_true(sim_row.count("已识别（IMSI 可用）") == 1, "SIM card status label appears only once")
        assert_true("data-sim-badge" not in sim_header, "SIM status header no longer contains the status badge")
        assert_true("<th>SIM 标识</th>" in body, "dashboard shows masked SIM identity")
        assert_true('data-sim-field="sim_identity"' in body, "SIM identity cell exposes live update field")
        assert_true("46011******4699" in body, "SIM identity is masked")
        assert_true("460115033554699" not in body, "full IMSI is not exposed in dashboard HTML")
        assert_true("<th>短信计数</th>" in body, "dashboard shows SMS counters")
        assert_true('data-sim-field="sms_counts"' in body, "SMS counters cell exposes live update field")
        assert_true("已发 0 / 已收 3 / 失败 0" in body, "SMS counters are rendered")
        assert_true('data-sim-field="signal"' in body, "SIM status cells expose live update fields")
        assert_true("<th>网络强度</th>" not in body, "dashboard no longer shows network strength row")
        assert_true('data-sim-field="network_level"' not in body, "network level cell is removed")
        assert_true('class="status-pill ok"' in body, "network status is shown as an ok capsule")
        assert_true("<th>网络状态</th>" not in body, "network status is not shown as a separate row")
        assert_true('class="sim-status-head"' in body, "SIM status panel has a header action row")
        assert_true('data-sim-refresh' in body, "SIM status panel has a refresh button")
        assert_true('new EventSource("/api/sim-status/stream")' in body, "dashboard subscribes to SIM status SSE")
        assert_true('refresh.addEventListener("click", startStream)' in body, "refresh button starts a new SSE stream")
        assert_true("if (source) source.close();" in body, "refresh closes the existing SSE stream before restarting")
        assert_true("renderSimState(status);" in body, "SSE updates SIM row badge")
        assert_true('setField("sim_identity", status.sim_identity);' in body, "SSE updates masked SIM identity")
        assert_true('setField("sms_counts", status.sms_counts);' in body, "SSE updates SMS counters")
        assert_true("renderNetworkLevel(status);" not in body, "SSE no longer updates removed network level row")
    finally:
        cleanup(base)


def test_dashboard_sim_status_places_phone_signal_unit_and_data_switch():
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
                "sim_state": "已识别（IMSI 可用）",
                "sim_identity": "46011******4699",
                "phone_number": "+8613800138000",
                "sms_counts": "已发 0 / 已收 3 / 失败 0",
                "signal": "80",
                "network_level": "80%",
                "network_switch": "数据关闭",
                "network_switch_tone": "ok",
                "operator": "中国电信",
                "checked_at": "2026-06-10 22:00:00 +0800",
                "error": "",
            }
            body = admin.AdminHandler.render_dashboard(Dummy()).decode("utf-8")
        finally:
            admin.subprocess.run = old_run
            admin.get_sim_status = old_get_sim_status

        sim_identity_pos = body.index("<th>SIM 标识</th>")
        phone_pos = body.index("<th>手机号</th>")
        assert_true(sim_identity_pos < phone_pos, "phone number row is directly below SIM identity context")
        assert_true('data-sim-field="phone_number"' in body, "phone number cell exposes live update field")
        assert_true("+8613800138000" in body, "phone number is rendered")
        signal_row = body[body.index("<tr><th>信号强度</th>") : body.index("</tr>", body.index("<tr><th>信号强度</th>"))]
        assert_true(">80%<" in signal_row, "numeric signal strength is displayed with a percent unit")
        operator_row = body[body.index("<tr><th>运营商</th>") : body.index("</tr>", body.index("<tr><th>运营商</th>"))]
        assert_true("<th>网络强度</th>" not in body, "network strength row is removed")
        assert_true('data-sim-field="network_level"' not in body, "removed network strength row has no live update field")
        assert_true("中国电信" in operator_row and "数据关闭" in operator_row, "data switch is appended to operator row")
        assert_true(operator_row.index("中国电信") < operator_row.index("数据关闭"), "operator text appears before data switch")
        assert_true("2026/06/10 22:00:00" in body, "dashboard checked time uses JS-style display format")
        assert_true('setField("phone_number", status.phone_number);' in body, "SSE updates phone number")
        assert_true("renderOperator(status);" in body, "SSE updates operator row with data switch")
    finally:
        cleanup(base)


def test_dashboard_sim_status_renders_dbm_signal_quality_pill():
    admin, base = load_admin()
    try:
        admin.init_db()

        class Dummy:
            table_inbound = admin.AdminHandler.table_inbound
            table_outbound = admin.AdminHandler.table_outbound

        old_get_sim_status = admin.get_sim_status
        try:
            admin.get_sim_status = lambda: {
                "sim_state": "已识别（IMSI 可用）",
                "sim_identity": "23410******7404",
                "phone_number": "+44 - 7922675277",
                "sms_counts": "已发 0 / 已收 3 / 失败 0",
                "signal": "-60 dBm",
                "signal_dbm": -60,
                "network_switch": "数据关闭",
                "network_switch_tone": "ok",
                "operator": "O2 UK",
                "checked_at": "2026-06-12 18:40:00 +0800",
                "error": "",
            }
            body = admin.AdminHandler.render_dashboard(Dummy()).decode("utf-8")
        finally:
            admin.get_sim_status = old_get_sim_status

        signal_row = body[body.index("<tr><th>信号强度</th>") : body.index("</tr>", body.index("<tr><th>信号强度</th>"))]
        assert_true("-60 dBm" in signal_row, "dBm signal is shown as the main signal value")
        assert_true('class="status-pill ok"' in signal_row, "strong dBm signal uses a green quality capsule")
        assert_true(">极强/强<" in signal_row, "strong dBm signal labels the quality")
        assert_true("renderSignal(status);" in body, "SSE updates signal with the rich renderer")
        assert_true('setField("signal", formatSignal(status.signal));' not in body, "SSE no longer overwrites signal pill as text")
        assert_true('"ok"' in body and "极强/强" in body, "SSE script knows the green signal quality")
    finally:
        cleanup(base)


def test_signal_quality_uses_mobile_dbm_strength_table():
    admin, base = load_admin()
    try:
        assert_true(admin.signal_quality_for_dbm(-50) == ("极强/强", "ok"), "-50 dBm is strong")
        assert_true(admin.signal_quality_for_dbm(-79) == ("极强/强", "ok"), "-79 dBm is still strong")
        assert_true(admin.signal_quality_for_dbm(-80) == ("良好", "good"), "-80 dBm is good")
        assert_true(admin.signal_quality_for_dbm(-89) == ("良好", "good"), "-89 dBm is still good")
        assert_true(admin.signal_quality_for_dbm(-90) == ("一般", "warn"), "-90 dBm is normal")
        assert_true(admin.signal_quality_for_dbm(-99) == ("一般", "warn"), "-99 dBm is still normal")
        assert_true(admin.signal_quality_for_dbm(-100) == ("较弱", "bad"), "-100 dBm is weak")
        assert_true(admin.signal_quality_for_dbm(None) == ("", ""), "missing dBm has no quality")
    finally:
        cleanup(base)


def test_dashboard_sim_error_uses_bad_pill_without_error_detail():
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
                "sim_state": "未识别（无法访问 SIM 卡）",
                "sim_state_tone": "muted",
                "sim_identity": "",
                "sms_counts": "",
                "signal": "",
                "network_level": "",
                "network_switch": "未知",
                "network_switch_tone": "muted",
                "operator": "",
                "checked_at": admin.now_text(),
                "error": "pve agent timeout",
            }
            body = admin.AdminHandler.render_dashboard(Dummy()).decode("utf-8")
        finally:
            admin.subprocess.run = old_run
            admin.get_sim_status = old_get_sim_status

        sim_row = body[body.index("<tr><th>SIM 卡</th>") : body.index("</tr>", body.index("<tr><th>SIM 卡</th>"))]
        assert_true('class="status-pill bad"' in sim_row, "SIM error is shown as a bad capsule")
        assert_true(">未识别（无法访问 SIM 卡）<" in sim_row, "SIM error capsule keeps the sim_state label")
        assert_true(">不可用<" not in sim_row, "SIM error capsule does not replace sim_state with a generic label")
        assert_true(">sim_state<" not in sim_row, "SIM error capsule does not show the field name")
        assert_true("pve agent timeout" not in body, "dashboard omits the bottom SIM error detail")
        assert_true("data-sim-error" not in body, "dashboard does not render or update a SIM error detail row")
        assert_true("状态读取失败" not in body, "SSE script does not inject the old bottom error detail")
    finally:
        cleanup(base)


def test_dashboard_uses_pve_service_label_in_pve_dispatch_mode():
    admin, base = load_admin()
    old_mode = os.environ.get("SMS_DISPATCH_MODE")
    try:
        os.environ["SMS_DISPATCH_MODE"] = "pve"
        admin.init_db()

        class Dummy:
            table_inbound = admin.AdminHandler.table_inbound
            table_outbound = admin.AdminHandler.table_outbound

        old_get_sim_status = admin.get_sim_status
        try:
            admin.get_sim_status = lambda: {
                "sim_state": "已识别（IMSI 可用）",
                "sim_identity": "46011******4699",
                "sms_counts": "已发 0 / 已收 3 / 失败 0",
                "signal": "80",
                "network_level": "80%",
                "network_switch": "数据关闭",
                "network_switch_tone": "ok",
                "operator": "Test",
                "checked_at": admin.now_text(),
                "error": "",
            }
            body = admin.AdminHandler.render_dashboard(Dummy()).decode("utf-8")
        finally:
            admin.get_sim_status = old_get_sim_status

        assert_true("PVE Radio Agent" in body, "PVE mode dashboard labels the PVE service")
        assert_true("gammu-smsd" not in body, "PVE mode dashboard does not show local gammu-smsd")
    finally:
        if old_mode is None:
            os.environ.pop("SMS_DISPATCH_MODE", None)
        else:
            os.environ["SMS_DISPATCH_MODE"] = old_mode
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


def test_strategy_page_uses_table_and_modals_without_command_timeout():
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
        table = body[body.index("<table>") : body.index("</table>", body.index("<table>"))]
        create_dialog = body[body.index('id="strategy-create"') : body.index("</dialog>", body.index('id="strategy-create"'))]
        edit_dialog = body[body.index(f'id="strategy-edit-{strategy_id}"') : body.index("</dialog>", body.index(f'id="strategy-edit-{strategy_id}"'))]
        row = table[table.index("Delete me") : table.index("</tr>", table.index("Delete me"))]

        assert_true('<div class="toolbar"><h2>发送策略</h2>' in body, "strategy page uses the list toolbar")
        assert_true('onclick="document.getElementById(\'strategy-create\').showModal()"' in body, "strategy create opens a modal")
        assert_true("<th>ID</th><th>名称</th><th>发送间隔</th><th>失败重试</th>" in table, "strategy page renders a table list")
        assert_true("strategy-card" not in body, "strategy page no longer renders card forms")
        assert_true("提交命令超时" not in body, "strategy command timeout field is removed from the UI")
        assert_true('name="command_timeout_seconds"' not in body, "strategy command timeout input is removed from forms")
        assert_true("默认策略不能删除" not in body, "default strategy delete hint is not shown in the actions column")
        assert_true('action="/settings"' in create_dialog, "strategy create modal posts to settings")
        assert_true(f'action="/settings/{strategy_id}/delete"' in row, "strategy row contains delete action")
        assert_true(f"strategy-edit-{strategy_id}" in row, "strategy row opens an edit modal")
        assert_true(f'action="/settings"' in edit_dialog, "strategy edit modal posts updates")
        assert_true(f'value="{strategy_id}"' in edit_dialog, "strategy edit modal carries strategy id")
    finally:
        cleanup(base)


def test_strategy_send_interval_uses_value_and_unit_ui():
    admin, base = load_admin()
    try:
        admin.init_db()
        admin.save_strategy_from_form(
            {
                "name": "Every two hours",
                "send_interval_value": "2",
                "send_interval_unit": "hour",
                "retry_interval_minutes": "10",
                "max_retries": "1",
                "command_timeout_seconds": "45",
                "active": "yes",
            }
        )
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM send_strategies WHERE name = 'Every two hours'").fetchone()
        assert_true(row["send_interval_minutes"] == 120, "strategy stores computed interval minutes")

        body = admin.AdminHandler.render_settings(object()).decode("utf-8")
        start = body.index(f'id="strategy-edit-{row["id"]}"')
        section = body[start : body.index("</dialog>", start)]
        options = admin.strategy_options_html(row["id"])

        assert_true('class="strategy-interval-field"' in section, "strategy send interval occupies its own row")
        assert_true(
            section.index('class="strategy-interval-field"') < section.index('<div class="row">'),
            "strategy send interval appears above retry/max fields",
        )
        assert_true('name="send_interval_value"' in section, "strategy form uses a numeric interval input")
        assert_true('value="2"' in section, "strategy interval input displays the clearer value")
        assert_true('name="send_interval_unit"' in section, "strategy form has an interval unit selector")
        assert_true('<option value="hour" selected>小时</option>' in section, "strategy interval unit displays hours")
        assert_true("间隔为 0" in section and "不开启下一次发送" in section, "strategy form explains zero interval behavior")
        assert_true("每 2 小时发送间隔" in options, "strategy dropdown displays a clear interval label")
    finally:
        cleanup(base)


def test_strategy_update_preserves_hidden_command_timeout():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Keep timeout', 3, 7, 1, 88, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)

        admin.save_strategy_from_form(
            {
                "strategy_id": str(strategy_id),
                "name": "Keep timeout edited",
                "send_interval_value": "2",
                "send_interval_unit": "hour",
                "retry_interval_minutes": "9",
                "max_retries": "2",
                "active": "yes",
            }
        )

        with admin.db() as conn:
            row = conn.execute("SELECT * FROM send_strategies WHERE id = ?", (strategy_id,)).fetchone()
        assert_true(row["command_timeout_seconds"] == 88, "hidden command timeout is preserved on strategy edit")
    finally:
        cleanup(base)


def test_delete_and_archive_list_actions_require_confirmation():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        delete_plan_id = admin.create_send_plan("Delete confirm", "13000000000", "delete body", admin.now_text(), strategy["id"])
        archive_plan_id = admin.create_send_plan("Archive confirm", "13000000001", "archive body", admin.now_text(), strategy["id"])
        with admin.db() as conn:
            outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (archive_plan_id,)).fetchone()["outbound_id"]
            conn.execute(
                "UPDATE outbound_sms SET status = 'submitted', attempts = 1, submitted_at = ?, updated_at = ? WHERE id = ?",
                (admin.now_text(), admin.now_text(), outbound_id),
            )
            cur = conn.execute(
                """
                INSERT INTO send_strategies
                  (name, send_interval_minutes, retry_interval_minutes, max_retries, command_timeout_seconds,
                   active, is_default, created_at, updated_at)
                VALUES ('Delete strategy confirm', 3, 7, 1, 45, 1, 0, ?, ?)
                """,
                (admin.now_text(), admin.now_text()),
            )
            strategy_id = int(cur.lastrowid)

        plans_body = admin.AdminHandler.render_plans(object()).decode("utf-8")
        settings_body = admin.AdminHandler.render_settings(object()).decode("utf-8")
        delete_plan_form = plans_body[
            plans_body.index(f'action="/plans/{delete_plan_id}/delete"') : plans_body.index("</form>", plans_body.index(f'action="/plans/{delete_plan_id}/delete"'))
        ]
        archive_plan_form = plans_body[
            plans_body.index(f'action="/plans/{archive_plan_id}/archive"') : plans_body.index("</form>", plans_body.index(f'action="/plans/{archive_plan_id}/archive"'))
        ]
        strategy_delete_form = settings_body[
            settings_body.index(f'action="/settings/{strategy_id}/delete"') : settings_body.index("</form>", settings_body.index(f'action="/settings/{strategy_id}/delete"'))
        ]

        assert_true('onsubmit="return confirm(' in delete_plan_form, "plan delete requires a second confirmation")
        assert_true("确认删除" in delete_plan_form, "plan delete confirmation names the destructive action")
        assert_true('onsubmit="return confirm(' in archive_plan_form, "plan archive requires a second confirmation")
        assert_true("确认归档" in archive_plan_form, "plan archive confirmation names the action")
        assert_true('onsubmit="return confirm(' in strategy_delete_form, "strategy delete requires a second confirmation")
        assert_true("确认删除" in strategy_delete_form, "strategy delete confirmation names the destructive action")
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
        assert_true(info["sim_state"] == "已识别（IMSI 可用）", "SIM IMSI marks SIM as identified")
        assert_true(info["signal"] == "-67 dBm", "signal strength is parsed")
        assert_true(info["network_level"] == "78 percent", "network level is parsed")
        assert_true(info["network_state"] == "home network", "network state is parsed")
        assert_true(info["network_switch"] == "数据关闭", "network registration still reports data switch as closed")
        assert_true(info["network_switch_tone"] == "ok", "closed data switch uses safe capsule")
        assert_true(info["operator"] == "China Mobile", "operator is parsed")
    finally:
        cleanup(base)


def test_parse_smsd_monitor_output():
    admin, base = load_admin()
    try:
        output = """
Client: Gammu 1.42.0 on Linux
PhoneID: quectel-em120r
IMEI: 015930000049750
IMSI: 460115033554699
Sent: 0
Received: 1
Failed: 0
BatterPercent: 0
NetworkSignal: 100
"""
        info = admin.parse_smsd_monitor_output(output)
        assert_true(info["sim_state"] == "已识别（IMSI 可用）", "monitor IMSI marks SIM as identified")
        assert_true(info["signal"] == "-51 dBm", "monitor signal is converted to dBm")
        assert_true(info["signal_dbm"] == -51, "monitor signal exposes numeric dBm")
        assert_true(info["network_level"] == "100%", "monitor network level follows signal")
        assert_true(info["network_state"] == "smsd 运行中", "monitor status reports smsd source")
        assert_true(info["network_switch"] == "数据关闭", "monitor signal does not imply data switch is on")
        assert_true(info["network_switch_tone"] == "ok", "closed data switch uses safe capsule")
        assert_true(info["operator"] == "中国电信", "monitor IMSI infers China Telecom")
        assert_true(info["sim_identity"] == "46011******4699", "monitor IMSI is masked for display")
        assert_true(info["sms_counts"] == "已发 0 / 已收 1 / 失败 0", "monitor SMS counters are formatted")
    finally:
        cleanup(base)


def test_parse_smsd_monitor_output_shows_missing_sim_and_network_off():
    admin, base = load_admin()
    try:
        output = """
Client: Gammu 1.42.0 on Linux
PhoneID: quectel-em120r
Sent: 0
Received: 1
Failed: 0
BatterPercent: 0
NetworkSignal: 0
"""
        info = admin.parse_smsd_monitor_output(output)
        assert_true(info["sim_state"] == "未识别（无 IMSI）", "missing monitor IMSI is shown as a specific SIM state")
        assert_true(info["sim_identity"] == "", "missing monitor IMSI has no display identity")
        assert_true(info["sms_counts"] == "已发 0 / 已收 1 / 失败 0", "monitor SMS counters still render without IMSI")
        assert_true(info["network_switch"] == "数据关闭", "missing signal still reports data switch as closed")
        assert_true(info["network_switch_tone"] == "ok", "closed data switch remains safe")
    finally:
        cleanup(base)


def test_parse_smsd_monitor_output_hides_unknown_negative_signal():
    admin, base = load_admin()
    try:
        output = """
Client: Gammu 1.42.0 on Linux
PhoneID: quectel-em120r
IMEI: 015930000049750
IMSI: 460115033554699
Sent: 0
Received: 0
Failed: 0
BatterPercent: 0
NetworkSignal: -1
"""
        info = admin.parse_smsd_monitor_output(output)
        assert_true(info["sim_state"] == "已识别（IMSI 可用）", "monitor IMSI still identifies SIM")
        assert_true(info["signal"] == "", "unknown monitor signal is hidden")
        assert_true(info["network_level"] == "", "unknown monitor signal does not render a percentage")
        assert_true(info["operator"] == "中国电信", "monitor IMSI still infers operator")
    finally:
        cleanup(base)


def test_parse_smsd_health_output_reports_recent_no_sim():
    admin, base = load_admin()
    try:
        output = """
Jun 10 20:35:41 sms gammu-smsd[549]: Error getting security status: 无法访问 SIM 卡。 (NOSIM[49])
Jun 10 20:36:14 sms gammu-smsd[549]: Error at init connection: 未知错误。 (UNKNOWN[27])
"""
        status = admin.parse_smsd_health_output(output)
        assert_true(status is not None, "recent smsd no-SIM log produces a status override")
        assert_true(status["sim_state"] == "未识别（无法访问 SIM 卡）", "NOSIM is shown as no SIM")
        assert_true(status["sim_state_tone"] == "bad", "NOSIM uses a bad capsule")
        assert_true(status["sim_identity"] == "", "stale IMSI is cleared for no SIM")
        assert_true(status["signal"] == "", "stale signal is cleared for no SIM")
        assert_true(status["operator"] == "", "stale operator is cleared for no SIM")
        assert_true(status["network_switch"] == "数据关闭", "data switch stays closed")
    finally:
        cleanup(base)


def test_get_sim_status_prefers_smsd_health_issue_over_stale_monitor_memory():
    admin, base = load_admin()
    try:
        stale_monitor = {
            "sim_state": "已识别（IMSI 可用）",
            "sim_state_tone": "ok",
            "sim_identity": "46011******4699",
            "sms_counts": "已发 0 / 已收 3 / 失败 0",
            "signal": "81",
            "network_level": "81%",
            "network_state": "smsd 运行中",
            "network_switch": "数据关闭",
            "network_switch_tone": "ok",
            "operator": "中国电信",
            "battery": "0%",
            "error": "",
            "source_updated_at": "",
            "checked_at": admin.now_text(),
        }
        no_sim = admin.parse_smsd_health_output("无法访问 SIM 卡。 (NOSIM[49])")

        old_monitor = admin.read_smsd_monitor_status
        old_health = admin.read_smsd_health_status
        try:
            admin.read_smsd_monitor_status = lambda: stale_monitor
            admin.read_smsd_health_status = lambda: no_sim
            status = admin.get_sim_status()
        finally:
            admin.read_smsd_monitor_status = old_monitor
            admin.read_smsd_health_status = old_health

        assert_true(status["sim_state"] == "未识别（无法访问 SIM 卡）", "recent smsd issue overrides stale monitor SIM state")
        assert_true(status["sim_identity"] == "", "recent smsd issue clears stale monitor IMSI")
        assert_true(status["signal"] == "", "recent smsd issue clears stale monitor signal")
        assert_true(status["sms_counts"] == "已发 0 / 已收 3 / 失败 0", "SMS counters can remain visible with the no-SIM override")
    finally:
        cleanup(base)


def test_get_sim_status_can_bypass_cache_and_use_smsd_monitor():
    admin, base = load_admin()
    try:
        admin.init_db()
        calls = []

        class RunResult:
            returncode = 0
            stdout = "IMSI: 460115033554699\nNetworkSignal: 100\nBatterPercent: 0\n"
            stderr = ""

        old_run = admin.subprocess.run
        try:
            admin.subprocess.run = lambda cmd, **kwargs: calls.append(cmd) or RunResult()
            status = admin.get_sim_status(use_cache=False)
        finally:
            admin.subprocess.run = old_run

        assert_true(calls and "gammu-smsd-monitor" in calls[0][0], "SIM status uses smsd monitor first")
        assert_true(status["sim_state"] == "已识别（IMSI 可用）", "uncached monitor status identifies SIM")
        assert_true(status["sim_identity"] == "46011******4699", "uncached monitor status masks IMSI")
        assert_true(status["signal"] == "-51 dBm", "uncached monitor status converts signal to dBm")
        assert_true(status["signal_dbm"] == -51, "uncached monitor status exposes numeric dBm")
        assert_true(status["network_level"] == "100%", "uncached monitor status keeps percentage level")
        assert_true(status["network_switch"] == "数据关闭", "uncached monitor status keeps data switch closed")
    finally:
        cleanup(base)


def test_get_sim_status_ignores_smsd_database_for_realtime_status():
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
                        '2026-06-10 11:12:00', 'yes', 'yes', 'imei', 'db-imsi',
                        '99999', 'DB Operator', 'Gammu', 90, 1, 0, 0)
                """
            )

        class RunResult:
            returncode = 0
            stdout = """
Phone information
Signal strength      : -67 dBm
Network level        : 88 percent
SIM IMSI             : 460115033554699
"""
            stderr = ""

        old_monitor = admin.read_smsd_monitor_status
        old_run = admin.subprocess.run
        try:
            admin.read_smsd_monitor_status = lambda: None
            admin.subprocess.run = lambda *args, **kwargs: RunResult()
            status = admin.get_sim_status()
        finally:
            admin.read_smsd_monitor_status = old_monitor
            admin.subprocess.run = old_run

        assert_true(status["signal"] == "-67 dBm", "real-time status ignores stale smsd database signal")
        assert_true(status["network_level"] == "88 percent", "real-time status uses serial monitor output")
        assert_true(status["operator"] == "中国电信", "real-time status infers operator from serial IMSI")
    finally:
        cleanup(base)


def test_get_sim_status_uses_pve_agent_when_configured():
    old_url = os.environ.get("PVE_RADIO_AGENT_URL")
    os.environ["PVE_RADIO_AGENT_URL"] = "http://pve-agent.local:8091"
    admin, base = load_admin()
    try:
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "sim_state": "已识别（IMSI 可用）",
                        "sim_identity": "46011******4699",
                        "operator": "中国电信",
                        "signal": "-63 dBm",
                        "network_switch": "数据关闭",
                        "checked_at": "2026-06-10 22:00:00 +0800",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        old_urlopen = admin.urllib.request.urlopen
        try:
            admin.urllib.request.urlopen = lambda request, timeout=0: calls.append((request.full_url, timeout)) or Response()
            status = admin.get_sim_status(use_cache=False)
        finally:
            admin.urllib.request.urlopen = old_urlopen

        assert_true(
            calls == [("http://pve-agent.local:8091/sim/status", admin.PVE_AGENT_TIMEOUT_SECONDS)],
            "SIM status calls PVE agent endpoint with the configured timeout",
        )
        assert_true(status["operator"] == "中国电信", "PVE agent operator is returned")
        assert_true(status["network_switch"] == "数据关闭", "PVE agent keeps data switch closed")
    finally:
        if old_url is None:
            os.environ.pop("PVE_RADIO_AGENT_URL", None)
        else:
            os.environ["PVE_RADIO_AGENT_URL"] = old_url
        cleanup(base)


def test_pve_agent_timeout_keeps_cached_static_identity_only():
    old_url = os.environ.get("PVE_RADIO_AGENT_URL")
    os.environ["PVE_RADIO_AGENT_URL"] = "http://pve-agent.local:8091"
    admin, base = load_admin()
    try:
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(
                    {
                        "sim_state": "已识别（IMSI 可用）",
                        "sim_identity": "23410******7404",
                        "phone_number": "07922675277",
                        "country_code": "44",
                        "phone_number_e164": "+44 - 7922675277",
                        "operator": "O2 UK",
                        "sms_counts": "已发 1 / 已收 2 / 失败 0",
                        "signal": "-51 dBm",
                        "signal_dbm": -51,
                        "network_switch": "数据关闭",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            calls.append((request.full_url, timeout))
            if len(calls) == 1:
                return Response()
            raise TimeoutError("timed out")

        old_urlopen = admin.urllib.request.urlopen
        old_cache = getattr(admin, "PVE_STATIC_STATUS_CACHE", {}).copy()
        try:
            if hasattr(admin, "PVE_STATIC_STATUS_CACHE"):
                admin.PVE_STATIC_STATUS_CACHE.clear()
            admin.urllib.request.urlopen = fake_urlopen
            first = admin.get_sim_status(use_cache=False)
            second = admin.get_sim_status(use_cache=False)
        finally:
            admin.urllib.request.urlopen = old_urlopen
            if hasattr(admin, "PVE_STATIC_STATUS_CACHE"):
                admin.PVE_STATIC_STATUS_CACHE.clear()
                admin.PVE_STATIC_STATUS_CACHE.update(old_cache)

        assert_true(first["operator"] == "O2 UK", "first successful PVE status returns operator")
        assert_true(second["error"] == "timed out", "timeout is still visible as a dynamic read failure")
        assert_true(second["sim_identity"] == "23410******7404", "timeout keeps cached SIM identity")
        assert_true(second["phone_number"] == "07922675277", "timeout keeps cached raw phone number")
        assert_true(second["phone_number_e164"] == "+44 - 7922675277", "timeout keeps cached formatted phone number")
        assert_true(second["operator"] == "O2 UK", "timeout keeps cached operator")
        assert_true(second["sms_counts"] == "", "timeout does not reuse dynamic SMS counters")
        assert_true(second["signal"] == "", "timeout does not reuse dynamic signal")
    finally:
        if old_url is None:
            os.environ.pop("PVE_RADIO_AGENT_URL", None)
        else:
            os.environ["PVE_RADIO_AGENT_URL"] = old_url
        cleanup(base)


def test_sse_event_formats_json_payload():
    admin, base = load_admin()
    try:
        event = admin.format_sse_event("sim-status", {"sim_state": "已识别", "signal": "100%"})
        assert_true(event.startswith("event: sim-status\n"), "SSE event starts with event name")
        data_line = [line for line in event.splitlines() if line.startswith("data: ")][0]
        payload = json.loads(data_line.removeprefix("data: "))
        assert_true(payload["sim_state"] == "已识别", "SSE payload is JSON")
        assert_true(event.endswith("\n\n"), "SSE event is terminated by a blank line")
    finally:
        cleanup(base)


def test_sim_status_stream_refreshes_every_second():
    admin, base = load_admin()
    try:
        assert_true(admin.SIM_STATUS_STREAM_INTERVAL_SECONDS == 1, "SIM status stream default interval is one second")
        sleeps = []
        events = []

        class FakeWFile:
            def write(self, payload):
                events.append(payload.decode("utf-8"))

            def flush(self):
                pass

        class Dummy:
            wfile = FakeWFile()

            def send_response(self, status):
                pass

            def send_header(self, key, value):
                pass

            def end_headers(self):
                pass

        old_seconds = admin.SIM_STATUS_STREAM_SECONDS
        old_interval = admin.SIM_STATUS_STREAM_INTERVAL_SECONDS
        old_sleep = admin.time.sleep
        old_get_status = admin.get_sim_status
        try:
            admin.SIM_STATUS_STREAM_SECONDS = 3
            admin.SIM_STATUS_STREAM_INTERVAL_SECONDS = 1
            admin.time.sleep = lambda seconds: sleeps.append(seconds)
            admin.get_sim_status = lambda use_cache=False: {"sim_state": "已识别", "signal": "-51 dBm"}
            admin.AdminHandler.stream_sim_status(Dummy())
        finally:
            admin.SIM_STATUS_STREAM_SECONDS = old_seconds
            admin.SIM_STATUS_STREAM_INTERVAL_SECONDS = old_interval
            admin.time.sleep = old_sleep
            admin.get_sim_status = old_get_status

        assert_true(sleeps == [1, 1], "SIM status stream sleeps one second between events")
        assert_true(sum("event: sim-status" in event for event in events) == 3, "stream emits the configured number of status events")
        assert_true(any("event: done" in event for event in events), "stream emits done after status events")
    finally:
        cleanup(base)


def test_sim_status_stream_payload_prefers_e164_phone_number():
    admin, base = load_admin()
    try:
        status = {
            "sim_state": "已识别（IMSI 可用）",
            "phone_number": "07922675277",
            "country_code": "44",
            "phone_number_e164": "+447922675277",
        }
        payload = admin.sim_status_stream_payload(status)
        assert_true(payload["phone_number"] == "+447922675277", "SIM status stream sends E.164 phone number")
        assert_true(payload["phone_number_e164"] == "+447922675277", "SIM status stream keeps explicit E.164 field")
        assert_true(status["phone_number"] == "07922675277", "SIM status stream payload does not mutate source status")
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


def test_status_badge_distinguishes_waiting_sent_and_timeout():
    admin, base = load_admin()
    try:
        assert_true("等待 PVE 发送" in admin.status_badge("dispatching"), "dispatching means waiting for PVE execution")
        assert_true("已提交" in admin.status_badge("submitted"), "submitted means the SMS can no longer be cancelled")
        assert_true("已发送" in admin.status_badge("sent"), "sent has a distinct final status")
        assert_true("发送超时" in admin.status_badge("send_timeout"), "send timeout has a distinct final status")
    finally:
        cleanup(base)


def test_mail_settings_are_saved_in_database_and_password_is_not_echoed():
    admin, base = load_admin()
    try:
        admin.init_db()
        admin.save_mail_settings(
            {
                "mail_enabled": "yes",
                "mail_to": "ops@example.com",
                "mail_from": "sms@example.com",
                "smtp_host": "smtp.example.com",
                "smtp_port": "465",
                "smtp_user": "sms@example.com",
                "smtp_password": "secret-pass",
                "smtp_security": "ssl",
            }
        )
        settings = admin.mail_config()
        assert_true(settings["MAIL_ENABLED"] == "1", "mail config is enabled from database")
        assert_true(settings["MAIL_TO"] == "ops@example.com", "recipient is stored in database")
        assert_true(settings["MAIL_FROM"] == "sms@example.com", "sender is stored in database")
        assert_true(settings["SMTP_HOST"] == "smtp.example.com", "SMTP host is stored in database")
        assert_true(settings["SMTP_PASSWORD"] == "secret-pass", "SMTP password is stored in database")

        admin.save_mail_settings(
            {
                "mail_enabled": "yes",
                "mail_to": "ops2@example.com",
                "mail_from": "sms@example.com",
                "smtp_host": "smtp.example.com",
                "smtp_port": "465",
                "smtp_user": "sms@example.com",
                "smtp_password": "",
                "smtp_security": "ssl",
            }
        )
        settings = admin.mail_config()
        assert_true(settings["MAIL_TO"] == "ops2@example.com", "mail settings update non-secret fields")
        assert_true(settings["SMTP_PASSWORD"] == "secret-pass", "blank password keeps existing database secret")

        settings_body = admin.AdminHandler.render_settings(object()).decode("utf-8")
        mail_body = admin.AdminHandler.render_mail_settings(object()).decode("utf-8")
        assert_true('href="/mail">邮件配置</a>' in mail_body, "mail configuration has a left nav entry")
        assert_true("<h2>邮件配置</h2>" not in settings_body, "strategy page no longer embeds mail configuration")
        assert_true('name="smtp_host"' not in settings_body, "strategy page does not render SMTP fields")
        assert_true("<h2>邮件配置</h2>" in mail_body, "mail page shows mail configuration")
        assert_true("ops2@example.com" in mail_body, "mail page displays non-secret mail fields")
        assert_true("secret-pass" not in mail_body, "mail page does not echo SMTP password")
        actions = mail_body[mail_body.index('<div class="actions form-actions-right">') : mail_body.index("</div>", mail_body.index('<div class="actions form-actions-right">'))]
        assert_true('formaction="/mail/test"' in actions, "mail page has a test mail button")
        assert_true(actions.index("发送测试邮件") < actions.index("保存邮件配置"), "test button is left of save button")
        assert_true(".form-actions-right { justify-content:flex-end;" in mail_body, "mail page actions are aligned right")
    finally:
        cleanup(base)


def test_mail_test_saves_form_and_sends_detection_mail():
    admin, base = load_admin()
    try:
        admin.init_db()
        calls = []
        old_send = admin.send_notification
        try:
            admin.send_notification = lambda subject, plain, html_body="": calls.append((subject, plain, html_body)) or (1, "")

            class Dummy:
                def __init__(self):
                    self.responses = []

                def read_form(self):
                    return {
                        "mail_enabled": "yes",
                        "mail_to": "ops-test@example.com",
                        "mail_from": "sms@example.com",
                        "smtp_host": "smtp.example.com",
                        "smtp_port": "465",
                        "smtp_user": "sms@example.com",
                        "smtp_password": "secret-pass",
                        "smtp_security": "ssl",
                    }

                def send_html(self, body, status=200, headers=None):
                    self.responses.append((body.decode("utf-8"), status))

                render_mail_settings = admin.AdminHandler.render_mail_settings

            dummy = Dummy()
            admin.AdminHandler.handle_mail_test(dummy)
        finally:
            admin.send_notification = old_send

        settings = admin.mail_config()
        assert_true(settings["MAIL_TO"] == "ops-test@example.com", "test action saves submitted mail settings first")
        assert_true(settings["SMTP_PASSWORD"] == "secret-pass", "test action saves submitted SMTP password")
        assert_true(calls, "test action sends a detection mail")
        assert_true("邮件配置测试" in calls[0][0], "test mail subject is clear")
        assert_true("检测邮件" in calls[0][1], "test mail body explains it is a detection mail")
        assert_true(dummy.responses and dummy.responses[0][1] == 200, "successful test renders the mail page")
        assert_true("测试邮件已发送" in dummy.responses[0][0], "successful test shows a clear notice")
    finally:
        cleanup(base)


def test_send_notification_uses_database_smtp_settings():
    admin, base = load_admin()
    try:
        admin.init_db()
        admin.save_mail_settings(
            {
                "mail_enabled": "yes",
                "mail_to": "ops@example.com",
                "mail_from": "sms@example.com",
                "smtp_host": "smtp.example.com",
                "smtp_port": "465",
                "smtp_user": "sms@example.com",
                "smtp_password": "secret-pass",
                "smtp_security": "ssl",
            }
        )
        calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=0, context=None):
                calls.append(("connect", host, port, timeout, bool(context)))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def login(self, user, password):
                calls.append(("login", user, password))

            def send_message(self, msg):
                calls.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content()))

        old_ssl = admin.smtplib.SMTP_SSL
        try:
            admin.smtplib.SMTP_SSL = FakeSMTP
            ok, err = admin.send_notification("Test subject", "plain body")
        finally:
            admin.smtplib.SMTP_SSL = old_ssl

        assert_true(ok == 1 and err == "", "database SMTP config sends mail")
        assert_true(calls[0] == ("connect", "smtp.example.com", 465, 30, True), "SMTP SSL connection uses database host and port")
        assert_true(("login", "sms@example.com", "secret-pass") in calls, "SMTP login uses database credentials")
        send_call = [call for call in calls if call[0] == "send"][0]
        assert_true(send_call[1] == "sms@example.com", "message sender comes from database")
        assert_true(send_call[2] == "ops@example.com", "message recipient comes from database")
        assert_true("plain body" in send_call[4], "message body is sent")
    finally:
        cleanup(base)


def test_reconcile_submitted_outbox_timeout_removes_file_and_marks_timeout():
    admin, base = load_admin()
    try:
        admin.init_db()
        outbox = admin.BASE / "spool" / "outbox"
        outbox.mkdir(parents=True)
        outbox_file = outbox / "OUTC20260610_203858_00_+8613003630212_sms0.smsbackup"
        outbox_file.write_text("queued sms", encoding="utf-8")
        item_id = admin.create_immediate_outbound("+8613003630212", "stale queued", admin.now_text())
        submitted_at = (dt.datetime.now().astimezone() - dt.timedelta(minutes=admin.SUBMITTED_SEND_TIMEOUT_MINUTES + 1)).strftime(TIME_FORMAT)
        with admin.db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'submitted', attempts = 1, submitted_at = ?, updated_at = ?,
                    command_output = ?
                WHERE id = ?
                """,
                (submitted_at, submitted_at, f"Written message with ID {outbox_file}", item_id),
            )
        admin.reconcile_submitted_outbounds()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(not outbox_file.exists(), "timed-out outbox file is removed to avoid later batch sending")
        assert_true(row["status"] == "send_timeout", "timed-out submitted SMS is marked as send timeout")
        assert_true("已从 outbox 移除" in row["last_error"], "timeout explains that the smsd queue file was removed")
    finally:
        cleanup(base)


def test_reconcile_submitted_sent_file_marks_sent():
    admin, base = load_admin()
    try:
        admin.init_db()
        outbox = admin.BASE / "spool" / "outbox"
        sent = admin.BASE / "spool" / "sent"
        outbox.mkdir(parents=True)
        sent.mkdir(parents=True)
        filename = "OUTC20260610_203858_00_+8613003630212_sms0.smsbackup"
        sent_file = sent / filename
        sent_file.write_text("sent sms", encoding="utf-8")
        item_id = admin.create_immediate_outbound("+8613003630212", "sent queued", admin.now_text())
        submitted_at = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).strftime(TIME_FORMAT)
        with admin.db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'submitted', attempts = 1, submitted_at = ?, updated_at = ?,
                    command_output = ?
                WHERE id = ?
                """,
                (submitted_at, submitted_at, f"Written message with ID {outbox / filename}", item_id),
            )
        admin.reconcile_submitted_outbounds()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(row["status"] == "sent", "submitted SMS with sent spool file is marked sent")
        assert_true(row["last_error"] == "", "sent row does not keep an error")
    finally:
        cleanup(base)


def test_planned_sms_sent_success_sends_mail_notification():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        plan_id = admin.create_send_plan("Success plan", "+8613003630212", "plan sent ok", admin.now_text(), strategy["id"])
        outbox = admin.BASE / "spool" / "outbox"
        sent = admin.BASE / "spool" / "sent"
        outbox.mkdir(parents=True)
        sent.mkdir(parents=True)
        filename = "OUTC20260610_203858_00_+8613003630212_sms0.smsbackup"
        sent_file = sent / filename
        sent_file.write_text("sent sms", encoding="utf-8")
        submitted_at = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).strftime(TIME_FORMAT)
        with admin.db() as conn:
            outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]
            conn.execute(
                """
                UPDATE outbound_sms
                SET status = 'submitted', attempts = 1, submitted_at = ?, updated_at = ?,
                    command_output = ?
                WHERE id = ?
                """,
                (submitted_at, submitted_at, f"Written message with ID {outbox / filename}", outbound_id),
            )
        calls = []
        old_send = admin.send_notification
        try:
            admin.send_notification = lambda subject, plain, html_body="": calls.append((subject, plain, html_body)) or (1, "")
            admin.reconcile_submitted_outbounds()
        finally:
            admin.send_notification = old_send
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (outbound_id,)).fetchone()
        assert_true(row["status"] == "sent", "planned submitted SMS is marked sent")
        assert_true(row["final_notified"] == 1, "successful planned SMS records mail notification")
        assert_true(calls, "successful planned SMS sends a mail notification")
        assert_true("短信发送成功" in calls[0][0], "success mail subject is clear")
        assert_true(f"计划 ID: #{plan_id}" in calls[0][1], "success mail includes plan id")
        assert_true("plan sent ok" in calls[0][1], "success mail includes SMS text")
    finally:
        cleanup(base)


def test_process_one_due_blocks_no_sim_before_injecting():
    admin, base = load_admin()
    try:
        admin.init_db()
        item_id = admin.create_immediate_outbound("+8613003630212", "do not queue", admin.now_text())
        called = []
        old_health = admin.read_smsd_health_status
        old_submit = admin.submit_sms
        try:
            admin.read_smsd_health_status = lambda: admin.sim_problem_status("未识别（无法访问 SIM 卡）", "smsd 无法访问 SIM 卡")
            admin.submit_sms = lambda row, timeout: called.append(row["id"]) or ("submitted", "should not happen")
            admin.process_one_due(item_id, ignore_interval=True)
        finally:
            admin.read_smsd_health_status = old_health
            admin.submit_sms = old_submit
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(called == [], "no-SIM preflight blocks gammu-smsd-inject")
        assert_true(row["status"] == "failed", "immediate no-SIM send fails without entering smsd outbox")
        assert_true("未写入 smsd 发送队列" in row["last_error"], "failure explains that no outbox file was created")
    finally:
        cleanup(base)


def test_process_one_due_pve_dispatch_creates_idempotent_job_without_local_gammu():
    old_mode = os.environ.get("SMS_DISPATCH_MODE")
    os.environ["SMS_DISPATCH_MODE"] = "pve"
    admin, base = load_admin()
    try:
        admin.init_db()
        item_id = admin.create_immediate_outbound("+8613003630212", "dispatch me", admin.now_text())
        called = []
        old_submit = admin.submit_sms
        try:
            admin.submit_sms = lambda row, timeout: called.append(row["id"]) or ("failed", "local gammu should not run")
            admin.process_one_due(item_id, ignore_interval=True)
        finally:
            admin.submit_sms = old_submit
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
            jobs = conn.execute("SELECT * FROM sms_dispatch_jobs WHERE outbound_id = ?", (item_id,)).fetchall()
        assert_true(called == [], "PVE dispatch mode does not call local gammu-smsd-inject")
        assert_true(row["status"] == "dispatching", "outbound moves to dispatching while PVE owns execution")
        assert_true(row["dispatch_job_id"], "outbound keeps the dispatch job id")
        assert_true(len(jobs) == 1, "one dispatch job is created")
        assert_true(jobs[0]["status"] == "queued", "dispatch job starts queued for PVE agent")
        assert_true(jobs[0]["idempotency_key"] == f"outbound:{item_id}:attempt:1", "dispatch job has stable idempotency key")
    finally:
        if old_mode is None:
            os.environ.pop("SMS_DISPATCH_MODE", None)
        else:
            os.environ["SMS_DISPATCH_MODE"] = old_mode
        cleanup(base)


def test_cancel_dispatching_sets_cancel_request_without_overwriting_status():
    admin, base = load_admin()
    try:
        admin.init_db()
        item_id = admin.create_immediate_outbound("+8613003630212", "cancel while dispatching", admin.now_text())
        with admin.db() as conn:
            conn.execute("UPDATE outbound_sms SET status = 'dispatching', updated_at = ? WHERE id = ?", (admin.now_text(), item_id))
        ok, message = admin.request_cancel_outbound(item_id)
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(ok, "dispatching cancel request is accepted")
        assert_true("尝试取消" in message, "dispatching cancel explains it is only requested")
        assert_true(row["status"] == "dispatching", "dispatching status is not overwritten")
        assert_true(row["cancel_requested"] == 1, "cancel request flag is set")
        assert_true(row["cancel_requested_at"], "cancel request timestamp is stored")
    finally:
        cleanup(base)


def test_cancel_submitted_and_sent_are_rejected():
    admin, base = load_admin()
    try:
        admin.init_db()
        submitted_id = admin.create_immediate_outbound("+8613003630212", "submitted", admin.now_text())
        sent_id = admin.create_immediate_outbound("+8613003630213", "sent", admin.now_text())
        with admin.db() as conn:
            conn.execute("UPDATE outbound_sms SET status = 'submitted', updated_at = ? WHERE id = ?", (admin.now_text(), submitted_id))
            conn.execute("UPDATE outbound_sms SET status = 'sent', updated_at = ? WHERE id = ?", (admin.now_text(), sent_id))
        submitted_ok, submitted_message = admin.request_cancel_outbound(submitted_id)
        sent_ok, sent_message = admin.request_cancel_outbound(sent_id)
        with admin.db() as conn:
            submitted = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (submitted_id,)).fetchone()
            sent = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (sent_id,)).fetchone()
        assert_true(not submitted_ok, "submitted SMS cannot be cancelled")
        assert_true("已提交" in submitted_message, "submitted cancel explains it is too late")
        assert_true(not sent_ok, "sent SMS cannot be cancelled")
        assert_true("已发送" in sent_message, "sent cancel explains it is too late")
        assert_true(submitted["status"] == "submitted", "submitted status stays unchanged")
        assert_true(sent["status"] == "sent", "sent status stays unchanged")
    finally:
        cleanup(base)


def test_reconcile_dispatch_result_updates_outbound_status():
    admin, base = load_admin()
    try:
        admin.init_db()
        item_id = admin.create_immediate_outbound("+8613003630212", "dispatch result", admin.now_text())
        with admin.db() as conn:
            conn.execute("UPDATE outbound_sms SET status = 'dispatching', attempts = 1, updated_at = ? WHERE id = ?", (admin.now_text(), item_id))
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
            job_id = admin.create_dispatch_job(conn, row)
            conn.execute(
                """
                UPDATE sms_dispatch_jobs
                SET status = 'submitted', result_status = 'submitted', result_message = 'AT +CMGS OK',
                    submitted_at = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (admin.now_text(), admin.now_text(), admin.now_text(), job_id),
            )
        admin.reconcile_dispatch_jobs()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(row["status"] == "submitted", "submitted dispatch result updates outbound status")
        assert_true(row["submitted_at"], "submitted dispatch result stores submitted time")
        assert_true(row["command_output"] == "AT +CMGS OK", "dispatch result message is retained")
    finally:
        cleanup(base)


def test_reconcile_cancelled_dispatch_marks_requested_outbound_cancelled():
    admin, base = load_admin()
    try:
        admin.init_db()
        item_id = admin.create_immediate_outbound("+8613003630212", "dispatch cancelled", admin.now_text())
        with admin.db() as conn:
            conn.execute(
                "UPDATE outbound_sms SET status = 'dispatching', attempts = 1, cancel_requested = 1, updated_at = ? WHERE id = ?",
                (admin.now_text(), item_id),
            )
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
            job_id = admin.create_dispatch_job(conn, row)
            conn.execute(
                """
                UPDATE sms_dispatch_jobs
                SET status = 'cancelled', result_status = 'cancelled', result_message = 'cancelled before send',
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (admin.now_text(), admin.now_text(), job_id),
            )
        admin.reconcile_dispatch_jobs()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (item_id,)).fetchone()
        assert_true(row["status"] == "cancelled", "cancelled dispatch with cancel request marks outbound cancelled")
        assert_true("cancelled before send" in row["last_error"], "cancelled dispatch reason is retained")
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
        scheduled_at = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).strftime(TIME_FORMAT)
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
        plan_id = admin.create_send_plan("Recurring", "13000000000", "planned again", scheduled_at, strategy_id)
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
        original_scheduled = dt.datetime.strptime(original_plan["scheduled_at"], TIME_FORMAT)
        next_scheduled = dt.datetime.strptime(next_row["scheduled_at"], TIME_FORMAT)
        assert_true(sent["status"] == "submitted", "original plan outbound is submitted")
        assert_true(plan_count == 1, "recurring send stays inside the same plan")
        assert_true(plan["outbound_id"] != original_outbound_id, "plan points at the next outbound row")
        assert_true(plan["scheduled_at"] == next_row["scheduled_at"], "plan scheduled time moves to the next send")
        assert_true(next_row["status"] == "queued", "next planned send is queued")
        assert_true(next_row["send_type"] == "plan", "next planned send keeps plan type")
        assert_true(next_row["plan_id"] == plan_id, "next planned send links to the same plan")
        assert_true(next_scheduled - original_scheduled == dt.timedelta(minutes=3), "next send uses planned time plus strategy interval")
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


def test_update_send_plan_syncs_only_current_queued_outbound():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        plan_id = admin.create_send_plan("Editable plan", "13000000000", "old body", admin.now_text(), strategy["id"])
        with admin.db() as conn:
            outbound_id = conn.execute("SELECT outbound_id FROM send_plans WHERE id = ?", (plan_id,)).fetchone()["outbound_id"]

        admin.update_send_plan(plan_id, "+8613000000001", "new body", "+86", "13000000001")

        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            outbound = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (outbound_id,)).fetchone()
        assert_true(plan["destination"] == "+8613000000001", "plan destination is updated")
        assert_true(plan["country_code"] == "+86", "plan country code is updated separately")
        assert_true(plan["phone_number"] == "13000000001", "plan phone number is updated separately")
        assert_true(plan["text"] == "new body", "plan text is updated")
        assert_true(outbound["destination"] == "+8613000000001", "current queued outbound destination follows plan")
        assert_true(outbound["text"] == "new body", "current queued outbound text follows plan")

        with admin.db() as conn:
            conn.execute("UPDATE outbound_sms SET status = 'submitted', updated_at = ? WHERE id = ?", (admin.now_text(), outbound_id))
        admin.update_send_plan(plan_id, "+8613000000002", "final body", "+86", "13000000002")
        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            outbound = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (outbound_id,)).fetchone()
        assert_true(plan["destination"] == "+8613000000002", "unarchived plan remains editable after current outbound leaves queue")
        assert_true(plan["phone_number"] == "13000000002", "unarchived plan keeps edited phone number separately")
        assert_true(plan["text"] == "final body", "unarchived plan text remains editable")
        assert_true(outbound["destination"] == "+8613000000001", "non-queued outbound destination is not rewritten")
        assert_true(outbound["text"] == "new body", "non-queued outbound text is not rewritten")

        admin.archive_send_plan(plan_id)
        try:
            admin.update_send_plan(plan_id, "+8613000000003", "archived body")
        except ValueError as exc:
            assert_true("已归档" in str(exc), "archived plan update reports clear error")
        else:
            raise AssertionError("archived plan update should be blocked")
    finally:
        cleanup(base)


def test_plan_detail_and_edit_modals_are_separate_for_unarchived_plans_only():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        active_plan_id = admin.create_send_plan("Editable plan", "13000000000", "editable body", admin.now_text(), strategy["id"])
        archived_plan_id = admin.create_send_plan("Archived plan", "13000000001", "locked body", admin.now_text(), strategy["id"])
        admin.archive_send_plan(archived_plan_id)

        body = admin.AdminHandler.render_plans(object()).decode("utf-8")
        active_dialog = body[body.index(f'id="plan-detail-{active_plan_id}"') : body.index("</dialog>", body.index(f'id="plan-detail-{active_plan_id}"'))]
        active_edit_dialog = body[body.index(f'id="plan-edit-{active_plan_id}"') : body.index("</dialog>", body.index(f'id="plan-edit-{active_plan_id}"'))]
        archived_dialog = body[body.index(f'id="plan-detail-{archived_plan_id}"') : body.index("</dialog>", body.index(f'id="plan-detail-{archived_plan_id}"'))]

        first_detail_pair = active_dialog[active_dialog.index('<div class="detail-grid">') : active_dialog.index("<b>名称</b>")]
        assert_true(f"<b>ID</b><span>#{active_plan_id}</span>" in first_detail_pair, "plan detail shows ID as the first row")
        assert_true(f'action="/plans/{active_plan_id}/update"' not in active_dialog, "plan detail stays read-only")
        assert_true(f"plan-edit-{active_plan_id}" in body, "unarchived plan has a separate edit modal")
        assert_true(f'action="/plans/{active_plan_id}/update"' in active_edit_dialog, "separate edit modal posts plan updates")
        assert_true("<label>区号</label>" in active_edit_dialog, "plan edit modal exposes country code")
        assert_true('name="country_code"' in active_edit_dialog, "plan edit modal has country code input")
        assert_true('value="+86"' in active_edit_dialog, "plan edit modal pre-fills stored country code")
        assert_true("<label>目标号码</label>" in active_edit_dialog, "plan edit modal exposes phone number")
        assert_true('name="destination"' in active_edit_dialog, "plan edit modal keeps destination field for phone number")
        assert_true('value="13000000000"' in active_edit_dialog, "plan edit modal pre-fills stored phone number")
        assert_true("<label>短信内容</label>" in active_edit_dialog, "plan edit modal exposes SMS content")
        assert_true("editable body" in active_edit_dialog, "plan edit modal pre-fills SMS content")
        assert_true('<div class="actions modal-actions">' in active_edit_dialog, "edit modal uses bottom-right modal actions")
        assert_true(active_edit_dialog.index("关闭") < active_edit_dialog.index("保存修改"), "edit modal shows close before save")
        assert_true(f'plan-edit-{archived_plan_id}' not in body, "archived plan does not have an edit modal")
        assert_true(f'action="/plans/{archived_plan_id}/update"' not in archived_dialog, "archived plan detail does not have update form")
        assert_true("locked body" in archived_dialog, "archived plan still shows read-only content")
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
        assert_true("<th>发送次数</th>" in table, "plan list merges send counters into one column")
        assert_true("<th>失败次数</th>" not in table, "plan list no longer has a separate failure count column")
        assert_true("<th>成功次数</th>" not in table, "plan list no longer has a separate success count column")
        assert_true("<th>下次发送</th>" in table, "plan list shows next send time")
        assert_true("<th>内容</th>" not in table, "plan list hides content column")
        assert_true("<th>错误</th>" not in table, "plan list hides error column")
        send_count_cell = table[table.index('class="send-counts"') : table.index("</td>", table.index('class="send-counts"'))]
        assert_true('class="count-pill failure">1</span>' in send_count_cell, "failure count is red")
        assert_true('class="count-pill success">1</span>' in send_count_cell, "success count is green")
        assert_true(send_count_cell.index('failure">1') < send_count_cell.index('success">1'), "failure count appears before success count")
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


def test_plan_list_paginates_ten_per_page_below_table():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        for i in range(12):
            admin.create_send_plan(f"Paged plan {i}", f"130000001{i:02d}", f"body {i}", admin.now_text(), strategy["id"])

        class Dummy:
            path = "/plans?page=2"

        body = admin.AdminHandler.render_plans(Dummy()).decode("utf-8")
        table_start = body.index("<h2>发送计划</h2>")
        table_end = body.index("</table>", table_start)
        table = body[table_start:table_end]
        pager_pos = body.index("共 12 条")

        assert_true(table.count("<tr><td>#") == 2, "plans page shows ten rows per page")
        assert_true("page=1" in body, "plans pagination links to the previous page")
        assert_true(table_end < pager_pos, "plans pagination sits below the table")
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
        assert_true(body.index("</table>", body.index("<h2>发送列表</h2>")) < body.index("共 12 条"), "outbound pagination sits below the table")
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
        assert_true("计划列表操作" not in body, "planned queued row leaves the operation area empty")
    finally:
        cleanup(base)


def test_outbound_list_moves_error_into_detail_modal_and_shows_scheduled_minute():
    admin, base = load_admin()
    try:
        admin.init_db()
        raw_time = "2026-06-10 22:00:00 +0800"
        display_minute = "2026/06/10 22:00"
        outbound_id = admin.create_immediate_outbound("13000000000", "detail body", raw_time)
        with admin.db() as conn:
            conn.execute(
                """
                UPDATE outbound_sms
                SET created_at = ?, updated_at = ?, scheduled_at = ?,
                    next_attempt_at = ?, last_error = ?
                WHERE id = ?
                """,
                (raw_time, raw_time, raw_time, raw_time, "modem not ready", outbound_id),
            )

        class Dummy:
            path = "/outbound"
            table_outbound = admin.AdminHandler.table_outbound

        body = admin.AdminHandler.render_outbound(Dummy()).decode("utf-8")
        table = body[body.index("<h2>发送列表</h2>") : body.index("</table>", body.index("<h2>发送列表</h2>"))]
        dialog = body[body.index(f'id="outbound-detail-{outbound_id}"') : body.index("</dialog>", body.index(f'id="outbound-detail-{outbound_id}"'))]

        assert_true("<th>错误</th>" not in table, "outbound list no longer has an error column")
        assert_true("modem not ready" not in table, "outbound list hides row error text")
        assert_true("详情" in table, "outbound list exposes a detail action")
        assert_true("<b>计划发送时间</b>" in dialog, "detail modal adds scheduled send time")
        assert_true(
            f"<b>计划发送时间</b><span>{display_minute}</span>" in dialog,
            "scheduled send time is shown to minute precision",
        )
        assert_true(
            "<b>计划发送时间</b><span>2026/06/10 22:00:00</span>" not in dialog,
            "scheduled send time does not show seconds",
        )
        assert_true("modem not ready" in dialog, "detail modal contains row error text")
        assert_true("detail body" in dialog, "detail modal contains message text")
    finally:
        cleanup(base)


def test_admin_pages_display_times_in_js_style_format():
    admin, base = load_admin()
    try:
        admin.init_db()
        raw_time = "2026-06-10 22:00:00 +0800"
        display_time = "2026/06/10 22:00:00"
        display_minute = "2026/06/10 22:00"
        strategy = admin.get_default_strategy()
        outbound_id = admin.create_immediate_outbound("13000000000", "time body", raw_time)
        plan_id = admin.create_send_plan("Time plan", "13000000001", "planned time", raw_time, strategy["id"])
        with admin.db() as conn:
            conn.execute(
                """
                INSERT INTO inbound_sms
                  (received_at, sender, text, raw_ids, phone_id, sms_messages, decoded_parts, forwarded)
                VALUES (?, '+8613000000000', 'time inbound', '', '', 1, 1, 0)
                """,
                (raw_time,),
            )
            conn.execute("UPDATE outbound_sms SET created_at = ?, updated_at = ? WHERE id = ?", (raw_time, raw_time, outbound_id))

        class InboundDummy:
            path = "/inbound"
            table_inbound = admin.AdminHandler.table_inbound

        class OutboundDummy:
            path = "/outbound"
            table_outbound = admin.AdminHandler.table_outbound

        inbound_body = admin.AdminHandler.render_inbound(InboundDummy()).decode("utf-8")
        outbound_body = admin.AdminHandler.render_outbound(OutboundDummy()).decode("utf-8")
        plans_body = admin.AdminHandler.render_plans(object()).decode("utf-8")

        assert_true(display_time in inbound_body, "inbound page displays slash-formatted receive time")
        assert_true(display_time in outbound_body, "outbound page displays slash-formatted created time")
        assert_true(display_minute in plans_body, "plans page displays scheduled time to the minute")
        assert_true(raw_time not in inbound_body, "inbound page hides raw timezone time")
        assert_true(raw_time not in outbound_body, "outbound page hides raw timezone time")
        assert_true(raw_time not in plans_body, "plans page hides raw timezone time")
        plan_table = plans_body[plans_body.index("<h2>发送计划</h2>") : plans_body.index("</table>", plans_body.index("<h2>发送计划</h2>"))]
        assert_true(display_time not in plan_table, "plan list next send time hides seconds")
        assert_true(f"plan-detail-{plan_id}" in plans_body, "plan detail still renders after time formatting")
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


def test_inbound_page_paginates_ten_per_page_and_preserves_filters():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            for i in range(12):
                conn.execute(
                    """
                    INSERT INTO inbound_sms
                      (received_at, sender, text, raw_ids, phone_id, sms_messages, decoded_parts, forwarded)
                    VALUES (?, ?, ?, '', '', 1, 1, 1)
                    """,
                    (admin.now_text(), f"+861300000{i:02d}", f"invoice page {i}",),
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
            path = "/inbound?sender=130000&forwarded=1&page=2"
            table_inbound = admin.AdminHandler.table_inbound

        body = admin.AdminHandler.render_inbound(Dummy()).decode("utf-8")
        table_start = body.index("<h2>接收列表</h2>")
        table_end = body.index("</table>", table_start)
        table = body[table_start:table_end]
        pager_pos = body.index("共 12 条")

        assert_true(table.count("<tr><td>#") == 2, "inbound page shows ten rows per page")
        assert_true("sender=130000" in body and "forwarded=1" in body and "page=1" in body, "inbound pagination preserves filters")
        assert_true(table_end < pager_pos, "inbound pagination sits below the table")
    finally:
        cleanup(base)


def test_forward_pending_inbound_sms_sends_mail_and_updates_status():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            cur = conn.execute(
                """
                INSERT INTO inbound_sms
                  (received_at, sender, text, raw_ids, phone_id, sms_messages, decoded_parts, forwarded, forward_error)
                VALUES (?, '10000', '账单提醒', 'pve-cmgl-8', 'pve-radio-agent', 1, 1, 0, '')
                """,
                (admin.now_text(),),
            )
            record_id = int(cur.lastrowid)
        calls = []
        admin.send_notification = lambda subject, plain, html_body="": calls.append((subject, plain, html_body)) or (1, "")
        admin.forward_pending_inbound_sms()
        with admin.db() as conn:
            row = conn.execute("SELECT * FROM inbound_sms WHERE id = ?", (record_id,)).fetchone()
        assert_true(calls, "pending inbound SMS triggers mail notification")
        assert_true("新短信提醒" in calls[0][0], "inbound notification uses SMS subject")
        assert_true("账单提醒" in calls[0][1], "plain mail body contains SMS text")
        assert_true(row["forwarded"] == 1, "successful inbound mail marks row forwarded")
        assert_true(row["forward_error"] == "", "successful inbound mail clears forward error")
    finally:
        cleanup(base)


def test_forward_inbound_sms_by_id_only_sends_target_row():
    admin, base = load_admin()
    try:
        admin.init_db()
        with admin.db() as conn:
            first = conn.execute(
                """
                INSERT INTO inbound_sms
                  (received_at, sender, text, raw_ids, phone_id, sms_messages, decoded_parts, forwarded, forward_error)
                VALUES (?, '10000', 'first', 'first-id', 'pve', 1, 1, 0, '')
                """,
                (admin.now_text(),),
            )
            second = conn.execute(
                """
                INSERT INTO inbound_sms
                  (received_at, sender, text, raw_ids, phone_id, sms_messages, decoded_parts, forwarded, forward_error)
                VALUES (?, '10001', 'second', 'second-id', 'pve', 1, 1, 0, '')
                """,
                (admin.now_text(),),
            )
            first_id = int(first.lastrowid)
            second_id = int(second.lastrowid)
        calls = []
        admin.send_notification = lambda subject, plain, html_body="": calls.append((subject, plain, html_body)) or (1, "")
        admin.forward_inbound_sms_by_id(second_id)
        with admin.db() as conn:
            first_row = conn.execute("SELECT * FROM inbound_sms WHERE id = ?", (first_id,)).fetchone()
            second_row = conn.execute("SELECT * FROM inbound_sms WHERE id = ?", (second_id,)).fetchone()
        assert_true(len(calls) == 1, "inbound event forwards one row")
        assert_true("second" in calls[0][1], "event forwards the requested row")
        assert_true(first_row["forwarded"] == 0, "other pending rows remain for fallback polling")
        assert_true(second_row["forwarded"] == 1, "requested row is marked forwarded")
    finally:
        cleanup(base)


def test_parse_inbound_notification_payload_accepts_json_and_plain_id():
    admin, base = load_admin()
    try:
        assert_true(admin.parse_inbound_notification_payload('{"inbound_id": 12}') == 12, "JSON inbound id is parsed")
        assert_true(admin.parse_inbound_notification_payload("13") == 13, "plain inbound id is parsed")
        assert_true(admin.parse_inbound_notification_payload("{}") is None, "missing id is ignored")
    finally:
        cleanup(base)


def test_inbound_notification_loop_keeps_idle_subscription_without_error_reconnect():
    admin, base = load_admin()

    class StopEvent:
        def __init__(self):
            self.done = False

        def is_set(self):
            return self.done

        def wait(self, _seconds):
            self.done = True
            return True

    class FakeSock:
        def __init__(self):
            self.closed = False

        def settimeout(self, _timeout):
            pass

        def sendall(self, _payload):
            pass

        def close(self):
            self.closed = True

    class FakeSelect:
        @staticmethod
        def select(_readers, _writers, _errors, _timeout):
            stop_event.done = True
            return [], [], []

    stop_event = StopEvent()
    sock = FakeSock()
    logs = []
    reads = []
    try:
        admin.sms_redis_url = lambda: "redis://127.0.0.1:6379/0"
        admin.redis_connect = lambda _url, timeout=5: (sock, object())

        def read_value(_reader):
            reads.append(1)
            if len(reads) == 1:
                return ["subscribe", "sms:inbound", 1]
            raise OSError("cannot read from timed out object")

        admin.redis_read_value = read_value
        admin.log_worker = logs.append
        admin.select = FakeSelect

        admin.inbound_notification_loop(stop_event)

        assert_true(reads == [1], "idle subscription reads only the subscribe acknowledgement")
        assert_true(logs == ["redis_subscribed channel=sms:inbound"], "idle subscription does not log reconnect errors")
        assert_true(sock.closed, "idle subscription closes the socket during shutdown")
    finally:
        cleanup(base)


def test_worker_interval_aligns_queue_scan_to_next_minute():
    previous = os.environ.get("SMS_WORKER_INTERVAL_SECONDS")
    admin, base = load_admin()
    try:
        now = dt.datetime(2026, 6, 11, 23, 0, 10, 500000)
        assert_true(admin.worker_interval_seconds(now) == 49.5, "worker waits until the next minute boundary")
        now = dt.datetime(2026, 6, 11, 23, 0, 59, 900000)
        assert_true(round(admin.worker_interval_seconds(now), 1) == 0.1, "worker can wake just after the next minute starts")
        now = dt.datetime(2026, 6, 11, 23, 1, 0, 0)
        assert_true(admin.worker_interval_seconds(now) == 0.0, "worker can scan immediately at a minute boundary")
        os.environ["SMS_WORKER_INTERVAL_SECONDS"] = "2.5"
        assert_true(admin.worker_interval_seconds(now) == 0.0, "worker queue scan interval is no longer sub-minute configurable")
    finally:
        if previous is None:
            os.environ.pop("SMS_WORKER_INTERVAL_SECONDS", None)
        else:
            os.environ["SMS_WORKER_INTERVAL_SECONDS"] = previous
        cleanup(base)


def test_create_send_plan_creates_planned_outbound():
    admin, base = load_admin()
    try:
        admin.init_db()
        strategy = admin.get_default_strategy()
        scheduled_at = (dt.datetime.now().astimezone() + dt.timedelta(minutes=20)).strftime(TIME_FORMAT)
        plan_id = admin.create_send_plan(
            "Morning check",
            "+8613000000000",
            "planned hello",
            scheduled_at,
            strategy["id"],
            "+86",
            "13000000000",
        )
        with admin.db() as conn:
            plan = conn.execute("SELECT * FROM send_plans WHERE id = ?", (plan_id,)).fetchone()
            outbound = conn.execute("SELECT * FROM outbound_sms WHERE id = ?", (plan["outbound_id"],)).fetchone()
        assert_true(plan["strategy_id"] == strategy["id"], "plan records strategy id")
        assert_true(plan["country_code"] == "+86", "plan stores country code separately")
        assert_true(plan["phone_number"] == "13000000000", "plan stores phone number separately")
        assert_true(plan["destination"] == "+8613000000000", "plan stores combined destination for sending")
        assert_true(outbound["send_type"] == "plan", "planned outbound uses plan type")
        assert_true(outbound["plan_id"] == plan_id, "planned outbound links back to plan")
        assert_true(outbound["destination"] == "+8613000000000", "planned outbound uses combined destination")
        assert_true(outbound["scheduled_at"] == scheduled_at, "planned outbound keeps scheduled time")
    finally:
        cleanup(base)


def main():
    tests = [
        test_postgres_initial_migration_contains_dispatch_and_cancel_fields,
        test_postgres_migration_adds_send_plan_phone_parts_to_existing_tables,
        test_dockerfile_sets_admin_container_timezone,
        test_pve_agent_parses_unread_cmgl_messages,
        test_pve_agent_dispatch_uses_gammu_smsd_inject,
        test_pve_agent_send_sms_gammu_marks_sent_when_smsd_moves_spool_file,
        test_pve_agent_status_reports_failed_smsd_over_stale_monitor_memory,
        test_pve_agent_status_reports_active_smsd_init_errors_over_empty_monitor_identity,
        test_pve_agent_status_checks_at_presence_over_stale_monitor_imsi,
        test_pve_agent_status_checks_mbim_presence_over_stale_monitor_imsi,
        test_pve_agent_status_checks_mbim_presence_over_empty_monitor_identity,
        test_pve_agent_status_reads_phone_number_from_mbim,
        test_pve_agent_status_builds_e164_for_uk_local_mbim_number,
        test_pve_agent_reuses_static_identity_without_rereading_mbim_number,
        test_pve_agent_status_converts_monitor_signal_to_dbm_without_at_query,
        test_pve_agent_status_does_not_convert_unknown_signal_to_dbm,
        test_pve_agent_status_checks_at_presence_when_smsd_is_inactive,
        test_pve_watchdog_treats_empty_monitor_imsi_as_not_ready,
        test_pve_watchdog_prefers_mbim_presence_over_stale_monitor_imsi,
        test_pve_watchdog_recovery_reenables_mbim_software_radio,
        test_pve_watchdog_main_does_not_force_wwan_down_when_sim_ready,
        test_sms_received_hook_can_disable_local_mail_forwarding,
        test_sms_received_hook_publishes_inbound_notification_when_configured,
        test_sms_received_hook_defers_and_combines_multipart_within_short_window,
        test_sms_received_hook_flushes_incomplete_multipart_after_short_window,
        test_strategy_schema_and_default_minutes,
        test_create_outbound_records_type_and_strategy_snapshot,
        test_destination_from_form_applies_country_code,
        test_split_destination_for_form_separates_known_country_codes,
        test_destination_parts_from_form_keep_storage_fields_separate,
        test_send_interval_allows_180_days,
        test_delete_strategy_removes_non_default_but_keeps_snapshots,
        test_delete_default_strategy_is_blocked,
        test_dashboard_omits_cost_protection_notice,
        test_dashboard_sim_status_places_phone_signal_unit_and_data_switch,
        test_dashboard_sim_status_renders_dbm_signal_quality_pill,
        test_signal_quality_uses_mobile_dbm_strength_table,
        test_dashboard_sim_error_uses_bad_pill_without_error_detail,
        test_dashboard_uses_pve_service_label_in_pve_dispatch_mode,
        test_login_page_uses_border_box_sizing,
        test_send_form_uses_default_strategy_and_country_code_field,
        test_plan_form_country_code_and_optional_time,
        test_optional_future_time_allows_blank_for_immediate_send,
        test_strategy_page_uses_table_and_modals_without_command_timeout,
        test_strategy_send_interval_uses_value_and_unit_ui,
        test_strategy_update_preserves_hidden_command_timeout,
        test_delete_and_archive_list_actions_require_confirmation,
        test_parse_sim_status_output,
        test_parse_smsd_monitor_output,
        test_parse_smsd_monitor_output_shows_missing_sim_and_network_off,
        test_parse_smsd_monitor_output_hides_unknown_negative_signal,
        test_parse_smsd_health_output_reports_recent_no_sim,
        test_get_sim_status_prefers_smsd_health_issue_over_stale_monitor_memory,
        test_get_sim_status_can_bypass_cache_and_use_smsd_monitor,
        test_get_sim_status_ignores_smsd_database_for_realtime_status,
        test_get_sim_status_uses_pve_agent_when_configured,
        test_pve_agent_timeout_keeps_cached_static_identity_only,
        test_sse_event_formats_json_payload,
        test_sim_status_stream_refreshes_every_second,
        test_sim_status_stream_payload_prefers_e164_phone_number,
        test_sms_command_uses_unicode_for_chinese_text,
        test_sms_command_keeps_gsm7_without_unicode_flag,
        test_status_badge_distinguishes_waiting_sent_and_timeout,
        test_mail_settings_are_saved_in_database_and_password_is_not_echoed,
        test_mail_test_saves_form_and_sends_detection_mail,
        test_send_notification_uses_database_smtp_settings,
        test_reconcile_submitted_outbox_timeout_removes_file_and_marks_timeout,
        test_reconcile_submitted_sent_file_marks_sent,
        test_planned_sms_sent_success_sends_mail_notification,
        test_process_one_due_blocks_no_sim_before_injecting,
        test_process_one_due_pve_dispatch_creates_idempotent_job_without_local_gammu,
        test_cancel_dispatching_sets_cancel_request_without_overwriting_status,
        test_cancel_submitted_and_sent_are_rejected,
        test_reconcile_dispatch_result_updates_outbound_status,
        test_reconcile_cancelled_dispatch_marks_requested_outbound_cancelled,
        test_retry_wait_uses_minutes,
        test_normal_send_is_unbound_and_ignores_strategy_interval,
        test_planned_send_is_not_blocked_by_global_submission_interval,
        test_planned_send_checks_only_same_plan_interval,
        test_successful_plan_creates_next_outbound_from_strategy_interval,
        test_successful_plan_with_zero_strategy_interval_does_not_create_next_outbound,
        test_delete_send_plan_soft_deletes_only_without_send_history,
        test_archive_send_plan_keeps_plan_visible_after_send_history,
        test_update_send_plan_syncs_only_current_queued_outbound,
        test_plan_detail_and_edit_modals_are_separate_for_unarchived_plans_only,
        test_plan_list_uses_modal_counts_next_time_and_archive_action_for_history,
        test_plan_list_shows_delete_for_plan_without_send_history_and_hides_deleted,
        test_plan_list_paginates_ten_per_page_below_table,
        test_outbound_page_uses_send_list_title_and_paginates_ten_per_page,
        test_outbound_page_filters_by_plan_strategy_status_and_type,
        test_outbound_filters_are_compact_right_aligned_and_plan_rows_do_not_cancel_there,
        test_outbound_list_moves_error_into_detail_modal_and_shows_scheduled_minute,
        test_admin_pages_display_times_in_js_style_format,
        test_inbound_page_filters_by_sender_keyword_and_forward_status,
        test_inbound_page_paginates_ten_per_page_and_preserves_filters,
        test_forward_pending_inbound_sms_sends_mail_and_updates_status,
        test_forward_inbound_sms_by_id_only_sends_target_row,
        test_parse_inbound_notification_payload_accepts_json_and_plain_id,
        test_inbound_notification_loop_keeps_idle_subscription_without_error_reconnect,
        test_worker_interval_aligns_queue_scan_to_next_minute,
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
