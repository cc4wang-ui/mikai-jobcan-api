"""
mikai Jobcan Bridge — v6.1.0
Phase B Railway Backend

v6.1: page.content() 取代 page.evaluate() 抓 JS URLs（避免 context destroyed）
"""

import json
import re
import asyncio
import os
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from playwright.async_api import async_playwright, Browser

# ── Config ──────────────────────────────────────────────
API_KEY = os.getenv("API_KEY", "mikai-prod-2026")
PORT = int(os.getenv("PORT", "8080"))
VERSION = "6.1.0"

JOBCAN_LOGIN_URL = "https://id.jobcan.jp/users/sign_in"
JOBCAN_WF_BASE = "https://ssl.wf.jobcan.jp"

FORM_666628 = {
    "form_id": 666628, "flow_id": 401080, "flow_id_high": 401084,
    "form_type": 1, "client": 53786, "group_id": 560177, "group_name": "Board",
}

# ── Playwright singleton ────────────────────────────────
_browser: Optional[Browser] = None

async def get_browser() -> Browser:
    global _browser
    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(
            headless=True,
            args=['--disable-blink-features=AutomationControlled',
                  '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
    return _browser

# ── Login ───────────────────────────────────────────────
async def login_jobcan(email: str, password: str) -> tuple:
    browser = await get_browser()
    ctx = await browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
    page = await ctx.new_page()
    await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

    await page.goto(JOBCAN_LOGIN_URL)
    await page.wait_for_load_state('networkidle')
    await page.fill('#user_email', email)
    await page.fill('#user_password', password)
    await page.click('[name="commit"]')

    for _ in range(20):
        await asyncio.sleep(1)
        if 'sign_in' not in page.url:
            break
    if 'sign_in' in page.url:
        await ctx.close()
        raise HTTPException(status_code=401, detail="Jobcan login failed")

    await page.goto(JOBCAN_WF_BASE + '/', wait_until='domcontentloaded')
    await asyncio.sleep(4)

    # verify page is alive
    for _ in range(5):
        try:
            await page.evaluate('() => 1')
            break
        except Exception:
            await asyncio.sleep(1)

    cookies = await ctx.cookies()
    csrf = next((c['value'] for c in cookies if c['name'] == 'csrftoken'), None)
    return ctx, {"csrf": csrf, "page": page}

# ── Models ──────────────────────────────────────────────
class FillRequest(BaseModel):
    email: str
    password: str
    items: list
    action: str = "draft"

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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def check_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}

# ══════════════════════════════════════════════════════════
# /api/recon-js
# ══════════════════════════════════════════════════════════
@app.post("/api/recon-js")
async def recon_js(req: ReconJSRequest, x_api_key: str = Header(None)):
    check_api_key(x_api_key)
    ctx, tokens = await login_jobcan(req.email, req.password)
    page = tokens["page"]
    csrf = tokens["csrf"]

    results = {
        "login": "ok", "csrf": csrf, "page_url": page.url,
        "js_files": [], "submission_snippets": [], "form_json_snippets": [],
        "angular_info": None, "errors": [],
    }

    async def safe_eval(js, fallback=None):
        for i in range(3):
            try:
                return await page.evaluate(js)
            except Exception as e:
                if 'destroyed' in str(e) or 'navigation' in str(e):
                    await asyncio.sleep(2)
                else:
                    results["errors"].append(f"eval: {str(e)[:80]}")
                    return fallback
        return fallback

    try:
        # 1. Get JS URLs from raw HTML — no evaluate needed
        html = await page.content()
        script_srcs = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html)
        js_urls = []
        for src in script_srcs:
            if src.startswith('//'):
                src = 'https:' + src
            elif src.startswith('/'):
                src = JOBCAN_WF_BASE + src
            elif not src.startswith('http'):
                src = JOBCAN_WF_BASE + '/' + src
            js_urls.append(src)
        results["js_files"] = js_urls
        print(f"[RECON] {len(js_urls)} script tags found")

        # 2. Download & search each app JS
        search_terms = [
            'requests/new', 'request/new',
            'form_json', 'formJson', 'form_id',
            'is_draft', 'isDraft',
            'flow_id', 'flowId', 'group_id', 'groupId',
            'submitRequest', 'createRequest', 'saveRequest', 'saveDraft',
            '$http.post', '.post(',
        ]

        skip_libs = ['angular.min', 'angular-', 'jquery', 'bootstrap',
                     'moment', 'lodash', 'underscore', 'ui-', 'polyfill',
                     'cdn', 'vendor']

        for js_url in js_urls:
            fname = js_url.split('/')[-1].split('?')[0]
            if any(lib in fname.lower() for lib in skip_libs):
                continue
            try:
                js = await safe_eval(f'''async () => {{
                    try {{ const r = await fetch("{js_url}"); return await r.text(); }}
                    catch(e) {{ return "ERR:" + e.message; }}
                }}''')
                if not js or str(js).startswith("ERR:") or len(str(js)) < 100:
                    continue
                js = str(js)
                print(f"[RECON] {fname}: {len(js)} bytes")

                for term in search_terms:
                    for m in re.finditer(re.escape(term), js, re.IGNORECASE):
                        s = max(0, m.start() - 300)
                        e = min(len(js), m.end() + 300)
                        snippet = js[s:e]
                        is_sub = any(t in term.lower() for t in ['request', 'submit', 'save', 'draft', 'post', 'create'])
                        k = "submission_snippets" if is_sub else "form_json_snippets"
                        results[k].append({"file": fname[:60], "size": len(js), "term": term, "snippet": snippet})
            except Exception as ex:
                results["errors"].append(f"fetch {fname}: {str(ex)[:80]}")

        # 3. AngularJS info
        results["angular_info"] = await safe_eval('''() => {
            try {
                if (!window.angular) return {error: "no angular"};
                var inj = angular.element(document.body).injector();
                if (!inj) return {error: "no injector"};
                var rt = inj.get("$route");
                var routes = {};
                if (rt && rt.routes) {
                    for (var p in rt.routes) {
                        routes[p] = {controller: rt.routes[p].controller, template: rt.routes[p].templateUrl};
                    }
                }
                return {version: angular.version ? angular.version.full : "?", routes: routes};
            } catch(e) { return {error: e.message}; }
        }''')

        # Deduplicate
        for k in ["submission_snippets", "form_json_snippets"]:
            seen = set()
            uniq = []
            for item in results[k]:
                sig = item.get("snippet", "")[:80]
                if sig not in seen:
                    seen.add(sig)
                    uniq.append(item)
            results[k] = uniq[:30]

    except Exception as ex:
        results["errors"].append(traceback.format_exc()[-300:])
    finally:
        await ctx.close()
    return results

