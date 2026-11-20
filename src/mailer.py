"""메일 발송.

- 모드 (.env MAIL_MODE_OPERATOR / MAIL_MODE_CUSTOMER): 정확히 'live' 일 때만 실제 발송,
  그 외(기본)는 dry-run: 발송하지 않고 .eml 파일로 저장.
- 방식 (.env MAIL_TRANSPORT):
    smtp    (기본) SMTP 서버에 로그인해 발송. 접속 정보는 .env 의 SMTP_*
    outlook 이 PC 의 클래식 Outlook 을 COM 으로 조작해 발송. SMTP 로그인 불필요,
            Outlook 의 해당 계정으로 나가고 보낸 편지함에 남는다.
"""
from __future__ import annotations

import mimetypes
import smtplib
import ssl
import tempfile
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from .config import env, now

KINDS = {"review": "MAIL_MODE_OPERATOR", "notify": "MAIL_MODE_OPERATOR", "customer": "MAIL_MODE_CUSTOMER"}

OL_MAIL_ITEM = 0
OL_FOLDER_OUTBOX = 4
OL_SEND_USING_ACCOUNT_DISPID = 64209


def mail_mode(kind: str) -> str:
    return "live" if env(KINDS[kind]).lower() == "live" else "dryrun"


def transport() -> str:
    return "outlook" if env("MAIL_TRANSPORT").lower() == "outlook" else "smtp"


def smtp_configured() -> bool:
    return bool(env("SMTP_HOST") and env("SMTP_USER") and env("SMTP_PASSWORD"))


def build_message(*, sender: str, sender_name: str, to: list[str], cc: list[str] | None,
                  subject: str, body: str, html: str | None = None,
                  attachments: list[Path] | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(sender.split("@")[-1] if "@" in sender else None))
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for p in attachments or []:
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def _addrs(msg: EmailMessage, header: str) -> list[str]:
    h = msg[header]
    return [a.addr_spec for a in h.addresses] if h is not None else []


def _send_smtp(msg: EmailMessage) -> None:
    if not smtp_configured():
        raise RuntimeError("SMTP_HOST/SMTP_USER/SMTP_PASSWORD 가 .env 에 없습니다")
    host, port = env("SMTP_HOST"), int(env("SMTP_PORT", "587") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            s.send_message(msg)


def _outlook_app():
    """(Outlook Application, 우리가 새로 띄웠는지). 이미 실행 중인 클래식 Outlook 이 있으면 그것을 쓴다."""
    import win32com.client

    try:
        return win32com.client.GetActiveObject("Outlook.Application"), False
    except Exception:  # noqa: BLE001 - 실행 중이 아님
        return win32com.client.DispatchEx("Outlook.Application"), True


def _send_outlook(msg: EmailMessage, wait_sec: int = 90) -> None:
    """클래식 Outlook 으로 발송. From 주소와 일치하는 Outlook 계정으로 보낸다.

    Send() 후 '보낼 편지함'에서 빠질 때까지 기다린다. 시간 안에 안 빠지면 오류로 처리하되
    Outlook 은 끄지 않는다(나중에 전송될 수 있으므로 재시도 전에 보낸 편지함/보낼 편지함 확인 필요).
    """
    import pythoncom

    frm = (_addrs(msg, "From") or [""])[0]
    to, cc = _addrs(msg, "To"), _addrs(msg, "Cc")
    body_part = msg.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part is not None else ""

    pythoncom.CoInitialize()
    started, app, ok = False, None, False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for part in msg.iter_attachments():
                p = Path(tmp) / (part.get_filename() or "attachment.bin")
                p.write_bytes(part.get_payload(decode=True))
                files.append(p)

            app, started = _outlook_app()
            ns = app.GetNamespace("MAPI")
            accounts = [a for a in ns.Accounts]
            account = next((a for a in accounts if (a.SmtpAddress or "").lower() == frm.lower()), None)
            if account is None:
                have = ", ".join(a.SmtpAddress for a in accounts) or "(없음)"
                raise RuntimeError(f"클래식 Outlook 에 '{frm}' 계정이 없습니다. 설정된 계정: {have}")

            outbox = ns.GetDefaultFolder(OL_FOLDER_OUTBOX)
            before = outbox.Items.Count
            item = app.CreateItem(OL_MAIL_ITEM)
            item.To = "; ".join(to)
            if cc:
                item.CC = "; ".join(cc)
            item.Subject = str(msg["Subject"])
            item.Body = body
            for p in files:
                item.Attachments.Add(str(p))
            try:
                item._oleobj_.Invoke(OL_SEND_USING_ACCOUNT_DISPID, 0, 8, 0, account)
            except Exception:  # noqa: BLE001 - late binding 이 아닌 경우
                item.SendUsingAccount = account
            item.Send()

            deadline = time.time() + wait_sec
            while outbox.Items.Count > before and time.time() < deadline:
                time.sleep(1)
            if outbox.Items.Count > before:
                raise RuntimeError("Outlook 보낼 편지함에서 나가지 않았습니다. Outlook 이 오프라인이거나 "
                                   "대화상자가 떠 있는지 확인하세요. (나중에 전송될 수 있으니 재시도 전에 보낸 편지함을 확인)")
            ok = True
    finally:
        if app is not None and started and ok:
            try:
                app.Quit()
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()


def deliver(msg: EmailMessage, kind: str, outbox: Path, live: bool = False) -> tuple[str, str]:
    """(mode, 사본 경로). dry-run 은 .eml 저장만, live 는 선택한 방식으로 발송 후에도 사본 저장.

    live=True 는 운영자가 화면에서 직접 '발송'을 눌렀을 때: .env 의 모드와 무관하게 실제 발송.
    (자동 스케줄러는 live 를 넘기지 않으므로 .env 의 MAIL_MODE_* 를 따른다.)
    """
    mode = "live" if (live or mail_mode(kind) == "live") else "dryrun"
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f'{now():%Y%m%d_%H%M%S}_{kind}_{mode}.eml'
    path.write_bytes(bytes(msg))
    if mode != "live":
        return mode, str(path)
    if transport() == "outlook":
        _send_outlook(msg)
    else:
        _send_smtp(msg)
    return mode, str(path)
