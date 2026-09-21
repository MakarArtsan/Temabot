-- Схема БД по docs/TZ.md §3.
-- Файл идемпотентный: применяется при каждом старте bot (шаг 13), не ломая данные.

create extension if not exists vector;

-- ---------------------------------------------------------------- чаты и люди

create table if not exists chats (
  id            bigserial primary key,
  tg_id         bigint unique not null,
  title         text,
  is_protected  boolean default false,
  -- управление из админки (TZ §4.8)
  collect       boolean default false,   -- userbot пишет сообщения
  digest        boolean default false,   -- делать дайджест
  copier        text    default 'ask',   -- allow | deny | ask
  digest_time   time    default '23:30',
  retention_days int    default 365,
  settings      jsonb   default '{}',    -- профиль интересов, веса, модель, пороги, промпты
  added_at      timestamptz default now(),
  constraint chats_copier_check check (copier in ('allow', 'deny', 'ask'))
);

create table if not exists authors (
  tg_user_id  bigint primary key,
  name        text,
  weight      real    default 1.0,      -- «вес мнения», правится в админке
  muted       boolean default false,    -- исключить из дайджеста
  hide_from_ratings boolean default false,  -- /optout или вручную
  updated_at  timestamptz default now()
);

create table if not exists copier_blocklist (
  tg_user_id bigint primary key,
  reason     text,
  created_at timestamptz default now()
);

create table if not exists settings (
  key        text primary key,
  value      jsonb,
  updated_at timestamptz default now()
);

create table if not exists llm_usage (
  id         bigserial primary key,
  chat_id    bigint,
  purpose    text,                      -- score|summary|qa|vision
  model      text,
  tokens_in  int,
  tokens_out int,
  cost_usd   numeric(10, 5),
  created_at timestamptz default now()
);
create index if not exists llm_usage_created_idx on llm_usage (created_at desc);

-- ------------------------------------------------------------------ сообщения

create table if not exists messages (
  id            bigserial primary key,
  chat_id       bigint references chats(id) on delete cascade,
  tg_msg_id     bigint not null,
  tg_user_id    bigint,
  author_name   text,
  text          text,
  transcript    text,                   -- расшифровка голосового
  media_type    text,                   -- voice|photo|doc|video|null
  media_path    text,
  reply_to      bigint,                 -- tg_msg_id родителя
  topic_id      bigint,                 -- forum topic, если включены
  thread_id     bigint,                 -- наш вычисленный тред
  source        text default 'collector',  -- collector|copier|manual
  is_pinned_by_me boolean default false,
  copy_count    int default 0,          -- сколько раз копировали через бота
  reactions     int default 0,          -- сумма реакций
  reply_count   int default 0,          -- сколько раз на сообщение ответили
  date          timestamptz not null,
  edited_at     timestamptz,
  -- мягкое удаление (TZ §4.1): дайджест за день должен остаться честным
  deleted_at    timestamptz,
  raw           jsonb,
  unique (chat_id, tg_msg_id)
);
create index if not exists messages_chat_date_idx on messages (chat_id, date desc);
create index if not exists messages_chat_thread_idx on messages (chat_id, thread_id);
create index if not exists messages_reply_idx on messages (chat_id, reply_to);
create index if not exists messages_fts_idx on messages
  using gin (to_tsvector('russian', coalesce(text, '') || ' ' || coalesce(transcript, '')));

-- ------------------------------------------------------------------- RAG-чанки

create table if not exists chunks (
  id          bigserial primary key,
  chat_id     bigint,
  thread_id   bigint,
  msg_ids     bigint[],
  text        text,
  embedding   vector(1024),
  date_from   timestamptz,
  date_to     timestamptz
);
-- В ТЗ был ivfflat с lists = 100, но такой индекс требует обучения на данных:
-- пока чанков меньше числа списков, поиск возвращает пустоту — то есть ровно в
-- первые дни работы, когда индекс только наполняется. HNSW работает с первой
-- строки и не нуждается в перестроении по мере роста базы.
create index if not exists chunks_embedding_idx on chunks
  using hnsw (embedding vector_cosine_ops);
create index if not exists chunks_chat_thread_idx on chunks (chat_id, thread_id);

-- -------------------------------------------------------------------- дайджест

