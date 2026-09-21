#!/usr/bin/env bash
# Локальный Postgres с pgvector для интеграционных тестов (TZ шаг 2).
#
#   ./scripts/test_db.sh start
#   TEST_DATABASE_URL="postgresql://postgres@127.0.0.1:5433/tgdigest_test" make test
#   ./scripts/test_db.sh stop
#
# На Debian/Ubuntu нужны пакеты: postgresql-16 postgresql-16-pgvector
set -euo pipefail

PGBIN="${PGBIN:-/usr/lib/postgresql/16/bin}"
PGDATA="${PGDATA:-/var/lib/postgresql/tgdigest-test}"
PGPORT="${PGPORT:-5433}"
DB_NAME="${DB_NAME:-tgdigest_test}"

case "${1:-start}" in
  start)
    if [ ! -d "$PGDATA/base" ]; then
      mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA"
      su postgres -c "$PGBIN/initdb -D $PGDATA -A trust -U postgres"
    fi
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-p $PGPORT -c listen_addresses=127.0.0.1' -l $PGDATA/pg.log start"
    sleep 2
    psql -h 127.0.0.1 -p "$PGPORT" -U postgres -qc "create database $DB_NAME;" 2>/dev/null || true
    psql -h 127.0.0.1 -p "$PGPORT" -U postgres -d "$DB_NAME" -qc "create extension if not exists vector;"
    echo "TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:$PGPORT/$DB_NAME"
    ;;
  stop)
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA stop" || true
    ;;
  *)
    echo "Использование: $0 [start|stop]" >&2
    exit 1
    ;;
esac
