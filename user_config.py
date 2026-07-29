from __future__ import annotations
import json
import os
from dataclasses import dataclass, field

from loguru import logger

REQUIRED_COLUMN_NAMES = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье",
    "Следующая неделя", "Далекие времена", "Долгий ящик", "Архив",
]

@dataclass
class UserConfig:
    user_id: str                          # уникальный slug, например "owner" или "alice"
    telegram_chat_id: int
    kaiten_board_id: int
    kaiten_lane_id: int                   # 0 = определить автоматически при setup
    kaiten_space_id: int
    timezone: str = "Europe/Moscow"
    column_ids: dict[str, int] = field(default_factory=dict)  # заполняется board_setup или из конфига
    kaiten_token: str | None = None       # уже разрешённый токен (заполняется при загрузке из env)
    kaiten_base_url: str | None = None    # уже разрешённый base_url (заполняется при загрузке)
    # Параметризованные маппинги для мульти-аккаунта (опциональны — если None, используются дефолты)
    tag_ids: dict[str, int] | None = None
    importance_options: dict[str, int] | None = None
    weekday_options: dict[str, int] | None = None
    field_ids: dict[str, str] | None = None   # keys: "event", "importance", "weekday"


def load_users() -> list[UserConfig]:
    """Загружает список пользователей из users.json, USERS_JSON env или одиночного env (обратная совместимость).

    После загрузки из статических источников дополняет список онбординг-пользователями
    из SQLite (дедуп по user_id — записи из json/env имеют приоритет).
    """
    path = os.getenv("USERS_CONFIG_PATH", "users.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        users = [_parse_user(item) for item in data]
    elif os.getenv("USERS_JSON"):
        data = json.loads(os.getenv("USERS_JSON"))  # type: ignore[arg-type]
        users = [_parse_user(item) for item in data]
    else:
        users = _load_from_env()
    users = _merge_db_users(users)
    return users


def _parse_user(item: dict) -> UserConfig:
    column_ids = {k: int(v) for k, v in item.get("column_ids", {}).items()}
    token_env = item.get("kaiten_token_env")       # например "KAITEN_TOKEN_ALICE"
    base_url_env = item.get("kaiten_base_url_env") # например "KAITEN_BASE_URL_ALICE"
    kaiten_token = os.getenv(token_env) if token_env else None
    kaiten_base_url = os.getenv(base_url_env) if base_url_env else None
    # Параметризованные маппинги для мульти-аккаунта
    tag_ids = (
        {k: int(v) for k, v in item["tag_ids"].items()} if item.get("tag_ids") else None
    )
    importance_options = (
        {k: int(v) for k, v in item["importance_options"].items()}
        if item.get("importance_options") else None
    )
    weekday_options = (
        {k: int(v) for k, v in item["weekday_options"].items()}
        if item.get("weekday_options") else None
    )
    field_ids = dict(item["field_ids"]) if item.get("field_ids") else None
    return UserConfig(
        user_id=item["user_id"],
        telegram_chat_id=int(item["telegram_chat_id"]),
        kaiten_board_id=int(item["kaiten_board_id"]),
        kaiten_lane_id=int(item.get("kaiten_lane_id", 0)),
        kaiten_space_id=int(item.get("kaiten_space_id", os.getenv("KAITEN_SPACE_ID", "197396"))),
        timezone=item.get("timezone", "Europe/Moscow"),
        column_ids=column_ids,
        kaiten_token=kaiten_token,
        kaiten_base_url=kaiten_base_url,
        tag_ids=tag_ids,
        importance_options=importance_options,
        weekday_options=weekday_options,
        field_ids=field_ids,
    )


def _load_from_env() -> list[UserConfig]:
    """Backward-compat: один пользователь из переменных окружения."""
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    board_id = os.getenv("KAITEN_BOARD_ID")
    lane_id = os.getenv("KAITEN_LANE_ID", "0")
    space_id = os.getenv("KAITEN_SPACE_ID", "197396")
    if not chat_id or not board_id:
        raise RuntimeError(
            "users.json не найден, и TELEGRAM_CHAT_ID / KAITEN_BOARD_ID не заданы в env."
        )
    return [UserConfig(
        user_id="default",
        telegram_chat_id=int(chat_id),
        kaiten_board_id=int(board_id),
        kaiten_lane_id=int(lane_id),
        kaiten_space_id=int(space_id),
        kaiten_token=None,
        kaiten_base_url=None,
    )]


def _merge_db_users(users: list[UserConfig]) -> list[UserConfig]:
    """Догружает онбординг-пользователей из БД (db.load_user_records()).

    Токен резолвится из os.getenv(record['kaiten_token_env']); если переменной нет —
    пользователь пропускается с warning (владелец ещё не добавил ключ в Railway env).
    Дедуп по user_id: записи из json/env имеют приоритет.
    """
    import db  # локальный импорт чтобы не тянуть db на уровень модуля
    seen = {u.user_id for u in users}
    try:
        records = db.load_user_records()
    except Exception as exc:
        logger.error("load_users: не удалось загрузить пользователей из БД — {}", exc)
        return users
    for rec in records:
        if rec["user_id"] in seen:
            continue
        token = os.getenv(rec["kaiten_token_env"], "")
        if not token:
            logger.warning(
                "load_users: пользователь user={} пропущен — env-переменная {} не задана",
                rec["user_id"], rec["kaiten_token_env"],
            )
            continue
        users.append(UserConfig(
            user_id=rec["user_id"],
            telegram_chat_id=int(rec["telegram_chat_id"]),
            kaiten_board_id=int(rec["kaiten_board_id"]),
            kaiten_lane_id=int(rec.get("kaiten_lane_id", 0)),
            kaiten_space_id=int(rec["kaiten_space_id"]),
            timezone=rec.get("timezone", "Europe/Moscow"),
            column_ids={k: int(v) for k, v in (rec.get("column_ids") or {}).items()},
            kaiten_token=token,
            kaiten_base_url=rec.get("kaiten_base_url"),
        ))
        seen.add(rec["user_id"])
        logger.info("load_users: подключён онбординг-пользователь user={}", rec["user_id"])
    return users
