# ---------------------------------------------------------------------------
# Solana Meme Detector — Production Image
# Python 3.11 + Node.js 18 + Playwright Chromium
# ---------------------------------------------------------------------------
FROM node:18-slim

# Install Python 3.11 + Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    ca-certificates curl \
    # Playwright / Chromium deps
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 libgtk-3-0 \
    libx11-6 libxext6 libxss1 libxtst6 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf python3 /usr/bin/python

WORKDIR /app

# ---------- Node.js dependencies (pinned, no ^ in package.json) ----------
COPY js/package.json ./js/
RUN cd js && npm install --prefer-offline

# ---------- Python dependencies ----------
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ---------- Playwright browser ----------
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium --with-deps

# ---------- Application source ----------
COPY . .

# Create runtime directories
RUN mkdir -p data/metadata

EXPOSE 8501

# Default: monitor-only (safe). Override in Railway to run full pipeline.
CMD ["python", "-m", "bot.main", "--mode", "monitor-only", "--network", "mainnet"]
