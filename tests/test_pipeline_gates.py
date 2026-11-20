"""승인/발송 안전장치 테스트. 실제 NCP/SMTP 호출 없음 (임시 DB·폴더)."""
from pathlib import Path
from unittest import mock

import pytest

from src import config, db, mailer, pipeline

RM = "2026-10"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db.set_db_path(tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(config, "customer_dir", lambda cid: tmp_path / "reports" / cid)
    monkeypatch.setenv("MAIL_MODE_OPERATOR", "dryrun")
    monkeypatch.setenv("MAIL_MODE_CUSTOMER", "dryrun")
    db.set_global("mail_from", "noreply@example.com")
    db.save_settings("skmns", operator_emails="op@example.com", customer_to="cust@example.com")
    d = config.month_dir("skmns", RM, "draft")
    pptx, pdf = d / "r.pptx", d / "r.pdf"
    pptx.write_bytes(b"pptx")
    pdf.write_bytes(b"%PDF-1.4 test")
    db.update_run("skmns", RM, status="draft", draft_at="2026-11-19 09:00:00",
                  pptx_path=str(pptx), pdf_path=str(pdf), issues_json="[]")
    yield tmp_path
    db.set_db_path(None)


def test_send_requires_approval(env):
    with pytest.raises(pipeline.PipelineError, match="미승인"):
        pipeline.send_customer("skmns", RM)


def test_dryrun_send_saves_eml_and_does_not_mark_sent(env):
    pipeline.approve("skmns", RM, "tester")
    with mock.patch("smtplib.SMTP") as smtp, mock.patch("smtplib.SMTP_SSL") as smtps:
        assert pipeline.send_customer("skmns", RM) == "dryrun"
        smtp.assert_not_called()
        smtps.assert_not_called()
    assert db.get_run("skmns", RM)["status"] == "approved"
    assert list(config.month_dir("skmns", RM, "outbox").glob("*_customer_dryrun.eml"))


def test_live_send_marks_sent_and_blocks_duplicate(env, monkeypatch):
    pipeline.approve("skmns", RM, "tester")
    monkeypatch.setenv("MAIL_MODE_CUSTOMER", "live")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    with mock.patch("smtplib.SMTP") as smtp:
        assert pipeline.send_customer("skmns", RM) == "live"
        assert smtp.return_value.__enter__.return_value.send_message.call_count == 1
        with pytest.raises(pipeline.PipelineError, match="이미 발송"):
            pipeline.send_customer("skmns", RM)
        assert smtp.return_value.__enter__.return_value.send_message.call_count == 1
    assert db.get_run("skmns", RM)["status"] == "sent"


def test_input_change_after_approval_blocks_send(env):
    pipeline.approve("skmns", RM, "tester")
    db.add_support("skmns", RM, "2026-10-05", "장애대응", "x", "완료")
    assert any("변경" in p for p in pipeline.check_sendable("skmns", RM))
    with pytest.raises(pipeline.PipelineError):
        pipeline.send_customer("skmns", RM)


def test_tampered_approved_pdf_blocks_send(env):
    pipeline.approve("skmns", RM, "tester")
    Path(db.get_run("skmns", RM)["approved_pdf"]).write_bytes(b"changed")
    assert any("승인 후 변경" in p for p in pipeline.check_sendable("skmns", RM))


def _live(monkeypatch):
    monkeypatch.setenv("MAIL_MODE_CUSTOMER", "live")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")


def test_direct_send_dryrun_approves_without_review_mail(env):
    with mock.patch("smtplib.SMTP") as smtp:
        assert pipeline.approve_and_send("skmns", RM, "tester") == "dryrun"
        smtp.assert_not_called()
    run = db.get_run("skmns", RM)
    assert run["status"] == "approved" and run["approved_by"] == "tester"
    assert not run.get("review_mail_at")  # 점검 메일 없이 진행


def test_direct_send_live_sends_once(env, monkeypatch):
    _live(monkeypatch)
    with mock.patch("smtplib.SMTP") as smtp:
        assert pipeline.approve_and_send("skmns", RM, "tester") == "live"
        send = smtp.return_value.__enter__.return_value.send_message
        assert send.call_count == 1
        with pytest.raises(pipeline.PipelineError, match="이미 발송"):
            pipeline.approve_and_send("skmns", RM, "tester")
        assert send.call_count == 1
    assert db.get_run("skmns", RM)["status"] == "sent"


def test_direct_send_failure_reverts_approval(env, monkeypatch):
    _live(monkeypatch)
    with mock.patch("smtplib.SMTP") as smtp:
        smtp.return_value.__enter__.return_value.login.side_effect = RuntimeError("auth failed")
        with pytest.raises(RuntimeError):
            pipeline.approve_and_send("skmns", RM, "tester")
    run = db.get_run("skmns", RM)
    assert run["status"] == "draft" and not run["approved_at"]


def test_direct_send_blocked_without_recipient_or_when_stale(env):
    db.save_settings("skmns", customer_to="")
    with pytest.raises(pipeline.PipelineError, match="받는 사람"):
        pipeline.approve_and_send("skmns", RM, "t")
    assert db.get_run("skmns", RM)["status"] == "draft"
    db.save_settings("skmns", customer_to="cust@example.com")
    db.add_support("skmns", RM, "2026-10-05", "장애대응", "x", "완료")
    with pytest.raises(pipeline.PipelineError, match="다시 만들기"):
        pipeline.approve_and_send("skmns", RM, "t")


def test_manual_live_flag_sends_even_when_env_mode_is_dryrun(env, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    assert mailer.mail_mode("customer") == "dryrun"  # .env 는 dry-run
    pipeline.approve("skmns", RM, "t")
    with mock.patch("smtplib.SMTP") as smtp:
        assert pipeline.send_customer("skmns", RM, live=True) == "live"
        assert smtp.return_value.__enter__.return_value.send_message.call_count == 1
    assert db.get_run("skmns", RM)["status"] == "sent"


def test_automatic_path_still_follows_env_mode(env):
    pipeline.approve("skmns", RM, "t")
    with mock.patch("smtplib.SMTP") as smtp:
        assert pipeline.send_customer("skmns", RM) == "dryrun"  # 스케줄러 호출 형태 (live 미지정)
        smtp.assert_not_called()
    assert db.get_run("skmns", RM)["status"] == "approved"


def test_approve_and_send_live_flag(env, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    with mock.patch("smtplib.SMTP") as smtp:
        assert pipeline.approve_and_send("skmns", RM, "t", live=True) == "live"
        assert smtp.return_value.__enter__.return_value.send_message.call_count == 1
    assert db.get_run("skmns", RM)["status"] == "sent"


def test_customer_mail_from_operator_to_customer(env):
    import email
    from email import policy

    db.save_settings("skmns", operator_emails="op@example.com, op2@example.com",
                     customer_to="cust@example.com", customer_cc="cc@example.com")
    pipeline.approve("skmns", RM, "t")
    pipeline.send_customer("skmns", RM)  # dry-run: .eml 저장
    eml = next(config.month_dir("skmns", RM, "outbox").glob("*_customer_dryrun.eml"))
    msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert "op@example.com" in msg["From"] and "op2@example.com" not in msg["From"]
    assert msg["To"] == "cust@example.com" and msg["Cc"] == "cc@example.com"
    assert [a.get_filename() for a in msg.iter_attachments()] == ["r.pdf"]


def test_review_mail_to_operator_not_customer(env):
    import email
    from email import policy

    pipeline.send_review_mail("skmns", RM)
    eml = next(config.month_dir("skmns", RM, "outbox").glob("*_review_*.eml"))
    msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert msg["To"] == "op@example.com" and "cust@example.com" not in str(msg["To"]) + str(msg["Cc"])


def test_customer_mail_blocked_without_operator_address(env):
    db.save_settings("skmns", operator_emails="")
    pipeline.approve("skmns", RM, "t")
    assert any("운영자 이메일" in p for p in pipeline.check_sendable("skmns", RM))
    with pytest.raises(pipeline.PipelineError):
        pipeline.send_customer("skmns", RM)


def test_review_mail_enabled_defaults_on_and_is_saved(env):
    assert int(db.get_settings("skmns")["review_mail_enabled"]) == 1
    db.save_settings("skmns", review_mail_enabled=0)
    assert int(db.get_settings("skmns")["review_mail_enabled"]) == 0


def test_mail_mode_defaults_to_dryrun(monkeypatch):
    for v in ("", "LIVE ", "true", "yes", "1"):
        monkeypatch.setenv("MAIL_MODE_CUSTOMER", v)
        assert mailer.mail_mode("customer") == ("live" if v.strip().lower() == "live" else "dryrun")
    monkeypatch.delenv("MAIL_MODE_CUSTOMER")
    assert mailer.mail_mode("customer") == "dryrun"


def test_review_mail_goes_to_operator_only(env):
    assert pipeline.send_review_mail("skmns", RM) == "dryrun"
    m = db.list_mail("skmns", RM)[0]
    assert m["kind"] == "review" and m["recipients"] == "op@example.com"
