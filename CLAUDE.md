# AI Personal Planner — Project Context

## Язык общения

Все ответы, отчёты о ходе работ и сообщения пользователю — **только на русском языке**,
независимо от того, на каком языке написаны технические термины в коде/логах. Это касается
и главного ассистента, и всех субагентов при отчёте оркестратору.

## Что это

Сервис автоматического планирования дня: Kaiten (kanban) + Telegram-бот + Claude API.
Каждое утро алгоритм расставляет задачи по временным слотам с учётом приоритетов,
шлёт расписание в Telegram, принимает команды и управляет карточками через кнопки.

**Текущий статус:** работает в продакшене на Railway.app, личное использование.
**Перспектива:** коммерческий SaaS-продукт для персонального планирования.

---

## Треки развития проекта

| Трек | Описание |
|---|---|
| **Разработка** | Python-сервис, API-интеграции, алгоритм утра, Telegram-бот |
| **Продукт** | Функциональные требования, UX, roadmap |
| **Маркетинг** | Позиционирование, контент, целевая аудитория |
| **Стратегия** | Монетизация, конкуренты, go-to-market |

Для работы по каждому треку используй соответствующий агент или обращайся напрямую.

---

## Технологический стек

| Компонент | Решение |
|---|---|
| Язык | Python 3.11+ |
| Telegram | python-telegram-bot v20+ (async) |
| Планировщик | APScheduler 3.x (AsyncIOScheduler) |
| Kaiten API | httpx (async), Bearer-токен |
| Claude API | anthropic SDK, модель `claude-sonnet-4-6` |
| БД | SQLite (через db.py) |
| Логи | loguru |
| Хостинг | Railway.app |
| Деплой | git push → Railway auto-deploy |
| Конфиг | .env (python-dotenv) |

---

## Структура файлов сервиса

```
planner/
├── bot.py                # Точка входа, сборка зависимостей, запуск
├── handlers.py           # Telegram: команды + ConversationHandler (кнопки)
├── notifier.py           # Отправка сообщений без входящего апдейта
├── scheduler.py          # APScheduler джобы (06:30 утро, 06:00 архив, 1min напоминалки)
├── morning_logic.py      # Алгоритм расстановки задач по времени (v4)
├── evening_logic.py      # Вечерний итог (временно отключён)
├── kaiten_client.py      # HTTP-клиент к Kaiten API + dataclasses
├── board_logic.py        # Бизнес-логика поверх Kaiten API
├── claude_client.py      # Интеграция с Claude API
├── prompts.py            # Системные промпты
├── db.py                 # SQLite: флаги morning_done/evening_done
├── requirements.txt
├── .env                  # Не в git
└── docs/
    ├── requirements.md   # Функциональные и нефункциональные требования
    ├── interfaces.md     # Реальные ID колонок Kaiten, поля карточек, публичные методы
    └── skill-development.md  # Паттерны кода, принятые в проекте
```

---

## Ключевые переменные окружения

```
KAITEN_TOKEN, KAITEN_BASE_URL, KAITEN_SPACE_ID=197396, KAITEN_BOARD_ID=476640, KAITEN_LANE_ID=623640
TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
ANTHROPIC_API_KEY
DB_PATH=/data/state.db
```

---

## Структура канбан-доски Kaiten

**Колонки (ПОЛНЫЕ имена, не аббревиатуры!):**
Понедельник (1688101), Вторник (1689798), Среда (1689899), Четверг (1689903), Пятница (1689912),
Суббота (6122424), Воскресенье (6122425), Следующая неделя (6122270), Далекие времена (6122271),
Долгий ящик (1688100), Архив (6122269).

**Разделители внутри колонки:** Утро / День / Вечер / На контроле — обычные карточки
с `blocked=True`, `title="-"` (дефис!), секция определяется по `block_reason`.

**Кастомные поля карточки:**
- `properties.id_590358` — event_time `{"date": "...", "time": "...", "tzOffset": 180}`
- `properties.id_590382` — importance `[17244396/397/398]` = среднее/важное/критическое
- `properties.id_590359` — weekday `[17244346..352]` = ПН..ВС

**Теги (TAG_IDS):** ежедневно=1074844, по будням=1074837, по выходным=1074843,
еженедельно=407071, напомнить=1076451, вечерняя=1097987.

**Важно:** теги добавляются через `POST /cards/{id}/tags` с телом `{"name": "tagname"}`.

---

## Алгоритм утра (v4, morning_logic.py)

1. **Сбор карточек** — вчерашняя колонка (обычный день) или все дни + следующая неделя (пн)
2. **Фаза 1** — карточки с `event_time.date() == today` → сразу в нужный раздел, резервируют слот
3. **Фаза 2** — 9 групп приоритета (critical+today → Утро, critical+soon → День, вечерняя → Вечер)
4. **Фаза 3** — единый блок 09:00–19:00 для групп 0–5, 19:00–22:00 для групп 6–8.
   Сегментированное размещение: задача огибает фиксированные события.
5. **Фаза 4** — overflow на следующий день, после вс → Следующая неделя

**Утро/День — ярлыки приоритета, не временные окна.** Оба берут из единого пула 09:00–19:00.

---

## Деплой

```bash
git add -A
git commit -m "feat/fix: описание"
git push  # Railway подхватывает автоматически
```

Логи: Railway Dashboard → вкладка Logs.
Сброс флага утра для конкретного пользователя: `python -c "import db; db.reset_flags('YYYY-MM-DD', 'user_id')"` через Railway Shell.
Сброс для всех пользователей: `python -c "import sqlite3, os; conn=sqlite3.connect(os.getenv('DB_PATH','state.db')); conn.execute(\"UPDATE daily_flags SET morning_done=0 WHERE date='YYYY-MM-DD'\"); conn.commit(); print('done')"` через Railway Shell.

---

## Работа с агентами

Агенты находятся в `.claude/agents/`. При задаче по конкретному модулю укажи оркестратору какой агент использовать, он прочитает нужный файл.

## Работа с агентами

Агенты находятся в `.claude/agents/`. При задаче по конкретному модулю
укажи оркестратору какой агент использовать, он прочитает нужный файл.

| Агент | Файл | Отвечает за |
|---|---|---|
| Kaiten | `.claude/agents/kaiten-agent.md` | kaiten_client.py, board_logic.py работа с Kaiten API|
| Scheduler | `.claude/agents/scheduler-agent.md` | morning_logic.py, scheduler.py, db.py утренняя/вечерняя логика, APScheduler|
| Telegram | `.claude/agents/telegram-agent.md` | bot.py, handlers.py, notifier.py бот, хендлеры, ConversationHandler|
| Claude API | `.claude/agents/claude-api-agent.md` | claude_client.py, prompts.py промпты, Claude API интеграция|
| Persona | `.claude/agents/persona-agent.md` | симуляция использования сервиса воображаемой персоной (скилл feedback) |

Скилл разработки: `.claude/skills/development/SKILL.md` — читать перед любыми изменениями кода.



Читай `docs/interfaces.md` перед изменениями — там реальные ID и контракты между модулями.
Читай `docs/requirements.md` перед добавлением функций — там актуальные требования.
