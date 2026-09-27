# syntax=docker/dockerfile:1

FROM python:3.12.13-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

ENV HOME=/config \
    XDG_CACHE_HOME=/config/cache \
    PATH=/usr/local/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN test "${APP_UID}" -gt 0 \
    && test "${APP_GID}" -gt 0 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" foxden \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" \
        --home-dir /config --no-create-home --shell /usr/sbin/nologin foxden \
    && install -d -o foxden -g foxden -m 0750 \
        /app /config /config/cache /staging /music

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt

COPY --chown=foxden:foxden src/ ./src/
COPY --chown=foxden:foxden scripts/ ./scripts/
RUN python -m pip install --no-cache-dir --no-deps .

USER foxden:foxden

EXPOSE 8000
STOPSIGNAL SIGTERM

# The wrapper imports supported *_FILE secrets, applies the configured umask,
# and immediately execs the selected service. It does not remain as a root or
# supervisor process.
ENTRYPOINT ["python", "-m", "foxden_music.healthcheck", "exec", "--"]
CMD ["foxden-music-web"]
