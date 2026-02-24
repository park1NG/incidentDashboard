import time
import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import os
import json
import hashlib
import sqlite3
import datetime as dt
import re
import html
import time
from typing import Optional, Dict, Any, List, Tuple
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import feedparser
from google import genai
from google.genai import types
from dateutil import parser as dateparser
from dotenv import load_dotenv

# ==========================================
# 1. 환경 변수 및 설정
# ==========================================
env_path = Path(__file__).resolve().parent / ".env"

# GitHub Actions 환경에서는 secrets/env를 신뢰하고 .env로 덮어쓰지 않음
if os.getenv("GITHUB_ACTIONS", "").lower() == "true":
    pass
else:
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)

def _require_env(name: str) -> str:
    v = (os.getenv(name) or "").strip()
    if not v:
        raise RuntimeError(f"Missing required env: {name}")
    return v

# -----------------------------
# REQUIRED ENV (여기서 단일 소스로 확정)
# -----------------------------
NOTION_TOKEN = _require_env("NOTION_TOKEN")
ARTICLES_DB_ID = _require_env("ARTICLES_DB_ID")

# (선택) 분류 워크플로우에서만 쓰더라도, 존재하면 읽어둠
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

NAVER_CLIENT_ID = (os.getenv("NAVER_CLIENT_ID") or "").strip()
NAVER_CLIENT_SECRET = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()

# Notion API 버전(고정)
NOTION_VERSION = "2025-09-03"  # 사용자 지정 버전

# -----------------------------
# Sanity check (변수 선언 이후)
# -----------------------------
if len(ARTICLES_DB_ID.replace("-", "")) != 32:
    raise RuntimeError(f"ARTICLES_DB_ID format looks wrong: {ARTICLES_DB_ID}")

# -----------------------------
# Debug / Export flags
# -----------------------------
# 디버그 덤프 기능
DEBUG_DUMP = os.getenv("DEBUG_DUMP", "0") == "1"
DEBUG_PATH = os.getenv("DEBUG_PATH", "debug_published_at.jsonl")

# Notion query 디버그(요청 시에만 사용)
DEBUG_NOTION_QUERY = os.getenv("DEBUG_NOTION_QUERY", "0") == "1"

# 처리 결과 덤프 (기본 ON)
EXPORT_AFTER_RUN = os.getenv("EXPORT_AFTER_RUN", "1") == "1"
EXPORT_PATH = os.getenv("EXPORT_PATH", "processed_pages.jsonl")
EXPORT_PRINT_LIMIT = int(os.getenv("EXPORT_PRINT_LIMIT", "30"))  # Actions 로그 과다 출력 방지

# 분류 스위치 (기본 OFF: classify_pending.py에서 수행)
ENABLE_AI = os.getenv("ENABLE_AI", "0") == "1"

def dump_debug(obj: dict):
    if not DEBUG_DUMP:
        return
    try:
        with open(DEBUG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass

def dump_notion_query_debug(obj: dict):
    if not DEBUG_NOTION_QUERY:
        return
    try:
        with open("debug_notion_query.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass

# -----------------------------
# Notion headers
# -----------------------------
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
    "User-Agent": "IncidentDashboard/1.0 (+local)",
}

# -----------------------------
# Runtime settings
# -----------------------------
UPDATE_EXISTING = False
DRY_RUN = False
SQLITE_PATH = "state.sqlite"

# -----------------------------
# Sources
# -----------------------------
RSS_FEEDS = [
    ("데일리시큐", "https://www.dailysecu.com/rss/allArticle.xml"),
    ("보안뉴스", "http://www.boannews.com/media/news_rss.xml"),
]

NAVER_KEYWORDS = [
    "침해사고", "개인정보 유출", "해킹", "랜섬웨어", "DDoS", "계정 탈취", "피싱", "취약점 악용",
    "악성코드", "스미싱 피해"
]

# ==========================================
# 2. SQLite 로컬 DB
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
    if not raw:
        return None
    try:
        d = dateparser.parse(raw)
        if not d.tzinfo:
            d = d.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        return d.astimezone(dt.timezone.utc).isoformat()
    except Exception:
        return dt.datetime.now(dt.timezone.utc).isoformat()

# ==========================================
# 4. 데이터 수집
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
            if not url or not title:
                continue

            pub_date = parse_datetime_any(e.get("published") or e.get("updated"))

            dump_debug({
                "source": source_name,
                "title": title,
                "url": url,
                "pub_raw": e.get("published")
            })

            summary = clean_text(e.get("summary") or e.get("description") or "")
            out.append({
                "source": source_name,
                "title": title,
                "url": url,
                "published_iso": pub_date,
                "summary": summary[:500]
            })
    return out

def collect_from_naver():
    if not NAVER_CLIENT_ID:
        return []
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
                        "source": "네이버뉴스",
                        "keyword": kw,
                        "title": title,
                        "url": link
                    })

                    out.append({
                        "source": "네이버뉴스",
                        "title": title,
                        "url": link,
                        "published_iso": pub_date,
                        "summary": summary[:500]
                    })
            time.sleep(0.1)
        except Exception:
            continue
    return out

