"""
kaiten_client.py — асинхронный HTTP-клиент к Kaiten API.

Конфиг из .env:
    KAITEN_TOKEN      — Bearer-токен
    KAITEN_BASE_URL   — https://wonderlabst.kaiten.ru/api/latest
    KAITEN_SPACE_ID   — ID пространства (197396)
    KAITEN_BOARD_ID   — ID доски (476640)
    KAITEN_LANE_ID    — ID дорожки (623640)
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

# ── Конфиг ────────────────────────────────────────────────────────────────────

KAITEN_TOKEN    = os.getenv("KAITEN_TOKEN", "")
KAITEN_BASE_URL = os.getenv("KAITEN_BASE_URL", "")
KAITEN_SPACE_ID = int(os.getenv("KAITEN_SPACE_ID", "197396"))
KAITEN_BOARD_ID = int(os.getenv("KAITEN_BOARD_ID", "476640"))
KAITEN_LANE_ID  = int(os.getenv("KAITEN_LANE_ID",  "623640"))

ARCHIVE_COLUMN_ID = 6122269

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
    block_reason: str | None        # "Утро" / "День" / "Вечер" / "На контроле"
    description: str | None = None
    size: int | None = None         # complexity (часы)
    due_date: str | None = None     # ISO datetime строка
    tag_ids: list[int] = field(default_factory=list)
    tags: list[Tag] = field(default_factory=list)
    properties: dict = field(default_factory=dict)
    archived: bool = False
    state: int = 1

    @property
    def event_time(self) -> datetime | None:
        """Парсит properties["id_590358"] → datetime в UTC+3."""
        raw = self.properties.get("id_590358")
        if not raw or not isinstance(raw, dict):
            return None
        try:
            date_str = raw.get("date", "")
            time_str = raw.get("time", "00:00:00")
            tz_offset = raw.get("tzOffset", 180)
            tz = timezone(timedelta(minutes=tz_offset))
            return datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=tz)
        except Exception:
            return None

    @property
    def importance(self) -> str | None:
        """Парсит properties["id_590382"] → 'среднее'/'важное'/'критическое'."""
        raw = self.properties.get("id_590382")
        if not raw or not isinstance(raw, list):
            return None
        return IMPORTANCE_BY_ID.get(raw[0])

    @property
    def weekday(self) -> str | None:
        """Парсит properties["id_590359"] → 'ПН'/'ВТ'/...'ВС'."""
        raw = self.properties.get("id_590359")
        if not raw or not isinstance(raw, list):
            return None
        return WEEKDAY_BY_ID.get(raw[0])

    @property
    def due_date_parsed(self) -> datetime | None:
        """Парсит due_date → datetime (UTC)."""
        if not self.due_date:
            return None
        try:
            return datetime.fromisoformat(self.due_date.replace("Z", "+00:00"))
        except Exception:
            return None


# ── Фабричные функции ─────────────────────────────────────────────────────────

def _parse_tag(raw: dict) -> Tag:
    return Tag(id=raw["id"], name=raw.get("name", ""))


def _parse_card(raw: dict) -> Card:
    tags_raw = raw.get("tags") or []
    return Card(
        id=raw["id"],
        title=raw.get("title", ""),
        column_id=raw.get("column_id", 0),
        sort_order=float(raw.get("sort_order") or 0),
        blocked=bool(raw.get("blocked", False)),
        block_reason=raw.get("block_reason"),
        description=raw.get("description"),
        size=raw.get("size"),
        due_date=raw.get("due_date"),
        tag_ids=raw.get("tag_ids") or [],
        tags=[_parse_tag(t) for t in tags_raw],
        properties=raw.get("properties") or {},
        archived=bool(raw.get("archived", False)),
        state=raw.get("state", 1),
    )


def _parse_column(raw: dict) -> Column:
    return Column(
        id=raw["id"],
        title=raw.get("title", ""),
        sort_order=float(raw.get("sort_order") or 0),
    )


# ── KaitenClient ──────────────────────────────────────────────────────────────

class KaitenClient:
    """Асинхронный клиент к Kaiten REST API."""

    def __init__(self) -> None:
        if not KAITEN_TOKEN:
            logger.warning("KAITEN_TOKEN не задан — запросы к API будут падать с 401")
        self._client = httpx.AsyncClient(
            base_url=KAITEN_BASE_URL,
            headers={
                "Authorization": f"Bearer {KAITEN_TOKEN}",
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

    # ── Внутренний хелпер ─────────────────────────────────────────────────────

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

    # ── Публичные методы ──────────────────────────────────────────────────────

    async def get_columns(self) -> list[Column]:
        """GET /boards/{board_id}/columns → список колонок доски."""
        data = await self._request("GET", f"/boards/{KAITEN_BOARD_ID}/columns")
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
        limit = 100
        offset = 0

        while True:
            data = await self._request(
                "GET",
                f"/spaces/{KAITEN_SPACE_ID}/cards",
                params={"column_id": column_id, "limit": limit, "offset": offset},
            )
            if not isinstance(data, list):
                logger.warning("get_cards({}): неожиданный ответ на offset={}", column_id, offset)
                break

            batch = [_parse_card(c) for c in data]
            all_cards.extend(batch)
            logger.debug(
                "get_cards({}): offset={} получено={} накоплено={}",
                column_id, offset, len(batch), len(all_cards),
            )

            if len(data) < limit:
                break  # последняя страница
            offset += limit

        return all_cards

    async def get_card(self, card_id: int) -> Card | None:
        """GET /cards/{card_id} — полная структура карточки."""
        data = await self._request("GET", f"/cards/{card_id}")
        if not data:
            return None
        return _parse_card(data)

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
        """POST /cards — создаёт карточку.

        board_id и lane_id подставляются автоматически из конфига.
        """
        body: dict[str, Any] = {
            "board_id": KAITEN_BOARD_ID,
            "lane_id":  KAITEN_LANE_ID,
            "column_id": column_id,
            "title": title,
        }
        if description is not None:
            body["description"] = description
        if size is not None:
            body["size"] = size
        if due_date is not None:
            body["due_date"] = due_date
        if sort_order is not None:
            body["sort_order"] = sort_order
        if tag_ids is not None:
            body["tag_ids"] = tag_ids
        if properties is not None:
            body["properties"] = properties

        data = await self._request("POST", "/cards", json=body)
        if not data:
            logger.error("create_card: не удалось создать карточку «{}»", title)
            return None
        card = _parse_card(data)
        logger.info("create_card: создана карточка id={} «{}»", card.id, card.title)
        return card

    async def move_card(self, card_id: int, column_id: int, sort_order: float) -> Card | None:
        """PATCH /cards/{card_id} — перемещает карточку в другую колонку."""
        data = await self._request(
            "PATCH",
            f"/cards/{card_id}",
            json={"column_id": column_id, "sort_order": sort_order},
        )
        if not data:
            logger.error("move_card: не удалось переместить карточку id={}", card_id)
            return None
        card = _parse_card(data)
        logger.info(
            "move_card: карточка id={} перемещена в column_id={} sort_order={}",
            card_id, column_id, sort_order,
        )
        return card

    async def update_card(self, card_id: int, **fields: Any) -> Card | None:
        """PATCH /cards/{card_id} — обновляет произвольные поля карточки."""
        if not fields:
            logger.warning("update_card({}): нет полей для обновления", card_id)
            return None
        data = await self._request("PATCH", f"/cards/{card_id}", json=fields)
        if not data:
            logger.error("update_card: не удалось обновить карточку id={}", card_id)
            return None
        logger.info("update_card: обновлена карточка id={} поля={}", card_id, list(fields))
        return _parse_card(data)

    async def archive_card(self, card_id: int, comment: str | None = None) -> bool:
        """Архивирует карточку: сначала добавляет комментарий (если передан),
        затем перемещает в колонку Архив (id=6122269).

        Возвращает True при успехе.
        """
        if comment:
            await self.add_comment(card_id, comment)

        data = await self._request(
            "PATCH",
            f"/cards/{card_id}",
            json={"column_id": ARCHIVE_COLUMN_ID},
        )
        if not data:
            logger.error("archive_card: не удалось архивировать карточку id={}", card_id)
            return False
        logger.info("archive_card: карточка id={} перемещена в Архив", card_id)
        return True

    async def add_comment(self, card_id: int, text: str) -> bool:
        """POST /cards/{card_id}/comments — добавляет комментарий к карточке.

        Возвращает True при успехе.
        """
        data = await self._request(
            "POST",
            f"/cards/{card_id}/comments",
            json={"text": text},
        )
        if data is None:
            logger.error("add_comment: не удалось добавить комментарий к карточке id={}", card_id)
            return False
        logger.info("add_comment: комментарий добавлен к карточке id={}", card_id)
        return True

    async def get_comments(self, card_id: int) -> list[str]:
        """GET /cards/{card_id}/comments — список текстов комментариев."""
        data = await self._request("GET", f"/cards/{card_id}/comments")
        if not isinstance(data, list):
            return []
        return [c.get("text", "") for c in data if c.get("text")]
