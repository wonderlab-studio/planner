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
| Создать кастомное поле | POST | `/company/custom-properties` — body: `{"name": str, "type": str, ["multi_select": bool]}` |
| Добавить вариант select | POST | `/company/custom-properties/{property_id}/select-values` — body: `{"value": str}` |
| Список тегов аккаунта | GET | `/company/tags` |
| Список кастомных полей | GET | `/company/custom-properties` — не подтверждён эмпирически; при ошибке возвращает [] |

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
| `id_time_of_day` | `[int]` | **time_of_day** (время дня, select) | синтетический ключ — используется только у аккаунтов с автосозданными полями |

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

# Канонические плейсхолдеры (реальных ID на основном аккаунте нет)
TIME_OF_DAY_OPTIONS = {"Утро": 1, "День": 2, "Вечер": 3}
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

### Пример создания кастомного поля (POST /company/custom-properties)

```json
{
  "name": "Важность",
  "type": "select",
  "multi_select": false
}
```

Ответ содержит `"id"` — используется для добавления вариантов:

### Пример добавления варианта select (POST /company/custom-properties/{id}/select-values)

```json
{
  "value": "критическое"
}
```

Ответ содержит `"id"` варианта — это и есть option_id, записываемый в `properties`.

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
        tag_ids: dict[str, int] | None = None,              # fallback → глобальный TAG_IDS
        importance_options: dict[str, int] | None = None,   # fallback → глобальный IMPORTANCE_OPTIONS
        weekday_options: dict[str, int] | None = None,      # fallback → глобальный WEEKDAY_OPTIONS
        field_ids: dict[str, str] | None = None,            # fallback → {"event": "id_590358", ...}
        time_of_day_options: dict[str, int] | None = None,  # fallback → {} (на основном аккаунте поля нет)
    ): ...
    # field_ids canonical keys: "event", "importance", "weekday", "time_of_day"
    # Все параметры опциональны — обратная совместимость полная.

    # ── Методы-билдеры и конфигурации ────────────────────────────────────────

    def tag_id(self, name: str) -> int | None:
        """Возвращает ID тега по имени для данного Kaiten-аккаунта или None."""

    def event_time_property(self, dt: datetime) -> dict:
        """dt — aware datetime. Возвращает {field_key: {...}} готовое для update_card(properties=...).
        Использует реальный ключ поля из self._field_ids["event"]."""

    def importance_property(self, name: str) -> dict | None:
        """name — 'среднее'|'важное'|'критическое'.
        Возвращает {field_key: [option_id]} или None если имя не найдено."""

    def configure_custom_fields(
        self,
        *,
        field_ids: dict[str, str] | None = None,
        importance_options: dict[str, int] | None = None,
        weekday_options: dict[str, int] | None = None,
        tag_ids: dict[str, int] | None = None,
        time_of_day_options: dict[str, int] | None = None,
    ) -> None:
        """Обновляет instance-конфигурацию клиента ПОСЛЕ __init__.
        Все параметры — keyword-only, все опциональны (None = не трогать соответствующий маппинг).
        Вызывается из board_setup.py после автосоздания полей/тегов для нового аккаунта."""

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

    async def create_custom_property(
        self, name: str, prop_type: str, multi_select: bool | None = None
    ) -> dict | None:
        """POST /company/custom-properties — создаёт кастомное поле в аккаунте Kaiten.
        prop_type: "date", "select", "text" и др.
        multi_select: для "select" — True/False/None (не передавать).
        Возвращает JSON ответа (содержит "id") или None при ошибке."""

    async def create_select_value(self, property_id: int, value: str) -> dict | None:
        """POST /company/custom-properties/{property_id}/select-values
        Добавляет вариант select к кастомному полю.
        Возвращает JSON ответа (содержит "id" варианта) или None при ошибке."""

    async def get_tags(self) -> list[dict]:
        """GET /company/tags — список всех тегов аккаунта.
        Возвращает list[dict] с полями "id", "name" и др.
        При ошибке возвращает [] (не None) — вызывающий код итерирует напрямую."""

    async def get_custom_properties(self) -> list[dict]:
        """GET /company/custom-properties — список существующих кастомных полей аккаунта.
        Возвращает list[dict] с полями "id", "name", "type" и др.
        При ошибке (в т.ч. 404 если эндпоинт не поддерживается) возвращает [] — защита
        от дублей в board_setup отключается, не ломая логику создания полей.
        Эндпоинт не подтверждён эмпирически — по аналогии с GET /company/tags."""
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

## 4. board_setup.py — публичный интерфейс

