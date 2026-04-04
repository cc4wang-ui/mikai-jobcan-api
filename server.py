"""
mikai Jobcan Bridge — v7.2.0

v7.0: body 結構對了 (500→400)
v7.1: 診斷出缺少 flow_data/circulation_data/request_files
v7.2: 動態從 Jobcan API 取得 flow_data，組裝完整 body
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
VERSION = "7.2.0"
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
    if x_api_key != API_KEY: raise HTTPException(status_code=403)

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION}


# ── Field map ──
CB = {3831493:["稟議","事後稟議","再稟議"],3818321:["当社からの支払い（費用）","取引先からの受取（売上）"],3818329:["契約書","発注書","申込書","利用規約合意"]}
FMAP = {
    "ringi_type":(3831493,"稟議の種類",7),"contract_date":(3831494,"契約締結日",4),
    "content_type":(3818321,"内容",7),"application_type":(3818329,"申請内容",7),
    "vendor_type":(3818323,"取引先種別",6),"vendor_name":(3822625,"取引先名",1),
    "vendor_website":(3818337,"取引先ウェブサイト",1),"bank_info":(3841064,"銀行情報",2),
    "tax_status":(3841065,"課税事業者情報",6),"tax_number":(3841066,"課税事業者番号",1),
    "project_name":(3831525,"プロジェクトまたは予算項目名",1),
    "contract_purpose":(3831524,"契約書名・目的",2),
    "budget_method":(4143713,"予算稟議の方法",6),
    "amount_range":(3869371,"金額の範囲",6),"amount":(3818325,"発注額",3),
    "payment_cycle":(3818340,"支払サイクル",6),"antisocial":(3818330,"反社チェック",5),
    "nda":(3831551,"秘密保持契約書の締結",6),"basic_contract":(3831552,"取引基本契約書",6),
    "competitor_quote":(3822626,"相見積もり",5),"signing_method":(3818338,"締結方法",5),
    "legal_check":(3831553,"リーガルチェック",5),"payment_method":(3818341,"支払手段",6),
}
DMAP = {f"form_item{fid}":(fid,n,t) for _,(fid,n,t) in FMAP.items()}
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

def build_form_items(payload):
    items = []
    for k,v in payload.items():
        if k.startswith('_'): continue
        v = str(v).strip()
        if not v: continue
        if k in FMAP: fid,name,itype = FMAP[k]
        elif k in DMAP: fid,name,itype = DMAP[k]
        else: continue
        fi = {"row_number":1,"form_item_id":fid,"form_item_type":itype,"form_item_name":name,"content":v}
        if itype==7 and fid in CB:
            fi["select_item_labels_obj"]=[{"label":l,"checked":(l==v or v in l)} for l in CB[fid]]
        items.append(fi)
    return items

def build_form_json_raw(payload):
    items = []
    for k,v in payload.items():
        if k.startswith('_'): continue
        v = str(v).strip()
        if not v: continue
        if k in FMAP: fid,name,itype = FMAP[k]
        elif k in DMAP: fid,name,itype = DMAP[k]
        else: continue
        fj = {"id":fid,"item_name":name,"item_type":itype,"request_content":v,"input_pattern":1,"row_number":1,"new_flg":0}
        if fid in SL: fj["select_item_labels"]=SL[fid]
        if itype==7 and fid in CB:
            fj["is_required"]=True
            fj["select_item_labels_obj"]=[{"label":l,"checked":(l==v or v in l)} for l in CB[fid]]
        items.append(fj)
    return items


async def fetch_flow_data(page, csrf, flow_id, form_id, kind=0):
    """Fetch flow data from Jobcan API (mimics FlowService.getForNewRequest)"""
    # Try multiple possible flow API endpoints
    endpoints = [
        f"/api/v1/flows/{flow_id}/?form_id={form_id}&kind={kind}&request_user_id=1111126",
        f"/api/v1/flows/{flow_id}/",
        f"/api/v1/flows/new/?flow_id={flow_id}&form_id={form_id}",
        f"/api/v1/flow_step_users/?flow_id={flow_id}&form_id={form_id}",
    ]
    
    for ep in endpoints:
        try:
            resp = await page.evaluate(f'''async () => {{
                try {{
                    const r = await fetch("{JOBCAN_WF_BASE}{ep}", {{
                        headers: {{'X-CSRFToken': '{csrf}', 'X-Requested-With': 'XMLHttpRequest'}},
                        credentials: 'include'
                    }});
                    const text = await r.text();
                    let json = null;
                    try {{ json = JSON.parse(text); }} catch(e) {{}}
                    return {{status: r.status, json: json, text: text.substring(0, 500)}};
                }} catch(e) {{ return {{error: e.message}}; }}
            }}''')
            
            st = resp.get('status', 0)
            print(f"[FLOW] {ep} → {st}")
            
            if st == 200 and resp.get('json'):
                return resp['json']
        except Exception as e:
            print(f"[FLOW] {ep} → error: {str(e)[:100]}")
    
    return None


@app.post("/api/fill")
async def fill(req: FillRequest, x_api_key: str = Header(None)):
    check_api_key(x_api_key)
    ctx, tokens = await login_jobcan(req.email, req.password)
    page = tokens["page"]
    csrf = tokens["csrf"]
    print(f"[LOGIN] OK | CSRF={csrf}")

    results = []
    try:
        # Step 1: Fetch flow data
        FI = FORM_666628
        kind = 0 if req.action == "draft" else 1
        
        print("[FLOW] Fetching flow data...")
        flow_data = await fetch_flow_data(page, csrf, FI["flow_id"], FI["form_id"], kind)
        
        if flow_data:
            print(f"[FLOW] Got flow_data: {json.dumps(flow_data, ensure_ascii=False)[:300]}")
        else:
            print("[FLOW] Could not fetch flow_data, will try without")
        
        for idx, item in enumerate(req.items):
            payload = item.get("payload", {})
            title = payload.get("_title", "mikai自動申請")
            form_items = build_form_items(payload)
            form_json_raw = build_form_json_raw(payload)
            
            print(f"\n[FILL] #{idx+1}: {title} ({len(form_items)} items)")

            # Build complete body
            body = {
                "title": title,
                "kind": kind,
                "form_type": FI["form_type"],
                "edit_flow_flg": False,
                "project": None,
                "project_name": None,
                "project_code": None,
                "group": FI["group_id"],
                "group_name": FI["group_name"],
                "group_code": "",
                "requester_group_id": None,
                "requester_position_id": None,
                "form_id": FI["form_id"],
                "form_data": {
                    "form_items": form_items,
                    "request_form_custom_item": None,
                },
                "request_data_json": {
                    "form_json": form_json_raw,
                    "title": title,
                },
                "total_amount": 0,
                "currency_code": 392,
                "currency_show_flg": True,
                # ★ v7.2: 新增的欄位
                "flow_data": flow_data if flow_data else {},
                "circulation_data": [],
                "last_approval_period": None,
                "request_files": [],
            }
            
            # Try variations
            variations = []
            
            # V1: full body with flow_data
            variations.append(("V1:full", body))
            
            # V2: flow_data as flow steps array (if flow_data is a dict with steps)
            if flow_data and isinstance(flow_data, dict):
                v2 = dict(body)
                # Maybe flow_data needs to include flow_id
                if 'flow_steps' not in flow_data and 'id' in flow_data:
                    v2["flow_data"] = flow_data
                variations.append(("V2:flow_raw", v2))
            
            # V3: without flow_data (empty object)
            v3 = dict(body)
            v3["flow_data"] = {}
            variations.append(("V3:flow_empty", v3))
            
            # V4: without flow_data (null)
            v4 = dict(body)
            v4["flow_data"] = None
            variations.append(("V4:flow_null", v4))
            
            # V5: stringified request_data_json
            v5 = dict(body)
            v5["request_data_json"] = json.dumps(body["request_data_json"], ensure_ascii=False)
            variations.append(("V5:rdj_str", v5))

            attempt_results = []
            for vname, vbody in variations:
                try:
                    bs = json.dumps(vbody, ensure_ascii=False)
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
                    
                    st = resp.get('status',0)
                    print(f"[API] {vname} → {st}")
                    if st != 500:
                        print(f"[API] Response: {resp.get('text','')[:300]}")
                    
                    attempt_results.append({
                        "v":vname,"http":st,
                        "resp":resp.get('json') or resp.get('text','')[:500]
                    })
                    
                    if st in (200,201):
                        print(f"[API] ★★★ SUCCESS ★★★")
                        results.append({"row":idx,"status":"success","v":vname,"http":st,
                            "resp":resp.get('json') or resp.get('text','')[:500]})
                        break
                except Exception as ex:
                    attempt_results.append({"v":vname,"error":str(ex)[:200]})
            else:
                results.append({
                    "row":idx,"status":"diagnosing",
                    "flow_data_fetched": flow_data is not None,
                    "flow_data_preview": json.dumps(flow_data, ensure_ascii=False)[:500] if flow_data else None,
                    "attempts":attempt_results
                })
    finally:
        await ctx.close()
    return {"results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
