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

# Windows 스케줄러 실행 시 stdout이 로그 파일로 리다이렉트되면 기본 인코딩이
# cp949가 되어 유니코드 문자(—, 한글 특수문자 등) print에서 크래시 난다
# (2026-09-22 7시 실행 실패 사례). UTF-8로 강제한다.
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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


def _get_gh_token() -> str | None:
    """GitHub API 토큰 확보. 우선순위:
    1) 환경변수 GH_TOKEN / GITHUB_TOKEN
    2) .env 의 GH_TOKEN
    3) gh CLI (gh auth token) — 사내망에서 인증된 경우."""
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        val = os.environ.get("GH_TOKEN", "").strip()
        if val:
            return val
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _contents_api_upload(token: str, repo: str, remote_path: str,
                         content_bytes: bytes, message: str,
                         max_retries: int = 3) -> tuple[bool, str]:
    """GitHub Contents API 로 파일 1개 업로드(PUT). 성공 여부와 메시지 반환.
    사내망에서 git push(403) 대신 사용 — 70KB(요청 ~95KB) 이하만 통과.
    이미 원격에 파일이 있으면 SHA 를 조회해 갱신, 없으면 신규 생성.
    일시적 네트워크 오류(10053 연결 강제 종료, HTTP 5xx/429)는
    재시도(5s/10s 백오프)로 극복한다."""
    import base64
    import json as _json
    import time
    import urllib.request
    import urllib.error

    last_err = "no attempt"
    for attempt in range(1, max_retries + 1):
        ok, info = _contents_api_upload_once(token, repo, remote_path,
                                             content_bytes, message)
        if ok:
            return True, info
        last_err = info
        # 영구 실패(4xx 인증/권한 등)는 재시도해도 무의미 — 즉시 중단.
        if info.startswith("SHA 조회 실패 HTTP 4") or info.startswith("HTTP 4"):
            break
        if attempt < max_retries:
            wait = 5 * attempt
            print(f"  [retry] {remote_path} 시도 {attempt} 실패({info}) — {wait}s 후 재시도")
            time.sleep(wait)
    return False, last_err


