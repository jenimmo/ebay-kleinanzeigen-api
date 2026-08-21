FROM python:3.12-slim-bookworm

WORKDIR /app

# Install Python dependencies before copying source (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + system deps for Playwright
RUN playwright install --with-deps chromium

COPY . .

# Persistent jobs/results live here. Mount /app/data as a volume in production.
RUN mkdir -p /app/data
ENV KLEINANZEIGEN_JOB_DATABASE_PATH=/app/data/kleinanzeigen_jobs.sqlite3

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
