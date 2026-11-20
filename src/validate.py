"""운영자 점검 항목 (고객사 메일에는 포함하지 않음)."""
from __future__ import annotations


def issue(level: str, item: str, message: str) -> dict:
    return {"level": level, "item": item, "message": message}


def validate(data: dict, prev: dict | None, ccfg: dict) -> list[dict]:
    """data: build_report_data 결과. prev: 전월 data(없으면 None)."""
    out: list[dict] = []
    th = ccfg["thresholds"]

    # 장비
    for r in data["devices"]:
        if r.get("missing"):
            out.append(issue("error", "장비", f'{r["name"]}: NCP 서버 목록에 없음'))
            continue
        if r["os"].startswith("UNMAPPED"):
            out.append(issue("error", "장비", f'{r["name"]}: OS 매핑 없음 {r["os"]} (customers yaml os_mapping)'))
        if r.get("status") and r["status"] != "running":
            out.append(issue("warn", "장비", f'{r["name"]}: 현재 상태 {r["status"]}'))
    for n in data.get("extra_servers", []):
        out.append(issue("info", "장비", f"설정에 없는 서버가 NCP 에 있음: {n} (보고서 미포함)"))

    if prev:
        pd = {r["name"]: r for r in prev.get("devices", []) if not r.get("missing")}
        cd = {r["name"]: r for r in data["devices"] if not r.get("missing")}
        if len(pd) != len(cd):
            out.append(issue("warn", "전월 대비", f"서버 수 변경 {len(pd)} → {len(cd)}"))
        for name, r in cd.items():
            p = pd.get(name)
            if not p:
                out.append(issue("warn", "전월 대비", f"{name}: 신규"))
                continue
            for k, label in (("cpu", "CPU"), ("memory_gb", "메모리"), ("storage", "스토리지"),
                             ("public_ip", "공인IP"), ("os", "OS")):
                if p.get(k) != r.get(k):
                    out.append(issue("warn", "전월 대비", f"{name} {label} 변경 {p.get(k)} → {r.get(k)}"))
    else:
        out.append(issue("info", "전월 대비", "전월 데이터 없음 (비교 생략)"))

    # 성능
    perf_errors = data.get("perf_errors", [])
    for e in perf_errors:
        out.append(issue("error", "성능", e))
    labels = {"cpu": "CPU", "mem": "메모리", "swap": "SWAP", "disk": "디스크"}
    missing = {r["name"]: [label for k, label in labels.items() if r.get(f"{k}_max") is None]
               for r in data["performance"]}
    missing = {n: v for n, v in missing.items() if v}
    if missing and not perf_errors:  # 연결 실패면 위 메시지 하나로 충분
        out.append(issue("error", "성능", "값 없음 — " + "; ".join(f'{n}: {", ".join(v)}' for n, v in missing.items())))
    for r in data["performance"]:
        if (r.get("cpu_max") or 0) >= th["cpu_pct"]:
            out.append(issue("warn", "임계치", f'{r["name"]} CPU 최대 {r["cpu_max"]}% ≥ {th["cpu_pct"]}%'))
        if (r.get("mem_max") or 0) >= th["mem_pct"]:
            out.append(issue("warn", "임계치", f'{r["name"]} 메모리 최대 {r["mem_max"]}% ≥ {th["mem_pct"]}%'))
        if (r.get("disk_max") or 0) >= th["disk_pct"]:
            out.append(issue("warn", "임계치", f'{r["name"]} 디스크 최대 {r["disk_max"]}% ≥ {th["disk_pct"]}%'))

    # 백업 (같은 판정은 한 줄로)
    groups: dict[tuple, list[str]] = {}
    for b in data["backup"]:
        if b["override"]:
            if b["level"] != "ok":
                out.append(issue("info", "백업", f'{b["server"]}: 직접 지정 "{b["override"]}" (자동 확인 {b["auto_result"]})'))
        elif b["level"] != "ok":
            groups.setdefault((b["level"], b["auto_result"], b["detail"]), []).append(b["server"])
    for (level, res, detail), servers in groups.items():
        out.append(issue("error" if level == "missing" else "warn", "백업",
                         f'{", ".join(servers)}: {res} — {detail}'))

    # 지원 내역
    if not data["support"]:
        out.append(issue("warn", "지원 내역", "입력된 지원 내역 없음"))
    return out
