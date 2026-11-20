"""MSP 월간 보고서 운영 웹 UI (로컬 전용).

실행:  .venv\\Scripts\\python.exe app.py   → http://127.0.0.1:8000
웹 서버와 함께 자동 스케줄러(스냅샷/점검일 초안·점검메일/발송일 발송)가 동작한다.
"""
from __future__ import annotations

import json
import secrets
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for

from src import db, jobs, mailer, pipeline, schedule
from src.config import env, get_customer, load_config, load_customers, now, project_path

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
app.secret_key = secrets.token_hex(32)
CSRF_TOKEN = secrets.token_hex(32)
CFG = load_config()
ALLOWED_HOSTS = {f"127.0.0.1:{CFG['web']['port']}", f"localhost:{CFG['web']['port']}"}
REPORTS_ROOT = project_path(CFG["paths"]["reports_dir"]).resolve()


@app.before_request
def guard():
    # DNS rebinding / 외부 접근 차단 + CSRF
    if request.host not in ALLOWED_HOSTS:
        abort(403)
    if request.method == "POST" and request.form.get("csrf") != CSRF_TOKEN:
        abort(400, "CSRF token mismatch")


@app.context_processor
def inject():
    return {
        "csrf": CSRF_TOKEN,
        "mode_operator": mailer.mail_mode("review"),
        "mode_customer": mailer.mail_mode("customer"),
        "today": now().date(),
        "fmt_day": schedule.fmt,
    }


def _check_cid(cid: str) -> dict:
    try:
        return get_customer(cid)
    except KeyError:
        abort(404)


def _check_month(m: str) -> str:
    try:
        y, mm = map(int, m.split("-"))
        date(y, mm, 1)
    except ValueError:
        abort(404)
    return f"{y:04d}-{mm:02d}"


def next_action(run: dict, stale: bool, sendable: list[str], st: dict) -> dict:
    """화면 상단 '지금 할 일' 한 가지."""
    s = run.get("status") or "none"
    if s == "sent":
        return {"key": "done", "tone": "ok", "title": "발송 완료", "desc": f"{run.get('sent_at')} 고객사로 발송했습니다."}
    if s == "none" or not run.get("draft_at"):
        return {"key": "make", "tone": "todo", "title": "보고서를 만들어 주세요",
                "desc": "NCP 에서 장비·성능 정보를 가져와 PPT/PDF 초안을 만듭니다. (점검일에 자동 실행)"}
    if stale:
        return {"key": "rebuild", "tone": "todo", "title": "보고서를 다시 만들어 주세요",
                "desc": "지원 내역이나 백업 결과가 바뀌었습니다. 다시 만든 뒤 승인할 수 있습니다."}
    if s == "draft":
        return {"key": "approve", "tone": "todo", "title": "검토 후 승인해 주세요",
                "desc": "아래 '확인 필요' 항목과 지원 내역을 확인하고 승인하면 발송일에 자동 발송됩니다."}
    if sendable:
        return {"key": "blocked", "tone": "warn", "title": "승인됨 · 발송 준비가 덜 됐습니다",
                "desc": " / ".join(sendable)}
    if int(st["auto_send"]):
        return {"key": "wait", "tone": "ok", "title": "승인 완료 · 자동 발송 대기 중",
                "desc": "발송일·시각에 고객사로 자동 발송됩니다."}
    return {"key": "manual", "tone": "warn", "title": "승인 완료 · 수동 발송 필요",
            "desc": "자동 발송이 꺼져 있습니다. '지금 발송'을 눌러 보내세요."}


# ---------------- 대시보드 ----------------
@app.get("/")
def dashboard():
    sm = f"{now():%Y-%m}"
    cards = []
    for cid, c in load_customers().items():
        sc = pipeline.schedule_for(cid, sm)
        run = db.get_run(cid, sc.report_month)
        st = db.get_settings(cid)
        na = next_action(run, db.is_stale(run), pipeline.check_sendable(cid, sc.report_month), st)
        cards.append({"cid": cid, "name": c["name"], "sc": sc, "run": run, "st": st, "na": na,
                      "busy": jobs.running(cid),
                      "issues": json.loads(run.get("issues_json") or "[]")})
    return render_template("dashboard.html", cards=cards, send_month=sm, jobs=db.list_jobs(limit=15))


