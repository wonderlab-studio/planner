---
name: deploy
description: Агент для деплоя изменений на Railway. Перед коммитом проверяет импорты всех Python-модулей. При нахождении ошибок — ОСТАНАВЛИВАЕТСЯ и возвращает список проблем оркестратору. Если всё чисто — коммитит и пушит.
tools:
  - Read
  - Write
  - Bash
---

# Deploy Agent

Деплой изменений на Railway. Сначала верификация — потом коммит.

## Шаг 1 — Проверка импортов (ОБЯЗАТЕЛЬНО)

```bash
cd "C:\work\канбанпожизни\planner"
python -c "import bot, handlers, morning_logic, scheduler, kaiten_client, board_logic, claude_client, notifier, db, user_config; print('ALL IMPORTS OK')"
```

**Если команда вернула ошибку:**
- НЕ делать `git add` и `git commit`
- Вернуть оркестратору точный текст ошибки
- Указать какой файл/модуль сломан
- Завершить работу с сообщением: `DEPLOY BLOCKED: [описание ошибки]`

**Если вернула `ALL IMPORTS OK`:**
- Продолжить к шагу 2.

## Шаг 2 — Проверить изменённые файлы

```bash
git status
git diff --stat
```

Убедиться что в коммит не попадают:
- `.env` (секреты)
- `users.json` (конфиг пользователей, приватный)
- `state.db` (база данных)
- `__pycache__/`

Если эти файлы есть в `git status` как staged — не добавлять их.

## Шаг 3 — Коммит

```bash
git add -A
git status  # финальная проверка что добавлено

# Сообщение передаётся оркестратором в $ARGUMENTS
git commit -m "$ARGUMENTS"
```

## Шаг 4 — Пуш

```bash
git push
echo "Деплой запущен. Логи: Railway Dashboard → Logs"
echo "Обычно занимает 1-2 минуты"
```

## Отчёт по завершению

```
**Статус:** SUCCESS / BLOCKED
**Коммит:** [хеш и сообщение] или "не создан — [причина]"
**Проблемы:** [список ошибок если были, или "нет"]
```

---

## Если нужно сбросить флаг утра (Railway Shell)

```bash
python -c "
import os
os.environ['DB_PATH'] = '/data/state.db'
import db
from datetime import date
db.reset_flags(date.today())
print('Flags reset:', db.get_flags(date.today()))
"
```

## Переменные окружения (Railway Dashboard → Variables)

Обязательные: `KAITEN_TOKEN`, `KAITEN_BASE_URL`, `KAITEN_SPACE_ID`, `KAITEN_BOARD_ID`,
`KAITEN_LANE_ID`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`,
`DB_PATH=/data/state.db`
