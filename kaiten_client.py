"""
kaiten_client.py — асинхронный HTTP-клиент к Kaiten API.

Конфиг из .env (модульные globals, общие для всего приложения):
    KAITEN_TOKEN      — Bearer-токен (fallback если token не передан в __init__)
    KAITEN_BASE_URL   — https://wonderlabst.kaiten.ru/api/latest
    KAITEN_SPACE_ID   — ID пространства (197396)

Board-специфичные параметры передаются в __init__:
    board_id  — ID доски (напр. 476640)
    lane_id   — ID дорожки (напр. 623640)
    token     — Bearer-токен пользователя (опционально, fallback → KAITEN_TOKEN из env)
    base_url  — базовый URL API (опционально, fallback → KAITEN_BASE_URL из env)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── Конфиг (модульные globals — общие для всех пользователей одной org) ────────

KAITEN_TOKEN    = os.getenv("KAITEN_TOKEN", "")
KAITEN_BASE_URL = os.getenv("KAITEN_BASE_URL", "")
KAITEN_SPACE_ID = int(os.getenv("KAITEN_SPACE_ID", "197396"))

TZ_MSK = timezone(timedelta(hours=3))

# ── Маппинги кастомных полей ───────────────────────────────────────────────────

IMPORTANCE_OPTIONS: dict[str, int] = {
    "среднее":     17244396,
    "важное":      17244397,
    "критическое": 17244398,
}
IMPORTANCE_BY_ID: dict[int, str] = {v: k for k, v in IMPORTANCE_OPTIONS.items()}

WEEKDAY_OPTIONS: dict[str, int] = {
    "ПН": 17244346,
    "ВТ": 17244347,
    "СР": 17244348,
    "ЧТ": 17244349,
    "ПТ": 17244350,
    "СБ": 17244351,
    "ВС": 17244352,
}
WEEKDAY_BY_ID: dict[int, str] = {v: k for k, v in WEEKDAY_OPTIONS.items()}

# Канонические плейсхолдеры для поля «Время дня» (4-е кастомное поле, задел на будущее).
# На основном аккаунте это поле не создано — значения 1/2/3 используются ТОЛЬКО как
# внутренние канонические ID при нормализации properties нового аккаунта.
# Канонический ключ properties: "id_time_of_day" (синтетический).
TIME_OF_DAY_OPTIONS: dict[str, int] = {"Утро": 1, "День": 2, "Вечер": 3}
TIME_OF_DAY_BY_ID: dict[int, str] = {v: k for k, v in TIME_OF_DAY_OPTIONS.items()}

# Теги
TAG_IDS: dict[str, int] = {
    "еженедельно": 407071,
    "по будням":   1074837,
    "по выходным": 1074843,
    "ежедневно":   1074844,
    "напомнить":   1076451,
}


# ── Dataclasses ───────────────────────────────────────────────────────────────

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
    block_reason: str | None
    description: str | None = None
    size: int | None = None
    due_date: str | None = None
    tag_ids: list[int] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    archived: bool = False
    state: int = 1
    updated_at: str | None = None
    last_moved_at: str | None = None

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def event_time(self) -> datetime | None:
        """Парсит properties.id_590358 → datetime (UTC+3).

        Формат значения: {"date": "YYYY-MM-DD", "time": "HH:MM:SS", "tzOffset": 180}
        Возвращает None если поле отсутствует или не парсится.
        """
        raw = self.properties.get("id_590358")
        if not raw or not isinstance(raw, dict):
            return None
        try:
            date_str = raw.get("date", "")
            time_str = raw.get("time", "00:00:00")
            dt_str   = f"{date_str}T{time_str}"
            dt = datetime.fromisoformat(dt_str)
            return dt.replace(tzinfo=TZ_MSK)
        except (ValueError, TypeError):
            return None

    @property
    def importance(self) -> str | None:
        """Парсит properties.id_590382 → 'среднее'/'важное'/'критическое' или None."""
        raw = self.properties.get("id_590382")
        if not raw or not isinstance(raw, list) or len(raw) == 0:
            return None
        return IMPORTANCE_BY_ID.get(raw[0])

    @property
    def weekday(self) -> str | None:
        """Парсит properties.id_590359 → 'ПН'/'ВТ'/.../'ВС' или None."""
        raw = self.properties.get("id_590359")
        if not raw or not isinstance(raw, list) or len(raw) == 0:
            return None
        return WEEKDAY_BY_ID.get(raw[0])

    @property
    def time_of_day(self) -> str | None:
        """Парсит properties.id_time_of_day → 'Утро'/'День'/'Вечер' или None.

        Поле не настроено для основного аккаунта — None является штатным состоянием.
        Заполняется только для аккаунтов, прошедших автонастройку через board_setup.
        """
        raw = self.properties.get("id_time_of_day")
        if not raw or not isinstance(raw, list) or len(raw) == 0:
            return None
        return TIME_OF_DAY_BY_ID.get(raw[0])

    @property
    def updated_at_parsed(self) -> datetime | None:
        """Парсит поле updated_at → datetime (aware, UTC+3) или None."""
        if not self.updated_at:
            return None
        try:
            # Kaiten возвращает ISO 8601 со смещением или без него
            dt = datetime.fromisoformat(self.updated_at.replace("Z", "+00:00"))
            return dt.astimezone(TZ_MSK)
        except (ValueError, TypeError):
            return None

    @property
    def last_moved_at_parsed(self) -> datetime | None:
        """Парсит last_moved_at (дата перемещения между колонками) → datetime (aware, UTC+3).
        Fallback на updated_at, если last_moved_at отсутствует в ответе API."""
        raw = self.last_moved_at or self.updated_at
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(TZ_MSK)
        except (ValueError, TypeError):
            return None

    @property
    def due_date_parsed(self) -> datetime | None:
        """Парсит поле due_date → datetime (aware, UTC+3) или None."""
        if not self.due_date:
            return None
        try:
            dt = datetime.fromisoformat(self.due_date.replace("Z", "+00:00"))
            return dt.astimezone(TZ_MSK)
        except (ValueError, TypeError):
            return None


# ── Вспомогательные парсеры ───────────────────────────────────────────────────

def _parse_column(data: dict) -> Column:
    return Column(
        id=data["id"],
        title=data.get("title", ""),
        sort_order=float(data.get("sort_order", 0)),
    )


def _parse_tag(data: dict) -> Tag:
    return Tag(id=data["id"], name=data.get("name", ""))


def _parse_card(
    data: dict,
    field_ids: dict[str, str] | None = None,
    importance_by_id: dict[int, str] | None = None,
    weekday_by_id: dict[int, str] | None = None,
    time_of_day_by_id: dict[int, str] | None = None,
) -> Card:
    """Парсит сырой dict карточки из Kaiten API в объект Card.

    Параметр field_ids — маппинг канонических ключей ("event", "importance", "weekday",
    "time_of_day") на реальные ключи свойств в Kaiten-аккаунте (напр. "id_590358").
    Если реальный ключ отличается от канонического — значение копируется под канонический
    ключ в properties, чтобы Card.event_time / .importance / .weekday / .time_of_day
    продолжали работать независимо от реальных ID полей в конкретном Kaiten-аккаунте.

    importance_by_id / weekday_by_id / time_of_day_by_id — реверс-маппинги реальных
    option_id → имя для текущего аккаунта. Если переданы — option_id в properties
    дополнительно транслируются в канонические ID (из IMPORTANCE_OPTIONS / WEEKDAY_OPTIONS /
    TIME_OF_DAY_OPTIONS), чтобы Card.importance / .weekday / .time_of_day возвращали
    корректный результат для аккаунта с нестандартными ID вариантов select.
    Для основного аккаунта трансляция является no-op (canonical_id == исходный id).
    """
    tags_raw = data.get("tags") or []

    # Нормализуем properties под канонические ключи, если field_ids нестандартные
    props = data.get("properties") or {}
    if field_ids:
        _canonical = {
            "event":       "id_590358",
            "importance":  "id_590382",
            "weekday":     "id_590359",
            "time_of_day": "id_time_of_day",
        }
        needs_remap = any(
            field_ids.get(k, v) != v for k, v in _canonical.items()
        )
        if needs_remap:
            props = dict(props)
            for key, canonical in _canonical.items():
                actual = field_ids.get(key, canonical)
                if actual != canonical and actual in props:
                    props[canonical] = props[actual]

    # Транслируем ID вариантов select в канонические (поддержка мульти-аккаунта).
    # Для основного аккаунта importance_by_id совпадает с IMPORTANCE_BY_ID,
    # поэтому трансляция является no-op — canonical_id == исходный id.
    _needs_val_remap = (
        (importance_by_id is not None and "id_590382" in props)
        or (weekday_by_id is not None and "id_590359" in props)
        or (time_of_day_by_id is not None and "id_time_of_day" in props)
    )
    if _needs_val_remap:
        props = dict(props)  # гарантируем изменяемую копию
        if importance_by_id and "id_590382" in props:
            raw_list = props["id_590382"]
            if isinstance(raw_list, list) and raw_list:
                name = importance_by_id.get(raw_list[0])
                canonical_id = IMPORTANCE_OPTIONS.get(name) if name else None
                if canonical_id is not None:
                    props["id_590382"] = [canonical_id]
        if weekday_by_id and "id_590359" in props:
            raw_list = props["id_590359"]
            if isinstance(raw_list, list) and raw_list:
                name = weekday_by_id.get(raw_list[0])
                canonical_id = WEEKDAY_OPTIONS.get(name) if name else None
                if canonical_id is not None:
                    props["id_590359"] = [canonical_id]
        if time_of_day_by_id and "id_time_of_day" in props:
            raw_list = props["id_time_of_day"]
            if isinstance(raw_list, list) and raw_list:
                name = time_of_day_by_id.get(raw_list[0])
                canonical_id = TIME_OF_DAY_OPTIONS.get(name) if name else None
                if canonical_id is not None:
                    props["id_time_of_day"] = [canonical_id]

    return Card(
        id=data["id"],
        title=data.get("title", ""),
        column_id=data.get("column_id", 0),
        sort_order=float(data.get("sort_order", 0)),
        blocked=bool(data.get("blocked", False)),
        block_reason=data.get("block_reason"),
        description=data.get("description"),
        size=data.get("size"),
        due_date=data.get("due_date"),
        tag_ids=data.get("tag_ids") or [],
        tags=[_parse_tag(t) for t in tags_raw if isinstance(t, dict)],
        properties=props,
        archived=bool(data.get("archived", False)),
        state=data.get("state", 1),
        updated_at=data.get("updated_at"),
        last_moved_at=data.get("last_moved_at"),
    )


# ── KaitenClient ──────────────────────────────────────────────────────────────

class KaitenClient:
    """Асинхронный HTTP-клиент к Kaiten REST API."""

    def __init__(
        self,
        board_id: int,
        lane_id: int,
        token: str | None = None,
        base_url: str | None = None,
        tag_ids: dict[str, int] | None = None,
        importance_options: dict[str, int] | None = None,
        weekday_options: dict[str, int] | None = None,
        field_ids: dict[str, str] | None = None,
        time_of_day_options: dict[str, int] | None = None,
        space_id: int | None = None,
    ) -> None:
        self._board_id = board_id
        self._lane_id = lane_id
        self._token = token or os.getenv("KAITEN_TOKEN", "")
        self._base_url = base_url or os.getenv("KAITEN_BASE_URL", "")
        if not self._token:
            logger.error("KaitenClient: KAITEN_TOKEN не задан для board_id={}", board_id)
            raise RuntimeError("KAITEN_TOKEN не задан")
        if not self._base_url:
            logger.error("KaitenClient: KAITEN_BASE_URL не задан для board_id={}", board_id)
            raise RuntimeError("KAITEN_BASE_URL не задан")
        # Параметризованные маппинги (поддержка мульти-аккаунта)
        self._tag_ids = tag_ids or dict(TAG_IDS)
        self._importance_options = importance_options or dict(IMPORTANCE_OPTIONS)
        self._importance_by_id = {v: k for k, v in self._importance_options.items()}
        self._weekday_options = weekday_options or dict(WEEKDAY_OPTIONS)
        self._weekday_by_id = {v: k for k, v in self._weekday_options.items()}
        self._time_of_day_options: dict[str, int] = time_of_day_options or {}
        self._time_of_day_by_id: dict[int, str] = {v: k for k, v in self._time_of_day_options.items()}
        self._field_ids = field_ids or {
            "event":       "id_590358",
            "importance":  "id_590382",
            "weekday":     "id_590359",
            "time_of_day": "id_time_of_day",
        }
        # Per-user space ID — fallback на модульную константу для обратной совместимости
        self._space_id = space_id or KAITEN_SPACE_ID
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KaitenClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ── Внутренние хелперы ────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any | None:
        """Выполняет запрос, логирует ошибки, возвращает распарсенный JSON или None."""
        try:
            resp = await self._client.request(method, url, params=params, json=json)
            if resp.status_code >= 400:
                logger.error(
                    "Kaiten API error: {} {} → HTTP {} | body: {}",
                    method, url, resp.status_code, resp.text[:300],
                )
                return None
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        except httpx.TimeoutException:
            logger.error("Kaiten API timeout: {} {}", method, url)
            return None
        except Exception as e:
            logger.exception("Kaiten API unexpected error: {} {} | {}", method, url, e)
            return None

    def _to_card(self, data: dict) -> Card:
        """Парсит dict карточки в Card, применяя field_ids и реверс-мапы этого инстанса."""
        return _parse_card(
            data,
            self._field_ids,
            importance_by_id=self._importance_by_id,
            weekday_by_id=self._weekday_by_id,
            time_of_day_by_id=self._time_of_day_by_id if self._time_of_day_by_id else None,
        )

    # ── Публичные методы ──────────────────────────────────────────────────────

    def tag_id(self, name: str) -> int | None:
        """Возвращает ID тега по имени для этого Kaiten-аккаунта или None."""
        return self._tag_ids.get(name)

    def event_time_property(self, dt: datetime) -> dict:
        """Возвращает {field_key: {...}} готовое для update_card(properties=...).

        dt — aware datetime (например, datetime.now(TZ_MSK)).
        Использует реальный ключ поля из self._field_ids["event"].
        """
        return {self._field_ids["event"]: {
            "date": dt.strftime("%Y-%m-%d"),
            "time": dt.strftime("%H:%M:%S"),
            "tzOffset": 180,
        }}

    def importance_property(self, name: str) -> dict | None:
        """Возвращает {field_key: [option_id]} для записи поля важности.

        name — 'среднее' | 'важное' | 'критическое'.
        Возвращает None если имя не найдено в importance_options.
        """
        opt_id = self._importance_options.get(name)
        if opt_id is None:
            return None
        return {self._field_ids["importance"]: [opt_id]}

    def weekday_property(self, name: str) -> dict | None:
        """Возвращает {field_key: [option_id]} для записи поля дня недели.

        name — 'ПН' | 'ВТ' | 'СР' | 'ЧТ' | 'ПТ' | 'СБ' | 'ВС'.
        Возвращает None если имя не найдено в weekday_options (например, для аккаунта
        без этого поля).
        """
        opt_id = self._weekday_options.get(name)
        if opt_id is None:
            return None
        return {self._field_ids["weekday"]: [opt_id]}

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

        Используется когда реальные ID кастомных полей/тегов становятся известны
        только во время выполнения — например, при автосоздании полей в board_setup.py
        во время онбординга нового Kaiten-аккаунта.
        """
        if field_ids is not None:
            self._field_ids.update(field_ids)
        if importance_options is not None:
            self._importance_options.update(importance_options)
            self._importance_by_id = {v: k for k, v in self._importance_options.items()}
        if weekday_options is not None:
            self._weekday_options.update(weekday_options)
            self._weekday_by_id = {v: k for k, v in self._weekday_options.items()}
        if tag_ids is not None:
            self._tag_ids.update(tag_ids)
        if time_of_day_options is not None:
            self._time_of_day_options = {**self._time_of_day_options, **time_of_day_options}
            self._time_of_day_by_id = {v: k for k, v in self._time_of_day_options.items()}

    async def get_columns(self) -> list[Column]:
        """GET /boards/{board_id}/columns → список колонок доски."""
        data = await self._request("GET", f"/boards/{self._board_id}/columns")
        if not isinstance(data, list):
            logger.warning("get_columns: неожиданный ответ: {}", data)
            return []
        result = [_parse_column(c) for c in data]
        logger.debug("get_columns: получено {} колонок", len(result))
        return result

    async def get_cards(self, column_id: int) -> list[Card]:
        """GET /spaces/{space_id}/cards?column_id=... с пагинацией.

        Возвращает ВСЕ карточки колонки — и задачи, и разделители (blocked=True).
        Запрашивает страницами по 100 пока API не вернёт меньше limit.
        """
        all_cards: list[Card] = []
        limit  = 100
        offset = 0
        while True:
            data = await self._request(
                "GET",
                f"/spaces/{self._space_id}/cards",
                params={"column_id": column_id, "limit": limit, "offset": offset},
            )
            if not isinstance(data, list):
                logger.warning(
                    "get_cards: неожиданный ответ для col_id={}: {}", column_id, data
                )
                break
            all_cards.extend(self._to_card(c) for c in data)
            if len(data) < limit:
                break
            offset += limit
        logger.debug("get_cards: col_id={} итого={}", column_id, len(all_cards))
        return all_cards

    async def get_card(self, card_id: int) -> Card | None:
        """GET /cards/{card_id} → полная структура карточки или None."""
        data = await self._request("GET", f"/cards/{card_id}")
        if not data or not isinstance(data, dict):
            logger.warning("get_card: карточка id={} не найдена", card_id)
            return None
        return self._to_card(data)

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
        """POST /cards → создаёт карточку. lane_id и board_id подставляются автоматически."""
        body: dict = {
            "title":     title,
            "board_id":  self._board_id,
            "column_id": column_id,
            "lane_id":   self._lane_id,
        }
        if description is not None:
            body["description"] = description
        if size is not None:
            body["size"] = size
            body["size_text"] = str(size)
        if due_date is not None:
            body["due_date"] = due_date
        if sort_order is not None:
            body["sort_order"] = sort_order
        if tag_ids:
            body["tag_ids"] = tag_ids
        if properties:
            body["properties"] = properties

        data = await self._request("POST", "/cards", json=body)
        if not data or not isinstance(data, dict):
            logger.error("create_card: не удалось создать карточку «{}»", title)
            return None
        card = self._to_card(data)
        logger.info("create_card: создана «{}» id={}", title, card.id)
        return card

    async def move_card(self, card_id: int, column_id: int, sort_order: float) -> Card | None:
        """PATCH /cards/{card_id} → перемещает карточку в другую колонку."""
        data = await self._request(
            "PATCH",
            f"/cards/{card_id}",
            json={"column_id": column_id, "sort_order": sort_order},
        )
        if not data or not isinstance(data, dict):
            logger.error(
                "move_card: не удалось переместить id={} → col={}", card_id, column_id
            )
            return None
        logger.debug("move_card: id={} → col={} so={:.4f}", card_id, column_id, sort_order)
        return self._to_card(data)

    async def update_card(self, card_id: int, **fields) -> Card | None:
        """PATCH /cards/{card_id} → обновляет произвольные поля карточки.

        Если среди полей передан `size` без явного `size_text` — автоматически
        добавляет `size_text` (строковое представление), т.к. Kaiten хранит/отображает
        размер через size_text, а не только через числовое size.
        """
        if "size" in fields and "size_text" not in fields:
            fields = {**fields, "size_text": str(fields["size"])}
        data = await self._request("PATCH", f"/cards/{card_id}", json=fields)
        if not data or not isinstance(data, dict):
            logger.error("update_card: не удалось обновить id={} fields={}", card_id, fields)
            return None
        logger.debug("update_card: id={} fields={}", card_id, list(fields.keys()))
        return self._to_card(data)

    async def archive_card(
        self, card_id: int, archive_column_id: int, comment: str | None = None
    ) -> bool:
        """Архивирует карточку: перемещает в архивную колонку.

        Если передан comment — добавляет его перед перемещением.
        Возвращает True при успехе.
        """
        if comment:
            await self.add_comment(card_id, comment)
        result = await self.move_card(card_id, archive_column_id, 1.0)
        if result is None:
            logger.error("archive_card: не удалось архивировать id={}", card_id)
            return False
        logger.info("archive_card: id={} → col={}", card_id, archive_column_id)
        return True

    async def add_comment(self, card_id: int, text: str) -> bool:
        """POST /cards/{card_id}/comments — добавляет комментарий к карточке.

        Возвращает True при успехе, False при ошибке.
        """
        data = await self._request(
            "POST",
            f"/cards/{card_id}/comments",
            json={"text": text},
        )
        if data is None:
            logger.error("add_comment: не удалось добавить комментарий к id={}", card_id)
            return False
        logger.info("add_comment: комментарий добавлен к id={}", card_id)
        return True

    async def get_comments(self, card_id: int) -> list[str]:
        """GET /cards/{card_id}/comments → список текстов комментариев.

        Комментарии возвращаются в хронологическом порядке (старые → новые).
        Сортировка: сначала по полю даты создания (created / created_at / createdAt /
        timestamp), fallback — по числовому id (монотонно возрастает), финальный
        fallback — исходный порядок ответа API (sort стабилен).
        Возвращает пустой список при ошибке или если комментариев нет.
        """
        data = await self._request("GET", f"/cards/{card_id}/comments")
        if not isinstance(data, list):
            logger.debug("get_comments: нет комментариев для id={}", card_id)
            return []

        dict_items = [item for item in data if isinstance(item, dict)]

        # Определяем поле для сортировки по первому элементу — логируем один раз
        date_field_used = "index"
        if dict_items:
            first = dict_items[0]
            _found = False
            for _candidate in ("created", "created_at", "createdAt", "timestamp"):
                _val = first.get(_candidate)
                if _val:
                    try:
                        datetime.fromisoformat(str(_val).replace("Z", "+00:00"))
                        date_field_used = _candidate
                        _found = True
                        break
                    except (ValueError, TypeError):
                        pass
            if not _found and isinstance(first.get("id"), (int, float)):
                date_field_used = "id"
        logger.debug(
            "get_comments: id={} сортировка по полю='{}'", card_id, date_field_used
        )

        def _sort_key(indexed_item: tuple[int, dict]) -> tuple[int, float, int, int]:
            idx, item = indexed_item
            for candidate in ("created", "created_at", "createdAt", "timestamp"):
                val = item.get(candidate)
                if val:
                    try:
                        parsed = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
                        return (0, parsed.timestamp(), 0, idx)
                    except (ValueError, TypeError):
                        pass
            id_val = item.get("id")
            if isinstance(id_val, (int, float)):
                return (1, 0.0, int(id_val), idx)
            return (2, 0.0, 0, idx)

        # enumerate по исходному data сохраняет оригинальный индекс для стабильности
        indexed = [(i, item) for i, item in enumerate(data) if isinstance(item, dict)]
        indexed.sort(key=_sort_key)

        texts: list[str] = []
        for _, item in indexed:
            text = item.get("text") or item.get("body") or ""
            if text:
                texts.append(str(text))

        logger.debug("get_comments: id={} комментариев={}", card_id, len(texts))
        return texts

    async def delete_card(self, card_id: int) -> bool:
        """DELETE /cards/{card_id} — удаляет карточку навсегда.

        Kaiten возвращает 204 при успехе.
        Возвращает True при успехе, False при ошибке.
        """
        data = await self._request("DELETE", f"/cards/{card_id}")
        if data is None:
            logger.error("delete_card: не удалось удалить id={}", card_id)
            return False
        logger.info("delete_card: удалена карточка id={}", card_id)
        return True

    async def get_lanes(self) -> list[dict]:
        """GET /boards/{board_id}/lanes → список дорожек доски.

        Возвращает список сырых dict (поля id, title и т.д.).
        """
        data = await self._request("GET", f"/boards/{self._board_id}/lanes")
        if not isinstance(data, list):
            logger.warning("get_lanes: неожиданный ответ: {}", data)
            return []
        logger.debug("get_lanes: получено {} дорожек", len(data))
        return data

    async def create_column(self, title: str, sort_order: float = 1000.0) -> dict | None:
        """POST /boards/{board_id}/columns → создаёт колонку.

        Возвращает сырой dict с id или None при ошибке.
        """
        data = await self._request(
            "POST",
            f"/boards/{self._board_id}/columns",
            json={"title": title, "sort_order": sort_order},
        )
        if not data or not isinstance(data, dict):
            logger.error("create_column: не удалось создать колонку «{}»", title)
            return None
        logger.info("create_column: создана «{}» id={}", title, data.get("id"))
        return data

    async def delete_column(self, column_id: int) -> bool:
        """DELETE /boards/{board_id}/columns/{column_id} → удаляет колонку.

        Возвращает True при успехе, False при ошибке.
        """
        data = await self._request(
            "DELETE",
            f"/boards/{self._board_id}/columns/{column_id}",
        )
        if data is None:
            logger.error("delete_column: не удалось удалить колонку id={}", column_id)
            return False
        logger.info("delete_column: удалена колонка id={}", column_id)
        return True

    async def add_tag_by_name(self, card_id: int, tag_name: str) -> bool:
        """POST /cards/{card_id}/tags — добавляет тег к карточке по имени.

        Тег передаётся по имени (не по ID), что обеспечивает совместимость
        с мульти-пользовательским режимом, где ID тегов могут отличаться.
        Возвращает True при успехе, False при ошибке.
        """
        data = await self._request(
            "POST",
            f"/cards/{card_id}/tags",
            json={"name": tag_name},
        )
        if data is None:
            logger.error("add_tag_by_name: не удалось добавить тег «{}» к карточке id={}", tag_name, card_id)
            return False
        logger.info("add_tag_by_name: тег «{}» добавлен к карточке id={}", tag_name, card_id)
        return True

    async def remove_tag_by_name(self, card_id: int, tag_name: str) -> bool:
        """DELETE /cards/{card_id}/tags/{tag_id} — удаляет тег с карточки по имени.

        Резолвит tag_id через self.tag_id(tag_name) (per-инстанс маппинг, мульти-аккаунт).
        Если тег с таким именем не сконфигурирован для этого аккаунта — логирует warning, возвращает False.
        ЭНДПОИНТ НЕ ПОДТВЕРЖДЁН ЭМПИРИЧЕСКИ (симметричен POST .../tags по конвенции Kaiten REST API,
        но реального теста на проде не было) — при HTTP-ошибке (_request вернёт None) логировать error
        и вернуть False, не бросать исключение. Если в проде эндпоинт окажется другим — это будет видно
        по логам при первом использовании (баг всплывёт при реальном редактировании регулярности).
        Возвращает True при успехе, False при ошибке/если тег не найден в конфигурации.
        """
        tid = self.tag_id(tag_name)
        if tid is None:
            logger.warning("remove_tag_by_name: тег «{}» не сконфигурирован для этого аккаунта", tag_name)
            return False
        data = await self._request("DELETE", f"/cards/{card_id}/tags/{tid}")
        if data is None:
            logger.error(
                "remove_tag_by_name: не удалось удалить тег «{}» (id={}) с карточки id={}",
                tag_name, tid, card_id,
            )
            return False
        logger.info("remove_tag_by_name: тег «{}» удалён с карточки id={}", tag_name, card_id)
        return True

    async def block_card(self, card_id: int, reason: str) -> bool:
        """POST /cards/{card_id}/blockers — блокирует карточку.

        Единственный рабочий способ заблокировать карточку через Kaiten API
        (PATCH с blocked/block_reason игнорируется API).
        Возвращает True при успехе (статус 200), False при ошибке.
        """
        data = await self._request(
            "POST",
            f"/cards/{card_id}/blockers",
            json={"reason": reason},
        )
        if data is None:
            logger.warning(
                "block_card: не удалось заблокировать карточку id={} reason=«{}»",
                card_id, reason,
            )
            return False
        logger.info("block_card: карточка id={} заблокирована, reason=«{}»", card_id, reason)
        return True

    async def create_blocked_card(
        self,
        column_id: int,
        title: str,
        block_reason: str,
        sort_order: float = 1.0,
    ) -> dict | None:
        """Создаёт карточку-разделитель (blocked=True) в указанной колонке.

        Kaiten API игнорирует поля blocked/block_reason при POST, поэтому
        после создания карточки выполняется отдельный POST /blockers для блокировки.
        """
        body = {
            "title": title,
            "board_id": self._board_id,
            "column_id": column_id,
            "lane_id": self._lane_id,
            "sort_order": sort_order,
        }
        data = await self._request("POST", "/cards", json=body)
        if not data or not isinstance(data, dict):
            logger.error("create_blocked_card: не удалось создать карточку «{}»", title)
            return None
        card_id = data.get("id")
        if not card_id:
            return data
        # Блокируем через /blockers (PATCH с blocked/block_reason игнорируется Kaiten)
        ok = await self.block_card(card_id, block_reason)
        if not ok:
            logger.warning(
                "create_blocked_card: карточка создана (id={}) но не заблокирована", card_id
            )
        return data

    async def create_custom_property(
        self, name: str, prop_type: str, multi_select: bool | None = None
    ) -> dict | None:
        """POST /company/custom-properties — создаёт кастомное поле в аккаунте Kaiten.

        name       — отображаемое имя поля (напр. "Важность")
        prop_type  — тип поля: "date", "select", "text" и др.
        multi_select — для типа "select": True = мультивыбор, False = одиночный выбор.
                       Если None — параметр не передаётся.

        Возвращает распарсенный JSON ответа (содержит "id" созданного поля) или None при ошибке.
        """
        body: dict = {"name": name, "type": prop_type}
        if multi_select is not None:
            body["multi_select"] = multi_select
        return await self._request("POST", "/company/custom-properties", json=body)

    async def create_select_value(self, property_id: int, value: str) -> dict | None:
        """POST /company/custom-properties/{property_id}/select-values — добавляет вариант select.

        property_id — ID поля типа "select" (из create_custom_property).
        value       — отображаемое значение варианта (напр. "критическое").

        Возвращает JSON ответа (содержит "id" созданного варианта) или None при ошибке.
        """
        return await self._request(
            "POST",
            f"/company/custom-properties/{property_id}/select-values",
            json={"value": value},
        )

    async def get_tags(self) -> list[dict]:
        """GET /company/tags — список всех тегов аккаунта Kaiten.

        Возвращает список dict с полями "id", "name" и др.
        При ошибке возвращает пустой список (не None) — вызывающий код итерирует напрямую.
        """
        data = await self._request("GET", "/company/tags")
        return data if isinstance(data, list) else []

    async def get_custom_properties(self) -> list[dict]:
        """GET /company/custom-properties — список существующих кастомных полей аккаунта.

        Возвращает список dict с полями "id", "name", "type" и др., или пустой список
        при ошибке / если эндпоинт не поддерживается данным инстансом Kaiten.

        Используется в board_setup.py для защиты от дублей при повторной попытке
        автосоздания кастомных полей (если предыдущая попытка прервалась на середине).
        Если эндпоинт вернёт 404 или любую ошибку — _request вернёт None,
        isinstance(None, list) = False, метод корректно вернёт [] и защита от дублей
        отключится, не ломая логику создания полей.
        """
        data = await self._request("GET", "/company/custom-properties")
        return data if isinstance(data, list) else []
