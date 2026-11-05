FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

COPY seeding_checker.py recheck.py config.example.yaml ./
COPY helper ./helper
COPY app ./app
COPY web ./web

# 사용자 데이터는 볼륨(/data)에만 쓴다. 키는 어디에도 저장하지 않는다.
RUN useradd -m app && mkdir -p /data && chown app /data
ENV DATA_DIR=/data REQUIRE_HTTPS=1 MAX_JOBS=1 MAX_CAP_USD=20 JOB_TTL_DAYS=7 SERVER_VIDEO=0
EXPOSE 8000
# 볼륨은 root 소유로 마운트되므로 시작할 때 소유자를 바꾼 뒤, 앱은 일반 사용자(app)로 실행한다
CMD ["sh", "-c", "chown -R app:app /data && exec runuser -u app -- uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
