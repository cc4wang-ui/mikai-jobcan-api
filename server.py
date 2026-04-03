"""
mikai Jobcan Bridge v5 — Direct API Strategy
登入後直接 POST Jobcan REST API。不操作 DOM、不渲染表單。

API:
  POST /api/fill    — 登入 → POST Jobcan API → 回傳結果
  POST /api/recon   — 登入 → 回傳 cookie/token（診斷用）
  GET  /api/health  — ヘルスチェック
"""

import os, json, traceback
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

# ══════════════════════════════════════════════════════════
# 設定
# ══════════════════════════════════════════════════════════

API_KEY = os.environ.get('API_KEY', 'mikai-dev-key-change-me')
JOBCAN_LOGIN_URL = 'https://id.jobcan.jp/users/sign_in'
JOBCAN_WF_BASE = 'https://ssl.wf.jobcan.jp'

FLOW_TO_FORM = {'発注稟議': '666628', '支払依頼': '666591'}
FORM_META = {
    '666628': {'flow_id': 401080, 'group_id': 560177, 'group_name': 'Board'},
    '666591': {'flow_id': 0, 'group_id': 0, 'group_name': ''},  # 支払依頼 — 要確認
}

# ── 全欄位定義 ──
FIELD_DEFS = {
    'form_item3831493': {'id':'3831493','name':'稟議の種類','type':7,'options':['稟議','事後稟議','再稟議']},
    'form_item3831494': {'id':'3831494','name':'契約締結日','type':4},
    'form_item3818321': {'id':'3818321','name':'内容','type':7,'options':['当社からの支払い（費用）','取引先からの受取（売上）']},
    'form_item3818329': {'id':'3818329','name':'申請内容','type':7,'options':['契約書','発注書','申込書','利用規約合意']},
    'form_item3818323': {'id':'3818323','name':'取引先種別','type':6},
    'form_item3818324': {'id':'3818324','name':'発注先','type':9},
    'form_item3822625': {'id':'3822625','name':'取引先名','type':1},
    'form_item3818337': {'id':'3818337','name':'取引先ウェブサイト','type':1},
    'form_item3841064': {'id':'3841064','name':'銀行情報','type':2},
    'form_item3841065': {'id':'3841065','name':'課税事業者情報','type':6},
    'form_item3841066': {'id':'3841066','name':'課税事業者番号','type':1},
    'form_item3831525': {'id':'3831525','name':'プロジェクトまたは予算項目名','type':1},
    'form_item3831524': {'id':'3831524','name':'契約書名・目的','type':2},
    'form_item4143713': {'id':'4143713','name':'予算稟議の方法','type':6},
    'form_item3818322': {'id':'3818322','name':'予算申請','type':13},
    'form_item4143714': {'id':'4143714','name':'複数予算申請記載欄','type':2},
    'form_item3818328': {'id':'3818328','name':'予算関連備考','type':2},
    'form_item3869371': {'id':'3869371','name':'金額の範囲','type':6},
    'form_item3818325': {'id':'3818325','name':'発注額','type':3},
    'form_item3818340': {'id':'3818340','name':'支払サイクル','type':6},
    'form_item3818330': {'id':'3818330','name':'反社チェック','type':5},
    'form_item3818331': {'id':'3818331','name':'証券番号','type':1},
    'form_item3818332': {'id':'3818332','name':'反社チェック完了番号','type':1},
    'form_item3831551': {'id':'3831551','name':'秘密保持経書の締結','type':6},
    'form_item3831552': {'id':'3831552','name':'取引基本契約書','type':6},
    'form_item3822626': {'id':'3822626','name':'相見積もり','type':5},
    'form_item3818338': {'id':'3818338','name':'締結方法','type':5},
    'form_item3831553': {'id':'3831553','name':'リーガルチェック','type':5},
    'form_item3818339': {'id':'3818339','name':'リーガルチェックURL','type':1},
    'form_item3818341': {'id':'3818341','name':'支払手段','type':6},
}

# ══════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════

class FillPayload(BaseModel):
    payload: dict
    flow_type: str = '発注稟議'
    title: str = ''
    row_num: int = 0

