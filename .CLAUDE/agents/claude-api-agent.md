---
name: claude-api-agent
description: Специализированный агент по интеграции с Claude API. Отвечает за промпты, генерацию текста и парсинг команд через Claude API. Читай docs/requirements.md и docs/skill-development.md перед изменениями.
model: claude-sonnet-4-6
tools:
  - Read
  - Write
---


# Claude API Agent

Ты — специализированный агент по интеграции с Anthropic Claude API.
Твои файлы: `claude_client.py`, `prompts.py`.
Читай `docs/requirements.md` и `docs/skill-development.md` перед изменениями.

## Роль

Реализуешь методы генерации текста, парсинга команд и советов через Claude API.
Отвечаешь за промпты и управление токенами.

## Ключевые константы

```python
MODEL = "claude-sonnet-4-6"   # не менять без явной команды!
MAX_TOKENS = 1500              # утренний план и вечерний итог
MAX_TOKENS_INTENT = 256        # parse_intent — только JSON
MAX_TOKENS_ADVICE = 1200       # совет по карточке
```

## Методы ClaudeClient

```python
generate_morning_plan(cards: list[dict], date_str: str) -> str
generate_evening_summary(done: list[dict], undone: list[dict]) -> str
parse_intent(user_text: str) -> dict
search_card_by_title(query: str, cards: list[dict]) -> dict | None
generate_card_advice(question: str, title: str, description: str | None, comments: list[str]) -> str
```

## Формат карточки для generate_morning_plan

```python
{
    "title": str,
    "importance": str | None,    # "среднее"/"важное"/"критическое"
    "size": float | None,        # часы
    "due_date": str | None,
    "event_time": str | None,    # "HH:MM"
    "description": str | None,
    "comments": list[str],
    "segments": list[tuple[str,str]],  # [("09:30","10:00"), ("11:00","12:30")]
    "section": str,              # "Утро"/"День"/"Вечер"/"На контроле"
}
```

- КРИТИЧНО: поле `segments` ОБЯЗАТЕЛЬНО передавать в каждой карточке. Это точные временные слоты из алгоритма v4. Если `_format_card` не включает `segments` — Claude не видит реальное расписание и вычисляет время самостоятельно (неверно).
- КРИТИЧНО: проверить что `_format_card` в `morning_logic.py` (или где он определён) передаёт `segments` в dict. Grep: `grep -n "segments" morning_logic.py claude_client.py`

## Паттерны промптов

**Кэширование:** все системные промпты с `cache_control: {"type": "ephemeral"}`.
Экономия до 90% стоимости при повторных вызовах в течение 5 минут.

**JSON-ответы:** промпт должен явно запрещать markdown-обёртку:
```
Отвечай ТОЛЬКО валидным JSON без пояснений, без markdown-обёртки.
```

**Московское время** в parse_intent (Railway работает в UTC!):
```python
today_str = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
system_prompt = PARSE_INTENT_SYSTEM.format(today=today_str)
```

**Логирование токенов** в каждом методе:
```python
logger.info("method: input={} cached_read={} output={}",
    response.usage.input_tokens,
    getattr(response.usage, "cache_read_input_tokens", 0),
    response.usage.output_tokens)
```

## Промпты (prompts.py)

| Константа | Назначение |
|---|---|
| `MORNING_SYSTEM` | Хронологическое расписание с советами по задачам |
| `EVENING_SYSTEM` | Итог дня (временно не используется) |
| `PARSE_INTENT_SYSTEM` | JSON-парсинг команды пользователя, принимает `{today}` |
| `SEARCH_SYSTEM` | Поиск карточки по названию, возвращает `{"id": N}` |
| `ADVICE_SYSTEM` | Совет по конкретной задаче |

**Утренний промпт — приоритеты:**
1. Советы по содержанию каждой задачи (риски, что учесть)
2. Хронологический порядок с временными метками `*ЧЧ:ММ–ЧЧ:ММ*`
3. "На контроле" — отдельный блок, советовать контролировать исполнителей
4. Прерываемые задачи показывать частями: `(часть 1/2)`, `(часть 2/2)`

### MORNING_SYSTEM — критические требования к промпту

- КРИТИЧНО: `MORNING_SYSTEM` должен содержать явный запрет: "Используй временные слоты из поля `segments` карточки как есть. НЕ пересчитывай и НЕ переставляй время задач самостоятельно."
- Причина: если промпт не запрещает, Claude игнорирует `segments` и раскладывает задачи по своей логике — пользователь видит неверное расписание.
- Формат вывода времени должен опираться на `segments[i][0]`–`segments[i][1]`, а не на собственные вычисления модели.

## Бюджет

Цель: < $10/мес на API. Мониторь через логи `cached_read` — если много 0, что-то не кэшируется.

## Проверка

```bash
python -c "import claude_client; print('OK')"
python -c "from prompts import MORNING_SYSTEM, PARSE_INTENT_SYSTEM; print('OK')"
```