def _contents_api_upload_once(token: str, repo: str, remote_path: str,
                              content_bytes: bytes, message: str) -> tuple[bool, str]:
    """Contents API PUT 1회 실행 (재시도 래퍼에서 호출)."""
    import base64
    import json as _json
    import urllib.request
    import urllib.error

    api_url = f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            sha = _json.loads(resp.read().decode("utf-8")).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return False, f"SHA 조회 실패 HTTP {e.code}"
    except Exception:
        pass
    content_b64 = base64.b64encode(content_bytes).decode()
    payload = {"message": message, "content": content_b64}
    if sha:
        payload["sha"] = sha
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(api_url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            r = _json.loads(resp.read().decode("utf-8"))
            return True, r.get("commit", {}).get("sha", "")[:12]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:120]
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def git_commit_push_site() -> None:
    """로컬 LLM 모드에서만 LLM 빌드 결과를 GitHub Pages에 반영.

    사내망에서 git push 는 403 차단되므로, GitHub Contents API(PUT)로
    site/ 하위 파일들을 직접 업로드한다. 사내 보안은 70KB(요청 ~95KB)
    이하 파일만 업로드를 허용하므로, index.html 은 카드에 제목만 표시하고
    전체 요약은 summaries-N.json 으로 분할(build.py 가 생성).

    동작 조건:
    - GitHub Actions 환경이 아니고 (CI는 deploy.yml이 자체 빌드/배포)
    - LLM_ENABLED=1 (사내 LLM 사용)일 때만.
    그 외(키워드 방식 로컬 실행)는 업로드 안 함 → Pages는 그날 Actions 결과 유지.

    실패해도 update.py 종료코드에 영향 안 줌(배포는 부가 기능)."""
    if IS_GITHUB_ACTIONS:
        print("[update] GitHub Actions 환경 — 로컬 업로드 생략 (deploy.yml이 담당)")
        return
    if not is_llm_enabled():
        print("[update] LLM 미사용(키워드 방식) — Pages 업로드 생략. Actions 결과가 서비스됨.")
        return

    token = _get_gh_token()
    if not token:
        print("[update] GitHub 토큰 없음 (GH_TOKEN 환경변수 또는 gh auth 미인증) — 업로드 생략")
        return

    repo = os.environ.get("GH_REPO", "jwh271-source/semi-trends")
    site_dir = ROOT / "site"
    if not (site_dir / "index.html").exists():
        print("[update] site/index.html 없음 — 업로드 생략")
        return

    import glob
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))
    ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    msg_base = f"chore(local): daily LLM update {ts}"

    # 업로드 대상: index.html + summaries-index.json + summaries-1..N.json
    # 모두 70KB 이하이도록 build.py 가 분할 생성. articles.json 등은 제외.
    # 주의: glob "summaries-*.json"은 summaries-index.json도 매칭하므로
    # index.json을 먼저 넣고 glob 결과에서는 제외해 중복 업로드를 막는다.
    targets = [site_dir / "index.html", site_dir / "summaries-index.json"]
    targets += sorted(p for p in site_dir.glob("summaries-*.json")
                      if p.name != "summaries-index.json")
    upload_files = [p for p in targets if p.exists()]

    # 70KB 초과 파일이 섞여 있으면 LLM 버전 배포가 불완전해진다(최신 기사 누락).
    # 업로드 자체도 사내망에서 차단되므로, 시작 전에 전체 검증해 명확히 경고.
    oversized = [p for p in upload_files if p.stat().st_size > 71680]
    for p in oversized:
        print(f"  [over] {p.name} ({p.stat().st_size/1024:.0f}KB) — 70KB 초과! build.py 분할 확인 필요")
    if oversized:
        print("[update] 70KB 초과 파일 존재 — 업로드 중단. Pages는 이전 버전 유지.")
        return

    print(f"[update] Pages 자동 배포(LLM 버전) — Contents API 업로드 {len(upload_files)}개 파일 ...")
    ok = 0
    failed = []
    for p in upload_files:
        remote_path = str(p.relative_to(ROOT)).replace("\\", "/")
        content = p.read_bytes()
        size_kb = len(content) / 1024
        success, info = _contents_api_upload(token, repo, remote_path, content, msg_base)
        if success:
            print(f"  [ok] {remote_path} ({size_kb:.0f}KB) -> {info}")
            ok += 1
        else:
            print(f"  [fail] {remote_path} ({size_kb:.0f}KB) — {info}")
            failed.append(p.name)

    if not failed:
        print(f"[update] 업로드 완료: {ok}개 파일")
    elif ok > 0:
        print(f"[update] 부분 업로드: {ok}/{len(upload_files)}개 — 실패: {', '.join(failed)}")
        print("[update] 불일치 배포 방지 — Pages 배포 트리거 생략. 재실행 필요.")
        return
    else:
        print("[update] 업로드 실패 — 토큰/네트워크 확인. Pages는 이전 버전 유지.")
        return

    # LLM 버전 업로드 성공 후 Pages 배포 트리거.
    # 본 설계 의도: 6시 cron 은 항상 키워드 빌드(보험)를 배포하므로, 로컬 7시 LLM
    # 업로드 직후 deploy.yml 을 수동 트리거(workflow_dispatch)해야 LLM 버전이
    # Pages 에 즉시 반영된다. deploy.yml 은 수동 트리거 + 최근 LLM 커밋 감지 시
    # 빌드를 생략하고 올라온 site/ 를 그대로 배포(덮어쓰기).
    trigger_pages_deploy(token, repo)


def trigger_pages_deploy(token: str, repo: str) -> None:
    """LLM 업로드 후 GitHub Actions(deploy.yml)를 수동 트리거해 Pages 즉시 갱신.
    gh CLI 가 있으면 gh workflow run, 없으면 GitHub API POST /actions/workflows."""
    workflow = os.environ.get("DEPLOY_WORKFLOW", "deploy.yml")
    # 1) gh CLI 시도 (사내망에선 gh 인증 있음)
    try:
        r = subprocess.run(
            ["gh", "workflow", "run", workflow, "-R", repo],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        if r.returncode == 0:
            print(f"[update] Pages 배포 트리거 완료 (gh workflow run {workflow})")
            print("  -> 수 분 내 Pages 갱신: https://jwh271-source.github.io/semi-trends/")
            return
        print(f"[update] gh workflow run 실패: {(r.stderr or r.stdout).strip()[:80]}")
    except Exception as e:
        print(f"[update] gh CLI 없음/실패: {e}")
    # 2) GitHub API 폴백 (workflow_dispatch 이벤트 발생)
    try:
        import urllib.request
        import json as _json
        # workflow ID 또는 파일명으로 트리거 (ref=main)
        url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
        req = urllib.request.Request(
            url,
            data=_json.dumps({"ref": "main"}).encode(),
            method="POST",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=30)
        print(f"[update] Pages 배포 트리거 완료 (API dispatch {workflow})")
    except Exception as e:
        code = getattr(e, "code", "?")
        print(f"[update] Pages 배포 트리거 실패 (HTTP {code}) — 수동으로 Actions 실행 필요")
        print("  -> GitHub Actions 탭에서 'Build & Deploy' 수동 실행(Run workflow)")



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
