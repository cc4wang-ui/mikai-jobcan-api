"""
mikai Jobcan Bridge — v7.1.0

v7.0 → 400 (不再 500！body 結構對了)
v7.1: 系統性 variation 找出 400 的具體原因
"""

import json, asyncio, os, traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from playwright.async_api import async_playwright, Browser

API_KEY = os.getenv("API_KEY", "mikai-prod-2026")
PORT = int(os.getenv("PORT", "8080"))
VERSION = "7.1.0"
JOBCAN_LOGIN_URL = "https://id.jobcan.jp/users/sign_in"
JOBCAN_WF_BASE = "https://ssl.wf.jobcan.jp"
FORM_666628 = {
    "form_id": 666628, "flow_id": 401080, "form_type": 1,
    "client": 53786, "group_id": 560177, "group_name": "Board",
}

_browser: Optional[Browser] = None
async def get_browser():
    global _browser
    if _browser is None or not _browser.is_connected():
        pw = await async_playwright().start()
        _browser = await pw.chromium.launch(headless=True,
            args=['--disable-blink-features=AutomationControlled','--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
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
        try: await page.evaluate('()=>1'); break
        except: await asyncio.sleep(1)
    cookies = await ctx.cookies()
    csrf = next((c['value'] for c in cookies if c['name']=='csrftoken'), None)
    return ctx, {"csrf": csrf, "page": page}

class FillRequest(BaseModel):
    email: str
    password: str
    items: list
    action: str = "draft"

@asynccontextmanager
async def lifespan(app):
    print(f"[BOOT] mikai Jobcan Bridge v{VERSION} on port {PORT}")
    yield
    if _browser: await _browser.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
def check_api_key(x_api_key: str = Header(None)):
    if x_api_key != API_KEY: raise HTTPException(status_code=403, detail="Invalid API key")

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}


# ── Field definitions ──
CB = {
    3831493: ["稟議","事後稟議","再稟議"],
    3818321: ["当社からの支払い（費用）","取引先からの受取（売上）"],
    3818329: ["契約書","発注書","申込書","利用規約合意"],
}
FMAP = {
    "ringi_type":(3831493,"稟議の種類",7,1),"contract_date":(3831494,"契約締結日",4,1),
    "content_type":(3818321,"内容",7,1),"application_type":(3818329,"申請内容",7,1),
    "vendor_type":(3818323,"取引先種別",6,1),"vendor_name":(3822625,"取引先名",1,1),
    "vendor_website":(3818337,"取引先ウェブサイト",1,1),"bank_info":(3841064,"銀行情報",2,1),
    "tax_status":(3841065,"課税事業者情報",6,1),"tax_number":(3841066,"課税事業者番号",1,1),
    "project_name":(3831525,"プロジェクトまたは予算項目名",1,1),
    "contract_purpose":(3831524,"契約書名・目的",2,1),
    "budget_method":(4143713,"予算稟議の方法",6,1),
    "amount_range":(3869371,"金額の範囲",6,1),"amount":(3818325,"発注額",3,1),
    "payment_cycle":(3818340,"支払サイクル",6,1),"antisocial":(3818330,"反社チェック",5,1),
    "nda":(3831551,"秘密保持契約書の締結",6,1),"basic_contract":(3831552,"取引基本契約書",6,1),
    "competitor_quote":(3822626,"相見積もり",5,1),"signing_method":(3818338,"締結方法",5,1),
    "legal_check":(3831553,"リーガルチェック",5,1),"payment_method":(3818341,"支払手段",6,1),
}
DMAP = {f"form_item{fid}": (fid,n,t,r) for _,(fid,n,t,r) in FMAP.items()}


def build_form_items(payload):
    """prepareFormItemsData() format: form_item_id, form_item_type, form_item_name, content"""
    items = []
    for key, value in payload.items():
        if key.startswith('_'): continue
        v = str(value).strip()
        if not v: continue
        if key in FMAP: fid,name,itype,row = FMAP[key]
        elif key in DMAP: fid,name,itype,row = DMAP[key]
        else: continue
        fi = {"row_number":row,"form_item_id":fid,"form_item_type":itype,"form_item_name":name,"content":v}
        if itype == 7 and fid in CB:
            fi["select_item_labels_obj"] = [{"label":l,"checked":(l==v or v in l)} for l in CB[fid]]
        items.append(fi)
    return items


def build_form_json_raw(payload):
    """Raw form_json items for request_data_json (matching form definition structure)"""
    SL = {
        3831493:"稟議\n事後稟議\n再稟議",3818321:"当社からの支払い（費用）\n取引先からの受取（売上）",
        3818329:"契約書\n発注書\n申込書\n利用規約合意",3818323:"新規\n既存",
        3841065:"課税事業者\n免税事業者",4143713:"単独\n複数",
        3869371:"予算内\n500万円以上\n期間総予算の5%を超えるもの",
        3818340:"単発\n30日\n60日\n75日\nその他",3831551:"YES\nNO",3831552:"YES\nNO",
        3818341:"銀行振込\nクレジットカード\nPaypal\n紙付書\nその他",
        3818330:"上場企業(不要)\n非上場企業（反社チェック実施）",3822626:"未\n済",
        3818338:"電子契約\n書面契約（捺印）\n利用規約合意\nその他",3831553:"YES\nNO",
    }
    items = []
    for key, value in payload.items():
        if key.startswith('_'): continue
        v = str(value).strip()
        if not v: continue
        if key in FMAP: fid,name,itype,_ = FMAP[key]
        elif key in DMAP: fid,name,itype,_ = DMAP[key]
        else: continue
        fj = {"id":fid,"item_name":name,"item_type":itype,"request_content":v,"input_pattern":1,"row_number":1,"new_flg":0}
        if fid in SL: fj["select_item_labels"] = SL[fid]
        if itype == 7 and fid in CB:
            fj["is_required"] = True
            fj["select_item_labels_obj"] = [{"label":l,"checked":(l==v or v in l)} for l in CB[fid]]
        items.append(fj)
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
            form_items = build_form_items(payload)
            form_json_raw = build_form_json_raw(payload)
            kind = 0 if req.action == "draft" else 1
            FI = FORM_666628
            
            print(f"\n[FILL] #{idx+1}: {title} ({len(form_items)} items)")

            # Base body (V1 got 400 — structure correct, content wrong)
            base = {
                "title": title, "kind": kind, "form_type": FI["form_type"],
                "edit_flow_flg": False, "project": None, "project_name": None, "project_code": None,
                "group": FI["group_id"], "group_name": FI["group_name"], "group_code": "",
                "requester_group_id": None, "requester_position_id": None,
                "form_id": FI["form_id"],
                "form_data": {"form_items": form_items, "request_form_custom_item": None},
                "request_data_json": {"form_json": form_json_raw, "title": title},
                "total_amount": 0, "currency_code": 392, "currency_show_flg": True,
            }
            
            variations = []
            
            # V1: base (reference — got 400)
            variations.append(("V1:base", base))
            
            # V2: stringify request_data_json
            v2 = dict(base)
            v2["request_data_json"] = json.dumps(base["request_data_json"], ensure_ascii=False)
            variations.append(("V2:rdj_str", v2))
            
            # V3: no request_data_json at all
            v3 = {k:v for k,v in base.items() if k != "request_data_json"}
            variations.append(("V3:no_rdj", v3))
            
            # V4: add flow_id (maybe Django needs it)
            v4 = dict(base)
            v4["flow_id"] = FI["flow_id"]
            variations.append(("V4:+flow_id", v4))
            
            # V5: request_files at top level
            v5 = dict(base)
            v5["request_files"] = []
            variations.append(("V5:+req_files", v5))
            
            # V6: form_data stringify
            v6 = dict(base)
            v6["form_data"] = json.dumps(base["form_data"], ensure_ascii=False)
            variations.append(("V6:fd_str", v6))
            
            # V7: all together - flow_id + request_files + stringify rdj
            v7 = dict(base)
            v7["flow_id"] = FI["flow_id"]
            v7["request_files"] = []
            v7["request_data_json"] = json.dumps(base["request_data_json"], ensure_ascii=False)
            variations.append(("V7:combined", v7))
            
            # V8: minimal — only what's absolutely needed
            v8 = {
                "title": title, "kind": kind, "form_id": FI["form_id"],
                "form_data": {"form_items": form_items},
            }
            variations.append(("V8:ultra_min", v8))

            attempt_results = []
            for vname, body in variations:
                try:
                    bs = json.dumps(body, ensure_ascii=False)
                    print(f"[API] {vname} ({len(bs)} bytes)")
                    
                    resp = await page.evaluate('''async (args) => {
                        var url=args[0],body=args[1],csrf=args[2];
                        try {
                            var r=await fetch(url,{method:'POST',
                                headers:{'Content-Type':'application/json','X-CSRFToken':csrf,
                                    'X-Requested-With':'XMLHttpRequest','Referer':'https://ssl.wf.jobcan.jp/'},
                                body:body,credentials:'include'});
                            var text='';try{text=await r.text()}catch(e){}
                            var jd=null;try{jd=JSON.parse(text)}catch(e){}
                            return {status:r.status,text:text.substring(0,1000),json:jd};
                        }catch(e){return{error:e.message};}
                    }''', [f"{JOBCAN_WF_BASE}/api/v1/requests/new/", bs, csrf])
                    
                    st = resp.get('status', 0)
                    print(f"[API] {vname} → {st} | {resp.get('text','')[:200]}")
                    
                    attempt_results.append({
                        "v": vname, "http": st,
                        "resp": resp.get('json') or resp.get('text','')[:500],
                    })
                    
                    # 200/201 = success, stop
                    if st in (200, 201):
                        print(f"[API] ★★★ {vname} SUCCESS ★★★")
                        results.append({"row":idx,"status":"success","v":vname,"http":st,
                            "resp":resp.get('json') or resp.get('text','')[:500]})
                        break
                except Exception as ex:
                    attempt_results.append({"v":vname,"error":str(ex)[:200]})
            else:
                # None succeeded — return ALL results for diagnosis
                results.append({"row":idx,"status":"diagnosing","attempts":attempt_results})
    finally:
        await ctx.close()
    return {"results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