# ══════════════════════════════════════════════════════════
# /api/fill — 系統性 body 格式嘗試
# ══════════════════════════════════════════════════════════
def build_form_json_666628(payload: dict) -> list:
    ALIAS = {
        "ringi_type":(3831493,"稟議の種類",7),"contract_date":(3831494,"契約締結日",4),
        "content_type":(3818321,"内容",7),"application_type":(3818329,"申請内容",7),
        "vendor_type":(3818323,"取引先種別",6),"vendor_name":(3822625,"取引先名",1),
        "vendor_website":(3818337,"取引先ウェブサイト",1),"bank_info":(3841064,"銀行情報",2),
        "tax_status":(3841065,"課税事業者情報",6),"tax_number":(3841066,"課税事業者番号",1),
        "project_name":(3831525,"プロジェクトまたは予算項目名",1),
        "contract_purpose":(3831524,"契約書名・目的",2),
        "budget_method":(4143713,"予算稟議の方法",6),"budget_request":(3818322,"予算申請",13),
        "multi_budget":(4143714,"複数予算申請記載欄",2),"budget_note":(3818328,"予算関連備考",2),
        "amount_range":(3869371,"金額の範囲",6),"amount":(3818325,"発注額",3),
        "payment_cycle":(3818340,"支払サイクル",6),"antisocial":(3818330,"反社チェック",5),
        "stock_number":(3818331,"証券番号",1),"antisocial_num":(3818332,"反社チェック完了番号",1),
        "nda":(3831551,"秘密保持契約書の締結",6),"basic_contract":(3831552,"取引基本契約書",6),
        "competitor_quote":(3822626,"相見積もり",5),"signing_method":(3818338,"締結方法",5),
        "legal_check":(3831553,"リーガルチェック",5),"legal_url":(3818339,"リーガルチェックURL",1),
        "payment_method":(3818341,"支払手段",6),
    }
    DIRECT = {f"form_item{fid}": (fid, n, t) for _, (fid, n, t) in ALIAS.items()}
    CB = {3831493:["稟議","事後稟議","再稟議"],3818321:["当社からの支払い（費用）","取引先からの受取（売上）"],3818329:["契約書","発注書","申込書","利用規約合意"]}
    RD = {3818330:["上場企業(不要)","非上場企業（反社チェック実施）"],3822626:["未","済"],3818338:["電子契約","書面契約（捺印）","利用規約合意","その他"],3831553:["YES","NO"]}
    DD = {3818323:["新規","既存"],3841065:["課税事業者","免税事業者"],4143713:["単独","複数"],3869371:["予算内","500万円以上","期間総予算の5%を超えるもの"],3818340:["単発","30日","60日","75日","その他"],3831551:["YES","NO"],3831552:["YES","NO"],3818341:["銀行振込","クレジットカード","Paypal","紙付書","その他"]}

    items = []
    for key, value in payload.items():
        if key.startswith('_'): continue
        value = str(value).strip()
        if not value: continue
        if key in ALIAS: fid, name, itype = ALIAS[key]
        elif key in DIRECT: fid, name, itype = DIRECT[key]
        else: continue
        item = {"id": fid, "input_name": f"form_item{fid}", "item_name": name, "item_type": itype, "request_content": value}
        if itype == 7 and fid in CB:
            item["select_item_labels"] = CB[fid]
            item["select_item_labels_obj"] = [{"label": l, "checked": l == value or value in l} for l in CB[fid]]
        if itype == 5 and fid in RD: item["select_item_labels"] = RD[fid]
        if itype == 6 and fid in DD: item["select_item_labels"] = DD[fid]
        items.append(item)
    return items

