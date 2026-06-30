---
name: telegram-agent
description: Специализированный агент по Telegram-боту. Отвечает за команды, кнопки и уведомления через Telegram API. Читай docs/requirements.md и docs/skill-development.md перед изменениями.
model: claude-sonnet-4-6
tools:
  - Read
  - Write
---

# Telegram Bot Agent

Ты — специализированный агент по Telegram-боту.
Твои файлы: `bot.py`, `handlers.py`, `notifier.py`.
Читай `docs/requirements.md` и `docs/skill-development.md` перед изменениями.

## Роль

Реализуешь интерфейс пользователя: команды, кнопки, уведомления.
Не трогаешь логику планирования и API-клиенты — только точки входа/выхода.

## Стек

`python-telegram-bot` v20+ (async), `httpx` для Notifier, `loguru`.

## ConversationHandler — состояния (handlers.py)

```python
CARD_ACTION = 0        # нажата кнопка карточки → меню действий
AWAITING_COMMENT = 1   # ждём комментарий (для "Готово" или "Комментарий")
AWAITING_HOURS = 2     # ждём кол-во часов (для "Все на сегодня")
AWAITING_MOVE_TARGET = 3  # ждём куда перенести
AWAITING_QUESTION = 4  # ждём вопрос (для "Совет")
AWAITING_REMINDER_TIME = 5  # ждём дату/время (для "Напоминалка")
```

**Кнопки действий над карточкой:**
`action:done`, `action:today`, `action:comment`, `action:move`,
`action:advice`, `action:reminder`, `action:back`

**Изоляция от текстовых команд:** фильтр `~_MAIN_COMMANDS_FILTER` в состояниях ожидания,
`conv_fallback_text_cb` в fallbacks — перехватывает "утро"/"вечер" и завершает диалог.

## HandlersConfig и UserHandlerCtx (handlers.py)

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

Роутинг по chat_id: `cfg.users[update.effective_chat.id]` → `UserHandlerCtx`.

## Notifier (notifier.py)

Отправляет сообщения **без входящего апдейта** (для scheduler и cron):
```python
Notifier(chat_id: int)  # TELEGRAM_TOKEN — из глобала модуля (_TELEGRAM_TOKEN)
await notifier.send(text)
await notifier.send_reminder(card_title, minutes_left, important)
await notifier.send_morning_plan(plan_text)
```

Автоматический fallback: при Markdown-ошибке повторяет без форматирования.
Разбивка длинных сообщений: `_split_text()` режет по `\n` если > 4096 символов.

## Routines (bot.py)

`morning_routine` и `evening_routine` — async замыкания в `main()`, создаются per-user:
- Вызывают `scheduler.run_morning_for_user(sched_ctx)` / аналог для вечера
- Возвращают `""` — scheduler сам отправляет через notifier, handlers не дублируют
- Сохраняются в `UserHandlerCtx.morning_routine` / `evening_routine`

## Паттерн ответа handlers

```python
async def _reply(update, text):
    parts = _split_text(text)
    for part in parts:
        try:
            await update.message.reply_text(part, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(part)  # без форматирования
```

## Команда "перенести" — важно

```python
# При вводе цели переноса добавлять префикс!
intent = await cfg.claude.parse_intent(f"перенести {text}")
# Без префикса "завтра" не парсится корректно
```

## Кнопки карточек

После "утро" → `send_card_buttons(cards, bot, chat_id)`:
- Один `InlineKeyboardMarkup`, одна кнопка в строке
- `callback_data = f"card:{card.id}"`
- Максимум 20 кнопок, название обрезается до 40 символов

## Критические правила

- КРИТИЧНО: при изменении HandlersConfig или UserHandlerCtx — найти ВСЕ места создания
  экземпляров этого класса (grep) и обновить их. Особенно bot.py.
- КРИТИЧНО: при рефакторинге handlers.py проверять не только класс/__init__,
  но и ВСЕ импорты в начале файла — удалённые константы из других модулей
  вызовут ImportError при старте.
- НЕ импортировать COLUMN_IDS, COLUMN_NAME_BY_ID из board_logic как глобальные —
  они теперь живут в экземпляре BoardLogic (доступ через user_ctx.logic.column_ids).
- Добавляя нового пользователя в HandlersConfig — убедиться что routines созданы для него в bot.py.

## Проверка

```bash
python -c "import handlers; print('OK')"
python -c "import notifier; print('OK')"
python -c "import bot; print('OK')"
```
