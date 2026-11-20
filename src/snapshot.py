"""매일 서버 이미지 목록 스냅샷 + 월간 백업 현황 계산.

이미지는 3일 보관 후 삭제되므로 월말 조회로는 이력을 알 수 없다.
매일 스냅샷을 reports/<cid>/snapshots/YYYY-MM-DD.json 에 저장하고,
월간 보고서 생성 시 이 스냅샷들을 합쳐 일자별 이미지 생성 여부를 판단한다.
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from . import ncloud_cli
from .config import customer_dir, now

KEEP_DAYS = 3  # 이미지 보관일수 -> 스냅샷 1회로 최근 3일(당일 포함)을 볼 수 있음


def snapshot_dir(cid: str) -> Path:
    d = customer_dir(cid) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def has_snapshot(cid: str, day: date) -> bool:
    return (snapshot_dir(cid) / f"{day.isoformat()}.json").exists()


def take_snapshot(cid: str) -> Path:
    servers = ncloud_cli.call("getServerInstanceList")
    images = ncloud_cli.call("getMemberServerImageInstanceList")
    t = now()
    data = {
        "taken_at": t.isoformat(),
        "servers": {s["serverInstanceNo"]: s["serverName"]
                    for s in servers["getServerInstanceListResponse"]["serverInstanceList"]},
        "images": [
            {"no": i["memberServerImageInstanceNo"], "name": i["memberServerImageName"],
             "server_no": i["originalServerInstanceNo"], "create_date": i["createDate"],
             "status": i["memberServerImageInstanceStatus"]["code"]}
            for i in images["getMemberServerImageInstanceListResponse"].get("memberServerImageInstanceList", [])
        ],
    }
    p = snapshot_dir(cid) / f"{t.date().isoformat()}.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _load_snapshots(cid: str) -> dict[date, dict]:
    out = {}
    for p in snapshot_dir(cid).glob("*.json"):
        try:
            out[date.fromisoformat(p.stem)] = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def backup_status(cid: str, report_month: str, ccfg: dict, overrides: dict[str, str]) -> list[dict]:
    bcfg = ccfg["backup"]
    if not bcfg.get("auto_judge", True):
        # 고정 모드: 스냅샷으로 판정하지 않고 설정의 고정 문구(기본 빈칸)를 그대로 쓴다
        fixed = bcfg.get("fixed_result", "") or ""
        return [{
            "server": server, "frequency": bcfg["frequency"], "policy": bcfg["policy"],
            "auto_result": fixed, "level": "ok", "detail": "고정 문구 사용 (자동 판정 꺼짐)",
            "override": overrides.get(server, ""), "result": overrides.get(server) or fixed,
            "ok_days": 0, "missing_days": [], "unknown_days": 0,
        } for server in bcfg["targets"]]

    y, m = map(int, report_month.split("-"))
    days = [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]
    snaps = _load_snapshots(cid)
    snap_days = sorted(snaps)

    # 서버별 이미지 생성일 집합
    created: dict[str, set[date]] = {}
    for s in snaps.values():
        for img in s["images"]:
            name = s["servers"].get(img["server_no"], img["server_no"])
            try:
                d = datetime.strptime(img["create_date"], "%Y-%m-%dT%H:%M:%S%z").date()
            except ValueError:
                continue
            created.setdefault(name, set()).add(d)

    def observable(d: date) -> bool:
        # d 일자 이미지는 d ~ d+2 일 스냅샷에서 보인다
        return any(d <= sd <= d + timedelta(days=KEEP_DAYS - 1) for sd in snap_days)

    rows = []
    bcfg = ccfg["backup"]
    for server in bcfg["targets"]:
        have = created.get(server, set())
        ok = [d for d in days if d in have]
        unknown = [d for d in days if d not in have and not observable(d)]
        missing = [d for d in days if d not in have and observable(d)]
        if not ok and not missing:
            auto, level = "확인 불가", "unknown"
            detail = "스냅샷 이력 없음 (일일 스냅샷 수집 이전 기간)"
        elif missing:
            auto, level = f"누락 {len(missing)}일", "missing"
            detail = "누락: " + ", ".join(f"{d.month}/{d.day}" for d in missing)
        elif unknown:
            auto, level = "확인 불가", "partial"
            detail = f"확인 {len(ok)}일, 이력 없음 {len(unknown)}일"
        else:
            auto, level = "정상", "ok"
            detail = f"{len(ok)}/{len(days)}일 이미지 생성 확인"
        rows.append({
            "server": server, "frequency": bcfg["frequency"], "policy": bcfg["policy"],
            "auto_result": auto, "level": level, "detail": detail,
            "override": overrides.get(server, ""),
            "result": overrides.get(server) or auto,
            "ok_days": len(ok), "missing_days": [d.isoformat() for d in missing],
            "unknown_days": len(unknown),
        })
    return rows
