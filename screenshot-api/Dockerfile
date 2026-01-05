FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY shared/ ./shared/
COPY screenshot-api/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

COPY screenshot-api/main.py ./

EXPOSE 8000
CMD ["python", "main.py"]
