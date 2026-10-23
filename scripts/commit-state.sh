#!/usr/bin/env bash
# Сохраняет состояние (state/), тексты (content/), журналы (logs/), отчёты (previews/) и настройки
# в репозиторий. При гонке с другими изменениями версия ЭТОГО запуска для state.json всегда побеждает.
set -euo pipefail
BRANCH="${GITHUB_REF_NAME:-main}"
git config user.name "autopost-bot"
git config user.email "autopost-bot@users.noreply.github.com"

paths=()
for p in state content logs previews queue ops config/settings.yaml; do
  [ -e "$p" ] && paths+=("$p")
done
[ ${#paths[@]} -eq 0 ] && { echo "Нечего сохранять"; exit 0; }
git add -A -- "${paths[@]}"
if git diff --cached --quiet; then
  echo "Изменений нет"
  exit 0
fi
git commit -q -m "$1"

keep="$(mktemp -d)"
[ -f state/state.json ] && cp state/state.json "$keep/state.json"

for i in 1 2 3 4 5; do
  if git push -q origin "HEAD:$BRANCH"; then echo "Сохранено"; exit 0; fi
  echo "Отправка отклонена, обновляюсь (попытка $i)"
  git fetch -q origin "$BRANCH"
  if ! git rebase -q -X theirs "origin/$BRANCH"; then
    git rebase --abort || true
    echo "Не удалось совместить изменения" >&2
    exit 1
  fi
  # состояние этого запуска важнее: возвращаем его целиком и проверяем, что JSON корректен
  if [ -f "$keep/state.json" ]; then
    cp "$keep/state.json" state/state.json
    python3 -c "import json; json.load(open('state/state.json'))"
    git add state/state.json
    git diff --cached --quiet || git commit -q -m "$1 (повторная отправка)"
  fi
  sleep $((i * 3))
done
echo "Не удалось сохранить состояние" >&2
exit 1
