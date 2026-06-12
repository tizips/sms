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


KNOWN_COUNTRY_CODES = ("86", "44", "1")


def normalize_phone_number(value: str) -> str:
    text = value.strip().replace(" ", "")
    if not text.isdigit() or len(text) < 5 or len(text) > 20:
        raise ValueError("手机号格式不正确，只支持 5-20 位数字")
    return text


def split_destination_for_form(destination: str) -> tuple[str, str]:
    text = destination.strip().replace(" ", "")
    if not text:
        return "+86", ""
    if not text.startswith("+"):
        return "+86", text
    digits = text[1:]
    for code in sorted(KNOWN_COUNTRY_CODES, key=len, reverse=True):
        if digits.startswith(code) and len(digits) > len(code):
            return f"+{code}", digits[len(code) :]
    return "+86", text


def destination_parts_from_form(form: dict) -> tuple[str, str, str]:
    destination = form.get("destination", "").strip()
    if destination.replace(" ", "").startswith("+"):
        combined = normalize_destination(destination)
        country_code, phone_number = split_destination_for_form(combined)
        if phone_number.startswith("+"):
            raise ValueError("粘贴国际号码时需要使用已识别的区号")
        return country_code, normalize_phone_number(phone_number), combined
    country_code = normalize_country_code(form.get("country_code", "+86"))
    phone_number = normalize_phone_number(destination)
    return country_code, phone_number, normalize_destination(country_code + phone_number)


def destination_from_form(form: dict) -> str:
    return destination_parts_from_form(form)[2]


def sim_status_stream_payload(status: dict) -> dict:
    payload = dict(status)
    if payload.get("phone_number_e164"):
        payload["phone_number"] = payload["phone_number_e164"]
    return payload


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


