---
name: skill-development
description: Занимается разработкой и обновлениями продукта - планировщика. Читает требования, тестирует, деплоит и пишет код при помощи группы субагентов. Проверяет код и руководит субагентами. Ведёт документацию по паттернам и соглашениям в проекте. Использует субагентов где это возможно, чтобы не писать код самому. Использовать скилл когда просят что-то разработать, доработать сервис, исправить баги, протестировать, задеплоить.
---

# Skill: Development — AI Personal Planner

Паттерны и соглашения принятые в этом проекте. Читай перед любыми изменениями в коде.

---

## Стиль кода

- Python 3.11+, `from __future__ import annotations` в каждом файле
- Async/await везде где есть I/O (httpx, anthropic, telegram)
- Логирование только через `loguru` (`from loguru import logger`)
- Конфиг только через `.env` + `python-dotenv`
- Dataclasses для моделей данных (`@dataclass`, `field(default_factory=...)`)

---

## Обработка ошибок

```python
# Правило: ошибка одной карточки не прерывает пакет
for card in cards:
    try:
        await process(card)
    except Exception as exc:
        logger.error("context: «{}» (id={}) — {}", card.title, card.id, exc)
        continue  # никогда не raise наружу из batch-операций

# Правило: методы клиентов возвращают None/False при ошибке, не бросают
async def some_api_call(self) -> Card | None:
    data = await self._request(...)
    if not data:
        logger.error("...")
        return None
    return _parse_card(data)
```

---

## Kaiten API — ключевые паттерны

```python
# Авторизация
headers = {"Authorization": f"Bearer {KAITEN_TOKEN}"}

# Карточки колонки (пагинация обязательна)
GET /spaces/{space_id}/cards?column_id={id}&limit=100&offset=0

# Перемещение
PATCH /cards/{id}  body={"column_id": ..., "sort_order": float}

# Добавление тега (не через tag_ids в PATCH — не работает!)
POST /cards/{id}/tags  body={"name": "tagname"}

# Установка event_time (dict, не строка!)
PATCH /cards/{id}  body={"properties": {"id_590358": {"date": "YYYY-MM-DD", "time": "HH:MM:SS", "tzOffset": 180}}}

# Комментарий
POST /cards/{id}/comments  body={"text": "..."}

# Удаление
DELETE /cards/{id}
```

**sort_order:** float, вставка между двумя картами = среднее арифметическое их sort_order.
**Разделители** (Утро/День/Вечер/На контроле): `blocked=True`, `title="-"`, секция из `block_reason`.

### Особенности Kaiten API (из продакшена)

- КРИТИЧНО: `blocked=True` при `POST /cards` **игнорируется Kaiten API** — карточка создаётся разблокированной. Для создания разделителя нужен отдельный `PATCH /cards/{id}` с `{"blocked": True, "block_reason": "..."}` после POST.
- КРИТИЧНО: теги добавляются ТОЛЬКО через `POST /cards/{id}/tags` с `{"name": "tagname"}` — поле `tag_ids` в PATCH игнорируется.
- КРИТИЧНО: `archive_card` всегда через `BoardLogic.archive_card()`, не через `KaitenClient` напрямую — только BoardLogic знает `archive_column_id`.

---

## Claude API — паттерны

```python
# Всегда claude-sonnet-4-6, не другие названия
MODEL = "claude-sonnet-4-6"

# Кэширование системного промпта
system=[{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}]

# Московское время для parse_intent (не date.today() — Railway в UTC!)
today_str = datetime.now(timezone(timedelta(hours=3))).date().isoformat()

# JSON-ответы: инициализировать raw_text до try
raw_text = ""
try:
    raw_text = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1].lstrip("json").strip()
    result = json.loads(raw_text)
except json.JSONDecodeError:
    return {"action": "unknown", "raw": user_text}
```

---

## Telegram — паттерны

```python
# python-telegram-bot v20+, всё async
# Разбивка длинных сообщений (лимит Telegram = 4096 символов)
def _split_text(text: str, max_len: int = 4096) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text); break
        cut = text.rfind("\n", 0, max_len)
        if cut == -1: cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts

# Fallback при ошибке Markdown — НЕ передавать parse_mode="text" (Telegram API не принимает)
# Правильный fallback: повторить запрос БЕЗ параметра parse_mode вообще
try:
    await bot.send_message(chat_id, text, parse_mode="Markdown")
except Exception:
    await bot.send_message(chat_id, text)  # без parse_mode — plain text
```

---

## APScheduler — паттерны

```python
# Всегда именованная таймзона, не UTC+03:00
AsyncIOScheduler(timezone="Europe/Moscow")
CronTrigger(hour=6, minute=30, timezone="Europe/Moscow")

# Джобы через безопасные обёртки
async def _safe_morning(self):
    try:
        await self.run_morning_job()
    except Exception as exc:
        logger.error("morning_job unhandled: {}", exc)
```

---

## Правила оркестратора (ОБЯЗАТЕЛЬНО)

**Оркестратор НИКОГДА не пишет Python-код сам.** Только читает, планирует, делегирует.

- НЕЛЬЗЯ: открывать `.py` файлы через Edit/Write и редактировать их напрямую
- МОЖНО: Read `.py` файлов только для понимания интерфейсов перед постановкой задачи субагенту
- МОЖНО: Write/Edit `.md` файлов (docs/requirements.md, docs/interfaces.md и т.д.)
- Вся реализация Python — через субагентов (Agent tool с нужным типом агента)