# ==========================================
# 5. AI 분석 (Gemini)
# ==========================================
def analyze_articles_batch(items, timeout: float = 60.0):
    api_key = GEMINI_API_KEY
    if not api_key:
        return {}

    # 🚨 핵심 수정: 타임아웃을 Config가 아니라 Client 객체 생성 시점에 명시적으로 주입!
    # 이렇게 해야 내부 httpx가 5초 디폴트를 버리고 우리가 준 60초를 온전히 사용합니다.
    client = genai.Client(
        api_key=api_key,
        http_options={'timeout': timeout} 
    )
    
    results = {}
    BATCH_SIZE = 5 # 🚨 워크플로우에 맞춰서 5 (또는 2)로 유지

    print(f"\n🤖 AI 분석 시작 (대상: {len(items)}개)...")

    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i: i + BATCH_SIZE]

        batch_input = []
        for idx, item in enumerate(batch):
            batch_input.append({
                "custom_id": i + idx,
                "title": item['title'],
                "summary": item['summary']
            })

        batch_idx = (i // BATCH_SIZE) + 1
        print(f"   ➤ AI Batch {batch_idx} 전송 중...")

        prompt_text = (
            "You are an elite Cyber Security Analyst. Your goal is to identify ACTUAL technical security breaches, hacks, and vulnerabilities.\n"
            # ... (프롬프트 내용 중략 - 기존과 동일하게 유지해 주세요) ...
            "Input Data:\n" + json.dumps(batch_input, ensure_ascii=False)
        )

        def _invoke():
            return client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',
                    temperature=0.0,
                    top_p=0.1,
                    top_k=1
                    # 🚨 여기서 http_options 제거됨 (위로 올렸으므로)
                )
            )

        try:
            # Wall-Clock Hard Guard 유지
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_invoke)
                try:
                    resp = future.result(timeout=timeout)
                except FutureTimeout:
                    raise TimeoutError("TIME BUDGET EXCEEDED: Gemini SDK blocked and was hard-terminated")

            if resp.text:
                parsed = json.loads(resp.text)
                for res in parsed:
                    c_id = res.get('custom_id')
                    if c_id is not None and 0 <= c_id < len(items):
                        original_url = items[c_id]['url']
                        results[original_url] = res
                print(f"      ✅ Batch {batch_idx} 성공")
            time.sleep(2)

        except TimeoutError:
            raise
        except Exception as e:
            print(f"   ❌ Batch Error: {e}")
            raise

    return results    api_key = GEMINI_API_KEY
        if not api_key:
            return {}

        # 🚨 핵심 수정: 타임아웃을 Config가 아니라 Client 객체 생성 시점에 명시적으로 주입!
        # 이렇게 해야 내부 httpx가 5초 디폴트를 버리고 우리가 준 60초를 온전히 사용합니다.
        client = genai.Client(
            api_key=api_key,
            http_options={'timeout': timeout} 
        )
        
        results = {}
        BATCH_SIZE = 5 # 🚨 워크플로우에 맞춰서 5 (또는 2)로 유지

        print(f"\n🤖 AI 분석 시작 (대상: {len(items)}개)...")

        for i in range(0, len(items), BATCH_SIZE):
            batch = items[i: i + BATCH_SIZE]

            batch_input = []
            for idx, item in enumerate(batch):
                batch_input.append({
                    "custom_id": i + idx,
                    "title": item['title'],
                    "summary": item['summary']
                })

            batch_idx = (i // BATCH_SIZE) + 1
            print(f"   ➤ AI Batch {batch_idx} 전송 중...")

            prompt_text = (
                "You are an elite Cyber Security Analyst. Your goal is to identify ACTUAL technical security breaches, hacks, and vulnerabilities.\n"
                # ... (프롬프트 내용 중략 - 기존과 동일하게 유지해 주세요) ...
                "Input Data:\n" + json.dumps(batch_input, ensure_ascii=False)
            )

            def _invoke():
                return client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt_text,
                    config=types.GenerateContentConfig(
                        response_mime_type='application/json',
                        temperature=0.0,
                        top_p=0.1,
                        top_k=1
                        # 🚨 여기서 http_options 제거됨 (위로 올렸으므로)
                    )
                )

            try:
                # Wall-Clock Hard Guard 유지
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_invoke)
                    try:
                        resp = future.result(timeout=timeout)
                    except FutureTimeout:
                        raise TimeoutError("TIME BUDGET EXCEEDED: Gemini SDK blocked and was hard-terminated")

                if resp.text:
                    parsed = json.loads(resp.text)
                    for res in parsed:
                        c_id = res.get('custom_id')
                        if c_id is not None and 0 <= c_id < len(items):
                            original_url = items[c_id]['url']
                            results[original_url] = res
                    print(f"      ✅ Batch {batch_idx} 성공")
                time.sleep(2)

            except TimeoutError:
                raise
            except Exception as e:
                print(f"   ❌ Batch Error: {e}")
                raise

        return results
