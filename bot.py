import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from currency import Rates
from db import DB
from poller import run_poller
from sources import build_sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

db = DB(config.DB_PATH)
sources = build_sources()
ready = [s for s in sources if s.ready]
router = Router()

HELP = (
    "Просто напиши название бренда (например <b>Nike</b>), и я начну присылать новые объявления "
    "с этого момента.\n\n"
    "/list: мои бренды (можно удалить)\n"
    "/countries: включить или выключить страны и площадки\n"
    "/help: эта подсказка"
)
ADMIN_HELP = "\n\nАдмин:\n/allow ID: дать доступ\n/deny ID: забрать доступ\n/users: список"


def has_access(uid: int) -> bool:
    return uid == config.ADMIN_ID or db.is_allowed(uid)


async def deny(m: Message):
    db.ensure_user(m.from_user.id, m.from_user.full_name or "")
    await m.answer(f"Нет доступа. Твой ID: <code>{m.from_user.id}</code>. Отправь его админу.")


@router.message(Command("start", "help"))
async def start(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    await m.answer(HELP + (ADMIN_HELP if m.from_user.id == config.ADMIN_ID else ""))


# ---------- бренды
@router.message(Command("list"))
async def list_subs(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    subs = db.user_subs(m.from_user.id)
    if not subs:
        return await m.answer("Брендов пока нет. Напиши название бренда.")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"❌ {s['brand']}", callback_data=f"del:{s['id']}")] for s in subs]
    )
    await m.answer("Твои бренды (нажми, чтобы удалить):", reply_markup=kb)


@router.callback_query(F.data.startswith("del:"))
async def del_sub(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    db.del_sub(c.from_user.id, int(c.data.split(":")[1]))
    subs = db.user_subs(c.from_user.id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"❌ {s['brand']}", callback_data=f"del:{s['id']}")] for s in subs]
    )
    await c.message.edit_text("Твои бренды (нажми, чтобы удалить):" if subs else "Список пуст.", reply_markup=kb if subs else None)
    await c.answer("Удалено")


# ---------- страны
def countries_kb(uid: int) -> InlineKeyboardMarkup:
    off = db.disabled_sources(uid)
    rows, row = [], []
    for s in ready:
        mark = "❌" if s.code in off else "✅"
        row.append(InlineKeyboardButton(text=f"{mark} {s.flag} {s.label}", callback_data=f"tg:{s.code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="✅ Включить все", callback_data="all:on"),
            InlineKeyboardButton(text="❌ Выключить все", callback_data="all:off"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("countries"))
async def countries(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    soon = ", ".join(s.label for s in sources if not s.ready)
    text = "Где искать. Нажми на страну, чтобы переключить.\n✅ включено, ❌ выключено"
    if soon:
        text += f"\nСкоро: {soon}"
    await m.answer(text, reply_markup=countries_kb(m.from_user.id))


@router.callback_query(F.data.startswith("tg:"))
async def toggle(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    code = c.data.split(":")[1]
    off = db.disabled_sources(c.from_user.id)
    now_on = code in off
    db.set_source(c.from_user.id, code, now_on)
    await c.answer("Включено ✅" if now_on else "Выключено ❌")  # сразу убирает «часики»
    try:
        await c.message.edit_reply_markup(reply_markup=countries_kb(c.from_user.id))
    except Exception:
        pass


@router.callback_query(F.data.startswith("all:"))
async def toggle_all(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    enable = c.data.endswith("on")
    for s in ready:
        db.set_source(c.from_user.id, s.code, enable)
    await c.answer("Все включены ✅" if enable else "Все выключены ❌")
    try:
        await c.message.edit_reply_markup(reply_markup=countries_kb(c.from_user.id))
    except Exception:
        pass


# ---------- админка
def is_admin(m: Message) -> bool:
    return m.from_user.id == config.ADMIN_ID


@router.message(Command("allow"))
async def allow(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        return await m.answer("Формат: /allow 123456789")
    db.ensure_user(uid, allowed=True)
    await m.answer(f"Доступ выдан: {uid}")


@router.message(Command("deny"))
async def deny_cmd(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        return await m.answer("Формат: /deny 123456789")
    db.ensure_user(uid, allowed=False)
    await m.answer(f"Доступ закрыт: {uid}")


@router.message(Command("users"))
async def users(m: Message):
    if not is_admin(m):
        return
    rows = db.users()
    text = "\n".join(f"{'✅' if r['allowed'] else '⛔'} <code>{r['id']}</code> {html.escape(r['name'] or '')}" for r in rows)
    await m.answer(text or "Пока никого.")


# ---------- диагностика: показывает, какие площадки отвечают
@router.message(Command("test"))
async def test_sources(m: Message):
    if not is_admin(m):
        return
    wait = await m.answer("Проверяю площадки, подожди до 30 секунд...")

    async def check(s):
        try:
            items = await asyncio.wait_for(s.search("nike"), timeout=25)
            return f"✅ {s.label}: {len(items)} объявл."
        except Exception as e:
            resp = getattr(e, "response", None)
            code = f"HTTP {resp.status_code}" if resp is not None else type(e).__name__
            return f"❌ {s.label}: {code}"

    lines = await asyncio.gather(*(check(s) for s in ready))
    await wait.edit_text("Результат проверки (запрос «nike»):\n" + "\n".join(lines))


# ---------- любой текст = новый бренд
@router.message(F.text & ~F.text.startswith("/"))
async def add_brand(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    brand = " ".join(m.text.lower().split())[:50]
    if db.add_sub(m.from_user.id, brand):
        await m.answer(
            f"Ищу <b>{html.escape(brand)}</b>. Пришлю только то, что появится с этого момента.\n"
            "Страны: /countries"
        )
    else:
        await m.answer("Этот бренд уже в списке.")


async def main():
    if not config.BOT_TOKEN or not config.ADMIN_ID:
        raise SystemExit("Заполни BOT_TOKEN и ADMIN_ID в файле .env")
    db.ensure_user(config.ADMIN_ID, "admin", allowed=True)
    rates = Rates()
    await rates.refresh()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(rates.loop())
    asyncio.create_task(run_poller(db, bot, rates, sources))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
