"""
mikai Jobcan Bridge — v6.0.0
Phase B Railway Backend

v6 重點: 
- /api/recon-js: 抓 Jobcan 的 AngularJS 應用 JS，反推 submission body 格式
- /api/fill: 系統性嘗試多種 body 格式（一次跑完全部 pattern）
"""

import json
import re
import asyncio
import time
import os
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from playwright.async_api import async_playwright, Browser, BrowserContext

# ── Config ──────────────────────────────────────────────
API_KEY = os.getenv("API_KEY", "mikai-prod-2026")
PORT = int(os.getenv("PORT", "8080"))
VERSION = "6.0.0"

JOBCAN_LOGIN_URL = "https://id.jobcan.jp/users/sign_in"
JOBCAN_WF_BASE = "https://ssl.wf.jobcan.jp"

# 發注稟議 form definition (from recon 2026-04-04)
FORM_666628 = {
    "form_id": 666628,
    "flow_id": 401080,
    "flow_id_high": 401084,  # >=500万
    "form_type": 1,
    "client": 53786,
    "group_id": 560177,
    "group_name": "Board",
}

# ── Playwright singleton ────────────────────────────────
_browser: Optional[Browser] = None

async def get_browser() -> Browser:
    global _browser
    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-gpu',
                '--disable-dev-shm-usage',
            ]
        )
    return _browser


