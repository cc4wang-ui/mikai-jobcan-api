# 使用官方 Microsoft Playwright Python image
# 已含 Chromium + 所有系統依賴，避免 Railway 下載卡住
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# Python 依存関係
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーション
COPY server.py .

# Railway は PORT 環境変数を自動設定
CMD ["python", "server.py"]
