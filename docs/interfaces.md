# Интерфейсы модулей

> Получено через реальные запросы к API Kaiten (май 2026).
> Пространство: itrask (id=197396), Доска: Weekly (id=476640).

---

## 1. Kaiten API — реальная структура

### Переменные окружения (.env)

```
KAITEN_TOKEN=...
KAITEN_BASE_URL=https://wonderlabst.kaiten.ru/api/latest
KAITEN_SPACE_ID=197396
KAITEN_BOARD_ID=476640
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
ANTHROPIC_API_KEY=...
```

---

### Рабочие эндпоинты

| Действие | Метод | URL |
|---|---|---|
| Список досок пространства | GET | `/spaces/{space_id}/boards` |
| Колонки доски | GET | `/boards/{board_id}/columns` |
| Карточки колонки | GET | `/spaces/{space_id}/cards?column_id={col_id}` |
| Одна карточка | GET | `/cards/{card_id}` |
| Создать карточку | POST | `/cards` |
| Обновить карточку | PATCH | `/cards/{card_id}` |
| Удалить карточку | DELETE | `/cards/{card_id}` — возвращает 204 |
| Добавить комментарий | POST | `/cards/{card_id}/comments` |
| Получить комментарии | GET | `/cards/{card_id}/comments` |
| Добавить тег | POST | `/cards/{card_id}/tags` — body: `{"name": "tagname"}` |
| Заблокировать карточку | POST | `/cards/{card_id}/blockers` — body: `{"reason": "Утро"}` |

> Авторизация: заголовок `Authorization: Bearer {KAITEN_TOKEN}`
> API возвращает максимум 100 карточек за запрос — необходима пагинация
> (параметры: `?limit=100&offset=0`)

**Не работают в этом инстансе:**
- `GET /columns/{id}/cards` — возвращает 404
- `PATCH /cards/{id}` с полем `tag_ids` — теги игнорируются, использовать `POST /cards/{id}/tags`
- `POST /cards` с полями `blocked`/`block_reason` — игнорируются Kaiten при создании
- `PATCH /cards/{id}` с полями `blocked`/`block_reason` — также игнорируется; для блокировки использовать `POST /cards/{id}/blockers`

---

### Колонки доски (актуальные ID)

| Название | column_id |
|---|---|
| Понедельник | 1688101 |
| Вторник | 1689798 |
| Среда | 1689899 |
| Четверг | 1689903 |
| Пятница | 1689912 |
| Суббота | 6122424 |
| Воскресенье | 6122425 |
| Следующая неделя | 6122270 |
| Далекие времена | 6122271 |
| Долгий ящик | 1688100 |
| Архив | 6122269 |

---

### Поля карточки (card object)

#### Стандартные поля

