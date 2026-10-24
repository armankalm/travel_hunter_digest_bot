"""Курсы валют к тенге, чтобы сортировать цены в разных валютах вместе."""

import json
import logging
import sqlite3
import urllib.request

from .db import now_iso

log = logging.getLogger(__name__)

RATES_URL = "https://open.er-api.com/v6/latest/KZT"


def load_rates(conn: sqlite3.Connection, fallback: dict[str, float]) -> dict[str, float]:
    """Тенге за единицу валюты. Порядок: свежие из сети → последние сохранённые → из конфига."""
    rates = {k.upper(): float(v) for k, v in fallback.items()}
    rates.update({r["currency"]: r["kzt_per_unit"] for r in conn.execute("SELECT * FROM rates")})

    try:
        with urllib.request.urlopen(RATES_URL, timeout=10) as resp:
            data = json.load(resp)
        fresh = {cur: 1 / per_kzt for cur, per_kzt in data["rates"].items() if per_kzt}
        ts = now_iso()
        conn.executemany(
            "INSERT OR REPLACE INTO rates (currency, kzt_per_unit, updated_at) VALUES (?, ?, ?)",
            [(cur, val, ts) for cur, val in fresh.items() if cur in rates or cur in ("USD", "EUR", "KGS", "RUB", "UZS")],
        )
        rates.update(fresh)
    except Exception as e:  # сеть недоступна — работаем на сохранённых курсах
        log.warning("Не удалось обновить курсы валют: %s", e)

    rates["KZT"] = 1.0
    return rates
