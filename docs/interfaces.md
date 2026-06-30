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
| Добавить комментарий | POST | `/cards/{card_id}/comments` |

> ⚠️ Авторизация: заголовок `Authorization: Bearer {KAITEN_TOKEN}`  
> ⚠️ API возвращает максимум 100 карточек за запрос — необходима пагинация  
> (параметры: `?limit=100&offset=0` — уточнить при интеграции)

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

> ℹ️ sort_order разделителей одинаковы во всех колонках — они создаются один раз  
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

---

## 2. KaitenClient — публичные методы

```python
# kaiten_client.py
# Стек: httpx (async), loguru, python-dotenv

class KaitenClient:

    async def get_columns(self) -> list[Column]:
        """GET /boards/{board_id}/columns
        Возвращает все колонки доски с id и title."""

    async def get_cards(self, column_id: int) -> list[Card]:
        """GET /spaces/{space_id}/cards?column_id={column_id}
        Возвращает ВСЕ карточки колонки с пагинацией (limit=100, offset).
        Включает и разделители (blocked=True), и задачи."""

    async def get_card(self, card_id: int) -> Card:
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
    ) -> Card:
        """POST /cards
        Создаёт карточку. lane_id и board_id подставляются автоматически."""

    async def move_card(self, card_id: int, column_id: int, sort_order: float) -> Card:
        """PATCH /cards/{card_id}
        Перемещает карточку в другую колонку с указанным sort_order."""

    async def update_card(self, card_id: int, **fields) -> Card:
        """PATCH /cards/{card_id}
        Обновляет произвольные поля карточки."""

    async def archive_card(self, card_id: int, comment: str | None = None) -> None:
        """Перемещает карточку в колонку Архив (id=6122269).
        Если передан comment — добавляет комментарий перед перемещением."""

    async def add_comment(self, card_id: int, text: str) -> None:
        """POST /cards/{card_id}/comments  body={"text": text}"""
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
        """Возвращает column_id колонки текущего дня недели."""

    def get_yesterday_column_id(self) -> int:
        """Возвращает column_id колонки вчерашнего дня."""

    async def get_section_sort_order(
        self,
        column_id: int,
        section: str,   # "Утро" | "День" | "Вечер" | "На контроле"
    ) -> float:
        """Возвращает sort_order для вставки ПЕРВОЙ позицией в секцию.
        Алгоритм: sort_order разделителя + epsilon (если секция пуста)
        или среднее между разделителем и первой задачей секции."""

    def sort_cards_by_priority(self, cards: list[Card]) -> list[Card]:
        """Сортировка задач для расстановки по дню.
        Порядок: 1) есть event_time → по времени события
                 2) due_date == сегодня → срочные вперёд
                 3) importance: критическое > важное > среднее
                 4) size: меньше → раньше (быстрые задачи вперёд)
        Разделители (blocked=True) из сортировки исключаются."""

    def is_regular_task(self, card: Card) -> bool:
        """True если карточка имеет тег ежедневно/по будням/по выходным/еженедельно."""

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

    # Удобные свойства (вычисляются из properties)
    @property
    def event_time(self) -> datetime | None:
        """Парсит properties.id_590358 → datetime (UTC+3)"""

    @property
    def importance(self) -> str | None:
        """Парсит properties.id_590382 → 'среднее'/'важное'/'критическое'"""

    @property
    def weekday(self) -> str | None:
        """Парсит properties.id_590359 → 'ПН'/'ВТ'/...'ВС'"""
```

---

## 5. Открытые вопросы (TODO)

| # | Вопрос | Статус | Как решить |
|---|---|---|---|
| 1 | option_id для importance | ✅ Решено | среднее=17244396, важное=17244397, критическое=17244398 |
| 2 | option_id для weekday | ✅ Решено | ПН=17244346 … ВС=17244352 |
| 3 | Пагинация: точные параметры offset/limit | 🔲 Открыт | Проверить при реализации `get_cards()` |
| 4 | Эндпоинт архивирования: PATCH move или отдельный | 🔲 Открыт | Проверить `PATCH /cards/{id}` с `"column_id": 6122269` |
| 5 | Разделители в других колонках | 🔲 Открыт | sort_order получать динамически, не хардкодить |
