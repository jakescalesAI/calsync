FROM python:3.13-slim
RUN pip install --no-cache-dir "python-dateutil>=2.8" "tzdata>=2024.1" \
    && useradd --uid 10001 --home-dir /data --no-create-home calsync \
    && mkdir -p /data /app && chown calsync /data
COPY calsync.py calsync_web.py /app/
ENV CALSYNC_HOME=/data CALSYNC_BIND=0.0.0.0 CALSYNC_PORT=8484 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER calsync
VOLUME /data
EXPOSE 8484
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8484/healthz', timeout=4)"]
CMD ["python", "calsync.py", "web"]
