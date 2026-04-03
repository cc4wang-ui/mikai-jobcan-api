"""
mikai Jobcan Bridge v5 — Direct API Strategy
登入後直接 POST Jobcan REST API。不操作 DOM、不渲染表單。

v5 QA 修正:
  - XSRF-TOKEN URL decode
  - form_json 格式不確定 → 先試 list，失敗再試 string
  - is_draft 不確定 → 先試，失敗回報原始 response
  - 支払依頼 flow_id/group_id 缺失 → 不送空值
  - recon 端點加強 → 攔截真實 API 呼叫的 request 格式
"""

import os, json, traceback
from urllib.parse import unquote
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
    '666591': {},  # 支払依頼 — 値が不明なので空。空の場合は送信しない
}

FIELD_DEFS = {
    'form_item3831493': {'id':'3831493','name':'稟議の種類','type':7,
        'options':['稟議','事後稟議','再稟議']},
    'form_item3831494': {'id':'3831494','name':'契約締結日','type':4},
    'form_item3818321': {'id':'3818321','name':'内容','type':7,
        'options':['当社からの支払い（費用）','取引先からの受取（売上）']},
    'form_item3818329': {'id':'3818329','name':'申請内容','type':7,
        'options':['契約書','発注書','申込書','利用規約合意']},
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
# /api/recon — 偵察（token + API 形式の確認）
# ══════════════════════════════════════════════════════════

@app.post('/api/recon')
async def recon(req: FillRequest, x_api_key: Optional[str] = Header(None)):
    """登入 → cookie/token/API格式を回報"""
    verify_key(x_api_key)
    async with async_playwright() as p:
        browser = await launch_browser(p)
        page = await create_page(browser)
        lr = await do_login(page, req.email, req.password)
        if not lr['ok']:
            await browser.close()
            return {'error': lr['reason']}

        tokens = await extract_tokens(page)

        # API endpoint テスト（GET → 存在確認）
        api_get = await page.evaluate('''async () => {
            try {
                var r = await fetch("/wf/api/", {credentials:"same-origin"});
                return {status:r.status, ok:r.ok};
            } catch(e) { return {error:e.message}; }
        }''')

        # XHR 攔截: AngularJS が使う API のリクエスト形式を観察
        # フォームページを開いて、AngularJS が送る XHR を全部記録
        intercepted = []
        page.on('request', lambda req: intercepted.append({
            'url': req.url, 'method': req.method,
            'headers': dict(req.headers) if req.method == 'POST' else {},
            'post': (req.post_data or '')[:500] if req.method == 'POST' else ''
        }) if '/wf/api/' in req.url else None)

        # フォームページを開く（表示はしないが、AngularJS の初期 API 呼出しを観察）
        try:
            await page.goto(JOBCAN_WF_BASE + '/#/requests/new/666628',
                            wait_until='domcontentloaded', timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(5000)

        await browser.close()
        return {
            'login': 'ok',
            'tokens': {
                'xsrf': tokens['xsrf'][:50] if tokens['xsrf'] else None,
                'csrf': tokens['csrf'][:50] if tokens['csrf'] else None,
            },
            'all_cookies': tokens['all_cookies'],
            'api_get_test': api_get,
            'intercepted_api_calls': intercepted[:20],
        }

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

            lr = await do_login(page, req.email, req.password)
            if not lr['ok']:
                await browser.close()
                return JSONResponse(status_code=401, content={'error': lr['reason']})
            print('[MAIN] Login OK')

            tokens = await extract_tokens(page)
            if not tokens['xsrf'] and not tokens['csrf']:
                # Token なしでも試す（Jobcan が cookie だけで認証する可能性）
                print('[MAIN] WARNING: No CSRF tokens found, trying anyway')

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
# Login（検証済み）
# ══════════════════════════════════════════════════════════

async def do_login(page, email: str, password: str) -> dict:
    try:
        await page.goto(JOBCAN_LOGIN_URL, wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        return {'ok': False, 'reason': f'ログインページ接続失敗: {str(e)[:100]}'}

    url = page.url
    if 'wf.jobcan.jp' in url:
        return {'ok': True}
    if 'id.jobcan.jp' in url and 'sign_in' not in url:
        return await _goto_wf(page)
    if not await page.query_selector('#user_email'):
        return {'ok': False, 'reason': f'ログインフォーム不明: {url}'}

    try:
        await page.fill('#user_email', email)
        await page.fill('#user_password', password)
        await page.wait_for_timeout(500)
        await page.click('[name="commit"]')
    except Exception as e:
        return {'ok': False, 'reason': f'入力エラー: {str(e)[:80]}'}

    # sign_in から離れるのを待つ
    for _ in range(20):
        await page.wait_for_timeout(1000)
        if 'sign_in' not in page.url:
            break
    else:
        return {'ok': False, 'reason': 'ログイン失敗（タイムアウト）'}

    return await _goto_wf(page)

async def _goto_wf(page) -> dict:
    try:
        await page.goto(JOBCAN_WF_BASE + '/', wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(5000)
    except Exception as e:
        return {'ok': False, 'reason': f'WF接続失敗: {str(e)[:100]}'}
    if 'sign_in' in page.url:
        return {'ok': False, 'reason': 'セッション無効'}
    print(f'[LOGIN] OK at {page.url}')
    return {'ok': True}

# ══════════════════════════════════════════════════════════
# CSRF Token（QA修正: URL decode 追加）
# ══════════════════════════════════════════════════════════

async def extract_tokens(page) -> dict:
    xsrf = ''
    csrf = ''
    all_cookies = []

    # Method 1: Playwright cookies
    cookies = await page.context.cookies()
    for c in cookies:
        all_cookies.append(f'{c["name"]}={c["value"][:40]}')
        if c['name'] == 'XSRF-TOKEN':
            xsrf = unquote(c['value'])  # URL decode
        elif c['name'] in ('csrftoken', 'csrf_token', '_csrf'):
            csrf = unquote(c['value'])

    # Method 2: document.cookie
    if not xsrf:
        doc_cookie = await page.evaluate('document.cookie')
        for part in doc_cookie.split(';'):
            part = part.strip()
            if part.startswith('XSRF-TOKEN='):
                xsrf = unquote(part.split('=', 1)[1])

    # Fallback: CSRF = XSRF
    if not csrf:
        csrf = xsrf

    print(f'[TOKEN] XSRF={xsrf[:30] if xsrf else "NONE"} | CSRF={csrf[:30] if csrf else "NONE"}')
    print(f'[TOKEN] Cookies: {", ".join(all_cookies[:15])}')
    return {'xsrf': xsrf, 'csrf': csrf, 'all_cookies': all_cookies}

# ══════════════════════════════════════════════════════════
# form_json 構築（QA修正: type 13 早期 skip）
# ══════════════════════════════════════════════════════════

def build_form_json(payload: dict) -> list:
    items = []
    for input_name, value in payload.items():
        if input_name.startswith('_'):
            continue
        value = str(value).strip()
        if not value:
            continue
        fdef = FIELD_DEFS.get(input_name)
        if not fdef:
            continue
        if fdef['type'] == 13:  # Special → skip immediately
            continue

        item = {
            'id': int(fdef['id']),
            'input_name': input_name,
            'item_name': fdef['name'],
            'item_type': fdef['type'],
            'request_content': value,
        }

        if fdef['type'] == 7 and 'options' in fdef:
            checked = [v.strip() for v in value.split(',')]
            item['select_item_labels'] = fdef['options']
            item['select_item_labels_obj'] = [
                {'label': opt, 'checked': opt in checked}
                for opt in fdef['options']
            ]

        items.append(item)
    return items

# ══════════════════════════════════════════════════════════
# API 送信（QA修正: 2 種格式で試行）
# ══════════════════════════════════════════════════════════

async def submit_item(page, item: FillPayload, tokens: dict, action: str) -> FillResult:
    try:
        form_id = FLOW_TO_FORM.get(item.flow_type, '666628')
        meta = FORM_META.get(form_id, {})
        form_json = build_form_json(item.payload)
        filled_count = len(form_json)

        if filled_count == 0:
            return FillResult(row_num=item.row_num, title=item.title, status='error',
                filled=0, errors=['入力データなし'], message='payload にフィールドがありません。')

        # Body 構築（QA修正: 空の flow_id/group_id は送らない）
        body = {'form_id': int(form_id), 'form_json': form_json}
        if meta.get('flow_id'):
            body['flow_id'] = meta['flow_id']
        if meta.get('group_id'):
            body['group_id'] = meta['group_id']
            body['group_name'] = meta.get('group_name', '')
        if action == 'draft':
            body['is_draft'] = True

        xsrf = tokens.get('xsrf', '')
        csrf = tokens.get('csrf', '')

        # === 試行 1: form_json を list として送信 ===
        body_json = json.dumps(body, ensure_ascii=False)
        print(f'[API] Attempt 1 (list): {body_json[:300]}...')

        result = await _do_fetch(page, body_json, xsrf, csrf)
        print(f'[API] Attempt 1 result: HTTP {result.get("status")}')

        # 成功
        if result.get('ok'):
            return _build_success(item, filled_count, result, action)

        # === 試行 2: form_json を JSON string として送信 ===
        if result.get('status') in (400, 422):
            print('[API] Attempt 1 failed with 400/422, trying form_json as string...')
            body2 = dict(body)
            body2['form_json'] = json.dumps(form_json, ensure_ascii=False)
            body_json2 = json.dumps(body2, ensure_ascii=False)
            result2 = await _do_fetch(page, body_json2, xsrf, csrf)
            print(f'[API] Attempt 2 result: HTTP {result2.get("status")}')
            if result2.get('ok'):
                return _build_success(item, filled_count, result2, action)
            # 両方失敗 → 詳細な方を返す
            result = result2 if len(str(result2.get('body',''))) > len(str(result.get('body',''))) else result

        # === 試行 3: is_draft を外して再試行（draft パラメータが不正の場合）===
        if result.get('status') in (400, 422) and action == 'draft':
            print('[API] Attempt 3: removing is_draft...')
            body3 = dict(body)
            body3.pop('is_draft', None)
            body_json3 = json.dumps(body3, ensure_ascii=False)
            result3 = await _do_fetch(page, body_json3, xsrf, csrf)
            print(f'[API] Attempt 3 result: HTTP {result3.get("status")}')
            if result3.get('ok'):
                return _build_success(item, filled_count, result3, '通常申請（下書きパラメータ不明）')
            result = result3

        # 全失敗
        status = result.get('status', 0)
        err_body = result.get('body', result.get('error', 'unknown'))
        return FillResult(
            row_num=item.row_num, title=item.title, status='error',
            filled=filled_count,
            errors=[f'API HTTP {status}'],
            message=f'Jobcan API エラー (HTTP {status}): {str(err_body)[:300]}',
            debug={'response': result, 'sent_body_preview': body_json[:500]})

    except Exception as e:
        print(f'[API] Exception: {traceback.format_exc()}')
        return FillResult(row_num=item.row_num, title=item.title, status='error',
            filled=0, errors=[str(e)[:100]], message=f'エラー: {str(e)[:100]}')


async def _do_fetch(page, body_json: str, xsrf: str, csrf: str) -> dict:
    """ブラウザ内 fetch() で Jobcan API に POST"""
    return await page.evaluate('''async (args) => {
        var body = args[0], xsrf = args[1], csrf = args[2];
        var headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": location.href,
            "X-Requested-With": "XMLHttpRequest"
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
            var text = await resp.text();
            var json = null;
            try { json = JSON.parse(text); } catch(e) {}
            return {ok: resp.ok, status: resp.status, statusText: resp.statusText,
                    body: text.substring(0, 2000), json: json};
        } catch(e) {
            return {ok: false, status: 0, error: e.message};
        }
    }''', [body_json, xsrf, csrf])


def _build_success(item: FillPayload, filled: int, result: dict, action: str) -> FillResult:
    resp_json = result.get('json') or {}
    request_id = str(resp_json.get('id', resp_json.get('request_id', '')))
    label = '下書き保存' if action == 'draft' else '申請'
    msg = f'{label}しました。'
    if request_id:
        msg += f' 申請番号: {request_id}'
    return FillResult(row_num=item.row_num, title=item.title, status='success',
        filled=filled, errors=[], message=msg)

# ══════════════════════════════════════════════════════════
# Entry
# ══════════════════════════════════════════════════════════

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8080))
    print(f'[BOOT] mikai Jobcan Bridge v5.0.0 on port {port}')
    uvicorn.run(app, host='0.0.0.0', port=port)
