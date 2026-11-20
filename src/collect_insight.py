"""Cloud Insight 조회 전용 클라이언트 + 성능 현황 수집.

공식 문서 기준 (api.ncloud-docs.com):
- 서명: base64(HMAC-SHA256(secret, f"{METHOD} {URI}\\n{timestamp_ms}\\n{access_key}"))
  URI 는 호스트 제외, 경로 + 쿼리스트링.
- schema key 조회: GET  /cw_fea/real/cw/api/schema/system/list  -> [{cw_key, prodName}, ...]
- 데이터 조회:     POST /cw_fea/real/cw/api/data/query           -> [[ts_ms, value], ...]
- 보존: Min1 8일, Min5 1개월, Min30 3개월, Hour2 6개월, Day1 1년
  권장 조회 구간: Min1 1시간, Min5 6시간, Min30/Hour2 1주, Day1 1개월

조회 전용: ALLOWED_ENDPOINTS 외의 (method, path) 는 PermissionError.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import requests

from .config import get_api_keys, get_customer, load_config, month_dir, now, tz
from .ncloud_cli import log_api

SCHEMA_LIST_PATH = "/cw_fea/real/cw/api/schema/system/list"
DATA_QUERY_PATH = "/cw_fea/real/cw/api/data/query"

ALLOWED_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {("GET", SCHEMA_LIST_PATH), ("POST", DATA_QUERY_PATH)}
)

CHUNK = {"Min1": timedelta(hours=1), "Min5": timedelta(hours=6), "Min30": timedelta(weeks=1),
         "Hour2": timedelta(weeks=1), "Day1": timedelta(days=31)}
RETENTION = {"Min1": timedelta(days=8), "Min5": timedelta(days=31), "Min30": timedelta(days=90),
             "Hour2": timedelta(days=180), "Day1": timedelta(days=365)}

METRIC_KEYS = ("cpu", "mem", "swap", "disk")


def r2(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def make_signature(method: str, uri: str, timestamp: str, access_key: str, secret_key: str) -> str:
    msg = f"{method} {uri}\n{timestamp}\n{access_key}"
    digest = hmac.new(secret_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class CloudInsight:
    def __init__(self, base_url: str, access_key: str, secret_key: str, region: str = "KR"):
        self.base_url = base_url.rstrip("/")
        self._ak = access_key
        self._sk = secret_key
        self.region = region

    def _request(self, method: str, path: str, body: dict | None = None):
        if (method, path) not in ALLOWED_ENDPOINTS:
            log_api("insight", f"{method} {path}", "BLOCKED")
            raise PermissionError(f"허용되지 않은 Cloud Insight 호출: {method} {path}")
        ts = str(int(time.time() * 1000))
        headers = {
            "x-ncp-apigw-timestamp": ts,
            "x-ncp-iam-access-key": self._ak,
            "x-ncp-apigw-signature-v2": make_signature(method, path, ts, self._ak, self._sk),
            "x-ncp-region_code": self.region,
            "Content-Type": "application/json",
        }
        detail = f'{body.get("metric", "")}/{body.get("interval", "")}' if body else ""
        for attempt in range(6):
            resp = requests.request(method, self.base_url + path, headers=headers,
                                    data=json.dumps(body) if body is not None else None, timeout=60)
            log_api("insight", f"{method} {path}", f"HTTP {resp.status_code}", detail)
            if resp.status_code != 429:
                break
            # 호출 제한: 대기 후 재시도 (서명 타임스탬프도 새로 만든다)
            time.sleep(2 * 2**attempt)
            ts = str(int(time.time() * 1000))
            headers["x-ncp-apigw-timestamp"] = ts
            headers["x-ncp-apigw-signature-v2"] = make_signature(method, path, ts, self._ak, self._sk)
        if resp.status_code != 200:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}")
        result = resp.json()
        # 게이트웨이가 HTTP 200 + 오류 본문({"code": 403, "msg": ...})을 주는 경우가 있다
        if isinstance(result, dict) and "code" in result and "msg" in result and "data" not in result:
            log_api("insight", f"{method} {path}", f"BODY code={result['code']}", str(result["msg"]))
            hint = " (이 키에 Cloud Insight 조회 권한이 없습니다)" if str(result["code"]) == "403" else ""
            raise RuntimeError(f"{method} {path} 거부: code={result['code']} msg={result['msg']}{hint}")
        return result

    def schema_list(self) -> list[dict]:
        return self._request("GET", SCHEMA_LIST_PATH)

    def query(self, cw_key: str, product_name: str, metric: str, dimensions: dict,
              start: datetime, end: datetime, interval: str, aggregation: str = "AVG",
              query_aggregation: str = "AVG"):
        body = {
            "timeStart": int(start.timestamp() * 1000),
            "timeEnd": int(end.timestamp() * 1000),
            "cw_key": cw_key,
            "productName": product_name,
            "metric": metric,
            "interval": interval,
            "aggregation": aggregation,
            "queryAggregation": query_aggregation,
            "dimensions": dimensions,
        }
        return self._request("POST", DATA_QUERY_PATH, body)

    def query_range(self, *, start: datetime, end: datetime, interval: str, **kw) -> list[tuple[int, float]]:
        """권장 구간 단위로 나눠 조회 후 합친다 (timestamp 중복 제거)."""
        points: dict[int, float] = {}
        cur = start
        while cur < end:
            nxt = min(cur + CHUNK[interval], end)
            res = self.query(start=cur, end=nxt, interval=interval, **kw)
            series = res.get("data", res) if isinstance(res, dict) else res
            for ts, val in series or []:
                if val is not None:
                    points[int(ts)] = float(val)
            cur = nxt
        return sorted(points.items())


def month_range(month: str, tzname: str | None = None) -> tuple[datetime, datetime]:
    from zoneinfo import ZoneInfo

    z = ZoneInfo(tzname) if tzname else tz()
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1, tzinfo=z)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=z)
    return start, end - timedelta(milliseconds=1)


def _client() -> CloudInsight:
    cfg = load_config()
    ak, sk = get_api_keys()
    return CloudInsight(cfg["cloud_insight"]["base_url"], ak, sk, cfg["ncloud"]["region"])


def find_cw_key(ci: CloudInsight, product_name: str, save_to=None) -> str:
    schemas = ci.schema_list()
    if save_to:
        save_to.write_text(json.dumps(schemas, ensure_ascii=False, indent=2), encoding="utf-8")
    # schema 목록의 prodName 은 "Server(VPC)", 데이터 조회의 productName 예시는 "System/Server(VPC)"
    short = product_name.split("/")[-1]
    exact = [s for s in schemas if s.get("prodName") in (product_name, short)]
    if len(exact) == 1:
        return exact[0]["cw_key"]
    raise RuntimeError(f"schema 목록에서 '{product_name}' 의 cw_key 를 하나로 찾지 못했습니다 "
                       f"(후보: {[s.get('prodName') for s in schemas if 'Server' in s.get('prodName', '')]})")


def empty_perf_row(dev: dict) -> dict:
    row = {"name": dev["name"], "cpu": dev.get("cpu"), "memory_gb": dev.get("memory_gb"),
           "storage": dev.get("storage"), "fs_name": dev.get("disk_mount", "")}
    for k in METRIC_KEYS:
        row[f"{k}_max"] = None
        row[f"{k}_avg"] = None
    return row


def collect_performance(cid: str, report_month: str, device_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Cloud Insight 로 전월 성능 지표 수집. (행 목록, 오류 메시지 목록) 반환. 예외를 밖으로 던지지 않는다."""
    ccfg = get_customer(cid)
    pcfg = ccfg["performance"]
    rows = [empty_perf_row(d) for d in device_rows if not d.get("missing")]
    errors: list[str] = []
    interval, agg = pcfg["interval"], pcfg.get("aggregation", "AVG")
    start, end = month_range(report_month)
    if now() - start > RETENTION[interval]:
        return rows, [f"{interval} 데이터 보존기간({RETENTION[interval].days}일)이 지나 {report_month} 조회 불가"]

    raw_dir = month_dir(cid, report_month, "raw/insight")
    try:
        ci = _client()
        cw_key = find_cw_key(ci, pcfg["product_name"], raw_dir / "schema_system_list.json")
    except Exception as e:  # noqa: BLE001 - 수집 실패는 보고서 점검 항목으로 남긴다
        return rows, [f"Cloud Insight 연결 실패: {e}"]

    devs = {d["name"]: d for d in device_rows if not d.get("missing")}
    tasks = []
    for row in rows:
        dev = devs[row["name"]]
        for key in METRIC_KEYS:
            spec = pcfg["metrics"][key]
            dims = {"instanceNo": dev["instance_no"]}
            for dk, dv in (spec.get("dimensions") or {}).items():
                dims[dk] = str(dv).format(**dev)
            tasks.append((row, key, spec["metric"], dims))

    def run(task):
        row, key, metric, dims = task
        pts = ci.query_range(cw_key=cw_key, product_name=pcfg["product_name"], metric=metric,
                             dimensions=dims, start=start, end=end, interval=interval, aggregation=agg)
        (raw_dir / f'{row["name"]}_{key}_{metric}_{interval}_{agg}.json').write_text(json.dumps(pts), encoding="utf-8")
        return task, pts

    workers = load_config()["cloud_insight"].get("max_workers", 4)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(run, t) for t in tasks]
        for f in futures:
            try:
                (row, key, metric, dims), pts = f.result()
            except Exception as e:  # noqa: BLE001
                errors.append(f"조회 실패: {e}")
                continue
            if not pts:
                errors.append(f'{row["name"]} {metric} {dims}: 데이터 없음 (미수집 지표일 수 있음)')
                continue
            vals = [v for _, v in pts]
            row[f"{key}_max"] = r2(max(vals))
            row[f"{key}_avg"] = r2(sum(vals) / len(vals))
    return rows, errors


