import os
import sys
import time
import random
import datetime as dt
import re
import json
import requests

# ✅ ingest 모듈(공통 Notion/Gemini 설정 + 함수 재사용)
import ingest_news_to_notion as core

# =============================
# Runtime & Budget knobs (env)
# =============================
MAX_PENDING = int(os.getenv("MAX_PENDING", "50"))         
MAX_RUNTIME_SEC = int(os.getenv("MAX_RUNTIME_SEC", "600"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))            
SLEEP_BETWEEN_BATCHES = float(os.getenv("SLEEP_BETWEEN_BATCHES", "2.0")) 
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))          
BASE_BACKOFF = float(os.getenv("BASE_BACKOFF", "2"))      
MAX_BACKOFF = float(os.getenv("MAX_BACKOFF", "60"))       

SCRIPT_START_TIME = time.time()

# =============================
# Notion property names
# =============================
PROP_AI_STATE = "AI_State"
PROP_AI_ERROR = "AI_Error"
PROP_AI_STATUS = "AI_Status"
PROP_RISK_LEVEL = "Risk_Level"
PROP_AI_REASON = "AI_Reason"
PROP_URL = "URL"
PROP_TITLE = "Title"
PROP_SUMMARY = "Summary"

def remaining_budget_sec() -> float:
    return max(0.0, MAX_RUNTIME_SEC - (time.time() - SCRIPT_START_TIME))

def is_time_budget_exceeded() -> bool:
    return remaining_budget_sec() <= 0.0

def is_time_budget_exc(exc: Exception) -> bool:
    return "TIME BUDGET EXCEEDED" in str(exc).upper()

def sleep_with_budget(seconds: float) -> bool:
    if seconds <= 0: return True
    remain = remaining_budget_sec()
    if remain <= 0: return False
    time.sleep(min(seconds, remain))
    return not is_time_budget_exceeded()

def budgeted_timeout(default: float, min_required: float = 10.0) -> float:
    remain = remaining_budget_sec()
    if remain < min_required:
        raise TimeoutError("Time Budget Exceeded before executing network I/O")
    return min(default, remain)

def write_github_summary(msg: str):
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass
    print(msg)

# 🚨 예외 원인 정밀 분류 로직 (Telemetry 정상화)
def is_gemini_429(exc: Exception) -> bool:
    if hasattr(exc, 'code') and str(exc.code) == '429': return True
    if hasattr(exc, 'status') and 'RESOURCE_EXHAUSTED' in str(exc.status): return True
    s = str(exc).upper()
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "QUOTA EXCEEDED" in s

def is_timeout_exc(exc: Exception) -> bool:
    s = str(exc).lower()
    return (
        isinstance(exc, requests.exceptions.Timeout) or
        "timed out" in s or
        "timeout" in s or
        "read operation timed out" in s
    )

def is_dns_exc(exc: Exception) -> bool:
    s = str(exc).lower()
    return "name or service not known" in s or "temporary failure in name resolution" in s

def gemini_failure_reason(exc: Exception) -> str:
    if exc is None: return "Gemini Unknown Error (no exception)"
    if is_time_budget_exc(exc): return f"Time Budget Exceeded: {exc}"
    if is_gemini_429(exc): return f"Gemini Quota/429: {exc}"
    if is_timeout_exc(exc): return f"Gemini Timeout: {exc}"
    if is_dns_exc(exc): return f"Gemini DNS Failure: {exc}"
    return f"Gemini Unexpected Error: {exc}"

