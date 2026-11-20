"""프로젝트 파일에 Access/Secret Key 가 들어 있지 않은지 검사한다.

.env 의 실제 값(있으면)과 ~/.ncloud/configure 의 키 값을 읽어, 그 문자열이
.env 이외의 파일에 존재하는지 확인한다. 키 값 자체는 출력하지 않는다.
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache"}


def secrets() -> set[str]:
    vals = set()
    for f in (ROOT / ".env", Path.home() / ".ncloud" / "configure"):
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"\s*[\w.]*(access_?key|secret_?key|PASSWORD)\w*\s*=\s*(\S+)", line, re.I)
                if m and len(m.group(2)) >= 8:
                    vals.add(m.group(2))
    for k in ("NCLOUD_ACCESS_KEY", "NCLOUD_SECRET_KEY", "SMTP_PASSWORD"):
        if len(os.environ.get(k, "")) >= 8:
            vals.add(os.environ[k])
    return vals


def main() -> int:
    vals = secrets()
    hits = []
    for p in ROOT.rglob("*"):
        if p.is_dir() or SKIP_DIRS & set(p.parts) or p.name == ".env":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(v in text for v in vals):
            hits.append(p.relative_to(ROOT))
    print(f"검사 키 수: {len(vals)}, 노출 파일: {len(hits)}")
    for h in hits:
        print("  !!", h)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
