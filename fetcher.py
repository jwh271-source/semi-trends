"""
fetcher.py — RSS 피드 수집 + 키워드 기반 주제 분류 + 영문 기사 한국어 번역.

실행: python fetcher.py
결과: site/articles.json (분류된 기사 목록)

분류 카테고리:
  environmental  반도체 업계 전반의 환경 이슈 (ESG·탈탄소·지속가능성·기후)
  regulation    규제 (환경·물사용·배출물질 통합)
  legislation  법령 변경 (법령 개정·제정·시행)
  pou_scrubber POU Scrubber / 1차 Scrubber (Point-of-Use 스크러버·배기 처리 기술 동향)
  other         기타 반도체 관련

설계 결정:
- 영문 기사는 제목+요약을 한국어로 자동 번역(deep-translator Google Translate)한다.
  API 키 불필요, 오프라인 환경에서는 번역 실패 시 원문 그대로 보관.
- 번역은 네트워크 호출이므로 기사당 약간의 지연 발생. 대량 기사에 대해
  제목은 번역, 요약은 긴 경우에만 번역(300자 이하 원문 유지 옵션).
- 한국어 출처는 그대로 사용.
- require_keyword=true 인 출처는 반도체/환경 키워드가 있는 기사만 수집하여 노이즈 제거.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

# 회사/프록시 환경에서 SSL 검증 실패 방지
try:
    import pip_system_certs  # noqa: F401
except ImportError:
    pass

try:
    import requests
except ImportError:
    requests = None
    import urllib.request

# .env 파일에서 환경변수 로드 (LLM 토큰 등). python-dotenv 없으면 시스템 환경변수 사용.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
OUT_FILE = ROOT / "site" / "articles.json"

# 한국 표준시 (New 배지 날짜 비교용)
KST = timezone(timedelta(hours=9))

TIMEOUT = 15
USER_AGENT = "semi-trends-bot/1.0 (+local research aggregator)"

# 수집 날짜 상한: 최근 N일 이내 기사만 수집(그 이전은 노이즈).
# 환경변수 MAX_AGE_DAYS 로 조정 가능(기본 60일 ≒ 2개월).
# IEEE 등 일부 출처가 4개월 전 기사까지 주므로 너무 오래된 동향은
# "오늘의 브리핑"에 섞이지 않도록 컷오프.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "60"))

# =========================================================================
# 번역 설정
# =========================================================================
# 번역 켜기/끄기: 환경변수 TRANSLATE (기본 "1" = 켜짐).
#   "0" 이면 번역 안 하고 영문 원문 그대로 표시 (빠른 배포용).
# 타임아웃: 환경변수 TRANSLATE_TIMEOUT (기본 7초).
#   → GitHub Actions 서버에서 Google 비공개 엔드포인트가 차단되어 응답이
#      안 올 때 15초씩 대기하며 43분 멈춤이 발생했음. 7초면 충분하고
#      막히면 빠르게 다음 엔진으로 넘어감.
TRANSLATE_ENABLED = os.environ.get("TRANSLATE", "1") not in ("0", "false", "False")
TRANSLATE_TIMEOUT = int(os.environ.get("TRANSLATE_TIMEOUT", "7"))

# =========================================================================
# LLM 설정 (사내 LLM — 로컬에서만 사용, GitHub Actions는 키워드 방식)
# =========================================================================
# LLM 모드 켜기/끄기: 환경변수 LLM_ENABLED (기본 "0" = 끄짐).
#   "1"이면 LLM으로 기사 분류 + 요약 + 키워드 추천.
#   로컬 .env에 LLM_ENABLED=1 설정 시 작동. GitHub Actions에는 설정 안 함.
LLM_ENABLED = os.environ.get("LLM_ENABLED", "0") == "1"
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://common.llm.skhynix.com")
LLM_TOKEN = os.environ.get("LLM_TOKEN", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "GLM-5.2")  # 사내 허용 모델
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "20"))  # thinking 비활성화로 응답 ~5초, 여유 20초
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1000"))  # thinking 없이 text만 → 1000면 충분

# --- 번역 엔진들 (우선순위: Google 다중 엔드포인트 → MyMemory) ---
# 모든 엔진에 TRANSLATE_TIMEOUT 초 타임아웃 → 어느 하나 막혀도 멈추지 않음.
# 전부 실패하면 호출자가 원문 유지.
#
# 실측(2026-09-09 GitHub Actions 로그): Google 비공개 엔드포인트가 Actions IP에서
# rate-limit/차단되어 영문 기사 34건 중 16건(47%)만 번역, 나머지는 MyMemory quota
# 소진 후 원문 유지. deep-translator의 GoogleTranslator는 한국어 미지원,
# Microsoft/Libre는 API 키 필요 → 무료 한국어 폴백은 Google 비공개 + MyMemory뿐.
# 따라서 Google 엔드포인트를 여러 variant(client/host 조합)로 순차 시도해
# 차단 회피 확률을 높이고, 엔진 간 짧은 지연으로 rate-limit 완화.
_MYMEMORY = None  # deep-translator 인스턴스 캐시 (지연 초기화)

# Google 비공개 번역 엔드포인트 variant. host/client 조합별로 차단 정책이 달라
# 한 variant가 막혀도 다음 variant는 성공할 확률을 가짐.
_GOOGLE_VARIANTS = [
    ("https://translate.googleapis.com/translate_a/single", "gtx"),
    ("https://translate.googleapis.com/translate_a/single", "dict-chrome-ex"),
    ("https://translate.google.com/translate_a/single", "gtx"),
    ("https://translate.googleapis.com/translate_a/single", "webx"),
]
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _get_mymemory():
    """MyMemory 번역기 인스턴스. 실패 시 False."""
    global _MYMEMORY
    if _MYMEMORY is not None:
        return _MYMEMORY
    try:
        from deep_translator import MyMemoryTranslator
        _MYMEMORY = MyMemoryTranslator(source='en-GB', target='ko-KR')
    except Exception:
        _MYMEMORY = False
    return _MYMEMORY


def _translate_google(text: str) -> str | None:
    """Google 비공개 엔드포인트 번역. 여러 variant 순차 시도. 전부 실패 시 None.
    정식 API가 아님 — 데이터센터 IP(GitHub Actions 등)는 rate-limit될 수 있어
    client/host 조합을 바꿔가며 차단을 회피한다. 브라우저 UA로 자동화 판별 완화."""
    if requests is None:
        return None
    for url, client in _GOOGLE_VARIANTS:
        try:
            params = {"client": client, "sl": "en", "tl": "ko", "dt": "t", "q": text}
            r = requests.get(
                url, params=params,
                headers={"User-Agent": _BROWSER_UA},
                timeout=TRANSLATE_TIMEOUT,
            )
            if r.status_code != 200:
                continue
            # data[0] = 번역 세그먼트 배열 [[번역문, 원문, ...], ...]
            parts = [seg[0] for seg in r.json()[0] if seg and seg[0]]
            out = "".join(parts).strip() or None
            if out:
                return out
        except Exception:
            continue
    return None


def _translate_mymemory(text: str) -> str | None:
    """MyMemory 번역 (deep-translator). 일 약 5000어 제한. 실패 시 None."""
    tr = _get_mymemory()
    if not tr:
        return None
    try:
        return tr.translate(text[:4800]) or None
    except Exception:
        return None


def _try_translate(text: str) -> str | None:
    """번역 엔진들을 우선순위대로 시도. 성공 시 번역문, 전부 실패 시 None.
    번역 비활성화 시 None 반환 → 호출자가 원문 유지.
    Google(다중 variant) → MyMemory 순서."""
    if not TRANSLATE_ENABLED:
        return None
    for engine in (_translate_google, _translate_mymemory):
        out = engine(text)
        if out:
            return out
    return None


def translate_title(title: str) -> str:
    """기사 제목 번역 — 필수. 번역 실패해도 원문 반환(절대 빈값 안 됨).
    제목은 짧아서 번역 성공률이 높고, 사용자가 기사를 식별하는 핵심이므로
    끝까지 시도함. (한 엔진당 최대 7초×3엔진 = 21초, 제목은 보통 1~2초)"""
    if not title:
        return title
    out = _try_translate(title[:2000])
    return out if out else title


def translate_summary(summary: str) -> str:
    """기사 요약 번역 — 최대한 시도. 실패 시 원문 유지.
    요약은 길어서 시간이 더 걸리지만, 7초 타임아웃×3엔진으로
    한 기사당 최대 21초. 영문 기사 80건이면 최악의 경우 약 28분."""
    if not summary:
        return summary
    out = _try_translate(summary[:1500])
    return out if out else summary


# =========================================================================
# LLM 모듈 (사내 LLM: GLM-5.2)
# — 로컬에서만 작동. GitHub Actions는 키워드 방식 사용.
# — 분류(classify), 요약(summarize), 키워드 추천(extract_keywords) 지원.
# =========================================================================

def _llm_call(user_prompt: str, system_prompt: str = "") -> str | None:
    """사내 LLM API 호출. 응답의 text 부분만 반환. 실패/비활성 시 None.
    응답 구조: content 배열에 {type:'thinking'|'text'} 섞여 있음.
    thinking(사고 과정)은 제외하고 type=='text'만 합쳐서 반환."""
    if not LLM_ENABLED or not LLM_TOKEN or requests is None:
        return None
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    try:
        r = requests.post(
            f"{LLM_BASE_URL}/v1/messages",
            headers={
                "Authorization": f"Bearer {LLM_TOKEN}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": LLM_MODEL,
                "max_tokens": LLM_MAX_TOKENS,
                "messages": messages,
                # GLM-5.2는 기본 thinking(확장 사고) 모드. thinking 없이 바로 text 답을
                # 내게 비활성화 — 기사당 15~60초 → 약 5초로 3~10배 속도 향상.
                # thinking 켜면 max_tokens을 thinking이 전부 소비해 text가 안 나옴.
                "thinking": {"type": "disabled"},
            },
            timeout=LLM_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        # content 배열에서 type=='text'인 것만 추출 (thinking 제외)
        parts = [c.get("text", "") for c in r.json().get("content", []) if c.get("type") == "text"]
        return "".join(parts).strip() or None
    except Exception:
        return None


# LLM 분류용 카테고리 정의 (키워드 방식의 CATEGORIES와 동일한 키 사용)
LLM_CATEGORIES = {
    "semiconductor": "반도체 (팹/파운드리/메모리/장비/기업 실적/증설/투자)",
    "environmental": "환경 (ESG/탄소/지속가능성/기후/온실가스/재생에너지)",
    "safety": "안전 (화재/폭발/화학물질 사고/산업안전/중대재해/작업자 안전)",
    "regulation": "규제 (물 사용/폐수/배출물질/화학물질/환경 인허가/법적 리스크)",
}


def llm_process_article(title: str, summary: str, is_english: bool = False) -> dict | None:
    """LLM으로 기사 분류+요약+키워드 추출(+영문 번역)을 한 번에 처리.
    반환: {category, title_ko, summary, keywords} 또는 None.
    - is_english=True면 영문 제목도 한국어로 번역(title_ko)하여 반환.
      (Google/MyMemory 번역 대신 LLM이 직접 번역+분류+요약을 한 번에 처리)
    - 분류+요약+키워드+번역을 개별 LLM 호출하는 대신 한 번에 JSON으로 받아 속도 향상.
    - 응답이 JSON이 아니면 부분 파싱."""
    if not LLM_ENABLED:
        return None
    cats = "\n".join(f"- {k}: {v}" for k, v in LLM_CATEGORIES.items())
    # 영문 기사: title_ko 필드 추가 요청 (한국어 번역 제목)
    title_ko_field = ""
    if is_english:
        title_ko_field = ' "title_ko": "제목의 한국어 번역",\n'
    prompt = (
        f"아래 기사를 분석해 JSON 형식으로 답하세요. 마크다운 없이 순수 JSON만.\n"
        f"반도체 제조업 공장 환경·안전·규제 특화 사이트. 일반 경제/증시/정치 뉴스는 category를 none으로.\n"
        f"summary와 title_ko(영문인 경우)는 반드시 자연스러운 한국어로 작성.\n\n"
        f"형식:\n"
        f'{{"category": "semiconductor|environmental|safety|regulation|none",\n'
        f'{title_ko_field}'
        f' "summary": "한국어 3~4문장 요약 (핵심 사실/수치/영향, 추측 제외)",\n'
        f' "keywords": ["키워드1", "키워드2", "키워드3"]}}\n\n'
        f"카테고리:\n{cats}\n\n"
        f"기사:\n제목: {title}\n본문: {summary[:1500]}"
    )
    out = _llm_call(prompt)
    if not out:
        return None
    # JSON 파싱 시도 — 실패하면 텍스트에서 부분 추출
    try:
        # 응답에서 JSON 블록 추출 (```json ... ``` 또는 { ... })
        m = re.search(r"\{.*\}", out, re.DOTALL)
        raw = m.group(0) if m else out
        data = json.loads(raw)
        category = str(data.get("category", "")).strip().lower()
        # none이면 관련 없음 (정확히 "none"이거나 빈 값)
        if category == "none" or category == "" or "없" in category:
            return {"category": "", "title_ko": "", "summary": "", "keywords": []}
        # 유효한 카테고리 키인지 확인 (부분 매칭 허용: "semiconductors" → "semiconductor")
        matched_cat = ""
        for key in LLM_CATEGORIES:
            if key in category or category in key:
                matched_cat = key
                break
        if not matched_cat:
            # 카테고리를 못 찾으면 전체 텍스트에서 다시 시도
            out_lower = out.lower()
            for key in LLM_CATEGORIES:
                if key in out_lower:
                    matched_cat = key
                    break
        if not matched_cat:
            return None
        category = matched_cat
        title_ko = str(data.get("title_ko", "")).strip()
        summary_text = str(data.get("summary", "")).strip()
        keywords_raw = data.get("keywords", [])
        keywords = [str(k).strip() for k in keywords_raw if str(k).strip()][:5]
        return {"category": category, "title_ko": title_ko, "summary": summary_text, "keywords": keywords}
    except Exception:
        # JSON 파싱 실패 — 텍스트에서 카테고리만 추출 시도
        out_lower = out.lower()
        for key in LLM_CATEGORIES:
            if key in out_lower:
                return {"category": key, "title_ko": "", "summary": "", "keywords": []}
        return None


# --- 키워드 정의 (소문자 매칭) ---
# 규제/법령은 반도체·제조업 "공장 환경 규제"에 특화: 오염물질 배출 시설,
# 용수/폐수, 화학물질, 배기 처리 등 공장에서 법적 리스크를 검토해야 하는 규제.
# 기타 반도체는 반도체 기업(SK하이닉스/삼성전자/TSMC 등)과 반도체 적용 기술 트렌드.
KEYWORDS = [
    ("safety", [
        # 반도체/제조업 안전 이슈: 화재·폭발·화학물질 사고·산업안전·작업자 안전
        # 화재/폭발
        "화재", "fire", "발화", "폭발", "explosion",
        "가스 누출", "gas leak", "가스누출", "누출 사고",
        # 화학물질 사고
        "화학 사고", "chemical accident", "화학물질 사고", "유독물",
        "화학물질 노출", "chemical exposure", "노출 사고",
        "위험물 사고", "위험물 누출",
        # 산업안전/안전관리/사고
        "산업안전", "industrial safety", "안전관리", "safety management",
        "안전사고", "안전 사고", "산재", "산업재해", "industrial accident",
        "작업자 안전", "worker safety", "공장 안전", "factory safety",
        "fab safety", "fab 안전", "안전 점검", "safety inspection",
        "안전 규정", "safety regulation", "안전 교육",
        "공장 사고", "plant accident", "제조업 사고", "작업 사고",
        "작업자 사망", "사망 사고", "중대재해", "fatal accident",
        # 반도체 fab 특화 안전
        "실록사 누출", "silane leak", "수소 안전", "특수가스 안전",
        "특수가스 누출", "specialty gas", "수소 누출", "hydrogen leak",
        "정전기 사고", "cleanroom 안전", "클린룸 사고", "cleanroom accident",
        # 화학물질 노출/사고 (본문 탐지 보강)
        "화학물질 노출", "chemical exposure", "유해 물질", "hazardous substance",
        "유독가스", "toxic gas", "독성 가스",
        # 산업안전 일반 (본문 탐지 보강)
        "산업안전", "industrial safety", "안전 점검", "safety inspection",
        "안전 규정", "safety regulation", "안전 교육", "safety training",
        "작업자 안전", "worker safety", "공장 안전", "factory safety",
        "fab safety", "fab 안전", "안전 사고", "industrial accident",
        "중대재해", "serious accident", "작업자 사망", "fatal accident",
        # 법령 관련 안전 (산안법 등)
        "산업안전보건법", "산안법", "occupational safety",
        "안전보건", "안전보건관리", "안전관리", "safety management",
    ]),
    ("legislation", [
        # 한국 법령 (공장 환경·안전 관련 법령 개정/제정/시행)
        "법령개정", "법령 개정", "법률 개정", "법률개정", "법률 제정", "법률제정",
        "법률안", "개정법률", "시행령", "시행규칙", "법제처",
        "법개정", "법 개정", "환경법", "환경 법",
        "오염물질 배출", "배출 시설", "대기환경보전법", "수질환경보전법",
        "환경영향평가", "환경기술", "환경산업", "화학물질관리법",
        "중소기업제품 구매촉진법", "중처법",
        "산업안전보건법", "산안법", "위험물안전관리법",
        "화학물질관리법", "화관법", "화학물질의 등록",
        # 영문
        "bill passed", "act amended", "act enacted", "regulation enacted",
        "legislation", "statute", "rulemaking", "final rule", "proposed rule",
        "amendment", "enacted", "promulgat",
    ]),
    ("regulation", [
        # 공장 환경 규제 특화 키워드. 모호한 단일 단어(reach, emission, 방출,
        # 폐기물, 용수 등)는 다른 맥락에서 자주 나오므로 복합어로만 사용.
        # 물사용/폐수 (공장 용수/폐수 규제)
        "water regulation", "water restriction", "water use regulation",
        "물 사용 규제", "물사용 규제", "용수 규제", "물 규제",
        "ultrapure water", "초순수", "water stewardship", "water permit",
        "물 허가", "수자원 규제", "water discharge permit", "withdrawal permit",
        "물 재활용", "water reuse", "water recycling",
        "폐수 규제", "폐수 배출", "폐수 처리", "wastewater regulation",
        "effluent limit", "effluent standard", "폐수 규제 기준",
        "공업 용수", "industrial water use",
        # 배출물질 (오염물질 배출 시설 규제) — 복합어 또는 명확 전문 용어
        "pfc", "perfluorocarbon", "nf3", "sf6", "c2f6", "cf4",
        "voc regulation", "volatile organic", "휘발성 유기",
        "hazardous air pollutant", "hap emission",
        "air pollutant", "대기오염물질", "대기오염",
        "waste regulation", "폐기물 규제", "폐기물 처리",
        "chemical regulation", "화학물질 규제", "tsca", "reach regulation",
        "emission limit", "emission standard", "배출 허용", "배출 규제",
        "배출 기준", "배출량 규제", "오염물질 배출", "오염물질",
        "배출 시설", "오염 방지 시설", "방지 시설", "pollution control",
        "공장 환경", "fab 환경", "factory environmental",
        "환경 규제", "environmental regulation", "법적 리스크", "legal risk",
        "환경영향평가", "환경 기준", "배출허용기준",
        # 규제/정책 맥락 복합어 (edie/Triple Pundit 환경 매체의 정책 기사)
        "지속가능성 보고", "sustainability reporting", "보고 요구", "reporting requirement",
        "기후 적응", "climate adaptation", "화석연료세", "carbon tax", "탄소세",
        "탄소 배출 거래제", "emissions trading", "cap-and-trade", "ets",
        "환경 영향", "environmental impact", "환경 평가", "environmental assessment",
        "허가제", "permitting", "인허가", "환경 인허가",
        # 규제 강화 추가 키워드 (환경 허가/배출/폐수/화학물질 신고)
        "환경 허가", "environmental permit", "배출 허가", "discharge permit",
        "폐수 기준", "effluent standard", "폐수 규제", "wastewater regulation",
        "화학물질 신고", "chemical reporting", "화학물질 등록", "chemical registration",
        "환경 기준", "environmental standard", "오염 방지", "pollution prevention",
        "배출량 제한", "emission limit", "대기 배출", "air emission",
        "수질 기준", "water quality standard", "환경 규제 강화", "환경 규제 완화",
        # 주의: 단일 단어 "reach", "emission", "방출", "폐기물", "용수" 등은
        # 다른 맥락에서 자주 나와 복합어로만 사용. "regulation/규제/standard/기준"
        # 같은 너무 넓은 단어도 제외.
    ]),
    ("environmental", [
        "esg", "sustainability", "지속가능", "탈탄소", "decarboniz",
        "carbon neutral", "net zero", "carbon footprint", "탄소중립", "탄소 발자국",
        "renewable energy", "신재생에너지", "green semiconductor", "친환경 반도체",
        "circular economy", "순환경제", "재생에너지", "environmental",
        "환경 경영", "환경영향", "environmental impact", "기후변화", "climate change",
        "온실가스", "greenhouse gas", "ghg", "환경 이슈", "환경이슈",
    ]),
    ("semiconductor", [
        # 핵심 반도체 기업 (이름 자체가 강한 신호)
        "tsmc", "삼성전자", "sk hynix", "하이닉스", "intel", "micron", "asml",
        "applied materials", "lam research", "어플라이드머티리얼즈",
        # 핵심 기술/제품 (구체적 전문 용어)
        "fab", "팹", "파운드리", "foundry", "memory", "dram", "nand",
        "hbm", "high bandwidth memory", "시스템반도체", "advanced node", "선단 공정",
        "euv", "high-na", "lithography", "노광", "wafer", "웨이퍼",
        # 핵심 뉴스 신호 (증설/투자/파트너십/양산/공정)
        "증설", "capacity expansion", "투자", "investment", "파트너십", "partnership",
        "양산", "mass production", "tape-out", "파운드리 고객",
        "공정", "process node", "미세공정", "yield", "수율",
        # 주의: "semiconductor", "반도체", "chip", "칩" 같은 너무 일반적 단어는
        # 단독으로는 분류 안 함 (다른 구체적 키워드와 함께 있을 때만 반도체로).
    ]),
]

# 카테고리 표시 메타데이터
# 최종 카테고리: 반도체, 환경, 안전, 규제, 법령 변경
CATEGORIES = {
    "semiconductor": {"label": "반도체",          "emoji": "🔌", "order": 1},
    "environmental": {"label": "환경",            "emoji": "🌱", "order": 2},
    "safety":        {"label": "안전",            "emoji": "🦺", "order": 3},
    "regulation":    {"label": "규제",            "emoji": "⚖️", "order": 4},
    "legislation":   {"label": "법령 변경",       "emoji": "📜", "order": 5},
}

# 반도체/환경 관련성 필터 (require_keyword=true 출처에서 기사가 이 키워드 중 하나라도
# 포함해야 수집). 광범위 한국 뉴스 피드에서 노이즈 제거용.
RELEVANCE_KEYWORDS = [
    "반도체", "semiconductor", "웨이퍼", "wafer", "팹", "fab", "파운드리", "foundry",
    "시스템반도체", "메모리", "dram", "nand", "hbm", "선단", "노광", "euv",
    "esg", "지속가능", "탄소", "탈탄소", "환경", "decarbon", "carbon",
    "스크러버", "스크래버", "scrubber", "배기", "abatement",
    "초순수", "ultrapure", "pfc", "voc", "배출", "emission",
    "tsmc", "삼성전자", "하이닉스", "intel", "micron", "asml",
    "규제", "regulation", "법령", "legislation",
    "친환경", "순환경제", "water", "물 사용",
    # 대기질/배기 (Air Quality News 등)
    "air quality", "대기질", "대기 오염", "air pollution", "emission",
    "abatement", "scrubber", "배기", "exhaust", "vent gas", "f-gas",
    "particulate", "미세먼지", "filter", "필터",
    # 환경 규제/폐수/용수 (edie/Triple Pundit 등 환경 매체)
    "폐수", "wastewater", "effluent", "용수", "오염", "pollut",
    "environmental regulation", "환경 규제", "compliance", "법적 리스크",
    "net zero", "탄소중립", "온실가스", "greenhouse gas", "기후", "climate",
    "sustainability", "지속가능성", "circular economy", "순환경제",
    # 안전 사고/산업안전 (동아일보 사회면 등)
    "화재", "fire", "폭발", "explosion", "가스 누출", "누출", "leak",
    "산업안전", "산재", "산업재해", "안전사고", "공장 사고",
    "작업자 사망", "중대재해", "화학 사고", "화학물질 사고",
]

# 노이즈(시세/증시/투자 동향) 블랙리스트. 제목이 이 단어들로 주를 이루면
# 반도체 기사라도 "시장 동향"이지 "제조/환경/규제 동향"이 아니므로 제외.
# 주의: "증가/상승/하락" 같은 일반 동사는 제외 대상이 아님 (장비 매출 보고서 등
# 유의미한 기사를 잘못 잡음). 명확한 증시/시세 명사만 노이즈로 간주.
NOISE_TITLE_KEYWORDS = [
    # 증시/시세 (명확한 시장 거래 용어)
    "코스피", "코스닥", "증시", "주가", "주식", "시세", "상한가", "하한가",
    "급등", "급락", "폭락", "폭등", "장중", "종가", "시초가",
    "기관 매수", "외국인 매수", "외국인 매도", "순매수", "순매도",
    "목표가", "투자의견", "리서치", "컨센서스",
    "증권", "배당", "주주",
    # 일반 경제 동향 (반도체와 무관한 매크로)
    "환율", "원달러", "유가", "금값", "인플레이션",
    # noise 영문 (명확한 시장 용어만)
    "stock", "share price", "market cap", "rally",
    "plunge", "surge", "slump", "earnings beat", "guidance",
    "analyst rating", "price target", "buy rating",
    # CSR/봉사/활동 노이즈 (반도체 본질 아님)
    "봉사활동", "봉사 활동", "자원봉사", "사회공헌", "CSR", "참여", "기부",
    "후원", "협찬", "캠페인", "선행", "수상", "시상식", "행사 참여",
    "volunteer", "charity", "donation", "philanthrop",
    # 반도체 무관 산업 (반도체 키워드가 우연히 있어도 제외).
    # 주의: 정치계 인물(대통령/지사/국회의원)은 노이즈가 아님 — 이들이 반도체
    # 업계에 규제를 가하는 기사는 잡아야 하므로, 정치 주체 자체는 제외 대상이 아님.
    "대한항공", "항공사", "비행기", "운항", "해운", "해운업",
]

# 강한 노이즈 (이 단어가 1개만 있어도 제외)
STRONG_NOISE = [
    "코스피", "코스닥", "증시", "주가", "주식", "시세", "상한가", "하한가",
    "급등", "급락", "폭등", "폭락", "목표가", "투자의견",
    "봉사활동", "봉사 활동", "자원봉사", "사회공헌",
    "대한항공", "항공사",
    "stock", "share price", "rally", "plunge",
]

CORE_KEYWORDS = [
    "반도체", "semiconductor", "웨이퍼", "wafer", "팹", "fab", "파운드리",
    "시스템반도체", "메모리", "dram", "nand", "hbm", "노광", "euv", "선단",
    "esg", "지속가능", "탄소", "탈탄소", "환경", "친환경", "온실가스",
    "스크러버", "스크래버", "scrubber", "배기", "abatement", "초순수",
    "pfc", "voc", "배출", "emission", "규제", "regulation", "법령",
    "탄소중립", "net zero", "decarbon", "water", "물 사용",
]


def is_noise_title(title: str) -> bool:
    """제목이 시세/증시 동향 노이즈이면 True.
    강한 노이즈 단어가 1개 있거나, 일반 노이즈 2개 이상 + 본질 키워드 부재."""
    if not title:
        return False
    t = title.lower()
    # 강한 노이즈 1개 → 즉시 제외
    for w in STRONG_NOISE:
        if w in t:
            return True
    noise_hits = sum(1 for w in NOISE_TITLE_KEYWORDS if w in t)
    core_hits = sum(1 for w in CORE_KEYWORDS if w in t)
    # 노이즈 2개 이상 + 본질 키워드 0개 → 시장 동향 기사
    if noise_hits >= 2 and core_hits == 0:
        return True
    return False


def fetch_url(url: str) -> str | None:
    headers = {"User-Agent": USER_AGENT}
    try:
        if requests is not None:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            if r.status_code != 200:
                return None
            return r.text
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [skip] {url} -> {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _has_word(text_lower: str, keyword: str) -> bool:
    """단어 경계 기반 매칭. 짧은 키워드(hap, act, reach 등)가 다른 단어의
    일부분으로 잘못 매칭되는 것을 방지. 한국어는 단어 경계 개념이 없으므로
    한국어 키워드는 그대로 부분 매칭, 영문은 단어 경계 매칭 사용.
    복합 키워드(띄어쓰기 포함)는 단순 부분 매칭."""
    if not keyword:
        return False
    # 띄어쓰기/하이픈 포함 복합 키워드는 부분 매칭
    if " " in keyword or "-" in keyword:
        return keyword in text_lower
    # 한국어(가-힣 포함)는 부분 매칭
    if re.search(r"[가-힣]", keyword):
        return keyword in text_lower
    # 영문 알파벳 단어는 단어 경계 매칭 (hap, act, reach 등 보호)
    return bool(re.search(r"\b" + re.escape(keyword) + r"\b", text_lower))


def _count_hits(text_lower: str, keywords: list[str]) -> int:
    return sum(1 for w in keywords if _has_word(text_lower, w))


def _hit_words(text_lower: str, keywords: list[str]) -> list[str]:
    return [w for w in keywords if _has_word(text_lower, w)]


def classify(text: str, title: str = "") -> str:
    """기사를 분류. 어느 카테고리도 아니면 빈 문자열(=수집 제외) 반환.
    title은 반도체 카테고리 정제용(제목에 핵심 키워드 있을 때만 반도체).
    카테고리: 반도체 / 환경 / 안전 / 규제 / 법령 변경"""
    if not text:
        return ""
    return classify_scored(text, title)


# 공장/제조업 환경 맥락 키워드: 규제/법령 카테고리가 "공장 환경 규제" 맥락인지 확인.
# 이 단어들이 함께 있어야 공장 환경 규제 기사로 인정 (일반 정책 뉴스와 구분).
FACTORY_CONTEXT = [
    # 한국 공장/환경/안전
    "공장", "fab", "팹", "제조", "manufacturing", "제조업", "공단",
    "오염물질", "오염", "pollut", "배출 시설", "배출시설", "방지 시설",
    "폐수", "용수", "폐기물", "대기오염", "수질", "화학물질",
    "환경", "environmental", "환경부", "환경 규제",
    "초순수", "ultrapure", "scrubber", "스크러버", "배기", "abatement",
    "pfc", "voc", "emission", "배출", "온실가스",
    # 안전 맥락 (공장 안전 사고/산업안전)
    "안전", "safety", "산재", "산업안전", "화재", "폭발", "누출", "leak",
    "작업자", "worker", "산업재해",
    # 영문 fab 환경
    "semiconductor fab", "chip plant", "wafer fab", "foundry",
    "industrial water", "wastewater", "effluent", "exhaust",
    "factory", "plant", "facility",
]

# 정책 신호: 정부/정치계가 규제를 만드는 맥락.
# 정치 주체(대통령/지사/국회의원 등)도 포함 — 이들이 반도체 업계에 규제를
# 가하는 기사를 잡기 위함. 단, 수집 단계의 관련성 필터(반도체/공장 키워드)가
# 동반되어야만 수집되므로 일반 정치 뉴스는 걸러짐.
POLICY_SIGNALS = [
    # 한국 정부/기관/정치 주체
    "정부", "정책", "시행", "개정", "제정", "법제", "국회", "환경부", "산업부",
    "국토부", "규제개혁", "규제 철폐", "규제 신설", "규제 강화", "규제 완화",
    "허가", "인허가", "승인", "지정", "고시", "공고", "예고", "법령", "법률",
    # 정치 주체 (반도체 규제 맥락에서 나올 때만 의미)
    "대통령", "지사", "도지사", "시장", "국회의원", "장관", "총리", "청와대",
    "여당", "야당", "국회 본회의", "상임위", "정책위",
    # 영문 정책 신호
    "government", "regulator", "agency", "federal", "department of",
    "ministry", "commission", "authority", "directive", "decree",
    "implement", "enforce", "compliance deadline", "permitted", "banned",
    "phased out", "phase-out", "prohibit", "restrict",
    "regulation", "governor", "senate", "congress", "parliament", "white house",
    # 주의: "act", "rule", "limit", "standard", "market" 같은 너무 짧거나 모호한
    # 단어는 제외 (다른 단어의 일부/일반 명사로 자주 매칭).
]


# 카테고리별 키워드 (classify_scored에서 사용). KEYWORDS 정의 뒤에 평가되도록
# 함수 본체가 실행 시점에 참조하므로 모듈 로드 순서에 안전하게 두기 위해
# classify_scored 정의 이전에 평가해 둔다.
KEYWORDS_SAFETY = [w for cat, words in KEYWORDS if cat == "safety" for w in words]
KEYWORDS_LEGISLATION = [w for cat, words in KEYWORDS if cat == "legislation" for w in words]
KEYWORDS_REGULATION = [w for cat, words in KEYWORDS if cat == "regulation" for w in words]
KEYWORDS_ENVIRONMENTAL = [w for cat, words in KEYWORDS if cat == "environmental" for w in words]
KEYWORDS_SEMICONDUCTOR = [w for cat, words in KEYWORDS if cat == "semiconductor" for w in words]


def classify_scored(combined: str, title: str = "") -> str:
    """기사를 분류. 어느 카테고리도 아니면 빈 문자열(=수집 제외) 반환.
    반도체 카테고리는 제목에 핵심 기업/기술 키워드가 있을 때만 분류하여
    summary에 부차적 기업명 언급된 일반 뉴스가 섞이는 것을 방지."""
    t = (combined or "").lower()
    if not t.strip():
        return ""
    title_lower = (title or "").lower()

    factory_hits = _count_hits(t, FACTORY_CONTEXT)
    policy_hits = _count_hits(t, POLICY_SIGNALS)

    # --- Safety: 안전 사고/화재/폭발/화학물질 사고/산업안전 ---
    # 강화: 제목에 안전 키워드 1개 → 즉시 safety(기존).
    #       제목엔 없지만 본문에 안전 키워드 ≥ 2개 → safety(신규).
    #       제목만 보면 fab 사고 기사가 제목에 안 적혀도 본문에 화재/누출이
    #       나오는 경우를 놓쳐 0건이 되므로 본문 탐지를 추가.
    #       단일 단어 우연 매칭 방지를 위해 본문은 2개 이상 히트로 임계치 설정.
    if _count_hits(title_lower, KEYWORDS_SAFETY) >= 1:
        return "safety"
    if _count_hits(t, KEYWORDS_SAFETY) >= 2:
        return "safety"

    # --- Legislation: 기사 기반 분류 제외 ---
    # 법령 변경 카드는 공공데이터포털 API 데이터만 표시 (legislation.py).
    # 기사가 법령 키워드에 매칭되어도 legislation으로 분류하지 않고,
    # 아래 규제/환경/반도체 카테고리로 흐르도록 둔다.

    # --- Regulation: 공장 환경 규제 특화 키워드 + 공장/정책 맥락 ---
    # 강화: 규제 키워드 1 + (공장 또는 정책) → regulation(기존).
    #       규제 키워드 ≥ 2 → regulation(기존).
    #       규제 키워드 1 + 안전 키워드 1(본문) → regulation(신규) —
    #       규제+안전이 섞인 "안전 규제 강화" 기사를 잡기 위함.
    reg_hits = _count_hits(t, KEYWORDS_REGULATION)
    if reg_hits >= 1 and (factory_hits >= 1 or policy_hits >= 1):
        return "regulation"
    if reg_hits >= 2:
        return "regulation"
    if reg_hits >= 1 and _count_hits(t, KEYWORDS_SAFETY) >= 1:
        return "regulation"

    # --- Environmental: 환경/ESG/탄소 ---
    if _count_hits(t, KEYWORDS_ENVIRONMENTAL) >= 1:
        return "environmental"

    # --- Semiconductor: 제목에 핵심 기업/기술 키워드 있을 때만 ---
    # summary에 부차적 기업명 언급된 일반 뉴스(삼성 공채, 애플 폴더블 등) 제외.
    if _count_hits(title_lower, KEYWORDS_SEMICONDUCTOR) >= 1:
        return "semiconductor"

    # 어느 카테고리도 아니면 빈 문자열 → 수집 제외
    return ""


def is_relevant(text: str) -> bool:
    """반도체/환경 관련성 검사. require_keyword=true 출처에서 사용."""
    t = (text or "").lower()
    return any(w in t for w in RELEVANCE_KEYWORDS)


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# 깨진 인코딩(Latin-1로 잘못 디코딩된 UTF-8) 감지:
# 정상 한글이 하나도 없고, 라틴 확장/특수문자 깨짐이 2개 이상 섞여 있으면 깨진 것.
_HANGUL_RE = re.compile(r"[가-힣]")
_BROKEN_CHARS_RE = re.compile(r"[ÃÂíìëêóñòôõöðïî˜œ€Ÿ¥©®]")

def is_broken_kr(s: str) -> bool:
    """한글이 없고 라틴 확장 깨짐 패턴이 있으면 True."""
    if not s:
        return False
    if _HANGUL_RE.search(s):
        return False
    return bool(_BROKEN_CHARS_RE.search(s))


def parse_date(entry) -> str | None:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        st = getattr(entry, field, None)
        if st:
            try:
                dt = datetime(*st[:6], tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                continue
    return None


# --- LLM 추천 키워드 자동 반영 ---
# keyword_suggestions.json(이전 LLM 실행이 뽑은 키워드)에서 빈도가 높은
# 키워드를 KEYWORDS 리스트에 자동 병합 → 키워드 방식이 놓치는 기사를 점진적 보완.
# 노이즈 방지: (a) 너무 짧거나 일반적인 단어 제외, (b) 빈도 임계치(기본 3회)
# 이상인 키워드만 채택, (c) 이미 있는 키워드는 중복 추가 안 함.
_LLM_KW_FILE = ROOT / "site" / "keyword_suggestions.json"
# 자동 반영에서 제외할 너무 일반적인 단어(불용어).
_KW_STOPWORDS = {
    "semiconductor", "반도체", "chip", "칩", "기업", "company", "tech",
    "산업", "industry", "시장", "market", "뉴스", "news", "전망", "outlook",
    "투자", "investment", "개발", "development", "기술", "technology",
    "보고", "report", "발표", "announce", "계획", "plan", "프로젝트", "project",
}
# 임계치: LLM 모드(키워드 많음)는 3, 키워드 모드(적음)는 2로 더 민감하게.
_KW_MIN_FREQ = int(os.environ.get("LLM_KW_MIN_FREQ", "3"))


def _load_llm_keyword_boost() -> dict[str, list[str]]:
    """이전 keyword_suggestions.json에서 빈도≥임계치 키워드를 카테고리별로 수집.
    반환: {category: [추가할 키워드, ...]}. 파일 없거나 LLM 미사용 시 빈 dict."""
    if not _LLM_KW_FILE.exists():
        return {}
    try:
        d = json.loads(_LLM_KW_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    # 카테고리별 키워드 빈도 집계
    freq: dict[str, dict[str, int]] = {}
    for s in d.get("suggestions", []):
        cat = s.get("category", "")
        if not cat or cat not in {k for k, _ in KEYWORDS}:
            continue
        for kw in s.get("keywords", []):
            kw = str(kw).strip().lower()
            # 너무 짧거나(2자 미만), 불용어, 숫자만이면 제외
            if len(kw) < 2 or kw in _KW_STOPWORDS or kw.isdigit():
                continue
            freq.setdefault(cat, {})
            freq[cat][kw] = freq[cat].get(kw, 0) + 1
    # 임계치 이상 키워드만, 기존 KEYWORDS에 없는 것만 채택
    boost: dict[str, list[str]] = {}
    existing = {w for _, words in KEYWORDS for w in words}
    for cat, kws in freq.items():
        new = [kw for kw, c in kws.items() if c >= _KW_MIN_FREQ and kw not in existing]
        if new:
            boost[cat] = new
    return boost


def _apply_keyword_boost(boost: dict[str, list[str]]) -> int:
    """KEYWORDS 리스트에 boost 키워드를 in-place로 추가. 추가된 총 개수 반환."""
    if not boost:
        return 0
    added = 0
    for i, (cat, words) in enumerate(KEYWORDS):
        if cat in boost:
            KEYWORDS[i] = (cat, words + boost[cat])
            added += len(boost[cat])
    return added


def collect() -> dict:
    # LLM 추천 키워드 자동 반영(이전 실행 결과 기반).
    if LLM_ENABLED:
        boost = _load_llm_keyword_boost()
        if boost:
            n = _apply_keyword_boost(boost)
            # 병합 후 KEYWORDS_* 재계산 (분류에서 참조)
            global KEYWORDS_SAFETY, KEYWORDS_LEGISLATION, KEYWORDS_REGULATION
            global KEYWORDS_ENVIRONMENTAL, KEYWORDS_SEMICONDUCTOR
            KEYWORDS_SAFETY = [w for cat, words in KEYWORDS if cat == "safety" for w in words]
            KEYWORDS_LEGISLATION = [w for cat, words in KEYWORDS if cat == "legislation" for w in words]
            KEYWORDS_REGULATION = [w for cat, words in KEYWORDS if cat == "regulation" for w in words]
            KEYWORDS_ENVIRONMENTAL = [w for cat, words in KEYWORDS if cat == "environmental" for w in words]
            KEYWORDS_SEMICONDUCTOR = [w for cat, words in KEYWORDS if cat == "semiconductor" for w in words]
            print(f"[fetcher] LLM 추천 키워드 {n}개 자동 반영: " +
                  ", ".join(f"{c}+{len(v)}" for c, v in boost.items()))

    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = data.get("sources", [])
    articles = []
    seen_links: set[str] = set()
    feed_ok = 0
    feed_fail = 0
    keyword_suggestions = []  # LLM 모드: 키워드 방식이 놓친 기사의 새 키워드 후보

    print(f"[fetcher] {len(sources)}개 출처 수집 시작")
    for src in sources:
        name = src.get("name", "?")
        url = src.get("url", "")
        lang = src.get("lang", "ko")
        require_kw = src.get("require_keyword", False)
        print(f"  - {name} ({lang})")
        raw = fetch_url(url)
        if not raw:
            feed_fail += 1
            continue
        parsed = feedparser.parse(raw)
        if not parsed.entries:
            feed_fail += 1
            continue
        feed_ok += 1
        added = 0
        skipped_irrelevant = 0
        skipped_noise = 0
        skipped_unclassified = 0
        skipped_old = 0
        for entry in parsed.entries:
            title = strip_html(getattr(entry, "title", ""))
            link = getattr(entry, "link", "")
            if not title or not link:
                continue
            if link in seen_links:
                continue
            summary = strip_html(getattr(entry, "summary", "") or getattr(entry, "description", ""))

            # 노이즈 필터 (모든 출처): 시세/증시 동향 기사 제외.
            # 주식/시세 기사는 반도체 키워드가 있어도 본 페이지 주제에 부합하지 않음.
            if is_noise_title(title):
                skipped_noise += 1
                continue

            # 관련성 필터 (광범위 한국 뉴스 피드 노이즈 제거)
            combined_for_relevance = f"{title} {summary}"
            if require_kw and not is_relevant(combined_for_relevance):
                skipped_irrelevant += 1
                continue

            date = parse_date(entry)

            # 날짜 상한: 최근 MAX_AGE_DAYS 일 이내 기사만 수집.
            # 날짜가 없는 기사(None)는 보수적으로 유지(최신으로 간주) —
            # RSS에서 날짜 누락되는 경우가 있어 무조건 버리면 손실 큼.
            if date:
                try:
                    dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - dt).days
                    if age_days > MAX_AGE_DAYS:
                        skipped_old += 1
                        continue
                except Exception:
                    pass

            # 번역+분류+요약 처리.
            # LLM 모드: LLM이 한 번에 번역(영문)+분류+요약+키워드 처리 (Google 번역 불필요).
            # 키워드 모드(GitHub Actions): Google 번역 → 키워드 분류 → 발췌 요약.
            title_final = title
            summary_final = summary

            # LLM 모드에서 영문 기사: Google 번역 안 하고 LLM에 원문 전달.
            # LLM이 한국어로 번역+분류+요약을 한 번에 수행.
            # 키워드 모드에서 영문 기사: 기존대로 Google 번역 사용.
            if lang == "en" and not LLM_ENABLED:
                title_final = translate_title(title)
                sum_trunc = summary[:1000] if summary else ""
                if sum_trunc:
                    summary_final = translate_summary(sum_trunc)
                time.sleep(0.2)  # 번역 엔진 rate-limit 회피

            # 깨진 인코딩 복구 (한국 출처): title과 summary가 Latin-1 잘못
            # 디코딩으로 깨질 수 있다. summary가 정상이면 그 첫 문장으로 제목 복구,
            # 둘 다 깨졌으면 빈 값 처리 후 기사를 건너뛴다.
            if lang == "ko" and is_broken_kr(title_final):
                if summary_final and not is_broken_kr(summary_final):
                    first_sent = re.split(r"[.!?]\s|\n", summary_final)[0].strip()
                    if first_sent and _HANGUL_RE.search(first_sent):
                        title_final = first_sent[:80]
                if is_broken_kr(title_final):
                    # 제목 복구 불가 → 건너뜀 (깨진 데이터는 수집 의미 없음)
                    continue
            if lang == "ko" and is_broken_kr(summary_final):
                summary_final = ""

            # 분류 (번역된 한국어 텍스트 기준 — 영문 기사도 한국어 키워드로 분류)
            hint = src.get("topic", "")
            # 분류용 전체 텍스트 (제목+요약+원문+힌트)
            classify_text = f"{title_final} {summary_final} {title} {summary} {hint}"
            # 반도체 카테고리 정제: 제목에 핵심 키워드가 있을 때만 반도체로
            # 분류 (summary에 부차적으로 기업명이 언급된 일반 뉴스 제외).
            classify_title = f"{title_final} {title}"

            # LLM 모드: 분류+요약+키워드(+영문 번역)를 한 번에 처리 (12초/기사).
            # LLM 실패 시 키워드 분류 + 발췌 요약으로 폴백.
            # GitHub Actions(LLM 없음)은 기존 classify() + 발췌 요약 사용.
            cat = ""
            if LLM_ENABLED and (summary_final or title_final):
                # 영문 기사는 원문(title, summary)을 LLM에 넘겨 번역+분류+요약을 한 번에.
                # 한국어 기사는 번역 없이 분류+요약만.
                llm_title = title if lang == "en" else title_final
                llm_summary = summary if lang == "en" else summary_final
                llm_result = llm_process_article(llm_title, llm_summary, is_english=(lang == "en"))
                if llm_result:
                    cat = llm_result["category"]
                    # 영문 기사: LLM 번역 제목 사용
                    if lang == "en" and llm_result["title_ko"]:
                        title_final = llm_result["title_ko"]
                    # LLM 요약이 있으면 발췌 대신 진짜 요약 사용
                    if llm_result["summary"]:
                        summary_final = llm_result["summary"]
                    # 키워드 추천 수집 (키워드 방식 보완용)
                    if llm_result["keywords"]:
                        keyword_suggestions.append({
                            "title": title_final[:80],
                            "category": cat,
                            "keywords": llm_result["keywords"],
                            "link": link,
                        })
            # LLM 실패 시 폴백: 영문 기사는 Google 번역으로 제목/요약 번역.
            # (LLM 모드에서는 위에서 Google 번역을 안 했으므로 여기서 보완)
            if lang == "en" and LLM_ENABLED and not cat:
                title_final = translate_title(title)
                sum_trunc = summary[:1000] if summary else ""
                if sum_trunc:
                    summary_final = translate_summary(sum_trunc)
                # 폴백 후 분류 재시도
                classify_text = f"{title_final} {summary_final} {title} {summary} {hint}"
                classify_title = f"{title_final} {title}"
            if not cat:
                cat = classify(classify_text, classify_title)
            # 분류 안 되면(빈 문자열) 수집 제외 — 핵심 주제와 무관한 기사 걸러내기
            if not cat:
                skipped_unclassified += 1
                continue

            articles.append({
                "title": title_final,
                "link": link,
                "summary": summary_final[:1000] if summary_final else "",
                "category": cat,
                "source": name,
                "lang": lang,
                "date": date,
                "original_title": title if lang == "en" else "",
            })
            seen_links.add(link)
            added += 1
        msg = f"      수집 {added}건"
        if skipped_irrelevant:
            msg += f" (관련성 {skipped_irrelevant}건 제외)"
        if skipped_noise:
            msg += f" (시세/노이즈 {skipped_noise}건 제외)"
        if skipped_unclassified:
            msg += f" (주제무관 {skipped_unclassified}건 제외)"
        if skipped_old:
            msg += f" (날짜초과 {skipped_old}건 제외)"
        print(msg)

    # 날짜 내림차순 정렬
    def sort_key(a):
        return a["date"] or ""
    articles.sort(key=sort_key, reverse=True)

    print(f"[fetcher] 완료: 피드 성공 {feed_ok}, 실패 {feed_fail}, 총 기사 {len(articles)}건")

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "feed_ok": feed_ok,
        "feed_fail": feed_fail,
        "total": len(articles),
        "categories": CATEGORIES,
        "articles": articles,
        "keyword_suggestions": keyword_suggestions,
        "llm_enabled": LLM_ENABLED,
    }


def _date_str_kst(iso: str | None) -> str:
    """ISO 날짜를 KST YYYY-MM-DD 문자열로 변환. 없으면 빈 문자열."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def main():
    result = collect()
    # New 배지: 업데이트한 날짜(fetched_at, KST)와 동일한 날짜의 기사에만 표시.
    # 매일 6시 업데이트 시 "오늘 새로 수집된 기사"를 즉시 식별.
    fetch_date = datetime.now(KST).strftime("%Y-%m-%d")
    new_count = 0
    for a in result["articles"]:
        is_new = (_date_str_kst(a.get("date")) == fetch_date)
        a["is_new"] = is_new
        if is_new:
            new_count += 1
    result["new_count"] = new_count
    result["fetch_date"] = fetch_date
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[fetcher] 저장 -> {OUT_FILE} (오늘 날짜 새 기사 {new_count}건)")

    # LLM 모드: 키워드 추천 결과 별도 저장 (사람 검토용).
    # GitHub Actions(키워드 모드)에서는 빈 목록이라 저장 안 함.
    kw_suggestions = result.get("keyword_suggestions", [])
    if kw_suggestions:
        kw_file = OUT_FILE.parent / "keyword_suggestions.json"
        kw_file.write_text(
            json.dumps({
                "generated_at": datetime.now(KST).isoformat(),
                "count": len(kw_suggestions),
                "suggestions": kw_suggestions,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[fetcher] 키워드 추천 -> {kw_file} ({len(kw_suggestions)}건)")


if __name__ == "__main__":
    main()
