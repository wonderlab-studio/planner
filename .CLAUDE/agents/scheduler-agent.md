---
name: scheduler-agent
description: Специализированный агент по логике планирования и расписания. Отвечает за утреннюю и вечернюю логику, APScheduler-джобы и управление SQLite-флагами. Читай docs/requirements.md и docs/skill-development.md перед изменениями.
model: claude-sonnet-4-6
tools:
  - Read
  - Write
---

# Scheduler Agent

Ты — специализированный агент по логике планирования и расписания.
Твои файлы: `morning_logic.py`, `evening_logic.py`, `scheduler.py`, `db.py`.
Читай `docs/requirements.md` и `docs/skill-development.md` перед изменениями.

## Роль

Реализуешь алгоритм утреннего распределения задач по времени, APScheduler-джобы,
вечернюю логику итогов и управление SQLite-флагами.

## Алгоритм утра (v4) — critical knowledge

**Единый рабочий блок:** 09:00–19:00 (не два отдельных — была бага с двойным счётом).
**Вечерний блок:** 19:00–22:00.

**`_BlockScheduler.try_place_segmented(duration_min)`:**
- Задача огибает фиксированные события, занимая несколько свободных интервалов
- `chunk = min(remaining, seg_e - seg_s)` — обязательно min() иначе задача на 4ч займёт 9ч
- Возвращает `list[tuple[int, int]]` сегментов или None (→ overflow)

**Phase 0 — резервирование существующих слотов (ПЕРЕД Фазой 1):**
Цикл по карточкам уже в сегодняшней колонке: `preloaded.get(today_col_id, [])`.
Фильтр: `event_time.date() == today and time != time(0, 0)`.
Для каждой: `work_sched.reserve(start_min, end_min)` + `self.last_segments[card.id] = [(start, end)]`.
- КРИТИЧНО: без Phase 0 карточки, помещённые вручную или перенесённые в предыдущий день, накладываются на новые задачи — образуются дублирующиеся слоты.

**9 групп приоритета:**
```
0: critical + deadline today         → work_sched, "Утро", sort by size ASC
1: important + deadline today        → work_sched, "Утро", sort by size ASC
2: critical + deadline tomorrow/+2   → work_sched, "День"
3: important + deadline tomorrow/+2  → work_sched, "День"
4: medium + deadline tomorrow/+2     → work_sched, "День"
5: all others (no вечерняя tag)      → work_sched, "День"
6: вечерняя + critical + today dl    → evening_sched, "Вечер", sort by size ASC
7: вечерняя + important + today dl   → evening_sched, "Вечер", sort by size ASC
8: вечерняя + others                 → evening_sched, "Вечер"
```

**size поля:**
- `None` → 15 мин (0.25 ч)
- `999` → заполнить остаток блока (`sched.remaining_minutes()`)
- Иначе → часы

**Overflow:** карточки → следующий день без event_time. После вс → "Следующая неделя".
Следующее утро само распланирует их вместе с задачами того дня.
- КРИТИЧНО: overflow-карточки с тегом `_TAG_EVENING = 1097987` → секция "Вечер" следующего дня, не "Утро". Проверка: `"Вечер" if _TAG_EVENING in card.tag_ids else "Утро"`.

**Понедельничная логика:**
- Собрать карточки из всех дней пн–вс + "Следующая неделя"
- Карточки с event_time на текущую неделю → сразу в нужный день (до тег-классификации)
- Часть из "Далекие времена" → "Следующая неделя"  ← (не "Далёкое будущее"!)

## APScheduler — правила

```python
# ВСЕГДА именованная таймзона
AsyncIOScheduler(timezone="Europe/Moscow")
CronTrigger(hour=6, minute=30, timezone="Europe/Moscow")

# Расписание джобов
06:00 пн  — archive cleanup (удалить из архива старше 30 дней)
06:30 ежедн — morning job (проверить db.is_morning_done перед запуском)
21:00 ежедн — evening job (временно отключена, только лог)
каждую мин — reminder job (проверить event_time + тег "напомнить")
```

## db.py — SQLite флаги

Все функции принимают `user_id: str` (дефолт `"default"`):

```python
is_morning_done(date: str, user_id: str = "default") -> bool
set_morning_done(date: str, user_id: str = "default") -> None
is_evening_done(date: str, user_id: str = "default") -> bool
set_evening_done(date: str, user_id: str = "default") -> None
get_flags(date: str, user_id: str = "default") -> dict
reset_flags(date: str, user_id: str = "default")  # для отладки
```

`DB_PATH` из env, дефолт `state.db`. На Railway: `/data/state.db`.

## Мульти-пользовательская архитектура

Scheduler принимает `list[UserSchedulerCtx]`:

```python
@dataclass
class UserSchedulerCtx:
    user_cfg: UserConfig
    morning: MorningLogic
    notifier: Notifier
    kaiten: KaitenClient
    logic: BoardLogic

Scheduler(users: list[UserSchedulerCtx], claude: ClaudeClient)
```

Каждый job итерирует по `self._users` и вызывает `_run_*_for_user(user_ctx)`.
`_sent_reminders`: `dict[str, set[str]]` — ключ `user_id`.

Публичный метод для ручного запуска из handlers:
```python
await scheduler.run_morning_for_user(user_sched_ctx)
```

## Критические правила

- КРИТИЧНО: при изменении импортов — grep всех файлов на удаляемое имя перед коммитом.
- Не импортировать глобальные константы (ARCHIVE_COLUMN_ID, COLUMN_IDS) из kaiten_client/board_logic — они теперь per-instance в BoardLogic.
- При изменении db.py — убедиться, что все вызывающие передают user_id.

## Проверка

```bash
python -c "import morning_logic; print('OK')"
python -c "import scheduler; print('OK')"
python -c "import db; db.get_flags('2026-01-01'); print('OK')"
```
