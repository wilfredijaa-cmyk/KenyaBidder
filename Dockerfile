# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 KENYABIDDER_ENV=production
WORKDIR /app
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin kb && mkdir -p /data && chown kb /data
COPY pyproject.toml requirements.txt README.md ./
COPY kenyabidder ./kenyabidder
RUN pip install --no-cache-dir . "psycopg[binary]>=3.1"
USER kb
VOLUME ["/data"]
EXPOSE 8080
ENV KENYABIDDER_DATA=/data/state.json HOST=0.0.0.0 PORT=8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=4).status==200 else 1)"
CMD ["python", "-m", "kenyabidder"]
