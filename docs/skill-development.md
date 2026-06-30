# Skill Development — AI Personal Planner

Мастер-референс по паттернам, соглашениям и архитектуре проекта.
**Читать перед любыми изменениями.**

## Для кого этот документ

| Агент/Роль | Что читать в этом документе |
|---|---|
| **Оркестратор** | Всё — особенно «Правила рефакторинга», «Интерфейсы», «Поток данных» |
| **Kaiten-агент** | «Имена колонок», «Формат разделителей», «KaitenClient», «BoardLogic» |
| **Scheduler-агент** | «Scheduler», «db.py», «MorningLogic», «Понедельничная логика» |
| **Telegram-агент** | «HandlersConfig», «UserHandlerCtx», «Notifier», «bot.py» |
| **Claude API агент** | Паттерны промптов описаны в `agents/claude-api-agent.md` |
| **Retro-агент** | Всё — определяет что устарело и что нужно обновить на основе ошибок |
| **Deploy-агент** | «Правила рефакторинга» — что проверять перед деплоем |

---

## Архитектура (мульти-пользовательская, v2)

### Поток данных

```
users.json / env → load_users() → list[UserConfig]
  ↓ bot.py main()
для каждого user:
  KaitenClient(board_id, lane_id, token, base_url)  # per-user
  setup_board(client, user) → column_ids    # пропуск если user.column_ids задан
  BoardLogic(client, column_ids)            # per-user
  MorningLogic(client, logic)              # per-user
  Notifier(chat_id)                         # per-user
  → UserSchedulerCtx, UserHandlerCtx

Scheduler(users=[...], claude)             # один на всех, итерирует
HandlersConfig(users={chat_id: ctx}, claude)  # роутинг по chat_id
```

### Ключевые файлы

| Файл | Ответственность |
|---|---|
| user_config.py | UserConfig dataclass, load_users(), REQUIRED_COLUMN_NAMES |
| board_setup.py | Идемпотентная настройка доски, alias-маппинг имён |
| kaiten_client.py | HTTP-клиент, KaitenClient(board_id, lane_id, token, base_url) |
| board_logic.py | BoardLogic(client, column_ids), WEEKDAY_COLUMNS |
| morning_logic.py | Алгоритм утра v4, MorningLogic(client, logic) |
| scheduler.py | APScheduler джобы, UserSchedulerCtx, мульти-user цикл |
| handlers.py | Telegram handlers, HandlersConfig, UserHandlerCtx |
| notifier.py | Notifier(chat_id) — отправка без апдейта |
| db.py | SQLite флаги, daily_flags(date, user_id) |

---

## Реальные имена колонок Kaiten

**КРИТИЧНО:** код использует ПОЛНЫЕ имена, не аббревиатуры. Это должно совпадать с реальной доской.

```python
WEEKDAY_COLUMNS = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"
]

REQUIRED_COLUMN_NAMES = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
    "Следующая неделя", "Далекие времена", "Долгий ящик", "Архив",
]

# ID колонок (производственные, board_id=476640)
COLUMN_IDS = {
    "Понедельник": 1688101, "Вторник": 1689798, "Среда": 1689899,
    "Четверг": 1689903, "Пятница": 1689912, "Суббота": 6122424, "Воскресенье": 6122425,
    "Следующая неделя": 6122270, "Далекие времена": 6122271,
    "Долгий ящик": 1688100, "Архив": 6122269,
}
```

Колонка называется **"Далекие времена"** — не "Далёкое будущее", не "Далёкие времена".

---

## Формат разделителей (КРИТИЧНО)

Разделители — карточки с `blocked=True`. Формат:
```python
title = "-"                              # НЕ название секции!
block_reason = "Утро" | "День" | "Вечер" | "На контроле"
blocked = True
```

Разделители существуют ТОЛЬКО в колонках дней (Пн–Вс). Не в "Следующая неделя".

---

## Безопасность: секреты всегда в env

**КРИТИЧНО: никогда не хранить токены/пароли напрямую в конфиг-файлах (users.json и т.д.).**

Паттерн: в конфиге хранится только **имя** env-переменной, значение читается при старте.

```json
// users.json — ПРАВИЛЬНО
{
  "kaiten_token_env": "KAITEN_TOKEN_ALICE",
  "kaiten_base_url_env": "KAITEN_BASE_URL_ALICE"
}
```

```python
# user_config.py — разрешение ссылки при загрузке
token_env = item.get("kaiten_token_env")        # "KAITEN_TOKEN_ALICE"
kaiten_token = os.getenv(token_env) if token_env else None  # реальный токен из env
```

Правило: **значение секрета** живёт только в env (Railway Variables / .env). В любом файле, который попадает в git или передаётся между агентами, хранится лишь имя переменной.

---

## Мульти-пользовательские интерфейсы

### KaitenClient
```python
KaitenClient(
    board_id: int,
    lane_id: int,
    token: str | None = None,    # если None → fallback на KAITEN_TOKEN из env
    base_url: str | None = None, # если None → fallback на KAITEN_BASE_URL из env
)
# token и base_url — per-user (берутся из UserConfig.kaiten_token / .kaiten_base_url)
# Пример создания в bot.py:
# KaitenClient(board_id=user.kaiten_board_id, lane_id=user.kaiten_lane_id,
#              token=user.kaiten_token, base_url=user.kaiten_base_url)
```

