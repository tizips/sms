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
      function formatAdminTime(value) {
        var text = (value || "").trim();
        if (!text) return "-";
        var match = text.match(/^(\\d{4})-(\\d{2})-(\\d{2})[ T](\\d{2}):(\\d{2}):(\\d{2})/);
        if (match) return match[1] + "/" + match[2] + "/" + match[3] + " " + match[4] + ":" + match[5] + ":" + match[6];
        match = text.match(/^(\\d{4})\\/(\\d{2})\\/(\\d{2})[ T](\\d{2}):(\\d{2}):(\\d{2})/);
        if (match) return match[1] + "/" + match[2] + "/" + match[3] + " " + match[4] + ":" + match[5] + ":" + match[6];
        return text;
      }
      function formatSignal(value) {
        var text = (value || "").trim();
        if (!text) return "-";
        if (text.indexOf("%") >= 0 || /dbm|asu|percent/i.test(text)) return text;
        if (/^-?\\d+$/.test(text)) return text + "%";
        return text;
      }
      function pillTone(tone) {
        return ["ok", "bad", "warn", "muted", "info"].indexOf(tone) >= 0 ? tone : "muted";
      }
      function renderSimState(status) {
        var cell = panel.querySelector('[data-sim-field="sim_state"]');
        if (!cell) return;
        var hasError = Boolean(status.error);
        var label = status.sim_state || "未知";
        cell.textContent = "";
        var badge = document.createElement("span");
        badge.setAttribute("data-sim-badge", "");
        badge.className = "status-pill " + pillTone(hasError ? "bad" : status.sim_state_tone);
        badge.textContent = label;
        cell.appendChild(badge);
      }
      function renderOperator(status) {
        var cell = panel.querySelector('[data-sim-field="operator"]');
        if (!cell) return;
        cell.textContent = "";
        var value = document.createElement("span");
        value.setAttribute("data-operator-text", "");
        value.textContent = status.operator || "-";
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
        renderSimState(status);
        setField("sim_identity", status.sim_identity);
        setField("phone_number", status.phone_number);
        setField("sms_counts", status.sms_counts);
        setField("signal", formatSignal(status.signal));
        renderOperator(status);
        setField("checked_at", formatAdminTime(status.checked_at));
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
        ("/mail", "邮件配置", "mail"),
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
    .form-actions-right {{ justify-content:flex-end; }}
    .toolbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin:0 0 14px; }}
    .toolbar.pager {{ margin:14px 0 0; }}
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
    .send-counts {{ display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }}
    .send-count-separator {{ color:var(--muted); font-weight:800; }}
    dialog.modal {{ width:min(760px, calc(100vw - 32px)); border:1px solid var(--line); border-radius:8px; padding:0; color:var(--ink); }}
    dialog.modal::backdrop {{ background:rgba(15,23,42,.45); }}
    .modal-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; padding:16px 18px; border-bottom:1px solid var(--line); }}
    .modal-head h2 {{ margin:0; font-size:18px; }}
    .modal-body {{ padding:18px; }}
    .detail-grid {{ display:grid; grid-template-columns:140px 1fr; gap:8px 12px; font-size:13px; line-height:20px; }}
    .detail-grid b {{ color:#475467; }}
    .strategy-interval-field {{ margin-top:6px; }}
    .modal-actions {{ justify-content:flex-end; }}
    .interval-input {{ display:grid; grid-template-columns:minmax(0,1fr) 118px; gap:8px; }}
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
