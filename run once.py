"""Запуск для GitHub Actions. Если задан LOOP_MINUTES, работает непрерывно
столько минут: быстро отвечает на команды и регулярно опрашивает площадки."""
import asyncio
import logging
import os
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import bot as botmod
import config
from currency import Rates
from poller import source_loop

log = logging.getLogger("once")
db = botmod.db
LOOP_SECONDS = int(os.environ.get("LOOP_MINUTES") or 0) * 60


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


async def update_loop(bot: Bot, dp: Dispatcher, end: float):
    while True:
        try:
            await handle_updates(bot, dp)
        except Exception as e:
            log.warning("Ошибка получения сообщений: %r", e)
        if time.monotonic() >= end:
            break
        await asyncio.sleep(1)


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
        end = time.monotonic() + LOOP_SECONDS
        await asyncio.gather(
            update_loop(bot, dp, end),
            *(source_loop(db, bot, rates, s, end) for s in botmod.ready),
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
