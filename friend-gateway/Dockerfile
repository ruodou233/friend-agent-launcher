FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY friend-gateway/friend_gateway.py /app/friend_gateway.py
COPY friend-gateway/examples /app/examples

RUN addgroup --system --gid 10001 friendgateway \
    && adduser --system --uid 10001 --gid 10001 friendgateway \
    && mkdir -p /app/config \
    && chown 10001:10001 /app/config

USER 10001:10001
EXPOSE 3000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD-SHELL python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('FRIEND_GATEWAY_PORT', '3000') + '/healthz', timeout=2)"

CMD ["python3", "/app/friend_gateway.py"]
