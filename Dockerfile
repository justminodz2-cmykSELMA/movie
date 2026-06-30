# بيئة بايثون حديثة ومتوافقة مع runtime.txt
FROM python:3.11-slim

# تثبيت المتطلبات الأساسية (ffmpeg لمعالجة الصوت + مكتبات Playwright)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# نسخ المتطلبات أولاً للاستفادة من الـ cache
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت متصفح Playwright
RUN playwright install chromium --with-deps

# نسخ بقية المشروع
COPY main.py main.py

# تشغيل السيرفر عبر Gunicorn
CMD gunicorn --bind 0.0.0.0:$PORT main:app --timeout 120 --workers 2 --threads 4
