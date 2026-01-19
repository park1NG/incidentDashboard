import os, json, hashlib, sqlite3, datetime as dt, re, html
from typing import Optional, Dict, Any, Tuple, List
from zoneinfo import ZoneInfo

import requests
import feedparser
from dateutil import parser as dateparser
from dotenv import load_dotenv

load_dotenv()

DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"
DEBUG_PATH = "debug_published_at.jsonl"
UPDATE_EXISTING = os.getenv("UPDATE_EXISTING", "0") == "1"

def dump_debug(obj: dict):
    if not DEBUG_DUMP:
        return
    with open(DEBUG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------- ENV ----------
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
ARTICLES_DB_ID = os.getenv("ARTICLES_DB_ID")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03")

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

if not NOTION_TOKEN or not ARTICLES_DB_ID:
    raise SystemExit("NOTION_TOKEN 또는 ARTICLES_DB_ID가 .env에 없음")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
    "User-Agent": "IncidentDashboard/1.0 (+local)",
}

SQLITE_PATH = "state.sqlite"

# ---------- SOURCES ----------
RSS_FEEDS = [
    ("데일리시큐", "https://www.dailysecu.com/rss/allArticle.xml"),
    ("보안뉴스", "http://www.boannews.com/media/news_rss.xml"),
]

# 네이버는 “보안/해킹 탭” 직접 호출이 아니라, 검색 API이므로 키워드 기반으로 최신순 수집
NAVER_KEYWORDS = [
    "침해사고", "개인정보 유출", "해킹", "랜섬웨어", "DDoS", "계정 탈취", "피싱", "취약점 악용"
]

# ---------- SQLite (중복 방지) ----------
def init_sqlite():
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles_seen (
                fingerprint TEXT PRIMARY KEY,
                source TEXT,
                url TEXT,
                notion_page_id TEXT,
                first_seen_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_at TEXT,
                status TEXT,
                message TEXT
            )
        """)
        conn.commit()

def seen_fingerprint(fp: str) -> Optional[str]:
    with sqlite3.connect(SQLITE_PATH) as conn:
        cur = conn.execute("SELECT notion_page_id FROM articles_seen WHERE fingerprint=?", (fp,))
        row = cur.fetchone()
        return row[0] if row else None

def mark_seen(fp: str, source: str, url: str, notion_page_id: str):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO articles_seen (fingerprint, source, url, notion_page_id, first_seen_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT first_seen_at FROM articles_seen WHERE fingerprint=?), ?))
        """, (fp, source, url, notion_page_id, fp, now))
        conn.commit()

def log_run(status: str, message: str):
    with sqlite3.connect(SQLITE_PATH) as conn:
        conn.execute("INSERT INTO runs (run_at, status, message) VALUES (?, ?, ?)",
                     (dt.datetime.now(dt.timezone.utc).isoformat(), status, message))
        conn.commit()