class FillRequest(BaseModel):
    email: str
    password: str
    items: List[FillPayload]
    action: str = 'draft'

class FillResult(BaseModel):
    row_num: int
    title: str
    status: str
    filled: int
    errors: List[str]
    message: str
    debug: Optional[dict] = None

# ══════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════

app = FastAPI(title='mikai Jobcan Bridge', version='5.0.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True,
                   allow_methods=['*'], allow_headers=['*'])

def verify_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail='Invalid API key')

@app.get('/api/health')
async def health():
    return {'status': 'ok', 'version': '5.0.0', 'strategy': 'direct_api_post'}

# ══════════════════════════════════════════════════════════
# /api/recon — 偵察
# ══════════════════════════════════════════════════════════

@app.post('/api/recon')
async def recon(req: FillRequest, x_api_key: Optional[str] = Header(None)):
    verify_key(x_api_key)
    async with async_playwright() as p:
        browser = await launch_browser(p)
        page = await create_page(browser)
        lr = await do_login(page, req.email, req.password)
        if not lr['ok']:
            await browser.close()
            return {'error': lr['reason']}

        cookies = await page.context.cookies()
        cookie_list = [{'name':c['name'],'value':c['value'][:80],'domain':c['domain']} for c in cookies]
        doc_cookie = await page.evaluate('document.cookie')

        # API テスト: GET /wf/api/ を試す
        api_test = await page.evaluate('''async () => {
            try {
                var r = await fetch("/wf/api/", {credentials:"same-origin"});
                var t = await r.text();
                return {status:r.status, body:t.substring(0,500)};
            } catch(e) { return {error:e.message}; }
        }''')

        await browser.close()
        return {'login':'ok','cookies':cookie_list,'document_cookie':doc_cookie[:500],'api_test':api_test}

# ══════════════════════════════════════════════════════════
# /api/fill — メイン
# ══════════════════════════════════════════════════════════

@app.post('/api/fill')
async def fill_jobcan(req: FillRequest, x_api_key: Optional[str] = Header(None)):
    verify_key(x_api_key)
    if not req.items:
        raise HTTPException(status_code=400, detail='items が空です')

    results = []
    try:
        async with async_playwright() as p:
            browser = await launch_browser(p)
            page = await create_page(browser)

            # Login
            lr = await do_login(page, req.email, req.password)
            if not lr['ok']:
                await browser.close()
                return JSONResponse(status_code=401, content={'error': lr['reason']})
            print('[MAIN] Login OK')

            # CSRF tokens
            tokens = await extract_tokens(page)
            print(f'[MAIN] XSRF={bool(tokens["xsrf"])} CSRF={bool(tokens["csrf"])}')

            # Submit each item
            for i, item in enumerate(req.items):
                print(f'[MAIN] Item {i+1}/{len(req.items)}: {item.title}')
                r = await submit_item(page, item, tokens, req.action)
                results.append(r)
                print(f'[MAIN] -> {r.status}: {r.message[:80]}')

            await browser.close()
    except Exception as e:
        print(f'[MAIN] Fatal: {traceback.format_exc()}')
        return JSONResponse(status_code=500, content={'error': str(e)[:200]})

    return {'results': [r.model_dump() for r in results]}

# ══════════════════════════════════════════════════════════
# ブラウザ
# ══════════════════════════════════════════════════════════

async def launch_browser(p):
    return await p.chromium.launch(headless=True, args=[
        '--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
        '--disable-gpu','--disable-blink-features=AutomationControlled'])

