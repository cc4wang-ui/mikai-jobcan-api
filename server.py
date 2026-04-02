"""
mikai Jobcan 自動填入 — Railway Backend
FastAPI + Playwright (Headless Chromium)

API:
  POST /api/fill   — 登入 Jobcan → 自動填入 → 下書き保存
  GET  /api/health — ヘルスチェック

環境変数:
  API_KEY  — Vercel 前端認證用（Railway 設定）
  PORT     — Railway 自動設定
"""

import os
import json
import time
import asyncio
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
# ══════════════════════════════════════════════════════════

API_KEY = os.environ.get('API_KEY', 'mikai-dev-key-change-me')
JOBCAN_LOGIN_URL = 'https://id.jobcan.jp/users/sign_in'
JOBCAN_FORM_URLS = {
    '発注稟議': 'https://wf.jobcan.jp/#/requests/new/666628',
    '支払依頼': 'https://wf.jobcan.jp/#/requests/new/666591',
}

# ── Jobcan 欄位 → 日本語名（デバッグ用）───────────────────
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

# 欄位 type 定義（用來決定填入策略）
FIELD_TYPES = {
    'form_item3831493': 7,   # checkbox
    'form_item3831494': 4,   # date
    'form_item3818321': 7,   # checkbox
    'form_item3818329': 7,   # checkbox
    'form_item3818323': 6,   # dropdown
    'form_item3818324': 9,   # company selector
    'form_item3822625': 1,   # text
    'form_item3818337': 1,   # text
    'form_item3841064': 2,   # textarea
    'form_item3841065': 6,   # dropdown
    'form_item3841066': 1,   # text
    'form_item3831525': 1,   # text
    'form_item3831524': 2,   # textarea
    'form_item4143713': 6,   # dropdown
    'form_item3818322': 13,  # special
    'form_item4143714': 2,   # textarea
    'form_item3818328': 2,   # textarea
    'form_item3869371': 6,   # dropdown
    'form_item3818325': 3,   # number
    'form_item3818340': 6,   # dropdown
    'form_item3818330': 5,   # radio
    'form_item3818331': 1,   # text
    'form_item3818332': 1,   # text
    'form_item3831551': 6,   # dropdown
    'form_item3831552': 6,   # dropdown
    'form_item3822626': 5,   # radio
    'form_item3818338': 5,   # radio
    'form_item3831553': 5,   # radio
    'form_item3818339': 1,   # text
    'form_item3818341': 6,   # dropdown
}

# ══════════════════════════════════════════════════════════
# Pydantic Models
# ══════════════════════════════════════════════════════════

class FillPayload(BaseModel):
    """單筆 Jobcan 申請"""
    payload: dict           # form_item → value のマッピング
    flow_type: str = '発注稟議'  # 発注稟議 or 支払依頼
    title: str = ''
    row_num: int = 0        # Sheet の行番号（結果回報用）

class FillRequest(BaseModel):
    """API リクエスト"""
    email: str
    password: str
    items: List[FillPayload]
    action: str = 'draft'   # draft = 下書き保存, submit = 申請

class FillResult(BaseModel):
    """單筆結果"""
    row_num: int
    title: str
    status: str             # success / partial / error
    filled: int
    errors: List[str]
    message: str

# ══════════════════════════════════════════════════════════
# FastAPI App
# ══════════════════════════════════════════════════════════

app = FastAPI(title='mikai Jobcan Bridge', version='1.0.0')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],  # Vercel からのリクエストを許可
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """API Key 認証"""
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail='Invalid API key')


@app.get('/api/health')
async def health():
    return {
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'service': 'mikai-jobcan-bridge',
    }


@app.post('/api/fill')
async def fill_jobcan(req: FillRequest, x_api_key: Optional[str] = Header(None)):
    """Jobcan フォームに自動入力"""
    verify_api_key(x_api_key)

    if not req.items:
        raise HTTPException(status_code=400, detail='items が空です')
    if len(req.items) > 20:
        raise HTTPException(status_code=400, detail='一度に最大 20 件まで')

    results = []

    async with async_playwright() as p:
        print(f'[FILL] Starting browser for {req.email}...')
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-blink-features=AutomationControlled',
            ]
        )
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 900},
            locale='ja-JP',
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        )
        # navigator.webdriver を隠す
        await context.add_init_script('''
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        ''')
        page = await context.new_page()

        # ── Step 1: ログイン ──
        try:
            login_result = await login_jobcan(page, req.email, req.password)
            if not login_result['ok']:
                print(f'[FILL] Login failed: {login_result["reason"]}')
                await browser.close()
                return JSONResponse(
                    status_code=401,
                    content={
                        'error': f'Jobcan ログイン失敗: {login_result["reason"]}',
                        'debug_url': login_result.get('url', ''),
                        'debug_title': login_result.get('title', ''),
                    }
                )
            print(f'[FILL] Login success for {req.email}')
        except Exception as e:
            print(f'[FILL] Login exception: {str(e)}')
            await browser.close()
            return JSONResponse(
                status_code=500,
                content={'error': f'ログインエラー: {str(e)}'}
            )

        # ── Step 2: 各申請を処理 ──
        for item in req.items:
            result = await process_single_item(page, item, req.action)
            results.append(result)

        await browser.close()

    return {'results': [r.dict() for r in results]}


