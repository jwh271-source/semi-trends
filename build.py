"""
build.py — site/articles.json 을 읽어 site/index.html 생성.

레이아웃: 카테고리 섹션마다 카드가 가로로 줄지는 그리드(원래 디자인).
각 카테고리 섹션이 세로로 쌓이고, 섹션 내 카드는 반응형 가로 그리드.

실행: python build.py
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IN_FILE = ROOT / "site" / "articles.json"
OUT_FILE = ROOT / "site" / "index.html"

KST = timezone(timedelta(hours=9))


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "날짜 미상"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%Y-%m-%d")
    except Exception:
        return "날짜 미상"


def fmt_fetched(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M (KST)")
    except Exception:
        return iso


def esc(s: str) -> str:
    return html.escape(s or "")


def build_sections(articles: list[dict], cats_meta: dict) -> str:
    """카테고리 섹션 생성. 각 섹션 내 카드는 가로 그리드.
    카테고리당 최신순 MAX_PER_CAT건만 표시 (기사 과다 방지)."""
    MAX_PER_CAT = 20
    by_cat: dict[str, list[dict]] = {}
    for a in articles:
        c = a.get("category", "semiconductor")
        by_cat.setdefault(c, []).append(a)

    ordered = sorted(cats_meta.items(), key=lambda kv: kv[1].get("order", 99))

    sections = []
    for key, meta in ordered:
        all_items = by_cat.get(key, [])
        total_count = len(all_items)
        items = all_items[:MAX_PER_CAT]  # 최신순 상한
        label = meta.get("label", key)
        emoji = meta.get("emoji", "")
        cards_html = []
        for a in items:
            title = esc(a.get("title", ""))
            link = esc(a.get("link", ""))
            summary = esc(a.get("summary", ""))
            source = esc(a.get("source", ""))
            date = fmt_date(a.get("date"))
            lang = a.get("lang", "ko")
            orig = a.get("original_title", "")
            is_new = a.get("is_new", False)
            lang_badge = '<span class="badge-tr">번역</span>' if lang == "en" else ''
            new_badge = '<span class="badge-new">NEW</span>' if is_new else ''
            orig_html = f'<p class="card-orig">원문: {esc(orig)}</p>' if (lang == "en" and orig) else ''
            # 팝업용 전체 데이터 (JSON 안전하게 이스케이프)
            popup_data = json.dumps({
                "title": a.get("title", ""),
                "summary": a.get("summary", ""),
                "source": a.get("source", ""),
                "date": date,
                "link": a.get("link", ""),
                "original_title": orig,
                "lang": lang,
            }, ensure_ascii=False)
            popup_data_esc = html.escape(popup_data)
            card = f"""<article class="card" data-popup="{popup_data_esc}">
  <div class="card-head">
    <span class="src">{source}</span>
    <span class="date">{date} {lang_badge} {new_badge}</span>
  </div>
  <h3 class="card-title">{title}</h3>
  {f'<p class="card-sum">{summary}</p>' if summary else ''}
  {orig_html}
  <a class="card-link" href="{link}" target="_blank" rel="noopener">원문 보기 →</a>
</article>"""
            cards_html.append(card)
        body = "\n".join(cards_html)

        # 빈 카테고리 안내 메시지 ("관련 기사 없음"만)
        if not items:
            empty_msg = {
                "semiconductor": "현재 수집된 출처에 반도체 업계 동향 기사가 없습니다.",
                "environmental": "현재 수집된 출처에 반도체 업계 환경 이슈 기사가 없습니다.",
                "safety": "현재 수집된 출처에 반도체/제조업 안전 사고 기사가 없습니다.",
                "regulation": "현재 수집된 출처에 반도체 공장 환경 규제 기사가 없습니다.",
                "legislation": "해당 기간에 대상 법령의 개정 사항이 없습니다.",
            }.get(key, "현재 수집된 출처에 관련 기사가 없습니다.")
            body = f'<div class="col-empty">{empty_msg}</div>'

        count_display = f"{len(items)}" if total_count <= 20 else f"{len(items)}/{total_count}"
        sections.append(f"""<section class="cat-section" data-cat="{key}">
  <header class="cat-head">
    <span class="cat-emoji">{emoji}</span>
    <h2 class="cat-label">{label}</h2>
    <span class="cat-count">{count_display}</span>
  </header>
  <div class="cards">
{body}
  </div>
