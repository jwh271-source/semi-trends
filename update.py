"""
update.py — fetcher + legislation + build 한 번에 실행. 매일 6시 스케줄러가 호출.

실행: python update.py
"""

from __future__ import annotations

import sys
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable or "python"


def run(script: str):
    print(f"\n=== {script} 시작 ===", flush=True)
    # -u: 자식 파이썬의 stdout 버퍼링 해제 (Actions 로그 실시간 출력)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    r = subprocess.run(
        [PYTHON, "-u", str(ROOT / script)],
        cwd=str(ROOT),
        env=env,
    )
    if r.returncode != 0:
        print(f"[update] {script} 실패 (exit {r.returncode})", flush=True)
        sys.exit(r.returncode)
    print(f"=== {script} 완료 ===", flush=True)


def main():
    print("========================================")
    print(" semi-trends 업데이트 시작")
    print("========================================")
    run("fetcher.py")
    run("legislation.py")  # 법령 개정 추적 (인증키 있을 때만 동작)
    run("build.py")
    print("\n[update] 모두 완료. site/index.html 확인하세요.")


if __name__ == "__main__":
    main()
