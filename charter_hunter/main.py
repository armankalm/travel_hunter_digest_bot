"""Точка входа: hunter login | hunter run [--dry-run] [--all] | hunter bot | hunter search ..."""

import argparse
import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import bot, db, digest, telegram
from .extract import MODEL, extract, looks_like_offer
from .rates import load_rates

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "hunter.db"
SESSION_PATH = ROOT / "data" / "telegram"
PARALLEL = 4  # одновременных вызовов claude -p

log = logging.getLogger("hunter")


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def valid_offer(o: dict) -> bool:
    try:
        date.fromisoformat(o["depart_date"])
        if o["return_date"]:
            date.fromisoformat(o["return_date"])
    except ValueError:
        return False
    return o["price"] > 0 and bool(o["origin"]) and bool(o["destination"])


def normalize(o: dict, aliases: dict[str, str]) -> dict:
    """Приводит синонимы к одному написанию: «Анталия» → «Анталья», «Air Asia» → «AirAsia»."""
    for field in ("origin", "destination", "airline"):
        o[field] = aliases.get(o[field].strip(), o[field].strip())
    return o


def extract_new(conn, rates: dict[str, float], aliases: dict[str, str]) -> None:
    """Разбирает все ещё не обработанные сообщения."""
    pending = db.unprocessed_messages(conn)
    # Посты без цен (визы, реклама) не отправляем в модель
    for msg in pending:
        if not looks_like_offer(msg["text"]):
            db.mark_processed(conn, msg["chat_key"], msg["message_id"])
    conn.commit()
    pending = [m for m in pending if looks_like_offer(m["text"])]
    if not pending:
        return
    log.info("Разбираю %d сообщений моделью %s (claude -p)", len(pending), MODEL)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        futures = {pool.submit(extract, m["text"], m["posted_at"]): m for m in pending}
        for i, fut in enumerate(as_completed(futures), 1):
            msg = futures[fut]
            try:
                offers = fut.result()
            except Exception as e:  # оставляем сообщение необработанным — повторим в следующий запуск
                log.error("Ошибка разбора %s: %s", msg["link"], e)
                continue
            for o in filter(valid_offer, offers):
                o["price_kzt"] = o["price"] * rates.get(o["currency"], 1.0)
                db.save_offer(conn, msg, normalize(o, aliases))
            db.mark_processed(conn, msg["chat_key"], msg["message_id"])
            conn.commit()
            log.info("[%d/%d] %s → %d предложений", i, len(pending), msg["link"], len(offers))


async def run(dry_run: bool, include_seen: bool) -> None:
    config = load_config()
    conn = db.connect(DB_PATH)
    token = None if dry_run else bot.get_token()
    client = telegram.make_client(SESSION_PATH)
    async with client:  # при первом запуске спросит телефон и код
        await telegram.collect(client, conn, config["sources"], config.get("first_run_days", 7))

        rates = load_rates(conn, config.get("fallback_rates_kzt", {}))
        conn.commit()
        extract_new(conn, rates, config.get("aliases") or {})

        today = date.today()
        flights = digest.select_flights(actual_offers(conn, config, today), include_seen)

        if flights:
            title = "Все актуальные билеты" if include_seen else "Новые билеты"
            blocks = digest.build(flights, today, title, config.get("max_per_direction", 0))
        else:
            blocks = [f"✈️ {today.strftime('%d.%m.%Y')}: новых билетов нет"]

        if dry_run:
            print(digest.to_plain("\n\n".join(blocks)))
        else:
            await asyncio.to_thread(bot.sync_subscribers, conn, token)
            delivered = await asyncio.to_thread(bot.broadcast, conn, token, blocks)
            if delivered:
                db.mark_digested(conn, flights)
                conn.commit()
                log.info("Дайджест разослан подписчикам бота (%d): %d рейсов", delivered, len(flights))
            else:
                log.warning("Дайджест никому не отправлен: у бота нет подписчиков — напишите ему /start")
    conn.close()


def actual_offers(conn, config: dict, today: date) -> list:
    """Предложения с вылетом не раньше сегодня из городов вылета в конфиге."""
    origins = {c.lower() for c in config["origins"]}
    return [
        o
        for o in db.offers_for_digest(conn, today.isoformat())
        if o["origin"].lower() in origins and (config.get("include_tours") or o["kind"] == "ticket")
    ]


