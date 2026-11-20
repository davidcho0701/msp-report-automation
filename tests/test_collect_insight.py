import base64
import hashlib
import hmac
from unittest import mock

import pytest
import requests

from src import collect_insight as ci


def test_signature_matches_documented_format():
    msg = "GET /cw_fea/real/cw/api/schema/system/list\n1700000000000\nAK"
    expected = base64.b64encode(hmac.new(b"SK", msg.encode(), hashlib.sha256).digest()).decode()
    assert ci.make_signature("GET", ci.SCHEMA_LIST_PATH, "1700000000000", "AK", "SK") == expected


@pytest.mark.parametrize("method,path", [
    ("POST", "/cw_fea/real/cw/api/data/send"),            # SendData (쓰기)
    ("POST", "/cw_fea/real/cw/api/schema"),               # Custom schema 생성
    ("DELETE", ci.SCHEMA_LIST_PATH),
    ("PUT", ci.DATA_QUERY_PATH),
])
def test_write_endpoints_blocked(method, path):
    client = ci.CloudInsight("https://example.invalid", "AK", "SK")
    with mock.patch.object(requests, "request") as req:
        with pytest.raises(PermissionError):
            client._request(method, path, {})
        req.assert_not_called()


def _resp(status, payload):
    r = mock.Mock()
    r.status_code = status
    r.text = str(payload)
    r.json.return_value = payload
    return r


def test_throttle_429_is_retried(monkeypatch):
    monkeypatch.setattr(ci.time, "sleep", lambda s: None)
    client = ci.CloudInsight("https://example.invalid", "AK", "SK")
    seq = [_resp(429, {"error": "Throttle"}), _resp(429, {"error": "Throttle"}), _resp(200, [[1, 2.0]])]
    with mock.patch.object(requests, "request", side_effect=seq) as req:
        assert client._request("GET", ci.SCHEMA_LIST_PATH) == [[1, 2.0]]
        assert req.call_count == 3
        # 재시도마다 서명이 새로 만들어졌는지 (타임스탬프 헤더 존재)
        assert all("x-ncp-apigw-signature-v2" in c.kwargs["headers"] for c in req.call_args_list)


def test_error_body_with_http_200_raises_permission_hint():
    client = ci.CloudInsight("https://example.invalid", "AK", "SK")
    with mock.patch.object(requests, "request", return_value=_resp(200, {"code": 403, "msg": "View/getMetric"})):
        with pytest.raises(RuntimeError, match="조회 권한"):
            client._request("GET", ci.SCHEMA_LIST_PATH)


def test_month_range_kst():
    s, e = ci.month_range("2026-09", "Asia/Seoul")
    assert (s.year, s.month, s.day, s.hour) == (2026, 9, 1, 0)
    assert (e.month, e.day, e.hour, e.minute) == (9, 30, 23, 59)