```python
async def setup_board(
    client: KaitenClient,
    user: UserConfig,
    *,
    needs_custom_fields: bool = True,
) -> tuple[dict[str, int], dict | None]:
    """
    Настраивает доску пользователя.

    Параметр needs_custom_fields определяется вызывающим кодом (bot.py) на основе
    того, есть ли уже сохранённая конфигурация полей для пользователя (field_ids в
    SQLite или users.json). True = создать кастомные поля и теги. False = пропустить
    (поля уже настроены). Это делает механизм устойчивым к частичным сбоям: даже
    если колонки уже существуют (созданы при предыдущем неполном запуске), поля
    будут созданы при следующем перезапуске, пока field_ids не сохранены.

    Возвращает (column_ids, discovered_config):
        column_ids       — dict[str, int]: маппинг имя → id всех стандартных колонок
        discovered_config — dict с ключами field_ids / importance_options / weekday_options /
                            time_of_day_options / tag_ids если needs_custom_fields=True
                            и создание завершилось успешно, иначе None.

    Побочные эффекты:
        - user.kaiten_lane_id обновляется если был 0
        - user.column_ids обновляется на месте
        - client.configure_custom_fields() вызывается если discovered_config не None

    Сигнатура изменена в июле 2026:
    - (июль 2026 v1) возвращала только dict[str, int]
    - (июль 2026 v2) добавлен второй элемент кортежа discovered_config
    - (июль 2026 v3) добавлен параметр needs_custom_fields; убран is_new_board
      (триггер перенесён из состояния колонок в отсутствие сохранённой конфигурации)

    Вызывающий код (bot.py) должен:
        column_ids, cfg = await setup_board(client, user, needs_custom_fields=not has_field_config)
    """
```

---

## 5. Dataclasses (типы)

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
        """Парсит properties.id_590382 → 'среднее'/'важное'/'критическое'
        После нормализации _parse_card значение всегда каноническое — даже для
        аккаунтов с другими option_id."""

    @property
    def weekday(self) -> str | None:
        """Парсит properties.id_590359 → 'ПН'/'ВТ'/...'ВС'
        После нормализации _parse_card значение всегда каноническое."""

    @property
    def time_of_day(self) -> str | None:
        """Парсит properties.id_time_of_day → 'Утро'/'День'/'Вечер' или None.
        Поле не существует на основном аккаунте — None является штатным состоянием.
        Заполняется только для аккаунтов, прошедших автонастройку в board_setup."""

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

## 6. Открытые вопросы (TODO)

| # | Вопрос | Статус | Как решить |
|---|---|---|---|
| 1 | option_id для importance | Решено | среднее=17244396, важное=17244397, критическое=17244398 |
| 2 | option_id для weekday | Решено | ПН=17244346 … ВС=17244352 |
| 3 | Пагинация: точные параметры offset/limit | Решено | limit=100, offset+=100 пока len(batch) < limit |
| 4 | Эндпоинт архивирования: PATCH move или отдельный | Решено | PATCH /cards/{id} с column_id=6122269 |
| 5 | Разделители в других колонках | Открыт | sort_order получать динамически, не хардкодить |
| 6 | Блокировка карточки через API | Решено | POST /cards/{id}/blockers с {"reason": "..."} (PATCH игнорируется) |
| 7 | Поле last_moved_at в ответе API | Открыт | Добавлено в Card и _parse_card, нужно проверить реально ли Kaiten возвращает это поле. Если нет — last_moved_at_parsed автоматически использует updated_at как fallback. |
| 8 | GET /company/custom-properties | Открыт | Эндпоинт добавлен в get_custom_properties() по аналогии с /company/tags, но не проверен эмпирически. При 404 метод вернёт [] и защита от дублей отключится (безопасный fallback). |

---

## 7. Мультиаккаунт Kaiten: онбординг нового пользователя

> Wave 1 (параметризация KaitenClient) — реализована, июль 2026.
> Wave 1.5 (автосоздание полей/тегов через board_setup) — реализована, июль 2026.
> Wave 1.6 (needs_custom_fields + защита от дублей) — реализована, июль 2026.
> Wave 2 (проводка из users.json в bot.py + переход handlers/scheduler на builder-методы) — отдельная задача, ещё не выполнена.

### Автоматическое создание полей и тегов (новая доска)

При первом запуске для нового Kaiten-аккаунта `setup_board` получает `needs_custom_fields=True`
(вызывающий код bot.py определяет это по отсутствию сохранённого field_ids для пользователя)
и выполняет полную настройку. Этот триггер не зависит от состояния колонок, что делает
механизм устойчивым к частичным сбоям.

**Создаваемые кастомные поля** (через `POST /company/custom-properties`):

| Поле | Тип | Варианты |
|---|---|---|
| «Событие» | `date` | — |
| «Важность» | `select` (одиночный) | среднее, важное, критическое |
| «День недели» | `select` (одиночный) | ПН, ВТ, СР, ЧТ, ПТ, СБ, ВС |
| «Время дня» | `select` (одиночный) | Утро, День, Вечер |

Варианты select создаются через `POST /company/custom-properties/{id}/select-values`.
Порядок создания вариантов определяет их ID — сервис не предполагает фиксированный порядок,
а получает реальные ID из ответа API.

