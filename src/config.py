"""설정 로더. 비밀정보는 환경 변수 / .env 에서만 읽는다."""
from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
CUSTOMERS_DIR = PROJECT_ROOT / "config" / "customers"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except ImportError:
        pass


_load_env()


def load_config(path: Path | None = None) -> dict:
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_customers() -> dict[str, dict]:
    out = {}
    for p in sorted(CUSTOMERS_DIR.glob("*.yaml")):
        with open(p, encoding="utf-8") as f:
            c = yaml.safe_load(f)
        out[c["id"]] = c
    return out


def get_customer(cid: str) -> dict:
    customers = load_customers()
    if cid not in customers:
        raise KeyError(f"알 수 없는 고객사: {cid}")
    return customers[cid]


def project_path(rel: str) -> Path:
    return PROJECT_ROOT / rel


def tz() -> ZoneInfo:
    return ZoneInfo(load_config()["timezone"])


def now() -> datetime:
    return datetime.now(tz())


def customer_dir(cid: str) -> Path:
    return project_path(load_config()["paths"]["reports_dir"]) / cid


def month_dir(cid: str, report_month: str, sub: str | None = None, create: bool = True) -> Path:
    d = customer_dir(cid) / report_month
    if sub:
        d = d / sub
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_api_keys() -> tuple[str, str]:
    """Cloud Insight REST 용 키. .env 또는 환경 변수에서만 읽는다."""
    ak, sk = env("NCLOUD_ACCESS_KEY"), env("NCLOUD_SECRET_KEY")
    if not ak or not sk:
        raise RuntimeError(
            "NCLOUD_ACCESS_KEY / NCLOUD_SECRET_KEY 가 .env 에 없습니다."
        )
    return ak, sk
