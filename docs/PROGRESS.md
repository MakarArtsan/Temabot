# Прогресс работ

Сверяться перед каждой итерацией и обновлять после. Нумерация шагов — из `docs/TZ.md` §6.
Легенда: ✅ сделано · 🟡 частично · ⬜ не начато

| Шаг | Что | Статус |
|---|---|---|
| 0 | Доступы (api_id/hash, ID группы, ключ LLM, Postgres+pgvector) | ⬜ на стороне владельца |
| 1 | Скелет: pyproject, config, .env.example, пустые модули, ruff/mypy, Makefile | ✅ |
| 2 | БД: `schema.sql` + `repo.py` + тесты | ⬜ |
| 3 | Collector (только текст, dry-run, FloodWait, докачка) | ⬜ |
| 4 | Бэкфилл истории порциями по 200 | ⬜ |
| 5 | Медиа: faster-whisper для голосовых, очередь с семафором | ⬜ |
| 6 | Треды (§4.2) + дайджест map-reduce (§4.3) + `make digest DATE=` | ⬜ |
| 7 | Бот aiogram: whitelist, команды §4.5 кроме /ask, APScheduler 23:30 | ⬜ |
| 8 | RAG: embed, chunk, гибридный поиск с RRF, `/ask` | ⬜ |
| 9 | Встроить бот-копировщик (§4.6) | 🟡 файл `src/bot/handlers_copier.py` положен, не подключён |
| 10 | Несколько групп и управление доступом (§4.8) | ⬜ |
| 11 | Умный отбор, обратная связь, retrain (§4.7) | ⬜ |
| 12 | Веб-админка (§4.9) | ⬜ |
| 12.5 | Рейтинги участников (§4.10) | ⬜ |
| 13 | Деплой на Amvera (§6, шаг 13) | ⬜ |

## Сделано на шаге 1

- `docs/TZ.md` — ТЗ в репозитории, `CLAUDE.md` — правила разработки.
- `src/config.py` — pydantic-settings, все переменные из TZ §5 + `APP_ROLE`, `EMBED_DIM`,
  `LOG_LEVEL`, `WEB_HOST/PORT`. Пустые числовые переменные в `.env` не ломают валидацию.
- Скелет пакетов по TZ §2: все модули импортируемые, с TODO и ссылкой на раздел ТЗ.
- `pyproject.toml`: зависимости разбиты на extras (`media`, `embed`, `web`, `ml`, `dev`),
  чтобы collector не тянул FastAPI, а бот — whisper. ruff + mypy + pytest настроены.
- `Makefile`: install-dev, run-collector/bot/web, migrate, login, backfill, digest,
  ratings, lint, fmt, typecheck, test, check.
- `.gitignore`: `.env`, `*.session`, `data/` — секретов в репозитории нет.
- `tests/test_config.py` — загрузка конфига, дефолты, extra_body для LLM.

## Открытые вопросы к владельцу

1. **Шаг 0 не выполнен** — нужны `TG_API_ID`/`TG_API_HASH`, ID группы, ключ LLM-провайдера
   и строка подключения к Postgres с pgvector. Без них шаги 3+ нельзя проверить на живых данных.
2. **Имя пакета/проекта**: в ТЗ каталог называется `tg-digest`, репозиторий — `Temabot`.
   Собрано в корне `Temabot` с пакетом `src/`, имя в pyproject — `tg-digest`.
3. **Эмбеддинги на Amvera**: по ТЗ §6 шаг 13 bge-m3 локально + whisper ≈ 3–4 ГБ ОЗУ.
   Решение (`EMBED_BACKEND=api` или дорогой тариф) нужно принять до шага 8.
