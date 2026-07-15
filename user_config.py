from __future__ import annotations
import json
import os
from dataclasses import dataclass, field

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
    """Загружает список пользователей из users.json, USERS_JSON env или одиночного env (обратная совместимость)."""
    path = os.getenv("USERS_CONFIG_PATH", "users.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [_parse_user(item) for item in data]
    users_json_env = os.getenv("USERS_JSON")
    if users_json_env:
        data = json.loads(users_json_env)
        return [_parse_user(item) for item in data]
    return _load_from_env()


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
