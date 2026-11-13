import asyncio
import html
import logging
import random
import os
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import config
import translate
from currency import Rates
from db import DB
from sources import Item, Source

log = logging.getLogger("poller")
MAX_PER_TERM = 20  # защита от потока сообщений
CONCURRENCY = 4  # сколько запросов к одной площадке одновременно


TZ = timezone(timedelta(hours=int(os.environ.get("TZ_HOURS") or 5)))  # Екатеринбург = UTC+5


def when_line(item: Item) -> str:
    now = time.time()
    if item.created:
        ago = max(0, int(now - item.created))
        if ago < 90:
            rel = "только что"
        elif ago < 3600:
            rel = f"{ago // 60} мин назад"
        elif ago < 86400:
            rel = f"{ago // 3600} ч назад"
        else:
            rel = f"{ago // 86400} дн назад"
        clock = datetime.fromtimestamp(item.created, TZ).strftime("%d.%m %H:%M")
        return f"🕒 Выставлено: {clock} ({rel})\n"
    return f"🕒 Найдено ботом: {datetime.fromtimestamp(now, TZ).strftime('%d.%m %H:%M')}\n"


def format_caption(item: Item, source: Source, brand: str, rates: Rates) -> str:
    price = f"{item.price:,.0f}".replace(",", " ")
    rub = rates.to_rub(item.price, item.currency)
    rub_s = f" ≈ <b>{rub:,.0f} ₽</b>".replace(",", " ") if rub is not None else ""
    return (
        f"🛍 {html.escape(item.title)[:200]}\n"
        f"{source.flag} {source.label} · бренд: {html.escape(brand)}\n"
        f"💰 {price} {item.currency}{rub_s}\n"
        f"{when_line(item)}"
        f"🔗 {item.url}"
    )


async def send(bot: Bot, uid: int, item: Item, caption: str):
    for _ in range(3):
        try:
            if item.photo:
                try:
                    await bot.send_photo(uid, item.photo, caption=caption)
                    return
                except TelegramBadRequest:
                    pass  # плохое фото: шлём текстом
            await bot.send_message(uid, caption, disable_web_page_preview=False)
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            return  # пользователь заблокировал бота


def dedup_key(source: Source, item) -> str:
    # У Vinted номер объявления общий для всех стран: FR и NL показывают одно и то же
    return f"vinted:{item.id}" if source.code.startswith("vinted_") else f"{source.code}:{item.id}"


async def process(db: DB, bot: Bot, rates: Rates, source: Source, brand: str, query: str):
    items = await source.search(query)
    first_time = not db.is_baselined(source.code, query)
    new = [i for i in items if not db.is_seen(source.code, i.id)]
    for i in new:
        db.mark_seen(source.code, i.id)
    users = db.subscribers(brand, source.code)
    if first_time:
        db.set_baselined(source.code, query)  # старые объявления не шлём
        for i in new:
            for uid, _ in users:
                db.claim_sent(uid, dedup_key(source, i))
        return
    for i in list(reversed(new))[-MAX_PER_TERM:]:  # от старых к новым
        caption = format_caption(i, source, brand, rates)
        key = dedup_key(source, i)
        rub = rates.to_rub(i.price, i.currency)
        for uid, limit in users:
            if limit and rub is not None and rub > limit:
                continue  # дороже лимита цены
            if db.claim_sent(uid, key):  # одно и то же объявление в другой стране/по другому бренду не шлём
                await send(bot, uid, i, caption)


async def poll_source_once(db: DB, bot: Bot, rates: Rates, source: Source) -> tuple[int, int]:
    """Проверяет ВСЕ бренды на площадке одновременно. Новое отправляется сразу."""
    sem = asyncio.Semaphore(CONCURRENCY)
    result = {"ok": 0, "fail": 0}

    async def one(brand: str, query: str):
        async with sem:
            try:
                await process(db, bot, rates, source, brand, query)
                result["ok"] += 1
            except Exception as e:
                result["fail"] += 1
                log.warning("%s '%s': %s", source.code, query, e)
            await asyncio.sleep(random.uniform(0.1, 0.4))

    jobs = []
    for brand in db.active_terms(source.code):
        queries = await translate.queries(brand, source.lang)
        jobs += [one(brand, q) for q in queries]
    if jobs:
        await asyncio.gather(*jobs)
    return result["ok"], result["fail"]


async def source_loop(db: DB, bot: Bot, rates: Rates, source: Source, end: float | None = None):
    """Бесконечный цикл (VPS) или цикл до момента end (GitHub Actions)."""
    backoff = 0
    interval = getattr(source, "interval", None) or config.POLL_INTERVAL
    # разносим старт площадок по времени, чтобы запросы не шли пачкой
    await asyncio.sleep(random.uniform(0, min(interval, 60) if getattr(source, "interval", None) else 5))
    while True:
        try:
            ok, fail = await poll_source_once(db, bot, rates, source)
            backoff = min(backoff * 2 + 30, 600) if (fail and not ok) else 0
        except Exception as e:
            backoff = min(backoff * 2 + 30, 600)
            log.warning("%s: ошибка %s, пауза %ss", source.code, e, backoff)
        if end is not None and time.monotonic() >= end:
            break
        await asyncio.sleep(interval + backoff + random.uniform(0, 3))


async def run_poller(db: DB, bot: Bot, rates: Rates, sources: list[Source]):
    await asyncio.gather(*(source_loop(db, bot, rates, s) for s in sources if s.ready))
