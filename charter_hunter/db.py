"""SQLite-хранилище: прочитанные сообщения, извлечённые предложения, курсы валют, подписчики бота."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    chat_key        TEXT PRIMARY KEY,
    last_message_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    chat_key   TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    posted_at  TEXT NOT NULL,
    text       TEXT NOT NULL,
    link       TEXT NOT NULL,
    processed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_key, message_id)
);

-- Последняя цена рейса от каждого канала (новый пост канала заменяет старый)
CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY,
    chat_key    TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    link        TEXT NOT NULL,
    posted_at   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    price       REAL NOT NULL,
    currency    TEXT NOT NULL,
    price_kzt   REAL NOT NULL,
    airline     TEXT NOT NULL,
    seats       INTEGER NOT NULL,
    UNIQUE (chat_key, origin, destination, depart_date, return_date)
);

-- Рейсы, уже показанные в дайджесте, и цена, по которой показаны
CREATE TABLE IF NOT EXISTS flights (
    origin         TEXT NOT NULL,
    destination    TEXT NOT NULL,
    depart_date    TEXT NOT NULL,
    return_date    TEXT NOT NULL,
    digested_price REAL NOT NULL,
    digested_at    TEXT NOT NULL,
    PRIMARY KEY (origin, destination, depart_date, return_date)
);

CREATE TABLE IF NOT EXISTS rates (
    currency    TEXT PRIMARY KEY,
    kzt_per_unit REAL NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Кто нажал /start в боте
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    subscribed_at TEXT NOT NULL
);

-- Служебное состояние бота: номер следующего обновления getUpdates
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Бот работает в отдельном потоке (asyncio.to_thread), но не одновременно с основным — это безопасно
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def last_message_id(conn: sqlite3.Connection, chat_key: str) -> int:
    row = conn.execute("SELECT last_message_id FROM sources WHERE chat_key = ?", (chat_key,)).fetchone()
    return row["last_message_id"] if row else 0


def save_message(conn: sqlite3.Connection, chat_key: str, message_id: int, posted_at: str, text: str, link: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO messages (chat_key, message_id, posted_at, text, link) VALUES (?, ?, ?, ?, ?)",
        (chat_key, message_id, posted_at, text, link),
    )
    conn.execute(
        """INSERT INTO sources (chat_key, last_message_id) VALUES (?, ?)
           ON CONFLICT(chat_key) DO UPDATE SET last_message_id = MAX(last_message_id, excluded.last_message_id)""",
        (chat_key, message_id),
    )


def unprocessed_messages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM messages WHERE processed = 0 ORDER BY posted_at").fetchall()


def mark_processed(conn: sqlite3.Connection, chat_key: str, message_id: int) -> None:
    conn.execute("UPDATE messages SET processed = 1 WHERE chat_key = ? AND message_id = ?", (chat_key, message_id))


def save_offer(conn: sqlite3.Connection, msg: sqlite3.Row, offer: dict) -> None:
    """Сохраняет цену рейса от канала; более свежий пост того же канала заменяет старую цену."""
    conn.execute(
        """INSERT INTO offers
           (chat_key, message_id, link, posted_at, kind, origin, destination, depart_date, return_date,
            price, currency, price_kzt, airline, seats)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (chat_key, origin, destination, depart_date, return_date) DO UPDATE SET
               message_id = excluded.message_id, link = excluded.link, posted_at = excluded.posted_at,
               kind = excluded.kind, price = excluded.price, currency = excluded.currency,
               price_kzt = excluded.price_kzt, airline = excluded.airline, seats = excluded.seats
           WHERE excluded.posted_at >= offers.posted_at""",
        (
            msg["chat_key"],
            msg["message_id"],
            msg["link"],
            msg["posted_at"],
            offer["kind"],
            offer["origin"],
            offer["destination"],
            offer["depart_date"],
            offer["return_date"],
            offer["price"],
            offer["currency"],
            offer["price_kzt"],
            offer["airline"],
            offer["seats"],
        ),
    )


def offers_for_digest(conn: sqlite3.Connection, today: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT o.*, f.digested_price FROM offers o
           LEFT JOIN flights f USING (origin, destination, depart_date, return_date)
           WHERE o.depart_date >= ?""",
        (today,),
    ).fetchall()


def mark_digested(conn: sqlite3.Connection, flights: list[dict]) -> None:
    ts = now_iso()
    conn.executemany(
        "INSERT OR REPLACE INTO flights VALUES (?, ?, ?, ?, ?, ?)",
        [(f["origin"], f["destination"], f["depart_date"], f["return_date"], f["price_kzt"], ts) for f in flights],
    )


def add_subscriber(conn: sqlite3.Connection, chat_id: int, name: str) -> None:
    conn.execute(
        """INSERT INTO subscribers VALUES (?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET name = excluded.name""",
        (chat_id, name, now_iso()),
    )


def remove_subscriber(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))


def subscribers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM subscribers ORDER BY subscribed_at").fetchall()


def bot_offset(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM bot_state WHERE key = 'offset'").fetchone()
    return row["value"] if row else 0


def set_bot_offset(conn: sqlite3.Connection, offset: int) -> None:
    conn.execute("INSERT OR REPLACE INTO bot_state VALUES ('offset', ?)", (offset,))