| Поле | Тип | Описание |
|---|---|---|
| `id` | int | ID карточки |
| `title` | str | Название |
| `description` | str \| null | Описание |
| `column_id` | int | ID колонки |
| `lane_id` | int | ID дорожки (одна на всей доске: 623640) |
| `board_id` | int | ID доски (476640) |
| `sort_order` | float | Позиция в колонке, по возрастанию |
| `blocked` | bool | true → карточка-разделитель |
| `block_reason` | str \| null | Причина блокировки ("Утро"/"День"/"Вечер"/"На контроле") |
| `size` | int \| null | **complexity** — размер/трудоёмкость в часах |
| `due_date` | str \| null | **deadline** — ISO 8601, напр. `"2026-05-21T00:00:00.000Z"` |
| `tag_ids` | list[int] | ID меток |
| `tags` | list[Tag] | Полные объекты меток (в детальном ответе) |
| `state` | int | 1=открыта, 2=в работе, 3=завершена |
| `archived` | bool | Архивирована ли |
| `condition` | int | Состояние дорожки |
| `updated_at` | str \| null | ISO 8601 — дата последнего изменения |
| `last_moved_at` | str \| null | ISO 8601 — дата последнего перемещения между колонками (может отсутствовать в ответе API — см. TODO #7) |

#### Кастомные свойства (`properties`)

| Ключ | Тип значения | Поле системы | Пример |
|---|---|---|---|
| `id_590358` | `{"date": str, "time": str, "tzOffset": int}` | **event_time** | `{"date": "2026-05-16", "time": "10:00:37", "tzOffset": 180}` |
| `id_590359` | `[int]` | **weekday** (день недели, select) | `[17244350]` = ПТ |
| `id_590382` | `[int]` | **importance** (важность, select) | `[17244397]` = важное |

```python
IMPORTANCE_OPTIONS = {
    "среднее":     17244396,
    "важное":      17244397,
    "критическое": 17244398,
}

WEEKDAY_OPTIONS = {
    "ПН": 17244346,
    "ВТ": 17244347,
    "СР": 17244348,
    "ЧТ": 17244349,
    "ПТ": 17244350,
    "СБ": 17244351,
    "ВС": 17244352,
}
```

---

### Теги (актуальные ID)

| tag_id | Название |
|---|---|
| 407071 | еженедельно |
| 1074837 | по будням |
| 1074843 | по выходным |
| 1074844 | ежедневно |
| 1076451 | напомнить |

---

### Разделители внутри колонки дня

Разделители — обычные карточки с `blocked: true`. Секция определяется по полю `block_reason`.

**Структура колонки по sort_order (пример — Пятница):**

```
sort_order ≈ 1.79   │ [РАЗДЕЛИТЕЛЬ] block_reason="Утро"
                    │   задача утром...
sort_order ≈ 3.11   │ [РАЗДЕЛИТЕЛЬ] block_reason="День"
                    │   задача дня...
sort_order ≈ 3.21   │ [РАЗДЕЛИТЕЛЬ] block_reason="Вечер"
                    │   задача вечером...
sort_order ≈ 3.30   │ [РАЗДЕЛИТЕЛЬ] block_reason="На контроле"
                    │   задача на контроле...
```

**Как вставить карточку в нужную секцию:**

Найти разделитель секции и следующий разделитель (или конец списка).
Новый `sort_order` = среднее арифметическое `sort_order` разделителя и первой задачи после него.
Если задач нет — `sort_order` разделителя + 0.001.

**Актуальные sort_order разделителей (Пятница, id=1689912):**

| Секция | card_id | sort_order |
|---|---|---|
| Утро | 17953298 | 1.787043802799955 |
| День | 17953301 | 3.112153842315635 |
| Вечер | 64879984 | 3.2065135812174335 |
| На контроле | 17953300 | 3.2980453752078267 |

> sort_order разделителей одинаковы во всех колонках — они создаются один раз
> и не меняются. Получать их нужно динамически при старте системы.

---

### Пример создания карточки (POST /cards)

```json
{
  "board_id": 476640,
  "column_id": 1689912,
  "lane_id": 623640,
  "title": "Название задачи",
  "description": "Описание",
  "size": 2,
  "due_date": "2026-05-21T00:00:00.000Z",
  "sort_order": 2.0,
  "tag_ids": [1074837],
  "properties": {
    "id_590358": {"date": "2026-05-21", "time": "10:00:00", "tzOffset": 180},
    "id_590382": [17244397]
  }
}
```

### Пример перемещения карточки (PATCH /cards/{card_id})

```json
{
  "column_id": 6122269,
  "sort_order": 1.5
}
```

### Пример добавления комментария (POST /cards/{card_id}/comments)

```json
{
  "text": "Текст комментария"
}
```

### Пример добавления тега (POST /cards/{card_id}/tags)

```json
{
  "name": "ежедневно"
}
```

### Пример блокировки карточки (POST /cards/{card_id}/blockers)

```json
{
  "reason": "Утро"
}
```

---

## 2. KaitenClient — публичные методы

```python
# kaiten_client.py
# Стек: httpx (async), loguru, python-dotenv

class KaitenClient:

    def __init__(
        self,
        board_id: int,
        lane_id: int,
        token: str | None = None,             # fallback → KAITEN_TOKEN из env
        base_url: str | None = None,          # fallback → KAITEN_BASE_URL из env
        tag_ids: dict[str, int] | None = None,            # fallback → глобальный TAG_IDS
        importance_options: dict[str, int] | None = None, # fallback → глобальный IMPORTANCE_OPTIONS
        weekday_options: dict[str, int] | None = None,    # fallback → глобальный WEEKDAY_OPTIONS
        field_ids: dict[str, str] | None = None,          # fallback → {"event": "id_590358", ...}
    ): ...
    # field_ids canonical keys: "event", "importance", "weekday"
    # Все параметры опциональны — обратная совместимость полная.

    # ── Методы-билдеры для мульти-аккаунта ──────────────────────────────────

    def tag_id(self, name: str) -> int | None:
        """Возвращает ID тега по имени для данного Kaiten-аккаунта или None."""

    def event_time_property(self, dt: datetime) -> dict:
        """dt — aware datetime. Возвращает {field_key: {...}} готовое для update_card(properties=...).
        Использует реальный ключ поля из self._field_ids["event"]."""

    def importance_property(self, name: str) -> dict | None:
        """name — 'среднее'|'важное'|'критическое'.
        Возвращает {field_key: [option_id]} или None если имя не найдено."""

    # ── Основные методы ──────────────────────────────────────────────────────

    async def get_columns(self) -> list[Column]:
        """GET /boards/{board_id}/columns
        Возвращает все колонки доски с id и title."""

    async def get_cards(self, column_id: int) -> list[Card]:
        """GET /spaces/{space_id}/cards?column_id={column_id}
        Возвращает ВСЕ карточки колонки с пагинацией (limit=100, offset).
        Включает и разделители (blocked=True), и задачи."""

    async def get_card(self, card_id: int) -> Card | None:
        """GET /cards/{card_id}
        Полная структура карточки включая properties, tags, members."""

    async def create_card(
        self,
        column_id: int,
        title: str,
        *,
        description: str | None = None,
        size: int | None = None,
        due_date: str | None = None,
        sort_order: float | None = None,
        tag_ids: list[int] | None = None,
        properties: dict | None = None,
    ) -> Card | None:
        """POST /cards
        Создаёт карточку. lane_id и board_id подставляются автоматически."""

    async def move_card(self, card_id: int, column_id: int, sort_order: float) -> Card | None:
        """PATCH /cards/{card_id}
        Перемещает карточку в другую колонку с указанным sort_order."""

    async def update_card(self, card_id: int, **fields) -> Card | None:
        """PATCH /cards/{card_id}
        Обновляет произвольные поля карточки."""

    async def archive_card(self, card_id: int, archive_column_id: int, comment: str | None = None) -> bool:
        """Перемещает карточку в колонку Архив (archive_column_id передаётся снаружи).
        Если передан comment — добавляет комментарий перед перемещением.
        Возвращает True при успехе.
        КРИТИЧНО: вызывать только через BoardLogic.archive_card() — он знает archive_column_id."""

    async def add_comment(self, card_id: int, text: str) -> bool:
        """POST /cards/{card_id}/comments  body={"text": text}
        Возвращает True при успехе."""

    async def get_comments(self, card_id: int) -> list[str]:
        """GET /cards/{card_id}/comments
        Возвращает список текстов комментариев (пустой список при ошибке)."""

    async def delete_card(self, card_id: int) -> bool:
        """DELETE /cards/{card_id} — удаляет карточку навсегда.
        Kaiten возвращает 204 при успехе.
        Возвращает True при успехе, False при ошибке."""

    async def get_lanes(self) -> list[dict]:
        """GET /boards/{board_id}/lanes
        Возвращает список дорожек доски (сырые dict с полями id, title и т.д.)."""

    async def create_column(self, title: str, sort_order: float = 1000.0) -> dict | None:
        """POST /boards/{board_id}/columns
        Создаёт колонку. Возвращает сырой dict с id или None при ошибке."""

    async def delete_column(self, column_id: int) -> bool:
        """DELETE /boards/{board_id}/columns/{column_id}
        Удаляет колонку. Возвращает True при успехе, False при ошибке."""

    async def add_tag_by_name(self, card_id: int, tag_name: str) -> bool:
        """POST /cards/{card_id}/tags  body={"name": tag_name}
        Добавляет тег по имени (не по ID) — совместимо с мульти-пользовательским режимом.
        Возвращает True при успехе, False при ошибке."""

    async def block_card(self, card_id: int, reason: str) -> bool:
        """POST /cards/{card_id}/blockers  body={"reason": reason}
        Единственный рабочий способ заблокировать карточку (PATCH с blocked/block_reason
        игнорируется Kaiten API). Возвращает True при успехе (статус 200), False при ошибке."""

    async def create_blocked_card(
        self, column_id: int, title: str, block_reason: str, sort_order: float = 1.0
    ) -> dict | None:
        """Создаёт карточку-разделитель (blocked=True).
        POST /cards → получить id → POST /cards/{id}/blockers с reason=block_reason.
        (Kaiten игнорирует blocked/block_reason при POST и при PATCH — нужен /blockers.)
        Возвращает итоговый dict карточки или None при ошибке."""
```

---

## 3. BoardLogic — публичные методы

```python
# board_logic.py
# Зависит от KaitenClient. Реализует бизнес-логику поверх API.

COLUMN_IDS: dict[str, int] = {
    "Понедельник":      1688101,
    "Вторник":          1689798,
    "Среда":            1689899,
    "Четверг":          1689903,
    "Пятница":          1689912,
    "Суббота":          6122424,
    "Воскресенье":      6122425,
    "Следующая неделя": 6122270,
    "Далекие времена":  6122271,
    "Долгий ящик":      1688100,
    "Архив":            6122269,
}

class BoardLogic:

    def get_column_id(self, day: str) -> int:
        """'Понедельник' → 1688101. Поддерживает и доп. колонки."""

    def get_today_column_id(self) -> int:
        """Возвращает column_id колонки текущего дня недели (UTC+3)."""

    def get_yesterday_column_id(self) -> int:
        """Возвращает column_id колонки вчерашнего дня (UTC+3)."""

    def resolve_column_for_date(self, target_date: date) -> int:
        """Возвращает column_id для конкретной даты (UTC+3).

        Правила:
        - дата <= конец текущей недели (вс включительно) → колонка дня недели
        - дата <= следующее воскресенье (через 7 дней) → «Следующая неделя»
        - иначе → «Далекие времена»

        Используется в handlers.py для корректного определения целевой колонки
        при переносе карточки по ISO-дате дедлайна (а не по угаданному названию дня).
        """

    async def get_section_sort_order(
        self,
        column_id: int,
        section: str,   # "Утро" | "День" | "Вечер" | "На контроле"
    ) -> float:
        """Возвращает sort_order для вставки ПЕРВОЙ позицией в секцию.
        Алгоритм: sort_order разделителя + epsilon (если секция пуста)
        или среднее между разделителем и первой задачей секции."""

    def sort_cards_by_priority(self, cards: list[Card]) -> list[Card]:
        """Сортировка задач для расстановки по дню (UTC+3 для определения «сегодня»).
        Порядок: 1) есть event_time → по времени события
                 2) due_date == сегодня → срочные вперёд
                 3) importance: критическое > важное > среднее
                 4) size: меньше → раньше (быстрые задачи вперёд)
        Разделители (blocked=True) из сортировки исключаются."""

    def is_regular_task(self, card: Card) -> bool:
        """True если карточка имеет тег ежедневно/по будням/по выходным/еженедельно.
        Использует self._regular_tag_ids — per-instance множество из tag_id() клиента."""

    def should_include_today(self, card: Card, today: date) -> bool:
        """Проверяет, должна ли регулярная задача появиться сегодня.
        Логика по тегам: ежедневно → всегда, по будням → пн-пт,
        по выходным → сб-вс, еженедельно → только если weekday совпадает."""
```

---

## 4. Dataclasses (типы)

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class Column:
    id: int
    title: str
    sort_order: float

@dataclass
class Tag:
    id: int
    name: str

@dataclass
class Card:
    id: int
    title: str
    column_id: int
    sort_order: float
    blocked: bool
    block_reason: str | None       # "Утро" / "День" / "Вечер" / "На контроле"
    description: str | None = None
    size: int | None = None        # complexity (часы)
    due_date: str | None = None    # ISO datetime
    tag_ids: list[int] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    archived: bool = False
    state: int = 1
    updated_at: str | None = None      # ISO datetime последнего изменения
    last_moved_at: str | None = None   # ISO datetime последнего перемещения между колонками
                                        # (может отсутствовать в ответе API — fallback на updated_at)

    # Удобные свойства (вычисляются из properties / полей)
    @property
    def event_time(self) -> datetime | None:
        """Парсит properties.id_590358 → datetime (UTC+3)"""

    @property
    def importance(self) -> str | None:
        """Парсит properties.id_590382 → 'среднее'/'важное'/'критическое'"""

    @property
    def weekday(self) -> str | None:
        """Парсит properties.id_590359 → 'ПН'/'ВТ'/...'ВС'"""

    @property
    def updated_at_parsed(self) -> datetime | None:
        """Парсит updated_at → datetime aware (UTC+3)"""

    @property
    def last_moved_at_parsed(self) -> datetime | None:
        """Парсит last_moved_at → datetime aware (UTC+3).
        Fallback на updated_at если last_moved_at отсутствует в ответе API."""

    @property
    def due_date_parsed(self) -> datetime | None:
        """Парсит due_date → datetime aware (UTC+3)"""
```

---

## 5. Открытые вопросы (TODO)

| # | Вопрос | Статус | Как решить |
|---|---|---|---|
| 1 | option_id для importance | Решено | среднее=17244396, важное=17244397, критическое=17244398 |
| 2 | option_id для weekday | Решено | ПН=17244346 … ВС=17244352 |
| 3 | Пагинация: точные параметры offset/limit | Решено | limit=100, offset+=100 пока len(batch) < limit |
| 4 | Эндпоинт архивирования: PATCH move или отдельный | Решено | PATCH /cards/{id} с column_id=6122269 |
| 5 | Разделители в других колонках | Открыт | sort_order получать динамически, не хардкодить |
| 6 | Блокировка карточки через API | Решено | POST /cards/{id}/blockers с {"reason": "..."} (PATCH игнорируется) |
| 7 | Поле last_moved_at в ответе API | Открыт | Добавлено в Card и _parse_card, нужно проверить реально ли Kaiten возвращает это поле. Если нет — last_moved_at_parsed автоматически использует updated_at как fallback. |

---

## 6. Мультиаккаунт Kaiten: онбординг нового пользователя

> Механизм параметризации реализован в задаче C (июль 2026).
> Wave 2 (проводка из users.json в bot.py + переход handlers/scheduler на builder-методы) — отдельная задача, ещё не выполнена.

### Шаг 1 — Создать кастомные поля вручную в Kaiten UI

Kaiten API не поддерживает создание custom fields через API. Для каждого нового Kaiten-аккаунта нужно создать три поля вручную через интерфейс Kaiten (настройки пространства или доски):

| Поле | Тип | Варианты select (важен порядок!) |
|---|---|---|
| «Событие» | Дата + время | — |
| «Важность» | Select (одиночный) | среднее, важное, критическое |
| «День недели» | Select (одиночный) | ПН, ВТ, СР, ЧТ, ПТ, СБ, ВС |

Порядок вариантов select определяет их ID — первый вариант получит наименьший ID. Если создать варианты в другом порядке — ID будут другими и их нужно указать явно в конфиге пользователя.

### Шаг 2 — Узнать ID полей и вариантов

После создания полей нужно получить их реальные ключи и option ID:

```bash
# Создать тестовую карточку, заполнить все три поля, затем:
GET /cards/{card_id}
# В ответе найти объект properties — ключи вида "id_XXXXXX" и значения
```

Пример ответа:
```json
{
  "properties": {
    "id_590358": {"date": "2026-07-15", "time": "10:00:00", "tzOffset": 180},
    "id_590382": [17244397],
    "id_590359": [17244347]
  }
}
```

Из этого определяем `field_ids` и значения `importance_options` / `weekday_options`.

### Шаг 3 — Добавить конфиг пользователя в users.json

Новые опциональные ключи (если не заданы — используются дефолтные значения основного аккаунта):

```json
{
  "user_id": "bob",
  "telegram_chat_id": 987654321,
  "kaiten_board_id": 123456,
  "kaiten_lane_id": 0,
  "kaiten_space_id": 197396,
  "kaiten_token_env": "KAITEN_TOKEN_BOB",
  "kaiten_base_url_env": "KAITEN_BASE_URL_BOB",
  "tag_ids": {
    "ежедневно":   2000001,
    "по будням":   2000002,
    "по выходным": 2000003,
    "еженедельно": 2000004,
    "напомнить":   2000005
  },
  "importance_options": {
    "среднее":     20000010,
    "важное":      20000011,
    "критическое": 20000012
  },
  "weekday_options": {
    "ПН": 20000020,
    "ВТ": 20000021,
    "СР": 20000022,
    "ЧТ": 20000023,
    "ПТ": 20000024,
    "СБ": 20000025,
    "ВС": 20000026
  },
  "field_ids": {
    "event":      "id_600000",
    "importance": "id_600001",
    "weekday":    "id_600002"
  }
}
```

Если все четыре ключа отсутствуют — `KaitenClient` и `BoardLogic` используют глобальные дефолты из `kaiten_client.py` (значения основного аккаунта).

### Шаг 4 — Статус реализации

**Уже реализовано (Wave 1):**
- `KaitenClient.__init__` принимает `tag_ids`, `importance_options`, `weekday_options`, `field_ids`
- `_parse_card` нормализует properties под канонические ключи при нестандартных `field_ids`
- `BoardLogic._regular_tag_ids` строится через `client.tag_id()` — per-user множество
- Builder-методы: `client.tag_id()`, `client.event_time_property()`, `client.importance_property()`

**Ещё не сделано (Wave 2):**
- `user_config.py`: чтение `tag_ids`, `importance_options`, `weekday_options`, `field_ids` из users.json в `UserConfig`
- `bot.py`: передача этих значений в `KaitenClient(...)` при создании per-user клиента
- `morning_logic.py` / `handlers.py`: переход с хардкода на `client.event_time_property()` и `client.importance_property()`
