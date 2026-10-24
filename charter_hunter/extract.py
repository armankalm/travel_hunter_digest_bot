"""Разбор свободного текста поста в список предложений через `claude -p` (подписка Claude Code, без API-ключа)."""

import json
import os
import re
import shutil
import subprocess
import tempfile

MODEL = os.getenv("CLAUDE_MODEL") or "haiku"
TIMEOUT_SEC = 300

OFFER_SCHEMA = {
    "type": "object",
    "properties": {
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["ticket", "tour"]},
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "depart_date": {"type": "string"},
                    "return_date": {"type": "string"},
                    "price": {"type": "number"},
                    "currency": {"type": "string", "enum": ["KZT", "USD", "EUR", "KGS", "RUB", "UZS"]},
                    "airline": {"type": "string"},
                    "seats": {"type": "integer"},
                },
                "required": [
                    "kind", "origin", "destination", "depart_date", "return_date",
                    "price", "currency", "airline", "seats",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["offers"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Ты разбираешь посты из Telegram-каналов с продажей чартерных и «отказных» авиабилетов (Казахстан, Кыргызстан).
Извлеки каждое конкретное предложение: одна строка с датой и ценой = одно предложение. Если в посте 15 дат — верни 15 предложений.

Поля:
- kind: "ticket" — авиабилет (в одну сторону или туда-обратно). "tour" — пакет с отелем, трансфером, питанием или цена «на двоих».
- origin, destination: город на русском в именительном падеже, коротко и единообразно: Алматы, Астана, Шымкент, Бишкек, Анталья, Дубай, Пхукет, Нячанг, Фукуок, Гоа, Мале, Шарм-эль-Шейх и т.п. Аэропорт → город («Мопа (Гоа)» → «Гоа», «Nha Trang» → «Нячанг»).
- «Астана → Дубай → Астана» с двумя датами — один билет туда-обратно: origin = Астана, destination = Дубай, return_date = вторая дата.
- «Дубай → Астана» — билет в одну сторону с origin = Дубай.
- depart_date, return_date: формат YYYY-MM-DD. return_date = "" для билета в одну сторону. Год в постах обычно не пишут: бери ближайшую дату не раньше даты публикации (месяц-два назад от публикации — значит следующий год).
- price: число за одного человека для билета, как написано в посте (без пробелов). Для тура — полная цена как указана.
- currency: ₸, тг, KZT или цена без валюты → KZT. $ → USD, сом → KGS, ₽ → RUB.
- airline: название авиакомпании, если понятно (FZ → flydubai, FS → FlyArystan, KC → Air Astana, DV → SCAT). Однобуквенные пометки перед датой (P, S, SE, V, SP) — коды перевозчика у конкретного канала: если однозначно не расшифровываются, оставь их как есть. Нет данных — "".
- seats: число оставшихся мест, если указано («(1)», «1 место», «осталось 2 места»), иначе 0.

Посты без конкретных цен и дат (реклама, визы, отзывы, видео, общие фразы «билеты от 21 000») → пустой список offers."""

# Без хотя бы одной цены вида «85 000» / «85000» пост точно не содержит предложений
PRICE_HINT = re.compile(r"\d{2,3}[  .,]?\d{3}")


def looks_like_offer(text: str) -> bool:
    return bool(PRICE_HINT.search(text))


def extract(text: str, posted_at: str) -> list[dict]:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("Не найден CLI `claude` в PATH")

    cmd = [
        claude, "-p",
        "--model", MODEL,
        "--system-prompt", SYSTEM_PROMPT,
        "--json-schema", json.dumps(OFFER_SCHEMA),
        "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
    ]
    # Без ANTHROPIC_API_KEY CLI использует вход по подписке
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    prompt = f"Дата публикации: {posted_at[:10]}\n\nПост:\n{text}"
    # Запуск из пустой папки, чтобы CLI не подхватывал CLAUDE.md и файлы проекта
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=TIMEOUT_SEC, cwd=cwd, env=env
        )

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p вернул не JSON (код {proc.returncode}): {(proc.stderr or proc.stdout)[:500]}")
    if result.get("is_error") or "structured_output" not in result:
        raise RuntimeError(f"claude -p: {result.get('subtype')} {str(result.get('result'))[:500]}")
    return result["structured_output"]["offers"]
