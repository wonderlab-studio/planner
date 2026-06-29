from __future__ import annotations
import json
import os
from dataclasses import dataclass, field

REQUIRED_COLUMN_NAMES = [
    "Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс",
    "Следующая неделя", "Далёкое будущее", "Долгий ящик", "Архив",
]

@dataclass
class UserConfig:
    user_id: str                          # уникальный slug, например "owner" или "alice"
    telegram_chat_id: int
    kaiten_board_id: int
    kaiten_lane_id: int                   # 0 = определить автоматически при setup
    kaiten_space_id: int
    timezone: str = "Europe/Moscow"
    column_ids: dict[str, int] = field(default_factory=dict)  # заполняется board_setup


def load_users() -> list[UserConfig]:
    """Загружает список пользователей из users.json или из env (обратная совместимость)."""
    path = os.getenv("USERS_CONFIG_PATH", "users.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [_parse_user(item) for item in data]
    return _load_from_env()


def _parse_user(item: dict) -> UserConfig:
    return UserConfig(
        user_id=item["user_id"],
        telegram_chat_id=int(item["telegram_chat_id"]),
        kaiten_board_id=int(item["kaiten_board_id"]),
        kaiten_lane_id=int(item.get("kaiten_lane_id", 0)),
        kaiten_space_id=int(item.get("kaiten_space_id", os.getenv("KAITEN_SPACE_ID", "197396"))),
        timezone=item.get("timezone", "Europe/Moscow"),
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
    )]