Причина: написание кода в главной сессии засоряет контекст оркестратора деталями реализации.
При сложных задачах контекст нужен для архитектурных решений и координации, а не для кода.

---

## Правила реализации новых методов KaitenClient

- КРИТИЧНО: перед использованием нового метода `KaitenClient` в `handlers.py`, `scheduler.py`, `morning_logic.py` — убедиться, что метод РЕАЛЬНО существует в `kaiten_client.py`. Grep: `grep -n "def имя_метода" kaiten_client.py`
- КРИТИЧНО: при добавлении нового метода в `docs/interfaces.md` — сразу реализовать его в `kaiten_client.py` в том же PR/задаче. Документация без реализации ломает импортирующие модули.

---

## Тиры сложности задач

**Tier 1 — точечная правка** (1 файл, изменение понятно):
→ 1 реализующий агент, без Explore фазы

**Tier 2 — средняя задача** (1–2 модуля, задача сформулирована):
→ [Explore если файлы незнакомы] → 1–2 реализующих агента (параллельно если независимы)

**Tier 3 — сложная задача** (3+ модулей или требования неясны):
→ Explore агент → оркестратор изучает → параллельные реализующие агенты → deploy + retro агенты

---

## Протокол: Explore-first

Для задач где неясно какие файлы затрагиваются или неизвестны детали реализации:
1. Запустить Explore агент (subagent_type=Explore) — прочитает нужные файлы, вернёт краткий отчёт
2. Оркестратор изучает отчёт, формирует точное задание
3. Передать findings в промпт реализующего агента (не читать те же файлы заново)

Для задач где файлы и изменения известны точно (описаны в задаче):
→ Пропустить Explore шаг — сразу делегировать реализующему агенту.

---

## Протокол: параллельный запуск агентов

Если изменения в 2+ модулях **независимы** — запускать агентов ПАРАЛЛЕЛЬНО
(один вызов Agent в одном сообщении, несколько tool calls).

Если изменения **зависят** друг от друга (один использует результат другого) — последовательно.

Пример параллельно: fix в `kaiten_client.py` + fix в `morning_logic.py` (независимы).
Пример последовательно: новый метод в `kaiten_client.py` → использовать в `morning_logic.py`.

---

## Протокол деплоя (ОБЯЗАТЕЛЬНО)

Каждый деплой выполняется в три параллельных действия:

### 1. Обновить docs/requirements.md (оркестратор сам, ДО деплоя)

Перед вызовом deploy/retro оркестратор обновляет `docs/requirements.md`:
- Новые фичи → добавить как функциональные требования в нужный раздел
- Новые модули → добавить в раздел «Структура модулей»
- Изменения архитектуры → обновить соответствующий раздел

### 2. Вызвать deploy-агента и retro-агента ПАРАЛЛЕЛЬНО

```
Agent(deploy) ← сообщение коммита
Agent(retro)  ← структурированный отчёт об ошибках сессии
```

Retro-агенту передавать:

```
**Ошибки/краши:** [список с описанием что пошло не так, или "не было"]
**Вероятные причины:** [почему это произошло]
**Что сделано хорошо:** [паттерны, которые сработали]
**Предложения:** [конкретные улучшения для документации]
```

### 3. Если deploy-агент вернул BLOCKED

Deploy-агент останавливается при ImportError или синтаксической ошибке.
Оркестратор получает список ошибок → делегирует правки нужному субагенту →
повторяет деплой.

---

## Шаблон промпта для субагента

Обязательные разделы при постановке задачи реализующему агенту:

```
**Задача:** [что именно изменить — конкретно, не "улучши код"]

**Файлы для изменения:** [перечислить явно]
**Файлы НЕ трогать:** [перечислить явно]

**Контекст:** [ключевые выдержки из requirements/interfaces ИЛИ findings от Explore агента]

**Требования к реализации:** [паттерны, ограничения, на что обратить внимание]

**Проверка:** [что запустить после изменений: python -c "import X; print('OK')"]
```

---

## Шаблон ответа от субагента

Субагент завершает работу и возвращает структурированный отчёт:

```
**Сделано:** [список изменений с именами файлов и сутью правок]
**Пропущено:** [что не менялось и почему — если были отклонения от задачи]
**Интерфейс:** [изменились ли публичные методы/сигнатуры — важно для оркестратора]
**Проверка:** [результат синтаксис-чека или "не запускал — нет доступа к Bash"]
**Блокеры:** [проблемы требующие решения оркестратором, или "нет"]
```

---

## Агенты и их файлы

| Агент | Файлы | Тип |
|---|---|---|
| kaiten-agent | kaiten_client.py, board_logic.py | subagent_type=kaiten-agent |
| scheduler-agent | morning_logic.py, evening_logic.py, scheduler.py, db.py | subagent_type=scheduler-agent |
| telegram-agent | bot.py, handlers.py, notifier.py | subagent_type=telegram-agent |
| claude-api-agent | claude_client.py, prompts.py | subagent_type=claude-api-agent |
| deploy | деплой на Railway | subagent_type=deploy |
| retro | ретроспектива, улучшение документации | subagent_type=retro |
| Explore | поиск по кодовой базе | subagent_type=Explore |

**Правило:** каждый агент работает только со своими файлами.
Архитектурные решения (менять интерфейсы между модулями) — только через главный чат/оркестратор.
