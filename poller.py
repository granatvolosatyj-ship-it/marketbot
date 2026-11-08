import asyncio
import html
import logging
import random
import time

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


def format_caption(item: Item, source: Source, brand: str, rates: Rates) -> str:
    price = f"{item.price:,.0f}".replace(",", " ")
    rub = rates.to_rub(item.price, item.currency)
    rub_s = f" ≈ <b>{rub:,.0f} ₽</b>".replace(",", " ") if rub is not None else ""
    return (
        f"🛍 {html.escape(item.title)[:200]}\n"
        f"{source.flag} {source.label} · бренд: {html.escape(brand)}\n"
        f"💰 {price} {item.currency}{rub_s}\n"
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


async def process(db: DB, bot: Bot, rates: Rates, source: Source, brand: str, query: str):
    items = await source.search(query)
    first_time = not db.is_baselined(source.code, query)
    new = [i for i in items if not db.is_seen(source.code, i.id)]
    for i in new:
        db.mark_seen(source.code, i.id)
    if first_time:
        db.set_baselined(source.code, query)  # старые объявления не шлём
        return
    users = db.subscribers(brand, source.code)
    for i in list(reversed(new))[-MAX_PER_TERM:]:  # от старых к новым
        caption = format_caption(i, source, brand, rates)
        for uid in users:
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
        queries = [brand]
        if source.lang:
            queries += await translate.variants(brand, source.lang)
        jobs += [one(brand, q) for q in queries]
    if jobs:
        await asyncio.gather(*jobs)
    return result["ok"], result["fail"]


async def source_loop(db: DB, bot: Bot, rates: Rates, source: Source, end: float | None = None):
    """Бесконечный цикл (VPS) или цикл до момента end (GitHub Actions)."""
    backoff = 0
    await asyncio.sleep(random.uniform(0, 5))
    while True:
        try:
            ok, fail = await poll_source_once(db, bot, rates, source)
            backoff = min(backoff * 2 + 30, 600) if (fail and not ok) else 0
        except Exception as e:
            backoff = min(backoff * 2 + 30, 600)
            log.warning("%s: ошибка %s, пауза %ss", source.code, e, backoff)
        if end is not None and time.monotonic() >= end:
            break
        await asyncio.sleep(config.POLL_INTERVAL + backoff + random.uniform(0, 3))


async def run_poller(db: DB, bot: Bot, rates: Rates, sources: list[Source]):
    await asyncio.gather(*(source_loop(db, bot, rates, s) for s in sources if s.ready))