# ── Login helper ────────────────────────────────────────
async def login_jobcan(email: str, password: str) -> tuple[BrowserContext, dict]:
    """Login, return (context, token_info)"""
    browser = await get_browser()
    context = await browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
    )
    page = await context.new_page()
    
    # Stealth
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    """)
    
    await page.goto(JOBCAN_LOGIN_URL)
    await page.wait_for_load_state('networkidle')
    await page.fill('#user_email', email)
    await page.fill('#user_password', password)
    await page.click('[name="commit"]')
    
    # Wait for login (poll for sign_in to disappear)
    for _ in range(20):
        await asyncio.sleep(1)
        if 'sign_in' not in page.url:
            break
    
    if 'sign_in' in page.url:
        await context.close()
        raise HTTPException(status_code=401, detail="Jobcan login failed")
    
    # Navigate to WF
    await page.goto(JOBCAN_WF_BASE + '/', wait_until='domcontentloaded')
    await asyncio.sleep(2)
    
    # Get tokens
    cookies = await context.cookies()
    csrf = next((c['value'] for c in cookies if c['name'] == 'csrftoken'), None)
    sessionid = next((c['value'] for c in cookies if c['name'] == 'sessionid'), None)
    
    return context, {
        "csrf": csrf,
        "sessionid": sessionid,
        "page": page,
    }


# ── Models ──────────────────────────────────────────────
class FillRequest(BaseModel):
    email: str
    password: str
    items: list  # [{"payload": {...}, "flow_type": "発注稟議"}]
    action: str = "draft"  # draft | submit

class ReconJSRequest(BaseModel):
    email: str
    password: str


# ── App ─────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    print(f"[BOOT] mikai Jobcan Bridge v{VERSION} on port {PORT}")
    yield
    if _browser:
        await _browser.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def check_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}


# ══════════════════════════════════════════════════════════
# /api/recon-js — 抓 AngularJS 源碼，找 submission body 格式
# ══════════════════════════════════════════════════════════

@app.post("/api/recon-js")
async def recon_js(req: ReconJSRequest, x_api_key: str = Header(None)):
    check_api_key(x_api_key)
    
    context, tokens = await login_jobcan(req.email, req.password)
    page = tokens["page"]
    csrf = tokens["csrf"]
    
    results = {
        "login": "ok",
        "csrf": csrf,
        "js_files": [],
        "submission_snippets": [],
        "form_json_snippets": [],
        "angular_info": None,
    }
    
    try:
        # Collect all JS file URLs
        js_urls = await page.evaluate('''() => {
            return Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
        }''')
        results["js_files"] = js_urls
        
        # Search patterns for submission logic
        search_terms = [
            'requests/new', 'requests/create', 'request/new',
            'form_json', 'formJson', 'form_id', 'formId',
            'is_draft', 'isDraft', 'is_save',
            'flow_id', 'flowId',
            'group_id', 'groupId', 
            'submitRequest', 'createRequest', 'saveRequest', 'saveDraft',
            'newRequest', 'postRequest',
            '$http.post',
        ]
        
        # Download and search each non-library JS file
        for js_url in js_urls:
            fname = js_url.split('/')[-1].split('?')[0]
            # Skip known libraries
            if any(lib in fname.lower() for lib in ['angular.min', 'angular-', 'jquery', 'bootstrap', 'moment', 'lodash', 'underscore', 'ui-']):
                continue
            
            try:
                js_content = await page.evaluate('''async (url) => {
                    try {
                        const r = await fetch(url);
                        return await r.text();
                    } catch(e) { return "ERR:" + e.message; }
                }''', js_url)
                
                if js_content.startswith("ERR:") or len(js_content) < 100:
                    continue
                
                file_size = len(js_content)
                
                for term in search_terms:
                    # Case-insensitive search
                    for m in re.finditer(re.escape(term), js_content, re.IGNORECASE):
                        start = max(0, m.start() - 300)
                        end = min(len(js_content), m.end() + 300)
                        snippet = js_content[start:end]
                        
                        cat = "submission" if any(t in term.lower() for t in ['request', 'submit', 'save', 'draft', 'post', 'create']) else "form_json"
                        
                        results[f"{cat}_snippets"].append({
                            "file": fname[:60],
                            "size": file_size,
                            "term": term,
                            "snippet": snippet,
                        })
            except Exception as e:
                pass
        
        # Try to get AngularJS route info
        angular_info = await page.evaluate('''() => {
            try {
                if (!window.angular) return {error: "no angular"};
                const inj = angular.element(document.body).injector();
                if (!inj) return {error: "no injector"};
                const $route = inj.get("$route");
                const routes = {};
                if ($route && $route.routes) {
                    for (const [p, c] of Object.entries($route.routes)) {
                        routes[p] = {controller: c.controller, template: c.templateUrl};
                    }
                }
                return {version: angular.version?.full, routes};
            } catch(e) { return {error: e.message}; }
        }''')
        results["angular_info"] = angular_info
        
        # Deduplicate
        seen = set()
        for key in ["submission_snippets", "form_json_snippets"]:
            unique = []
            for item in results[key]:
                sig = item.get("snippet", "")[:100]
                if sig not in seen:
                    seen.add(sig)
                    unique.append(item)
            results[key] = unique[:30]
        
    finally:
        await context.close()
    
    return results


# ══════════════════════════════════════════════════════════
# /api/fill — 系統性嘗試多種 body 格式
# ══════════════════════════════════════════════════════════

def build_form_json_666628(payload: dict) -> list:
    """Build complete form_json for 発注稟議 with ALL required fields."""
    
    # Alias key → form field mapping (from JOBCAN_API_REFERENCE.md recon)
    ALIAS_MAP = {
        "ringi_type":       (3831493, "稟議の種類", 7),
        "contract_date":    (3831494, "契約締結日", 4),
        "content_type":     (3818321, "内容", 7),
        "application_type": (3818329, "申請内容", 7),
        "vendor_type":      (3818323, "取引先種別", 6),
        "vendor_name":      (3822625, "取引先名", 1),
        "vendor_website":   (3818337, "取引先ウェブサイト", 1),
        "bank_info":        (3841064, "銀行情報", 2),
        "tax_status":       (3841065, "課税事業者情報", 6),
        "tax_number":       (3841066, "課税事業者番号", 1),
        "project_name":     (3831525, "プロジェクトまたは予算項目名", 1),
        "contract_purpose": (3831524, "契約書名・目的", 2),
        "budget_method":    (4143713, "予算稟議の方法", 6),
        "budget_request":   (3818322, "予算申請", 13),
        "multi_budget":     (4143714, "複数予算申請記載欄", 2),
        "budget_note":      (3818328, "予算関連備考", 2),
        "amount_range":     (3869371, "金額の範囲", 6),
        "amount":           (3818325, "発注額", 3),
        "payment_cycle":    (3818340, "支払サイクル", 6),
        "antisocial":       (3818330, "反社チェック", 5),
        "stock_number":     (3818331, "証券番号", 1),
        "antisocial_num":   (3818332, "反社チェック完了番号", 1),
        "nda":              (3831551, "秘密保持契約書の締結", 6),
        "basic_contract":   (3831552, "取引基本契約書", 6),
        "competitor_quote": (3822626, "相見積もり", 5),
        "signing_method":   (3818338, "締結方法", 5),
        "legal_check":      (3831553, "リーガルチェック", 5),
        "legal_url":        (3818339, "リーガルチェックURL", 1),
        "payment_method":   (3818341, "支払手段", 6),
    }
    
    # Also support direct form_item keys
    DIRECT_MAP = {}
    for alias, (fid, name, itype) in ALIAS_MAP.items():
        DIRECT_MAP[f"form_item{fid}"] = (fid, name, itype)
    
    items = []
    for key, value in payload.items():
        if key.startswith('_'):
            continue
        value = str(value).strip()
        if not value:
            continue
        
        if key in ALIAS_MAP:
            fid, name, itype = ALIAS_MAP[key]
        elif key in DIRECT_MAP:
            fid, name, itype = DIRECT_MAP[key]
        else:
            continue
        
        item = {
            "id": fid,
            "input_name": f"form_item{fid}",
            "item_name": name,
            "item_type": itype,
            "request_content": value,
        }
        
        # For checkbox (type 7), add select_item_labels
        if itype == 7:
            labels_map = {
                3831493: ["稟議", "事後稟議", "再稟議"],
                3818321: ["当社からの支払い（費用）", "取引先からの受取（売上）"],
                3818329: ["契約書", "発注書", "申込書", "利用規約合意"],
            }
            if fid in labels_map:
                all_labels = labels_map[fid]
                item["select_item_labels"] = all_labels
                item["select_item_labels_obj"] = [
                    {"label": l, "checked": l == value or value in l} 
                    for l in all_labels
                ]
        
        # For radio (type 5), add select_item_labels
        if itype == 5:
            labels_map = {
                3818330: ["上場企業(不要)", "非上場企業（反社チェック実施）"],
                3822626: ["未", "済"],
                3818338: ["電子契約", "書面契約（捺印）", "利用規約合意", "その他"],
                3831553: ["YES", "NO"],
            }
            if fid in labels_map:
                item["select_item_labels"] = labels_map[fid]
        
        # For dropdown (type 6), add select_item_labels
        if itype == 6:
            labels_map = {
                3818323: ["新規", "既存"],
                3841065: ["課税事業者", "免税事業者"],
                4143713: ["単独", "複数"],
                3869371: ["予算内", "500万円以上", "期間総予算の5%を超えるもの"],
                3818340: ["単発", "30日", "60日", "75日", "その他"],
                3831551: ["YES", "NO"],
                3831552: ["YES", "NO"],
                3818341: ["銀行振込", "クレジットカード", "Paypal", "紙付書", "その他"],
            }
            if fid in labels_map:
                item["select_item_labels"] = labels_map[fid]
        
        items.append(item)
    
    return items


@app.post("/api/fill")
async def fill(req: FillRequest, x_api_key: str = Header(None)):
    check_api_key(x_api_key)
    
    context, tokens = await login_jobcan(req.email, req.password)
    page = tokens["page"]
    csrf = tokens["csrf"]
    
    print(f"[LOGIN] OK")
    print(f"[TOKEN] CSRF={csrf}")
    
    results = []
    
    try:
        for idx, item in enumerate(req.items):
            payload = item.get("payload", {})
            flow_type = item.get("flow_type", "発注稟議")
            title = payload.get("_title", payload.get("title", "mikai自動申請"))
            
            print(f"\n[MAIN] Item {idx+1}/{len(req.items)}: {title}")
            
            if flow_type == "発注稟議":
                form_id = 666628
                flow_id = FORM_666628["flow_id"]
                group_id = FORM_666628["group_id"]
                form_json_items = build_form_json_666628(payload)
            else:
                results.append({
                    "row": idx, "status": "skip",
                    "message": f"未対応の flow_type: {flow_type}"
                })
                continue
            
            is_draft = req.action == "draft"
            
            print(f"[API] form_json has {len(form_json_items)} items")
            
            # ── Try multiple body format patterns ──
            patterns = []
            
            # Pattern 1: form_json as list, with flow_id/group_id at top level
            patterns.append(("P1: list + flow+group", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group_id": group_id,
                "title": title,
                "is_draft": is_draft,
                "form_json": form_json_items,
            }))
            
            # Pattern 2: form_json as stringified JSON
            patterns.append(("P2: stringified form_json", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group_id": group_id,
                "title": title,
                "is_draft": is_draft,
                "form_json": json.dumps(form_json_items, ensure_ascii=False),
            }))
            
            # Pattern 3: with "group" instead of "group_id"
            patterns.append(("P3: group instead of group_id", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group": group_id,
                "title": title,
                "is_draft": is_draft,
                "form_json": form_json_items,
            }))
            
            # Pattern 4: with client field
            patterns.append(("P4: with client", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group_id": group_id,
                "client": FORM_666628["client"],
                "title": title,
                "is_draft": is_draft,
                "form_json": form_json_items,
            }))
            
            # Pattern 5: form as multipart-like structure
            patterns.append(("P5: form + request_json", {
                "form": form_id,
                "flow": flow_id,
                "group": group_id,
                "title": title,
                "is_draft": is_draft,
                "request_json": form_json_items,
            }))
            
            # Pattern 6: No is_draft, add status field
            patterns.append(("P6: status=draft", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group_id": group_id,
                "title": title,
                "status": "draft",
                "form_json": form_json_items,
            }))
            
            # Pattern 7: Minimal — just form_id and form_json
            patterns.append(("P7: minimal", {
                "form_id": form_id,
                "form_json": form_json_items,
            }))
            
            # Pattern 8: form_json stringified + all metadata
            patterns.append(("P8: stringified + all meta", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group_id": group_id,
                "group_name": FORM_666628["group_name"],
                "client": FORM_666628["client"],
                "form_type": FORM_666628["form_type"],
                "title": title,
                "is_draft": is_draft,
                "form_json": json.dumps(form_json_items, ensure_ascii=False),
            }))
            
            # Pattern 9: with request_user_id
            patterns.append(("P9: with user_id", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group_id": group_id,
                "request_user_id": 1111126,
                "title": title,
                "is_draft": is_draft,
                "form_json": form_json_items,
            }))
            
            # Pattern 10: snake_case form_items (not form_json)
            patterns.append(("P10: form_items key", {
                "form_id": form_id,
                "flow_id": flow_id,
                "group_id": group_id,
                "title": title,
                "is_draft": is_draft,
                "form_items": form_json_items,
            }))
            
            # Try each pattern
            attempt_results = []
            for pname, body in patterns:
                try:
                    body_str = json.dumps(body, ensure_ascii=False)
                    print(f"[API] {pname}: {body_str[:200]}...")
                    
                    resp = await page.evaluate('''async (args) => {
                        const [url, body, csrf] = args;
                        try {
                            const r = await fetch(url, {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                    'X-CSRFToken': csrf,
                                    'X-Requested-With': 'XMLHttpRequest',
                                    'Referer': 'https://ssl.wf.jobcan.jp/',
                                },
                                body: body,
                                credentials: 'include',
                            });
                            const status = r.status;
                            let text = '';
                            try { text = await r.text(); } catch(e) {}
                            // Try to parse as JSON
                            let json_data = null;
                            try { json_data = JSON.parse(text); } catch(e) {}
                            return {status, text: text.substring(0, 500), json: json_data};
                        } catch(e) {
                            return {error: e.message};
                        }
                    }''', [
                        f"{JOBCAN_WF_BASE}/api/v1/requests/new/",
                        body_str,
                        csrf,
                    ])
                    
                    status = resp.get('status', 0)
                    print(f"[API] {pname} → HTTP {status}")
                    
                    attempt_results.append({
                        "pattern": pname,
                        "http_status": status,
                        "response_preview": resp.get('text', '')[:300],
                        "json_response": resp.get('json'),
                    })
                    
                    # If we got anything other than 500, that's progress!
                    if status not in (500,):
                        print(f"[API] ★★★ {pname} got HTTP {status}! Breaking. ★★★")
                        if status in (200, 201):
                            results.append({
                                "row": idx, "status": "success",
                                "pattern": pname,
                                "response": resp.get('json') or resp.get('text', ''),
                            })
                        else:
                            # 400, 403, 422 etc = body format closer, but validation error
                            results.append({
                                "row": idx, "status": "validation_error",
                                "pattern": pname, "http_status": status,
                                "response": resp.get('json') or resp.get('text', '')[:500],
                            })
                        break
                    
                except Exception as e:
                    attempt_results.append({
                        "pattern": pname,
                        "error": str(e)[:200],
                    })
            else:
                # All patterns returned 500
                results.append({
                    "row": idx, "status": "all_500",
                    "message": "全 pattern 都回 500",
                    "attempts": attempt_results,
                })
    
    finally:
        await context.close()
    
    return {"results": results}


# ══════════════════════════════════════════════════════════
# /api/probe — 輕量測試：只測 API 路徑 + 基本 body
# ══════════════════════════════════════════════════════════

@app.post("/api/probe")
async def probe(req: FillRequest, x_api_key: str = Header(None)):
    """快速測試多個 API 路徑和 body 組合"""
    check_api_key(x_api_key)
    
    context, tokens = await login_jobcan(req.email, req.password)
    page = tokens["page"]
    csrf = tokens["csrf"]
    
    try:
        # Minimal test body
        test_body = {
            "form_id": 666628,
            "flow_id": 401080,
            "group_id": 560177,
            "title": "テスト",
            "is_draft": True,
            "form_json": build_form_json_666628({
                "ringi_type": "稟議",
                "contract_date": "2026/04/04",
                "content_type": "当社からの支払い（費用）",
                "application_type": "発注書",
                "vendor_type": "新規",
                "vendor_name": "テスト株式会社",
                "project_name": "テストプロジェクト",
                "contract_purpose": "テスト発注",
                "budget_method": "単独",
                "amount_range": "予算内",
                "amount": "100000",
                "payment_cycle": "単発",
                "nda": "NO",
                "basic_contract": "NO",
                "payment_method": "銀行振込",
                "tax_status": "課税事業者",
            }),
        }
        
        # Test paths
        paths = [
            "/api/v1/requests/new/",
            "/api/v1/requests/",
            "/api/v1/request/new/",
            "/api/v1/request/create/",
        ]
        
        # Test content types
        content_types = [
            "application/json",
            "application/x-www-form-urlencoded",
        ]
        
        probe_results = []
        
        for path in paths:
            for ct in content_types:
                body_str = json.dumps(test_body, ensure_ascii=False)
                
                resp = await page.evaluate('''async (args) => {
                    const [url, body, csrf, contentType] = args;
                    try {
                        const headers = {
                            'Content-Type': contentType,
                            'X-CSRFToken': csrf,
                            'X-Requested-With': 'XMLHttpRequest',
                            'Referer': 'https://ssl.wf.jobcan.jp/',
                        };
                        
                        let finalBody = body;
                        if (contentType === 'application/x-www-form-urlencoded') {
                            const obj = JSON.parse(body);
                            const params = new URLSearchParams();
                            for (const [k, v] of Object.entries(obj)) {
                                params.append(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
                            }
                            finalBody = params.toString();
                        }
                        
                        const r = await fetch(url, {
                            method: 'POST',
                            headers,
                            body: finalBody,
                            credentials: 'include',
                        });
                        const status = r.status;
                        let text = '';
                        try { text = await r.text(); } catch(e) {}
                        let json_data = null;
                        try { json_data = JSON.parse(text); } catch(e) {}
                        return {status, text: text.substring(0, 300), json: json_data};
                    } catch(e) {
                        return {error: e.message};
                    }
                }''', [
                    f"{JOBCAN_WF_BASE}{path}",
                    body_str,
                    csrf,
                    ct,
                ])
                
                probe_results.append({
                    "path": path,
                    "content_type": ct,
                    "status": resp.get("status"),
                    "response": resp.get("json") or resp.get("text", "")[:300],
                })
                
                print(f"[PROBE] {path} ({ct}) → {resp.get('status')}")
        
        return {"csrf": csrf, "results": probe_results}
    
    finally:
        await context.close()


# ══════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