def is_notion_429(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code == 429: return True
    s = str(exc).upper()
    if re.search(r'\b429\b', s): return True
    return "RATE LIMITED" in s or "TOO MANY REQUESTS" in s

_NOTION_QUERY_URL = None
def get_notion_query_url() -> str:
    global _NOTION_QUERY_URL
    if _NOTION_QUERY_URL: return _NOTION_QUERY_URL
    target_id = core.ARTICLES_DB_ID
    eval_errors = []
    
    ds_test_url = f"https://api.notion.com/v1/data_sources/{target_id}"
    try:
        r = requests.get(ds_test_url, headers=core.HEADERS, timeout=budgeted_timeout(10.0))
        r.raise_for_status() 
        _NOTION_QUERY_URL = f"https://api.notion.com/v1/data_sources/{target_id}/query"
        return _NOTION_QUERY_URL
    except TimeoutError:
        raise
    except Exception as e: 
        eval_errors.append(f"DataSource 확인 실패: {e}")

    db_url = f"https://api.notion.com/v1/databases/{target_id}"
    try:
        r = requests.get(db_url, headers=core.HEADERS, timeout=budgeted_timeout(15.0))
        r.raise_for_status() 
        data_sources = r.json().get("data_sources", [])
        if data_sources:
            _NOTION_QUERY_URL = f"https://api.notion.com/v1/data_sources/{data_sources[0].get('id')}/query"
            return _NOTION_QUERY_URL
        else:
            eval_errors.append("Database에 연결된 Data Source가 없음")
    except TimeoutError:
        raise
    except Exception as e: 
        eval_errors.append(f"Database 확인 실패: {e}")

    raise RuntimeError(f"🚨 유효한 Data Source ID를 찾을 수 없습니다. (입력 ID: {target_id}) | 상세: {' / '.join(eval_errors)}")

def query_pages_by_state(state: str, page_size: int = 100):
    query_url = get_notion_query_url()
    safe_page_size = min(100, max(1, page_size))
    
    payload = {
        "filter": {"property": PROP_AI_STATE, "select": {"equals": state}},
        "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
        "page_size": safe_page_size
    }
    last_exc = None
    for attempt in range(3):
        try:
            r = requests.post(query_url, headers=core.HEADERS, json=payload, timeout=budgeted_timeout(60.0))
            r.raise_for_status()
            res_data = (r.json() or {}).get("results", [])
            
            # 🚨 디버그용 아티팩트 파일 생성 
            if os.getenv("DEBUG_NOTION_QUERY") == "1":
                try:
                    with open("debug_notion_query.jsonl", "w", encoding="utf-8") as f:
                        for item in res_data:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                except Exception as debug_e:
                    print(f"⚠️ Debug file write failed: {debug_e}")
                    
            return res_data
        except requests.exceptions.ReadTimeout as e:
            last_exc = e
            print(f"⚠️ Notion API 응답 지연. 5초 후 재시도... ({attempt + 1}/3)")
            if not sleep_with_budget(5.0):
                raise TimeoutError("Time Budget Exceeded during query backoff")
        except Exception as e:
            last_exc = e
            if is_time_budget_exc(e): raise  
            print(f"⚠️ Notion API 조회 실패: {e}")
            if not sleep_with_budget(2.0):
                raise TimeoutError("Time Budget Exceeded during query backoff")
                
    raise RuntimeError(f"Notion API 조회 최종 실패 (3회 시도): {last_exc}")

def _plain_text_from_rich(parts):
    return "".join([p.get("plain_text", "") for p in (parts or [])])

def safe_update_notion(page_id: str, props: dict, max_retries: int = 2):
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            timeout_s = budgeted_timeout(10.0)
            core.update_notion_page(page_id, props, timeout=timeout_s)
            
            if not sleep_with_budget(0.3): 
                return False, Exception("Time Budget Exceeded after update success")
            return True, None
        except Exception as e:
            last_exc = e
            if is_time_budget_exc(e): 
                return False, e
                
            if is_notion_429(e):
                sleep_s = (1.5 ** attempt) * (0.8 + random.random() * 0.4)
            else:
                sleep_s = 0.5 
            
            if not sleep_with_budget(sleep_s):
                return False, Exception("Time Budget Exceeded during backoff sleep")
                
    return False, last_exc

def mark_failed(page_id: str, err_code: str, reason: str):
    props = {
        PROP_AI_STATE: {"select": {"name": "Failed"}},
        PROP_AI_ERROR: {"rich_text": [{"text": {"content": err_code[:2000]}}]},
        PROP_AI_REASON: {"rich_text": [{"text": {"content": reason[:2000]}}]},
    }
    return safe_update_notion(page_id, props)

def apply_classification(page_id: str, ai: dict):
    props = {
        PROP_AI_STATUS: {"select": {"name": ai.get("ai_status", "Info")}},
        PROP_RISK_LEVEL: {"select": {"name": ai.get("ai_risk", "Low")}},
        PROP_AI_REASON: {"rich_text": [{"text": {"content": (ai.get("ai_reason") or "-")[:2000]}}]},
        PROP_AI_ERROR: {"rich_text": [{"text": {"content": ""}}]},
        PROP_AI_STATE: {"select": {"name": "Done"}},
    }
    return safe_update_notion(page_id, props)

def main():
    if not core.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    safe_pending_limit = min(100, MAX_PENDING)
    if MAX_PENDING > 100:
        print(f"⚠️ 설정된 MAX_PENDING({MAX_PENDING})이 API 한도(100)를 초과하여 100개로 클램핑합니다.")
    print(f"🔍 최대 {safe_pending_limit}개의 Pending 기사를 가져옵니다. (시간 제한: {MAX_RUNTIME_SEC}초)")
    
    try:
        pages = query_pages_by_state("Pending", safe_pending_limit)
    except Exception as e:
        msg = f"🛑 **[초기 조회 실패]** 데이터를 가져오지 못했습니다: {e}"
        write_github_summary(f"### 🔴 Circuit Breaker Tripped (Initialization)\n{msg}")
        sys.exit(1)
    
    if not pages:
        write_github_summary("### 🟢 Incident Dashboard\n✅ 처리할 Pending 항목이 없습니다. (수렴 완료)")
        sys.exit(0)

    stats = {
        "target_urls": 0,
        "target_pages": len(pages),
        "attempted_urls": 0,
        "success_pages": 0,
        "ai_missing_pages": 0,
        "missing_url_pages": 0,
        "update_error_pages": 0,
        "circuit_breaker": False,
        "cb_reason": ""
    }

    counted_error_pages = set()
    def bump_update_error(pid: str):
        if pid not in counted_error_pages:
            counted_error_pages.add(pid)
            stats["update_error_pages"] += 1

    notion_unhealthy_streak = 0
    
    def mark_notion_failure(is_429: bool) -> int:
        nonlocal notion_unhealthy_streak
        delta = 3 if is_429 else 1
        notion_unhealthy_streak += delta
        return delta

    def mark_notion_success(delta: int = 1):
        nonlocal notion_unhealthy_streak
        notion_unhealthy_streak = max(0, notion_unhealthy_streak - delta)

    stop_all = False 
    items = []
    url_to_page_ids = {}

    for p in pages:
        if stop_all: break
        if is_time_budget_exceeded():
            stop_all = True
            stats["circuit_breaker"] = True
            stats["cb_reason"] = "Time Budget Exceeded (Parsing)"
            break

        page_id = p.get("id")
        props = p.get("properties") or {}

        url = (props.get(PROP_URL) or {}).get("url") or ""
        title = _plain_text_from_rich((props.get(PROP_TITLE) or {}).get("title"))[:2000]
        summary_prop = props.get(PROP_SUMMARY) or {}
        summary = _plain_text_from_rich(summary_prop.get("rich_text") or [])[:2000]

        if not page_id or not url:
            if page_id: 
                fb_success, fb_err = mark_failed(page_id, "MISSING_URL", "URL missing")
                if fb_success:
                    stats["missing_url_pages"] += 1
                    mark_notion_success(1) 
                else:
                    if fb_err and is_time_budget_exc(fb_err):
                        stop_all = True
                        stats["circuit_breaker"] = True
                        stats["cb_reason"] = "Time Budget Exceeded (MISSING_URL 처리 중)"
                        break

                    bump_update_error(page_id)
                    mark_notion_failure(is_notion_429(fb_err))
                    
                    if notion_unhealthy_streak >= 5:
                        stop_all = True
                        stats["circuit_breaker"] = True
                        stats["cb_reason"] = "Notion API 연속 에러 (Parsing 중)"
                        break 
            continue

        if url not in url_to_page_ids:
            items.append({
                "url": url,
                "title": title or "(no title)",
                "summary": summary or "",
                "source": "Notion",
                "published_iso": None,
                "fingerprint": "pending",
            })
            url_to_page_ids[url] = []
        
        url_to_page_ids[url].append(page_id)

    stats["target_urls"] = len(items)

    print(f"🤖 분류 시작 (URL 대상: {stats['target_urls']}개, Page 대상: {stats['target_pages']}개)")

    for i in range(0, stats["target_urls"], BATCH_SIZE):
        if stop_all: break

        if is_time_budget_exceeded():
            stop_all = True
            stats["circuit_breaker"] = True
            stats["cb_reason"] = "Time Budget Exceeded (배치 진입 전)"
            break

        batch = items[i:i + BATCH_SIZE]
        stats["attempted_urls"] += len(batch)
        print(f"\n➤ Batch {i // BATCH_SIZE + 1} ({len(batch)} URLs) 처리 중...")

        last_exc = None
        ai_results = {}
        
        for attempt in range(MAX_RETRIES + 1):
            try:
                timeout_s = budgeted_timeout(60.0)
                ai_results = core.analyze_articles_batch(batch, timeout=timeout_s)
                
                if not ai_results: raise Exception("AI 반환 결과가 비어있습니다.")
                break 
            except Exception as e:
                last_exc = e
                if is_time_budget_exc(e): break 
                
                # 🚨 수정: 무조건 재시도하는 것이 아니라, Timeout 등 원인에 맞게 로깅 및 백오프
                if attempt < MAX_RETRIES:
                    fail_reason = gemini_failure_reason(e)
                    print(f"   ⚠️ Gemini 호출 실패: {fail_reason}. 대기 후 재시도... ({attempt+1}/{MAX_RETRIES})")
                    sleep_sec = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** attempt)) * (0.7 + random.random() * 0.6)
                    if not sleep_with_budget(sleep_sec):
                        stop_all = True
                        stats["circuit_breaker"] = True
                        stats["cb_reason"] = "Time Budget Exceeded (Gemini backoff 중)"
                        break
                else:
                    print(f"   ❌ 예기치 않은 에러 최종 발생: {e}")
                    break 

        if stop_all: break

        if not ai_results:
            stop_all = True
            stats["circuit_breaker"] = True
            # 🚨 수정: 무조건 Quota로 찍지 않고 정밀 분석된 사유 기록
            stats["cb_reason"] = gemini_failure_reason(last_exc)
            break

        for b in batch:
            url = b["url"]
            if url not in ai_results:
                for page_id in url_to_page_ids.get(url, []):
                    if is_time_budget_exceeded():
                        stop_all = True; stats["circuit_breaker"] = True; stats["cb_reason"] = "Time Budget Exceeded (Failed 기록 중)"
                        break

                    print(f"   ⚠️ AI 응답 누락 격리: {url[:30]}...")
                    success, err = mark_failed(page_id, "AI_MISSING", "Gemini partial response missing (1st time). Isolated.")
                    
                    if success:
                        stats["ai_missing_pages"] += 1
                        mark_notion_success(1)
                    else:
                        if err and is_time_budget_exc(err):
                            stop_all = True; stats["circuit_breaker"] = True; stats["cb_reason"] = "Time Budget Exceeded (AI_MISSING 격리 중)"
                            break

                        bump_update_error(page_id) 
                        mark_notion_failure(is_notion_429(err))
                        
                        if notion_unhealthy_streak >= 5:
                            stop_all = True; stats["circuit_breaker"] = True; stats["cb_reason"] = "Notion API 연속 에러"
                            break
                if stop_all: break
        if stop_all: break

        for url, ai in ai_results.items():
            for page_id in url_to_page_ids.get(url, []):
                if is_time_budget_exceeded():
                    stop_all = True; stats["circuit_breaker"] = True; stats["cb_reason"] = "Time Budget Exceeded (Done 기록 진입 전)"
                    break
                
                success, err = apply_classification(page_id, ai)
                
                if success:
                    stats["success_pages"] += 1
                    mark_notion_success(1)
                    print(f"   ✅ 반영 완료: {ai.get('ai_status')} / {ai.get('ai_risk')}")
                else:
                    if err and is_time_budget_exc(err):
                        stop_all = True; stats["circuit_breaker"] = True; stats["cb_reason"] = "Time Budget Exceeded (Done 기록 중)"
                        break

                    bump_update_error(page_id) 
                    print(f"   ⚠️ Notion 업데이트 실패. Failed 격리 시도: {url[:30]}...")

                    applied_penalty = mark_notion_failure(is_notion_429(err))
                    
                    fb_success, fb_err = mark_failed(page_id, "NOTION_UPDATE_FAILED", f"Gemini success, but Notion update failed: {err}")
                    
                    if fb_success:
                        mark_notion_success(applied_penalty)
                    else:
                        if fb_err and is_time_budget_exc(fb_err):
                            stop_all = True; stats["circuit_breaker"] = True; stats["cb_reason"] = "Time Budget Exceeded (NOTION_UPDATE_FAILED 롤백 중)"
                            break

                        print(f"   ❌ Failed 격리조차 실패 (Notion 다운 의심): {fb_err}")
                        bump_update_error(page_id) 
                        mark_notion_failure(is_notion_429(fb_err))

                    if notion_unhealthy_streak >= 5:
                        stop_all = True
                        stats["circuit_breaker"] = True
                        stats["cb_reason"] = "Notion API 연속 업데이트 실패 (Rate Limit 등)"
                        break
            if stop_all: break

        if stop_all: break

        if not sleep_with_budget(SLEEP_BETWEEN_BATCHES):
            stop_all = True
            stats["circuit_breaker"] = True
            stats["cb_reason"] = "Time Budget Exceeded (배치 간격 대기 중)"
            break

    skipped_pages = max(0, stats["target_pages"] - (stats["success_pages"] + stats["ai_missing_pages"] + stats["missing_url_pages"] + stats["update_error_pages"]))

    summary_msg = (
        f"### 📊 Incident Dashboard: Classification Report\n"
        f"- **실행 시간**: {int(time.time() - SCRIPT_START_TIME)}초\n"
        f"--- \n"
        f"- 🎯 **전체 타겟 (Page)**: {stats['target_pages']} 개 (입력 URL: {stats['target_urls']} 개)\n"
        f"- 🚀 **AI 분석 시도 (URL)**: {stats['attempted_urls']} 개\n"
        f"- ⏭️ **미시도 보류 (Page)**: {skipped_pages} 개 (다음 런 처리)\n"
        f"--- \n"
        f"- ✅ **분류 완료 (Done)**: {stats['success_pages']} 개\n"
        f"- ❌ **AI 누락 격리 (Failed)**: {stats['ai_missing_pages']} 개\n"
        f"- 🚫 **URL 누락 격리 (Failed)**: {stats['missing_url_pages']} 개\n"
        f"- ⚠️ **Notion 통신 에러**: {stats['update_error_pages']} 개\n"
    )

    if stats["circuit_breaker"]:
        cb_msg = f"### 🟡 Circuit Breaker Tripped\n- **사유**: {stats['cb_reason']}\n\n"
        summary_msg = cb_msg + summary_msg

    write_github_summary(summary_msg)

if __name__ == "__main__":
    main()