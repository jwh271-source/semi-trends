"""
update.py — fetcher + legislation + build 한 번에 실행. 매일 7시 로컬 스케줄러가 호출.

실행: python update.py

로컬 자동 배포(사내 LLM 경로):
- 로컬 스케줄러(매일 7시)가 update.py를 실행한다.
- LLM_ENABLED=1(사내 LLM 사용)일 때만 fetcher+build 완료 후
  site/index.html을 git commit+push → GitHub Pages가 LLM 버전을 서비스.
- GitHub Actions 환경(GITHUB_ACTIONS=true)에서는 여기서 push하지 않는다.
  deploy.yml이 자체 push 단계를 갖고 있으므로 이중 커밋 방지.
- LLM 없이 키워드 방식으로 로컬에서 돌릴 때도 push하지 않는다(의도치 않은
  Pages 덮어쓰기 방지). Pages는 그날 Actions가 만든 키워드 버전이 서비스됨.
"""

from __future__ import annotations

import sys
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable or "python"

# GitHub Actions 러너 환경 감지. CI에선 로컬 전용 push 로직을 건너뛴다.
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


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


def is_llm_enabled() -> bool:
    """사내 LLM을 '실제로 사용 가능한' 상태인지 판단.
    LLM_ENABLED=1 이고 LLM_TOKEN이 비어있지 않아야 True.
    토큰이 없으면 fetcher.py의 _llm_call이 즉시 폴백(키워드 방식)하므로,
    이땐 LLM 버전이 아니라 키워드 버전 → Pages push를 안 해 Actions 결과 유지.
    fetcher.py와 동일하게 .env를 먼저 로드한 뒤 환경변수를 읽는다."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    val = os.environ.get("LLM_ENABLED", "0")
    token = os.environ.get("LLM_TOKEN", "").strip()
    return val == "1" and bool(token)


def git_commit_push_site() -> None:
    """로컬 LLM 모드에서만 site/index.html을 커밋+푸시 → Pages에 LLM 버전 반영.

    동작 조건:
    - GitHub Actions 환경이 아니고 (CI는 deploy.yml이 자체 push)
    - LLM_ENABLED=1 (사내 LLM 사용)일 때만.
    그 외(키워드 방식 로컬 실행)는 push 안 함 → Pages는 그날 Actions 결과 유지.

    실패해도 update.py 종료코드에 영향 안 줌(배포는 부가 기능).
    site/articles.json 등은 .gitignore로 무시되므로 index.html만 들어간다."""
    if IS_GITHUB_ACTIONS:
        print("[update] GitHub Actions 환경 — 로컬 push 생략 (deploy.yml이 담당)")
        return
    if not is_llm_enabled():
        print("[update] LLM 미사용(키워드 방식) — Pages push 생략. Actions 결과가 서비스됨.")
        return

    SITE = ROOT / "site" / "index.html"
    if not SITE.exists():
        print("[update] site/index.html 없음 — push 생략")
        return

    def g(*args: str) -> tuple[int, str]:
        r = subprocess.run(
            ["git", *args], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return r.returncode, (r.stdout + r.stderr).strip()

    print("[update] Pages 자동 배포(LLM 버전) — git commit+push ...")
    # 안전 가드: 작업 디렉토리에 site/index.html 외의 수정사항(개발 중 변경)
    # 이 있으면 pull --rebase 시 충돌/손실 위험이 있다. 그런 경우 push를
    # 건너뛰고 안전하게 종료 → 개발자가 수동으로 커밋/푸시하게 둔다.
    # 매일 7시 스케줄러 실행 시점엔 코드 수정사항이 없으므로 정상 동작한다.
    code, out = g("status", "--porcelain")
    if code != 0:
        print(f"[update] git status 확인 실패: {out}")
        return
    dirty = [ln for ln in out.splitlines() if ln.strip()]
    other_dirty = [ln for ln in dirty if "site/index.html" not in ln]
    if other_dirty:
        print(f"[update] 작업 디렉토리에 site/index.html 외 수정사항 {len(other_dirty)}건 — 자동 push 중단.")
        print("  → 개발 중 변경이 있으면 수동 커밋/푸시하세요. Pages는 이전 버전 유지.")
        return
    # 원격 동기화 (clean tree → 안전).
    code, out = g("pull", "--rebase", "origin", "main")
    if code != 0:
        print(f"[update] git pull --rebase 실패: {out}")
        print("  → 원격 동기화 실패. Pages는 이전 버전 유지.")
        return
    # 스테이지 + 변경사항 체크.
    code, out = g("add", "site/index.html")
    if code != 0:
        print(f"[update] git add 실패: {out}")
        return
    code, out = g("diff", "--cached", "--quiet")
    if code == 0:
        print("[update] site/index.html 변경 없음 — 커밋 생략")
        return
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    msg = f"chore(local): daily LLM update {ts}"
    code, out = g("commit", "-m", msg)
    if code != 0:
        print(f"[update] git commit 실패: {out}")
        return
    code, out = g("push", "origin", "HEAD:main")
    if code != 0:
        print(f"[update] git push 실패: {out}")
        print("  → 인증/네트워크 확인. Pages는 이전 버전 유지.")
        return
    print(f"[update] Pages 푸시 완료: {msg}")
    print("  → https://jwh271-source.github.io/semi-trends/ 에 LLM 버전 반영 (수 분 내 갱신)")


def main():
    print("========================================")
    print(" semi-trends 업데이트 시작")
    print("========================================")
    run("fetcher.py")
    run("legislation.py")  # 법령 개정 추적 (인증키 있을 때만 동작)
    run("build.py")
    git_commit_push_site()  # 로컬 LLM 모드에서만 Pages 자동 배포
    print("\n[update] 모두 완료. site/index.html 확인하세요.")


if __name__ == "__main__":
    main()
