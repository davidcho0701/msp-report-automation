from datetime import date

from src import schedule as s


def test_weekday_20th_is_send_date():
    sc = s.compute("2026-11")          # 2026-11-20 금
    assert sc.send_date == date(2026, 11, 20)
    assert sc.review_date == date(2026, 11, 19)
    assert sc.report_month == "2026-10"


def test_sunday_20th_moves_to_friday():
    sc = s.compute("2026-12")          # 2026-12-20 일
    assert sc.send_date == date(2026, 12, 18)
    assert sc.review_date == date(2026, 12, 17)


def test_monday_send_review_is_previous_friday():
    sc = s.compute("2027-09")          # 2027-09-20 월
    assert sc.send_date == date(2027, 9, 20)
    assert sc.review_date == date(2027, 9, 17)   # 금


def test_review_skips_holiday_before_send():
    sc = s.compute("2026-07")          # 2026-07-20 월, 7/17(금) 제헌절
    assert sc.send_date == date(2026, 7, 20)
    assert sc.review_date == date(2026, 7, 16)


def test_extra_holiday_and_override():
    sc = s.compute("2026-11", extra={"2026-11-20": "창립기념일"})
    assert sc.send_date == date(2026, 11, 19)
    sc = s.compute("2026-11", override={"send_date": "2026-11-23", "review_date": None, "note": "고객 요청"})
    assert sc.manual and sc.send_date == date(2026, 11, 23) and sc.review_date == date(2026, 11, 20)


def test_month_helpers():
    assert s.report_month_for("2027-01") == "2026-12"
    assert s.send_month_for("2026-12") == "2027-01"
    assert s.shift_month("2026-01", -1) == "2025-12"