@app.post("/api/fill")
async def fill(req: FillRequest, x_api_key: str = Header(None)):
    check_api_key(x_api_key)
    ctx, tokens = await login_jobcan(req.email, req.password)
    page = tokens["page"]
    csrf = tokens["csrf"]
    print(f"[LOGIN] OK | CSRF={csrf}")

    results = []
    try:
        for idx, item in enumerate(req.items):
            payload = item.get("payload", {})
            flow_type = item.get("flow_type", "発注稟議")
            title = payload.get("_title", payload.get("title", "mikai自動申請"))
            if flow_type != "発注稟議":
                results.append({"row": idx, "status": "skip", "msg": f"未対応: {flow_type}"})
                continue
            fj = build_form_json_666628(payload)
            is_draft = req.action == "draft"
            print(f"\n[FILL] #{idx+1}: {title} ({len(fj)} fields)")

            FI = FORM_666628
            patterns = [
                ("P1:list+flow+group", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group_id":FI["group_id"],"title":title,"is_draft":is_draft,"form_json":fj}),
                ("P2:stringify", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group_id":FI["group_id"],"title":title,"is_draft":is_draft,"form_json":json.dumps(fj,ensure_ascii=False)}),
                ("P3:group_key", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group":FI["group_id"],"title":title,"is_draft":is_draft,"form_json":fj}),
                ("P4:+client", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group_id":FI["group_id"],"client":FI["client"],"title":title,"is_draft":is_draft,"form_json":fj}),
                ("P5:alt_keys", {"form":FI["form_id"],"flow":FI["flow_id"],"group":FI["group_id"],"title":title,"is_draft":is_draft,"request_json":fj}),
                ("P6:status_draft", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group_id":FI["group_id"],"title":title,"status":"draft","form_json":fj}),
                ("P7:minimal", {"form_id":FI["form_id"],"form_json":fj}),
                ("P8:stringify+meta", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group_id":FI["group_id"],"group_name":FI["group_name"],"client":FI["client"],"form_type":FI["form_type"],"title":title,"is_draft":is_draft,"form_json":json.dumps(fj,ensure_ascii=False)}),
                ("P9:+user_id", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group_id":FI["group_id"],"request_user_id":1111126,"title":title,"is_draft":is_draft,"form_json":fj}),
                ("P10:form_items", {"form_id":FI["form_id"],"flow_id":FI["flow_id"],"group_id":FI["group_id"],"title":title,"is_draft":is_draft,"form_items":fj}),
            ]

            attempt_results = []
            for pname, body in patterns:
                try:
                    bs = json.dumps(body, ensure_ascii=False)
                    print(f"[API] {pname}")
                    resp = await page.evaluate('''async (args) => {
                        var url=args[0], body=args[1], csrf=args[2];
                        try {
                            var r = await fetch(url, {
                                method:'POST',
                                headers:{'Content-Type':'application/json','X-CSRFToken':csrf,'X-Requested-With':'XMLHttpRequest','Referer':'https://ssl.wf.jobcan.jp/'},
                                body:body, credentials:'include'
                            });
                            var text=''; try{text=await r.text()}catch(e){}
                            var jd=null; try{jd=JSON.parse(text)}catch(e){}
                            return {status:r.status, text:text.substring(0,500), json:jd};
                        } catch(e){return {error:e.message};}
                    }''', [f"{JOBCAN_WF_BASE}/api/v1/requests/new/", bs, csrf])

                    st = resp.get('status', 0)
                    print(f"[API] {pname} → {st}")
                    attempt_results.append({"pattern":pname,"http":st,"preview":resp.get('text','')[:300],"json":resp.get('json')})

                    if st not in (500,):
                        print(f"[API] ★ {pname} → {st} ★")
                        results.append({"row":idx,"status":"ok" if st in(200,201) else "validation","pattern":pname,"http":st,"resp":resp.get('json') or resp.get('text','')[:500]})
                        break
                except Exception as ex:
                    attempt_results.append({"pattern":pname,"error":str(ex)[:200]})
            else:
                results.append({"row":idx,"status":"all_500","attempts":attempt_results})
    finally:
        await ctx.close()
    return {"results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