# ---------- Notion (2025-09-03 data_source 방식) ----------
def retrieve_database(database_id: str) -> dict:
    r = requests.get(f"https://api.notion.com/v1/databases/{database_id}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def retrieve_data_source(data_source_id: str) -> dict:
    r = requests.get(f"https://api.notion.com/v1/data_sources/{data_source_id}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def query_data_source(data_source_id: str, payload: dict) -> dict:
    r = requests.post(f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
                      headers=HEADERS, data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()

def create_page_in_data_source(data_source_id: str, properties: dict) -> dict:
    payload = {"parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties}
    r = requests.post("https://api.notion.com/v1/pages", headers=HEADERS, data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()

def update_page(page_id: str, properties: dict) -> dict:
    r = requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=HEADERS,
                       data=json.dumps({"properties": properties}), timeout=30)
    r.raise_for_status()
    return r.json()

def discover_articles_data_source_id() -> Tuple[str, str]:
    db = retrieve_database(ARTICLES_DB_ID)
    ds = (db.get("data_sources") or [])
    if not ds:
        raise RuntimeError("ARTICLES_DB_ID Database에 data_sources가 없음(원본 DB인지 확인 필요)")
    return ds[0]["id"], ds[0].get("name", "")

def pick_title_property_name(ds: dict) -> str:
    props = ds.get("properties", {}) or {}
    for name, meta in props.items():
        if meta.get("type") == "title":
            return name
    raise RuntimeError("Data Source에서 title 타입 속성을 찾지 못함")

def build_properties(ds: dict, title_text: str, source: str, url: str,
                     published_iso: Optional[str], ingested_iso: str,
                     summary: Optional[str], fingerprint: str) -> Dict[str, Any]:
    props = ds.get("properties", {}) or {}
    title_name = pick_title_property_name(ds)

    out: Dict[str, Any] = {
        title_name: {"title": [{"type": "text", "text": {"content": title_text}}]},
        "Source": {"select": {"name": source}},
        "URL": {"url": url},
        "Ingested At": {"date": {"start": ingested_iso}},
        "Fingerprint": {"rich_text": [{"type": "text", "text": {"content": fingerprint}}]},
    }
    if published_iso:
        out["Published At"] = {"date": {"start": published_iso}}
    if summary:
        out["Summary"] = {"rich_text": [{"type": "text", "text": {"content": summary[:1900]}}]}
    return out

def query_page_id_by_fingerprint(data_source_id: str, fingerprint: str) -> Optional[str]:
    payload = {
        "page_size": 1,
        "filter": {"property": "Fingerprint", "rich_text": {"equals": fingerprint}}
    }
    res = query_data_source(data_source_id, payload)
    results = res.get("results") or []
    return results[0].get("id") if results else None

# ---------- Utils ----------
TAG_RE = re.compile(r"<[^>]+>")

def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = TAG_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()

def parse_datetime_any(raw: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    return: (iso_utc, iso_kst, raw)
    """
    if not raw:
        return (None, None, None)
    try:
        d = dateparser.parse(raw)
        # 타임존이 없으면(naive) -> 일단 KST로 간주(한국 기사/네이버 특성상 안전)
        if not d.tzinfo:
            d = d.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        d_utc = d.astimezone(dt.timezone.utc)
        d_kst = d.astimezone(ZoneInfo("Asia/Seoul"))
        return (d_utc.isoformat(), d_kst.isoformat(), raw)
    except Exception:
        return (None, None, raw)


# ---------- Collectors ----------
def fetch_rss(url: str) -> feedparser.FeedParserDict:
    r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    r.raise_for_status()
    return feedparser.parse(r.content)

def collect_from_rss() -> List[Dict[str, Any]]:
    out = []
    for source_name, feed_url in RSS_FEEDS:
        try:
            feed = fetch_rss(feed_url)
        except Exception:
            continue

        for e in (feed.entries[:50] or []):
            url = e.get("link")
            title = clean_text(e.get("title", ""))
            if not url or not title:
                continue

            raw = e.get("published") or e.get("updated")
            pub_utc, pub_kst, pub_raw = parse_datetime_any(raw)

            dump_debug({
                "source": source_name,
                "title": title,
                "url": url,
                "pub_raw": pub_raw,
                "pub_utc": pub_utc,
                "pub_kst": pub_kst,
                "chosen_for_notion": pub_kst,   # ✅ Notion에 넣을 값
            })

            summary = clean_text(e.get("summary") or e.get("description") or "")

            out.append({
                "source": source_name,
                "title": title,
                "url": url,
                "published_iso": pub_kst,  # ✅ 문자열만
                "summary": summary,
            })
    return out

def collect_from_naver() -> List[Dict[str, Any]]:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []

    api_url = "https://openapi.naver.com/v1/search/news.json"
    api_headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    out = []
    for kw in NAVER_KEYWORDS:
        params = {"query": kw, "display": 50, "start": 1, "sort": "date"}
        r = requests.get(api_url, headers=api_headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        for it in (data.get("items") or []):
            title = clean_text(it.get("title", ""))
            summary = clean_text(it.get("description", ""))
            url = it.get("originallink") or it.get("link")

            pub_raw = it.get("pubDate")
            pub_utc, pub_kst, pub_raw = parse_datetime_any(pub_raw)

            if not url or not title:
                continue

            dump_debug({
                "source": "네이버뉴스",
                "keyword": kw,
                "title": title,
                "url": url,
                "pub_raw": pub_raw,
                "pub_utc": pub_utc,
                "pub_kst": pub_kst,
                "chosen_for_notion": pub_kst,   # ✅ Notion에 넣을 값
            })

            out.append({
                "source": "네이버뉴스",
                "title": title,
                "url": url,
                "published_iso": pub_kst,  # ✅ 문자열만
                "summary": summary,
            })
    return out

# ---------- Main ----------
def main():
    init_sqlite()

    ds_id, ds_name = discover_articles_data_source_id()
    ds = retrieve_data_source(ds_id)
    title_prop = pick_title_property_name(ds)
    print(f"[Notion] Articles data_source: {ds_name} / {ds_id} (title={title_prop})")

    ingested_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    items = []
    items.extend(collect_from_rss())
    items.extend(collect_from_naver())

    # 키워드/소스 중복으로 같은 URL이 많이 나올 수 있어 1차로 URL 기준 dedupe
    dedup = {}
    for x in items:
        dedup[(x["source"], x["url"])] = x
    items = list(dedup.values())

    created = updated = skipped = errors = 0

    for x in items:
        fp = hashlib.sha256(f'{x["source"]}|{x["url"]}'.encode("utf-8")).hexdigest()

        # 로컬 중복 방지
        if seen_fingerprint(fp):
            skipped += 1
            continue

        props = build_properties(
            ds=ds,
            title_text=x["title"],
            source=x["source"],
            url=x["url"],
            published_iso=x["published_iso"],
            ingested_iso=ingested_iso,
            summary=x["summary"],
            fingerprint=fp
        )

        try:
            existing = query_page_id_by_fingerprint(ds_id, fp)
            if existing:
                if UPDATE_EXISTING:
                    update_page(existing, props)
                    updated += 1
                else:
                    skipped += 1
                mark_seen(fp, x["source"], x["url"], existing)
            else:
                page = create_page_in_data_source(ds_id, props)
                pid = page.get("id")
                mark_seen(fp, x["source"], x["url"], pid)
                created += 1
        except Exception as ex:
            resp = getattr(ex, "response", None)
            if resp is not None:
                print(f"[업서트 실패] {x['source']} | {x['title'][:60]}... | {resp.status_code} | {resp.text}")
            else:
                print(f"[업서트 실패] {x['source']} | {x['title'][:60]}... | {ex}")
            errors += 1

    msg = f"created={created}, updated={updated}, skipped={skipped}, errors={errors}"
    print("✅ 완료:", msg)
    log_run("ok" if errors == 0 else "partial", msg)

if __name__ == "__main__":
    main()
