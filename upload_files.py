"""upload_files.py — 지정한 파일들을 GitHub Contents API로 업로드 (일회성).

사내망에서 git push가 403 차단되므로 update.py의 _contents_api_upload를
재활용해 지정 파일을 원격 main에 커밋. 업로드 후 Pages 재배포는 workflow_dispatch로.

기존 upload_pwa.py를 일반화 — FILES 대신 명령행 인자로 파일 목록을 받아
아이콘/PWA 자산 교체, LLM 버전 복구 등 재사용.

실행:
  python upload_files.py site/index.html site/sw.js ...
  python upload_files.py --msg "커밋 메시지" site/icons/icon-192.png ...

제약: 파일당 70KB(사내망 업로드 한계). 초과 시 해당 파일에서 중단.
"""
import sys
from pathlib import Path

from common import reconfigure_utf8

reconfigure_utf8()

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from update import _contents_api_upload, _get_gh_token  # noqa: E402

REPO = "jwh271-source/semi-trends"
DEFAULT_MSG = "chore: file upload via Contents API (upload_files.py)"
MAX_BYTES = 71680  # 사내망 GitHub 업로드 한계 70KB


def main(argv: list[str]) -> int:
    msg = DEFAULT_MSG
    files: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--msg":
            msg = next(it, DEFAULT_MSG)
        else:
            files.append(arg)
    if not files:
        print("사용법: python upload_files.py [--msg MSG] <파일1> [파일2 ...]")
        return 1

    token = _get_gh_token()
    if not token:
        print("[upload] GitHub 토큰 없음 (GH_TOKEN 또는 gh auth) — 중단")
        return 1

    for rel in files:
        p = ROOT / rel
        if not p.exists():
            print(f"[skip] {rel} 없음")
            continue
        content = p.read_bytes()
        if len(content) > MAX_BYTES:
            print(f"[over] {rel} ({len(content)/1024:.0f}KB) 70KB 초과 — 중단")
            return 1
        ok, info = _contents_api_upload(token, REPO, rel, content, msg)
        print(f"  [{'ok' if ok else 'fail'}] {rel} ({len(content)/1024:.1f}KB) -> {info}")
        if not ok:
            return 1
    print("[upload] 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