@app.get("/c/<cid>")
def customer_home(cid):
    _check_cid(cid)
    return redirect(url_for("month_view", cid=cid, sm=f"{now():%Y-%m}"))


# ---------------- 고객사 월 화면 ----------------
@app.get("/c/<cid>/<sm>")
def month_view(cid, sm):
    ccfg = _check_cid(cid)
    sm = _check_month(sm)
    st = db.get_settings(cid)
    sc = pipeline.schedule_for(cid, sm)
    rm = sc.report_month
    run = db.get_run(cid, rm)
    data = pipeline.load_data(cid, rm)
    issues = json.loads(run.get("issues_json") or "[]")
    try:
        cm = pipeline.customer_mail_parts(cid, rm)
        cm_err = None
    except (ValueError, pipeline.PipelineError) as e:
        cm, cm_err = None, str(e)
    sendable = pipeline.check_sendable(cid, rm)
    stale = db.is_stale(run)
    return render_template(
        "month.html", cid=cid, ccfg=ccfg, st=st, sc=sc, rm=rm, sm=sm, run=run, data=data,
        issues=issues, stale=stale, cm=cm, cm_err=cm_err, sendable=sendable,
        na=next_action(run, stale, sendable, st),
        # '바로 발송'은 승인 단계를 건너뛰므로 '미승인'만 문제로 보지 않는다
        sendable_direct=[p for p in sendable if p != "미승인"],
        calendar=schedule.month_calendar(sm, db.get_extra_holidays()),
        support=db.list_support(cid, rm), mails=db.list_mail(cid, rm), jobs=db.list_jobs(cid, 12),
        busy=jobs.running(cid), prev_sm=schedule.shift_month(sm, -1), next_sm=schedule.shift_month(sm, 1),
        files=_files(cid, rm, run),
    )


def _files(cid, rm, run):
    out = []
    for label, key in (("초안 PPTX", "pptx_path"), ("초안 PDF", "pdf_path"), ("승인본 PDF", "approved_pdf")):
        p = run.get(key)
        if p and Path(p).exists():
            out.append({"label": label, "key": key, "name": Path(p).name})
    return out


@app.get("/c/<cid>/<sm>/file/<key>")
def download(cid, sm, key):
    _check_cid(cid)
    rm = schedule.report_month_for(_check_month(sm))
    if key not in ("pptx_path", "pdf_path", "approved_pdf"):
        abort(404)
    p = db.get_run(cid, rm).get(key)
    if not p:
        abort(404)
    p = Path(p).resolve()
    if REPORTS_ROOT not in p.parents or not p.exists():
        abort(404)
    return send_file(p, as_attachment=True, download_name=p.name)


