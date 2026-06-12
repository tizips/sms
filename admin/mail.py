def mail_config() -> dict:
    settings = get_mail_settings()
    return {
        "MAIL_ENABLED": settings["mail_enabled"],
        "MAIL_TO": settings["mail_to"],
        "MAIL_FROM": settings["mail_from"],
        "SMTP_HOST": settings["smtp_host"],
        "SMTP_PORT": settings["smtp_port"],
        "SMTP_USER": settings["smtp_user"],
        "SMTP_PASSWORD": settings["smtp_password"],
        "SMTP_SECURITY": settings["smtp_security"],
    }


def send_notification(subject: str, plain: str, html_body: str = "") -> tuple[int, str]:
    cfg = mail_config()
    if cfg.get("MAIL_ENABLED") != "1":
        return 0, "mail is not enabled"
    to_addr = cfg.get("MAIL_TO", "")
    if not to_addr:
        return 0, "MAIL_TO is not configured"
    from_addr = cfg.get("MAIL_FROM", "sms-gateway@localhost")
    host = cfg.get("SMTP_HOST", "")
    if not host:
        return 0, "SMTP_HOST is not configured"
    port = parse_positive_int(cfg.get("SMTP_PORT", "465"), 465, 1, 65535)
    security = cfg.get("SMTP_SECURITY", "ssl")
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(plain, charset="utf-8")
    if html_body:
        msg.add_alternative(html_body, subtype="html", charset="utf-8")
    try:
        context = ssl.create_default_context()
        if security == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                if cfg.get("SMTP_USER"):
                    smtp.login(cfg.get("SMTP_USER", ""), cfg.get("SMTP_PASSWORD", ""))
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if security == "starttls":
                    smtp.starttls(context=context)
                if cfg.get("SMTP_USER"):
                    smtp.login(cfg.get("SMTP_USER", ""), cfg.get("SMTP_PASSWORD", ""))
                smtp.send_message(msg)
        return 1, ""
    except Exception as exc:
        return 0, str(exc)


def send_test_mail() -> tuple[int, str]:
    sent_at = now_text()
    subject = "短信后台邮件配置测试"
    plain = (
        "这是一封短信后台的邮箱配置检测邮件。\n\n"
        f"发送时间: {sent_at}\n\n"
        "如果你收到这封检测邮件，说明当前邮件配置可以正常发送通知。"
    )
    html_body = (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;"
        "background:#f6f7f9;padding:24px;color:#1d2939;\">"
        "<div style=\"max-width:620px;margin:0 auto;background:#fff;border:1px solid #e4e7ec;border-radius:8px;overflow:hidden;\">"
        "<div style=\"background:#2563eb;color:#fff;padding:18px 22px;font-size:20px;font-weight:700;\">邮件配置测试</div>"
        "<div style=\"padding:20px 22px;font-size:14px;line-height:24px;\">"
        "<p>这是一封短信后台的邮箱配置检测邮件。</p>"
        "<p>如果你收到这封检测邮件，说明当前邮件配置可以正常发送通知。</p>"
        f"<p><b>发送时间:</b> {shell_quote(sent_at)}</p>"
        "</div></div></div>"
    )
    return send_notification(subject, plain, html_body)


def notify_plan_success(row: sqlite3.Row) -> None:
    if row["send_type"] != "plan" or not row["plan_id"] or int(row["final_notified"] or 0):
        return
    subject = f"短信发送成功 #{row['id']} - {row['destination']}"
    plain = (
        "计划短信已发送成功。\n\n"
        f"记录 ID: #{row['id']}\n"
        f"计划 ID: #{row['plan_id']}\n"
        f"目标号码: {row['destination']}\n"
        f"提交时间: {row['submitted_at'] or row['updated_at']}\n\n"
        f"短信内容:\n{row['text']}\n"
    )
    html_body = (
        "<div style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;"
        "background:#f6f7f9;padding:24px;color:#1d2939;\">"
        "<div style=\"max-width:620px;margin:0 auto;background:#fff;border:1px solid #e4e7ec;border-radius:8px;overflow:hidden;\">"
        "<div style=\"background:#067647;color:#fff;padding:18px 22px;font-size:20px;font-weight:700;\">短信发送成功</div>"
        "<div style=\"padding:20px 22px;font-size:14px;line-height:24px;\">"
        f"<p><b>记录 ID:</b> #{row['id']}</p>"
        f"<p><b>计划 ID:</b> #{row['plan_id']}</p>"
        f"<p><b>目标号码:</b> {shell_quote(row['destination'])}</p>"
        f"<pre style=\"white-space:pre-wrap;background:#f8fafc;border:1px solid #e4e7ec;border-radius:6px;padding:12px;\">{shell_quote(row['text'])}</pre>"
        "</div></div></div>"
    )
    ok, err = send_notification(subject, plain, html_body)
    with db() as conn:
        conn.execute(
            "UPDATE outbound_sms SET final_notified = ?, command_output = command_output || ? WHERE id = ?",
            (1 if ok else 0, f"\nsuccess_notify_error={err}" if err else "\nsuccess_notified=1", row["id"]),
        )


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


