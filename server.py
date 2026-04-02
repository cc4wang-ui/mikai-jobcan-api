"""
mikai Jobcan 自動填入 — Railway Backend v2
FastAPI + Playwright (Headless Chromium)

修正履歴:
  v1 → v2: wf.jobcan.jp → ssl.wf.jobcan.jp (DNS解決問題修正)
           ログインフロー修正 (id.jobcan.jp/account/profile 対応)
           全 timeout 明示化、エラーハンドリング強化

API:
  POST /api/fill   — Jobcan ログイン → 自動填入 → 下書き保存
  GET  /api/health — ヘルスチェック
"""

import os
import json
import traceback
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

# ══════════════════════════════════════════════════════════
# 設定
# CRITICAL: ssl.wf.jobcan.jp を使用（wf.jobcan.jp は Railway DNS で解決不可）
# ══════════════════════════════════════════════════════════

API_KEY = os.environ.get('API_KEY', 'mikai-dev-key-change-me')

JOBCAN_LOGIN_URL = 'https://id.jobcan.jp/users/sign_in'
JOBCAN_WF_BASE = 'https://ssl.wf.jobcan.jp'

JOBCAN_FORM_URLS = {
    '発注稟議': JOBCAN_WF_BASE + '/#/requests/new/666628',
    '支払依頼': JOBCAN_WF_BASE + '/#/requests/new/666591',
}

FIELD_LABELS = {
    'form_item3831493': '稟議の種類',
    'form_item3831494': '契約締結日',
    'form_item3818321': '内容',
    'form_item3818329': '申請内容',
    'form_item3818323': '取引先種別',
    'form_item3818324': '発注先',
    'form_item3822625': '取引先名',
    'form_item3818337': '取引先ウェブサイト',
    'form_item3841064': '銀行情報',
    'form_item3841065': '課税事業者情報',
    'form_item3841066': '課税事業者番号',
    'form_item3831525': 'プロジェクト/予算項目名',
    'form_item3831524': '契約書名・目的',
    'form_item4143713': '予算稟議の方法',
    'form_item3818322': '予算申請',
    'form_item4143714': '複数予算申請記載欄',
    'form_item3818328': '予算関連備考',
    'form_item3869371': '金額の範囲',
    'form_item3818325': '発注額',
    'form_item3818340': '支払サイクル',
    'form_item3818330': '反社チェック',
    'form_item3818331': '証券番号',
    'form_item3818332': '反社チェック完了番号',
    'form_item3831551': '秘密保持契約書',
    'form_item3831552': '取引基本契約書',
    'form_item3822626': '相見積もり',
    'form_item3818338': '締結方法',
    'form_item3831553': 'リーガルチェック',
    'form_item3818339': 'リーガルチェックURL',
    'form_item3818341': '支払手段',
}

FIELD_TYPES = {
    'form_item3831493': 7,
    'form_item3831494': 4,
    'form_item3818321': 7,
    'form_item3818329': 7,
    'form_item3818323': 6,
    'form_item3818324': 9,
    'form_item3822625': 1,
    'form_item3818337': 1,
    'form_item3841064': 2,
    'form_item3841065': 6,
    'form_item3841066': 1,
    'form_item3831525': 1,
    'form_item3831524': 2,
    'form_item4143713': 6,
    'form_item3818322': 13,
    'form_item4143714': 2,
    'form_item3818328': 2,
    'form_item3869371': 6,
    'form_item3818325': 3,
    'form_item3818340': 6,
    'form_item3818330': 5,
    'form_item3818331': 1,
    'form_item3818332': 1,
    'form_item3831551': 6,
    'form_item3831552': 6,
    'form_item3822626': 5,
    'form_item3818338': 5,
    'form_item3831553': 5,
    'form_item3818339': 1,
    'form_item3818341': 6,
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

# ══════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════

app = FastAPI(title='mikai Jobcan Bridge', version='2.0.0')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

@app.middleware('http')
async def log_requests(request: Request, call_next):
    print(f'[HTTP] {request.method} {request.url.path}')
    response = await call_next(request)
    print(f'[HTTP] -> {response.status_code}')
    return response

def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail='Invalid API key')