### BoardLogic
```python
BoardLogic(client: KaitenClient, column_ids: dict[str, int])
# column_ids — маппинг "Понедельник" → id, etc.
# Доступ: logic.column_ids["Понедельник"], logic.column_name_by_id[1688101]
# WEEKDAY_COLUMNS — импортировать из board_logic, не хардкодить
```

### Notifier
```python
Notifier(chat_id: int)  # токен из _TELEGRAM_TOKEN глобала модуля
```

### db.py
```python
# Все функции принимают user_id
is_morning_done(date: str, user_id: str = "default") -> bool
set_morning_done(date: str, user_id: str = "default") -> None
# Аналогично: is_evening_done, set_evening_done, get_flags, reset_flags
```

### HandlersConfig
```python
@dataclass
class UserHandlerCtx:
    user_id: str
    kaiten: KaitenClient
    logic: BoardLogic
    notifier: Notifier
    morning_routine: Callable[[], Awaitable[None]]
    evening_routine: Callable[[], Awaitable[None]]

@dataclass
class HandlersConfig:
    users: dict[int, UserHandlerCtx]  # telegram_chat_id → контекст
    claude: ClaudeClient
```

### Scheduler
```python
@dataclass
class UserSchedulerCtx:
    user_cfg: UserConfig
    morning: MorningLogic
    notifier: Notifier
    kaiten: KaitenClient
    logic: BoardLogic

Scheduler(users: list[UserSchedulerCtx], claude: ClaudeClient)
# Публичный метод для ручного запуска:
await scheduler.run_morning_for_user(user_sched_ctx)
```

---

## board_setup.py — логика настройки доски

При старте: если `user.column_ids` не пустой → board_setup пропускается полностью.

**Alias-маппинг (`_NAME_ALIASES`):** нестандартные имена → стандартные.
Например, "Пн" → "Понедельник". Alias всегда перезаписывает точное совпадение.

**Идемпотентность:**
- Если колонка/разделитель уже есть → не создаёт дубль
- Удаляет лишние колонки ТОЛЬКО если они ПУСТЫЕ
- Дублирующие стандартные колонки (после alias-маппинга) — удаляются если пустые

**Пропуск setup:** если `user.column_ids` задан в users.json → setup не вызывается.
Это предотвращает случайное создание дублей на уже настроенных досках.

---

## Правила безопасного рефакторинга

### Перед удалением/переименованием константы из модуля

```bash
# ОБЯЗАТЕЛЬНО: найти все использования в проекте перед удалением
grep -r "ИМЯ_КОНСТАНТЫ" *.py
```

Если константа используется в нескольких файлах — обновить ВСЕ в рамках одного PR.
Особенно опасно: COLUMN_IDS, ARCHIVE_COLUMN_ID, WEEKDAY_COLUMNS, COLUMN_NAME_BY_ID.

### При изменении сигнатуры класса

Найти все места создания экземпляров:
```bash
grep -r "ClassName(" *.py
```

Особенно критично для: `KaitenClient()`, `BoardLogic()`, `Notifier()`, `HandlersConfig()`, `UserHandlerCtx()`.

### При изменении публичного интерфейса модуля

Если убираешь экспорт из модуля (константу, класс, функцию):
```bash
grep -r "from module_name import" *.py
grep -r "import module_name" *.py
```

### Финальная проверка перед деплоем

```bash
python -c "import bot, handlers, morning_logic, scheduler, kaiten_client, board_logic, claude_client, notifier, db, user_config; print('ALL IMPORTS OK')"
```

Эта команда проверяет весь граф импортов сразу.

---

## Добавление нового пользователя

1. Создать (или обновить) `users.json`:
```json
[
  {
    "user_id": "alice",
    "telegram_chat_id": 123456789,
    "kaiten_board_id": 999999,
    "kaiten_lane_id": 0,
    "kaiten_space_id": 197396,
    "kaiten_token_env": "KAITEN_TOKEN_ALICE",
    "kaiten_base_url_env": "KAITEN_BASE_URL_ALICE"
  }
]
```
2. Добавить `KAITEN_TOKEN_ALICE` и `KAITEN_BASE_URL_ALICE` в Railway Variables (или .env)
3. `kaiten_lane_id: 0` → определится автоматически при board_setup
4. Перезапустить сервис → board_setup создаст колонки и разделители

Если доска уже настроена (известны ID колонок) — добавить `column_ids` в users.json,
тогда board_setup будет пропущен.

---

## Протокол деплоя

1. Оркестратор обновляет `docs/requirements.md` (новые фичи/изменения)
2. Параллельно вызывает `deploy` и `retro` агентов:
   - `deploy` — проверяет импорты → коммитит → пушит (или возвращает BLOCKED)
   - `retro` — получает список ошибок сессии → обновляет документацию
3. Если deploy вернул BLOCKED → оркестратор делегирует правку нужному субагенту → повторяет деплой
