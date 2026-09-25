# Один образ на все роли (TZ §6, шаг 13). По умолчанию APP_ROLE=all: bot, web и
# collector работают в одном контейнере — на Amvera это один проект.
# Роль выбирается во время запуска, а не при сборке, поэтому при желании из того
# же образа можно поднять и три отдельных проекта (APP_ROLE=bot|web|collector).
FROM python:3.11-slim

# EXTRAS решает, какие необязательные зависимости попадут в образ.
# По умолчанию без `embed`: sentence-transformers тянет torch, а это ~2 ГБ и
# около 2 ГБ ОЗУ сверху. В продакшене эмбеддинги берутся через API (TZ §6).
ARG EXTRAS=media,web,ml

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ROLE=all \
    DATA_DIR=/data \
    TG_SESSION=/data/collector.session \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=80

WORKDIR /app

# ffmpeg нужен faster-whisper для чтения голосовых в формате ogg/opus
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

# Зависимости ставим раньше кода: правка кода не пересобирает слой с пакетами
COPY pyproject.toml README.md ./
COPY src/__init__.py src/__init__.py
RUN pip install --upgrade pip && pip install ".[${EXTRAS}]"

COPY src/ src/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# /data — постоянное хранилище Amvera: сессия, медиа, кэш моделей
RUN mkdir -p /data/media /data/models

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
  CMD /entrypoint.sh healthcheck

ENTRYPOINT ["/entrypoint.sh"]
