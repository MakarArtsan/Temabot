#!/usr/bin/env sh
# Точка входа: выбирает процесс по APP_ROLE (TZ §6, шаг 13).
# all (по умолчанию) — bot, web и collector в одном контейнере под супервизором.
set -e

ROLE="${APP_ROLE:-all}"

# Проверка живости для HEALTHCHECK: у web есть http-ручка, у остальных ролей
# проверяем, что процесс вообще отвечает на импорт конфигурации.
if [ "$1" = "healthcheck" ]; then
    case "$ROLE" in
        web|all) exec curl -fsS "http://127.0.0.1:${WEB_PORT:-80}/healthz" ;;
        *)   exec python -c "from src.config import cfg; assert cfg.APP_ROLE" ;;
    esac
fi

mkdir -p "${DATA_DIR:-/data}/media" "${DATA_DIR:-/data}/models"

# Файл сессии — это полный доступ к аккаунту (TZ §0)
if [ -f "${TG_SESSION}" ]; then
    chmod 600 "${TG_SESSION}" || true
fi

echo "Запускаю роль: ${ROLE}"

case "$ROLE" in
    all)
        # Схему накатывает сам супервизор, до старта сервисов
        exec python -m src.supervisor
        ;;
    collector)
        # Строго один экземпляр: вторая копия с той же сессией даёт
        # AUTH_KEY_DUPLICATED, и Telegram может сбросить авторизацию.
        exec python -m src.collector
        ;;
    bot)
        # Схему накатывает сам бот при старте (MIGRATE_ON_START) — он один,
        # гонки схемы не будет
        exec python -m src.bot.main
        ;;
    web)
        exec python -m src.web.app
        ;;
    migrate)
        exec python -m src.db.migrate
        ;;
    *)
        echo "Неизвестная роль: ${ROLE}. Ожидается all, collector, bot, web или migrate." >&2
        exit 1
        ;;
esac
