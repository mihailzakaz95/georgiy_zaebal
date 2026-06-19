FROM python:3.11-slim

# Устанавливаем Chrome
RUN apt-get update && apt-get install -y \
    wget gnupg2 curl unzip \
    libglib2.0-0 libnss3 libgconf-2-4 libfontconfig1 \
    libxss1 libxtst6 libx11-xcb1 libxcb-dri3-0 \
    libdrm2 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libxkbcommon0 \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD ["python", "-u", "main.py"]