# ══════════════════════════════════════════════════════════
# Jobcan ログイン
# ══════════════════════════════════════════════════════════

async def login_jobcan(page, email: str, password: str) -> dict:
    """Jobcan にログイン — 結果を dict で返す"""
    print(f'[LOGIN] Navigating to {JOBCAN_LOGIN_URL}...')
    await page.goto(JOBCAN_LOGIN_URL, wait_until='networkidle')
    await page.wait_for_timeout(2000)

    current_url = page.url
    current_title = await page.title()
    print(f'[LOGIN] Page loaded: {current_url} | Title: {current_title}')

    # ログインページか確認
    email_input = await page.query_selector('#user_email')
    if not email_input:
        # SSO リダイレクト等でログインページではない場合
        page_text = await page.evaluate('document.body ? document.body.innerText.substring(0, 500) : ""')
        print(f'[LOGIN] No email field found. Page text: {page_text[:200]}')
        return {
            'ok': False,
            'reason': f'ログインページが表示されません。URL: {current_url}',
            'url': current_url,
            'title': current_title,
        }

    # メール/パスワード入力
    print(f'[LOGIN] Filling credentials for {email}...')
    await page.fill('#user_email', email)
    await page.fill('#user_password', password)
    await page.wait_for_timeout(500)
    await page.click('[name="commit"]')

    # ログイン完了を待機（最大 20 秒）
    print('[LOGIN] Waiting for redirect...')
    try:
        await page.wait_for_url('**/wf.jobcan.jp/**', timeout=20000)
        print(f'[LOGIN] Success! URL: {page.url}')
        return {'ok': True}
    except Exception:
        pass

    # 失敗 — 原因を特定
    final_url = page.url
    final_title = await page.title()
    page_text = await page.evaluate('document.body ? document.body.innerText.substring(0, 1000) : ""')
    print(f'[LOGIN] Failed. URL: {final_url} | Title: {final_title}')
    print(f'[LOGIN] Page text: {page_text[:300]}')

    reason = 'メール/パスワードが正しくないか、追加認証が必要です。'

    # エラーメッセージ検出
    if 'メールアドレスまたはパスワード' in page_text:
        reason = 'メールアドレスまたはパスワードが正しくありません。'
    elif 'CAPTCHA' in page_text.upper() or 'recaptcha' in page_text.lower():
        reason = 'CAPTCHA が表示されています。Jobcan が自動ログインをブロックしています。'
    elif '二段階認証' in page_text or '認証コード' in page_text or 'two-factor' in page_text.lower():
        reason = '二段階認証（2FA）が有効です。Jobcan の設定で 2FA を無効にしてください。'
    elif 'id.jobcan.jp' in final_url:
        reason = f'ログインページから遷移しません。ページ内容: {page_text[:100]}'

    return {
        'ok': False,
        'reason': reason,
        'url': final_url,
        'title': final_title,
    }


# ══════════════════════════════════════════════════════════
# 単一申請の処理
# ══════════════════════════════════════════════════════════

async def process_single_item(page, item: FillPayload, action: str) -> FillResult:
    """1 件の申請を処理: フォーム開く → 入力 → 保存"""
    try:
        # 1. フォームを開く
        form_url = JOBCAN_FORM_URLS.get(item.flow_type, JOBCAN_FORM_URLS['発注稟議'])
        await page.goto(form_url, wait_until='networkidle')
        await page.wait_for_timeout(3000)  # AngularJS レンダリング待ち

        # 2. フォーム入力
        filled, errors = await fill_form(page, item.payload)

        # 3. 下書き保存 or 申請
        save_msg = ''
        if action == 'draft':
            save_msg = await save_draft(page)
        elif action == 'submit':
            save_msg = await submit_form(page)

        status = 'success' if not errors else 'partial'
        return FillResult(
            row_num=item.row_num,
            title=item.title,
            status=status,
            filled=filled,
            errors=errors,
            message=f'{filled} 項目入力完了。{save_msg}',
        )

    except Exception as e:
        return FillResult(
            row_num=item.row_num,
            title=item.title,
            status='error',
            filled=0,
            errors=[str(e)],
            message=f'エラー: {str(e)}',
        )


