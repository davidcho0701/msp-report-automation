"""운영 설정/상태 저장소 (SQLite, data/app.db).

UI 에서 바꾸는 값(수신자, 메일 문구, 일정 수동 지정, 지원 내역, 승인/발송 상태)은 모두 여기 저장한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import load_config, now, project_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_settings (
    customer_id     TEXT PRIMARY KEY,
    operator_emails TEXT NOT NULL DEFAULT '',
    customer_to     TEXT NOT NULL DEFAULT '',
    customer_cc     TEXT NOT NULL DEFAULT '',
    subject_tpl     TEXT NOT NULL,
    body_tpl        TEXT NOT NULL,
    base_day        INTEGER NOT NULL DEFAULT 20,
    review_time     TEXT NOT NULL DEFAULT '09:00',
    send_time       TEXT NOT NULL DEFAULT '10:00',
    auto_send       INTEGER NOT NULL DEFAULT 1,
    review_mail_enabled INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS global_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_overrides (
    customer_id TEXT NOT NULL,
    send_month  TEXT NOT NULL,
    review_date TEXT,
    send_date   TEXT,
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT,
    PRIMARY KEY (customer_id, send_month)
);
CREATE TABLE IF NOT EXISTS support_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  TEXT NOT NULL,
    report_month TEXT NOT NULL,
    date         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    content      TEXT NOT NULL,
    result       TEXT NOT NULL,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS backup_overrides (
    customer_id  TEXT NOT NULL,
    report_month TEXT NOT NULL,
    server       TEXT NOT NULL,
    result       TEXT NOT NULL,
    PRIMARY KEY (customer_id, report_month, server)
);
CREATE TABLE IF NOT EXISTS report_runs (
    customer_id      TEXT NOT NULL,
    report_month     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'none',
    draft_at         TEXT,
    inputs_updated_at TEXT,
    pptx_path        TEXT,
    pdf_path         TEXT,
    issues_json      TEXT,
    review_mail_at   TEXT,
    approved_at      TEXT,
    approved_by      TEXT,
    approved_pdf     TEXT,
    approved_sha256  TEXT,
    sent_at          TEXT,
    unapproved_notified_at TEXT,
    perf_source      TEXT,
    PRIMARY KEY (customer_id, report_month)
);
CREATE TABLE IF NOT EXISTS mail_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  TEXT NOT NULL,
    report_month TEXT NOT NULL,
    kind         TEXT NOT NULL,      -- review | customer | notify
    mode         TEXT NOT NULL,      -- dryrun | live
    recipients   TEXT NOT NULL,
    subject      TEXT NOT NULL,
    status       TEXT NOT NULL,      -- saved | sent | failed
    path         TEXT,
    error        TEXT,
    at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT,
    job         TEXT NOT NULL,
    status      TEXT NOT NULL,       -- start | ok | fail | skip
    message     TEXT NOT NULL DEFAULT '',
    at          TEXT NOT NULL
);
"""

DEFAULT_SUBJECT = "[UWS] {customer} {year}년 {month}월 월간 MSP 보고서"
DEFAULT_BODY = (
    "안녕하세요, {customer} 담당자님.\n"
    "UWS 클라우드 MSP 운영팀입니다.\n\n"
    "{year}년 {month}월({period}) 월간 MSP 보고서를 첨부와 같이 송부드립니다.\n"
    "확인 부탁드립니다.\n\n"
    "감사합니다.\n"
)

EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

_db_path_override: Path | None = None


def set_db_path(p: Path | None) -> None:
    """테스트용."""
    global _db_path_override
    _db_path_override = p