def sim_state_tone(sim_state: str = "", error: str = "") -> str:
    if error or "无法访问" in sim_state or "不可用" in sim_state or "通信失败" in sim_state:
        return "bad"
    if sim_state.startswith("未识别"):
        return "warn"
    if sim_state.startswith("已识别"):
        return "ok"
    return "muted"


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
        "sim_state_tone": "muted",
        "sim_identity": "",
        "phone_number": "",
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
    info["sim_state_tone"] = sim_state_tone(info["sim_state"])
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
    if signal in {"-1", "255"}:
        signal = ""
    battery = values.get("batterpercent", "")
    imsi = values.get("imsi", "")
    operator = values.get("netname", "") or values.get("netcode", "")
    sms_counts = format_sms_counts(values.get("sent", ""), values.get("received", ""), values.get("failed", ""))
    network_state = "smsd 运行中" if values else ""
    switch, switch_tone = describe_network_switch(signal, f"{signal}%" if signal else "", network_state)
    sim_state = describe_sim_state(imsi, bool(values))
    return {
        "sim_state": sim_state,
        "sim_state_tone": sim_state_tone(sim_state),
        "sim_identity": mask_sim_identity(imsi),
        "phone_number": "",
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


def sim_problem_status(sim_state: str, network_state: str) -> dict:
    switch, switch_tone = describe_network_switch("", "", network_state)
    return {
        "sim_state": sim_state,
        "sim_state_tone": sim_state_tone(sim_state),
        "sim_identity": "",
        "phone_number": "",
        "sms_counts": "",
        "signal": "",
        "network_level": "",
        "network_state": network_state,
        "network_switch": switch,
        "network_switch_tone": switch_tone,
        "operator": "",
        "battery": "",
        "error": "",
        "source_updated_at": "",
        "checked_at": now_text(),
    }


def parse_smsd_health_output(output: str) -> dict | None:
    if "NOSIM" in output or "无法访问 SIM 卡" in output:
        return sim_problem_status("未识别（无法访问 SIM 卡）", "smsd 无法访问 SIM 卡")
    if "Error at init connection" in output or "UNKNOWN[27]" in output:
        return sim_problem_status("状态不可用（串口通信失败）", "smsd 串口通信失败")
    return None


def read_smsd_health_status() -> dict | None:
    cmd = [
        "journalctl",
        "-u",
        "gammu-smsd",
        "--no-pager",
        "--since",
        f"{SMSD_HEALTH_LOG_MINUTES} minutes ago",
        "-n",
        "120",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return parse_smsd_health_output((result.stdout + "\n" + result.stderr).strip())


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
    if status.get("signal") or status.get("sim_identity") or str(status.get("sim_state") or "").startswith("已识别"):
        return status
    return None


def pve_radio_agent_url() -> str:
    return os.environ.get("PVE_RADIO_AGENT_URL", "").strip().rstrip("/")


def sms_redis_url() -> str:
    return (
        os.environ.get("SMS_REDIS_URL", "").strip()
        or os.environ.get("REDIS_URL", "").strip()
        or os.environ.get("VALKEY_URL", "").strip()
    )


def inbound_channel() -> str:
    return os.environ.get("SMS_INBOUND_CHANNEL", "sms:inbound").strip() or "sms:inbound"


def parse_redis_url(url: str) -> tuple[str, int, str, int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"redis", "valkey"}:
        raise ValueError("SMS_REDIS_URL must use redis:// or valkey://")
    password = urllib.parse.unquote(parsed.password or "")
    db_text = (parsed.path or "/0").lstrip("/") or "0"
    return parsed.hostname or "127.0.0.1", parsed.port or 6379, password, int(db_text)


def redis_command_bytes(*parts: str) -> bytes:
    payload = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        data = str(part).encode("utf-8")
        payload.append(f"${len(data)}\r\n".encode("ascii"))
        payload.append(data + b"\r\n")
    return b"".join(payload)


def redis_read_value(reader):
    line = reader.readline()
    if not line:
        raise ConnectionError("redis connection closed")
    prefix = line[:1]
    body = line[1:].rstrip(b"\r\n")
    if prefix == b"+":
        return body.decode("utf-8", errors="replace")
    if prefix == b"-":
        raise RuntimeError(body.decode("utf-8", errors="replace"))
    if prefix == b":":
        return int(body)
    if prefix == b"$":
        size = int(body)
        if size < 0:
            return None
        data = reader.read(size)
        reader.read(2)
        return data.decode("utf-8", errors="replace")
    if prefix == b"*":
        return [redis_read_value(reader) for _ in range(int(body))]
    raise RuntimeError(f"unknown redis response: {line!r}")


def redis_connect(url: str, timeout: float = 5.0):
    host, port, password, database = parse_redis_url(url)
    sock = socket.create_connection((host, port), timeout=timeout)
    reader = sock.makefile("rb")
    if password:
        sock.sendall(redis_command_bytes("AUTH", password))
        redis_read_value(reader)
    if database:
        sock.sendall(redis_command_bytes("SELECT", str(database)))
        redis_read_value(reader)
    return sock, reader


def parse_inbound_notification_payload(payload: str) -> int | None:
    text = (payload or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            value = parsed.get("inbound_id") or parsed.get("id")
        else:
            value = parsed
    except Exception:
        value = text
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def log_worker(message: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / "sms-admin-worker.log").open("a", encoding="utf-8") as fh:
        fh.write(f"{now_text()} {message}\n")


def read_pve_agent_sim_status() -> dict | None:
    base_url = pve_radio_agent_url()
    if not base_url:
        return None
    request = urllib.request.Request(f"{base_url}/sim/status", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "sim_state": "状态不可用（PVE Agent 通信失败）",
            "sim_state_tone": "bad",
            "sim_identity": "",
            "phone_number": "",
            "sms_counts": "",
            "signal": "",
            "network_level": "",
            "network_state": "",
            "network_switch": "数据关闭",
            "network_switch_tone": "ok",
            "operator": "",
            "battery": "",
            "error": str(exc)[:300],
            "source_updated_at": "",
            "checked_at": now_text(),
        }
    defaults = {
        "sim_state": "状态未知",
        "sim_state_tone": "muted",
        "sim_identity": "",
        "phone_number": "",
        "sms_counts": "",
        "signal": "",
        "network_level": "",
        "network_state": payload.get("registration_state", ""),
        "network_switch": "数据关闭",
        "network_switch_tone": "ok",
        "operator": "",
        "battery": "",
        "error": "",
        "source_updated_at": "",
        "checked_at": now_text(),
    }
    defaults.update({key: value for key, value in payload.items() if value is not None})
    defaults["network_switch"] = "数据关闭"
    defaults["network_switch_tone"] = "ok" if defaults.get("wwan_state") in {"", "DOWN"} else "warn"
    return defaults


def render_status_pill(label: str, tone: str = "muted") -> str:
    safe_tone = tone if tone in {"ok", "bad", "warn", "muted", "info"} else "muted"
    return f'<span class="status-pill {safe_tone}">{html.escape(label or "未知")}</span>'


def render_network_level_value(status: dict) -> str:
    level = html.escape(str(status.get("network_level") or "-"))
    return f'<span data-network-level-text>{level}</span>'


def render_operator_value(status: dict) -> str:
    operator = html.escape(str(status.get("operator") or "-"))
    pill = render_status_pill(str(status.get("network_switch") or "未知"), str(status.get("network_switch_tone") or "muted"))
    return f'<span data-operator-text>{operator}</span>{pill}'


def render_sim_state_value(label: str, tone: str) -> str:
    safe_tone = tone if tone in {"ok", "bad", "warn", "muted", "info"} else "muted"
    escaped = html.escape(label)
    return f'<span class="status-pill {safe_tone}" data-sim-badge>{escaped}</span>'


def get_sim_status(use_cache: bool = False) -> dict:
    pve_status = read_pve_agent_sim_status()
    if pve_status:
        return pve_status

    smsd_status = read_smsd_monitor_status()
    smsd_health = read_smsd_health_status()
    if smsd_health:
        if smsd_status and smsd_status.get("sms_counts"):
            smsd_health["sms_counts"] = smsd_status["sms_counts"]
        return smsd_health
    if smsd_status:
        return smsd_status

    cmd = ["/usr/bin/gammu" if Path("/usr/bin/gammu").exists() else "gammu", "-c", str(SMSDRC), "monitor", "1"]
    status = {
        "sim_state": "状态未知",
        "sim_state_tone": "muted",
        "sim_identity": "",
        "phone_number": "",
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
            status["sim_state_tone"] = sim_state_tone(status["sim_state"], status["error"])
    except subprocess.TimeoutExpired:
        status["error"] = "读取 SIM 状态超时"
        status["sim_state_tone"] = sim_state_tone(status["sim_state"], status["error"])
    except Exception as exc:
        status["error"] = str(exc)[:300]
        status["sim_state_tone"] = sim_state_tone(status["sim_state"], status["error"])

    return status


def render_sim_status_panel() -> str:
    status = get_sim_status()
    sim_label = status.get("sim_state") or "未知"
    tone = "bad" if status.get("error") else str(status.get("sim_state_tone") or sim_state_tone(str(sim_label)))
    rows = [
        ("sim_state", "SIM 卡", render_sim_state_value(str(sim_label), tone)),
        ("sim_identity", "SIM 标识", html.escape(str(status.get("sim_identity") or "-"))),
        ("phone_number", "手机号", html.escape(str(status.get("phone_number") or "-"))),
        ("sms_counts", "短信计数", html.escape(str(status.get("sms_counts") or "-"))),
        ("signal", "信号强度", html.escape(format_signal_strength(status.get("signal")))),
        ("operator", "运营商", render_operator_value(status)),
        ("checked_at", "检测时间", html.escape(format_admin_time(status.get("checked_at")))),
    ]
    body = "".join(
        f'<tr><th>{html.escape(label)}</th><td data-sim-field="{html.escape(field)}">{value}</td></tr>'
        for field, label, value in rows
    )
    return (
        '<div id="sim-status-panel" class="panel band sim-status"><div class="sim-status-head"><h2>SIM 卡状态</h2>'
        '<button type="button" class="secondary sim-refresh" data-sim-refresh>刷新</button></div>'
        f'<table class="kv">{body}</table></div>'
    )
