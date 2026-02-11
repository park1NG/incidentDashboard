import os
import json
import hashlib
import sqlite3
import datetime as dt
import re
import html
import time
import traceback
from typing import Optional, Dict, Any, Tuple, List
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import feedparser
from google import genai
from google.genai import types
from dateutil import parser as dateparser
from dotenv import load_dotenv

# ==========================================
# 1. 환경 변수 및 설정 (기존 유지)
# ==========================================
env_path = Path(__file__).resolve().parent / '.env'
if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)

# 디버그 덤프 기능 (사용자 원본 복구)
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"
DEBUG_PATH = "debug_published_at.jsonl"

def dump_debug(obj: dict):
    if not DEBUG_DUMP:
        return
    try:
        with open(DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except:
        pass

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
ARTICLES_DB_ID = os.getenv("ARTICLES_DB_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NOTION_VERSION = "2025-09-03" # 사용자 지정 버전

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")

# 필수 키 검증
if not NOTION_TOKEN or not ARTICLES_DB_ID:
    # GitHub Actions에서는 Secrets가 주입되므로 에러 처리만 함
    print("🚨 Error: NOTION_TOKEN or ARTICLES_DB_ID is missing.")

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
    "User-Agent": "IncidentDashboard/1.0 (+local)",
}

# 설정 (수동 복구 모드용 변수)
UPDATE_EXISTING = True  # <-- 최초 1회만 True (기존 50개 복구용), 이후 False 권장
DRY_RUN = False
SQLITE_PATH = "state.sqlite"

# 소스 (사용자 지정 2개)
RSS_FEEDS = [
    ("데일리시큐", "https://www.dailysecu.com/rss/allArticle.xml"),
    ("보안뉴스", "http://www.boannews.com/media/news_rss.xml"),
]

NAVER_KEYWORDS = [
    "침해사고", "개인정보 유출", "해킹", "랜섬웨어", "DDoS", "계정 탈취", "피싱", "취약점 악용",
    "악성코드", "스미싱 피해"
]

# ==========================================
# 2. SQLite 로컬 DB (사용자 원본 기능 복구)
# ==========================================
def init_sqlite():
    try:
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
    except Exception as e:
        print(f"⚠️ SQLite Init Error (GitHub Actions에서는 무시 가능): {e}")

def seen_fingerprint(fp: str) -> Optional[str]:
    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            cur = conn.execute("SELECT notion_page_id FROM articles_seen WHERE fingerprint=?", (fp,))
            row = cur.fetchone()
            return row[0] if row else None
    except:
        return None

def mark_seen(fp: str, source: str, url: str, notion_page_id: str):
    try:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO articles_seen (fingerprint, source, url, notion_page_id, first_seen_at)
                VALUES (?, ?, ?, ?, COALESCE((SELECT first_seen_at FROM articles_seen WHERE fingerprint=?), ?))
            """, (fp, source, url, notion_page_id, fp, now))
            conn.commit()
    except:
        pass

def log_run(status: str, message: str):
    try:
        with sqlite3.connect(SQLITE_PATH) as conn:
            conn.execute("INSERT INTO runs (run_at, status, message) VALUES (?, ?, ?)",
                         (dt.datetime.now(dt.timezone.utc).isoformat(), status, message))
            conn.commit()
    except:
        pass

# ==========================================
# 3. 유틸리티 함수
# ==========================================
TAG_RE = re.compile(r"<[^>]+>")
def clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = TAG_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()

def parse_datetime_any(raw: Optional[str]):
    if not raw: return None
    try:
        d = dateparser.parse(raw)
        # 타임존이 없으면(naive) -> KST로 간주
        if not d.tzinfo:
            d = d.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        return d.astimezone(dt.timezone.utc).isoformat()
    except Exception:
        # 파싱 실패 시 현재 시간
        return dt.datetime.now(dt.timezone.utc).isoformat()

# ==========================================
# 4. 데이터 수집 (Collector - 디버그 덤프 복구)
# ==========================================
def fetch_rss(url: str) -> feedparser.FeedParserDict:
    r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    r.raise_for_status()
    return feedparser.parse(r.content)

def collect_from_rss():
    out = []
    print("📡 RSS 뉴스 수집 중...")
    for source_name, feed_url in RSS_FEEDS:
        try:
            feed = fetch_rss(feed_url)
        except Exception:
            continue

        for e in (feed.entries[:30] or []):
            url = e.get("link")
            title = clean_text(e.get("title", ""))
            if not url or not title: continue

            pub_date = parse_datetime_any(e.get("published") or e.get("updated"))
            
            # [복구] 디버그 덤프
            dump_debug({
                "source": source_name, "title": title, "url": url, "pub_raw": e.get("published")
            })

            summary = clean_text(e.get("summary") or e.get("description") or "")
            out.append({
                "source": source_name, "title": title, "url": url,
                "published_iso": pub_date, "summary": summary[:500]
            })
    return out

def collect_from_naver():
    if not NAVER_CLIENT_ID: return []
    out = []
    print("📡 네이버 뉴스 수집 중...")
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    
    for kw in NAVER_KEYWORDS:
        try:
            url = "https://openapi.naver.com/v1/search/news.json"
            params = {"query": kw, "display": 20, "start": 1, "sort": "date"}
            r = requests.get(url, headers=headers, params=params, timeout=30)
            if r.status_code == 200:
                for it in r.json().get('items', []):
                    title = clean_text(it.get("title", ""))
                    summary = clean_text(it.get("description", ""))
                    link = it.get("originallink") or it.get("link")
                    pub_date = parse_datetime_any(it.get("pubDate"))

                    dump_debug({
                        "source": "네이버뉴스", "keyword": kw, "title": title, "url": link
                    })

                    out.append({
                        "source": "네이버뉴스", "title": title, "url": link,
                        "published_iso": pub_date, "summary": summary[:500]
                    })
            time.sleep(0.1)
        except Exception: continue
    return out

# ==========================================
# 5. AI 분석 (Gemini) - [사용자 검증 프롬프트]
# ==========================================
def analyze_articles_batch(items):
    api_key = GEMINI_API_KEY
    if not api_key: return {}

    client = genai.Client(api_key=api_key)
    results = {} 
    BATCH_SIZE = 30
    
    print(f"\n🤖 AI 분석 시작 (대상: {len(items)}개)...")

    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i : i + BATCH_SIZE]
        
        batch_input = []
        for idx, item in enumerate(batch):
            batch_input.append({
                "custom_id": i + idx, 
                "title": item['title'], 
                "summary": item['summary']
            })

        batch_idx = (i // BATCH_SIZE) + 1
        print(f"   ➤ AI Batch {batch_idx} 전송 중...")

        # [절대 수정 금지] 사용자 검증 완료된 프롬프트
        prompt_text = (
            "You are an elite Cyber Security Analyst. Your goal is to identify ACTUAL technical security breaches, hacks, and vulnerabilities.\n"
            "Analyze the following news articles and classify them strictly.\n"
            "\n"
            "CRITICAL RULES for 'ai_status':\n"
            "1. 'Critical': MUST be a concrete, confirmed cyber attack event, data breach incident, zero-day vulnerability, or ransomware infection happening NOW or recently.\n"
            "   - STRICTLY EXCLUDE: Opinions, Editorials(사설/칼럼), Financial Reports(Earnings, Stock prices, Sales), and General Statistics/Surveys.\n"
            "   - IF the article is about 'Business Performance' (e.g., Sales, Operating Profit, Stock) even if it mentions security companies -> Set to 'Info'.\n"
            "   - IF the article is about 'Future Predictions' or 'Market Trends' (e.g., 'Risks will increase...') -> Set to 'Info'.\n"
            "   - IF the article is a 'Government/Company Statement' regarding an already known incident (e.g., Apology, Fine announcement) -> Set to 'Warning'.\n"
            "\n"
            "2. 'Warning': High-risk trends, generic phishing warnings (without specific victims), legal/compliance issues (Fines, Lawsuits), or government countermeasures.\n"
            "3. 'Info': General tech news, new product launches, policies, advertisements, promotions, appointments(인사), educational seminars, or business/financial news.\n"
            "4. 'Ignore': Entertainment news (dramas, movies), celebrity gossip, sports, or political scandals not related to cyber warfare.\n"
            "\n"
            "Specific Logic Override:\n"
            "- Drama/Movie/Webtoon plot: Set status to 'Info'.\n"
            "- Celebrity privacy (e.g., tax info): Set status to 'Warning'.\n"
            "- Marketing/Ad/Event: Set status to 'Info'.\n"
            "- Financial Results/Stocks: Set status to 'Info'.\n"
            "\n"
            "Return a strictly valid JSON list of objects.\n"
            "Each object MUST contain:\n"
            "- 'custom_id': The exact integer ID provided in the input.\n"
            "- 'ai_status': 'Critical', 'Warning', or 'Info'.\n"
            "- 'ai_risk': 'High', 'Medium', or 'Low'.\n"
            "- 'ai_reason': A short justification in Korean (Explain WHY).\n\n"
            "Input Data:\n" + json.dumps(batch_input, ensure_ascii=False)
        )

        try:
            resp = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json', 
                    temperature=0.0
                )
            )
            
            if resp.text:
                for res in json.loads(resp.text):
                    c_id = res.get('custom_id')
                    if c_id is not None and c_id < len(items):
                        original_url = items[c_id]['url']
                        results[original_url] = res
                print(f"      ✅ Batch {batch_idx} 성공")
            time.sleep(2) 

        except Exception as e:
            print(f"   ❌ Batch Error: {e}")
            
    return results

# ==========================================
# 6. Notion API 함수
# ==========================================
def query_page_id_by_fingerprint(fp):
    url = f"https://api.notion.com/v1/databases/{ARTICLES_DB_ID}/query"
    payload = {
        "filter": {"property": "Fingerprint", "rich_text": {"equals": fp}},
        "page_size": 1
    }
    try:
        r = requests.post(url, headers=HEADERS, json=payload)
        res = r.json().get("results")
        return res[0]["id"] if res else None
    except:
        return None

def create_notion_page(props):
    url = "https://api.notion.com/v1/pages"
    body = {"parent": {"database_id": ARTICLES_DB_ID}, "properties": props}
    r = requests.post(url, headers=HEADERS, json=body)
    r.raise_for_status()

def update_notion_page(page_id, props):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    body = {"properties": props}
    r = requests.patch(url, headers=HEADERS, json=body)
    r.raise_for_status()

# ==========================================
# 7. 메인 실행 (하이브리드 로직)
# ==========================================
def main():
    init_sqlite() # 로컬 DB 초기화 (Actions에선 무의미하나 로컬 테스트 위해 유지)
    
    print(f"🚀 Job Start (Update Mode: {UPDATE_EXISTING})")
    
    # 1. 수집
    raw_items = collect_from_rss() + collect_from_naver()
    dedup = {x['url']: x for x in raw_items}
    unique_items = list(dedup.values())
    print(f"📥 수집 완료: {len(unique_items)}개")

    # 2. 분석 및 저장 대상 선별 (로컬 DB + Notion API 하이브리드)
    items_to_process = []
    print("🔍 Notion 중복 검사 중...")
    
    for item in unique_items:
        fp = hashlib.sha256(f"{item['source']}|{item['url']}".encode("utf-8")).hexdigest()
        item['fingerprint'] = fp
        
        # 1차: 로컬 DB 확인 (로컬 실행 속도 향상용)
        # 단, UPDATE_EXISTING=True 일때는 무시하고 다시 체크
        if not UPDATE_EXISTING and seen_fingerprint(fp):
            continue

        # 2차: Notion API 확인 (GitHub Actions용 필수)
        # DRY_RUN일 때는 API 호출 아끼기 위해 스킵 가능하나 여기선 정확성 위해 호출
        page_id = query_page_id_by_fingerprint(fp)
        
        if page_id:
            if UPDATE_EXISTING:
                item['page_id'] = page_id
                items_to_process.append(item)
            else:
                # 이미 있으면 로컬 DB에도 업데이트해주고 스킵
                mark_seen(fp, item['source'], item['url'], page_id)
                continue
        else:
            # 없으면 추가
            item['page_id'] = None
            items_to_process.append(item)
            
        time.sleep(0.2) 

    print(f"✨ 최종 처리 대상: {len(items_to_process)}개")

    if not items_to_process:
        print("✅ 처리할 기사가 없습니다.")
        return

    # 3. AI 분석
    ai_results = analyze_articles_batch(items_to_process)

    # 4. 저장
    print("💾 Notion 반영 시작...")
    success = 0
    errors = 0
    
    for item in items_to_process:
        ai = ai_results.get(item['url'], {})
        
        props = {
            "Title": {"title": [{"text": {"content": item['title'][:2000]}}]},
            "Source": {"select": {"name": item['source']}},
            "URL": {"url": item['url']},
            "Published At": {"date": {"start": item['published_iso']}} if item['published_iso'] else None,
            "Fingerprint": {"rich_text": [{"text": {"content": item['fingerprint']}}]},
            "Summary": {"rich_text": [{"text": {"content": item['summary'][:2000]}}]},
            "AI_Status": {"select": {"name": ai.get("ai_status", "Info")}},
            "Risk_Level": {"select": {"name": ai.get("ai_risk", "Low")}},
            "AI_Reason": {"rich_text": [{"text": {"content": ai.get("ai_reason", "-")[:2000]}}]},
        }
        props = {k: v for k, v in props.items() if v is not None}

        if DRY_RUN:
            continue

        try:
            if item.get('page_id'):
                update_notion_page(item['page_id'], props)
                print(f"   🔄 [Updated] {item['title'][:30]}...")
                mark_seen(item['fingerprint'], item['source'], item['url'], item['page_id'])
            else:
                create_notion_page(props)
                status_icon = "🟢" if ai.get("ai_status") == "Critical" else "⚪"
                print(f"   {status_icon} [Created] {item['title'][:30]}...")
                # 새 페이지 ID는 여기서 굳이 받아오지 않고 'new'로 로컬 마킹
                mark_seen(item['fingerprint'], item['source'], item['url'], 'new_page')
            
            success += 1
            time.sleep(0.6) 
        except Exception as e:
            print(f"   ⚠️ [Fail] {e}")
            errors += 1

    msg = f"Created/Updated: {success}, Errors: {errors}"
    print(f"🎉 작업 완료! ({msg})")
    log_run("ok" if errors == 0 else "partial", msg)

if __name__ == "__main__":
    main()