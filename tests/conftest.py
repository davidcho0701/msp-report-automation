import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 테스트 호출은 운영 API 로그와 분리
os.environ["NCP_API_LOG"] = str(ROOT / "logs" / "api_calls.test.log")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _default_smtp_transport(monkeypatch):
    """실제 .env 의 MAIL_TRANSPORT=outlook 이 테스트에 새어 들어가 Outlook 이 실행되는 것을 막는다."""
    monkeypatch.setenv("MAIL_TRANSPORT", "smtp")