**Защита от дублей при повторной попытке** (если предыдущая попытка прервалась на середине):
Перед созданием каждого поля `board_setup` вызывает `client.get_custom_properties()` и
проверяет, не существует ли поле с таким именем уже. Если существует — переиспользует ID
вместо повторного создания. Варианты select (среднее/важное/...) не проверяются на дубли —
осознанный компромисс ради простоты. Если `get_custom_properties()` вернёт `[]` (эндпоинт
не поддерживается) — защита отключается, поля создаются без проверки.

**Получение ID тегов** — через временную карточку:
1. Создать карточку-заглушку в колонке «Долгий ящик».
2. Добавить к ней все нужные теги через `POST /cards/{id}/tags` (по имени).
3. Получить все теги аккаунта через `GET /company/tags`.
4. Сопоставить имена → ID.
5. Удалить карточку-заглушку (`DELETE /cards/{id}`).

Создаваемые теги: `ежедневно`, `по будням`, `по выходным`, `еженедельно`, `напомнить`,
`вечерняя`, `жёсткое событие`, `не дробить`, `рабочая`.

Теги идемпотентны на стороне Kaiten (POST по имени переиспользует существующий тег),
поэтому для тегов защита от дублей не нужна.

**Результат**: `setup_board` вызывает `client.configure_custom_fields(...)` с реальными ID,
обновляя инстанс клиента на месте, и возвращает `discovered_config` (dict) вторым элементом
кортежа — вызывающий код (bot.py) должен сохранить его в `users.json` для следующих запусков.

### Ручная настройка (если автосоздание не сработало)

Новые опциональные ключи в `users.json` (если не заданы — используются дефолтные значения основного аккаунта):

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
    "event":       "id_600000",
    "importance":  "id_600001",
    "weekday":     "id_600002",
    "time_of_day": "id_600003"
  },
  "time_of_day_options": {
    "Утро":  30000001,
    "День":  30000002,
    "Вечер": 30000003
  }
}
```

Если все ключи отсутствуют — `KaitenClient` и `BoardLogic` используют глобальные дефолты
из `kaiten_client.py` (значения основного аккаунта).

### Статус реализации

**Уже реализовано (Wave 1 + 1.5 + 1.6):**
- `KaitenClient.__init__` принимает `tag_ids`, `importance_options`, `weekday_options`, `field_ids`, `time_of_day_options`
- `KaitenClient.configure_custom_fields()` — обновление конфигурации после `__init__`
- `KaitenClient.get_custom_properties()` — список существующих полей (best-effort, защита от дублей)
- `_parse_card` нормализует ключи properties И транслирует option_id в канонические значения
- `BoardLogic._regular_tag_ids` строится через `client.tag_id()` — per-user множество
- Builder-методы: `client.tag_id()`, `client.event_time_property()`, `client.importance_property()`
- `client.create_custom_property()`, `client.create_select_value()`, `client.get_tags()`
- `board_setup.setup_board(needs_custom_fields=...)` — триггер по отсутствию конфигурации,
  не зависит от состояния колонок; защита от дублей при повторной попытке
- `Card.time_of_day` — свойство для 4-го кастомного поля

**Ещё не сделано (Wave 2):**
- `user_config.py`: чтение `tag_ids`, `importance_options`, `weekday_options`, `field_ids`, `time_of_day_options` из users.json в `UserConfig`
- `bot.py`: передача этих значений в `KaitenClient(...)` при создании per-user клиента; распаковка `(column_ids, cfg) = await setup_board(client, user, needs_custom_fields=not has_field_config)` и сохранение `cfg` в users.json
- `morning_logic.py` / `handlers.py`: переход с хардкода на `client.event_time_property()` и `client.importance_property()`

---

## 8. Недокументированные особенности Kaiten API (обнаруженные эмпирически)

Эти особенности нельзя найти чтением кода сервиса — они свойства самого Kaiten API,
обнаруженные через анализ сырых HTTP-ответов. При отладке необъяснимых расхождений
(поле записывается, но не читается / значение сохраняется не так, как ожидается) —
сравнивать сырые GET-ответы до и после PATCH, а не полагаться только на статус HTTP.

### Поля size / size_text / size_unit

Kaiten хранит размер карточки как триаду полей в сыром ответе `GET /cards/{id}`:

```json
{
  "size": 2,
  "size_text": "2",
  "size_unit": "hours"
}
```

- `size` — целое число; единственное поле, которое нужно читать и устанавливать через PATCH
- `size_text` — строковое представление; Kaiten вычисляет сам, только для чтения
- `size_unit` — единица измерения, по умолчанию `"hours"`; только для чтения

При установке: `PATCH /cards/{card_id}` с `{"size": 2}` — достаточно только `size`.
В `_parse_card` читается только `size` — это корректно, `size_text` и `size_unit` игнорировать.
