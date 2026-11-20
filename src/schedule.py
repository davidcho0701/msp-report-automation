"""발송일/점검일 계산.

규칙(기본):
- 발송일 = 발송월의 기준일(기본 20일). 주말/공휴일이면 직전 영업일.
- 점검일 = 발송일의 직전 영업일.
- 운영자가 UI 에서 지정한 날짜(수동)가 있으면 그것을 우선한다.
공휴일: holidays 패키지(KR, 대체공휴일 포함) + UI 에서 추가한 휴무일.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

import holidays as holidays_lib

WEEKDAYS = "월화수목금토일"


def kr_holidays(years: list[int], extra: dict[str, str] | None = None) -> dict[date, str]:
    h = {d: n for d, n in holidays_lib.KR(years=years, language="ko").items()}
    for ds, name in (extra or {}).items():
        try:
            h[date.fromisoformat(ds)] = name
        except ValueError:
            continue
    return h


def is_business_day(d: date, hol: dict[date, str]) -> bool:
    return d.weekday() < 5 and d not in hol


def prev_business_day(d: date, hol: dict[date, str]) -> date:
    d -= timedelta(days=1)
    while not is_business_day(d, hol):
        d -= timedelta(days=1)
    return d


def fmt(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def report_month_for(send_month: str) -> str:
    y, m = map(int, send_month.split("-"))
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


def send_month_for(report_month: str) -> str:
    y, m = map(int, report_month.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


def shift_month(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


@dataclass
class Schedule:
    send_month: str
    report_month: str
    base_date: date
    auto_send: date
    auto_review: date
    auto_reason: str
    send_date: date
    review_date: date
    manual: bool
    note: str = ""


def compute(send_month: str, base_day: int = 20, extra: dict[str, str] | None = None,
            override: dict | None = None) -> Schedule:
    y, m = map(int, send_month.split("-"))
    hol = kr_holidays([y - 1, y, y + 1], extra)
    base = date(y, m, min(base_day, calendar.monthrange(y, m)[1]))

    if is_business_day(base, hol):
        auto_send = base
        reason = f"기준일 {fmt(base)} 영업일"
    else:
        auto_send = prev_business_day(base + timedelta(days=1), hol)
        why = hol.get(base) or ("주말" if base.weekday() >= 5 else "휴무")
        reason = f"기준일 {fmt(base)} {why} → 직전 영업일 {fmt(auto_send)}"
    auto_review = prev_business_day(auto_send, hol)

    send_d, review_d, manual, note = auto_send, auto_review, False, ""
    if override:
        if override.get("send_date"):
            send_d, manual = date.fromisoformat(override["send_date"]), True
        if override.get("review_date"):
            review_d, manual = date.fromisoformat(override["review_date"]), True
        elif manual:
            review_d = prev_business_day(send_d, hol)
        note = override.get("note") or ""

    return Schedule(send_month, report_month_for(send_month), base, auto_send, auto_review,
                    reason, send_d, review_d, manual, note)


def month_calendar(send_month: str, extra: dict[str, str] | None = None) -> list[list[dict | None]]:
    """UI 달력용: 주 단위(월~일) 리스트."""
    y, m = map(int, send_month.split("-"))
    hol = kr_holidays([y], extra)
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(y, m):
        row = []
        for d in week:
            if d.month != m:
                row.append(None)
                continue
            row.append({"date": d, "day": d.day, "weekend": d.weekday() >= 5,
                        "holiday": hol.get(d), "iso": d.isoformat()})
        weeks.append(row)
    return weeks