@app.get('/api/health')
async def health():
    return {'status': 'ok', 'version': '2.0.0', 'wf_base': JOBCAN_WF_BASE}

@app.post('/api/fill')
async def fill_jobcan(req: FillRequest, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    if not req.items:
        raise HTTPException(status_code=400, detail='items が空です')
    if len(req.items) > 20:
        raise HTTPException(status_code=400, detail='最大 20 件')

    results = []
    try:
        async with async_playwright() as p:
            print(f'[MAIN] Launch browser for {req.email}')
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox',
                      '--disable-dev-shm-usage', '--disable-gpu',
                      '--disable-blink-features=AutomationControlled']
            )
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 900},
                locale='ja-JP',
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            )
            await context.add_init_script('Object.defineProperty(navigator,"webdriver",{get:()=>undefined})')
            page = await context.new_page()

            # Login
            lr = await login_jobcan(page, req.email, req.password)
            if not lr['ok']:
                print(f'[MAIN] Login failed: {lr["reason"]}')
                await browser.close()
                return JSONResponse(status_code=401, content={
                    'error': lr['reason'],
                    'debug_url': lr.get('url', ''),
                    'debug_title': lr.get('title', ''),
                })
            print(f'[MAIN] Login OK')

            # Process items
            for i, item in enumerate(req.items):
                print(f'[MAIN] Item {i+1}/{len(req.items)}: {item.title}')
                r = await process_item(page, item, req.action)
                results.append(r)
                print(f'[MAIN] -> {r.status} ({r.filled} filled)')

            await browser.close()
    except Exception as e:
        print(f'[MAIN] Fatal: {traceback.format_exc()}')
        return JSONResponse(status_code=500, content={'error': f'サーバーエラー: {str(e)[:200]}'})

    return {'results': [r.dict() for r in results]}

# ══════════════════════════════════════════════════════════
# Login
# ══════════════════════════════════════════════════════════

