import asyncio
import html
import logging
import random

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import config
import translate
from currency import Rates
from db import DB
from sources import Item, Source

log = logging.getLogger("poller")
MAX_PER_TERM = 20  # защита от потока сообщений


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
            await asyncio.sleep(0.05)


async def source_loop(db: DB, bot: Bot, rates: Rates, source: Source):
    backoff = 0
    await asyncio.sleep(random.uniform(0, 10))
    while True:
        try:
            for brand in db.active_terms(source.code):
                queries = [brand]
                if source.lang:
                    queries += await translate.variants(brand, source.lang)
                for q in queries:
                    await process(db, bot, rates, source, brand, q)
                    await asyncio.sleep(random.uniform(1.5, 3.5))
            backoff = 0
        except Exception as e:
            backoff = min(backoff * 2 + 30, 600)
            log.warning("%s: ошибка %s, пауза %ss", source.code, e, backoff)
        await asyncio.sleep(config.POLL_INTERVAL + backoff + random.uniform(0, 8))


async def run_poller(db: DB, bot: Bot, rates: Rates, sources: list[Source]):
    await asyncio.gather(*(source_loop(db, bot, rates, s) for s in sources if s.ready))