def build_inbound_plain(row: sqlite3.Row) -> str:
    return (
        "新短信通知\n\n"
        f"记录 ID: #{row['id']}\n"
        f"接收时间: {row['received_at']}\n"
        f"发送号码: {row['sender']}\n"
        f"模块标识: {row['phone_id'] or '-'}\n"
        f"消息标识: {row['raw_ids'] or '-'}\n\n"
        "短信内容:\n"
        f"{row['text'] or '(空短信)'}\n"
    )


def build_inbound_html(row: sqlite3.Row) -> str:
    safe_sender = shell_quote(row["sender"] or "unknown")
    safe_time = shell_quote(row["received_at"] or "")
    safe_text = shell_quote(row["text"] or "(空短信)")
    safe_ids = shell_quote(row["raw_ids"] or "-")
    safe_phone = shell_quote(row["phone_id"] or "-")
    preview = shell_quote((row["text"] or "空短信").replace("\n", " ")[:120])
    return f"""<!doctype html>
<html lang="zh-CN">
  <body style="margin:0;padding:0;background:#f4f6f8;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;">来自 {safe_sender}: {preview}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6f8;margin:0;padding:24px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border:1px solid #d9e0e8;border-radius:8px;overflow:hidden;">
            <tr>
              <td style="background:#1f6feb;color:#ffffff;padding:22px 24px;">
                <div style="font-size:13px;line-height:18px;opacity:.9;">SMS Gateway</div>
                <div style="font-size:22px;line-height:30px;font-weight:700;margin-top:4px;">新短信通知</div>
              </td>
            </tr>
            <tr>
              <td style="padding:22px 24px 8px 24px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:14px;line-height:22px;">
                  <tr>
                    <td style="width:92px;color:#667085;padding:7px 0;border-bottom:1px solid #edf1f5;">发送号码</td>
                    <td style="color:#101828;font-weight:700;padding:7px 0;border-bottom:1px solid #edf1f5;word-break:break-all;">{safe_sender}</td>
                  </tr>
                  <tr>
                    <td style="color:#667085;padding:7px 0;border-bottom:1px solid #edf1f5;">接收时间</td>
                    <td style="color:#101828;padding:7px 0;border-bottom:1px solid #edf1f5;">{safe_time}</td>
                  </tr>
                  <tr>
                    <td style="color:#667085;padding:7px 0;border-bottom:1px solid #edf1f5;">记录 ID</td>
                    <td style="color:#101828;padding:7px 0;border-bottom:1px solid #edf1f5;">#{row['id']}</td>
                  </tr>
                  <tr>
                    <td style="color:#667085;padding:7px 0;border-bottom:1px solid #edf1f5;">模块标识</td>
                    <td style="color:#101828;padding:7px 0;border-bottom:1px solid #edf1f5;">{safe_phone}</td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:14px 24px 24px 24px;">
                <div style="font-size:13px;line-height:18px;color:#667085;margin-bottom:8px;">短信内容</div>
                <div style="font-size:18px;line-height:30px;color:#101828;background:#f8fafc;border:1px solid #e4e9f0;border-radius:8px;padding:16px;white-space:pre-wrap;word-break:break-word;">{safe_text}</div>
                <div style="font-size:12px;line-height:18px;color:#98a2b3;margin-top:16px;word-break:break-all;">消息标识: {safe_ids}</div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def notify_inbound_sms(row: sqlite3.Row) -> tuple[int, str]:
    subject = f"新短信提醒 #{row['id']} - {row['sender']}"
    return send_notification(subject, build_inbound_plain(row), build_inbound_html(row))


def update_inbound_forward_status(row_id: int, ok: int, err: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE inbound_sms SET forwarded = ?, forward_error = ? WHERE id = ?",
            (1 if ok else 0, err[:1000], row_id),
        )


def forward_inbound_row(row: sqlite3.Row) -> None:
    ok, err = notify_inbound_sms(row)
    update_inbound_forward_status(int(row["id"]), ok, err)


def forward_inbound_sms_by_id(row_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM inbound_sms WHERE id = ? AND forwarded = 0",
            (row_id,),
        ).fetchone()
    if not row:
        return False
    forward_inbound_row(row)
    return True


def forward_pending_inbound_sms(limit: int = 20) -> None:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM inbound_sms
            WHERE forwarded = 0
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    for row in rows:
        forward_inbound_row(row)
