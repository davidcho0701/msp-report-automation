"""보고서 파이프라인: 수집 → 가공/검증 → PPT/PDF → 점검 메일 → 승인 → 고객사 발송.

안전 장치
- 고객사 메일은 '승인' 상태 + 승인 시점 PDF 해시 일치 + 승인 이후 입력 변경 없음 + 미발송일 때만.
- 실제 발송 여부는 mailer 의 모드(.env MAIL_MODE_CUSTOMER=live)로 별도 제어. 기본 dry-run.
- 한 달 보고서는 live 로 한 번만 발송(중복 방지).
"""
from __future__ import annotations

import calendar
import hashlib
import json
import shutil
from pathlib import Path

from . import db, mailer, perf_csv, snapshot, to_pdf
from .collect_cli import build_device_rows, fetch_raw
from .collect_insight import collect_performance, empty_perf_row
from .config import env, get_customer, month_dir
from .render_pptx import render
from .schedule import compute, send_month_for, shift_month
from .validate import validate


class PipelineError(RuntimeError):
    pass


# ---------- 공통 ----------
def period_text(report_month: str) -> tuple[int, int, int, str]:
    y, m = map(int, report_month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return y, m, last, f"{y}년 {m}월 1일 ~ {m}월 {last}일"


def file_stem(ccfg: dict, report_month: str) -> str:
    y, m = map(int, report_month.split("-"))
    return ccfg.get("file_name", "{yy}년 {m}월 월간보고서").format(
        yy=f"{y % 100:02d}", m=m, yyyy=y, mm=f"{m:02d}", yymm=f"{y % 100:02d}{m:02d}")


def data_path(cid: str, report_month: str) -> Path:
    return month_dir(cid, report_month, "draft") / "report_data.json"


def load_data(cid: str, report_month: str) -> dict | None:
    p = data_path(cid, report_month)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def schedule_for(cid: str, send_month: str):
    st = db.get_settings(cid)
    return compute(send_month, int(st["base_day"]), db.get_extra_holidays(), db.get_override(cid, send_month))


def fill(tpl: str, ccfg: dict, report_month: str) -> str:
    y, m, last, period = period_text(report_month)
    try:
        return tpl.format(customer=ccfg["name"], year=y, month=m, period=period, last_day=last)
    except (KeyError, IndexError, ValueError) as e:
        raise PipelineError(f"메일 문구의 치환자 오류: {e} (사용 가능: {{customer}} {{year}} {{month}} {{period}})")


def customer_mail_parts(cid: str, report_month: str) -> dict:
    """고객사 메일 구성. 보내는 사람 = 운영자 이메일(여러 개면 첫 번째)."""
    ccfg, st = get_customer(cid), db.get_settings(cid)
    ops = db.parse_emails(st["operator_emails"])
    return {
        "from": ops[0] if ops else "",
        "to": db.parse_emails(st["customer_to"]),
        "cc": db.parse_emails(st["customer_cc"]),
        "subject": fill(st["subject_tpl"], ccfg, report_month),
        "body": fill(st["body_tpl"], ccfg, report_month),
    }


def sender(cid: str | None = None) -> tuple[str, str]:
    """시스템 메일(점검/알림)의 보내는 사람. Outlook 방식이면 지정이 없을 때 첫 운영자 주소를 쓴다."""
    addr = db.get_global("mail_from")
    if not addr:
        if mailer.transport() == "outlook":  # SMTP_USER 는 Outlook 방식과 무관
            ops = db.parse_emails(db.get_settings(cid)["operator_emails"]) if cid else []
            addr = ops[0] if ops else ""
        else:
            addr = env("SMTP_USER")
    if not addr:
        raise PipelineError("보내는 사람 주소가 없습니다 (전체 설정 > 보내는 사람)")
    return addr, db.get_global("mail_from_name")


# ---------- 1) 초안 ----------
def make_draft(cid: str, report_month: str, recollect: bool = True) -> dict:
    run = db.get_run(cid, report_month)
    if run.get("status") == "sent":
        raise PipelineError("이미 고객사에 발송된 보고서입니다. 재생성할 수 없습니다.")
    ccfg = get_customer(cid)
    y, m, last, period = period_text(report_month)
    prev_data = load_data(cid, report_month)

    if recollect or not prev_data:
        raw = fetch_raw(cid, report_month)
        devices, extra = build_device_rows(raw, ccfg)
        perf, perf_errors = collect_performance(cid, report_month, devices)
        perf_source = "api"
    else:
        devices, extra = prev_data["devices"], prev_data.get("extra_servers", [])
        perf, perf_errors = prev_data["performance"], prev_data.get("perf_errors", [])
        perf_source = prev_data.get("perf_source", "api")

    backup = snapshot.backup_status(cid, report_month, ccfg, db.get_backup_overrides(cid, report_month))
    support = [{"date": e["date"], "kind": e["kind"], "content": e["content"], "result": e["result"]}
               for e in db.list_support(cid, report_month)]

    data = {
        "customer_id": cid, "customer": ccfg["name"], "report_month": report_month,
        "year": y, "month": m, "last_day": last, "period": period,
        "groups": [{"name": g["name"]} for g in ccfg["system_groups"]],
        "devices": devices, "extra_servers": extra,
        "performance": perf, "perf_errors": perf_errors, "perf_source": perf_source,
        "perf_csv_notes": (prev_data or {}).get("perf_csv_notes", []) if perf_source == "csv" else [],
        "backup": backup, "support": support,
        "generated_at": db.ts(),
    }
    issues = validate(data, load_data(cid, shift_month(report_month, -1)), ccfg)
    data["issues"] = issues

    draft = month_dir(cid, report_month, "draft")
    data_path(cid, report_month).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    pptx = render(data, ccfg, draft / f"{file_stem(ccfg, report_month)}.pptx")
    pdf_path = None
    try:
        pdf_path = str(to_pdf.convert(pptx))
    except Exception as e:  # noqa: BLE001
        issues.append({"level": "error", "item": "PDF", "message": f"PDF 변환 실패: {e}"})

    db.update_run(cid, report_month, status="draft", draft_at=db.ts(), pptx_path=str(pptx),
                  pdf_path=pdf_path, issues_json=json.dumps(issues, ensure_ascii=False),
                  perf_source=perf_source, approved_at=None, approved_by=None,
                  approved_pdf=None, approved_sha256=None)
    return data


def apply_perf_csv(cid: str, report_month: str, files: list[tuple[str, bytes]]) -> list[str]:
    """콘솔 CSV 로 성능 값을 채우고 보고서를 다시 만든다 (재수집 없이)."""
    data = load_data(cid, report_month)
    if not data:
        raise PipelineError("먼저 초안을 생성하세요 (장비 정보 필요)")
    perf = data["performance"] or [empty_perf_row(d) for d in data["devices"] if not d.get("missing")]
    notes = []
    for name, content in files:
        parsed, warns = perf_csv.parse_csv(content, report_month)
        notes += [f"{name}: {w}" for w in warns]
        notes += [f"{name}: {a}" for a in perf_csv.apply(perf, parsed)]
    data["performance"], data["perf_source"], data["perf_errors"] = perf, "csv", []
    data["perf_csv_notes"] = notes
    data_path(cid, report_month).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    make_draft(cid, report_month, recollect=False)
    return notes


# ---------- 2) 운영자 점검 메일 ----------
def send_review_mail(cid: str, report_month: str) -> str:
    ccfg, st = get_customer(cid), db.get_settings(cid)
    run = db.get_run(cid, report_month)
    if not run.get("pptx_path"):
        raise PipelineError("초안이 없습니다")
    ops = db.parse_emails(st["operator_emails"])
    if not ops:
        raise PipelineError("운영자 이메일이 설정되지 않았습니다 (고객사 설정)")
    sc = schedule_for(cid, send_month_for(report_month))
    issues = json.loads(run.get("issues_json") or "[]")
    try:
        cm = customer_mail_parts(cid, report_month)
    except (ValueError, PipelineError) as e:
        cm = {"to": [], "cc": [], "subject": f"(오류: {e})", "body": ""}

    icon = {"error": "[오류]", "warn": "[주의]", "info": "[참고]"}
    lines = [
        f"{ccfg['name']} {report_month} 월간 보고서 초안이 생성되었습니다. 검토 후 승인해 주세요.",
        "",
        f"■ 발송 예정: {sc.send_date} {st['send_time']} ({'수동 지정' if sc.manual else '자동'}; {sc.auto_reason})",
        f"■ 승인 화면: {db.get_global('app_url')}/c/{cid}/{sc.send_month}",
        "",
        "■ 운영자 점검 요약 (고객사 메일에는 포함되지 않음)",
        *([f"  {icon.get(i['level'], '')} {i['item']}: {i['message']}" for i in issues] or ["  이상 없음"]),
        "",
        "■ 고객사 메일 미리보기",
        f"  받는 사람: {', '.join(cm['to']) or '(미설정)'}",
        f"  참조: {', '.join(cm['cc']) or '-'}",
        f"  제목: {cm['subject']}",
        "  ---------------------------------------------",
        *[f"  {line}" for line in cm["body"].splitlines()],
        "  ---------------------------------------------",
        f"  첨부: {Path(run['pdf_path']).name if run.get('pdf_path') else '(PDF 없음)'}",
    ]
    atts = [Path(p) for p in (run.get("pptx_path"), run.get("pdf_path")) if p and Path(p).exists()]
    frm, frm_name = sender(cid)
    subject = f"[점검요청] {ccfg['name']} {report_month} 월간 보고서 (발송예정 {sc.send_date})"
    msg = mailer.build_message(sender=frm, sender_name=frm_name, to=ops, cc=None,
                               subject=subject, body="\n".join(lines), attachments=atts)
    try:
        mode, path = mailer.deliver(msg, "review", month_dir(cid, report_month, "outbox"))
    except Exception as e:
        db.log_mail(cid, report_month, "review", mailer.mail_mode("review"), ops, subject, "failed", error=str(e))
        raise
    db.log_mail(cid, report_month, "review", mode, ops, subject, "sent" if mode == "live" else "saved", path)
    db.update_run(cid, report_month, review_mail_at=db.ts())
    return mode


def notify_operator(cid: str, report_month: str, subject: str, body: str) -> str:
    st = db.get_settings(cid)
    ops = db.parse_emails(st["operator_emails"])
    if not ops:
        return f"운영자 이메일 미설정 - 알림 생략: {subject}"
    frm, frm_name = sender(cid)
    msg = mailer.build_message(sender=frm, sender_name=frm_name, to=ops, cc=None, subject=subject, body=body)
    try:
        mode, path = mailer.deliver(msg, "notify", month_dir(cid, report_month, "outbox"))
        db.log_mail(cid, report_month, "notify", mode, ops, subject, "sent" if mode == "live" else "saved", path)
        return f"{mode}: {subject}"
    except Exception as e:  # noqa: BLE001
        db.log_mail(cid, report_month, "notify", mailer.mail_mode("notify"), ops, subject, "failed", error=str(e))
        return f"알림 실패: {e}"


# ---------- 3) 승인 ----------
def approve(cid: str, report_month: str, by: str) -> None:
    run = db.get_run(cid, report_month)
    if run.get("status") == "sent":
        raise PipelineError("이미 발송됨")
    if run.get("status") not in ("draft", "approved") or not run.get("pdf_path"):
        raise PipelineError("승인할 초안(PDF)이 없습니다")
    if db.is_stale(run):
        raise PipelineError("초안 생성 후 지원 내역/백업 결과가 바뀌었습니다. '보고서 다시 만들기' 후 승인하세요.")
    pdf, pptx = Path(run["pdf_path"]), Path(run["pptx_path"])
    if not pdf.exists():
        raise PipelineError("PDF 파일이 없습니다")
    ap = month_dir(cid, report_month, "approved")
    for old in ap.iterdir():
        old.unlink()
    shutil.copy2(pdf, ap / pdf.name)
    shutil.copy2(pptx, ap / pptx.name)
    db.update_run(cid, report_month, status="approved", approved_at=db.ts(), approved_by=by or "운영자",
                  approved_pdf=str(ap / pdf.name), approved_sha256=sha256(ap / pdf.name))


def unapprove(cid: str, report_month: str) -> None:
    run = db.get_run(cid, report_month)
    if run.get("status") == "sent":
        raise PipelineError("이미 발송됨")
    db.update_run(cid, report_month, status="draft" if run.get("draft_at") else "none",
                  approved_at=None, approved_by=None, approved_pdf=None, approved_sha256=None)


# ---------- 4) 고객사 발송 ----------
def check_sendable(cid: str, report_month: str) -> list[str]:
    run = db.get_run(cid, report_month)
    problems = []
    if run.get("status") == "sent" or db.customer_already_sent(cid, report_month):
        problems.append("이미 발송 완료")
    if run.get("status") != "approved":
        problems.append("미승인")
    elif db.is_stale(run):
        problems.append("승인 후 입력(지원 내역 등)이 변경됨 → 재생성/재승인 필요")
    ap = Path(run["approved_pdf"]) if run.get("approved_pdf") else None
    if run.get("status") == "approved":
        if not ap or not ap.exists():
            problems.append("승인본 PDF 없음")
        elif sha256(ap) != run.get("approved_sha256"):
            problems.append("승인본 PDF 가 승인 후 변경됨")
    try:
        cm = customer_mail_parts(cid, report_month)
        if not cm["to"]:
            problems.append("고객사 받는 사람 미설정")
        if not cm["from"]:
            problems.append("운영자 이메일(고객사 메일의 보내는 사람) 미설정")
    except (ValueError, PipelineError) as e:
        problems.append(str(e))
    return problems


def reset_sent(cid: str, report_month: str, by: str = "운영자") -> None:
    """'발송 완료'를 초기화해 같은 달 보고서를 다시 보낼 수 있게 한다 (시험·재발송용).

    - 발송 이력(mail_log)은 지우지 않고 'reset' 기록을 남긴다. 중복 판정은 마지막 초기화 이후만 본다.
    - 승인도 해제된다. 다시 승인해야 발송할 수 있으므로 자동 발송이 곧바로 재발송하지 않는다.
    """
    run = db.get_run(cid, report_month)
    if run.get("status") != "sent" and not db.customer_already_sent(cid, report_month):
        raise PipelineError("발송 완료 상태가 아닙니다")
    db.log_mail(cid, report_month, "reset", "-", [], f"발송 상태 초기화 ({by})", "reset")
    db.update_run(cid, report_month, status="draft" if run.get("draft_at") else "none",
                  sent_at=None, approved_at=None, approved_by=None, approved_pdf=None,
                  approved_sha256=None, unapproved_notified_at=None)
    db.log_job(cid, "reset-sent", "ok", f"{report_month} 발송 상태 초기화 ({by}); 이전 발송 이력은 유지")


def approve_and_send(cid: str, report_month: str, by: str, live: bool = False) -> str:
    """운영자가 화면에서 '승인하고 바로 발송'을 눌렀을 때: 점검 메일/별도 승인 단계 없이 한 번에.

    - 초안(PDF)이 있어야 하고, 초안 이후 입력이 바뀌었으면(stale) 막힌다. 이미 발송했으면 막힌다.
    - 발송이 실패하면 승인을 되돌려 상태를 깨끗하게 둔다.
    - 사람이 누르지 않는 자동 발송은 여전히 '승인된 보고서'만 대상이다 (jobs.tick).
    """
    if db.get_run(cid, report_month).get("status") == "sent" or db.customer_already_sent(cid, report_month):
        raise PipelineError("이미 발송됨")
    approve(cid, report_month, by)
    try:
        return send_customer(cid, report_month, live=live)
    except Exception:
        unapprove(cid, report_month)
        raise


def send_customer(cid: str, report_month: str, live: bool = False) -> str:
    """승인된 보고서를 고객사로 발송. live=True(운영자 수동 발송)면 .env 모드와 무관하게 실제 발송."""
    problems = check_sendable(cid, report_month)
    if problems:
        raise PipelineError("발송 불가: " + "; ".join(problems))
    run = db.get_run(cid, report_month)
    cm = customer_mail_parts(cid, report_month)
    # 고객사 메일은 운영자 이름으로 나간다 (표시 이름: 기본 승인자 이름 → 없으면 발신자 이름)
    frm_name = db.get_global("operator_name") or db.get_global("mail_from_name")
    msg = mailer.build_message(sender=cm["from"], sender_name=frm_name, to=cm["to"], cc=cm["cc"],
                               subject=cm["subject"], body=cm["body"], attachments=[Path(run["approved_pdf"])])
    rcpts = cm["to"] + cm["cc"]
    try:
        mode, path = mailer.deliver(msg, "customer", month_dir(cid, report_month, "outbox"), live=live)
    except Exception as e:
        db.log_mail(cid, report_month, "customer", "live" if live else mailer.mail_mode("customer"),
                    rcpts, cm["subject"], "failed", error=str(e))
        raise
    if mode == "live":
        db.log_mail(cid, report_month, "customer", mode, rcpts, cm["subject"], "sent", path)
        db.update_run(cid, report_month, status="sent", sent_at=db.ts())
    else:
        db.log_mail(cid, report_month, "customer", mode, rcpts, cm["subject"], "saved", path)
    return mode
