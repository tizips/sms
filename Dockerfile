FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    SMS_BASE=/htdocs/sms

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir psycopg2-binary==2.9.9

WORKDIR /htdocs/sms

COPY admin ./admin
COPY migrations ./migrations
COPY bin/db-migrate ./bin/db-migrate
COPY docker/entrypoint.sh /usr/local/bin/sms-admin-entrypoint

RUN mkdir -p conf data logs spool/inbox spool/outbox spool/sent spool/error \
    && chmod +x /usr/local/bin/sms-admin-entrypoint /htdocs/sms/bin/db-migrate

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT', '8088'), timeout=3).read(1)"

ENTRYPOINT ["sms-admin-entrypoint"]
CMD ["python3", "/htdocs/sms/admin/main.py"]
