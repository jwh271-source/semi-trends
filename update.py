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

    # site/index.html 내용을 메모리에 보관 (rebase/충돌 중 손실 방지용 백업).
    site_content = SITE.read_bytes()

    # 안전 가드(완화): site/index.html 외의 dirty 파일이 있어도, site/index.html
    # "만" 안전하게 커밋하면 다른 변경사항에 영향을 주지 않는다. 따라서 push를
    # 중단하지 않고 site/index.html만 취급한다. 과거엔 다른 파일 1건만 있어도
    # push를 포기했는데, 이 때문에 매일 7시 LLM 덮어쓰기가 자주 실패했다.
    # 매일 7시 자동 실행 시점엔 코드 수정사항이 없으므로 정상 동작하고,
    # 개발 중 변경이 남은 날에도 LLM 버전은 확실히 Pages에 반영된다.

    # 1) 원격 동기화. dirty 파일이 있으면 rebase 충돌 위험이 있으므로,
    #    site/index.html의 변경분을 stash로 빼두고 pull 한 뒤 복구한다.
    #    site/articles.json 등 .gitignore 파일은 stash에 잡히지 않아 안전.
    code, out = g("stash", "push", "-m", "semi-trends-auto-push", "--", "site/index.html")
    stashed = code == 0 and "No local changes" not in out and "Saved" in out
    if code != 0 and "No local changes" not in out:
        # stash 실패(이미 스테이지됐거나 꼬인 경우) → 메모리 백업으로 복구 후 진행.
        print(f"[update] git stash 시도: {out}")
        SITE.write_bytes(site_content)
    code, out = g("pull", "--rebase", "--autostash", "origin", "main")
    if code != 0:
        print(f"[update] git pull --rebase 실패: {out}")
        print("  → 원격 동기화 실패. 메모리 백업으로 site/index.html 복구 후 계속.")
        # 실패해도 포기하지 않는다: 로컬 site/index.html을 메모리 백업으로 복구.
        SITE.write_bytes(site_content)
    if stashed:
        rc, ro = g("stash", "pop")
        if rc != 0:
            print(f"[update] git stash pop 실패(백업으로 복구): {ro}")
            SITE.write_bytes(site_content)

    # 2) site/index.html을 메모리 백업 기준으로 확정 (rebase가 건드렸을 수 있음).
    SITE.write_bytes(site_content)

    # 3) site/index.html만 스테이지 (다른 dirty 파일은 건드리지 않음).
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
    # push는 rebase 된 위에 단일 커밋이므로 fast-forward. 거부되면
    # (Actions가 먼저 push한 직후 등) pull --rebase 한 번 더 동기화 후 재시도.
    code, out = g("push", "origin", "HEAD:main")
    if code != 0:
        print(f"[update] git push 1차 실패, 동기화 후 재시도: {out}")
        rc2, ro2 = g("pull", "--rebase", "origin", "main")
        if rc2 == 0:
            # rebase 후 site/index.html이 보존되었는지 확인, 아니면 백업 복구.
            if SITE.read_bytes() != site_content:
                SITE.write_bytes(site_content)
                g("add", "site/index.html")
                g("commit", "-m", msg) if g("diff", "--cached", "--quiet")[0] != 0 else None
            code, out = g("push", "origin", "HEAD:main")
        else:
            print(f"[update] 2차 동기화 실패: {ro2}")
    if code != 0:
        print(f"[update] git push 최종 실패: {out}")
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
