"""Telegram-бот: подписка по /start, отписка по /stop, рассылка дайджеста всем подписчикам (Bot API)."""

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request

from . import db
from .telegram import split_for_telegram

log = logging.getLogger(__name__)

WELCOME = (
    "✈️ Вы подписаны на дайджест чартерных билетов.\n"
    "Новые билеты будут приходить сюда после каждого обновления. Отписаться: /stop"
)
GOODBYE = "Вы отписаны. Подписаться снова: /start"
SEND_INTERVAL = 0.05  # Telegram разрешает боту ~30 сообщений в секунду


class BotError(Exception):
    def __init__(self, code: int, description: str, retry_after: int = 0):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.retry_after = retry_after


def get_token() -> str:
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        raise SystemExit("Заполните TG_BOT_TOKEN в .env: создайте бота у @BotFather (см. .env.example)")
    return token


def api(token: str, method: str, timeout: int = 0, **params) -> dict | list:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        body = json.load(e)
    if not body.get("ok"):
        retry_after = (body.get("parameters") or {}).get("retry_after", 0)
        raise BotError(body.get("error_code", 0), body.get("description", ""), retry_after)
    return body["result"]


def send(token: str, chat_id: int, text: str) -> None:
    """Отправляет сообщение, при превышении лимита ждёт и повторяет."""
    while True:
        try:
            api(token, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                link_preview_options={"is_disabled": True})
            return
        except BotError as e:
            if e.code != 429:
                raise
            time.sleep(e.retry_after or 1)


def reply(token: str, chat_id: int, text: str) -> None:
    """Ответ на команду: ошибка отправки не должна мешать обработке остальных обновлений."""
    try:
        send(token, chat_id, text)
    except BotError as e:
        log.warning("Не удалось ответить %s: %s", chat_id, e)


def display_name(chat: dict) -> str:
    if chat.get("username"):
        return "@" + chat["username"]
    return " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("title", "")


def sync_subscribers(conn: sqlite3.Connection, token: str, timeout: int = 0) -> None:
    """Забирает накопившиеся у бота сообщения: /start подписывает, /stop и блокировка бота — отписывают.
    Telegram хранит необработанные обновления 24 часа, поэтому для надёжности держите запущенным `hunter bot`
    или запускайте дайджест хотя бы раз в сутки."""
    offset = db.bot_offset(conn)
    updates = api(token, "getUpdates", timeout=timeout, offset=offset,
                  allowed_updates=["message", "my_chat_member"])
    for u in updates:
        if msg := u.get("message"):
            chat, text = msg["chat"], (msg.get("text") or "").strip()
            if text.startswith("/start"):
                db.add_subscriber(conn, chat["id"], display_name(chat))
                log.info("Новый подписчик: %s", display_name(chat))
                reply(token, chat["id"], WELCOME)
            elif text.startswith("/stop"):
                db.remove_subscriber(conn, chat["id"])
                log.info("Отписался: %s", display_name(chat))
                reply(token, chat["id"], GOODBYE)
        elif member := u.get("my_chat_member"):
            if member["new_chat_member"]["status"] in ("kicked", "left"):  # пользователь заблокировал бота
                db.remove_subscriber(conn, member["chat"]["id"])
                log.info("Бот заблокирован: %s", display_name(member["chat"]))
        db.set_bot_offset(conn, u["update_id"] + 1)
        conn.commit()


def broadcast(conn: sqlite3.Connection, token: str, blocks: list[str]) -> int:
    """Рассылает дайджест всем подписчикам. Возвращает число получателей."""
    chunks = split_for_telegram(blocks)
    delivered = 0
    for sub in db.subscribers(conn):
        try:
            for chunk in chunks:
                send(token, sub["chat_id"], chunk)
                time.sleep(SEND_INTERVAL)
            delivered += 1
        except BotError as e:
            if e.code == 403:  # бот заблокирован или пользователь удалён
                db.remove_subscriber(conn, sub["chat_id"])
                conn.commit()
                log.info("Удалён подписчик %s: %s", sub["name"], e)
            else:
                log.error("Не удалось отправить %s: %s", sub["name"], e)
    return delivered


def listen(conn: sqlite3.Connection, token: str) -> None:
    """Постоянно слушает бота, чтобы сразу отвечать на /start и /stop."""
    me = api(token, "getMe")
    log.info("Бот @%s слушает подписки (Ctrl+C — выход)", me["username"])
    while True:
        try:
            sync_subscribers(conn, token, timeout=50)
        except (BotError, OSError) as e:
            log.error("Ошибка опроса бота: %s", e)
            time.sleep(5)