@app.post("/c/<cid>/<sm>/schedule")
def save_schedule(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    if request.form.get("action") == "reset":
        db.clear_override(cid, sm)
        flash("자동 일정으로 되돌렸습니다.", "ok")
        return redirect(url_for("month_view", cid=cid, sm=sm))
    rd, sd = request.form.get("review_date") or None, request.form.get("send_date") or None
    try:
        rdd = date.fromisoformat(rd) if rd else None
        sdd = date.fromisoformat(sd) if sd else None
    except ValueError:
        flash("날짜 형식 오류", "error")
        return redirect(url_for("month_view", cid=cid, sm=sm))
    auto = pipeline.schedule_for(cid, sm)
    eff_send = sdd or auto.auto_send
    eff_review = rdd or (None if sdd else auto.auto_review)
    if eff_review and eff_review > eff_send:
        flash("점검일은 발송일보다 늦을 수 없습니다.", "error")
        return redirect(url_for("month_view", cid=cid, sm=sm))
    db.set_override(cid, sm, rd, sd, request.form.get("note", "").strip())
    flash("일정을 수동으로 지정했습니다.", "ok")
    return redirect(url_for("month_view", cid=cid, sm=sm))


def _start(cid, sm, name, fn, *args, msg="작업을 시작했습니다."):
    if jobs.start_job(cid, name, fn, *args):
        flash(msg, "ok")
    else:
        flash("이미 다른 작업이 진행 중입니다.", "error")
    return redirect(url_for("month_view", cid=cid, sm=sm))


def _sync(cid, sm, fn, *args, ok="완료"):
    try:
        res = fn(*args)
        flash(ok if res is None else f"{ok} ({res})", "ok")
    except (pipeline.PipelineError, ValueError, RuntimeError) as e:
        flash(str(e), "error")
    return redirect(url_for("month_view", cid=cid, sm=sm))


@app.post("/c/<cid>/<sm>/draft")
def draft(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    rm = schedule.report_month_for(sm)
    recollect = request.form.get("recollect") == "1"
    return _start(cid, sm, f"{'collect+draft' if recollect else 'rebuild'} {rm}",
                  pipeline.make_draft, cid, rm, recollect,
                  msg="수집 및 초안 생성을 시작했습니다. (수 분 소요)" if recollect else "보고서를 다시 만드는 중입니다.")


@app.post("/c/<cid>/<sm>/review-mail")
def review_mail(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    rm = schedule.report_month_for(sm)
    return _sync(cid, sm, pipeline.send_review_mail, cid, rm, ok="점검 메일 처리")


@app.post("/c/<cid>/<sm>/approve")
def approve(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    rm = schedule.report_month_for(sm)
    by = request.form.get("by", "").strip() or db.get_global("operator_name") or "운영자"
    return _sync(cid, sm, pipeline.approve, cid, rm, by, ok="승인했습니다")


@app.post("/c/<cid>/<sm>/unapprove")
def unapprove(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    return _sync(cid, sm, pipeline.unapprove, cid, schedule.report_month_for(sm), ok="승인을 취소했습니다")


@app.post("/c/<cid>/<sm>/send")
def send_now(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    rm = schedule.report_month_for(sm)
    if request.form.get("confirm") != "yes":
        abort(400)
    # 운영자가 직접 누른 발송: .env 모드와 무관하게 실제 발송
    return _sync(cid, sm, pipeline.send_customer, cid, rm, True, ok="고객사에 발송했습니다")


@app.post("/c/<cid>/<sm>/send-direct")
def send_direct(cid, sm):
    """점검 메일/별도 승인 없이 승인+발송을 한 번에 (확인창 필수)."""
    _check_cid(cid)
    sm = _check_month(sm)
    rm = schedule.report_month_for(sm)
    if request.form.get("confirm") != "yes":
        abort(400)
    by = request.form.get("by", "").strip() or db.get_global("operator_name") or "운영자(바로 발송)"
    return _sync(cid, sm, pipeline.approve_and_send, cid, rm, by, True, ok="승인 후 고객사에 발송했습니다")


@app.post("/c/<cid>/<sm>/support")
def support_add(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    f = request.form
    if not (f.get("date") and f.get("kind") and f.get("content")):
        flash("날짜/지원종류/작업내용은 필수입니다.", "error")
    else:
        db.add_support(cid, schedule.report_month_for(sm), f["date"], f["kind"].strip(),
                       f["content"].strip(), f.get("result", "").strip() or "완료")
        flash("지원 내역을 추가했습니다. 보고서에 반영하려면 '보고서 다시 만들기'를 누르세요.", "ok")
    return redirect(url_for("month_view", cid=cid, sm=sm) + "#support")


@app.post("/c/<cid>/<sm>/support/<int:eid>/delete")
def support_delete(cid, sm, eid):
    _check_cid(cid)
    db.delete_support(cid, eid)
    flash("삭제했습니다.", "ok")
    return redirect(url_for("month_view", cid=cid, sm=_check_month(sm)) + "#support")


@app.post("/c/<cid>/<sm>/backup")
def backup_override(cid, sm):
    ccfg = _check_cid(cid)
    sm = _check_month(sm)
    rm = schedule.report_month_for(sm)
    for server in ccfg["backup"]["targets"]:
        db.set_backup_override(cid, rm, server, request.form.get(f"result_{server}", ""))
    flash("백업 작업결과를 저장했습니다. 보고서에 반영하려면 '보고서 다시 만들기'를 누르세요.", "ok")
    return redirect(url_for("month_view", cid=cid, sm=sm) + "#backup")


@app.post("/c/<cid>/<sm>/perf-csv")
def perf_csv_upload(cid, sm):
    _check_cid(cid)
    sm = _check_month(sm)
    files = [(f.filename, f.read()) for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("CSV 파일을 선택하세요.", "error")
        return redirect(url_for("month_view", cid=cid, sm=sm))
    return _start(cid, sm, f"perf-csv {schedule.report_month_for(sm)}", pipeline.apply_perf_csv,
                  cid, schedule.report_month_for(sm), files, msg="CSV 반영 및 보고서 재생성을 시작했습니다.")


# ---------------- 고객사 설정 ----------------
@app.route("/c/<cid>/settings", methods=["GET", "POST"])
def customer_settings(cid):
    ccfg = _check_cid(cid)
    if request.method == "POST":
        f = request.form
        try:
            ops = db.parse_emails(f.get("operator_emails", ""))
            to = db.parse_emails(f.get("customer_to", ""))
            cc = db.parse_emails(f.get("customer_cc", ""))
            base_day = int(f.get("base_day", "20"))
            if not 1 <= base_day <= 28:
                raise ValueError("기준일은 1~28 사이")
            for t in (f.get("review_time", ""), f.get("send_time", "")):
                h, m = map(int, t.split(":"))
                if not (0 <= h < 24 and 0 <= m < 60):
                    raise ValueError(f"시각 형식 오류: {t}")
            pipeline.fill(f.get("subject_tpl", ""), ccfg, "2026-01")
            pipeline.fill(f.get("body_tpl", ""), ccfg, "2026-01")
        except (ValueError, pipeline.PipelineError) as e:
            flash(str(e), "error")
            return redirect(url_for("customer_settings", cid=cid))
        db.save_settings(cid, operator_emails="\n".join(ops), customer_to="\n".join(to),
                         customer_cc="\n".join(cc), subject_tpl=f["subject_tpl"].strip(),
                         body_tpl=f["body_tpl"].replace("\r\n", "\n"), base_day=base_day,
                         review_time=f["review_time"], send_time=f["send_time"],
                         auto_send=1 if f.get("auto_send") else 0,
                         review_mail_enabled=1 if f.get("review_mail_enabled") else 0)
        flash("저장했습니다.", "ok")
        return redirect(url_for("customer_settings", cid=cid))
    return render_template("customer_settings.html", cid=cid, ccfg=ccfg, st=db.get_settings(cid),
                           sm=f"{now():%Y-%m}")


# ---------------- 전체 설정 ----------------
@app.route("/settings", methods=["GET", "POST"])
def global_settings():
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "general":
            mf = f.get("mail_from", "").strip()
            if mf:
                try:
                    db.parse_emails(mf)
                except ValueError as e:
                    flash(str(e), "error")
                    return redirect(url_for("global_settings"))
            for k in ("mail_from", "mail_from_name", "operator_name", "app_url"):
                db.set_global(k, f.get(k, "").strip())
            flash("저장했습니다.", "ok")
        elif action == "holiday_add":
            h = db.get_extra_holidays()
            try:
                d = date.fromisoformat(f.get("date", ""))
                h[d.isoformat()] = f.get("name", "").strip() or "휴무"
                db.set_extra_holidays(h)
                flash("휴무일을 추가했습니다.", "ok")
            except ValueError:
                flash("날짜 형식 오류", "error")
        elif action == "holiday_del":
            h = db.get_extra_holidays()
            h.pop(f.get("date", ""), None)
            db.set_extra_holidays(h)
            flash("삭제했습니다.", "ok")
        return redirect(url_for("global_settings"))
    y = now().year
    hol = schedule.kr_holidays([y, y + 1])
    env_status = {"NCLOUD 키 (.env)": bool(env("NCLOUD_ACCESS_KEY") and env("NCLOUD_SECRET_KEY"))}
    if mailer.transport() == "outlook":
        env_status["메일 발송 방식: 이 PC 의 클래식 Outlook (MAIL_TRANSPORT=outlook)"] = True
    else:
        env_status["메일 발송 방식: SMTP (.env)"] = mailer.smtp_configured()
    return render_template("global_settings.html",
                           g={k: db.get_global(k) for k in db.GLOBAL_DEFAULTS},
                           extra=db.get_extra_holidays(), env_status=env_status,
                           kr_holidays=sorted((d for d in hol.items() if d[0] >= now().date() - timedelta(days=31))))


def main():
    db.init_db()
    jobs.start_scheduler()
    app.run(host=CFG["web"]["host"], port=CFG["web"]["port"], debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
