# Прогресс работ

Сверяться перед каждой итерацией и обновлять после. Нумерация шагов — из `docs/TZ.md` §6.
Легенда: ✅ сделано · 🟡 частично · ⬜ не начато

| Шаг | Что | Статус |
|---|---|---|
| 0 | Доступы (api_id/hash, ID группы, ключ LLM, Postgres+pgvector) | ⬜ на стороне владельца |
| 1 | Скелет: pyproject, config, .env.example, пустые модули, ruff/mypy, Makefile | ✅ |
| 2 | БД: `schema.sql` + `repo.py` + тесты | ✅ |
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

## Сделано на шаге 2

- `src/db/schema.sql` — схема из TZ §3, **идемпотентная** (`if not exists` + именованные
  индексы), применяется при каждом старте без вреда данным.
- `src/db/pool.py` — пул asyncpg, кодек jsonb (в коде `dict`, не строка). SQL здесь не пишется.
- `src/db/models.py` — датаклассы строк + `Message.content` (текст и расшифровка равноправны).
- `src/db/repo.py` — единственное место с SQL: чаты, авторы, сообщения, state, дайджесты,
  учёт токенов.
- `src/db/migrate.py` + `make migrate` — применение схемы, отметка в `state.schema_applied_at`.
- 18 интеграционных тестов на настоящем Postgres 16 + pgvector 0.6. Без `TEST_DATABASE_URL`
  они skip, поэтому `make test` не падает на машине без базы.

**Решения, которых не было в ТЗ явно:**
1. Добавлена колонка `messages.deleted_at` — §4.1 требует мягкого удаления, а в схеме §3
   поля под него не было.
2. `upsert_message` не затирает поля, которые пишут другие процессы: `transcript`,
   `thread_id`, `is_pinned_by_me`, `copy_count`, `reactions`. Иначе бэкфилл поверх
   собранного стирал бы расшифровки и пометки копировщика.
3. Сутки считаются по локальной таймзоне (`Asia/Kamchatka`), а не по UTC — иначе дайджест
   за день режется на 12 часов раньше. Покрыто тестом.
4. `src/db/pool.py` — отдельный модуль (в §2 его нет). Правило «SQL только в repo.py»
   не нарушено: в пуле лежит только управление соединениями.
5. `chats.copier` ограничен `check (allow|deny|ask)`.

## Открытые вопросы к владельцу

1. **Шаг 0 не выполнен** — нужны `TG_API_ID`/`TG_API_HASH`, ID группы, ключ LLM-провайдера
   и строка подключения к Postgres с pgvector. Без них шаги 3+ нельзя проверить на живых данных.
2. **Имя пакета/проекта**: в ТЗ каталог называется `tg-digest`, репозиторий — `Temabot`.
   Собрано в корне `Temabot` с пакетом `src/`, имя в pyproject — `tg-digest`.
3. **Эмбеддинги на Amvera**: по ТЗ §6 шаг 13 bge-m3 локально + whisper ≈ 3–4 ГБ ОЗУ.
   Решение (`EMBED_BACKEND=api` или дорогой тариф) нужно принять до шага 8.
