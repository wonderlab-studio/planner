"""
db.py — модуль состояния на SQLite.

Хранит флаги выполнения утренней/вечерней логики по датам и пользователям.
Синхронный — вызывать из asyncio через loop.run_in_executor(None, func, args).

Конфиг из .env:
    DB_PATH — путь к файлу базы (дефолт: state.db)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── Конфиг ────────────────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("DB_PATH", "state.db")

# ── Инициализация БД ──────────────────────────────────────────────────────────

def _get_connection() -> sqlite3.Connection:
    """Открывает соединение с БД и возвращает его.

    check_same_thread=False нужен если соединение переиспользуется
    из разных потоков (например, через run_in_executor).
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """Создаёт таблицу daily_flags если её ещё нет, и применяет миграции."""
    db_file = Path(DB_PATH)
    if db_file.parent != Path(".") and not db_file.parent.exists():
        db_file.parent.mkdir(parents=True, exist_ok=True)

    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_flags (
                date          TEXT    NOT NULL,
                user_id       TEXT    NOT NULL DEFAULT '',
                morning_done  INTEGER NOT NULL DEFAULT 0,
                evening_done  INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, user_id)
            )
        """)
        conn.commit()

        # Миграция: добавить user_id если колонки нет
        cur = conn.execute("PRAGMA table_info(daily_flags)")
        cols = {row[1] for row in cur.fetchall()}
        if "user_id" not in cols:
            conn.execute("ALTER TABLE daily_flags ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
            conn.commit()
            logger.info("db: migrated daily_flags — added user_id column")

    logger.debug("db: таблица daily_flags готова (DB_PATH={})", DB_PATH)


# Инициализируем при импорте модуля
_init_db()


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _date_key(d: date | str) -> str:
    """Приводит дату к строке YYYY-MM-DD."""
    if isinstance(d, str):
        return d
    return d.isoformat()


def _ensure_row(conn: sqlite3.Connection, date_key: str, user_id: str) -> None:
    """Создаёт строку для (date, user_id) если её ещё нет (INSERT OR IGNORE)."""
    conn.execute(
        "INSERT OR IGNORE INTO daily_flags (date, user_id) VALUES (?, ?)",
        (date_key, user_id),
    )


# ── Публичный API ─────────────────────────────────────────────────────────────

def is_morning_done(d: date | str, user_id: str = "default") -> bool:
    """Возвращает True если утренняя логика для этой даты и пользователя уже выполнена."""
    key = _date_key(d)
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT morning_done FROM daily_flags WHERE date = ? AND user_id = ?",
            (key, user_id),
        ).fetchone()
    result = bool(row["morning_done"]) if row else False
    logger.debug("is_morning_done({}, {}): {}", key, user_id, result)
    return result


def set_morning_done(d: date | str, user_id: str = "default") -> None:
    """Отмечает утреннюю логику для этой даты и пользователя как выполненную."""
    key = _date_key(d)
    with _get_connection() as conn:
        _ensure_row(conn, key, user_id)
        conn.execute(
            "UPDATE daily_flags SET morning_done = 1 WHERE date = ? AND user_id = ?",
            (key, user_id),
        )
        conn.commit()
    logger.info("set_morning_done({}, {}): утро отмечено", key, user_id)


def is_evening_done(d: date | str, user_id: str = "default") -> bool:
    """Возвращает True если вечерняя логика для этой даты и пользователя уже выполнена."""
    key = _date_key(d)
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT evening_done FROM daily_flags WHERE date = ? AND user_id = ?",
            (key, user_id),
        ).fetchone()
    result = bool(row["evening_done"]) if row else False
    logger.debug("is_evening_done({}, {}): {}", key, user_id, result)
    return result


def set_evening_done(d: date | str, user_id: str = "default") -> None:
    """Отмечает вечернюю логику для этой даты и пользователя как выполненную."""
    key = _date_key(d)
    with _get_connection() as conn:
        _ensure_row(conn, key, user_id)
        conn.execute(
            "UPDATE daily_flags SET evening_done = 1 WHERE date = ? AND user_id = ?",
            (key, user_id),
        )
        conn.commit()
    logger.info("set_evening_done({}, {}): вечер отмечен", key, user_id)


def get_flags(d: date | str, user_id: str = "default") -> dict[str, bool]:
    """Возвращает оба флага для даты и пользователя одним вызовом.

    Пример: {"morning_done": True, "evening_done": False}
    Удобно для логирования и дебага.
    """
    key = _date_key(d)
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT morning_done, evening_done FROM daily_flags WHERE date = ? AND user_id = ?",
            (key, user_id),
        ).fetchone()
    if row is None:
        return {"morning_done": False, "evening_done": False}
    return {
        "morning_done": bool(row["morning_done"]),
        "evening_done": bool(row["evening_done"]),
    }


def reset_flags(d: date | str, user_id: str = "default") -> None:
    """Сбрасывает оба флага для даты и пользователя.

    Используется в тестах и при ручном перезапуске логики.
    """
    key = _date_key(d)
    with _get_connection() as conn:
        _ensure_row(conn, key, user_id)
        conn.execute(
            "UPDATE daily_flags SET morning_done = 0, evening_done = 0 WHERE date = ? AND user_id = ?",
            (key, user_id),
        )
        conn.commit()
    logger.warning("reset_flags({}, {}): флаги сброшены", key, user_id)
