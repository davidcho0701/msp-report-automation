"""Cloud Insight 콘솔에서 내려받은 CSV 로 성능 현황 채우기 (API 대체 수단).

예시 형식 (Monitoring___UWS_CPU.csv):
    DateTime,"lbsbu01, SERVER/avg_cpu_used_rto","lbsbu02, SERVER/avg_cpu_used_rto"
    26-09-01 00:00:00,0.119048,0.35122
컬럼 헤더 "서버명, 대상/지표" 에서 지표 이름으로 cpu/mem/swap/disk 를 판별한다.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from .collect_insight import r2

METRIC_PATTERNS = [
    ("cpu", re.compile(r"cpu_used_rto|cpu", re.I)),
    ("mem", re.compile(r"mem_usert|MEMORY", re.I)),
    ("swap", re.compile(r"swap", re.I)),
    ("disk", re.compile(r"DISK|fs_usert|FILESYSTEM", re.I)),
]


def classify(metric_part: str) -> str | None:
    # swap 이 MEMORY 범주에 있을 수 있으므로 swap 우선
    if re.search(r"swap", metric_part, re.I):
        return "swap"
    for key, pat in METRIC_PATTERNS:
        if pat.search(metric_part):
            return key
    return None


def parse_csv(content: bytes, report_month: str) -> tuple[dict[tuple[str, str], dict], list[str]]:
    """{(server, metric_key): {"max", "avg", "n", "column"}} 와 경고 목록.

    report_month 밖의 행은 제외한다.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = list(csv.reader(io.StringIO(text)))
    if not reader:
        return {}, ["빈 파일"]
    hdr, data = reader[0], reader[1:]
    y, m = map(int, report_month.split("-"))
    warns, out = [], {}
    in_month = []
    for r in data:
        if not r or not r[0].strip():
            continue
        try:
            t = datetime.strptime(r[0].strip(), "%y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                t = datetime.strptime(r[0].strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        if (t.year, t.month) == (y, m):
            in_month.append(r)
    skipped = len(data) - len(in_month)
    if skipped:
        warns.append(f"{report_month} 밖의 행 {skipped}개 제외")
    for c in range(1, len(hdr)):
        col = hdr[c]
        if "," not in col:
            warns.append(f"알 수 없는 컬럼: {col}")
            continue
        server, metric_part = [x.strip() for x in col.split(",", 1)]
        key = classify(metric_part)
        if not key:
            warns.append(f"지표 판별 불가: {col}")
            continue
        vals = []
        for r in in_month:
            if c < len(r) and r[c].strip() not in ("", "-", "null"):
                try:
                    vals.append(float(r[c]))
                except ValueError:
                    pass
        if not vals:
            warns.append(f"값 없음: {col}")
            continue
        out[(server, key)] = {"max": r2(max(vals)), "avg": r2(sum(vals) / len(vals)),
                              "n": len(vals), "column": col}
    return out, warns


def apply(perf_rows: list[dict], parsed: dict[tuple[str, str], dict]) -> list[str]:
    applied = []
    for row in perf_rows:
        for key in ("cpu", "mem", "swap", "disk"):
            v = parsed.get((row["name"], key))
            if v:
                row[f"{key}_max"], row[f"{key}_avg"] = v["max"], v["avg"]
                applied.append(f'{row["name"]} {key} ← {v["column"]} (n={v["n"]})')
    return applied