def db_path() -> Path:
    if _db_path_override:
        return _db_path_override
    d = project_path(load_config()["paths"]["data_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d / "app.db"


@contextmanager
def connect():
    con = sqlite3.connect(db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        # 기존 DB 마이그레이션: 점검 메일 사용 여부 컬럼
        cols = {r["name"] for r in con.execute("PRAGMA table_info(customer_settings)")}
        if "review_mail_enabled" not in cols:
            con.execute("ALTER TABLE customer_settings ADD COLUMN review_mail_enabled INTEGER NOT NULL DEFAULT 1")


def ts() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def parse_emails(text: str) -> list[str]:
    items = [x.strip() for x in re.split(r"[,;\s]+", text or "") if x.strip()]
    bad = [x for x in items if not EMAIL_RE.match(x)]
    if bad:
        raise ValueError("이메일 형식이 아닙니다: " + ", ".join(bad))
    return items


# ---------- 고객사 설정 ----------
def get_settings(cid: str) -> dict:
    with connect() as con:
        r = con.execute("SELECT * FROM customer_settings WHERE customer_id=?", (cid,)).fetchone()
        if r is None:
            con.execute(
                "INSERT INTO customer_settings (customer_id, subject_tpl, body_tpl, updated_at) VALUES (?,?,?,?)",
                (cid, DEFAULT_SUBJECT, DEFAULT_BODY, ts()),
            )
            r = con.execute("SELECT * FROM customer_settings WHERE customer_id=?", (cid,)).fetchone()
    return dict(r)


SETTINGS_FIELDS = {"operator_emails", "customer_to", "customer_cc", "subject_tpl", "body_tpl",
                   "base_day", "review_time", "send_time", "auto_send", "review_mail_enabled"}


def save_settings(cid: str, **fields) -> None:
    get_settings(cid)
    fields = {k: v for k, v in fields.items() if k in SETTINGS_FIELDS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as con:
        con.execute(f"UPDATE customer_settings SET {sets}, updated_at=? WHERE customer_id=?",
                    (*fields.values(), ts(), cid))


# ---------- 전역 설정 ----------
GLOBAL_DEFAULTS = {"mail_from": "", "mail_from_name": "UWS MSP 운영팀", "operator_name": "",
                   "extra_holidays": "[]", "app_url": "http://127.0.0.1:8000"}


def get_global(key: str) -> str:
    with connect() as con:
        r = con.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else GLOBAL_DEFAULTS.get(key, "")


def set_global(key: str, value: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO global_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_extra_holidays() -> dict[str, str]:
    """{'YYYY-MM-DD': '사유'}"""
    try:
        items = json.loads(get_global("extra_holidays") or "[]")
    except json.JSONDecodeError:
        items = []
    return {i["date"]: i.get("name", "휴무") for i in items}


def set_extra_holidays(d: dict[str, str]) -> None:
    set_global("extra_holidays", json.dumps(
        [{"date": k, "name": v} for k, v in sorted(d.items())], ensure_ascii=False))


# ---------- 일정 수동 지정 ----------
def get_override(cid: str, send_month: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM schedule_overrides WHERE customer_id=? AND send_month=?",
                        (cid, send_month)).fetchone()
    return dict(r) if r else None


def set_override(cid: str, send_month: str, review_date: str | None, send_date: str | None, note: str) -> None:
    with connect() as con:
        con.execute(
            "INSERT INTO schedule_overrides VALUES (?,?,?,?,?,?) ON CONFLICT(customer_id, send_month) "
            "DO UPDATE SET review_date=excluded.review_date, send_date=excluded.send_date, "
            "note=excluded.note, updated_at=excluded.updated_at",
            (cid, send_month, review_date, send_date, note, ts()))


def clear_override(cid: str, send_month: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM schedule_overrides WHERE customer_id=? AND send_month=?", (cid, send_month))


# ---------- 지원 내역 ----------
def list_support(cid: str, report_month: str) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM support_entries WHERE customer_id=? AND report_month=? "
                           "ORDER BY date, id", (cid, report_month)).fetchall()
    return [dict(r) for r in rows]


def add_support(cid: str, report_month: str, date: str, kind: str, content: str, result: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO support_entries (customer_id, report_month, date, kind, content, result, created_at) "
                    "VALUES (?,?,?,?,?,?,?)", (cid, report_month, date, kind, content, result, ts()))
    touch_inputs(cid, report_month)


def delete_support(cid: str, entry_id: int) -> None:
    with connect() as con:
        r = con.execute("SELECT report_month FROM support_entries WHERE id=? AND customer_id=?",
                        (entry_id, cid)).fetchone()
        con.execute("DELETE FROM support_entries WHERE id=? AND customer_id=?", (entry_id, cid))
    if r:
        touch_inputs(cid, r["report_month"])


# ---------- 백업 결과 수동 지정 ----------
def get_backup_overrides(cid: str, report_month: str) -> dict[str, str]:
    with connect() as con:
        rows = con.execute("SELECT server, result FROM backup_overrides WHERE customer_id=? AND report_month=?",
                           (cid, report_month)).fetchall()
    return {r["server"]: r["result"] for r in rows}


def set_backup_override(cid: str, report_month: str, server: str, result: str) -> None:
    with connect() as con:
        if result.strip():
            con.execute("INSERT INTO backup_overrides VALUES (?,?,?,?) ON CONFLICT(customer_id, report_month, server) "
                        "DO UPDATE SET result=excluded.result", (cid, report_month, server, result.strip()))
        else:
            con.execute("DELETE FROM backup_overrides WHERE customer_id=? AND report_month=? AND server=?",
                        (cid, report_month, server))
    touch_inputs(cid, report_month)


# ---------- 보고서 상태 ----------
RUN_FIELDS = {"status", "draft_at", "inputs_updated_at", "pptx_path", "pdf_path", "issues_json",
              "review_mail_at", "approved_at", "approved_by", "approved_pdf", "approved_sha256",
              "sent_at", "unapproved_notified_at", "perf_source"}


def get_run(cid: str, report_month: str) -> dict:
    with connect() as con:
        r = con.execute("SELECT * FROM report_runs WHERE customer_id=? AND report_month=?",
                        (cid, report_month)).fetchone()
    if r:
        return dict(r)
    return {"customer_id": cid, "report_month": report_month, "status": "none"}


def update_run(cid: str, report_month: str, **fields) -> None:
    bad = set(fields) - RUN_FIELDS
    if bad:
        raise ValueError(f"unknown run fields: {bad}")
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO report_runs (customer_id, report_month) VALUES (?,?)",
                    (cid, report_month))
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE report_runs SET {sets} WHERE customer_id=? AND report_month=?",
                        (*fields.values(), cid, report_month))


def touch_inputs(cid: str, report_month: str) -> None:
    update_run(cid, report_month, inputs_updated_at=ts())


def is_stale(run: dict) -> bool:
    """초안 생성 이후 지원내역/백업결과 등 입력이 바뀌었는지."""
    return bool(run.get("draft_at") and run.get("inputs_updated_at")
                and run["inputs_updated_at"] > run["draft_at"])


# ---------- 로그 ----------
def log_mail(cid, report_month, kind, mode, recipients, subject, status, path=None, error=None) -> None:
    with connect() as con:
        con.execute("INSERT INTO mail_log (customer_id, report_month, kind, mode, recipients, subject, status, path, error, at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cid, report_month, kind, mode, ", ".join(recipients), subject, status, path, error, ts()))


def list_mail(cid: str, report_month: str) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM mail_log WHERE customer_id=? AND report_month=? ORDER BY id DESC",
                           (cid, report_month)).fetchall()
    return [dict(r) for r in rows]


def customer_already_sent(cid: str, report_month: str) -> bool:
    """마지막 '발송 상태 초기화'(kind='reset') 이후에 실제 발송 기록이 있는지. 초기화 전 이력은 남기되 중복 판정에서 제외."""
    with connect() as con:
        last_reset = con.execute(
            "SELECT COALESCE(MAX(id), 0) AS rid FROM mail_log WHERE customer_id=? AND report_month=? AND kind='reset'",
            (cid, report_month)).fetchone()["rid"]
        r = con.execute("SELECT 1 FROM mail_log WHERE customer_id=? AND report_month=? AND kind='customer' "
                        "AND mode='live' AND status='sent' AND id>? LIMIT 1",
                        (cid, report_month, last_reset)).fetchone()
    return r is not None


def log_job(cid: str | None, job: str, status: str, message: str = "") -> None:
    with connect() as con:
        con.execute("INSERT INTO job_log (customer_id, job, status, message, at) VALUES (?,?,?,?,?)",
                    (cid, job, status, message[:4000], ts()))


def list_jobs(cid: str | None = None, limit: int = 30) -> list[dict]:
    with connect() as con:
        if cid:
            rows = con.execute("SELECT * FROM job_log WHERE customer_id=? ORDER BY id DESC LIMIT ?",
                               (cid, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM job_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
