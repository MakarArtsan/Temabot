#!/usr/bin/env bash
# Выкладка на Amvera пушем прямо в git-сервер проекта.
#
# Вебхук GitHub → Amvera у этого проекта не срабатывает, поэтому выкладываем сами:
# берём master с Amvera и кладём поверх него merge-коммит с деревом нашей ветки.
# Родителей два — наш HEAD и нынешний master Amvera: история не переписывается,
# force-push не нужен. Amvera видит новый коммит на master и пересобирает проект.
#
#   ./scripts/deploy_amvera.sh           # после commit и push в GitHub
#   ./scripts/deploy_amvera.sh --ours    # заменить и то, что поменяли через интерфейс Amvera
#
# Пароль — только из окружения (AMVERA_PASSWORD, логин — AMVERA_USER или artemmaka)
# через GIT_ASKPASS: ни в git config, ни в адрес remote, ни в историю команд он не
# попадает. DEPLOY_TRAILER — необязательные строки в конец сообщения коммита.
set -euo pipefail

URL="${AMVERA_GIT_URL:-https://git.waw0.amvera.ru/artemmaka/temabot}"

if [ -z "${AMVERA_PASSWORD:-}" ]; then
  echo "Не задан AMVERA_PASSWORD" >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "Есть незакоммиченные изменения — сначала commit и push в GitHub" >&2
  exit 1
fi

askpass="$(mktemp)"
trap 'rm -f "$askpass"' EXIT
cat > "$askpass" <<'EOF'
#!/bin/sh
case "$1" in
  Username*) echo "${AMVERA_USER:-artemmaka}" ;;
  Password*) echo "$AMVERA_PASSWORD" ;;
esac
EOF
chmod 700 "$askpass"
export GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0

git fetch --quiet "$URL" master
remote="$(git rev-parse FETCH_HEAD)"
ours="$(git rev-parse HEAD)"

if [ "$(git rev-parse "$remote^{tree}")" = "$(git rev-parse "$ours^{tree}")" ]; then
  echo "На Amvera уже этот код ($(git rev-parse --short "$ours")) — выкладывать нечего"
  exit 0
fi

# Что изменили на стороне Amvera (например, настройки через её интерфейс) и что
# выкладка заменит нашей версией: такое сначала переносят в репозиторий.
base="$(git merge-base "$ours" "$remote" || true)"
if [ -n "$base" ] && [ "${1:-}" != "--ours" ]; then
  replaced="$(git diff --name-only "$base" "$remote" | while read -r path; do
    git diff --quiet "$remote" "$ours" -- "$path" || echo "$path"
  done)"
  if [ -n "$replaced" ]; then
    echo "На Amvera изменены файлы, которые выкладка заменит нашей версией:" >&2
    echo "$replaced" >&2
    echo "Перенесите изменения в репозиторий или запустите с --ours" >&2
    exit 1
  fi
fi

message="Выкладка $(git rev-parse --short "$ours") из GitHub: $(git log -1 --format=%s "$ours")"
if [ -n "${DEPLOY_TRAILER:-}" ]; then
  message="$message

$DEPLOY_TRAILER"
fi
merge="$(git commit-tree "$ours^{tree}" -p "$ours" -p "$remote" -m "$message")"
git push --quiet "$URL" "$merge:refs/heads/master"
echo "Готово: $(git rev-parse --short "$merge") на master Amvera — сборка началась"
