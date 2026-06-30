# Skill Development — AI Personal Planner

Паттерны кода, соглашения и архитектурные решения. Читать перед изменениями.

## Архитектура (мульти-пользовательская, v2)

### Поток данных

```
users.json / env → load_users() → list[UserConfig]
  ↓ bot.py main()
для каждого user:
  KaitenClient(board_id, lane_id)           # per-user
  setup_board(client, user) → column_ids    # пропуск если user.column_ids задан
  BoardLogic(client, column_ids)            # per-user
  MorningLogic(client, logic)               # per-user
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
| kaiten_client.py | HTTP-клиент, KaitenClient(board_id, lane_id) |
| board_logic.py | BoardLogic(client, column_ids), WEEKDAY_COLUMNS |
| morning_logic.py | Алгоритм утра v4, MorningLogic(client, logic) |
| scheduler.py | APScheduler джобы, UserSchedulerCtx, мульти-user цикл |
| handlers.py | Telegram handlers, HandlersConfig, UserHandlerCtx |
| notifier.py | Notifier(chat_id) — отправка без апдейта |
| db.py | SQLite флаги, daily_flags(date, user_id) |

## Реальные имена колонок Kaiten

КРИТИЧНО: код использует ПОЛНЫЕ имена, не аббревиатуры.

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

## Формат разделителей (КРИТИЧНО)

Разделители — карточки с `blocked=True`. Формат:
```python
title = "-"                              # НЕ название секции!
block_reason = "Утро" | "День" | "Вечер" | "На контроле"
blocked = True
```

Разделители существуют ТОЛЬКО в колонках дней (Пн–Вс). Не в "Следующая неделя".

## Мульти-пользовательские интерфейсы

### KaitenClient
```python
KaitenClient(board_id: int, lane_id: int)
# KAITEN_TOKEN, KAITEN_BASE_URL, KAITEN_SPACE_ID — из env (общие для всех)
# board_id и lane_id — per-user
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

## board_setup.py — логика настройки доски

При старте: если `user.column_ids` не пустой → board_setup пропускается.

Alias-маппинг (`_NAME_ALIASES`): нестандартные имена → стандартные.
Например, если кто-то создал колонку "Пн" вместо "Понедельник".

Идемпотентность: если колонка/разделитель уже есть → не создаёт дубль.
Удаляет лишние колонки ТОЛЬКО если они ПУСТЫЕ.

## Правила безопасного рефакторинга

### Перед удалением/переименованием константы
```bash
# Обязательно: найти все использования в проекте
grep -r "ИМЯ_КОНСТАНТЫ" *.py
```
Если константа используется в нескольких файлах — обновить ВСЕ в рамках одного PR.

### При изменении сигнатуры класса
Найти все места создания экземпляров: `grep -r "ClassName("` по всем .py.
Особенно критично для: KaitenClient(), BoardLogic(), Notifier(), HandlersConfig(), UserHandlerCtx().

### При изменении импортов модуля
Если убираешь экспорт из модуля (константу, класс, функцию) — найти все файлы
которые её импортируют: `grep -r "from module import"` и `grep -r "import module"`.

### После каждой волны изменений
Перед деплоем запустить: `python -c "import bot"` — это проверит весь граф импортов.

## Добавление нового пользователя

1. Создать (или обновить) `users.json`:
```json
{
  "user_id": "alice",
  "telegram_chat_id": 123456789,
  "kaiten_board_id": 999999,
  "kaiten_lane_id": 0,
  "kaiten_space_id": 197396
}
```
2. `kaiten_lane_id: 0` → определится автоматически при board_setup
3. Перезапустить сервис → board_setup создаст колонки и разделители

Если доска уже настроена (известны ID колонок) — добавить `column_ids` в users.json,
тогда board_setup будет пропущен.
