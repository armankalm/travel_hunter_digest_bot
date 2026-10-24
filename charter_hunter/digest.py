"""Сборка дайджеста: группировка по направлению, сортировка по цене, затем по дате."""

import html
import re
import sqlite3
from collections import defaultdict
from datetime import date

CURRENCY_SIGN = {"KZT": "₸", "USD": "$", "EUR": "€", "KGS": "сом", "RUB": "₽", "UZS": "сум"}


def select_flights(rows: list[sqlite3.Row], include_seen: bool) -> list[dict]:
    """Один рейс = одна строка с самой низкой ценой среди каналов.
    Без include_seen остаются только рейсы, которых не было в дайджестах, или подешевевшие."""
    best: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        key = (r["origin"], r["destination"], r["depart_date"], r["return_date"])
        cur = best.get(key)
        if cur is None or (r["price_kzt"], cur["posted_at"]) < (cur["price_kzt"], r["posted_at"]):
            best[key] = r

    flights = []
    for r in best.values():
        seen_price = r["digested_price"]
        if include_seen or seen_price is None or r["price_kzt"] < seen_price - 1:
            flights.append(dict(r))
    return flights


def fmt_money(value: float, currency: str) -> str:
    return f"{value:,.0f}".replace(",", " ") + " " + CURRENCY_SIGN.get(currency, currency)


def fmt_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m")


def fmt_offer(o: dict) -> str:
    price = fmt_money(o["price_kzt"], "KZT")
    if o["currency"] != "KZT":
        price = f"≈{price} ({fmt_money(o['price'], o['currency'])})"

    dates = fmt_date(o["depart_date"])
    if o["return_date"]:
        dates += f"–{fmt_date(o['return_date'])}"

    parts = [f"<b>{price}</b>", dates]
    if o["digested_price"] and o["price_kzt"] < o["digested_price"]:
        parts.append(f"📉 было {fmt_money(o['digested_price'], 'KZT')}")
    if o["airline"]:
        parts.append(html.escape(o["airline"]))
    if o["seats"]:
        parts.append(f"мест: {o['seats']}")
    if o["kind"] == "tour":
        parts.append("тур")
    if o["link"]:
        parts.append(f'<a href="{html.escape(o["link"])}">пост</a>')
    return "• " + " · ".join(parts)


def build(flights: list[dict], today: date, title: str, max_per_direction: int) -> list[str]:
    """Блоки текста (HTML): заголовок + по блоку на направление."""
    groups: dict[tuple[str, str, bool], list[dict]] = defaultdict(list)
    for f in flights:
        groups[(f["origin"], f["destination"], bool(f["return_date"]))].append(f)

    for items in groups.values():
        items.sort(key=lambda f: (f["price_kzt"], f["depart_date"]))
    # Направления: сначала те, где самый дешёвый билет дешевле
    ordered = sorted(groups.items(), key=lambda kv: (kv[1][0]["price_kzt"], kv[1][0]["depart_date"]))

    header = f"✈️ <b>{title} {today.strftime('%d.%m.%Y')}</b> — рейсов: {len(flights)}, направлений: {len(groups)}"
    blocks = [header]
    for (origin, destination, round_trip), items in ordered:
        arrow = "⇄" if round_trip else "→"
        lines = [f"<b>{html.escape(origin)} {arrow} {html.escape(destination)}</b>"]
        shown = items[:max_per_direction] if max_per_direction else items
        lines += [fmt_offer(f) for f in shown]
        if len(items) > len(shown):
            rest = items[len(shown):]
            lines.append(f"… и ещё {len(rest)} рейсов от {fmt_money(rest[0]['price_kzt'], 'KZT')}")
        blocks.append("\n".join(lines))
    return blocks


def to_plain(text: str) -> str:
    """HTML для Telegram → текст для консоли: ссылка «пост» превращается в URL."""
    text = re.sub(r'<a href="([^"]+)">[^<]*</a>', lambda m: html.unescape(m.group(1)), text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))
