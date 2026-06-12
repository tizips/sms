PAGE_SIZE = 10


def request_page(path: str, default: int = 1) -> int:
    parsed = urllib.parse.urlparse(path or "")
    query = urllib.parse.parse_qs(parsed.query)
    return parse_positive_int(query.get("page", [str(default)])[0], default, 1, 999999)


def pagination_bar(path: str, base_query: dict, page_num: int, total: int, limit: int = PAGE_SIZE) -> str:
    offset = (page_num - 1) * limit

    def page_link(num: int, label: str) -> str:
        params = dict(base_query)
        params["page"] = str(num)
        return f'<a class="button secondary" href="{path}?{urllib.parse.urlencode(params)}">{html.escape(label)}</a>'

    pager_buttons = []
    if page_num > 1:
        pager_buttons.append(page_link(page_num - 1, "上一页"))
    if offset + limit < total:
        pager_buttons.append(page_link(page_num + 1, "下一页"))
    return (
        f'<div class="toolbar pager"><div class="hint">共 {total} 条，每页 {limit} 条，第 {page_num} 页</div>'
        f'<div class="table-actions">{"".join(pager_buttons)}</div></div>'
    )


def strategy_interval_control(minutes: int) -> str:
    value, selected_unit = send_interval_parts(int(minutes))
    unit_options = "".join(
        f'<option value="{key}"{" selected" if key == selected_unit else ""}>{label}</option>'
        for key, label, _factor in INTERVAL_UNITS
    )
    return f"""
      <label>发送间隔</label>
      <div class="interval-input">
        <input name="send_interval_value" type="number" min="0" max="{SEND_INTERVAL_MAX_MINUTES}" value="{value}">
        <select name="send_interval_unit">{unit_options}</select>
      </div>
      <div class="hint">最多 180 天；间隔为 0 时，计划短信发送成功后不开启下一次发送。</div>
    """


def confirm_submit_attr(message: str) -> str:
    return f' onsubmit="return confirm(\'{html.escape(message, quote=True)}\')"'


