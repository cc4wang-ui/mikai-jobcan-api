"""
mikai Jobcan Bridge — v7.0.0

v7 突破: 從 create_request_controllers.js 逆向取得正確 body 格式
- kind (0=下書き, 1=申請) 取代 is_draft
- form_data.form_items[] 取代 top-level form_json
- form_item_id/form_item_type/form_item_name/content 取代 id/item_type/item_name/request_content
- group 取代 group_id
- request_data_json 包含完整 form state
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
VERSION = "7.0.0"
JOBCAN_LOGIN_URL = "https://id.jobcan.jp/users/sign_in"
JOBCAN_WF_BASE = "https://ssl.wf.jobcan.jp"

FORM_666628 = {
    "form_id": 666628, "flow_id": 401080, "form_type": 1,
    "client": 53786, "group_id": 560177, "group_name": "Board",
    "currency_code": 392, "currency_show_flg": True,
}

_browser: Optional[Browser] = None

async def get_browser():
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
# Body builder — 從 AngularJS 源碼逆向確認的正確格式
# ══════════════════════════════════════════════════════════

# Checkbox labels (item_type 7)
CB_LABELS = {
    3831493: ["稟議", "事後稟議", "再稟議"],
    3818321: ["当社からの支払い（費用）", "取引先からの受取（売上）"],
    3818329: ["契約書", "発注書", "申込書", "利用規約合意"],
}

# Alias key → (field_id, item_name, item_type, input_name)
FIELD_MAP = {
    "ringi_type":       (3831493, "稟議の種類", 7, "form_item0"),
    "contract_date":    (3831494, "契約締結日", 4, "form_item1"),
    "content_type":     (3818321, "内容", 7, "form_item2"),
    "application_type": (3818329, "申請内容", 7, "form_item4"),
    "vendor_type":      (3818323, "取引先種別", 6, "form_item3"),
    "vendor_name":      (3822625, "取引先名", 1, "form_item0"),
    "vendor_website":   (3818337, "取引先ウェブサイト", 1, "form_item0"),
    "bank_info":        (3841064, "銀行情報", 2, "form_item0"),
    "tax_status":       (3841065, "課税事業者情報", 6, "form_item1"),
    "tax_number":       (3841066, "課税事業者番号", 1, "form_item2"),
    "project_name":     (3831525, "プロジェクトまたは予算項目名", 1, "form_item1"),
    "contract_purpose": (3831524, "契約書名・目的", 2, "form_item0"),
    "budget_method":    (4143713, "予算稟議の方法", 6, "form_item0"),
    "amount_range":     (3869371, "金額の範囲", 6, "form_item0"),
    "amount":           (3818325, "発注額", 3, "form_item0"),
    "payment_cycle":    (3818340, "支払サイクル", 6, "form_item3"),
    "antisocial":       (3818330, "反社チェック", 5, "form_item5"),
    "nda":              (3831551, "秘密保持契約書の締結", 6, "form_item0"),
    "basic_contract":   (3831552, "取引基本契約書", 6, "form_item1"),
    "competitor_quote": (3822626, "相見積もり", 5, "form_item1"),
    "signing_method":   (3818338, "締結方法", 5, "form_item1"),
    "legal_check":      (3831553, "リーガルチェック", 5, "form_item0"),
    "payment_method":   (3818341, "支払手段", 6, "form_item4"),
}

# Also support direct form_item keys
DIRECT_MAP = {}
for alias, (fid, name, itype, inp) in FIELD_MAP.items():
    DIRECT_MAP[f"form_item{fid}"] = (fid, name, itype, inp)

SELECT_LABELS = {
    3818323: "新規\n既存",
    3841065: "課税事業者\n免税事業者",
    4143713: "単独\n複数",
    3869371: "予算内\n500万円以上\n期間総予算の5%を超えるもの",
    3818340: "単発\n30日\n60日\n75日\nその他",
    3831551: "YES\nNO",
    3831552: "YES\nNO",
    3818341: "銀行振込\nクレジットカード\nPaypal\n紙付書\nその他",
    3818330: "上場企業(不要)\n非上場企業（反社チェック実施）",
    3822626: "未\n済",
    3818338: "電子契約\n書面契約（捺印）\n利用規約合意\nその他",
    3831553: "YES\nNO",
    3831493: "稟議\n事後稟議\n再稟議",
    3818321: "当社からの支払い（費用）\n取引先からの受取（売上）",
    3818329: "契約書\n発注書\n申込書\n利用規約合意",
}


def build_request_data(payload: dict, action: str) -> dict:
    """
    Build request_data matching Jobcan's AngularJS format exactly.
    Source: create_request_controllers.js → prepareRequestData() + prepareFormItemsData()
    """
    title = payload.get("_title", payload.get("title", "mikai自動申請"))
    FI = FORM_666628

    # ── Step 1: prepareFormItemsData() format ──
    # Keys: row_number, form_item_id, form_item_type, form_item_name, content
    form_items = []
    form_json_items = []  # for request_data_json

    for key, value in payload.items():
        if key.startswith('_'):
            continue
        value_str = str(value).strip()
        if not value_str:
            continue

        if key in FIELD_MAP:
            fid, name, itype, input_name = FIELD_MAP[key]
        elif key in DIRECT_MAP:
            fid, name, itype, input_name = DIRECT_MAP[key]
        else:
            continue

        # form_items entry (for form_data.form_items)
        fi = {
            "row_number": 1,
            "form_item_id": fid,
            "form_item_type": itype,
            "form_item_name": name,
            "content": value_str,
        }

        # For checkbox (type 7), add select_item_labels_obj
        if itype == 7 and fid in CB_LABELS:
            labels = CB_LABELS[fid]
            fi["select_item_labels_obj"] = [
                {"label": l, "checked": (l == value_str or value_str in l)}
                for l in labels
            ]

        form_items.append(fi)

        # form_json entry (for request_data_json.form_json)
        fj = {
            "id": fid,
            "input_name": input_name,
            "item_name": name,
            "item_type": itype,
            "request_content": value_str,
            "input_pattern": 1,
            "row_number": 1,
            "new_flg": 0,
        }
        if fid in SELECT_LABELS:
            fj["select_item_labels"] = SELECT_LABELS[fid]
        if itype == 7 and fid in CB_LABELS:
            labels = CB_LABELS[fid]
            fj["select_item_labels"] = "\n".join(labels)
            fj["is_required"] = True
        form_json_items.append(fj)

    # ── Step 2: prepareRequestData() format ──
    kind = 0 if action == "draft" else 1

    request_data = {
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
        # form_data with form_items (from prepareFormItemsData)
        "form_data": {
            "form_items": form_items,
            "request_form_custom_item": None,
        },
        # request_data_json (from _editRequestDataJson = full form state)
        "request_data_json": {
            "form_json": form_json_items,
            "title": title,
        },
        "total_amount": 0,
        "currency_code": FI["currency_code"],
        "currency_show_flg": FI["currency_show_flg"],
    }

    return request_data


# ══════════════════════════════════════════════════════════
# /api/fill
# ══════════════════════════════════════════════════════════
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
            title = payload.get("_title", payload.get("title", "mikai自動申請"))
            print(f"\n[FILL] #{idx+1}: {title}")

            # Build correct body
            body = build_request_data(payload, req.action)
            body_str = json.dumps(body, ensure_ascii=False)

            print(f"[API] form_items count: {len(body['form_data']['form_items'])}")
            print(f"[API] body preview: {body_str[:300]}...")

            # Try the correct format first, then variations
            attempts = [
                ("V1:correct", body_str),
            ]

            # V2: without request_data_json (maybe server doesn't need it)
            body_v2 = {k: v for k, v in body.items() if k != 'request_data_json'}
            attempts.append(("V2:no_rdj", json.dumps(body_v2, ensure_ascii=False)))

            # V3: without total_amount/currency (minimal)
            body_v3 = {
                "title": title,
                "kind": 0 if req.action == "draft" else 1,
                "form_type": FORM_666628["form_type"],
                "form_id": FORM_666628["form_id"],
                "group": FORM_666628["group_id"],
                "form_data": body["form_data"],
            }
            attempts.append(("V3:minimal", json.dumps(body_v3, ensure_ascii=False)))

            # V4: form_data.form_items at top level (flat)
            body_v4 = dict(body)
            body_v4["form_items"] = body["form_data"]["form_items"]
            del body_v4["form_data"]
            attempts.append(("V4:flat_items", json.dumps(body_v4, ensure_ascii=False)))

            attempt_results = []
            for vname, bs in attempts:
                try:
                    print(f"[API] Trying {vname}...")
                    resp = await page.evaluate('''async (args) => {
                        var url=args[0], body=args[1], csrf=args[2];
                        try {
                            var r = await fetch(url, {
                                method:'POST',
                                headers:{
                                    'Content-Type':'application/json',
                                    'X-CSRFToken':csrf,
                                    'X-Requested-With':'XMLHttpRequest',
                                    'Referer':'https://ssl.wf.jobcan.jp/'
                                },
                                body:body,
                                credentials:'include'
                            });
                            var text=''; try{text=await r.text()}catch(e){}
                            var jd=null; try{jd=JSON.parse(text)}catch(e){}
                            return {status:r.status, text:text.substring(0,1000), json:jd};
                        } catch(e){return {error:e.message};}
                    }''', [f"{JOBCAN_WF_BASE}/api/v1/requests/new/", bs, csrf])

                    st = resp.get('status', 0)
                    print(f"[API] {vname} → HTTP {st}")

                    # Log response detail for non-500
                    if st != 500:
                        print(f"[API] Response: {resp.get('text','')[:500]}")

                    attempt_results.append({
                        "variant": vname, "http": st,
                        "preview": resp.get('text','')[:500],
                        "json": resp.get('json'),
                    })

                    if st in (200, 201):
                        print(f"[API] ★★★ SUCCESS! {vname} → {st} ★★★")
                        results.append({
                            "row": idx, "status": "success",
                            "variant": vname, "http": st,
                            "resp": resp.get('json') or resp.get('text','')[:500],
                        })
                        break
                    elif st != 500:
                        print(f"[API] ★ {vname} → {st} (not 500 = progress!) ★")
                        results.append({
                            "row": idx, "status": "progress",
                            "variant": vname, "http": st,
                            "resp": resp.get('json') or resp.get('text','')[:500],
                        })
                        break

                except Exception as ex:
                    attempt_results.append({"variant": vname, "error": str(ex)[:200]})
            else:
                results.append({"row": idx, "status": "all_failed", "attempts": attempt_results})
    finally:
        await ctx.close()
    return {"results": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
