FROM python:3.12-slim

LABEL org.opencontainers.image.title="RouteCheck" \
      org.opencontainers.image.version="V2026.9.16" \
      org.opencontainers.image.source="https://github.com/Seven1echo/RouteCheck" \
      org.opencontainers.image.description="Mihomo 漏网之鱼采集、直连探测与直连规则生成器"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8787
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