async def create_page(browser):
    ctx = await browser.new_context(
        viewport={'width':1280,'height':900}, locale='ja-JP',
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
    await ctx.add_init_script('Object.defineProperty(navigator,"webdriver",{get:()=>undefined})')
    return await ctx.new_page()

# ══════════════════════════════════════════════════════════
# Login（検証済み — v2 以降変更なし）
# ══════════════════════════════════════════════════════════

async def do_login(page, email: str, password: str) -> dict:
    try:
        await page.goto(JOBCAN_LOGIN_URL, wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        return {'ok':False,'reason':f'ログインページ接続失敗: {str(e)[:100]}'}

    url = page.url
    if 'wf.jobcan.jp' in url:
        return {'ok':True}
    if 'id.jobcan.jp' in url and 'sign_in' not in url:
        return await _goto_wf(page)
    if not await page.query_selector('#user_email'):
        return {'ok':False,'reason':f'ログインフォーム不明: {url}'}

    try:
        await page.fill('#user_email', email)
        await page.fill('#user_password', password)
        await page.wait_for_timeout(500)
        await page.click('[name="commit"]')
    except Exception as e:
        return {'ok':False,'reason':f'入力エラー: {str(e)[:80]}'}

    for _ in range(20):
        await page.wait_for_timeout(1000)
        if 'sign_in' not in page.url:
            break
    else:
        return {'ok':False,'reason':'ログイン失敗（タイムアウト）'}

    return await _goto_wf(page)

async def _goto_wf(page) -> dict:
    try:
        await page.goto(JOBCAN_WF_BASE + '/', wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(5000)
    except Exception as e:
        return {'ok':False,'reason':f'WF接続失敗: {str(e)[:100]}'}
    if 'sign_in' in page.url:
        return {'ok':False,'reason':'セッション無効'}
    print(f'[LOGIN] OK at {page.url}')
    return {'ok':True}

# ══════════════════════════════════════════════════════════
# CSRF Token 取得
# ══════════════════════════════════════════════════════════

async def extract_tokens(page) -> dict:
    """cookie と meta tag から CSRF token を取得"""
    xsrf = ''
    csrf = ''
    all_cookies = []

    # Method 1: Playwright context.cookies()
    cookies = await page.context.cookies()
    for c in cookies:
        all_cookies.append(f'{c["name"]}={c["value"][:30]}...')
        if c['name'] == 'XSRF-TOKEN':
            xsrf = c['value']
        elif c['name'] == 'csrftoken' or c['name'] == 'csrf_token':
            csrf = c['value']

    # Method 2: document.cookie（context.cookies で取れない場合の fallback）
    if not xsrf:
        doc_cookie = await page.evaluate('document.cookie')
        for part in doc_cookie.split(';'):
            part = part.strip()
            if part.startswith('XSRF-TOKEN='):
                xsrf = part.split('=', 1)[1]
            elif part.startswith('csrftoken='):
                csrf = part.split('=', 1)[1]

    # Method 3: meta tag
    if not csrf:
        csrf = await page.evaluate('''() => {
            var el = document.querySelector('meta[name="csrf-token"]')
                  || document.querySelector('meta[name="_csrf"]');
            return el ? el.content : "";
        }''')

    # CSRF が見つからなければ XSRF を使う（Jobcan は XSRF-TOKEN で統一の可能性）
    if not csrf:
        csrf = xsrf

    print(f'[TOKEN] XSRF: {xsrf[:30] if xsrf else "NONE"} | CSRF: {csrf[:30] if csrf else "NONE"}')
    print(f'[TOKEN] All cookies: {", ".join(all_cookies[:10])}')

    return {'xsrf': xsrf, 'csrf': csrf, 'all_cookies': all_cookies}

# ══════════════════════════════════════════════════════════
# form_json 構築
# ══════════════════════════════════════════════════════════

def build_form_json(payload: dict) -> list:
    """
    AD 欄の payload → Jobcan API の form_json 配列に変換。
    各 item_type に応じた正しい構造を生成。
    """
    form_items = []

    for input_name, value in payload.items():
        if input_name.startswith('_'):
            continue
        value = str(value).strip()
        if not value:
            continue

        fdef = FIELD_DEFS.get(input_name)
        if not fdef:
            continue

        item = {
            'id': int(fdef['id']),
            'input_name': input_name,
            'item_name': fdef['name'],
            'item_type': fdef['type'],
            'request_content': value,
        }

        # Type 7: Checkbox — 需要 select_item_labels + select_item_labels_obj
        if fdef['type'] == 7 and 'options' in fdef:
            checked_values = [v.strip() for v in value.split(',')]
            item['select_item_labels'] = fdef['options']
            item['select_item_labels_obj'] = [
                {'label': opt, 'checked': opt in checked_values}
                for opt in fdef['options']
            ]

        # Type 13: Special — skip
        if fdef['type'] == 13:
            continue

        form_items.append(item)

    return form_items

# ══════════════════════════════════════════════════════════
# API 送信
# ══════════════════════════════════════════════════════════

async def submit_item(page, item: FillPayload, tokens: dict, action: str) -> FillResult:
    """Jobcan REST API に直接 POST"""
    try:
        form_id = FLOW_TO_FORM.get(item.flow_type, '666628')
        meta = FORM_META.get(form_id, FORM_META['666628'])

        # form_json 構築
        form_json = build_form_json(item.payload)
        filled_count = len(form_json)

        if filled_count == 0:
            return FillResult(row_num=item.row_num, title=item.title, status='error',
                filled=0, errors=['入力データなし'], message='payload にフィールドがありません。')

        # API リクエスト body
        body = {
            'form_id': int(form_id),
            'form_json': form_json,
        }

        # flow_id, group_id がある場合は追加
        if meta['flow_id']:
            body['flow_id'] = meta['flow_id']
        if meta['group_id']:
            body['group_id'] = meta['group_id']
            body['group_name'] = meta['group_name']

        # 下書き保存 vs 申請
        if action == 'draft':
            body['is_draft'] = True

        body_json = json.dumps(body, ensure_ascii=False)
        print(f'[API] POST body ({len(body_json)} chars): {body_json[:300]}...')

        # ブラウザ内部から fetch() で POST（cookie 自動送信、DNS 問題なし）
        xsrf = tokens.get('xsrf', '')
        csrf = tokens.get('csrf', '')

        result = await page.evaluate('''async (args) => {
            var body = args[0];
            var xsrf = args[1];
            var csrf = args[2];

            var headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": location.href
            };
            if (xsrf) headers["X-XSRF-TOKEN"] = xsrf;
            if (csrf) headers["X-CSRFToken"] = csrf;

            try {
                var resp = await fetch("/wf/api/requests/new/", {
                    method: "POST",
                    headers: headers,
                    credentials: "same-origin",
                    body: body
                });

                var respText = await resp.text();
                var respJson = null;
                try { respJson = JSON.parse(respText); } catch(e) {}

                return {
                    ok: resp.ok,
                    status: resp.status,
                    statusText: resp.statusText,
                    body: respText.substring(0, 2000),
                    json: respJson
                };
            } catch(e) {
                return { ok: false, status: 0, error: e.message };
            }
        }''', [body_json, xsrf, csrf])

        print(f'[API] Response: {result.get("status")} | {str(result.get("body",""))[:200]}')

        if result.get('ok'):
            # 成功
            resp_json = result.get('json', {})
            request_id = ''
            if resp_json:
                request_id = str(resp_json.get('id', resp_json.get('request_id', '')))
            action_label = '下書き保存' if action == 'draft' else '申請'
            msg = f'{action_label}しました。'
            if request_id:
                msg += f' 申請番号: {request_id}'
            return FillResult(row_num=item.row_num, title=item.title, status='success',
                filled=filled_count, errors=[], message=msg)
        else:
            # 失敗
            status = result.get('status', 0)
            err_body = result.get('body', result.get('error', 'unknown'))
            return FillResult(row_num=item.row_num, title=item.title, status='error',
                filled=filled_count, errors=[f'API {status}: {err_body[:200]}'],
                message=f'Jobcan API エラー (HTTP {status})',
                debug={'response': result})

    except Exception as e:
        print(f'[API] Exception: {traceback.format_exc()}')
        return FillResult(row_num=item.row_num, title=item.title, status='error',
            filled=0, errors=[str(e)[:100]], message=f'エラー: {str(e)[:100]}')

# ══════════════════════════════════════════════════════════
# Entry
# ══════════════════════════════════════════════════════════

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8080))
    print(f'[BOOT] mikai Jobcan Bridge v5.0.0 on port {port}')
    uvicorn.run(app, host='0.0.0.0', port=port)
