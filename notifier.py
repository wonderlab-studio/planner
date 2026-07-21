"""
notifier.py — модуль отправки уведомлений в Telegram без входящего апдейта.

Использует httpx напрямую (POST на api.telegram.org).
Конфиг из .env: TELEGRAM_TOKEN.
chat_id передаётся явно при создании экземпляра — поддержка мульти-пользователей.

Предназначен для вызова из scheduler.py и других модулей,
где нет входящего Update от пользователя.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv
from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kaiten_client import Card, TZ_MSK

load_dotenv()

# ── Конфиг ────────────────────────────────────────────────────────────────────

_TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")

_TELEGRAM_API_BASE = "https://api.telegram.org"
_SEND_MESSAGE_TIMEOUT = 15  # секунд

# Максимум символов в одном сообщении Telegram
_MAX_MESSAGE_LEN = 4096


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _split_text(text: str, max_len: int = _MAX_MESSAGE_LEN) -> list[str]:
    """Разбивает длинный текст на части не длиннее max_len.

    Старается разбить по переносам строк, а не по середине слова.
    """
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Ищем последний перенос строки в пределах лимита
        cut = text.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")

    return parts


def _is_control_section(sorted_cards: list[Card], target: Card) -> bool:
    """Определяет, находится ли target в секции «На контроле» — по позиции
    относительно разделителей (blocked=True) в списке, отсортированном по sort_order."""
    current: str | None = None
    for card in sorted_cards:
        if card.blocked:
            current = card.block_reason
        elif card.id == target.id:
            return current == "На контроле"
    return False


# ── Notifier ──────────────────────────────────────────────────────────────────

class Notifier:
    """Отправляет сообщения в Telegram-чат через Bot API без Update."""

    def __init__(self, chat_id: int) -> None:
        if not _TELEGRAM_TOKEN:
            logger.warning("Notifier: TELEGRAM_TOKEN не задан")

        self._chat_id = chat_id
        self._token = _TELEGRAM_TOKEN
        self._api_url = f"{_TELEGRAM_API_BASE}/bot{self._token}/sendMessage"

    # ── Внутренние хелперы ────────────────────────────────────────────────────

    async def _post(
        self,
        text: str,
        parse_mode: str | None = "Markdown",
        disable_notification: bool = False,
    ) -> int | None:
        """Отправляет одно сообщение. Возвращает message_id при успехе, None при ошибке.

        При ошибке парсинга Markdown автоматически повторяет без форматирования.
        """
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text":    text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if disable_notification:
            payload["disable_notification"] = True

        try:
            async with httpx.AsyncClient(timeout=_SEND_MESSAGE_TIMEOUT) as client:
                resp = await client.post(self._api_url, json=payload)

                if resp.status_code == 200:
                    return resp.json()["result"]["message_id"]

                # Telegram вернул ошибку — логируем детали
                body = resp.json() if resp.content else {}
                error_code  = body.get("error_code", resp.status_code)
                description = body.get("description", "")

                # Если ошибка в разметке — повторяем без parse_mode
                if parse_mode is not None and "can't parse" in description.lower():
                    logger.warning(
                        "Notifier._post: Markdown-ошибка ({}), повтор без форматирования",
                        description,
                    )
                    return await self._post(
                        text,
                        parse_mode=None,
                        disable_notification=disable_notification,
                    )

                logger.error(
                    "Notifier._post: Telegram API error {} — {}",
                    error_code, description,
                )
                return None

        except httpx.TimeoutException:
            logger.error("Notifier._post: таймаут при отправке сообщения")
            return None
        except Exception as exc:
            logger.exception("Notifier._post: неожиданная ошибка — {}", exc)
            return None

    async def _pin(self, message_id: int) -> None:
        """Закрепляет сообщение в чате (без уведомления)."""
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/pinChatMessage"
        payload = {
            "chat_id": self._chat_id,
            "message_id": message_id,
            "disable_notification": True,
        }
        try:
            async with httpx.AsyncClient(timeout=_SEND_MESSAGE_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.warning(
                        "Notifier._pin: HTTP {} — {}", resp.status_code, resp.text[:200]
                    )
        except Exception as exc:
            logger.warning("Notifier._pin: ошибка — {}", exc)

    async def _unpin_all(self) -> None:
        """Откепляет все закреплённые сообщения в чате."""
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/unpinAllChatMessages"
        payload = {"chat_id": self._chat_id}
        try:
            async with httpx.AsyncClient(timeout=_SEND_MESSAGE_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.warning(
                        "Notifier._unpin_all: HTTP {} — {}",
                        resp.status_code, resp.text[:200],
                    )
        except Exception as exc:
            logger.warning("Notifier._unpin_all: ошибка — {}", exc)

    # ── Публичные методы ──────────────────────────────────────────────────────

    async def send(self, text: str) -> None:
        """Отправляет произвольный текст. Длинные сообщения разбивает на части."""
        if not text:
            logger.warning("Notifier.send: передан пустой текст, пропускаем")
            return

        parts = _split_text(text)
        for i, part in enumerate(parts, start=1):
            message_id = await self._post(part)
            if message_id:
                logger.info(
                    "Notifier.send: часть {}/{} отправлена ({} символов)",
                    i, len(parts), len(part),
                )
            else:
                logger.error(
                    "Notifier.send: не удалось отправить часть {}/{}", i, len(parts)
                )

    async def send_and_pin(self, text: str, silent: bool = False) -> None:
        """Отправляет текст (с разбивкой если >4096 символов), затем откепляет все
        ранее закреплённые сообщения и закрепляет последнее отправленное.

        Ошибки pin/unpin не критичны — не бросает исключения.
        silent=True — отправить без звука (для утреннего авто-плана).
        """
        if not text:
            logger.warning("Notifier.send_and_pin: передан пустой текст, пропускаем")
            return
        parts = _split_text(text)
        last_message_id: int | None = None
        for i, part in enumerate(parts, start=1):
            message_id = await self._post(part, disable_notification=silent)
            if message_id is not None:
                last_message_id = message_id
                logger.info(
                    "Notifier.send_and_pin: часть {}/{} отправлена (id={})",
                    i, len(parts), message_id,
                )
            else:
                logger.error(
                    "Notifier.send_and_pin: не удалось отправить часть {}/{}", i, len(parts)
                )
        if last_message_id is not None:
            await self._unpin_all()
            await self._pin(last_message_id)

    async def send_card_buttons(self, cards: list, silent: bool = False) -> None:
        """Отправляет сообщение с InlineKeyboard из карточек (первая страница, до 20 кнопок).

        Карточки сортируются: «На контроле» — всегда в конец, затем по event_time.
        cards — list[Card] из kaiten_client.
        silent=True — отправить без звука (для утреннего авто-плана).
        Кнопки с event_time на сегодня отображают префикс времени «ЧЧ:ММ».
        """
        # Сортируем ПОЛНЫЙ список по sort_order — нужно для определения секции «На контроле»
        full_sorted = sorted(cards, key=lambda c: c.sort_order)
        task_cards = [c for c in cards if not c.blocked and not c.archived]
        if not task_cards:
            return
        task_cards.sort(
            key=lambda c: (
                _is_control_section(full_sorted, c),
                c.event_time is None,
                c.event_time or datetime.min.replace(tzinfo=TZ_MSK),
            )
        )
        max_buttons = 20
        btn_title_len = 40
        shown = task_cards[:max_buttons]
        today_msk = datetime.now(TZ_MSK).date()

        def _button_text(c: Card) -> str:
            prefix = ""
            if c.event_time and c.event_time.date() == today_msk:
                prefix = f"{c.event_time:%H:%M} "
            available = btn_title_len - len(prefix)
            title = c.title[:available] + ("…" if len(c.title) > available else "")
            return prefix + title

        keyboard = [
            [InlineKeyboardButton(
                text=_button_text(c),
                callback_data=f"card:{c.id}",
            )]
            for c in shown
        ]
        markup = InlineKeyboardMarkup(keyboard)
        suffix = (
            f" (первые {max_buttons} из {len(task_cards)}, напиши «утро» для остальных)"
            if len(task_cards) > max_buttons else ""
        )
        text = f"📋 *Карточки на сегодня*{suffix}:"
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": markup.to_dict(),
            "disable_notification": silent,
        }
        try:
            async with httpx.AsyncClient(timeout=_SEND_MESSAGE_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error(
                        "Notifier.send_card_buttons: HTTP {} — {}",
                        resp.status_code, resp.text[:200],
                    )
        except Exception as exc:
            logger.exception("Notifier.send_card_buttons: ошибка — {}", exc)

    async def send_morning_plan(self, plan_text: str) -> None:
        """Отправляет утренний план дня.

        Добавляет заголовок если его нет (план от Claude уже содержит форматирование).
        """
        logger.info("Notifier.send_morning_plan: отправка плана ({} символов)", len(plan_text))
        await self.send(plan_text)

    async def send_evening_summary(self, summary_text: str) -> None:
        """Отправляет вечерний итог дня."""
        logger.info(
            "Notifier.send_evening_summary: отправка итога ({} символов)", len(summary_text)
        )
        await self.send(summary_text)

    async def send_reminder(
        self,
        card_title: str,
        minutes_left: int,
        important: bool,
    ) -> None:
        """Отправляет напоминание о предстоящей задаче.

        Параметры:
            card_title   — название карточки
            minutes_left — сколько минут до события (15 или 30)
            important    — True если важность «важное» или «критическое»

        Формат сообщения:
            ⚠️ ВАЖНО: через 30 мин → «Звонок с заказчиком»
            🔔 Напоминание: через 15 мин → «Купить продукты»
        """
        if important:
            prefix = f"⚠️ *ВАЖНО*: через {minutes_left} мин"
        else:
            prefix = f"🔔 Напоминание: через {minutes_left} мин"

        text = f"{prefix} → «{card_title}»"

        logger.info(
            "Notifier.send_reminder: «{}» через {} мин (important={})",
            card_title, minutes_left, important,
        )
        await self.send(text)
