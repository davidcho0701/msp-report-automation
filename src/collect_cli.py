"""CLI 수집: 서버/블록스토리지/공인IP/서버이미지 원본 JSON 저장 + 장비 현황 변환."""
from __future__ import annotations

import argparse
import json

from . import ncloud_cli
from .config import get_customer, month_dir

RAW_SOURCES = {
    "servers": "getServerInstanceList",
    "block_storage": "getBlockStorageInstanceList",
    "public_ip": "getPublicIpInstanceList",
    "member_images": "getMemberServerImageInstanceList",
}
REQUIRED = {"servers", "block_storage"}
GIB = 1024**3


def fetch_raw(cid: str, report_month: str) -> dict[str, dict]:
    d = month_dir(cid, report_month, "raw")
    out = {}
    for key, api in RAW_SOURCES.items():
        try:
            data = ncloud_cli.call(api)
        except RuntimeError as e:
            if key in REQUIRED:
                raise
            data = {"_error": str(e), "_api": api}  # 권한 부족 등: 기록만
        (d / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        out[key] = data
    return out


def load_raw(cid: str, report_month: str) -> dict[str, dict]:
    d = month_dir(cid, report_month, "raw")
    return {k: json.loads((d / f"{k}.json").read_text(encoding="utf-8"))
            for k in RAW_SOURCES if (d / f"{k}.json").exists()}


def _os_label(image_code: str, mapping: dict[str, str]) -> str:
    for key, label in mapping.items():
        if key in (image_code or ""):
            return label
    return f"UNMAPPED({image_code})"


def server_list(raw: dict) -> list[dict]:
    return raw["servers"]["getServerInstanceListResponse"]["serverInstanceList"]


def build_device_rows(raw: dict[str, dict], ccfg: dict) -> tuple[list[dict], list[str]]:
    """설정의 서버 순서대로 장비 현황 행 + (설정에 없는 API 서버 이름 목록).

    스토리지 = 해당 서버에 연결(ATTAC)된 블록스토리지(BASIC 기본디스크 + SVRBS 추가) 합계.
    """
    servers = server_list(raw)
    by_name = {s["serverName"]: s for s in servers}
    blocks = raw["block_storage"]["getBlockStorageInstanceListResponse"]["blockStorageInstanceList"]

    rows, configured = [], set()
    for group in ccfg["system_groups"]:
        for sv in group["servers"]:
            configured.add(sv["name"])
            s = by_name.get(sv["name"])
            if s is None:
                rows.append({"group": group["name"], "name": sv["name"], "missing": True,
                             "category": sv["category"], "device_type": sv["device_type"]})
                continue
            attached = [b for b in blocks if b["serverInstanceNo"] == s["serverInstanceNo"]
                        and b["blockStorageInstanceStatus"]["code"] == "ATTAC"]
            storage_gb = int(sum(b["blockStorageSize"] for b in attached) / GIB)
            rows.append({
                "group": group["name"],
                "category": sv["category"],
                "device_type": sv["device_type"],
                "name": s["serverName"],
                "instance_no": s["serverInstanceNo"],
                "os": _os_label(s.get("serverImageProductCode", ""), ccfg["os_mapping"]),
                "public_ip": s.get("publicIp", ""),
                "cpu": int(s["cpuCount"]),
                "memory_gb": int(s["memorySize"] / GIB),
                "storage_gb": storage_gb,
                "storage": f"{storage_gb}G",
                "status": s.get("serverInstanceStatusName"),
                "disk_mount": sv.get("disk_mount", ""),
                "disk_device": sv.get("disk_device", ""),
                "disks": [f'{b["deviceName"]}:{b["blockStorageType"]["code"]}:{b["blockStorageSize"] // GIB}G'
                          for b in sorted(attached, key=lambda b: b["deviceName"])],
            })
    extra = sorted(n for n in by_name if n not in configured)
    return rows, extra


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--customer", default="skmns")
    ap.add_argument("--month", required=True, help="보고 대상 월 YYYY-MM")
    ap.add_argument("--offline", action="store_true", help="저장된 raw 로만 변환")
    a = ap.parse_args()
    ccfg = get_customer(a.customer)
    raw = load_raw(a.customer, a.month) if a.offline else fetch_raw(a.customer, a.month)
    rows, extra = build_device_rows(raw, ccfg)
    for r in rows:
        print(r)
    if extra:
        print("설정에 없는 서버:", extra)


if __name__ == "__main__":
    main()
