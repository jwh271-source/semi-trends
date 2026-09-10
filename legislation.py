"""
legislation.py — 대상 법령의 개정 사항을 공공데이터포털 Open API로 추적.

공공데이터포털 "법령공포정보" 서비스 (15000115) — lawSearchList.do 사용.
인증키는 .env 파일의 DATA_API_KEY에서 로드 (URL 디코딩된 원본 키).

필수 파라미터: serviceKey, target=law, query=법령명, numOfRows, pageNo

대상 법령 (8종):
  산업안전보건법, 중대재해 처벌 등에 관한 법률, 위험물안전관리법,
  화학물질관리법, 화학물질의 등록 및 평가 등에 관한 법률,
  대기환경보전법, 물환경보전법, 환경영향평가법

동작:
- 각 법령의 최신 공포일자/시행일자/제개정구분을 API에서 조회
- "현행" 법령의 개정 정보를 "법령 변경" 카드용 데이터로 저장
- build.py가 legislation.json을 읽어 법령 변경 섹션에 카드 생성

실행: python legislation.py
결과: site/legislation.json
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None
    import urllib.request

try:
    import pip_system_certs  # noqa: F401
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
OUT_FILE = ROOT / "site" / "legislation.json"
KST = timezone(timedelta(hours=9))
TIMEOUT = 15

# 공공데이터포털 법령공포정보 서비스 엔드포인트
API_URL = "https://apis.data.go.kr/1170000/law/lawSearchList.do"

# 대상 법령 (정확한 법령명)
TARGET_LAWS = [
    "산업안전보건법",
    "중대재해 처벌 등에 관한 법률",
    "위험물안전관리법",
    "화학물질관리법",
    "화학물질의 등록 및 평가 등에 관한 법률",
    "대기환경보전법",
    "물환경보전법",
    "환경영향평가법",  # 반도체 공장 인허가 관련
]


def get_api_key() -> str | None:
    """환경변수 DATA_API_KEY에서 인증키 로드. .env 파일 사용."""
    key = os.environ.get("DATA_API_KEY", "").strip()
    if not key:
        try:
            from dotenv import load_dotenv
            load_dotenv(str(ROOT / ".env"))
            key = os.environ.get("DATA_API_KEY", "").strip()
        except ImportError:
            pass
    # URL 인코딩된 키면 디코딩 (requests params가 다시 인코딩하므로 원본 필요)
    import urllib.parse
    return urllib.parse.unquote(key) if key else None


def fetch_law(key: str, law_name: str) -> dict | None:
    """법령 검색 API 호출 → 현행 법령의 공포/시행/개정 정보 추출."""
    params = {
        "serviceKey": key,
        "target": "law",
        "query": law_name,
        "numOfRows": "5",
        "pageNo": "1",
    }
    try:
        if requests is not None:
            r = requests.get(API_URL, params=params, timeout=TIMEOUT)
        else:
            import urllib.parse
            qs = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{API_URL}?{qs}")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                r_text = resp.read().decode("utf-8", errors="replace")
            r = type("R", (), {"text": r_text, "status_code": 200})()
        if r.status_code != 200:
            print(f"  [skip] {law_name}: HTTP {r.status_code}", file=sys.stderr)
            return None
        text = r.text
    except Exception as e:
        print(f"  [skip] {law_name}: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    # XML 파싱 (정규식 — 라이브러리 의존성 최소화)
    # 응답 구조: <LawSearch><law id="1"><법령일련번호>...</법령일련번호>...
    # <현행연혁코드>현행</현행연혁코드><법령명한글>...</법령명한글>
    # <공포일자>20260219</공포일자><시행일자>20260801</시행일자>
    # <제개정구분명>일부개정</제개정구분명><소관부처명>...</소관부처명>
    # <법령상세링크>/DRF/lawService.do?...</법령상세링크>
    if "resultCode>00" not in text and "success" not in text:
        print(f"  [skip] {law_name}: API 에러", file=sys.stderr)
        return None

    # "현행" 법령 찾기 (가장 최신)
    laws = re.findall(
        r"<law id=\"\d+\">(.*?)</law>", text, re.DOTALL
    )
    if not laws:
        return None
    # "현행" 상태인 첫 번째 법령
    for law_xml in laws:
        if "<현행연혁코드>현행</현행연혁코드>" not in law_xml:
            continue
        def field(tag):
            m = re.search(rf"<{tag}><!\[CDATA\[(.*?)\]\]></{tag}>", law_xml) \
                or re.search(rf"<{tag}>(.*?)</{tag}>", law_xml)
            return m.group(1) if m else ""
        def fmt_date(d):
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        prom = field("공포일자")
        eff = field("시행일자")
        link = field("법령상세링크")
        # HTML 엔티티(&amp; → &) 변환 + 절대 URL화
        link = link.replace("&amp;", "&") if link else ""
        full_link = f"https://www.law.go.kr{link}" if link else ""
        law_seq = field("법령일련번호")
        result = {
            "target_name": law_name,
            "law_name": field("법령명한글"),
            "law_id": field("법령ID"),
            "law_seq": law_seq,
            "promulgate_date": prom,
            "effect_date": eff,
            "promulgate_date_fmt": fmt_date(prom),
            "effect_date_fmt": fmt_date(eff),
            "amend_type": field("제개정구분명"),
            "ministry": field("소관부처명"),
            "detail_link": full_link,
            "status": "조회 완료",
        }
        # 개정 상세 정보 (이유 + 개정 조항) 추가 조회
        if law_seq:
            detail = fetch_amendment_detail(key, law_seq)
            result.update(detail)
        return result
    return None


def fetch_amendment_detail(key: str, mst: str) -> dict:
    """lawService.do에서 법령 본문을 조회해 개정 이유/개정 조항 추출.
    MST = 법령일련번호 (lawSearchList에서 얻은 값)."""
    svc_url = "https://www.law.go.kr/DRF/lawService.do"
    params = {
        "serviceKey": key,
        "OC": "sapphire_5",
        "target": "law",
        "MST": mst,
        "type": "XML",
    }
    try:
        if requests is not None:
            r = requests.get(svc_url, params=params, timeout=TIMEOUT)
        else:
            import urllib.parse
            qs = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{svc_url}?{qs}")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                r_text = resp.read().decode("utf-8", errors="replace")
            r = type("R", (), {"text": r_text, "status_code": 200})()
        if r.status_code != 200:
            return {}
        text = r.text
    except Exception as e:
        print(f"  [detail skip] MST={mst}: {type(e).__name__}", file=sys.stderr)
        return {}

    # 1) 제개정이유내용 추출 (개정 이유 전체)
    reason = ""
    reason_m = re.search(r"<제개정이유내용>(.*?)</제개정이유내용>", text, re.DOTALL)
    if reason_m:
        # CDATA 섹션에서 텍스트 추출
        reason = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", reason_m.group(1), flags=re.DOTALL)
        reason = re.sub(r"<[^>]+>", "", reason)
        reason = re.sub(r"\s+", " ", reason).strip()
        # ◇ 개정이유, ◇ 주요내용 등 마커 중심으로 정리
        # 너무 길면 500자로 제한
        reason = reason[:500]

    # 2) 조문시행일자문자열 추출 (개정된 조항 목록)
    amended_articles = ""
    art_m = re.search(r"<조문시행일자문자열>(.*?)</조문시행일자문자열>", text)
    if art_m:
        amended_articles = art_m.group(1).strip()

    # 3) 제개정구분
    amend_div = ""
    div_m = re.search(r"<제개정구분>(.*?)</제개정구분>", text)
    if div_m:
        amend_div = div_m.group(1).strip()

    return {
        "amend_reason": reason,
        "amended_articles": amended_articles,
        "amend_div": amend_div,
    }


def track() -> dict:
    api_key = get_api_key()
    if not api_key:
        print("[legislation] DATA_API_KEY 없음. .env 파일에 발급받은 인증키를 넣어주세요.")
        print("  발급: https://www.data.go.kr → 법령공포정보서비스 Open API 활용신청")
        return {
            "tracked_at": datetime.now(KST).isoformat(),
            "status": "no_api_key",
            "message": "공공데이터포털 API 인증키가 없습니다. .env에 DATA_API_KEY 설정 필요.",
            "laws": [],
        }
    print(f"[legislation] 대상 법령 {len(TARGET_LAWS)}종 추적 시작")
    # NEW 배지 기준: 최근 3일(KST) 이내에 공포 또는 시행된 법령.
    # 오늘만 보면 주말/공휴일에 공포된 법령이 월요일 6시 실행엔 NEW로 안 잡혀
    # 놓침. 3일 창으로 넓혀 금~월 공포분을 월요일에도 잡는다.
    # (이전 실행 결과와 비교하는 방식은 CI 환경에서 legislation.json이 매번
    #  새로 만들어지므로 항상 NEW가 되는 버그가 있음. 날짜 기준이 안전.)
    today_kst = datetime.now(KST)
    today_str = today_kst.strftime("%Y%m%d")
    three_days_ago_str = (today_kst - timedelta(days=3)).strftime("%Y%m%d")
    laws = []
    new_count = 0
    for name in TARGET_LAWS:
        print(f"  - {name}")
        info = fetch_law(api_key, name)
        if info:
            # 최근 3일 이내 공포 또는 시행된 법령만 NEW
            prom = info.get("promulgate_date", "")
            eff = info.get("effect_date", "")
            is_new = (
                (prom and three_days_ago_str <= prom <= today_str)
                or (eff and three_days_ago_str <= eff <= today_str)
            )
            info["is_new"] = is_new
            if is_new:
                new_count += 1
            new_tag = " [NEW]" if is_new else ""
            print(f"      {info['amend_type']} | 공포 {info['promulgate_date_fmt']} | 시행 {info['effect_date_fmt']}{new_tag}")
            laws.append(info)
        else:
            laws.append({"target_name": name, "status": "조회 실패"})
    return {
        "tracked_at": datetime.now(KST).isoformat(),
        "status": "ok",
        "total": len(laws),
        "new_count": new_count,
        "laws": laws,
    }


def main():
    result = track()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[legislation] 저장 -> {OUT_FILE} ({len(result.get('laws',[]))}건)")


if __name__ == "__main__":
    main()
