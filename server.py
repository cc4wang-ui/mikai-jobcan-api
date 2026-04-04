"""
mikai Jobcan Bridge — v6.2.0

v6.2: 
- /api/recon-crs: 抓 create_request_services.js 的完整內容
- /api/fill: 新增 form_items_data wrapper pattern
"""

import json, re, asyncio, os, traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from playwright.async_api import async_playwright, Browser

API_KEY = os.getenv("API_KEY", "mikai-prod-2026")
PORT = int(os.getenv("PORT", "8080"))
VERSION = "6.2.0"
JOBCAN_LOGIN_URL = "https://id.jobcan.jp/users/sign_in"
JOBCAN_WF_BASE = "https://ssl.wf.jobcan.jp"

FORM_666628 = {
    "form_id": 666628, "flow_id": 401080, "form_type": 1,
    "client": 53786, "group_id": 560177, "group_name": "Board",
}

_browser: Optional[Browser] = None

async def get_browser() -> Browser:
    global _browser
    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True,
            args=['--disable-blink-features=AutomationControlled',
                  '--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
    return _browser

async def login_jobcan(email, password):
    browser = await get_browser()
    ctx = await browser.new_context(user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
    page = await ctx.new_page()
    await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    await page.goto(JOBCAN_LOGIN_URL)
    await page.wait_for_load_state('networkidle')
    await page.fill('#user_email', email)
    await page.fill('#user_password', password)
    await page.click('[name="commit"]')
    for _ in range(20):
        await asyncio.sleep(1)
        if 'sign_in' not in page.url: break
    if 'sign_in' in page.url:
        await ctx.close()
        raise HTTPException(status_code=401, detail="Jobcan login failed")
    await page.goto(JOBCAN_WF_BASE+'/', wait_until='domcontentloaded')
    await asyncio.sleep(4)
    for _ in range(5):
        try:
            await page.evaluate('()=>1')
            break
        except: await asyncio.sleep(1)
    cookies = await ctx.cookies()
    csrf = next((c['value'] for c in cookies if c['name']=='csrftoken'), None)
    return ctx, {"csrf": csrf, "page": page}

class FillRequest(BaseModel):
    email: str
    password: str
    items: list
    action: str = "draft"

class ReconRequest(BaseModel):
    email: str
    password: str

@asynccontextmanager
async def lifespan(app):
    print(f"[BOOT] mikai Jobcan Bridge v{VERSION} on port {PORT}")
    yield
    if _browser: await _browser.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def check_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}


# ══════════════════════════════════════════════════════════
# /api/recon-crs — 抓 create_request_services.js 完整內容
# ══════════════════════════════════════════════════════════
@app.post("/api/recon-crs")
async def recon_crs(req: ReconRequest, x_api_key: str = Header(None)):
    check_api_key(x_api_key)
    ctx, tokens = await login_jobcan(req.email, req.password)
    page = tokens["page"]
    
    # Target JS files that build request_data
    targets = [
        f"{JOBCAN_WF_BASE}/static/wf/scripts/services/create_request_services.js?ci-build-345216",
        f"{JOBCAN_WF_BASE}/static/wf/scripts/controllers/create_request_controllers.js?ci-build-345216",
        f"{JOBCAN_WF_BASE}/static/wf/scripts/services/create_request/form_item_services.js?ci-build-345216",
        f"{JOBCAN_WF_BASE}/static/wf/scripts/controllers/request_new_controllers.js?ci-build-345216",
    ]
    
    results = {"login": "ok", "files": {}}
    
    try:
        for url in targets:
            fname = url.split('/')[-1].split('?')[0]
            try:
                content = await page.evaluate(f'''async () => {{
                    try {{ const r = await fetch("{url}"); return await r.text(); }}
                    catch(e) {{ return "ERR:" + e.message; }}
                }}''')
                if content and not str(content).startswith("ERR:"):
                    results["files"][fname] = str(content)
                    print(f"[RECON] {fname}: {len(str(content))} bytes")
                else:
                    results["files"][fname] = f"ERROR: {content}"
            except Exception as e:
                results["files"][fname] = f"FETCH_ERROR: {str(e)[:200]}"
    except Exception as e:
        results["error"] = traceback.format_exc()[-300:]
    finally:
        await ctx.close()
    
    return results


# ══════════════════════════════════════════════════════════
# /api/fill — 新增 form_items_data wrapper patterns
# ══════════════════════════════════════════════════════════
def build_form_items_data(payload: dict) -> list:
    """Build form_items_data array matching Jobcan's form definition structure."""
    ALIAS = {
        "ringi_type":(3831493,"稟議の種類",7,"form_item0"),
        "contract_date":(3831494,"契約締結日",4,"form_item1"),
        "content_type":(3818321,"内容",7,"form_item2"),
        "application_type":(3818329,"申請内容",7,"form_item4"),
        "vendor_type":(3818323,"取引先種別",6,"form_item3"),
        "vendor_name":(3822625,"取引先名",1,"form_item0"),
        "vendor_website":(3818337,"取引先ウェブサイト",1,"form_item0"),
        "bank_info":(3841064,"銀行情報",2,"form_item0"),
        "tax_status":(3841065,"課税事業者情報",6,"form_item1"),
        "tax_number":(3841066,"課税事業者番号",1,"form_item2"),
        "project_name":(3831525,"プロジェクトまたは予算項目名",1,"form_item1"),
        "contract_purpose":(3831524,"契約書名・目的",2,"form_item0"),
        "budget_method":(4143713,"予算稟議の方法",6,"form_item0"),
        "amount_range":(3869371,"金額の範囲",6,"form_item0"),
        "amount":(3818325,"発注額",3,"form_item0"),
        "payment_cycle":(3818340,"支払サイクル",6,"form_item3"),
        "antisocial":(3818330,"反社チェック",5,"form_item5"),
        "nda":(3831551,"秘密保持契約書の締結",6,"form_item0"),
        "basic_contract":(3831552,"取引基本契約書",6,"form_item1"),
        "competitor_quote":(3822626,"相見積もり",5,"form_item1"),
        "signing_method":(3818338,"締結方法",5,"form_item1"),
        "legal_check":(3831553,"リーガルチェック",5,"form_item0"),
        "payment_method":(3818341,"支払手段",6,"form_item4"),
    }
    DIRECT = {}
    for alias, (fid, n, t, inp) in ALIAS.items():
        DIRECT[f"form_item{fid}"] = (fid, n, t, inp)

    CB = {3831493:["稟議","事後稟議","再稟議"],3818321:["当社からの支払い（費用）","取引先からの受取（売上）"],3818329:["契約書","発注書","申込書","利用規約合意"]}
    RD = {3818330:["上場企業(不要)","非上場企業（反社チェック実施）"],3822626:["未","済"],3818338:["電子契約","書面契約（捺印）","利用規約合意","その他"],3831553:["YES","NO"]}
    DD = {3818323:["新規","既存"],3841065:["課税事業者","免税事業者"],4143713:["単独","複数"],3869371:["予算内","500万円以上","期間総予算の5%を超えるもの"],3818340:["単発","30日","60日","75日","その他"],3831551:["YES","NO"],3831552:["YES","NO"],3818341:["銀行振込","クレジットカード","Paypal","紙付書","その他"]}

    items = []
    for key, value in payload.items():
        if key.startswith('_'): continue
        value = str(value).strip()
        if not value: continue
        if key in ALIAS: fid, name, itype, input_name = ALIAS[key]
        elif key in DIRECT: fid, name, itype, input_name = DIRECT[key]
        else: continue

        item = {
            "id": fid,
            "input_name": input_name,
            "item_name": name,
            "item_type": itype,
            "request_content": value,
            "input_pattern": 1,
            "row_number": 1,
            "new_flg": 0,
        }
        if itype == 7 and fid in CB:
            item["select_item_labels"] = "\n".join(CB[fid])
            item["is_required"] = True
        if itype == 5 and fid in RD:
            item["select_item_labels"] = "\n".join(RD[fid])
        if itype == 6 and fid in DD:
            item["select_item_labels"] = "\n".join(DD[fid])
            item["is_required"] = True
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
            title = payload.get("_title", "mikai自動申請")
            fj_items = build_form_items_data(payload)
            is_draft = req.action == "draft"
            print(f"\n[FILL] #{idx+1}: {title} ({len(fj_items)} fields)")

            FI = FORM_666628

            # Build form_json as stringified JSON matching Jobcan's format
            form_json_obj = {
                "form_items_data": fj_items,
                "form_name": "契約・発注稟議",
                "description": "",
            }
            form_json_str = json.dumps(form_json_obj, ensure_ascii=False)

            # Also build flat list version
            flat_items = [{
                "id": i["id"],
                "input_name": i["input_name"],
                "item_name": i["item_name"],
                "item_type": i["item_type"],
                "request_content": i["request_content"],
            } for i in fj_items]

            patterns = [
                # P1: form_json = stringified {form_items_data: [...]} (matches form definition format)
                ("P1:wrapped_stringify", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "title": title,
                    "is_draft": is_draft,
                    "form_json": form_json_str,
                }),
                # P2: form_json = {form_items_data: [...]} (object, not string)
                ("P2:wrapped_object", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "title": title,
                    "is_draft": is_draft,
                    "form_json": form_json_obj,
                }),
                # P3: form_json = flat list (original attempt)
                ("P3:flat_list", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "title": title,
                    "is_draft": is_draft,
                    "form_json": flat_items,
                }),
                # P4: form_json = stringified flat list
                ("P4:flat_stringify", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "title": title,
                    "is_draft": is_draft,
                    "form_json": json.dumps(flat_items, ensure_ascii=False),
                }),
                # P5: with group (not group_id) + wrapped stringify
                ("P5:group+wrapped", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group": FI["group_id"], "title": title,
                    "is_draft": is_draft,
                    "form_json": form_json_str,
                }),
                # P6: with request_user_id + wrapped stringify
                ("P6:user+wrapped", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "request_user_id": 1111126,
                    "title": title, "is_draft": is_draft,
                    "form_json": form_json_str,
                }),
                # P7: with client + wrapped stringify
                ("P7:client+wrapped", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "client": FI["client"],
                    "title": title, "is_draft": is_draft,
                    "form_json": form_json_str,
                }),
                # P8: minimal — just form_id + form_json wrapped
                ("P8:minimal_wrapped", {
                    "form_id": FI["form_id"],
                    "title": title,
                    "is_draft": is_draft,
                    "form_json": form_json_str,
                }),
                # P9: with all metadata + wrapped
                ("P9:all_meta", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "client": FI["client"],
                    "form_type": FI["form_type"], "request_user_id": 1111126,
                    "title": title, "is_draft": is_draft,
                    "form_json": form_json_str,
                    "request_files": [],
                }),
                # P10: form_items_data at top level (not nested in form_json)
                ("P10:top_level_items", {
                    "form_id": FI["form_id"], "flow_id": FI["flow_id"],
                    "group_id": FI["group_id"], "title": title,
                    "is_draft": is_draft,
                    "form_items_data": fj_items,
                }),
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
                    print(f"[API] {pname} → HTTP {st}")
                    attempt_results.append({"pattern": pname, "http": st,
                        "preview": resp.get('text','')[:300], "json": resp.get('json')})

                    if st not in (500,):
                        print(f"[API] ★ {pname} → {st} ★")
                        results.append({"row": idx, "status": "ok" if st in (200,201) else "validation",
                            "pattern": pname, "http": st,
                            "resp": resp.get('json') or resp.get('text','')[:500]})
                        break
                except Exception as ex:
                    attempt_results.append({"pattern": pname, "error": str(ex)[:200]})
            else:
                results.append({"row": idx, "status": "all_500", "attempts": attempt_results})
    finally:
        await ctx.close()
    return {"results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
