# Настройка: что такое `.env` и как его заполнить

`.env` — обычный текстовый файл в корне проекта, строки вида `КЛЮЧ=значение`.
Программа читает из него пароли и ключи, поэтому их не приходится писать в коде.
Файл уже указан в `.gitignore` — в GitHub он не попадёт. Никому его не показывай
и не присылай в переписку.

## Как создать

В папке проекта:

```bash
cp .env.example .env
```

Теперь открой `.env` любым текстовым редактором (Блокнот, VS Code) и подставь значения
после знака `=`. Без кавычек, без пробелов вокруг `=`. Строки, начинающиеся с `#`, —
комментарии, их можно не трогать.

## Что уже известно

```env
TG_GROUP_ID=-1002354231333
OWNER_ID=132036441
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_REASONING_EFFORT=
TZ=Asia/Kamchatka
```

## Что нужно получить

| Ключ | Где взять |
|---|---|
| `TG_API_ID`, `TG_API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools → создать приложение. Выдадут число `api_id` и строку `api_hash` |
| `BOT_TOKEN` | @BotFather → твой бот-копировщик → `/token` (лучше перевыпустить: старый лежал в коде) |
| `LLM_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) → API keys |
| `DATABASE_URL` | Supabase → проект → Connect → строка вида `postgresql://postgres:ПАРОЛЬ@db.xxx.supabase.co:5432/postgres` |
| `TELEGRAPH_TOKEN` | оставь пустым: при первом запуске бот создаст аккаунт и напишет токен в лог — тогда впишешь |
| `TG_SESSION_STRING` | оставь пустым для локального запуска; для деплоя получишь через `make login` (см. ниже) |
| `WEB_SECRET_KEY` | любая длинная случайная строка, например из `python3 -c "import secrets; print(secrets.token_hex(32))"` |

## Первый вход в Telegram-аккаунт

```bash
make install-dev
make login
```

Telethon спросит номер телефона, потом код из Telegram и пароль двухфакторки.
После входа скрипт напечатает:

- **строку сессии** (`TG_SESSION_STRING`) — это полный доступ к твоему аккаунту.
  Она нужна только для деплоя на Amvera, где код ввести некуда. Хранить её можно
  только в секретах Amvera. Не коммитить, не пересылать, в лог не писать;
- **список групп с их id** — так можно проверить, что `TG_GROUP_ID` верный.

Файл `data/collector.session` после этого появится на диске с правами `600`.
Он равнозначен паролю от аккаунта.

## Проверка без записи в базу

```bash
make migrate                      # создать таблицы
python -m src.collector --dry-run # печатает приходящие сообщения в консоль
```

`--dry-run` ничего не пишет в БД — на нём удобно убедиться, что аккаунт видит группу
и сообщения разбираются правильно.

## Запуск

```bash
make run-collector   # сбор сообщений (строго один экземпляр!)
make run-bot         # бот в личке
```

**Важно:** две копии коллектора с одной сессией дают `AUTH_KEY_DUPLICATED`, и Telegram
может сбросить авторизацию. При обновлении — сначала остановить, потом запустить.

## Тесты

```bash
make check                        # ruff + mypy + тесты
./scripts/test_db.sh start        # поднять локальный Postgres с pgvector
TEST_DATABASE_URL="postgresql://postgres@127.0.0.1:5433/tgdigest_test" make test
```
