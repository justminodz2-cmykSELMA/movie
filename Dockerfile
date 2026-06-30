
# استخدام بيئة بايثون خفيفة وحديثة
FROM python:3.10-slim

# تحديث النظام وتثبيت المتطلبات الأساسية (مثل ffmpeg لمعالجة الصوتيات)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

# تحديد مجلد العمل
WORKDIR /app

# نسخ ملفات المشروع
COPY requirements.txt requirements.txt
COPY main.py main.py

# تثبيت متطلبات بايثون
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت متصفحات Playwright
RUN playwright install chromium --with-deps

# تشغيل السيرفر بواسطة Gunicorn
CMD gunicorn --bind 0.0.0.0:$PORT main:app --timeout 120 --workers 2 --threads 4