async def login_jobcan(page, email, password):
    # Step 1: Open login page
    try:
        print(f'[LOGIN] goto {JOBCAN_LOGIN_URL}')
        await page.goto(JOBCAN_LOGIN_URL, wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        return {'ok': False, 'reason': f'ログインページ接続失敗: {str(e)[:100]}', 'url': '', 'title': ''}

    url = page.url
    print(f'[LOGIN] now at: {url}')

    # Already on WF?
    if 'wf.jobcan.jp' in url:
        return {'ok': True}

    # Already authenticated (not on sign_in)?
    if 'id.jobcan.jp' in url and 'sign_in' not in url:
        print('[LOGIN] Already authed, go to WF')
        return await goto_wf(page)

    # Find login form
    if not await page.query_selector('#user_email'):
        return {'ok': False, 'reason': f'ログインフォーム不明: {url}', 'url': url, 'title': await stitle(page)}

    # Fill & submit
    print(f'[LOGIN] fill {email}')
    try:
        await page.fill('#user_email', email)
        await page.fill('#user_password', password)
        await page.wait_for_timeout(500)
        await page.click('[name="commit"]')
    except Exception as e:
        return {'ok': False, 'reason': f'入力エラー: {str(e)[:80]}', 'url': url, 'title': ''}

    # Wait for redirect (poll every 1s, max 20s)
    print('[LOGIN] waiting redirect...')
    for _ in range(20):
        await page.wait_for_timeout(1000)
        if 'sign_in' not in page.url:
            print(f'[LOGIN] redirected to: {page.url}')
            break
    else:
        txt = await stext(page)
        reason = 'メールアドレスまたはパスワードが正しくありません。'
        if 'captcha' in txt.lower():
            reason = 'CAPTCHA が表示されています。'
        elif '二段階' in txt or '認証コード' in txt:
            reason = '二段階認証が有効です。'
        return {'ok': False, 'reason': reason, 'url': page.url, 'title': await stitle(page)}

    # Navigate to WF
    return await goto_wf(page)

async def goto_wf(page):
    try:
        target = JOBCAN_WF_BASE + '/'
        print(f'[LOGIN] goto {target}')
        await page.goto(target, wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        return {'ok': False, 'reason': f'WF接続失敗: {str(e)[:100]}', 'url': page.url, 'title': await stitle(page)}

    if 'sign_in' in page.url:
        return {'ok': False, 'reason': 'セッション無効', 'url': page.url, 'title': await stitle(page)}

    print(f'[LOGIN] OK at {page.url}')
    return {'ok': True}

# ══════════════════════════════════════════════════════════
# Process single item
# ══════════════════════════════════════════════════════════

async def process_item(page, item, action):
    try:
        url = JOBCAN_FORM_URLS.get(item.flow_type, JOBCAN_FORM_URLS['発注稟議'])
        print(f'[ITEM] goto {url}')
        try:
            await page.goto(url, wait_until='networkidle', timeout=30000)
        except Exception as e:
            return FillResult(row_num=item.row_num, title=item.title, status='error',
                              filled=0, errors=[f'フォーム接続失敗: {str(e)[:80]}'],
                              message='フォームを開けません。')

        await page.wait_for_timeout(4000)
        fc = await page.evaluate('document.querySelectorAll("input,select,textarea").length')
        print(f'[ITEM] {fc} form elements found')
        if fc < 3:
            await page.wait_for_timeout(3000)

        filled, errors = await fill_form(page, item.payload)

        save_msg = ''
        if action == 'draft':
            save_msg = await save_draft(page)
        elif action == 'submit':
            save_msg = await submit_form(page)

        return FillResult(row_num=item.row_num, title=item.title,
                          status='success' if not errors else 'partial',
                          filled=filled, errors=errors,
                          message=f'{filled} 項目入力完了。{save_msg}')
    except Exception as e:
        print(f'[ITEM] error: {traceback.format_exc()}')
        return FillResult(row_num=item.row_num, title=item.title, status='error',
                          filled=0, errors=[str(e)[:100]], message=f'エラー: {str(e)[:100]}')

# ══════════════════════════════════════════════════════════
# Form filling
# ══════════════════════════════════════════════════════════

async def fill_form(page, payload):
    filled = 0
    errors = []
    for key, value in payload.items():
        if key.startswith('_'):
            continue
        value = str(value).strip()
        if not value:
            continue
        label = FIELD_LABELS.get(key, key)
        ft = FIELD_TYPES.get(key, 1)
        try:
            ok = False
            if ft in (1, 2, 3, 4, 9):
                ok = await fill_text(page, key, value)
            elif ft == 5:
                ok = await fill_radio(page, key, value)
            elif ft == 6:
                ok = await fill_dropdown(page, key, value)
            elif ft == 7:
                ok = await fill_checkbox(page, key, value)
            elif ft == 13:
                continue
            else:
                ok = await fill_text(page, key, value)
            if ok:
                filled += 1
                await page.wait_for_timeout(300)
            else:
                errors.append(label)
        except Exception as e:
            errors.append(f'{label}({str(e)[:30]})')
    return filled, errors

async def fill_text(page, fid, val):
    r = await page.evaluate('''(a) => {
        var f=a[0],v=a[1];
        var el=document.querySelector('[name="'+f+'"]')
            ||document.querySelector('[ng-model*="'+f+'"]')
            ||document.querySelector('#'+f)
            ||document.querySelector('[data-field-id="'+f+'"]');
        if(!el)return false;
        var p=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
        var d=Object.getOwnPropertyDescriptor(p,'value');
        if(d&&d.set)d.set.call(el,v);else el.value=v;
        el.dispatchEvent(new Event('focus',{bubbles:true}));
        el.dispatchEvent(new Event('input',{bubbles:true}));
        el.dispatchEvent(new Event('change',{bubbles:true}));
        el.dispatchEvent(new Event('blur',{bubbles:true}));
        if(window.angular){try{var s=angular.element(el).scope();if(s){
            var m=el.getAttribute('ng-model');if(m){try{var ps=m.split('.');var o=s;
            for(var i=0;i<ps.length-1;i++){o=o[ps[i]];if(!o)break;}
            if(o)o[ps[ps.length-1]]=v;}catch(e){}}s.$apply();}}catch(e){}}
        return true;
    }''', [fid, val])
    return bool(r)

async def fill_dropdown(page, fid, val):
    r = await page.evaluate('''(a) => {
        var f=a[0],v=a[1];
        var el=document.querySelector('select[name="'+f+'"]')
            ||document.querySelector('select[ng-model*="'+f+'"]')
            ||document.querySelector('select#'+f);
        if(!el||el.tagName!=='SELECT')return false;
        var ok=false;
        for(var i=0;i<el.options.length;i++){
            var t=el.options[i].text.trim();
            if(t.indexOf(v)!==-1||v.indexOf(t)!==-1){el.selectedIndex=i;ok=true;break;}}
        if(!ok)return false;
        el.dispatchEvent(new Event('change',{bubbles:true}));
        if(window.angular){try{var s=angular.element(el).scope();if(s){
            var m=el.getAttribute('ng-model');if(m){try{var ps=m.split('.');var o=s;
            for(var i=0;i<ps.length-1;i++){o=o[ps[i]];if(!o)break;}
            if(o)o[ps[ps.length-1]]=el.value;}catch(e){}}s.$apply();}}catch(e){}}
        return true;
    }''', [fid, val])
    return bool(r)

async def fill_radio(page, fid, val):
    r = await page.evaluate('''(a) => {
        var f=a[0],v=a[1];
        var g=document.querySelectorAll('input[type="radio"][name="'+f+'"]');
        if(!g.length)g=document.querySelectorAll('input[type="radio"][ng-model*="'+f+'"]');
        if(!g.length)return false;
        for(var i=0;i<g.length;i++){
            var p=g[i].closest('label')||g[i].parentElement;
            if(p&&p.textContent.trim().indexOf(v)!==-1){
                g[i].click();
                if(window.angular){try{angular.element(g[i]).scope().$apply();}catch(e){}}
                return true;}}
        return false;
    }''', [fid, val])
    return bool(r)

async def fill_checkbox(page, fid, val):
    vals = [v.strip() for v in val.split(',') if v.strip()]
    r = await page.evaluate('''(a) => {
        var f=a[0],vs=a[1];
        var g=document.querySelectorAll('input[type="checkbox"][name="'+f+'"]');
        if(!g.length)g=document.querySelectorAll('input[type="checkbox"][name="'+f+'[]"]');
        if(!g.length)g=document.querySelectorAll('input[type="checkbox"][ng-model*="'+f+'"]');
        if(!g.length)return false;
        var m=0;
        for(var i=0;i<g.length;i++){
            var p=g[i].closest('label')||g[i].parentElement;
            var t=p?p.textContent.trim():'';
            for(var j=0;j<vs.length;j++){
                if(t.indexOf(vs[j])!==-1){
                    if(!g[i].checked)g[i].click();m++;
                    if(window.angular){try{angular.element(g[i]).scope().$apply();}catch(e){}}
                    break;}}}
        return m>0;
    }''', [fid, vals])
    return bool(r)

# ══════════════════════════════════════════════════════════
# Save / Submit
# ══════════════════════════════════════════════════════════

async def save_draft(page):
    try:
        for sel in ['button:has-text("下書き")', 'a:has-text("下書き")',
                     '[ng-click*="draft"]', '[ng-click*="save"]']:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(3000)
                return '下書き保存しました。'
        return '⚠️ 下書きボタンが見つかりません。'
    except Exception as e:
        return f'⚠️ 保存エラー: {str(e)[:50]}'

async def submit_form(page):
    try:
        for sel in ['button:has-text("申請")', '[ng-click*="submit"]']:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(3000)
                for cs in ['button:has-text("OK")', 'button:has-text("はい")']:
                    cb = await page.query_selector(cs)
                    if cb:
                        await cb.click()
                        await page.wait_for_timeout(2000)
                        break
                return '申請しました。'
        return '⚠️ 申請ボタンが見つかりません。'
    except Exception as e:
        return f'⚠️ 申請エラー: {str(e)[:50]}'

# ══════════════════════════════════════════════════════════
# Utility
# ══════════════════════════════════════════════════════════

async def stitle(page):
    try: return await page.title()
    except: return ''

async def stext(page):
    try: return await page.evaluate('document.body?document.body.innerText.substring(0,1000):""')
    except: return ''

# ══════════════════════════════════════════════════════════
# Entry
# ══════════════════════════════════════════════════════════

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8080))
    print(f'[BOOT] mikai Jobcan Bridge v2.0.0 on port {port}')
    uvicorn.run(app, host='0.0.0.0', port=port)
