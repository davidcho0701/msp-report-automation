"""조회 전용 ncloud CLI 래퍼.

모든 ncloud CLI 호출은 반드시 call() 을 거친다.
- ALLOWED_APIS 에 정확히 일치하지 않는 API 이름은 subprocess 실행 전에 PermissionError.
- 셸을 거치지 않는다 (shell=False, java -jar 직접 실행).
- 호출 시각/API/결과를 로그 파일에 남긴다 (키 값은 기록하지 않음).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .config import load_config, project_path

# 조회 전용 허용 목록. 추가할 때는 반드시 get* 조회 API 인지 확인할 것.
ALLOWED_APIS: frozenset[str] = frozenset(
    {
        "getRegionList",
        "getServerInstanceList",
        "getBlockStorageInstanceList",
        "getPublicIpInstanceList",
        "getMemberServerImageInstanceList",
        "getServerImageList",
    }
)

_PARAM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.]*$")
_PARAM_VALUE_RE = re.compile(r"^[A-Za-z0-9._:/\-]+$")

_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        cfg = load_config()
        # 테스트 등에서 NCP_API_LOG 로 로그 위치를 바꿀 수 있다.
        log_path = Path(os.environ.get("NCP_API_LOG") or project_path(cfg["paths"]["log_file"]))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("ncloud_api")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            h = logging.FileHandler(log_path, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s\t%(message)s"))
            lg.addHandler(h)
        _logger = lg
    return _logger


def log_api(source: str, api_name: str, status: str, detail: str = "") -> None:
    """CLI / REST 호출 공통 로그."""
    _get_logger().info("%s\t%s\t%s\t%s", source, api_name, status, detail)


def check_allowed(api_name: str) -> None:
    if not isinstance(api_name, str) or api_name not in ALLOWED_APIS:
        log_api("cli", repr(api_name), "BLOCKED")
        raise PermissionError(f"허용되지 않은 ncloud API 입니다: {api_name!r}")


def _build_args(api_name: str, params: dict[str, str | int]) -> list[str]:
    args: list[str] = []
    for k, v in params.items():
        v = str(v)
        if not _PARAM_NAME_RE.match(k):
            raise ValueError(f"잘못된 파라미터 이름: {k!r}")
        if not _PARAM_VALUE_RE.match(v) or v.startswith("-"):
            raise ValueError(f"잘못된 파라미터 값: {k}={v!r}")
        args += [f"--{k}", v]
    return args


def call(api_name: str, **params: str | int) -> dict:
    """화이트리스트 API 만 실행하고 JSON(dict) 을 반환한다."""
    check_allowed(api_name)

    cfg = load_config()["ncloud"]
    params.setdefault("regionCode", cfg["region"])
    extra = _build_args(api_name, params)

    cli_dir = Path(cfg["cli_dir"])
    cmd = [
        str(cli_dir / cfg["java_rel"]),
        "-jar",
        str(cli_dir / cfg["jar_rel"]),
        cfg["service"],
        api_name,
        *extra,
        "--output",
        "json",
    ]
    started = datetime.now()
    proc = subprocess.run(
        cmd,
        cwd=str(cli_dir),
        capture_output=True,
        shell=False,
        timeout=cfg.get("timeout_sec", 120),
    )
    elapsed = (datetime.now() - started).total_seconds()
    stdout = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        log_api("cli", api_name, f"FAIL rc={proc.returncode}", f"{elapsed:.1f}s")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"{api_name} 실패 (rc={proc.returncode}): {stderr or stdout}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        log_api("cli", api_name, "FAIL non-json", f"{elapsed:.1f}s")
        raise RuntimeError(f"{api_name} 응답이 JSON 이 아닙니다: {stdout[:500]}") from e
    log_api("cli", api_name, "OK", f"{elapsed:.1f}s")
    return data
