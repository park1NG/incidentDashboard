import os
import time
import random
import requests

# ✅ ingest 모듈(공통 Notion/Gemini 설정 + 함수 재사용)
import ingest_news_to_notion as core


# =============================
# Runtime knobs (env)
# =============================
MAX_PENDING = int(os.getenv("MAX_PENDING", "20"))          # 한 번에 가져올 Pending 수
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "10"))           # Gemini 배치 크기
SLEEP_BETWEEN_BATCHES = float(os.getenv("SLEEP_BETWEEN_BATCHES", "3"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))          # 429 재시도 횟수
BASE_BACKOFF = float(os.getenv("BASE_BACKOFF", "2"))      # backoff base seconds
MAX_BACKOFF = float(os.getenv("MAX_BACKOFF", "60"))       # backoff cap seconds

# =============================
# Notion property names (고정)
# =============================
PROP_AI_STATE = "AI_State"
PROP_AI_ERROR = "AI_Error"
PROP_AI_STATUS = "AI_Status"
PROP_RISK_LEVEL = "Risk_Level"
PROP_AI_REASON = "AI_Reason"
PROP_URL = "URL"
PROP_TITLE = "Title"
PROP_SUMMARY = "Summary"
PROP_SOURCE = "Source"
PROP_PUBLISHED = "Published At"
PROP_FINGERPRINT = "Fingerprint"


def is_429_resource_exhausted(exc: Exception) -> bool:
    s = str(exc)
    return ("429" in s) or ("RESOURCE_EXHAUSTED" in s) or ("Too many requests" in s)


def backoff_sleep(attempt: int) -> None:
    # 지수 백오프 + 지터
    sleep_s = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** attempt))
    sleep_s = sleep_s * (0.7 + random.random() * 0.6)  # 0.7~1.3 jitter
    time.sleep(sleep_s)


def query_pending_pages(page_size: int):
    # 🚨 수정된 부분: 최신 Notion API 버전에 맞춰 data_sources 엔드포인트로 마이그레이션
    ds_id = core.get_active_data_source_id()
    url = f"https://api.notion.com/v1/data_sources/{ds_id}/query"
    
    payload = {
        "filter": {"property": PROP_AI_STATE, "select": {"equals": "Pending"}},
        "page_size": page_size
    }
    r = requests.post(url, headers=core.HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return (r.json() or {}).get("results", [])


def _plain_text_from_rich(parts):
    return "".join([p.get("plain_text", "") for p in (parts or [])])


def get_prop_url(props: dict, key: str) -> str:
    v = props.get(key) or {}
    if v.get("type") == "url":
        return v.get("url") or ""
    return ""


def get_prop_title(props: dict, key: str) -> str:
    v = props.get(key) or {}
    if v.get("type") == "title":
        return _plain_text_from_rich(v.get("title"))[:2000]
    return ""


def get_prop_rich_text(props: dict, key: str) -> str:
    v = props.get(key) or {}
    if v.get("type") == "rich_text":
        return _plain_text_from_rich(v.get("rich_text"))[:2000]
    return ""


def mark_pending_with_error(page_id: str, err_code: str, reason: str):
    props = {
        PROP_AI_STATE: {"select": {"name": "Pending"}},
        PROP_AI_ERROR: {"rich_text": [{"text": {"content": err_code[:2000]}}]},
        PROP_AI_REASON: {"rich_text": [{"text": {"content": reason[:2000]}}]},
    }
    core.update_notion_page(page_id, props)


def mark_failed(page_id: str, err_code: str, reason: str):
    props = {
        PROP_AI_STATE: {"select": {"name": "Failed"}},
        PROP_AI_ERROR: {"rich_text": [{"text": {"content": err_code[:2000]}}]},
        PROP_AI_REASON: {"rich_text": [{"text": {"content": reason[:2000]}}]},
    }
    core.update_notion_page(page_id, props)


def apply_classification(page_id: str, ai: dict):
    props = {
        PROP_AI_STATUS: {"select": {"name": ai.get("ai_status", "Info")}},
        PROP_RISK_LEVEL: {"select": {"name": ai.get("ai_risk", "Low")}},
        PROP_AI_REASON: {"rich_text": [{"text": {"content": (ai.get("ai_reason") or "-")[:2000]}}]},
        PROP_AI_ERROR: {"rich_text": [{"text": {"content": ""}}]},
        PROP_AI_STATE: {"select": {"name": "Done"}},
    }
    core.update_notion_page(page_id, props)


def main():
    if not core.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing. (GitHub Secrets/ENV에 설정 필요)")

    pages = query_pending_pages(MAX_PENDING)
    print(f"🧭 Pending 조회: {len(pages)}개 (max={MAX_PENDING})")
    if not pages:
        print("✅ Pending 없음")
        return

    # core.analyze_articles_batch 입력 형태로 구성
    items = []
    url_to_page_id = {}

    for p in pages:
        page_id = p.get("id")
        props = p.get("properties") or {}

        url = get_prop_url(props, PROP_URL)
        title = get_prop_title(props, PROP_TITLE)
        summary = get_prop_rich_text(props, PROP_SUMMARY)

        if not page_id or not url:
            if page_id:
                mark_failed(page_id, "MISSING_URL", "URL property missing; cannot classify.")
            continue

        items.append({
            "url": url,
            "title": title or "(no title)",
            "summary": summary or "",
            "source": "Notion",
            "published_iso": None,
            "fingerprint": "pending",
        })
        url_to_page_id[url] = page_id

    total = len(items)
    print(f"🤖 분류 대상: {total}개 (batch={BATCH_SIZE})")
    if total == 0:
        print("✅ 분류 대상 없음")
        return

    for i in range(0, total, BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        print(f"➤ Batch {i // BATCH_SIZE + 1} ({len(batch)} items)")

        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                ai_results = core.analyze_articles_batch(batch)  # url -> {ai_status, ai_risk, ai_reason}

                # 결과 반영
                for url, ai in (ai_results or {}).items():
                    page_id = url_to_page_id.get(url)
                    if page_id:
                        apply_classification(page_id, ai)

                # 누락 결과는 Pending 유지 + 표시
                missing = [b["url"] for b in batch if b["url"] not in (ai_results or {})]
                for url in missing:
                    page_id = url_to_page_id.get(url)
                    if page_id:
                        mark_pending_with_error(page_id, "AI_NO_RESULT", "AI returned no result; queued for retry.")

                last_exc = None
                break

            except Exception as e:
                last_exc = e
                if is_429_resource_exhausted(e):
                    if attempt == MAX_RETRIES:
                        for b in batch:
                            page_id = url_to_page_id.get(b["url"])
                            if page_id:
                                mark_pending_with_error(page_id, "429_RESOURCE_EXHAUSTED", "AI quota exhausted; queued for retry.")
                        break
                    backoff_sleep(attempt)
                else:
                    for b in batch:
                        page_id = url_to_page_id.get(b["url"])
                        if page_id:
                            mark_failed(page_id, "AI_ERROR", str(e))
                    break

        time.sleep(SLEEP_BETWEEN_BATCHES)

    print("🎉 classify_pending 완료")


if __name__ == "__main__":
    main()