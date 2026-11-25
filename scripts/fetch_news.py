"""PE 인수금융 레이더 - 뉴스 자동 수집 스크립트.

GitHub Actions가 매일 아침 실행합니다. 외부 패키지 없이 파이썬 기본 기능만 씁니다.
- 구글 뉴스 RSS: 설정 없이 동작
- 네이버 뉴스 검색 API: NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 시크릿을 등록하면 함께 사용

결과는 data/news.json 에 합쳐서 저장합니다 (기존 기사 유지, 중복 제거, 오래된 기사 정리).
"""
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_PATH = os.path.join(ROOT, "data", "news.json")
PE_PATH = os.path.join(ROOT, "data", "pe.json")
KST = timezone(timedelta(hours=9))

# ---- 여기만 고치면 수집 범위를 바꿀 수 있어요 ----
TOPIC_QUERIES = [
    "인수금융",
    "리파이낸싱 사모펀드",
    "인수금융 브릿지론",
    "사모펀드 경영권 인수",
    "사모펀드 우선협상대상자",
    "사모펀드 공개매수",
    "블라인드펀드 결성",
    "PEF 리캡",
]
SEARCH_PE_NAMES = True      # PE 이름으로도 검색할지
LOOKBACK = "2d"             # 구글 뉴스 검색 기간 (최근 2일)
RETAIN_DAYS = 90            # 이 기간보다 오래된 기사는 목록에서 정리
MAX_ITEMS = 400
EXCLUDE = ["부동산PF", "부동산 PF", "분양", "재건축", "재개발", "코인", "가상자산"]
MUST_HAVE_ANY = ["인수", "매각", "펀드", "리파이낸싱", "차환", "리캡", "공개매수",
                 "사모", "PEF", "PE", "바이아웃", "브릿지", "대주단", "주선", "M&A", "지분"]
TAG_RULES = [
    ("리파이낸싱·리캡", ["리파이낸싱", "차환", "리캡", "자본재조정", "만기 연장", "만기연장"]),
    ("브릿지론", ["브릿지론", "브릿지 론"]),
    ("공개매수", ["공개매수", "상장폐지", "자진상폐"]),
    ("펀드레이징", ["블라인드펀드", "블라인드 펀드", "펀드 결성", "출자사업", "위탁운용사", "펀드레이징"]),
    ("신규 인수", ["인수", "주식매매계약", "SPA", "우선협상", "우협", "경영권"]),
    ("매각", ["매각", "예비입찰", "본입찰", "엑시트", "회수"]),
    ("규제·리스크", ["제재", "금감원", "금융감독원", "공정위", "소송", "징계"]),
]
# ---------------------------------------------------

UA = "Mozilla/5.0 (compatible; pe-radar-bot/1.0)"


def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def to_kst_date(pub):
    try:
        dt = email.utils.parsedate_to_datetime(pub)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(KST).strftime("%Y-%m-%d")


def clean(text):
    text = re.sub(r"<[^>]+>", "", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_google_rss(xml_bytes):
    """구글 뉴스 RSS -> 기사 목록"""
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        src_el = it.find("source")
        source = clean(src_el.text) if src_el is not None and src_el.text else ""
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        out.append({
            "date": to_kst_date(it.findtext("pubDate")),
            "title": title,
            "summary": "",
            "source": source or "구글 뉴스",
            "url": (it.findtext("link") or "").strip(),
        })
    return out


def google_news(query):
    q = urllib.parse.quote(f"{query} when:{LOOKBACK}")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    return parse_google_rss(http_get(url))


def naver_news(query):
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not sec:
        return []
    url = "https://openapi.naver.com/v1/search/news.json?display=30&sort=date&query=" + urllib.parse.quote(query)
    data = json.loads(http_get(url, {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}))
    out = []
    for it in data.get("items", []):
        link = it.get("originallink") or it.get("link") or ""
        host = urllib.parse.urlparse(link).netloc.replace("www.", "")
        out.append({
            "date": to_kst_date(it.get("pubDate", "")),
            "title": clean(it.get("title")),
            "summary": clean(it.get("description"))[:160],
            "source": host or "네이버 뉴스",
            "url": link,
        })
    return out


def norm(title):
    return re.sub(r"[\W_]+", "", title).lower()


def relevant(item):
    t = item["title"] + " " + item["summary"]
    if any(x in t for x in EXCLUDE) and "인수금융" not in t:
        return False
    return any(x in t for x in MUST_HAVE_ANY)


def tag(item):
    t = item["title"] + " " + item["summary"]
    tags = [name for name, words in TAG_RULES if any(w in t for w in words)]
    return tags[:3] or ["시장 동향"]


def match_pe(item, pes):
    t = item["title"] + " " + item["summary"]
    found = []
    for p in pes:
        for a in p.get("aliases") or [p["name"]]:
            # 영문 약칭(IMM, EQT 등)은 단어 경계로 확인
            pat = r"(?<![A-Za-z])" + re.escape(a) + r"(?![A-Za-z])" if re.fullmatch(r"[A-Za-z&]+", a) else re.escape(a)
            if re.search(pat, t):
                found.append(p["name"])
                break
    return found


def merge(existing, new_items, pes, today):
    seen_url = {i["url"] for i in existing}
    seen_title = {norm(i["title"]) for i in existing}
    added = 0
    for it in new_items:
        if not it["url"].startswith("http") or not it["title"]:
            continue
        if it["url"] in seen_url or norm(it["title"]) in seen_title or not relevant(it):
            continue
        it["tags"] = tag(it)
        it["pe"] = match_pe(it, pes)
        it["auto"] = True
        existing.append(it)
        seen_url.add(it["url"])
        seen_title.add(norm(it["title"]))
        added += 1
    for it in existing:  # 직접 넣은 기사도 PE 언급을 다시 계산
        it["pe"] = match_pe(it, pes)
    cutoff = (today - timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%d")
    kept = [i for i in existing if i["date"] >= cutoff]
    kept.sort(key=lambda i: i["date"], reverse=True)
    return kept[:MAX_ITEMS], added


def main():
    with open(NEWS_PATH, encoding="utf-8") as f:
        news = json.load(f)
    with open(PE_PATH, encoding="utf-8") as f:
        pes = json.load(f)["items"]

    queries = list(TOPIC_QUERIES)
    if SEARCH_PE_NAMES:
        queries += [f'"{p["name"]}" (인수 OR 매각 OR 펀드 OR 인수금융)' for p in pes]

    collected, errors = [], 0
    for q in queries:
        for fn in (google_news, naver_news):
            try:
                collected += fn(q.replace('"', "") if fn is naver_news else q)
            except Exception as e:  # 한 검색이 실패해도 계속 진행
                errors += 1
                print(f"[경고] {fn.__name__} '{q}' 실패: {e}", file=sys.stderr)
            time.sleep(1)

    now = datetime.now(KST)
    items, added = merge(news.get("items", []), collected, pes, now)
    if errors and not collected:
        print("모든 검색이 실패했어요. 기존 파일을 그대로 둡니다.", file=sys.stderr)
        sys.exit(1)
    news["items"] = items
    news["updated_at"] = now.isoformat(timespec="minutes")
    with open(NEWS_PATH, "w", encoding="utf-8") as f:
        json.dump(news, f, ensure_ascii=False, indent=1)
    print(f"수집 {len(collected)}건 중 새 기사 {added}건 추가, 총 {len(items)}건 (실패한 검색 {errors}개)")


if __name__ == "__main__":
    main()
