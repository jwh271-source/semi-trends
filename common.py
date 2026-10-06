"""common.py — fetcher/legislation/build/update가 공유하는 상수·유틸.

4개 스크립트에 각각 복사돼 있던 KST 정의와 stdout 인코딩 방어를 한 곳으로.
- KST: NEW 배지/날짜 표시용 한국 표준시.
- reconfigure_utf8(): Windows 스케줄러 stdout 리다이렉트 시 기본 인코딩이
  cp949가 되어 유니코드 print가 크래시 나는 문제(2026-09-22~28 연속 실패
  사고)의 방어. 각 스크립트 main 진입 전에 호출한다.
"""

from __future__ import annotations

import sys
from datetime import timezone, timedelta

KST = timezone(timedelta(hours=9))


def reconfigure_utf8() -> None:
    """stdout/stderr를 UTF-8로 강제. 이미 리다이렉트된 스트림도 안전하게."""
    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
