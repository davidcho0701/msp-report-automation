"""Outlook 발송 방식 테스트. 실제 Outlook 은 실행하지 않는다 (가짜 COM 객체)."""
from pathlib import Path
from unittest import mock

import pytest

from src import mailer


class FakeAttachments:
    def __init__(self, sink):
        self.sink = sink

    def Add(self, path):
        p = Path(path)
        self.sink.append((p.name, p.read_bytes()))  # 발송 시점에 파일이 실제로 존재해야 함


class FakeItem:
    def __init__(self, app):
        self.app = app
        self.attached = []
        self.Attachments = FakeAttachments(self.attached)
        self.To = self.CC = self.Subject = self.Body = ""
        self.SendUsingAccount = None

    def Send(self):
        self.app.sent.append(self)
        self.app.pending = self.app.pending_after_send


class FakeItems:
    def __init__(self, app):
        self.app = app

    @property
    def Count(self):
        return self.app.pending


class FakeFolder:
    def __init__(self, app):
        self.Items = FakeItems(app)


class FakeAccount:
    def __init__(self, addr):
        self.SmtpAddress = addr


class FakeNS:
    def __init__(self, app):
        self.app = app
        self.Accounts = [FakeAccount("sbjo@uws.co.kr")]

    def GetDefaultFolder(self, n):
        assert n == mailer.OL_FOLDER_OUTBOX
        return FakeFolder(self.app)


class FakeApp:
    def __init__(self, pending_after_send=0):
        self.sent, self.pending, self.pending_after_send, self.quit = [], 0, pending_after_send, False

    def GetNamespace(self, name):
        return FakeNS(self)

    def CreateItem(self, kind):
        return FakeItem(self)

    def Quit(self):
        self.quit = True


def _msg(frm="sbjo@uws.co.kr", to=("cust@example.com",), cc=("cc@example.com",)):
    return mailer.build_message(sender=frm, sender_name="조성빈", to=list(to), cc=list(cc),
                                subject="제목 테스트", body="본문 줄1\n줄2", attachments=None)


def _with_pdf(tmp_path):
    pdf = tmp_path / "보고서.pdf"
    pdf.write_bytes(b"%PDF-test")
    return mailer.build_message(sender="sbjo@uws.co.kr", sender_name="", to=["cust@example.com"], cc=[],
                                subject="s", body="b", attachments=[pdf])


def test_outlook_send_uses_matching_account_and_attaches(tmp_path, monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(mailer, "_outlook_app", lambda: (app, True))
    mailer._send_outlook(_with_pdf(tmp_path), wait_sec=2)
    item = app.sent[0]
    assert item.To == "cust@example.com" and item.Subject == "s"
    assert item.SendUsingAccount.SmtpAddress == "sbjo@uws.co.kr"
    assert item.attached == [("보고서.pdf", b"%PDF-test")]
    assert app.quit is True  # 우리가 띄운 Outlook 은 성공 후 종료


def test_outlook_not_quit_when_already_running(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(mailer, "_outlook_app", lambda: (app, False))
    mailer._send_outlook(_msg(), wait_sec=2)
    assert app.quit is False
    assert app.sent[0].CC == "cc@example.com"


def test_outlook_missing_account_raises_without_sending(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(mailer, "_outlook_app", lambda: (app, True))
    with pytest.raises(RuntimeError, match="계정이 없습니다"):
        mailer._send_outlook(_msg(frm="other@uws.co.kr"), wait_sec=2)
    assert app.sent == []


def test_outlook_stuck_in_outbox_raises_and_keeps_outlook_open(monkeypatch):
    app = FakeApp(pending_after_send=1)
    monkeypatch.setattr(mailer, "_outlook_app", lambda: (app, True))
    monkeypatch.setattr(mailer.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="보낼 편지함"):
        mailer._send_outlook(_msg(), wait_sec=0)
    assert app.quit is False


def test_deliver_routes_by_transport(monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_MODE_CUSTOMER", "live")
    monkeypatch.setenv("MAIL_TRANSPORT", "outlook")
    with mock.patch.object(mailer, "_send_outlook") as ol, mock.patch.object(mailer, "_send_smtp") as sm:
        assert mailer.deliver(_msg(), "customer", tmp_path)[0] == "live"
        ol.assert_called_once()
        sm.assert_not_called()
    monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
    with mock.patch.object(mailer, "_send_outlook") as ol, mock.patch.object(mailer, "_send_smtp") as sm:
        mailer.deliver(_msg(), "customer", tmp_path)
        sm.assert_called_once()
        ol.assert_not_called()


def test_dryrun_never_touches_outlook(monkeypatch, tmp_path):
    monkeypatch.setenv("MAIL_MODE_CUSTOMER", "dryrun")
    monkeypatch.setenv("MAIL_TRANSPORT", "outlook")
    with mock.patch.object(mailer, "_send_outlook") as ol:
        assert mailer.deliver(_msg(), "customer", tmp_path)[0] == "dryrun"
        ol.assert_not_called()
