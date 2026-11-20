"""명령행 진입점 (웹 UI 를 쓰지 않을 때/작업 스케줄러용).

  python main.py web                              # 웹 UI + 자동 스케줄러
  python main.py snapshot  [--customer skmns]
  python main.py draft     --month 2026-10 [--no-recollect]
  python main.py review-mail --month 2026-10
  python main.py approve   --month 2026-10 --by 홍길동
  python main.py send      --month 2026-10      # 승인본만, 모드는 .env 에 따름
  python main.py schedule  --send-month 2026-12
"""
from __future__ import annotations

import argparse
import json

from src import db, pipeline, snapshot


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("web")
    for name in ("snapshot", "draft", "review-mail", "approve", "send", "schedule"):
        p = sub.add_parser(name)
        p.add_argument("--customer", default="skmns")
        if name in ("draft", "review-mail", "approve", "send"):
            p.add_argument("--month", required=True, help="보고 대상 월 YYYY-MM")
        if name == "draft":
            p.add_argument("--no-recollect", action="store_true")
        if name == "approve":
            p.add_argument("--by", default="")
        if name == "send":
            p.add_argument("--live", action="store_true", help=".env 모드와 무관하게 실제 발송")
        if name == "schedule":
            p.add_argument("--send-month", required=True)
    a = ap.parse_args()
    db.init_db()

    if a.cmd == "web":
        import app

        app.main()
    elif a.cmd == "snapshot":
        print(snapshot.take_snapshot(a.customer))
    elif a.cmd == "draft":
        d = pipeline.make_draft(a.customer, a.month, recollect=not a.no_recollect)
        print(json.dumps(d["issues"], ensure_ascii=False, indent=2))
    elif a.cmd == "review-mail":
        print(pipeline.send_review_mail(a.customer, a.month))
    elif a.cmd == "approve":
        pipeline.approve(a.customer, a.month, a.by)
        print("approved")
    elif a.cmd == "send":
        print(pipeline.send_customer(a.customer, a.month, live=a.live))
    elif a.cmd == "schedule":
        print(pipeline.schedule_for(a.customer, a.send_month))


if __name__ == "__main__":
    main()