</section>""")
    return "\n".join(sections)


def build_nav(cats_meta: dict, counts: dict) -> str:
    """카테고리 필터 버튼 (네비게이션)."""
    buttons = ['<button class="filter-btn active" data-filter="all">전체</button>']
    ordered = sorted(cats_meta.items(), key=lambda kv: kv[1].get("order", 99))
    for key, meta in ordered:
        label = meta.get("label", key)
        emoji = meta.get("emoji", "")
        n = counts.get(key, 0)
        buttons.append(
            f'<button class="filter-btn" data-filter="{key}">'
            f'<span class="fb-emoji">{emoji}</span> {label} '
            f'<span class="fb-count">{n}</span></button>'
        )
    return "\n".join(buttons)


def build_counts(articles: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for a in articles:
        c = a.get("category", "other")
        counts[c] = counts.get(c, 0) + 1
    return counts


CSS = """
:root{
  --bg:#f5f6f8; --surface:#ffffff; --surface-2:#eef0f3;
  --text:#1a1d23; --text-dim:#5b6270; --text-mute:#8a909c;
  --border:#dde1e7; --accent:#0b5cad; --accent-soft:#e8f0fb;
  --shadow:0 1px 2px rgba(20,25,35,.06), 0 1px 1px rgba(20,25,35,.04);
  --radius:10px;
}
:root:not([data-theme="light"]){
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --bg:#0e1116; --surface:#161a21; --surface-2:#1d222b;
      --text:#e7e9ee; --text-dim:#a3a9b6; --text-mute:#6f7686;
      --border:#272d36; --accent:#6ba8e8; --accent-soft:#1a2532;
      --shadow:0 1px 2px rgba(0,0,0,.3);
    }
  }
}
:root[data-theme="dark"]{
  --bg:#0e1116; --surface:#161a21; --surface-2:#1d222b;
  --text:#e7e9ee; --text-dim:#a3a9b6; --text-mute:#6f7686;
  --border:#272d36; --accent:#6ba8e8; --accent-soft:#1a2532;
  --shadow:0 1px 2px rgba(0,0,0,.3);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--text);
  font-family:"Pretendard","Noto Sans KR","Apple SD Gothic Neo","Malgun Gothic",
    "Segoe UI",system-ui,-apple-system,sans-serif;
  font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1200px;margin:0 auto;padding:0 20px}
header.site-head{
  border-bottom:1px solid var(--border);
  background:var(--surface);
  position:sticky; top:0; z-index:10;
  backdrop-filter:saturate(140%) blur(6px);
}
.head-row{display:flex;align-items:center;gap:14px;padding:14px 0}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px}
.logo .dot{width:10px;height:10px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 0 4px var(--accent-soft)}
.logo small{font-weight:500;color:var(--text-dim);font-size:12px}
.head-right{margin-left:auto;display:flex;align-items:center;gap:10px}
.update-info{font-size:12px;color:var(--text-mute);text-align:right;line-height:1.4}
.update-info b{color:var(--text-dim);font-weight:600}
.theme-btn{
  border:1px solid var(--border);background:var(--surface);color:var(--text-dim);
  padding:6px 10px;border-radius:8px;cursor:pointer;font-size:13px;
}
.theme-btn:hover{color:var(--text);border-color:var(--accent)}