# ---------- 1단계 검증용 CLI ----------
def main() -> None:
    ap = argparse.ArgumentParser(description="Cloud Insight 간격별 최대/평균 비교")
    ap.add_argument("--customer", default="skmns")
    ap.add_argument("--month", default="2026-09")
    ap.add_argument("--server", default="lbsbu02")
    ap.add_argument("--metric", default="avg_cpu_used_rto")
    ap.add_argument("--intervals", default="Min5,Min30,Hour2,Day1")
    a = ap.parse_args()

    ccfg = get_customer(a.customer)
    ci = _client()
    raw_dir = month_dir(a.customer, a.month, "raw/insight")
    cw_key = find_cw_key(ci, ccfg["performance"]["product_name"], raw_dir / "schema_system_list.json")
    servers = json.loads((month_dir(a.customer, a.month, "raw") / "servers.json").read_text(encoding="utf-8"))
    inst = {s["serverName"]: s["serverInstanceNo"]
            for s in servers["getServerInstanceListResponse"]["serverInstanceList"]}[a.server]
    start, end = month_range(a.month)
    print(f"{a.server} instanceNo={inst} cw_key={cw_key} metric={a.metric}")
    print(f'{"interval":<7}{"agg":<5}{"points":>7}{"최대":>8}{"평균":>8}')
    for interval in a.intervals.split(","):
        if now() - start > RETENTION[interval]:
            print(f"{interval:<7} 보존기간 경과")
            continue
        for agg in ("AVG", "MAX"):
            try:
                pts = ci.query_range(cw_key=cw_key, product_name=ccfg["performance"]["product_name"],
                                     metric=a.metric, dimensions={"instanceNo": inst},
                                     start=start, end=end, interval=interval, aggregation=agg)
            except RuntimeError as e:
                print(f"{interval:<7}{agg:<5} 오류: {e}")
                continue
            vals = [v for _, v in pts]
            if not vals:
                print(f"{interval:<7}{agg:<5}{0:>7}  데이터 없음")
                continue
            print(f"{interval:<7}{agg:<5}{len(vals):>7}{r2(max(vals)):>8.2f}{r2(sum(vals) / len(vals)):>8.2f}")


if __name__ == "__main__":
    main()