create table if not exists digests (
  id          bigserial primary key,
  chat_id     bigint,
  day         date,
  summary_md  text,
  topics      jsonb,
  msg_count   int,
  tokens_used int,
  created_at  timestamptz default now(),
  unique (chat_id, day)
);

-- каждая тема дайджеста хранится отдельно, чтобы на неё можно было поставить оценку
create table if not exists digest_items (
  id          bigserial primary key,
  digest_id   bigint references digests(id) on delete cascade,
  chat_id     bigint,
  thread_id   bigint,
  title       text,
  kind        text,          -- decision|insight|resource|announcement|question|drama|other
  features    jsonb,         -- все сигналы и оценки, из которых посчитан score
  score       real,
  shown       boolean,       -- попало в дайджест или отсеяно порогом
  embedding   vector(1024)   -- для проверки новизны
);
create index if not exists digest_items_digest_idx on digest_items (digest_id);
create index if not exists digest_items_chat_thread_idx on digest_items (chat_id, thread_id);

create table if not exists feedback (
  id          bigserial primary key,
  item_id     bigint references digest_items(id) on delete cascade,
  value       smallint,      -- +1 полезно, -1 мимо, -2 «больше такое не показывать»
  note        text,
  created_at  timestamptz default now()
);

create table if not exists qa_log (
  id         bigserial primary key,
  question   text,
  answer     text,
  sources    bigint[],
  created_at timestamptz default now()
);

-- -------------------------------------------------------------------- рейтинги

-- вклад участника в тред (заполняет map-стадия дайджеста, TZ §4.10)
create table if not exists thread_contrib (
  chat_id     bigint,
  thread_id   bigint,
  tg_user_id  bigint,
  role        text,          -- initiator | key | answerer
  day         date,
  primary key (chat_id, thread_id, tg_user_id, role)
);

-- дневная статистика участника, неделя и месяц = сумма по дням
create table if not exists author_stats_daily (
  chat_id         bigint,
  tg_user_id      bigint,
  day             date,
  messages        int default 0,
  words           int default 0,
  longest_msg     int default 0,     -- слов в самом длинном сообщении
  voice_sec       int default 0,
  links           int default 0,
  replies_got     int default 0,
  reactions_got   int default 0,
  questions_answered int default 0,  -- закрыл чужой вопрос (по thread_contrib)
  threads_started int default 0,     -- начал тред, прошедший в дайджест
  night_msgs      int default 0,     -- 00:00–06:00 по TZ
  -- Номинация «💬 Самый активный» считает реплики до двух слов за 0.3 (TZ §4.10),
  -- поэтому их количество нужно хранить отдельно от общего числа сообщений.
  short_msgs      int default 0,
  usefulness      real default 0,    -- формула в TZ §4.10
  primary key (chat_id, tg_user_id, day)
);

-- Колонка появилась после первой версии схемы — добираем на существующих базах.
alter table author_stats_daily add column if not exists short_msgs int default 0;

create table if not exists state (key text primary key, value jsonb);  -- last_msg_id и пр.

-- ------------------------------------------------- приватность (TZ §9)

-- Supabase отдаёт таблицы схемы public наружу через PostgREST под ролями
-- anon/authenticated, а publishable-ключ по своей природе публичный: его вшивают
-- в браузерный код. Без этого блока содержимое закрытой группы читается по нему
-- любым, кто этот ключ увидел.
--
-- RLS без единой политики = полный запрет для anon/authenticated. Процессы
-- collector/bot/web ходят напрямую под ролью-владельцем, на неё RLS не действует.

do $$
declare t text;
begin
  foreach t in array array[
    'chats', 'authors', 'copier_blocklist', 'settings', 'llm_usage', 'messages',
    'chunks', 'digests', 'digest_items', 'feedback', 'qa_log', 'thread_contrib',
    'author_stats_daily', 'state'
  ] loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

-- Второй рубеж: снять гранты публичных ролей, если они вообще есть в этой БД
-- (на обычном Postgres ролей anon/authenticated не существует — блок пропускается).
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on all tables in schema public from anon, authenticated;
    revoke all on all sequences in schema public from anon, authenticated;
    alter default privileges in schema public revoke all on tables from anon, authenticated;
    alter default privileges in schema public revoke all on sequences from anon, authenticated;
  end if;
end $$;