.hero{padding:30px 0 16px}
.hero h1{font-size:28px;line-height:1.2;margin:0 0 8px;letter-spacing:-.01em;text-wrap:balance}
.hero h1 .accent{color:var(--accent)}
.hero p{margin:0;color:var(--text-dim);font-size:14.5px;max-width:62ch;text-wrap:pretty}
.stat-row{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}
.stat{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:10px 14px;min-width:110px;
}
.stat .n{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.stat .l{font-size:12px;color:var(--text-mute);margin-top:2px}

.controls{position:sticky;top:62px;z-index:9;background:var(--bg);
  padding:14px 0 10px;border-bottom:1px solid var(--border)}
.search-box{width:100%;padding:11px 14px;border:1px solid var(--border);
  border-radius:9px;background:var(--surface);color:var(--text);font-size:14px;font-family:inherit}
.search-box:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.filter-btn{
  border:1px solid var(--border);background:var(--surface);color:var(--text-dim);
  padding:6px 12px;border-radius:20px;cursor:pointer;font-size:13px;
  font-family:inherit;transition:all .12s;
}
.filter-btn:hover{color:var(--text);border-color:var(--accent)}
.filter-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.filter-btn .fb-count{font-size:11px;opacity:.75;font-variant-numeric:tabular-nums}

main{padding:24px 0 60px}
.cat-section{margin-bottom:38px}
.cat-head{display:flex;align-items:center;gap:10px;margin-bottom:14px;
  padding-bottom:8px;border-bottom:1px solid var(--border)}
.cat-emoji{font-size:18px}
.cat-label{font-size:18px;font-weight:600;margin:0}
.cat-count{font-size:12px;color:var(--text-mute);font-variant-numeric:tabular-nums;
  background:var(--surface-2);padding:2px 8px;border-radius:10px}

/* 가로 카드 그리드 (원래 디자인) */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:14px 16px;display:flex;flex-direction:column;gap:7px;
  transition:border-color .12s,box-shadow .12s;
}
.card:hover{border-color:var(--accent);box-shadow:var(--shadow)}
.card-head{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:11.5px}
.card-head .src{color:var(--accent);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card-head .date{color:var(--text-mute);font-variant-numeric:tabular-nums;white-space:nowrap;
  display:flex;align-items:center;gap:6px}
.badge-tr{font-size:10px;background:var(--accent-soft);color:var(--accent);
  padding:1px 5px;border-radius:4px;font-weight:600}
.badge-new{font-size:10px;background:#dc2626;color:#fff;
  padding:1px 6px;border-radius:4px;font-weight:700;letter-spacing:.03em}
:root:not([data-theme="light"]) .badge-new{background:#ef4444}
:root[data-theme="dark"] .badge-new{background:#ef4444}
.card-title{margin:0;font-size:15px;font-weight:600;line-height:1.4}
.card-title a{color:var(--text);text-decoration:none}
.card-title a:hover{color:var(--accent)}
.card-sum{margin:0;font-size:13px;color:var(--text-dim);line-height:1.5;
  display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.card-orig{margin:0;font-size:11.5px;color:var(--text-mute);line-height:1.4;
  font-style:italic;border-top:1px dashed var(--border);padding-top:6px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card-link{font-size:12.5px;color:var(--accent);text-decoration:none;margin-top:auto;font-weight:500}
.card-link:hover{text-decoration:underline}

/* 카드 클릭 가능 표시 */
.card{cursor:pointer}
.card-title{cursor:pointer}
.card .card-link{cursor:pointer}
.card-sum{cursor:pointer}

/* 모달 팝업 */
.modal-overlay{display:none;position:fixed;inset:0;z-index:100;
  background:rgba(0,0,0,.5);padding:20px;overflow-y:auto}
.modal-overlay.active{display:flex;align-items:flex-start;justify-content:center}
.modal{
  background:var(--surface);border:1px solid var(--border);border-radius:12px;
  max-width:680px;width:100%;margin:40px auto;padding:0;
  box-shadow:0 8px 32px rgba(0,0,0,.2);overflow:hidden}
.modal-head{display:flex;align-items:flex-start;gap:12px;padding:18px 22px;
  border-bottom:1px solid var(--border)}
.modal-head h3{margin:0;font-size:17px;font-weight:600;line-height:1.35;flex:1;
  padding-right:30px}
.modal-close{background:none;border:none;font-size:24px;color:var(--text-mute);
  cursor:pointer;padding:0;line-height:1;flex-shrink:0}
.modal-close:hover{color:var(--text)}
.modal-body{padding:18px 22px;max-height:60vh;overflow-y:auto}
.modal-meta{display:flex;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--text-mute);
  margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)}
.modal-meta span{background:var(--surface-2);padding:3px 8px;border-radius:6px}
.modal-summary{font-size:14px;line-height:1.65;color:var(--text);white-space:pre-wrap;
  word-break:break-word}
.modal-orig{margin-top:12px;padding-top:10px;border-top:1px dashed var(--border);
  font-size:12.5px;color:var(--text-mute);font-style:italic}
.modal-foot{padding:14px 22px;border-top:1px solid var(--border);
  display:flex;justify-content:flex-end}
.modal-link-btn{background:var(--accent);color:#fff;border:none;padding:10px 20px;
  border-radius:8px;cursor:pointer;font-size:14px;font-weight:500;text-decoration:none}
.modal-link-btn:hover{opacity:.9}

.col-empty{text-align:center;padding:30px 16px;color:var(--text-mute);font-size:13px;
  grid-column:1/-1}

footer{border-top:1px solid var(--border);padding:24px 0;color:var(--text-mute);font-size:12.5px}
footer a{color:var(--text-dim)}
@media (max-width:560px){
  .hero h1{font-size:23px}
  .head-right .update-info{display:none}
  .cards{grid-template-columns:1fr}
  .controls{top:58px}
}
"""


JS = r"""
const root = document.documentElement;
const stored = (()=>{try{return localStorage.getItem('semi-trends-theme')}catch(e){return null}})();
if(stored){root.setAttribute('data-theme',stored)}
const themeBtn = document.getElementById('theme-btn');
function currentTheme(){
  return root.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
}
function updateThemeBtn(){themeBtn.textContent = currentTheme()==='dark'?'🌙 어둡게':'☀️ 밝게';}
updateThemeBtn();
themeBtn.addEventListener('click',()=>{
  const next = currentTheme()==='dark'?'light':'dark';
  root.setAttribute('data-theme',next);
  try{localStorage.setItem('semi-trends-theme',next)}catch(e){}
  updateThemeBtn();
});

// 필터와 검색 상태를 분리하여 충돌 방지.
// currentFilter: 현재 선택된 카테고리 필터. 'all' 또는 카테고리 키.
let currentFilter = 'all';
const filterBtns = document.querySelectorAll('.filter-btn');
const sections = document.querySelectorAll('.cat-section');
const search = document.getElementById('search');

function applyAll(){
  const q = search.value.trim().toLowerCase();
  sections.forEach(sec=>{
    // 1) 필터: 카테고리 불일치면 섹션 자체를 숨김
    if(currentFilter !== 'all' && sec.dataset.cat !== currentFilter){
      sec.style.display = 'none';
      return;
    }
    // 2) 검색: 검색어가 있으면 카드별 필터링
    if(!q){
      // 검색어 없으면 섹션 표시, 모든 카드 표시
      sec.style.display = '';
      sec.querySelectorAll('.card').forEach(c=>c.style.display='');
    } else {
      let anyVisible = false;
      sec.querySelectorAll('.card').forEach(card=>{
        const text = (card.textContent||'').toLowerCase();
        const show = text.includes(q);
        card.style.display = show?'':'none';
        if(show) anyVisible = true;
      });
      // 섹션에 보일 카드가 없으면 섹션 숨김
      sec.style.display = anyVisible?'':'none';
    }
  });
}

filterBtns.forEach(btn=>{
  btn.addEventListener('click',()=>{
    filterBtns.forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    applyAll();
  });
});

search.addEventListener('input', applyAll);

// --- 모달 팝업: 카드 클릭 시 전체 요약 표시 ---
const overlay = document.getElementById('modal-overlay');
const modalTitle = document.getElementById('modal-title');
const modalMeta = document.getElementById('modal-meta');
const modalSummary = document.getElementById('modal-summary');
const modalOrig = document.getElementById('modal-orig');
const modalLink = document.getElementById('modal-link');

function openModal(data){
  modalTitle.textContent = data.title || '';
  // 메타 정보 (출처, 날짜)
  let metaHtml = '';
  if(data.source) metaHtml += `<span>${data.source}</span>`;
  if(data.date) metaHtml += `<span>${data.date}</span>`;
  if(data.lang === 'en') metaHtml += `<span>번역</span>`;
  modalMeta.innerHTML = metaHtml;
  // 전체 요약 (" | " 구분자를 줄바꿈으로 변환해 가독성 향상)
  const sumText = (data.summary || '(요약 없음)').replace(/\s\|\s/g, '\n');
  modalSummary.textContent = sumText;
  // 원문 제목 (번역 기사)
  if(data.original_title && data.lang === 'en'){
    modalOrig.textContent = '원문: ' + data.original_title;
    modalOrig.style.display = '';
  } else {
    modalOrig.style.display = 'none';
  }
  // 원문 링크 버튼
  if(data.link){
    modalLink.href = data.link;
    modalLink.style.display = '';
  } else {
    modalLink.style.display = 'none';
  }
  overlay.classList.add('active');
}
function closeModal(){
  overlay.classList.remove('active');
}

// 카드 클릭 → 모달 열기 (원문 링크 클릭은 제외)
document.addEventListener('click', (e)=>{
  // 원문 보기 링크 클릭은 모달 없이 링크로 이동
  if(e.target.closest('.card-link')) return;
  const card = e.target.closest('.card');
  if(!card || !card.dataset.popup) return;
  try{
    const data = JSON.parse(card.dataset.popup);
    openModal(data);
  }catch(err){}
});
// 모달 닫기: 오버레이 클릭, 닫기 버튼, ESC
overlay.addEventListener('click', (e)=>{
  if(e.target === overlay) closeModal();
});
document.getElementById('modal-close-btn').addEventListener('click', closeModal);
document.addEventListener('keydown', (e)=>{
  if(e.key === 'Escape') closeModal();
});
"""


def load_legislation_cards() -> list[dict]:
    """legislation.json을 읽어 법령 변경 카드 목록으로 변환.
    API 인증키가 없으면 빈 목록(법령 변경 섹션은 기사 기반이 유지됨)."""
    leg_file = IN_FILE.parent / "legislation.json"
    if not leg_file.exists():
        return []
    try:
        leg = json.loads(leg_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    if leg.get("status") != "ok":
        return []
    cards = []
    for law in leg.get("laws", []):
        if law.get("status") != "조회 완료":
            continue
        eff_fmt = law.get("effect_date_fmt", "")
        prom_fmt = law.get("promulgate_date_fmt", "")
        prom = law.get("promulgate_date", "")
        title = law.get("law_name") or law.get("target_name", "")
        amend = law.get("amend_type", "")
        ministry = law.get("ministry", "")
        link = law.get("detail_link", "") or f"https://www.law.go.kr/lsInfoP.do?lsiSeq={law.get('law_seq','')}"
        reason = law.get("amend_reason", "")
        amended_arts = law.get("amended_articles", "")
        is_new = law.get("is_new", False)

        # 요약 구성: 개정구분 | 공포 | 시행 | 소관 + 개정 이유 + 개정 조항
        summary = f"{amend} | 공포 {prom_fmt} | 시행 {eff_fmt}"
        if ministry:
            summary += f" | 소관: {ministry}"
        # 개정된 조항 목록
        if amended_arts:
            summary += f" | 개정 조항: {amended_arts}"
        # 개정 이유 (너무 길면 300자로 제한)
        if reason:
            reason_short = reason[:300]
            if len(reason) > 300:
                reason_short += "…"
            summary += f" | 개정 이유: {reason_short}"

        cards.append({
            "title": f"[법령] {title}",
            "link": link,
            "summary": summary,
            "category": "legislation",
            "source": "법제처 (공공데이터포털 API)",
            "lang": "ko",
            "date": f"{prom[:4]}-{prom[4:6]}-{prom[6:8]}T00:00:00+09:00" if len(prom) == 8 else None,
            "original_title": "",
            "is_new": is_new,
        })
    return cards


def build_page():
    if not IN_FILE.exists():
        print("[build] articles.json 없음. 먼저 python fetcher.py 실행하세요.")
        return
    data = json.loads(IN_FILE.read_text(encoding="utf-8"))
    articles = data.get("articles", [])
    cats_meta = data.get("categories", {})
    fetched = fmt_fetched(data.get("fetched_at", ""))
    # 기사 기반 legislation 카테고리는 제외 (법령 변경은 API 데이터만 표시)
    articles = [a for a in articles if a.get("category") != "legislation"]
    # 법령 추적 카드(API 데이터)를 legislation 카테고리에 추가
    leg_cards = load_legislation_cards()
    if leg_cards:
        articles = leg_cards + articles
    counts = build_counts(articles)
    sections_html = build_sections(articles, cats_meta)
    nav_html = build_nav(cats_meta, counts)
    total = len(articles)
    feed_ok = data.get("feed_ok", 0)
    feed_fail = data.get("feed_fail", 0)

    page = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>반도체 산업 동향 브리핑</title>
<style>{CSS}</style>
</head>
<body>
<header class="site-head">
  <div class="wrap head-row">
    <div class="logo"><span class="dot"></span>반도체 산업 동향 브리핑 <small>반도체 · 환경 · 안전 · 규제 · 법령</small></div>
    <div class="head-right">
      <div class="update-info"><b>마지막 업데이트</b><br>{fetched}</div>
      <button class="theme-btn" id="theme-btn">☀️ 밝게</button>
    </div>
  </div>
</header>
<div class="wrap">
  <section class="hero">
    <h1>반도체 업계의 <span class="accent">반도체·환경·안전·규제</span> 동향을 한곳에서.</h1>
    <p>공신력 있는 출처의 최신 기사를 매일 오전 6시에 자동 수집·분류합니다. 영문 기사는 한국어로 자동 번역됩니다. 법령 변경은 대상 법령의 개정 사항을 공공데이터포털 API로 직접 추적합니다. 각 카드의 "원문 보기"를 누르면 출처 기사로 이동합니다.</p>
    <div class="stat-row">
      <div class="stat"><div class="n">{total}</div><div class="l">수집 기사</div></div>
      <div class="stat"><div class="n">{feed_ok}</div><div class="l">응답 출처</div></div>
      <div class="stat"><div class="n">{feed_fail}</div><div class="l">실패/건너뜀</div></div>
    </div>
  </section>
  <div class="controls">
    <input class="search-box" id="search" type="search" placeholder="제목·요약·출처에서 검색…">
    <div class="filters">{nav_html}</div>
  </div>
  <main>
{sections_html}
  </main>
</div>
<footer><div class="wrap">
  자동 수집된 링크와 요약은 각 출처의 저작권을 따릅니다. 영문 기사는 Google Translate로 자동 번역되어 표시됩니다. 본 페이지는 로컬 연구용으로 생성됩니다.
</div></footer>

<!-- 모달 팝업 (카드 클릭 시 전체 요약 표시) -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <div class="modal-head">
      <h3 id="modal-title"></h3>
      <button class="modal-close" id="modal-close-btn" aria-label="닫기">×</button>
    </div>
    <div class="modal-body">
      <div class="modal-meta" id="modal-meta"></div>
      <div class="modal-summary" id="modal-summary"></div>
      <div class="modal-orig" id="modal-orig" style="display:none"></div>
    </div>
    <div class="modal-foot">
      <a class="modal-link-btn" id="modal-link" href="#" target="_blank" rel="noopener">원문 보기 →</a>
    </div>
  </div>
</div>

<script>{JS}</script>
</body>
</html>"""
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(page, encoding="utf-8")
    print(f"[build] 생성 -> {OUT_FILE} (기사 {total}건)")


if __name__ == "__main__":
    build_page()
