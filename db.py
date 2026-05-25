"""
db.py — модуль состояния на SQLite.

Хранит флаги выполнения утренней/вечерней логики по датам.
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
    """Создаёт таблицу daily_flags если её ещё нет."""
    db_file = Path(DB_PATH)
    if db_file.parent != Path(".") and not db_file.parent.exists():
        db_file.parent.mkdir(parents=True, exist_ok=True)

    with _get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_flags (
                date          TEXT    PRIMARY KEY,
                morning_done  INTEGER NOT NULL DEFAULT 0,
                evening_done  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
    logger.debug("db: таблица daily_flags готова (DB_PATH={})", DB_PATH)


# Инициализируем при импорте модуля
_init_db()


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _date_key(d: date | str) -> str:
    """Приводит дату к строке YYYY-MM-DD."""
    if isinstance(d, str):
        return d
    return d.isoformat()


def _ensure_row(conn: sqlite3.Connection, date_key: str) -> None:
    """Создаёт строку для даты если её ещё нет (INSERT OR IGNORE)."""
    conn.execute(
        "INSERT OR IGNORE INTO daily_flags (date) VALUES (?)",
        (date_key,),
    )


# ── Публичный API ─────────────────────────────────────────────────────────────

def is_morning_done(d: date | str) -> bool:
    """Возвращает True если утренняя логика для этой даты уже выполнена."""
    key = _date_key(d)
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT morning_done FROM daily_flags WHERE date = ?",
            (key,),
        ).fetchone()
    result = bool(row["morning_done"]) if row else False
    logger.debug("is_morning_done({}): {}", key, result)
    return result


def set_morning_done(d: date | str) -> None:
    """Отмечает утреннюю логику для этой даты как выполненную."""
    key = _date_key(d)
    with _get_connection() as conn:
        _ensure_row(conn, key)
        conn.execute(
            "UPDATE daily_flags SET morning_done = 1 WHERE date = ?",
            (key,),
        )
        conn.commit()
    logger.info("set_morning_done({}): утро отмечено", key)


def is_evening_done(d: date | str) -> bool:
    """Возвращает True если вечерняя логика для этой даты уже выполнена."""
    key = _date_key(d)
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT evening_done FROM daily_flags WHERE date = ?",
            (key,),
        ).fetchone()
    result = bool(row["evening_done"]) if row else False
    logger.debug("is_evening_done({}): {}", key, result)
    return result


def set_evening_done(d: date | str) -> None:
    """Отмечает вечернюю логику для этой даты как выполненную."""
    key = _date_key(d)
    with _get_connection() as conn:
        _ensure_row(conn, key)
        conn.execute(
            "UPDATE daily_flags SET evening_done = 1 WHERE date = ?",
            (key,),
        )
        conn.commit()
    logger.info("set_evening_done({}): вечер отмечен", key)


def get_flags(d: date | str) -> dict[str, bool]:
    """Возвращает оба флага для даты одним вызовом.

    Пример: {"morning_done": True, "evening_done": False}
    Удобно для логирования и дебага.
    """
    key = _date_key(d)
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT morning_done, evening_done FROM daily_flags WHERE date = ?",
            (key,),
        ).fetchone()
    if row is None:
        return {"morning_done": False, "evening_done": False}
    return {
        "morning_done": bool(row["morning_done"]),
        "evening_done": bool(row["evening_done"]),
    }


def reset_flags(d: date | str) -> None:
    """Сбрасывает оба флага для даты.

    Используется в тестах и при ручном перезапуске логики.
    """
    key = _date_key(d)
    with _get_connection() as conn:
        _ensure_row(conn, key)
        conn.execute(
            "UPDATE daily_flags SET morning_done = 0, evening_done = 0 WHERE date = ?",
            (key,),
        )
        conn.commit()
    logger.warning("reset_flags({}): флаги сброшены", key)