def parse_date(value: str, today: date) -> date:
    """«05.11», «05.11.2026» или «2026-11-05». Без года — ближайшая такая дата не в прошлом."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        parts = [int(p) for p in value.split(".")]
        if len(parts) == 3:
            return date(parts[2], parts[1], parts[0])
        if len(parts) == 2:
            d = date(today.year, parts[1], parts[0])
            return d if d >= today else date(today.year + 1, parts[1], parts[0])
    except ValueError:
        pass
    raise SystemExit(f"Не понимаю дату «{value}». Примеры: 05.11, 05.11.2026, 2026-11-05")


def split_list(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


async def search(args) -> None:
    config = load_config()
    conn = db.connect(DB_PATH)
    today = date.today()
    aliases = {k.lower(): v.lower() for k, v in (config.get("aliases") or {}).items()}

    def matches(city: str, wanted: list[str]) -> bool:
        # Частичное совпадение: «анта» найдёт Анталью; синонимы из конфига тоже работают
        city = city.lower()
        return any(w in city or aliases.get(w, w) in city for w in wanted)

    flights = digest.select_flights(actual_offers(conn, config, today), include_seen=True)
    conn.close()

    title_parts = []
    if args.date:
        center = parse_date(args.date, today)
        lo = max(center - timedelta(days=args.days), today)
        hi = center + timedelta(days=args.days)
        flights = [f for f in flights if lo.isoformat() <= f["depart_date"] <= hi.isoformat()]
        title_parts.append(f"{lo.strftime('%d.%m')}–{hi.strftime('%d.%m')}")
    if args.to:
        flights = [f for f in flights if matches(f["destination"], split_list(args.to))]
        title_parts.append(args.to)
    if args.origin:
        flights = [f for f in flights if matches(f["origin"], split_list(args.origin))]
        title_parts.append(f"из {args.origin}")
    if args.min_price is not None:
        flights = [f for f in flights if f["price_kzt"] >= args.min_price]
    if args.max_price is not None:
        flights = [f for f in flights if f["price_kzt"] <= args.max_price]
    if args.min_price is not None or args.max_price is not None:
        title_parts.append(f"{args.min_price or 0:,}–{args.max_price or '∞':,} ₸".replace(",", " "))
    if args.oneway:
        flights = [f for f in flights if not f["return_date"]]
    if args.roundtrip:
        flights = [f for f in flights if f["return_date"]]

    title = "Поиск" + (f" ({', '.join(title_parts)})" if title_parts else "")
    if flights:
        blocks = digest.build(flights, today, title, 0)
    else:
        blocks = [f"🔍 {title}: ничего не найдено"]

    if args.send:
        client = telegram.make_client(SESSION_PATH)
        async with client:
            await telegram.send_to_saved(client, blocks)
        log.info("Результаты поиска отправлены в «Избранное»: %d рейсов", len(flights))
    else:
        print(digest.to_plain("\n\n".join(blocks)))


def listen_bot() -> None:
    conn = db.connect(DB_PATH)
    try:
        bot.listen(conn, bot.get_token())
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


async def login(qr: bool) -> None:
    client = telegram.make_client(SESSION_PATH)
    if qr:
        await telegram.qr_login(client)
    else:
        await client.start()
    me = await client.get_me()
    print(f"Вход выполнен: {me.first_name} (@{me.username}). Сессия сохранена в {SESSION_PATH}.session")
    await client.disconnect()


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="hunter", description="Дайджест чартерных билетов из Telegram")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login", help="войти в Telegram (один раз)")
    p_login.add_argument("--qr", action="store_true", help="войти по QR-коду вместо кода из сообщения")
    p_run = sub.add_parser("run", help="собрать новые посты и разослать дайджест подписчикам бота")
    p_run.add_argument("--dry-run", action="store_true", help="напечатать дайджест в консоль, не отправлять")
    p_run.add_argument("--all", action="store_true", help="показать все актуальные билеты, а не только новые")

    sub.add_parser("bot", help="слушать бота и сразу принимать подписки /start и /stop")

    p_search = sub.add_parser("search", help="найти рейсы в собранной базе по дате, направлению и цене")
    p_search.add_argument("--date", "-d", help="примерная дата вылета: 05.11, 05.11.2026 или 2026-11-05")
    p_search.add_argument("--days", type=int, default=5, help="± дней от даты (по умолчанию 5)")
    p_search.add_argument("--to", "-t", help="куда, через запятую: «Анталья,Пхукет,Нячанг»")
    p_search.add_argument("--from", dest="origin", help="откуда, через запятую (по умолчанию все из config.yaml)")
    p_search.add_argument("--min-price", type=int, help="цена от, ₸")
    p_search.add_argument("--max-price", type=int, help="цена до, ₸")
    p_search.add_argument("--oneway", action="store_true", help="только в одну сторону")
    p_search.add_argument("--roundtrip", action="store_true", help="только туда-обратно")
    p_search.add_argument("--send", action="store_true", help="отправить результат в «Избранное», а не в консоль")
    args = parser.parse_args()

    if args.cmd == "login":
        asyncio.run(login(args.qr))
    elif args.cmd == "bot":
        listen_bot()
    elif args.cmd == "search":
        asyncio.run(search(args))
    else:
        asyncio.run(run(dry_run=args.dry_run, include_seen=args.all))


if __name__ == "__main__":
    main()
