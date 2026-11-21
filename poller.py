import asyncio
import html
import logging
import random
import os
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import config
import translate
import vision
from currency import Rates
from db import DB
from sources import Item, Source

log = logging.getLogger("poller")
MAX_PER_TERM = 20  # защита от потока сообщений
MAX_AGE = 3 * 86400  # объявления старше 3 суток не присылаем
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


async def send(bot: Bot, uid: int, item: Item, caption: str, kb: InlineKeyboardMarkup | None = None):
    for _ in range(3):
        try:
            if item.photo:
                try:
                    return await bot.send_photo(uid, item.photo, caption=caption, reply_markup=kb)
                except TelegramBadRequest:
                    pass  # плохое фото: шлём текстом
            return await bot.send_message(uid, caption, disable_web_page_preview=False, reply_markup=kb)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            return  # пользователь заблокировал бота
        except TelegramBadRequest:
            if kb is None:
                return
            kb = None  # вдруг кнопка с неверной ссылкой: шлём без кнопок



# ------------------------------------------------ Certilogo (Stone Island, C.P. Company)
CERTI_BRANDS = {"stoneisland", "cpcompany"}
CERTI_WORDS = ("certilogo", "certi logo", "certi-logo", "сертилого", "серти лого", "серти-лого")


async def certilogo_note(source, item: Item) -> str | None:
    """Смотрит описание объявления: упомянут ли Certilogo. Проверить сам код может только человек
    на certilogo.com (у сервиса нет открытого API), поэтому это именно пометка, а не проверка подлинности."""
    try:
        r = await asyncio.wait_for(source.get(item.url, headers={"Accept": "text/html"}), timeout=20)
        if r.status_code != 200:
            return None
        text = r.text.lower()
        if any(w in text for w in CERTI_WORDS):
            return "🔎 Certilogo: упомянут в объявлении. Код надо проверить самому на certilogo.com (бот сам подлинность не проверяет)"
        return "🔎 Certilogo: в объявлении не упомянут (подлинность не подтверждена)"
    except Exception as e:
        log.warning("certilogo %s: %r", item.url, e)
        return None


async def add_note(bot: Bot, uid: int, msg, text: str, note_task):
    note = await note_task
    if not note or msg is None:
        return
    try:
        if getattr(msg, "photo", None):
            await bot.edit_message_caption(chat_id=uid, message_id=msg.message_id, caption=text + "\n" + note)
        else:
            await bot.edit_message_text(text + "\n" + note, chat_id=uid, message_id=msg.message_id)
    except Exception as e:
        log.warning("не удалось дописать Certilogo: %r", e)


def dedup_key(source: Source, item) -> str:
    # У Vinted номер объявления общий для всех стран: FR и NL показывают одно и то же
    return f"vinted:{item.id}" if source.code.startswith("vinted_") else f"{source.code}:{item.id}"


async def process(db: DB, bot: Bot, rates: Rates, source: Source, brand: str, query: str):
    items = await source.search(query)
    if getattr(source, "title_has_brand", False):  # поиск подмешивает чужие бренды: оставляем только нужный
        names = await translate.names(brand, query)
        items = [i for i in items if translate.has_brand(i.title, names)]
    first_time = not db.is_baselined(source.code, query)
    new = [i for i in items if not db.is_seen(source.code, i.id)]
    for i in new:
        db.mark_seen(source.code, i.id)
    users = db.subscribers(brand, source.code)
    if first_time:
        db.set_baselined(source.code, query)  # старые объявления не шлём
        for i in new:
            for uid, *_ in users:
                db.claim_sent(uid, dedup_key(source, i))
        return
    fresh = [i for i in new if not i.created or time.time() - i.created < MAX_AGE]  # старые объявления не шлём
    certi = source.code.startswith("vinted_") and translate.norm(await translate.latin(brand)) in CERTI_BRANDS
    refs = {sid: [vision.from_bytes(b) for b in bl] for sid, bl in db.refs_by_brand(brand).items()}
    for i in list(reversed(fresh))[-MAX_PER_TERM:]:  # от старых к новым
        caption = format_caption(i, source, brand, rates)
        key = dedup_key(source, i)
        rub = rates.to_rub(i.price, i.currency)
        emb, emb_done = None, False
        note_task = None
        for uid, limit, sub_id in users:
            if limit and rub is not None and rub > limit:
                continue  # дороже лимита цены
            extra = ""
            if sub_id in refs:  # бренд с фото: присылаем только похожее на твои фото
                if not emb_done:
                    emb = await vision.embed_url(i.photo) if i.photo else None
                    emb_done = True
                if emb is None:
                    if not vision.failed():
                        continue  # фото объявления не сравнить: не показываем
                    extra = "\n⚠️ Сравнение по фото сейчас недоступно"
                else:
                    sim = max(vision.similarity(emb, r) for r in refs[sub_id])
                    if sim < config.PHOTO_MATCH:
                        continue  # не похоже на твоё фото
                    extra = f"\n🖼 Похоже на твоё фото: {sim * 100:.0f}%"
            if db.claim_sent(uid, key):  # одно и то же объявление в другой стране/по другому бренду не шлём
                msg = await send(bot, uid, i, caption + extra)  # сначала сразу, без ожидания проверок
                if certi and msg is not None:
                    if note_task is None:
                        note_task = asyncio.create_task(certilogo_note(source, i))
                    asyncio.create_task(add_note(bot, uid, msg, caption + extra, note_task))


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
        t0 = time.monotonic()
        try:
            ok, fail = await poll_source_once(db, bot, rates, source)
            backoff = min(backoff * 2 + 30, 600) if (fail and not ok) else 0
        except Exception as e:
            backoff = min(backoff * 2 + 30, 600)
            log.warning("%s: ошибка %s, пауза %ss", source.code, e, backoff)
        if end is not None and time.monotonic() >= end:
            break
        # интервал считаем от НАЧАЛА круга: если круг уже длился дольше, следующий стартует сразу
        spent = time.monotonic() - t0
        await asyncio.sleep(max(1.0, interval - spent) + backoff + random.uniform(0, 1))


async def run_poller(db: DB, bot: Bot, rates: Rates, sources: list[Source]):
    await asyncio.gather(*(source_loop(db, bot, rates, s) for s in sources if s.ready))
