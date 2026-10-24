"""Чтение каналов/групп и отправка дайджеста в «Избранное» через личный аккаунт (Telethon)."""

import asyncio
import getpass
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel

from . import db

log = logging.getLogger(__name__)

TG_MESSAGE_LIMIT = 4000  # у Telegram лимит 4096 символов


def make_client(session_path: Path) -> TelegramClient:
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("Заполните TG_API_ID и TG_API_HASH в .env (см. .env.example)")
    session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(session_path), int(api_id), api_hash)


async def qr_login(client: TelegramClient) -> None:
    """Вход сканированием QR в приложении: Настройки → Устройства → Подключить устройство."""
    await client.connect()
    if await client.is_user_authorized():
        return
    qr = await client.qr_login()
    while True:
        code = qrcode.QRCode(border=1)
        code.add_data(qr.url)
        code.print_ascii(invert=True)
        print("Telegram на телефоне → Настройки → Устройства → Подключить устройство → отсканируйте код выше")
        try:
            await qr.wait(timeout=60)
            return
        except asyncio.TimeoutError:
            print("QR-код устарел, показываю новый…")
            await qr.recreate()
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Облачный пароль (двухэтапная проверка): "))
            return


def normalize_source(source: str) -> str:
    """«https://t.me/ticketal», «@ticketal», «ticketal» → «ticketal»."""
    return re.sub(r"^(https?://)?(t\.me/|telegram\.me/)?@?", "", source.strip()).strip("/")


def message_link(entity, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if isinstance(entity, Channel):  # приватный канал или супергруппа
        return f"https://t.me/c/{entity.id}/{message_id}"
    return ""


async def collect(client: TelegramClient, conn: sqlite3.Connection, sources: list[str], first_run_days: int) -> int:
    """Скачивает сообщения новее последнего прочитанного. Возвращает число новых сообщений."""
    total = 0
    since = datetime.now(timezone.utc) - timedelta(days=first_run_days)
    for source in sources:
        chat_key = normalize_source(source)
        try:
            entity = await client.get_entity(chat_key)
        except Exception as e:
            log.error("Не удалось открыть источник %s: %s", source, e)
            continue

        last_id = db.last_message_id(conn, chat_key)
        if last_id:
            messages = client.iter_messages(entity, min_id=last_id, reverse=True)
        else:
            messages = client.iter_messages(entity, offset_date=since, reverse=True)

        count = 0
        async for m in messages:
            if not m.message:
                continue
            db.save_message(conn, chat_key, m.id, m.date.isoformat(), m.message, message_link(entity, m.id))
            count += 1
        conn.commit()
        log.info("%s: новых сообщений %d", chat_key, count)
        total += count
    return total


def split_for_telegram(blocks: list[str]) -> list[str]:
    """Склеивает блоки в сообщения не длиннее лимита, не разрывая блок."""
    pieces = []
    for block in blocks:
        while len(block) > TG_MESSAGE_LIMIT:  # слишком длинное направление режем по строкам
            cut = block.rfind("\n", 0, TG_MESSAGE_LIMIT)
            pieces.append(block[:cut])
            block = block[cut + 1 :]
        pieces.append(block)

    chunks, current = [], ""
    for block in pieces:
        if current and len(current) + len(block) + 2 > TG_MESSAGE_LIMIT:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


async def send_to_saved(client: TelegramClient, blocks: list[str]) -> None:
    for chunk in split_for_telegram(blocks):
        await client.send_message("me", chunk, parse_mode="html", link_preview=False)