# ==========================================
# 6. Notion API 함수
# ==========================================
def query_page_id_by_fingerprint(fp):
    url = f"https://api.notion.com/v1/databases/{ARTICLES_DB_ID}/query"
    payload = {
        "filter": {
            "property": "Fingerprint",
            "rich_text": {"equals": fp}
        },
        "page_size": 1
    }

    try:
        r = requests.post(url, headers=HEADERS, json=payload)

        dump_notion_query_debug({
            "fingerprint": fp,
            "status_code": r.status_code,
            "retry_after": r.headers.get("Retry-After"),
            "response_preview": r.text[:200]
        })

        res = r.json().get("results")
        return res[0]["id"] if res else None

    except Exception as e:
        dump_notion_query_debug({
            "fingerprint": fp,
            "error": str(e)
        })
        return None

def create_notion_page(props: dict) -> dict:
    url = "https://api.notion.com/v1/pages"
    body = {"parent": {"database_id": ARTICLES_DB_ID}, "properties": props}
    r = requests.post(url, headers=HEADERS, json=body, timeout=30)
    r.raise_for_status()
    return r.json()

def update_notion_page(page_id: str, props: dict, timeout: float = 10.0):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    payload = {"properties": props}
    r = requests.patch(url, headers=HEADERS, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ==========================================
# 7. 처리 결과 덤프
# ==========================================
def safe_get_select_name(page_props: dict, key: str) -> Optional[str]:
    try:
        v = page_props.get(key)
        if not v:
            return None
        if v.get("type") == "select" and v.get("select"):
            return v["select"].get("name")
        return None
    except:
        return None

def safe_get_title(page_props: dict, key: str = "Title") -> Optional[str]:
    try:
        v = page_props.get(key)
        if not v:
            return None
        if v.get("type") == "title":
            parts = v.get("title") or []
            return "".join([p.get("plain_text", "") for p in parts])[:2000]
        return None
    except:
        return None

def safe_get_url(page_props: dict, key: str = "URL") -> Optional[str]:
    try:
        v = page_props.get(key)
        if not v:
            return None
        if v.get("type") == "url":
            return v.get("url")
        return None
    except:
        return None

def safe_get_rich_text(page_props: dict, key: str) -> Optional[str]:
    try:
        v = page_props.get(key)
        if not v:
            return None
        if v.get("type") == "rich_text":
            parts = v.get("rich_text") or []
            return "".join([p.get("plain_text", "") for p in parts])[:2000]
        return None
    except:
        return None

def export_processed_pages(processed: List[dict]):
    """
    processed: create/update 응답 기반으로 구성된 dict list
    -> JSONL 파일로 저장 + 일부는 stdout로 요약 출력
    """
    if not EXPORT_AFTER_RUN:
        return

    try:
        with open(EXPORT_PATH, "w", encoding="utf-8") as f:
            for row in processed:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

        print(f"\n📤 Export 완료: {EXPORT_PATH} (rows={len(processed)})")

        # Actions 로그 요약(과다 방지)
        print(f"📌 Export 샘플(상위 {min(EXPORT_PRINT_LIMIT, len(processed))}개):")
        for row in processed[:EXPORT_PRINT_LIMIT]:
            print(f" - {row.get('ai_status')} / {row.get('risk_level')} | {row.get('title')[:60]} | {row.get('page_id')}")
    except Exception as e:
        print(f"⚠️ Export 실패: {e}")

# ==========================================
# 8. 메인 실행
# ==========================================
def main():
    init_sqlite()
    print(f"🚀 Job Start (Update Mode: {UPDATE_EXISTING})")

    # 1) 수집
    raw_items = collect_from_rss() + collect_from_naver()
    dedup = {x['url']: x for x in raw_items}
    unique_items = list(dedup.values())
    print(f"📥 수집 완료: {len(unique_items)}개")

    # 2) 중복 검사 및 처리 대상 선별
    items_to_process = []
    print("🔍 Notion 중복 검사 중...")

    for item in unique_items:
        fp = hashlib.sha256(f"{item['source']}|{item['url']}".encode("utf-8")).hexdigest()
        item['fingerprint'] = fp

        if not UPDATE_EXISTING and seen_fingerprint(fp):
            continue

        page_id = query_page_id_by_fingerprint(fp)

        if page_id:
            if UPDATE_EXISTING:
                item['page_id'] = page_id
                items_to_process.append(item)
            else:
                mark_seen(fp, item['source'], item['url'], page_id)
                continue
        else:
            item['page_id'] = None
            items_to_process.append(item)

        time.sleep(0.2)

    print(f"✨ 최종 처리 대상: {len(items_to_process)}개")

    if not items_to_process:
        print("✅ 처리할 기사가 없습니다.")
        return

    # 3) AI 분석 (기본 OFF)
    ai_results = {}
    if ENABLE_AI:
        ai_results = analyze_articles_batch(items_to_process)

    # 4) Notion 반영 + 처리 결과 수집(덤프용)
    print("💾 Notion 반영 시작...")
    success = 0
    errors = 0
    processed_dump: List[dict] = []

    for item in items_to_process:
        ai = ai_results.get(item['url'], {})

        props = {
            "Title": {"title": [{"text": {"content": item['title'][:2000]}}]},
            "Source": {"select": {"name": item['source']}},
            "URL": {"url": item['url']},
            "Published At": {"date": {"start": item['published_iso']}} if item['published_iso'] else None,
            "Fingerprint": {"rich_text": [{"text": {"content": item['fingerprint']}}]},
            "Summary": {"rich_text": [{"text": {"content": item['summary'][:2000]}}]},
        }

        # ✅ 새 페이지는 무조건 Pending으로 적재 (분류는 별도 워커가 수행)
        if not item.get("page_id"):
            props.update({
                "AI_State": {"select": {"name": "Pending"}},
                "AI_Error": {"rich_text": [{"text": {"content": ""}}]},
            })

        # ✅ ENABLE_AI=1일 때만 이 런에서 분류까지 수행
        if ENABLE_AI:
            props.update({
                "AI_Status": {"select": {"name": ai.get("ai_status", "Info")}},
                "Risk_Level": {"select": {"name": ai.get("ai_risk", "Low")}},
                "AI_Reason": {"rich_text": [{"text": {"content": ai.get("ai_reason", "-")[:2000]}}]},
                "AI_State": {"select": {"name": "Done"}},
                "AI_Error": {"rich_text": [{"text": {"content": ""}}]},
            })

        props = {k: v for k, v in props.items() if v is not None}
        props = {k: v for k, v in props.items() if v is not None}

        if DRY_RUN:
            continue

        try:
            if item.get('page_id'):
                page_obj = update_notion_page(item['page_id'], props)
                print(f"   🔄 [Updated] {item['title'][:30]}...")
            else:
                page_obj = create_notion_page(props)
                status_icon = ("🟢" if (ENABLE_AI and ai.get("ai_status") == "Critical") else ("🟡" if not ENABLE_AI else "⚪"))
                print(f"   {status_icon} [Created] {item['title'][:30]}...")

            page_id = page_obj.get("id")
            page_props = page_obj.get("properties") or {}

            # 로컬 마킹
            if page_id:
                mark_seen(item['fingerprint'], item['source'], item['url'], page_id)
            else:
                mark_seen(item['fingerprint'], item['source'], item['url'], 'unknown_page_id')

            # 덤프용 데이터 구성 (Notion이 실제 저장한 값 기준)
            processed_dump.append({
                "page_id": page_id,
                "title": safe_get_title(page_props, "Title") or item['title'],
                "url": safe_get_url(page_props, "URL") or item['url'],
                "fingerprint": item['fingerprint'],
                "source": item['source'],
                "ai_status": safe_get_select_name(page_props, "AI_Status") or (ai.get("ai_status") if ENABLE_AI else None) or "Unclassified",
                "risk_level": safe_get_select_name(page_props, "Risk_Level") or (ai.get("ai_risk") if ENABLE_AI else None) or "Unknown",
                "ai_state": safe_get_select_name(page_props, "AI_State") or ("Done" if ENABLE_AI else "Pending"),
                "ai_error": safe_get_rich_text(page_props, "AI_Error") or "",
                "ai_reason": safe_get_rich_text(page_props, "AI_Reason") or ((ai.get("ai_reason") if ENABLE_AI else "") or ""),
                "created_time": page_obj.get("created_time"),
                "last_edited_time": page_obj.get("last_edited_time"),
                "created_by": (page_obj.get("created_by") or {}).get("id"),
                "last_edited_by": (page_obj.get("last_edited_by") or {}).get("id"),
                "op": "updated" if item.get("page_id") else "created",
                "run_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            })

            success += 1
            time.sleep(0.6)

        except Exception as e:
            print(f"   ⚠️ [Fail] {e}")
            errors += 1

    msg = f"Created/Updated: {success}, Errors: {errors}"
    print(f"🎉 작업 완료! ({msg})")
    log_run("ok" if errors == 0 else "partial", msg)

    # 5) 최종 덤프
    export_processed_pages(processed_dump)

if __name__ == "__main__":
    main()
