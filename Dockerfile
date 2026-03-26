FROM alpine:3.19

RUN apk add --no-cache \
    ffmpeg \
    bash \
    curl \
    python3 \
    py3-pip \
    alsa-utils

RUN pip3 install --break-system-packages --no-cache-dir aiohttp

COPY entrypoint.sh /entrypoint.sh
COPY relay.py /relay.py

RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${RELAY_PORT:-8100}/health || exit 1

CMD ["/entrypoint.sh"]
