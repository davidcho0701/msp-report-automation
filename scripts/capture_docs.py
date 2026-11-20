"""README 용 화면 캡처 생성 (docs/images/*.png).

실제 데이터는 쓰지 않는다. 임시 폴더에 가짜 고객사·가짜 수치로 별도 환경을 만들어 다른 포트에서 화면을 띄우고
headless Edge 로 조회 화면만 캡처한다. NCP 호출, 메일 발송, 스케줄러는 동작하지 않는다.

실행:  .\\.venv\\Scripts\\python.exe scripts\\capture_docs.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
PORT = 8765
WIDTH = 1100
BG = (245, 246, 248)

SM, RM = "2026-11", "2026-10"  # 발송 월 / 보고 월 (예시)


def edge_path() -> str:
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(p).exists():
            return p
    raise SystemExit("Microsoft Edge 를 찾지 못했습니다")


def build_config(work: Path) -> None:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    cfg["web"]["port"] = PORT
    cfg["paths"] = {"reports_dir": str(work / "reports"), "data_dir": str(work / "data"),
                    "log_file": str(work / "logs" / "api_calls.log")}
    (work / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    base = yaml.safe_load((ROOT / "config" / "customers" / "skmns.yaml").read_text(encoding="utf-8"))
    cdir = work / "customers"
    cdir.mkdir()
    specs = [
        ("demoa", "예시 고객사 A", "WEB", ["web-app01", "web-app02"]),
        ("demob", "예시 고객사 B", "DB", ["db-main01"]),
    ]
    for cid, name, group, servers in specs:
        c = dict(base)
        c.update({"id": cid, "name": name})
        c["system_groups"] = [{"name": group, "servers": [
            {"name": s, "category": "운영", "device_type": "VM", "disk_mount": "/data", "disk_device": "xvdb"}
            for s in servers]}]
        c["backup"] = dict(base["backup"], targets=servers)
        (cdir / f"{cid}.yaml").write_text(yaml.safe_dump(c, allow_unicode=True), encoding="utf-8")


def seed_demo_data(work: Path) -> None:
    from src import db, pipeline
    from src.config import month_dir

    db.init_db()
    for cid in ("demoa", "demob"):
        db.save_settings(cid, operator_emails="operator@example.com", customer_to="customer@example.com",
                         customer_cc="manager@example.com" if cid == "demoa" else "",
                         base_day=20, review_time="09:00", send_time="10:00", auto_send=1, review_mail_enabled=1)
    db.set_global("operator_name", "운영 담당자")
    db.set_global("mail_from_name", "UWS MSP 운영팀")
    db.set_extra_holidays({"2026-11-13": "회사 창립기념일"})

    # 지원 내역 (초안 생성 시각보다 먼저 입력해서 '재생성 필요'로 보이지 않게)
    db.add_support("demoa", RM, "2026-10-08", "정기점검", "월간 정기 점검 수행", "완료")
    db.add_support("demoa", RM, "2026-10-21", "장애대응", "web-app02 디스크 사용량 증가 확인 및 로그 정리", "완료")
    time.sleep(1.2)

    devices = [
        {"group": "WEB", "category": "운영", "device_type": "VM", "name": n, "instance_no": str(i),
         "os": "CentOS Linux 7.8-64", "public_ip": f"203.0.113.{10 + i}", "cpu": 4, "memory_gb": 16,
         "storage_gb": 150, "storage": "150G", "status": "running", "disk_mount": "/data",
         "disk_device": "xvdb", "disks": ["/dev/xvda:BASIC:50G", "/dev/xvdb:SVRBS:100G"]}
        for i, n in enumerate(["web-app01", "web-app02"], start=1)]
    perf = [
        {"name": "web-app01", "cpu": 4, "memory_gb": 16, "storage": "150G", "fs_name": "/data",
         "cpu_max": 12.40, "cpu_avg": 3.21, "mem_max": 41.90, "mem_avg": 38.55, "swap_max": 0.0, "swap_avg": 0.0,
         "disk_max": 0.85, "disk_avg": 0.12},
        {"name": "web-app02", "cpu": 4, "memory_gb": 16, "storage": "150G", "fs_name": "/data",
         "cpu_max": 18.70, "cpu_avg": 5.04, "mem_max": 52.30, "mem_avg": 47.10, "swap_max": 0.0, "swap_avg": 0.0,
         "disk_max": 87.31, "disk_avg": 62.40},
    ]
    policy = "서버 이미지 수동 생성, 3일 보관 후 수동 삭제"
    backup = [{"server": n, "frequency": "09:00 Daily", "policy": policy, "auto_result": "", "level": "ok",
               "detail": "고정 문구 사용 (자동 판정 꺼짐)", "override": "", "result": "", "ok_days": 0,
               "missing_days": [], "unknown_days": 0} for n in ("web-app01", "web-app02")]
    issues = [
        {"level": "warn", "item": "임계치", "message": "web-app02 디스크 최대 87.31% ≥ 85%"},
        {"level": "warn", "item": "전월 대비", "message": "web-app02 메모리 변경 8 → 16"},
        {"level": "info", "item": "전월 대비", "message": "전월 데이터 없음 (비교 생략)"},
    ]
    data = {"customer_id": "demoa", "customer": "예시 고객사 A", "report_month": RM, "year": 2026, "month": 10,
            "last_day": 31, "period": "2026년 10월 1일 ~ 10월 31일", "groups": [{"name": "WEB"}],
            "devices": devices, "extra_servers": [], "performance": perf, "perf_errors": [],
            "perf_source": "api", "perf_csv_notes": [], "backup": backup, "support": [],
            "generated_at": db.ts(), "issues": issues}
    draft = month_dir("demoa", RM, "draft")
    pipeline.data_path("demoa", RM).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    pptx, pdf = draft / "예시 보고서.pptx", draft / "예시 보고서.pdf"
    pptx.write_bytes(b"demo")
    pdf.write_bytes(b"%PDF-demo")
    db.update_run("demoa", RM, status="draft", draft_at=db.ts(), pptx_path=str(pptx), pdf_path=str(pdf),
                  issues_json=json.dumps(issues, ensure_ascii=False), review_mail_at=db.ts(), perf_source="api")

    subj = "[점검요청] 예시 고객사 A 2026-10 월간 보고서 (발송예정 2026-11-20)"
    db.log_mail("demoa", RM, "review", "live", ["operator@example.com"], subj, "sent")
    db.log_job("demoa", "collect+draft 2026-10", "ok", "점검 항목: 오류 0, 주의 2, 참고 1")
    db.log_job("demoa", "auto-review 2026-10", "ok", "live")


def autocrop(path: Path, max_height: int | None = None) -> None:
    from PIL import Image

    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    last = 0
    for y in range(h - 1, -1, -1):
        if any(px[x, y] != BG for x in range(0, w, 7)):
            last = y
            break
    bottom = min(h, last + 28)
    if max_height:
        bottom = min(bottom, max_height)
    im.crop((0, 0, w, bottom)).save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="msp_docs_"))
    try:
        build_config(work)
        os.environ.update({
            "APP_CONFIG": str(work / "config.yaml"), "APP_CUSTOMERS_DIR": str(work / "customers"),
            # 캡처 중에는 어떤 경우에도 메일이 나가지 않도록
            # (화면은 조회만 하므로 Outlook 방식을 표시해도 Outlook 은 실행되지 않는다)
            "MAIL_MODE_OPERATOR": "dryrun", "MAIL_MODE_CUSTOMER": "dryrun", "MAIL_TRANSPORT": "outlook"})
        sys.path.insert(0, str(ROOT))
        seed_demo_data(work)

        import app as webapp
        from werkzeug.serving import make_server

        srv = make_server("127.0.0.1", PORT, webapp.app, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(1)

        edge = edge_path()
        shots = [
            ("dashboard", "/", 600, None),
            ("month_main", f"/c/demoa/{SM}", 1500, None),
            ("month_schedule", f"/c/demoa/{SM}?open=sched", 1500, 438),
            ("month_report_tab", f"/c/demoa/{SM}#perf", 2600, None),
            ("month_mail_tab", f"/c/demoa/{SM}#mail", 2200, None),
            ("customer_settings", "/c/demoa/settings", 1500, None),
            ("global_settings", "/settings", 1800, 900),
        ]
        for name, url, height, crop_h in shots:
            target = OUT / f"{name}.png"
            subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            "--force-device-scale-factor=1", f"--window-size={WIDTH},{height}",
                            f"--user-data-dir={work / 'edge'}", f"--screenshot={target}",
                            f"http://127.0.0.1:{PORT}{url}"],
                           capture_output=True, timeout=120)
            autocrop(target, crop_h)
            print("saved", target.relative_to(ROOT))
        srv.shutdown()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