# ══════════════════════════════════════════════════════════
# フォーム入力 — field type 別の戦略
# ══════════════════════════════════════════════════════════

async def fill_form(page, payload: dict) -> tuple:
    """
    JSON payload のキーを順番に入力。
    field type に応じて異なる入力戦略を使用。

    Returns: (filled_count, error_list)
    """
    filled = 0
    errors = []

    for key, value in payload.items():
        # 内部メタフィールドはスキップ
        if key.startswith('_'):
            continue

        value = str(value).strip()
        if not value:
            continue

        label = FIELD_LABELS.get(key, key)
        field_type = FIELD_TYPES.get(key, 1)  # デフォルト: text

        try:
            success = False

            if field_type in (1, 2, 3, 4):
                # Text / Textarea / Number / Date
                success = await fill_text(page, key, value)
            elif field_type == 5:
                # Radio
                success = await fill_radio(page, key, value)
            elif field_type == 6:
                # Dropdown (SELECT)
                success = await fill_dropdown(page, key, value)
            elif field_type == 7:
                # Checkbox group
                success = await fill_checkbox(page, key, value)
            elif field_type == 9:
                # Company selector — text として入力
                success = await fill_text(page, key, value)
            elif field_type == 13:
                # Special (予算申請) — スキップ
                continue
            else:
                success = await fill_text(page, key, value)

            if success:
                filled += 1
                # AngularJS の再レンダリングを待つ
                await page.wait_for_timeout(300)
            else:
                errors.append(f'{label}（{key}）')

        except Exception as e:
            errors.append(f'{label}（{str(e)[:40]}）')

    return filled, errors


async def fill_text(page, field_id: str, value: str) -> bool:
    """
    Text / Textarea / Number / Date 入力
    nativeInputValueSetter + AngularJS $apply() パターン
    """
    result = await page.evaluate('''(args) => {
        const [fieldId, value] = args;

        // Strategy 1: name 属性
        let el = document.querySelector('[name="' + fieldId + '"]');

        // Strategy 2: ng-model
        if (!el) el = document.querySelector('[ng-model*="' + fieldId + '"]');

        // Strategy 3: id
        if (!el) el = document.querySelector('#' + fieldId);

        // Strategy 4: data-field-id (Jobcan カスタム属性)
        if (!el) el = document.querySelector('[data-field-id="' + fieldId + '"]');

        if (!el) return false;

        // nativeInputValueSetter — AngularJS の変更検知を正しくトリガー
        const proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value');

        if (setter && setter.set) {
            setter.set.call(el, value);
        } else {
            el.value = value;
        }

        // 全イベントを発火
        el.dispatchEvent(new Event('focus', { bubbles: true }));
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new Event('blur', { bubbles: true }));

        // AngularJS $apply
        if (window.angular) {
            try {
                const scope = angular.element(el).scope();
                if (scope) {
                    // ng-model を直接更新
                    const ngModel = el.getAttribute('ng-model');
                    if (ngModel && scope.$eval) {
                        try {
                            // ng-model のパスから最後のプロパティを取得
                            const parts = ngModel.split('.');
                            let obj = scope;
                            for (let i = 0; i < parts.length - 1; i++) {
                                obj = obj[parts[i]];
                                if (!obj) break;
                            }
                            if (obj) {
                                obj[parts[parts.length - 1]] = value;
                            }
                        } catch(e) {}
                    }
                    scope.$apply();
                }
            } catch(e) {}
        }

        return true;
    }''', [field_id, value])

    return bool(result)


async def fill_dropdown(page, field_id: str, value: str) -> bool:
    """
    Dropdown (SELECT) — option テキストで部分一致
    """
    result = await page.evaluate('''(args) => {
        const [fieldId, value] = args;

        // SELECT 要素を検索
        let el = document.querySelector('select[name="' + fieldId + '"]');
        if (!el) el = document.querySelector('select[ng-model*="' + fieldId + '"]');
        if (!el) el = document.querySelector('select#' + fieldId);

        if (!el || el.tagName !== 'SELECT') return false;

        // option テキストで部分一致検索
        let found = false;
        for (let i = 0; i < el.options.length; i++) {
            const optText = el.options[i].text.trim();
            if (optText.indexOf(value) !== -1 || value.indexOf(optText) !== -1) {
                el.selectedIndex = i;
                found = true;
                break;
            }
        }

        if (!found) return false;

        // イベント発火 + AngularJS $apply
        el.dispatchEvent(new Event('change', { bubbles: true }));

        if (window.angular) {
            try {
                const scope = angular.element(el).scope();
                if (scope) {
                    const ngModel = el.getAttribute('ng-model');
                    if (ngModel) {
                        try {
                            const parts = ngModel.split('.');
                            let obj = scope;
                            for (let i = 0; i < parts.length - 1; i++) {
                                obj = obj[parts[i]];
                                if (!obj) break;
                            }
                            if (obj) {
                                obj[parts[parts.length - 1]] = el.value;
                            }
                        } catch(e) {}
                    }
                    scope.$apply();
                }
            } catch(e) {}
        }

        return true;
    }''', [field_id, value])

    return bool(result)


