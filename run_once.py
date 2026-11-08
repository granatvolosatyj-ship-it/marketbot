"""Одноразовый запуск для GitHub Actions: обработать команды из Telegram,
один раз опросить площадки, отправить новое и завершиться."""
import asyncio
import logging
import random

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import bot as botmod
import config
import translate
from currency import Rates
from poller import process

log = logging.getLogger("once")
db = botmod.db


def get_offset() -> int | None:
    db.c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    r = db.c.execute("SELECT v FROM kv WHERE k='offset'").fetchone()
    return int(r["v"]) if r else None


def set_offset(v: int):
    db.c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('offset', ?)", (str(v),))
    db.c.commit()


async def handle_updates(bot: Bot, dp: Dispatcher):
    offset = get_offset()
    for _ in range(10):
        updates = await bot.get_updates(
            offset=offset, timeout=0, limit=100, allowed_updates=["message", "callback_query"]
        )
        if not updates:
            break
        for u in updates:
            try:
                await dp.feed_update(bot, u)
            except Exception:
                log.exception("Ошибка обработки сообщения")
            offset = u.update_id + 1
        set_offset(offset)


async def poll_source(bot: Bot, rates: Rates, s):
    for brand in db.active_terms(s.code):
        queries = [brand]
        if s.lang:
            queries += await translate.variants(brand, s.lang)
        for q in queries:
            try:
                await process(db, bot, rates, s, brand, q)
            except Exception as e:
                log.warning("%s '%s': %s", s.code, q, e)
            await asyncio.sleep(random.uniform(0.5, 1.5))


async def main():
    if not config.BOT_TOKEN or not config.ADMIN_ID:
        raise SystemExit("Нет BOT_TOKEN или ADMIN_ID")
    db.ensure_user(config.ADMIN_ID, "admin", allowed=True)
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(botmod.router)
    rates = Rates()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await handle_updates(bot, dp)
        await rates.refresh()
        await asyncio.wait_for(
            asyncio.gather(*(poll_source(bot, rates, s) for s in botmod.ready)), timeout=420
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
