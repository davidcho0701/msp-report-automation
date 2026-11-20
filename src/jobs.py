"""백그라운드 작업 실행 + 자동 스케줄러.

스케줄(고객사별, KST):
- 매일 snapshot_time: 서버 이미지 스냅샷 (하루 1회)
- 점검일 review_time 이후: 초안 없으면 생성 → 운영자 점검 메일 (해당 월 1회)
- 발송일 send_time 이후(자동 발송 ON):
    승인 + 발송 가능 → 고객사 발송 (하루 1회 시도)
    아니면 → 운영자에게 '발송 보류' 알림 (하루 1회)
당일이 지나면 자동으로 보충 실행하지 않는다(늦은 발송 방지). UI 에서 수동 처리.
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta

from . import db, pipeline, snapshot
from .config import load_config, load_customers, now

_locks: dict[str, threading.Lock] = {}
_running: dict[str, str] = {}
_guard = threading.Lock()
_last_attempt: dict[tuple, datetime] = {}
RETRY_GAP = timedelta(minutes=30)


def _lock(cid: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(cid, threading.Lock())


def running(cid: str) -> str | None:
    return _running.get(cid)


def run_job(cid: str, name: str, fn, *args, **kwargs) -> bool:
    """동기 실행 (같은 고객사 작업이 이미 돌고 있으면 False)."""
    lk = _lock(cid)
    if not lk.acquire(blocking=False):
        db.log_job(cid, name, "skip", f"다른 작업 진행 중: {_running.get(cid)}")
        return False
    _running[cid] = name
    db.log_job(cid, name, "start")
    try:
        result = fn(*args, **kwargs)
        if isinstance(result, dict) and "issues" in result:
            lv = [i["level"] for i in result["issues"]]
            result = f'점검 항목: 오류 {lv.count("error")}, 주의 {lv.count("warn")}, 참고 {lv.count("info")}'
        db.log_job(cid, name, "ok", "" if result is None else str(result)[:2000])
        return True
    except Exception as e:  # noqa: BLE001
        db.log_job(cid, name, "fail", f"{e}\n{traceback.format_exc(limit=3)}")
        return False
    finally:
        _running.pop(cid, None)
        lk.release()


def start_job(cid: str, name: str, fn, *args, **kwargs) -> bool:
    """UI 용 비동기 실행."""
    if _lock(cid).locked():
        return False
    threading.Thread(target=run_job, args=(cid, name, fn, *args), kwargs=kwargs, daemon=True).start()
    return True


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _due(t: datetime, hm: str) -> bool:
    h, m = _hm(hm)
    return (t.hour, t.minute) >= (h, m)


def _throttled(key: tuple, t: datetime) -> bool:
    last = _last_attempt.get(key)
    if last and t - last < RETRY_GAP:
        return True
    _last_attempt[key] = t
    return False


def _customer_mail_today(cid: str, report_month: str, today: str) -> bool:
    return any(m["kind"] == "customer" and m["at"].startswith(today) for m in db.list_mail(cid, report_month))


def tick(t: datetime | None = None) -> None:
    t = t or now()
    today = t.date()
    cfg = load_config()
    for cid in load_customers():
        st = db.get_settings(cid)

        # 1) 일일 스냅샷
        if _due(t, cfg["scheduler"]["snapshot_time"]) and not snapshot.has_snapshot(cid, today) \
                and not _throttled((cid, "snapshot", today), t):
            run_job(cid, "snapshot", snapshot.take_snapshot, cid)

        sc = pipeline.schedule_for(cid, f"{today:%Y-%m}")
        rm = sc.report_month
        run = db.get_run(cid, rm)

        # 2) 점검일: 초안 (+ 점검 메일은 설정에서 켠 경우에만)
        review_on = bool(int(st.get("review_mail_enabled", 1)))
        need_review = (not run.get("review_mail_at")) if review_on else (not run.get("draft_at"))
        if today == sc.review_date and _due(t, st["review_time"]) and need_review \
                and run.get("status") != "sent" and not _throttled((cid, "review", rm, today), t):
            def review():
                if not db.get_run(cid, rm).get("draft_at"):
                    pipeline.make_draft(cid, rm, recollect=True)
                if review_on:
                    return pipeline.send_review_mail(cid, rm)
                return "초안 생성 완료 (점검 메일 꺼짐)"
            run_job(cid, f"auto-review {rm}", review)

        # 3) 발송일
        if today == sc.send_date and _due(t, st["send_time"]) and int(st["auto_send"]) \
                and run.get("status") != "sent" and not _customer_mail_today(cid, rm, today.isoformat()) \
                and not _throttled((cid, "send", rm, today), t):
            problems = pipeline.check_sendable(cid, rm)
            if not problems:
                run_job(cid, f"auto-send {rm}", pipeline.send_customer, cid, rm)
            elif not (run.get("unapproved_notified_at") or "").startswith(today.isoformat()):
                def notify():
                    msg = pipeline.notify_operator(
                        cid, rm, f"[발송보류] {rm} 월간 보고서 - {', '.join(problems)}",
                        f"오늘({today}) 발송 예정이던 {rm} 보고서를 발송하지 않았습니다.\n"
                        f"사유: {'; '.join(problems)}\n\n"
                        f"처리: {db.get_global('app_url')}/c/{cid}/{sc.send_month}")
                    db.update_run(cid, rm, unapproved_notified_at=db.ts())
                    return msg
                run_job(cid, f"notify-hold {rm}", notify)


_stop = threading.Event()


def scheduler_loop() -> None:
    gap = load_config()["scheduler"].get("tick_seconds", 30)
    db.log_job(None, "scheduler", "start")
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:  # noqa: BLE001
            db.log_job(None, "scheduler", "fail", f"{e}\n{traceback.format_exc(limit=3)}")
        _stop.wait(gap)


def start_scheduler() -> threading.Thread:
    th = threading.Thread(target=scheduler_loop, name="scheduler", daemon=True)
    th.start()
    return th