def row_value(row, key: str, default: str = ""):
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


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
                status = sim_status_stream_payload(get_sim_status(use_cache=False))
                self.wfile.write(format_sse_event("sim-status", status).encode("utf-8"))
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
        elif path == "/mail":
            self.send_html(self.render_mail_settings())
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
        elif path == "/mail":
            self.handle_mail_settings()
        elif path == "/mail/test":
            self.handle_mail_test()
        elif path.startswith("/settings/") and path.endswith("/delete"):
            self.handle_strategy_delete(path)
        elif path == "/password":
            self.handle_password()
        elif path.startswith("/outbound/") and path.endswith("/cancel"):
            self.handle_cancel(path)
        elif path.startswith("/plans/") and path.endswith("/update"):
            self.handle_plan_update(path)
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
            sent_count = conn.execute("SELECT COUNT(*) AS c FROM outbound_sms WHERE status = 'sent'").fetchone()["c"]
            pending_count = conn.execute(
                "SELECT COUNT(*) AS c FROM outbound_sms WHERE status IN ('queued','retry_wait','sending','submitted')"
            ).fetchone()["c"]
            risk_count = conn.execute(
                "SELECT COUNT(*) AS c FROM outbound_sms WHERE status IN ('failed','ambiguous','send_timeout')"
            ).fetchone()["c"]
            recent_in = conn.execute("SELECT * FROM inbound_sms ORDER BY id DESC LIMIT 5").fetchall()
            recent_out = conn.execute("SELECT * FROM outbound_sms ORDER BY id DESC LIMIT 5").fetchall()
        if use_pve_dispatch():
            service_label = "PVE Radio Agent"
            status = get_sim_status()
            service_ok = not status.get("error")
            service = "active" if service_ok else "unavailable"
        else:
            service_label = "gammu-smsd"
            try:
                service = subprocess.run(["/bin/systemctl", "is-active", "gammu-smsd"], capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception:
                service = "unknown"
        service_badge = '<span class="badge ok">active</span>' if service == "active" else f'<span class="badge bad">{html.escape(service or "unknown")}</span>'
        stats = f"""
        <div class="grid stats">
          <div class="panel stat"><div class="label">接收短信</div><div class="value">{inbound_count}</div></div>
          <div class="panel stat"><div class="label">已发送</div><div class="value">{sent_count}</div></div>
          <div class="panel stat"><div class="label">待处理发送</div><div class="value">{pending_count}</div></div>
          <div class="panel stat"><div class="label">失败/不明/超时</div><div class="value">{risk_count}</div></div>
        </div>
        <div class="panel band"><b>服务状态</b>：{html.escape(service_label)} {service_badge}</div>
        """
        return page("数据面板", render_sim_status_panel() + stats + self.table_inbound(recent_in, "最近接收") + self.table_outbound(recent_out, "最近发送"), "dashboard")

    def table_inbound(self, rows, title: str = "接收列表") -> str:
        body = "".join(
            f"<tr><td>#{r['id']}</td><td>{html.escape(format_admin_time(r['received_at']))}</td><td>{html.escape(r['sender'])}</td><td class=\"msg\">{html.escape(r['text'])}</td><td>{'已转发' if r['forwarded'] else '未转发'}</td></tr>"
            for r in rows
        ) or '<tr><td colspan="5">暂无记录</td></tr>'
        return f'<h2>{html.escape(title)}</h2><table><tr><th>ID</th><th>时间</th><th>号码</th><th>内容</th><th>邮件</th></tr>{body}</table>'

    def table_outbound(self, rows, title: str = "发送列表") -> str:
        body = ""
        detail_modals = ""
        for r in rows:
            cancel = ""
            if r["status"] in ("queued", "retry_wait"):
                if r["send_type"] != "plan":
                    cancel = f'<form class="inline" method="post" action="/outbound/{r["id"]}/cancel"><button class="danger">取消</button></form>'
            plan_text = f"#{r['plan_id']}" if r["plan_id"] else "-"
            detail_id = f"outbound-detail-{r['id']}"
            detail_button = f'<button class="muted" type="button" onclick="document.getElementById(\'{detail_id}\').showModal()">详情</button>'
            scheduled_display = format_admin_minute_time(r["scheduled_at"])
            created_display = format_admin_time(r["created_at"])
            actions = f'<div class="table-actions">{detail_button}{cancel}</div>'
            body += (
                f"<tr><td>#{r['id']}</td><td>{send_type_badge(r['send_type'])}</td>"
                f"<td>{plan_text}</td><td>{html.escape(created_display)}</td><td>{html.escape(r['destination'])}</td>"
                f"<td>{html.escape(r['strategy_name'] or '-')}</td><td>{status_badge(r['status'])}</td>"
                f"<td>{r['attempts']}/{r['max_retries'] + 1}</td>"
                f"<td class=\"msg\">{html.escape(r['text'])}</td><td>{actions}</td></tr>"
            )
            detail_modals += f"""
            <dialog class="modal" id="{detail_id}">
              <div class="modal-head"><h2>发送详情 #{r['id']}</h2><form method="dialog"><button class="muted">关闭</button></form></div>
              <div class="modal-body">
                <div class="detail-grid">
                  <b>ID</b><span>#{r['id']}</span>
                  <b>类型</b><span>{send_type_badge(r['send_type'])}</span>
                  <b>计划ID</b><span>{html.escape(plan_text)}</span>
                  <b>创建</b><span>{html.escape(created_display)}</span>
                  <b>计划发送时间</b><span>{html.escape(scheduled_display)}</span>
                  <b>目标</b><span>{html.escape(r['destination'])}</span>
                  <b>策略</b><span>{html.escape(r['strategy_name'] or '-')}</span>
                  <b>状态</b><span>{status_badge(r['status'])}</span>
                  <b>尝试</b><span>{r['attempts']}/{r['max_retries'] + 1}</span>
                  <b>短信内容</b><span class="msg">{html.escape(r['text'])}</span>
                  <b>错误</b><span class="msg">{html.escape(r['last_error'] or '-')}</span>
                </div>
              </div>
            </dialog>
            """
        body = body or '<tr><td colspan="10">暂无记录</td></tr>'
        return (
            f'<h2>{html.escape(title)}</h2><table><tr><th>ID</th><th>类型</th><th>计划ID</th><th>创建</th><th>目标</th>'
            '<th>策略</th><th>状态</th><th>尝试</th><th>内容</th><th>操作</th></tr>'
            f"{body}</table>{detail_modals}"
        )

    def render_inbound(self) -> bytes:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        sender = query.get("sender", [""])[0].strip()
        keyword = query.get("keyword", [""])[0].strip()
        forwarded = query.get("forwarded", [""])[0].strip()
        page_num = parse_positive_int(query.get("page", ["1"])[0], 1, 1, 999999)
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
        limit = PAGE_SIZE
        offset = (page_num - 1) * limit
        with db() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM inbound_sms {where}", args).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM inbound_sms {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
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
        base_query = {}
        if sender:
            base_query["sender"] = sender
        if keyword:
            base_query["keyword"] = keyword
        if forwarded in ("0", "1"):
            base_query["forwarded"] = forwarded
        pager = pagination_bar("/inbound", base_query, page_num, total, limit)
        return page("接收列表", filters + self.table_inbound(rows) + pager, "inbound")

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
        limit = PAGE_SIZE
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
        pager = pagination_bar("/outbound", base_query, page_num, total, limit)
        return page("发送列表", filters + self.table_outbound(rows) + pager, "outbound")

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
        page_num = request_page(getattr(self, "path", "/plans"))
        limit = PAGE_SIZE
        offset = (page_num - 1) * limit
        notice = ""
        if error:
            notice = f'<div class="notice">{html.escape(error)}</div>'
        elif saved:
            notice = '<div class="notice">发送计划已保存。</div>'
        strategy_options = strategy_options_html()
        with db() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM send_plans WHERE deleted = 0").fetchone()["c"]
            rows = conn.execute(
                """
                SELECT p.*, o.status AS outbound_status, o.attempts AS outbound_attempts,
                       o.max_retries AS outbound_max_retries, o.last_error AS outbound_last_error,
                       o.id AS current_outbound_id,
                       (SELECT COUNT(*) FROM outbound_sms s
                        WHERE s.plan_id = p.id AND s.status IN ('submitted','sent')) AS success_count,
                       (SELECT COUNT(*) FROM outbound_sms s
                        WHERE s.plan_id = p.id AND s.status IN ('failed','send_timeout','ambiguous')) AS failure_count,
                       (SELECT COUNT(*) FROM outbound_sms s
                        WHERE s.plan_id = p.id
                          AND (s.attempts > 0 OR s.status IN ('submitted','sent','failed','send_timeout','ambiguous','sending'))) AS history_count
                FROM send_plans p
                LEFT JOIN outbound_sms o ON o.id = p.outbound_id
                WHERE p.deleted = 0
                ORDER BY p.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        plan_rows = ""
        detail_modals = ""
        edit_modals = ""
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
            next_time_display = "-" if next_time == "-" else format_admin_minute_time(next_time)
            if r["archived"]:
                action = ""
            elif int(r["history_count"] or 0) > 0:
                action = (
                    f'<form class="inline" method="post" action="/plans/{r["id"]}/archive"{confirm_submit_attr("确认归档发送计划？")}>'
                    '<button class="secondary">归档</button></form>'
                )
            else:
                action = (
                    f'<form class="inline" method="post" action="/plans/{r["id"]}/delete"{confirm_submit_attr("确认删除发送计划？")}>'
                    '<button class="danger">删除</button></form>'
                )
            detail_id = f"plan-detail-{r['id']}"
            edit_id = f"plan-edit-{r['id']}"
            detail_button = f'<button class="muted" type="button" onclick="document.getElementById(\'{detail_id}\').showModal()">详情</button>'
            edit_button = ""
            if not r["archived"]:
                edit_button = f'<button class="secondary" type="button" onclick="document.getElementById(\'{edit_id}\').showModal()">编辑</button>'
            send_counts = (
                f'<span class="send-counts"><span class="count-pill failure">{int(r["failure_count"] or 0)}</span>'
                f'<span class="send-count-separator">/</span>'
                f'<span class="count-pill success">{int(r["success_count"] or 0)}</span></span>'
            )
            edit_country_code = row_value(r, "country_code") or ""
            edit_phone_number = row_value(r, "phone_number") or ""
            if not edit_country_code or not edit_phone_number:
                fallback_country_code, fallback_phone_number = split_destination_for_form(r["destination"])
                edit_country_code = edit_country_code or fallback_country_code
                edit_phone_number = edit_phone_number or fallback_phone_number
            plan_rows += (
                f"<tr><td>#{r['id']}</td><td>{html.escape(r['name'])}</td><td>{html.escape(next_time_display)}</td>"
                f"<td>{html.escape(r['destination'])}</td><td>{html.escape(r['strategy_name'])}</td>"
                f"<td>{plan_status}</td>"
                f"<td>{send_counts}</td>"
                f"<td><div class=\"table-actions\">{detail_button}{edit_button}{action}</div></td></tr>"
            )
            if not r["archived"]:
                edit_modals += f"""
                <dialog class="modal" id="{edit_id}">
                  <div class="modal-head"><h2>编辑发送计划 #{r['id']}</h2></div>
                  <form class="plan-edit" method="post" action="/plans/{r['id']}/update">
                    <div class="modal-body">
                      <div class="row phone">
                        <div><label>区号</label><input name="country_code" value="{html.escape(edit_country_code)}" inputmode="tel" maxlength="6" required></div>
                        <div><label>目标号码</label><input name="destination" value="{html.escape(edit_phone_number)}" inputmode="tel" required></div>
                      </div>
                      <label>短信内容</label>
                      <textarea name="text" maxlength="1000" required>{html.escape(r['text'])}</textarea>
                      <div class="actions modal-actions">
                        <button class="muted" type="button" onclick="this.closest('dialog').close()">关闭</button>
                        <button>保存修改</button>
                      </div>
                    </div>
                  </form>
                </dialog>
                """
            detail_modals += f"""
            <dialog class="modal" id="{detail_id}">
              <div class="modal-head"><h2>计划详情 #{r['id']}</h2><form method="dialog"><button class="muted">关闭</button></form></div>
              <div class="modal-body">
                <div class="detail-grid">
                  <b>ID</b><span>#{r['id']}</span>
                  <b>名称</b><span>{html.escape(r['name'])}</span>
                  <b>目标</b><span>{html.escape(r['destination'])}</span>
                  <b>策略</b><span>{html.escape(r['strategy_name'])}</span>
                  <b>下次发送</b><span>{html.escape(next_time_display)}</span>
                  <b>当前发送ID</b><span>#{html.escape(str(r['current_outbound_id'] or '-'))}</span>
                  <b>状态</b><span>{plan_status}</span>
                  <b>发送次数</b><span>{send_counts}</span>
                  <b>短信内容</b><span class="msg">{html.escape(r['text'])}</span>
                  <b>最近错误</b><span class="msg">{html.escape(r['outbound_last_error'] or '-')}</span>
                </div>
              </div>
            </dialog>
            """
        plan_rows = plan_rows or '<tr><td colspan="8">暂无计划</td></tr>'
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
        pager = pagination_bar("/plans", {}, page_num, total, limit)
        body = f"""
        {notice}
        <div class="toolbar"><h2>发送计划</h2><button type="button" onclick="document.getElementById('plan-create').showModal()">新增发送计划</button></div>
        {create_modal}
        <table><tr><th>ID</th><th>名称</th><th>下次发送</th><th>目标</th><th>策略</th><th>状态</th><th>发送次数</th><th>操作</th></tr>{plan_rows}</table>
        {pager}
        {detail_modals}
        {edit_modals}
        """
        return page("发送计划", body, "plans")

    def handle_plan(self) -> None:
        form = self.read_form()
        try:
            if form.get("confirm") != "yes":
                raise ValueError("请先勾选同意费用确认")
            country_code, phone_number, destination = destination_parts_from_form(form)
            text = form.get("text", "").strip()
            if not text:
                raise ValueError("短信内容不能为空")
            segments, encoding = estimate_segments(text)
            if segments > 10:
                raise ValueError(f"短信预计 {segments} 段，超过后台单次 10 段限制")
            scheduled_at = optional_future_time_from_form(form.get("scheduled_at", ""))
            strategy_id = strategy_from_form(form.get("strategy_id", ""))
            name = form.get("name", "").strip() or f"{destination} {scheduled_at}"
            create_send_plan(name[:40], destination, text, scheduled_at, strategy_id, country_code, phone_number)
        except Exception as exc:
            self.send_html(self.render_plans(str(exc)), 400)
            return
        self.redirect("/plans")

    def handle_plan_update(self, path: str) -> None:
        try:
            plan_id = int(path.split("/")[2])
        except Exception:
            self.send_error(400)
            return
        form = self.read_form()
        try:
            country_code, phone_number, destination = destination_parts_from_form(form)
            text = form.get("text", "").strip()
            update_send_plan(plan_id, destination, text, country_code, phone_number)
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

        def strategy_form_fields(row=None) -> str:
            if row:
                strategy_id = f'<input type="hidden" name="strategy_id" value="{row["id"]}">'
                name = html.escape(row["name"])
                send_interval = int(row["send_interval_minutes"])
                retry_interval = int(row["retry_interval_minutes"])
                max_retries = int(row["max_retries"])
                active_checked = " checked" if row["active"] else ""
                default_checked = " checked" if row["is_default"] else ""
            else:
                strategy_id = ""
                name = ""
                send_interval = int(DEFAULT_STRATEGY["send_interval_minutes"])
                retry_interval = int(DEFAULT_STRATEGY["retry_interval_minutes"])
                max_retries = int(DEFAULT_STRATEGY["max_retries"])
                active_checked = " checked"
                default_checked = ""
            return f"""
                {strategy_id}
                <label>策略名称</label>
                <input name="name" maxlength="40" value="{name}" required>
                <div class="strategy-interval-field">{strategy_interval_control(send_interval)}</div>
                <div class="row">
                  <div><label>失败重试间隔（分钟）</label><input name="retry_interval_minutes" type="number" min="1" max="{RETRY_INTERVAL_MAX_MINUTES}" value="{retry_interval}"></div>
                  <div><label>最大重试次数</label><input name="max_retries" type="number" min="0" max="10" value="{max_retries}"></div>
                </div>
                <div class="checkline">
                  <label><input type="checkbox" name="active" value="yes"{active_checked}>启用</label>
                  <label><input type="checkbox" name="is_default" value="yes"{default_checked}>默认策略</label>
                </div>
            """

        strategy_rows = ""
        edit_modals = ""
        for row in list_strategies(active_only=False):
            edit_id = f"strategy-edit-{row['id']}"
            active_badge = '<span class="badge ok">启用</span>' if row["active"] else '<span class="badge muted">停用</span>'
            default_badge = '<span class="badge info">默认</span>' if row["is_default"] else "-"
            edit_button = f'<button class="secondary" type="button" onclick="document.getElementById(\'{edit_id}\').showModal()">编辑</button>'
            if row["is_default"]:
                delete_button = ""
            else:
                delete_button = (
                    f'<form class="inline" method="post" action="/settings/{row["id"]}/delete"{confirm_submit_attr("确认删除发送策略？")}>'
                    '<button class="danger">删除</button></form>'
                )
            strategy_rows += (
                f"<tr><td>#{row['id']}</td><td>{html.escape(row['name'])}</td>"
                f"<td>{html.escape(format_send_interval_minutes(row['send_interval_minutes']))}</td>"
                f"<td>{int(row['retry_interval_minutes'])} 分钟</td>"
                f"<td>{int(row['max_retries'])}</td><td>{active_badge}</td><td>{default_badge}</td>"
                f"<td><div class=\"table-actions\">{edit_button}{delete_button}</div></td></tr>"
            )
            edit_modals += f"""
            <dialog class="modal" id="{edit_id}">
              <div class="modal-head"><h2>编辑发送策略 #{row['id']}</h2></div>
              <form method="post" action="/settings">
                <div class="modal-body">
                  {strategy_form_fields(row)}
                  <div class="actions modal-actions">
                    <button class="muted" type="button" onclick="this.closest('dialog').close()">关闭</button>
                    <button>保存修改</button>
                  </div>
                </div>
              </form>
            </dialog>
            """
        create_modal = f"""
        <dialog class="modal" id="strategy-create">
          <div class="modal-head"><h2>新增发送策略</h2><form method="dialog"><button class="muted">关闭</button></form></div>
          <form method="post" action="/settings">
            <div class="modal-body">
              {strategy_form_fields()}
              <div class="actions modal-actions"><button>新增策略</button></div>
            </div>
          </form>
        </dialog>
        """
        body = f"""
        <div class="notice">保守策略：发送间隔和失败重试间隔都按分钟使用；只有 gammu-smsd-inject 明确返回失败才会重试，超时/状态不明不会重试。</div>
        {notice}
        <div class="toolbar"><h2>发送策略</h2><button type="button" onclick="document.getElementById('strategy-create').showModal()">新增发送策略</button></div>
        {create_modal}
        <table><tr><th>ID</th><th>名称</th><th>发送间隔</th><th>失败重试</th><th>最大重试</th><th>状态</th><th>默认</th><th>操作</th></tr>{strategy_rows}</table>
        {edit_modals}
        """
        return page("发送策略", body, "settings")

    def render_mail_settings(self, saved: str = "", error: str = "") -> bytes:
        notice = ""
        if error:
            notice = f'<div class="notice">{html.escape(error)}</div>'
        elif saved == "test":
            notice = '<div class="notice">测试邮件已发送，请检查收件箱。</div>'
        elif saved:
            notice = '<div class="notice">邮件配置已保存。</div>'
        mail = get_mail_settings()
        mail_enabled_checked = " checked" if mail["mail_enabled"] == "1" else ""
        security_options = "".join(
            f'<option value="{value}"{" selected" if mail["smtp_security"] == value else ""}>{label}</option>'
            for value, label in (("ssl", "SSL/TLS"), ("starttls", "STARTTLS"), ("none", "不加密"))
        )
        body = f"""
        {notice}
        <form class="panel wide" method="post" action="/mail">
          <h2>邮件配置</h2>
          <div class="checkline">
            <label><input type="checkbox" name="mail_enabled" value="yes"{mail_enabled_checked}>启用邮件通知</label>
          </div>
          <div class="row">
            <div><label>收件人</label><input name="mail_to" type="email" value="{html.escape(mail['mail_to'])}"></div>
            <div><label>发件人</label><input name="mail_from" type="email" value="{html.escape(mail['mail_from'])}"></div>
          </div>
          <div class="row three">
            <div><label>SMTP 主机</label><input name="smtp_host" value="{html.escape(mail['smtp_host'])}"></div>
            <div><label>SMTP 端口</label><input name="smtp_port" type="number" min="1" max="65535" value="{html.escape(mail['smtp_port'])}"></div>
            <div><label>加密方式</label><select name="smtp_security">{security_options}</select></div>
          </div>
          <div class="row">
            <div><label>SMTP 用户名</label><input name="smtp_user" value="{html.escape(mail['smtp_user'])}"></div>
            <div><label>SMTP 密码</label><input name="smtp_password" type="password" autocomplete="new-password" placeholder="留空保持不变"></div>
          </div>
          <div class="hint">邮件配置保存在数据库中。计划短信发送成功、发送失败、入站短信转发都会读取这里的配置。</div>
          <div class="actions form-actions-right">
            <button class="secondary" type="submit" formaction="/mail/test">发送测试邮件</button>
            <button>保存邮件配置</button>
          </div>
        </form>
        """
        return page("邮件配置", body, "mail")

    def handle_settings(self) -> None:
        form = self.read_form()
        try:
            save_strategy_from_form(form)
        except Exception as exc:
            self.send_html(self.render_settings(error=str(exc)), 400)
            return
        self.send_html(self.render_settings("1"))

    def handle_mail_settings(self) -> None:
        form = self.read_form()
        try:
            save_mail_settings(form)
        except Exception as exc:
            self.send_html(self.render_mail_settings(error=str(exc)), 400)
            return
        self.send_html(self.render_mail_settings("1"))

    def handle_mail_test(self) -> None:
        form = self.read_form()
        try:
            save_mail_settings(form)
            ok, err = send_test_mail()
            if not ok:
                raise ValueError(f"测试邮件发送失败：{err}")
        except Exception as exc:
            self.send_html(self.render_mail_settings(error=str(exc)), 400)
            return
        self.send_html(self.render_mail_settings("test"))

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
        request_cancel_outbound(item_id)
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
    start_inbound_notification_listener(stop_event)
    host = cfg.get("BIND_HOST", "0.0.0.0")
    port = int(cfg.get("PORT", "8088"))
    server = ThreadingHTTPServer((host, port), AdminHandler)
    server.cfg = cfg  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    finally:
        stop_event.set()