async def fill_radio(page, field_id: str, value: str) -> bool:
    """
    Radio ボタン — 同名の全要素から label テキストで一致
    """
    result = await page.evaluate('''(args) => {
        const [fieldId, value] = args;

        // name 属性で radio グループを検索
        let group = document.querySelectorAll('input[type="radio"][name="' + fieldId + '"]');

        // ng-model でも検索
        if (group.length === 0) {
            group = document.querySelectorAll('input[type="radio"][ng-model*="' + fieldId + '"]');
        }

        if (group.length === 0) return false;

        for (const el of group) {
            // 親要素のテキストで値を照合
            const parent = el.closest('label') || el.parentElement;
            const labelText = parent ? parent.textContent.trim() : '';

            if (labelText.indexOf(value) !== -1) {
                el.click();

                // AngularJS $apply
                if (window.angular) {
                    try {
                        const scope = angular.element(el).scope();
                        if (scope && scope.$apply) scope.$apply();
                    } catch(e) {}
                }
                return true;
            }
        }

        return false;
    }''', [field_id, value])

    return bool(result)


async def fill_checkbox(page, field_id: str, value: str) -> bool:
    """
    Checkbox グループ — 複数選択可（カンマ区切り対応）
    例: "稟議" or "稟議,事後稟議"
    """
    values = [v.strip() for v in value.split(',') if v.strip()]

    result = await page.evaluate('''(args) => {
        const [fieldId, values] = args;

        // checkbox グループを検索
        let group = document.querySelectorAll('input[type="checkbox"][name="' + fieldId + '"]');

        // name が配列形式の場合（name="form_itemXXX[]"）
        if (group.length === 0) {
            group = document.querySelectorAll('input[type="checkbox"][name="' + fieldId + '[]"]');
        }

        // ng-model でも検索
        if (group.length === 0) {
            group = document.querySelectorAll('input[type="checkbox"][ng-model*="' + fieldId + '"]');
        }

        if (group.length === 0) return false;

        let matched = 0;
        for (const el of group) {
            const parent = el.closest('label') || el.parentElement;
            const labelText = parent ? parent.textContent.trim() : '';

            for (const val of values) {
                if (labelText.indexOf(val) !== -1) {
                    if (!el.checked) {
                        el.click();
                    }
                    matched++;

                    if (window.angular) {
                        try {
                            const scope = angular.element(el).scope();
                            if (scope && scope.$apply) scope.$apply();
                        } catch(e) {}
                    }
                    break;
                }
            }
        }

        return matched > 0;
    }''', [field_id, values])

    return bool(result)


# ══════════════════════════════════════════════════════════
# 下書き保存 / 申請
# ══════════════════════════════════════════════════════════

async def save_draft(page) -> str:
    """下書き保存ボタンをクリック"""
    try:
        # Jobcan の下書きボタンを探す
        btn = await page.query_selector('button:has-text("下書き")')
        if not btn:
            btn = await page.query_selector('a:has-text("下書き")')
        if not btn:
            # AngularJS の ng-click で検索
            btn = await page.query_selector('[ng-click*="draft"]')

        if btn:
            await btn.click()
            await page.wait_for_timeout(3000)
            return '下書き保存しました。'
        else:
            return '下書きボタンが見つかりません。手動で保存してください。'
    except Exception as e:
        return f'下書き保存エラー: {str(e)[:50]}'


async def submit_form(page) -> str:
    """申請ボタンをクリック"""
    try:
        btn = await page.query_selector('button:has-text("申請")')
        if not btn:
            btn = await page.query_selector('[ng-click*="submit"]')

        if btn:
            await btn.click()
            await page.wait_for_timeout(3000)

            # 確認ダイアログがある場合
            confirm_btn = await page.query_selector('button:has-text("OK")')
            if confirm_btn:
                await confirm_btn.click()
                await page.wait_for_timeout(2000)

            return '申請しました。'
        else:
            return '申請ボタンが見つかりません。手動で申請してください。'
    except Exception as e:
        return f'申請エラー: {str(e)[:50]}'


# ══════════════════════════════════════════════════════════
# エントリポイント
# ══════════════════════════════════════════════════════════

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 8080))
    uvicorn.run(app, host='0.0.0.0', port=port)
