import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

import config
from currency import Rates
from db import DB
import avito
from poller import run_poller
from sources import build_sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

db = DB(config.DB_PATH)
sources = build_sources()
ready = [s for s in sources if s.ready]
router = Router()

HELP = (
    "Напиши название бренда (например <b>Nike</b>) или нажми «➕ Добавить бренд». Я спрошу максимальную цену "
    "и на каких площадках искать, и начну присылать новые объявления с этого момента.\n\n"
    "Кнопки внизу:\n"
    "📋 Мои бренды: настроить каждый бренд (где искать, лимит цены) или удалить\n"
    "💰 Лимит цены: общий лимит или для одного бренда (бот спросит сумму в рублях)\n"
    "🌍 Страны и площадки: включить или выключить площадки сразу для всех брендов\n"
    "⏸ Пауза / ▶️ Старт: остановить и снова включить уведомления\n"
)
ADMIN_HELP = "\n\nАдмин:\n/allow ID: дать доступ\n/deny ID: забрать доступ\n/users: список"


def has_access(uid: int) -> bool:
    return uid == config.ADMIN_ID or db.is_allowed(uid)


requested: set[int] = set()  # кому админу уже отправили заявку (в этом запуске)


async def deny(m: Message):
    """Нет доступа: человеку пишем его ID, админу присылаем заявку с кнопками «Принять» / «Отклонить»."""
    u = m.from_user
    db.ensure_user(u.id, u.full_name or "")
    if db.is_denied(u.id):
        return  # админ уже отклонил: молчим
    await m.answer(
        f"Нет доступа. Заявка отправлена админу, жди ответа. Твой ID: <code>{u.id}</code>."
    )
    if u.id in requested or not config.ADMIN_ID:
        return
    requested.add(u.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"acc:{u.id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rej:{u.id}"),
    ]])
    uname = f"@{u.username}" if u.username else "без username"
    try:
        await m.bot.send_message(
            config.ADMIN_ID,
            "🔔 <b>Новая заявка на доступ</b>\n"
            f"Имя: {html.escape(u.full_name or '—')}\n{html.escape(uname)}\nID: <code>{u.id}</code>",
            reply_markup=kb,
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("acc:"))
async def request_accept(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        return await c.answer()
    uid = int(c.data.split(":")[1])
    db.ensure_user(uid, allowed=True)
    requested.discard(uid)
    await c.message.edit_text(c.message.html_text + "\n\n✅ <b>Принят</b>")
    await c.answer("Доступ выдан")
    try:
        await c.bot.send_message(uid, "✅ Админ принял заявку. Нажми /start, чтобы начать.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("rej:"))
async def request_reject(c: CallbackQuery):
    if c.from_user.id != config.ADMIN_ID:
        return await c.answer()
    uid = int(c.data.split(":")[1])
    db.set_denied(uid, True)
    await c.message.edit_text(c.message.html_text + "\n\n❌ <b>Отклонён</b>")
    await c.answer("Отклонено")
    try:
        await c.bot.send_message(uid, "❌ Админ отклонил заявку на доступ.")
    except Exception:
        pass


BTN_FILTER = "➕ Добавить бренд"
BTN_BRANDS = "📋 Мои бренды"
BTN_LIMIT = "💰 Лимит цены"
BTN_WHERE = "🌍 Страны и площадки"
BTN_PAUSE = "⏸ Пауза"
BTN_RESUME = "▶️ Старт"
BTN_TEST = "🧪 Тест"
BTN_HELP = "❓ Помощь"


def menu_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_FILTER), KeyboardButton(text=BTN_BRANDS)],
        [KeyboardButton(text=BTN_LIMIT), KeyboardButton(text=BTN_WHERE)],
        [KeyboardButton(text=BTN_HELP)],
        [KeyboardButton(text=BTN_PAUSE), KeyboardButton(text=BTN_RESUME)],
    ]
    if uid == config.ADMIN_ID:
        rows.append([KeyboardButton(text=BTN_TEST)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False,
                               input_field_placeholder="Напиши бренд, например Nike")


@router.message(Command("start", "help", "menu"))
async def start(m: Message, command: CommandObject):
    if not has_access(m.from_user.id):
        return await deny(m)
    if command.command == "start" and db.is_paused(m.from_user.id):
        db.set_paused(m.from_user.id, False)
        await m.answer("▶️ Уведомления снова включены.")
    await m.answer(HELP + (ADMIN_HELP if m.from_user.id == config.ADMIN_ID else ""),
                   reply_markup=menu_kb(m.from_user.id))


def sub_label(s) -> str:
    lim = f" (до {s['max_rub']:,.0f} ₽)".replace(",", " ") if s["max_rub"] else ""
    return f"❌ {s['brand']}{lim}"


def parse_amount(text: str) -> float | None:
    """«5000», «5 000», «5к», «5000₽» -> число; None, если не число."""
    import re

    t = text.lower().replace(" ", "").replace("\u00a0", "").replace("₽", "").replace("руб", "").rstrip("р.")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)([kк]?)", t)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v * 1000 if m.group(2) else v


@router.message(Command("stop"))
async def stop(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    db.set_paused(m.from_user.id, True)
    await m.answer(
        "⏸ Уведомления остановлены. Пока пауза, объявления не копятся: когда включишь снова, "
        "придёт только то, что появится после.\n/resume: включить"
    )


@router.message(Command("resume"))
async def resume(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    db.set_paused(m.from_user.id, False)
    await m.answer("▶️ Уведомления включены. Пришлю то, что появится с этого момента.")


pending: dict[int, str] = {}  # кто сейчас вводит сумму лимита: "all" или название бренда


@router.callback_query(F.data.startswith("lim:"))
async def limit_pick(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    what = c.data.split(":", 1)[1]
    if what == "all":
        pending[c.from_user.id] = "all"
        target = "всех брендов"
    else:
        sub = next((s for s in db.user_subs(c.from_user.id) if str(s["id"]) == what), None)
        if not sub:
            return await c.answer("Бренд уже удалён")
        pending[c.from_user.id] = sub["brand"]
        target = html.escape(sub["brand"])
    await c.answer()
    await c.message.answer(
        f"Напиши максимальную цену в рублях для {target}. Например: <b>5000</b>\n"
        "Чтобы убрать лимит, напиши <b>off</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="cin")]]),
    )


def step1_kb(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♾ Без лимита", callback_data=f"nl:{sub_id}")],
        [InlineKeyboardButton(text="❌ Отмена (не добавлять бренд)", callback_data=f"nc:{sub_id}")],
    ])


def step1_text(brand: str) -> str:
    return (
        f"<b>{html.escape(brand)}</b>\n\n"
        "Шаг 1 из 2. До какой цены искать? Напиши максимальную цену в рублях (например <b>5000</b>) "
        "или нажми «Без лимита»."
    )


@router.callback_query(F.data.startswith("nc:"))
async def new_cancel(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    pending.pop(c.from_user.id, None)
    sub = db.get_sub(c.from_user.id, int(c.data.split(":")[1]))
    if sub:
        db.del_sub(c.from_user.id, sub["id"])
    await c.message.edit_text(f"Отменено. Бренд «{html.escape(sub['brand'])}» не добавлен." if sub else "Отменено.")
    await c.answer("Отменено")


@router.callback_query(F.data.startswith("nb:"))
async def new_back(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    sub = db.get_sub(c.from_user.id, int(c.data.split(":")[1]))
    if not sub:
        return await c.answer("Бренд уже удалён")
    pending[c.from_user.id] = f"new:{sub['id']}"
    await c.message.edit_text(step1_text(sub["brand"]), reply_markup=step1_kb(sub["id"]))
    await c.answer()


@router.callback_query(F.data == "cin")
async def cancel_input(c: CallbackQuery):
    pending.pop(c.from_user.id, None)
    await c.message.edit_text("Отменено.")
    await c.answer()


@router.callback_query(F.data == "close")
async def close_msg(c: CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        await c.message.edit_reply_markup(reply_markup=None)
    await c.answer()


def step2_text(sub, amount) -> str:
    lim = f"до {amount:,.0f} ₽".replace(",", " ") if amount else "без лимита цены"
    return (
        f"Шаг 2 из 2. <b>{html.escape(sub['brand'])}</b> ({lim}).\n"
        "Где искать? Нажми на площадки: ✅ ищу, ❌ не ищу. Когда выберешь, нажми «Готово».\n"
        "Сейчас включены все."
    )


@router.callback_query(F.data.startswith("nl:"))
async def new_no_limit(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    pending.pop(c.from_user.id, None)
    sub = db.get_sub(c.from_user.id, int(c.data.split(":")[1]))
    if not sub:
        return await c.answer("Бренд уже удалён")
    db.set_brand_limit(c.from_user.id, sub["brand"], None)
    await c.message.edit_text(step2_text(sub, None), reply_markup=brand_kb(sub["id"], done=True))
    await c.answer("Без лимита")


@router.callback_query(F.data.startswith("nd:"))
async def new_done(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    sub = db.get_sub(c.from_user.id, int(c.data.split(":")[1]))
    if not sub:
        return await c.answer("Бренд уже удалён")
    db.finish_setup(sub["id"])  # только теперь бренд включается: до этого ничего не присылаем
    lim = f"до {sub['max_rub']:,.0f} ₽".replace(",", " ") if sub["max_rub"] else "без лимита цены"
    await c.message.edit_text(
        f"Готово ✅ Ищу <b>{html.escape(sub['brand'])}</b>: {lim}, площадки: {where_text(db.brand_disabled(sub['id']))}.\n"
        "Пришлю только то, что появится с этого момента. Изменить: /list"
    )
    await c.answer("Готово")


@router.message(Command("cancel"))
async def cancel(m: Message):
    was = pending.pop(m.from_user.id, None)
    if was and was.startswith("new:"):
        db.finish_setup(int(was.split(":")[1]))
    await m.answer(
        "Отменено. Бренд включён без лимита цены и на всех площадках (изменить: /list)."
        if was and was.startswith("new:") else "Отменено."
    )


async def apply_limit(m: Message, what: str):
    uid = m.from_user.id
    t = m.text.strip().lower()
    off = t in ("off", "выкл", "нет", "0")
    amount = None if off else parse_amount(t)
    if not off and amount is None:
        return await m.answer("Не понял. Напиши число, например 5000, или off, чтобы убрать лимит. Отмена: /cancel")
    pending.pop(uid, None)
    if what.startswith("new:"):  # шаг 1 из 2 при добавлении бренда: цена -> дальше выбор площадок
        sub = db.get_sub(uid, int(what.split(":")[1]))
        if not sub:
            return await m.answer("Бренд уже удалён.")
        db.set_brand_limit(uid, sub["brand"], amount)
        return await m.answer(step2_text(sub, amount), reply_markup=brand_kb(sub["id"], done=True))
    if what == "all":
        db.set_global_limit(uid, amount)
        who = "всех брендов"
    else:
        db.set_brand_limit(uid, what, amount)
        who = html.escape(what)
    done_text = f"Лимит для {who} снят ✅" if off else f"Для {who}: не дороже {amount:,.0f} ₽ ✅".replace(",", " ")
    if what != "all":  # после лимита бренда сразу показываем выбор стран и площадок
        sub = next((s for s in db.user_subs(uid) if s["brand"] == what), None)
        if sub:
            return await m.answer(
                done_text + "\n\n" + brand_text(sub) + "\nХочешь везде, как есть? Просто закрой эту панель.",
                reply_markup=brand_kb(sub["id"]),
            )
    await m.answer(done_text)


@router.message(Command("limit"))
async def limit(m: Message, command: CommandObject):
    if not has_access(m.from_user.id):
        return await deny(m)
    uid = m.from_user.id
    args = (command.args or "").strip()
    if not args:
        pending.pop(uid, None)
        g = db.global_limit(uid)
        lines = [f"Общий лимит: {f'{g:,.0f} ₽'.replace(',', ' ') if g else 'нет'}"]
        for s in db.user_subs(uid):
            if s["max_rub"]:
                lines.append(f"{s['brand']}: до {s['max_rub']:,.0f} ₽".replace(",", " "))
        lines.append("\nДля чего поставить лимит? Нажми кнопку, потом напиши сумму в рублях.")
        rows = [[InlineKeyboardButton(text="🌐 Для всех брендов", callback_data="lim:all")]]
        rows += [[InlineKeyboardButton(text=f"💰 {s['brand']}", callback_data=f"lim:{s['id']}")] for s in db.user_subs(uid)]
        rows.append([InlineKeyboardButton(text="⬅️ Закрыть", callback_data="close")])
        return await m.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    parts = args.split()
    last = parts[-1].lower()
    off = last in ("off", "выкл", "нет", "0")
    amount = None if off else parse_amount(parts[-1])
    if not off and amount is None:
        return await m.answer("Не понял сумму. Пример: /limit 5000 или /limit nike 3000 или /limit off")
    if len(parts) == 1:  # общий лимит
        db.set_global_limit(uid, amount)
        return await m.answer(
            "Лимит снят ✅" if off else f"Не буду присылать дороже {amount:,.0f} ₽ ✅".replace(",", " ")
        )
    brand = " ".join(" ".join(parts[:-1]).lower().split())
    if not db.set_brand_limit(uid, brand, amount):
        return await m.answer(f"Бренда «{html.escape(brand)}» нет в твоём списке (/list).")
    await m.answer(
        f"Для {html.escape(brand)} лимит снят ✅" if off
        else f"{html.escape(brand)}: не дороже {amount:,.0f} ₽ ✅".replace(",", " ")
    )


# ---------- бренды
def where_text(off: set[str]) -> str:
    on = [s for s in ready if s.code not in off]
    if len(on) == len(ready):
        return "везде"
    if not on:
        return "нигде"
    if all(s.code.startswith("vinted_") for s in on) and len(on) == sum(s.code.startswith("vinted_") for s in ready):
        return "только Vinted"
    if not any(s.code.startswith("vinted_") for s in on):
        return f"Азия ({len(on)})"
    return f"{len(on)} из {len(ready)}"


def list_kb(uid: int) -> InlineKeyboardMarkup:
    rows = []
    for s in db.user_subs(uid):
        lim = f" · до {s['max_rub']:,.0f} ₽".replace(",", " ") if s["max_rub"] else ""
        mark = "⏳ не настроен · " if s["setup"] else "⚙️ "
        text = f"{mark}{s['brand']}{lim} · {where_text(db.brand_disabled(s['id']))}"
        rows.append(
            [
                InlineKeyboardButton(text=text[:60], callback_data=f"bs:{s['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"del:{s['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Закрыть", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


LIST_TEXT = "Твои бренды. ⚙️ выбрать, где искать (страны и площадки), 🗑 удалить:"


COUNTRY_RU = {"fr": "Франция", "de": "Германия", "it": "Италия", "es": "Испания", "nl": "Нидерланды",
              "be": "Бельгия", "at": "Австрия", "pt": "Португалия", "lu": "Люксембург", "lt": "Литва",
              "pl": "Польша", "cz": "Чехия"}


def source_title(s) -> str:
    if s.code.startswith("vinted_"):
        return COUNTRY_RU.get(s.code[len("vinted_"):], s.label)
    return s.label


SECTIONS = [
    ("vt", "🇪🇺 VINTED", lambda s: s.code.startswith("vinted_")),
    ("jp", "🇯🇵 ЯПОНИЯ", lambda s: s.code.endswith("_jp")),
    ("kr", "🇰🇷 КОРЕЯ", lambda s: s.code.endswith("_kr") or s.code == "bunjang"),
    ("cn", "🇨🇳 КИТАЙ", lambda s: s.code.endswith("_cn")),
]


def shown_sources():
    return [s for s in ready if s.code != "2ndstreet_jp"]  # 2nd STREET пока не работает


def group_codes(key: str) -> list[str]:
    fits = next(f for k, _, f in SECTIONS if k == key)
    return [s.code for s in shown_sources() if fits(s)]


def source_rows(off: set[str], cb, hcb) -> list[list[InlineKeyboardButton]]:
    """Заголовки (нажатие включает/выключает всю группу) и под ними страны по две в ряд."""
    rows, used = [], set()
    shown = shown_sources()
    for key, title, fits in SECTIONS:
        group = [s for s in shown if fits(s) and s.code not in used]
        if not group:
            continue
        used.update(s.code for s in group)
        n_off = sum(1 for s in group if s.code in off)
        mark = "✅" if n_off == 0 else ("❌" if n_off == len(group) else "🔸")
        rows.append([InlineKeyboardButton(text=f"{mark} {title} · все сразу", callback_data=hcb(key))])
        row = []
        for s in group:
            m = "❌" if s.code in off else "✅"
            row.append(InlineKeyboardButton(text=f"{m} {s.flag} {source_title(s)}", callback_data=cb(s.code)))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    for s in [s for s in shown if s.code not in used]:
        m = "❌" if s.code in off else "✅"
        rows.append([InlineKeyboardButton(text=f"{m} {s.flag} {s.label}", callback_data=cb(s.code))])
    return rows


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


def brand_kb(sub_id: int, done: bool = False) -> InlineKeyboardMarkup:
    done = done or db.is_setup(sub_id)  # бренд ещё не настроен: показываем «Готово»
    off = db.brand_disabled(sub_id)
    rows = source_rows(off, lambda code: f"bt:{sub_id}:{code}", lambda k: f"bg:{sub_id}:{k}")
    rows.append(
        [
            InlineKeyboardButton(text="🌍 Все", callback_data=f"bp:{sub_id}:all"),
            InlineKeyboardButton(text="🇪🇺 Vinted", callback_data=f"bp:{sub_id}:eu"),
            InlineKeyboardButton(text="🇯🇵🇰🇷 Азия", callback_data=f"bp:{sub_id}:asia"),
        ]
    )
    av = db.get_avito(sub_id)
    rows.append([InlineKeyboardButton(
        text="🇷🇺 Авито: настроено ✅ (изменить)" if av else "🇷🇺 Добавить поиск на Авито",
        callback_data=f"av:{sub_id}")])
    if done:
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"nd:{sub_id}")])
        rows.append([
            InlineKeyboardButton(text="⬅️ Назад к цене", callback_data=f"nb:{sub_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"nc:{sub_id}"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="💰 Лимит цены для этого бренда", callback_data=f"lim:{sub_id}")])
        rows.append([InlineKeyboardButton(text="⬅️ К списку брендов", callback_data="bl")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def brand_text(sub) -> str:
    return (
        f"Где искать <b>{html.escape(sub['brand'])}</b>?\n"
        "✅ ищу, ❌ не ищу. Нажимай на площадку, чтобы включить или выключить. "
        "Другие бренды настраиваются отдельно."
    )


@router.message(Command("list"))
async def list_subs(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    if not db.user_subs(m.from_user.id):
        return await m.answer("Брендов пока нет. Напиши название бренда.")
    await m.answer(LIST_TEXT, reply_markup=list_kb(m.from_user.id))


@router.callback_query(F.data.startswith("del:"))
async def del_sub(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    db.del_sub(c.from_user.id, int(c.data.split(":")[1]))
    subs = db.user_subs(c.from_user.id)
    await c.message.edit_text(LIST_TEXT if subs else "Список пуст.", reply_markup=list_kb(c.from_user.id) if subs else None)
    await c.answer("Удалено")


@router.callback_query(F.data == "bl")
async def brands_back(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    subs = db.user_subs(c.from_user.id)
    await c.message.edit_text(LIST_TEXT if subs else "Список пуст.", reply_markup=list_kb(c.from_user.id) if subs else None)
    await c.answer()


@router.callback_query(F.data.startswith("bs:"))
async def brand_open(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    sub = db.get_sub(c.from_user.id, int(c.data.split(":")[1]))
    if not sub:
        return await c.answer("Бренд уже удалён")
    await c.message.edit_text(brand_text(sub), reply_markup=brand_kb(sub["id"]))
    await c.answer()


@router.callback_query(F.data.startswith("bt:"))
async def brand_toggle(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    _, sid, code = c.data.split(":", 2)
    sub = db.get_sub(c.from_user.id, int(sid))
    if not sub or code not in {s.code for s in ready}:
        return await c.answer()
    off = db.brand_disabled(sub["id"])
    now_on = code in off
    off.symmetric_difference_update({code})
    db.set_brand_sources(sub["id"], off)
    await c.message.edit_reply_markup(reply_markup=brand_kb(sub["id"]))
    await c.answer("Включено ✅" if now_on else "Выключено ❌")  # сразу убирает «часики»


@router.callback_query(F.data.startswith("bg:"))
async def brand_group(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    _, sid, key = c.data.split(":")
    sub = db.get_sub(c.from_user.id, int(sid))
    if not sub:
        return await c.answer()
    codes = set(group_codes(key))
    off = db.brand_disabled(sub["id"])
    all_on = not (codes & off)  # все включены -> выключаем, иначе включаем все
    off = (off | codes) if all_on else (off - codes)
    db.set_brand_sources(sub["id"], off)
    await c.message.edit_reply_markup(reply_markup=brand_kb(sub["id"]))
    await c.answer("Группа выключена ❌" if all_on else "Группа включена ✅")


@router.callback_query(F.data.startswith("gg:"))
async def global_group(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    codes = group_codes(c.data.split(":")[1])
    off = db.disabled_sources(c.from_user.id)
    all_on = not (set(codes) & off)
    for code in codes:
        db.set_source(c.from_user.id, code, not all_on)
    await c.answer("Группа выключена ❌" if all_on else "Группа включена ✅")
    try:
        await c.message.edit_reply_markup(reply_markup=countries_kb(c.from_user.id))
    except Exception:
        pass


@router.callback_query(F.data.startswith("bp:"))
async def brand_preset(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    _, sid, kind = c.data.split(":")
    sub = db.get_sub(c.from_user.id, int(sid))
    if not sub:
        return await c.answer()
    codes = [s.code for s in ready]
    if kind == "eu":
        off = {x for x in codes if not x.startswith("vinted_")}
    elif kind == "asia":
        off = {x for x in codes if x.startswith("vinted_")}
    else:
        off = set()
    db.set_brand_sources(sub["id"], off)
    await c.message.edit_reply_markup(reply_markup=brand_kb(sub["id"]))
    await c.answer("Готово ✅")


# ---------- страны
def countries_kb(uid: int) -> InlineKeyboardMarkup:
    off = db.disabled_sources(uid)
    rows = source_rows(off, lambda code: f"tg:{code}", lambda k: f"gg:{k}")
    rows.append(
        [
            InlineKeyboardButton(text="✅ Включить все", callback_data="all:on"),
            InlineKeyboardButton(text="❌ Выключить все", callback_data="all:off"),
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Закрыть", callback_data="close")])
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
        if s.code.startswith("vinted_") and s.code not in ("vinted_fr", "vinted_de"):
            return f"⏭ {s.label}: не проверяю (та же система, что FR и DE; частые проверки вызывают блокировку 429)"
        try:
            items = await asyncio.wait_for(s.search("nike"), timeout=25)
            return f"✅ {s.label}: {len(items)} объявл."
        except Exception as e:
            resp = getattr(e, "response", None)
            code = f"HTTP {resp.status_code}" if resp is not None else html.escape(str(e)[:90] or type(e).__name__)
            line = f"❌ {s.label}: {code}"
            if resp is not None and s.code in ("vinted_fr", "bunjang", "2ndstreet_jp"):
                body = " ".join(resp.text.split())[:160]
                server = resp.headers.get("server", "?")
                line += f"\n   сервер: {html.escape(server)}\n   ответ: {html.escape(body)}"
            return line

    async def safe(s):
        try:
            return await asyncio.wait_for(check(s), timeout=40)
        except BaseException as e:  # что бы ни случилось, отчёт всё равно придёт
            return f"❌ {s.label}: {html.escape(type(e).__name__)}"

    async def av_check():
        try:
            n = len(await asyncio.wait_for(avito.search("nike"), timeout=30))
            return f"✅ Авито: {n} объявл."
        except BaseException as e:
            return f"❌ Авито: {html.escape(str(e)[:120] or type(e).__name__)}"

    lines = await asyncio.gather(*(safe(s) for s in ready), av_check())
    text = "Результат проверки (запрос «nike»):\n" + "\n".join(lines)
    try:
        await wait.edit_text(text)
    except Exception:
        import re as _re
        await m.answer(_re.sub(r"<[^>]*>", "", text), parse_mode=None)


# ---------- диагностика: показывает, что реально отдаёт страница поиска
RAW_URLS = {
    "bunjang": "https://m.bunjang.co.kr/search/products?q=nike&order=date",
    "2ndstreet_jp": "https://www.2ndstreet.jp/search?keyword=nike&sortBy=arrival",
}


@router.message(Command("raw"))
async def raw_page(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    import re

    code = (command.args or "").strip()
    s = next((x for x in sources if x.code == code), None)
    if s is None:
        return await m.answer("Формат: /raw vinted_fr  или  /raw bunjang  или  /raw 2ndstreet_jp")
    if code.startswith("vinted_"):
        url = f"https://www.vinted.{code.split('_')[1]}/catalog?search_text=nike&order=newest_first"
    else:
        url = RAW_URLS.get(code)
    if not url:
        return await m.answer("Для этой площадки проверка не настроена.")
    try:
        r = await s.get(url, headers={"Accept": "text/html,application/xhtml+xml",
                                      "Accept-Language": "en,ja;q=0.8,ko;q=0.6"})
    except Exception as e:
        return await m.answer("Ошибка: " + html.escape(repr(e))[:500])
    page = r.text
    hints = ("/items/", "/products/", "/product/", "goods", "detail")
    hrefs = []
    for h in re.findall(r'href="([^"]+)"', page):
        if any(x in h for x in hints) and h not in hrefs:
            hrefs.append(h)
    apis = list(dict.fromkeys(re.findall(r'https?://[\w.-]*api[\w.-]*/[^"\'\s<>\\]{0,80}', page)))
    out = [f"HTTP {r.status_code}, размер {len(page)}"]
    out.append("ссылки на товары: " + (" | ".join(hrefs[:5]) or "нет"))
    out.append("api-адреса: " + (" | ".join(apis[:5]) or "нет"))
    if hrefs:
        i = page.find(hrefs[0])
        out.append("кусок страницы:\n" + " ".join(page[max(0, i - 200): i + 900].split()))
    await m.answer(html.escape("\n".join(out)[:3500]))


@router.message(Command("peek"))
async def peek(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    key = (command.args or "").strip().lower()
    if key.startswith("avito") or key.startswith("авито"):
        await m.answer("Смотрю Авито, подожди до 40 секунд...")
        try:
            text = await asyncio.wait_for(avito.diag("nike"), timeout=40)
        except Exception as e:
            text = f"Ошибка: {e!r}"
        return await m.answer(html.escape(text)[:3800])
    for s in ready:
        if key and key in s.code and hasattr(s, "diag"):
            await m.answer("Смотрю, подожди до 40 секунд...")
            try:
                text = await asyncio.wait_for(s.diag("nike"), timeout=40)
            except Exception as e:
                text = f"Ошибка: {e!r}"
            return await m.answer(html.escape(text)[:3800])
    await m.answer("Формат: /peek bunjang, /peek fruits, /peek vinted или /peek 2nd")


# ---------- Авито: 3 вопроса (регион, категория, цена)
def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def av_summary(sub, av) -> str:
    lim = f"до {av['max_rub']:,.0f} ₽".replace(",", " ") if av["max_rub"] else "без лимита"
    return (f"🇷🇺 <b>Авито</b> для «{html.escape(sub['brand'])}»\n"
            f"Где: {avito.region_name(av['region'])}\n"
            f"Категория: {avito.cat_name(av['cat'])}\n"
            f"Цена: {lim}")


def av_q1(sub) -> tuple[str, InlineKeyboardMarkup]:
    return (f"🇷🇺 Авито · <b>{html.escape(sub['brand'])}</b>\n\n<b>Вопрос 1/3.</b> Где искать?",
            InlineKeyboardMarkup(inline_keyboard=[
                [_btn("🇷🇺 Все регионы", f"avr:{sub['id']}:all")],
                [_btn("📍 Определённый регион", f"avr:{sub['id']}:pick")],
                [_btn("⬅️ Назад", f"bpn:{sub['id']}")],
            ]))


def av_q2(sub) -> tuple[str, InlineKeyboardMarkup]:
    return (f"🇷🇺 Авито · <b>{html.escape(sub['brand'])}</b>\n\n<b>Вопрос 2/3.</b> Какая категория?",
            InlineKeyboardMarkup(inline_keyboard=[
                [_btn("📦 Все категории", f"avk:{sub['id']}:all")],
                [_btn("🎯 Что-то конкретное", f"avk:{sub['id']}:pick")],
                [_btn("⬅️ Назад", f"avs:{sub['id']}")],
            ]))


def av_q3(sub) -> tuple[str, InlineKeyboardMarkup]:
    return (f"🇷🇺 Авито · <b>{html.escape(sub['brand'])}</b>\n\n<b>Вопрос 3/3.</b> До какой цены?",
            InlineKeyboardMarkup(inline_keyboard=[
                [_btn("♾ Без лимита", f"avl:{sub['id']}:none")],
                [_btn("💰 Определённый лимит", f"avl:{sub['id']}:set")],
                [_btn("⬅️ Назад", f"avk:{sub['id']}:back")],
            ]))


avd: dict[int, dict] = {}  # черновик настроек Авито: {"sub": id, "region": ..., "cat": ...}


def _av_sub(c_or_m, sid: str):
    return db.get_sub(c_or_m.from_user.id, int(sid))


async def av_finish(uid: int, sub, max_rub, send_fn):
    d = avd.pop(uid, {})
    db.set_avito(sub["id"], d.get("region"), d.get("cat"), max_rub)
    av = db.get_avito(sub["id"])
    await send_fn(av_summary(sub, av) + "\n\n✅ Сохранено. Пришлю только то, что появится после этого.",
                  InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ К настройкам бренда", f"bpn:{sub['id']}")]]))


@router.callback_query(F.data.startswith("bpn:"))
async def av_back_panel(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    pending.pop(c.from_user.id, None)
    sub = _av_sub(c, c.data.split(":")[1])
    if not sub:
        return await c.answer("Бренд уже удалён")
    await c.message.edit_text(brand_text(sub), reply_markup=brand_kb(sub["id"]))
    await c.answer()


@router.callback_query(F.data.startswith("av:"))
async def av_open(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    sub = _av_sub(c, c.data.split(":")[1])
    if not sub:
        return await c.answer("Бренд уже удалён")
    av = db.get_avito(sub["id"])
    if av:
        await c.message.edit_text(av_summary(sub, av), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("✏️ Настроить заново", f"avs:{sub['id']}")],
            [_btn("🚫 Выключить Авито для бренда", f"avo:{sub['id']}")],
            [_btn("⬅️ Назад", f"bpn:{sub['id']}")],
        ]))
    else:
        avd[c.from_user.id] = {"sub": sub["id"], "region": None, "cat": None}
        text, kb = av_q1(sub)
        await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("avs:"))
async def av_restart(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    sub = _av_sub(c, c.data.split(":")[1])
    if not sub:
        return await c.answer("Бренд уже удалён")
    pending.pop(c.from_user.id, None)
    avd[c.from_user.id] = {"sub": sub["id"], "region": None, "cat": None}
    text, kb = av_q1(sub)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("avo:"))
async def av_off(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    sub = _av_sub(c, c.data.split(":")[1])
    if not sub:
        return await c.answer("Бренд уже удалён")
    db.del_avito(sub["id"])
    await c.message.edit_text(brand_text(sub), reply_markup=brand_kb(sub["id"]))
    await c.answer("Авито выключен для бренда")


def _draft(uid: int, sub) -> dict:
    d = avd.get(uid)
    if not d or d.get("sub") != sub["id"]:
        d = avd[uid] = {"sub": sub["id"], "region": None, "cat": None}
    return d


@router.callback_query(F.data.startswith("avr:"))
async def av_region(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    _, sid, val = c.data.split(":", 2)
    sub = _av_sub(c, sid)
    if not sub:
        return await c.answer("Бренд уже удалён")
    d = _draft(c.from_user.id, sub)
    pending.pop(c.from_user.id, None)
    if val == "pick":
        rows, row = [], []
        for slug in avito.TOP_REGIONS:
            row.append(_btn(avito.REGIONS[slug], f"avr:{sid}:{slug}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([_btn("✍️ Другой город (напишу)", f"avr:{sid}:type")])
        rows.append([_btn("⬅️ Назад", f"avs:{sid}")])
        await c.message.edit_text(
            f"🇷🇺 Авито · <b>{html.escape(sub['brand'])}</b>\n\n<b>Вопрос 1/3.</b> Какой регион?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return await c.answer()
    if val == "type":
        pending[c.from_user.id] = f"avcity:{sid}"
        await c.message.edit_text(
            "Напиши название города или области, например: <b>Казань</b> или <b>Московская область</b>.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", f"avr:{sid}:pick")]]))
        return await c.answer()
    d["region"] = None if val == "all" else val
    text, kb = av_q2(sub)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("avk:"))
async def av_category(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    _, sid, val = c.data.split(":", 2)
    sub = _av_sub(c, sid)
    if not sub:
        return await c.answer("Бренд уже удалён")
    d = _draft(c.from_user.id, sub)
    if val == "back":
        text, kb = av_q2(sub)
    elif val == "pick":
        rows = [[_btn(name, f"avk:{sid}:{slug}")] for slug, name in avito.CATEGORIES.items()]
        rows.append([_btn("⬅️ Назад", f"avk:{sid}:back")])
        text = (f"🇷🇺 Авито · <b>{html.escape(sub['brand'])}</b>\n\n"
                "<b>Вопрос 2/3.</b> Что именно ищем?")
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    else:
        d["cat"] = None if val == "all" else (val if val in avito.CATEGORIES else None)
        text, kb = av_q3(sub)
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("avl:"))
async def av_limit(c: CallbackQuery):
    if not has_access(c.from_user.id):
        return await c.answer()
    _, sid, val = c.data.split(":", 2)
    sub = _av_sub(c, sid)
    if not sub:
        return await c.answer("Бренд уже удалён")
    _draft(c.from_user.id, sub)
    if val == "set":
        pending[c.from_user.id] = f"avlim:{sid}"
        await c.message.edit_text(
            "Напиши максимальную цену в рублях, например: <b>5000</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Назад", f"avk:{sid}:back")]]))
        return await c.answer()
    pending.pop(c.from_user.id, None)

    async def out(text, kb):
        await c.message.edit_text(text, reply_markup=kb)

    await av_finish(c.from_user.id, sub, None, out)
    await c.answer("Сохранено")


async def av_text_input(m: Message, what: str):
    """Ответ текстом на вопросы Авито: город или сумма."""
    uid = m.from_user.id
    kind, sid = what.split(":", 1)
    sub = db.get_sub(uid, int(sid))
    if not sub:
        pending.pop(uid, None)
        return await m.answer("Бренд уже удалён.")
    d = _draft(uid, sub)
    if kind == "avcity":
        slug = avito.region_by_text(m.text)
        if not slug:
            return await m.answer("Такой город не нашёл. Напиши по-другому (например «Казань») или выбери из списка кнопкой «Назад».")
        pending.pop(uid, None)
        d["region"] = slug
        text, kb = av_q2(sub)
        return await m.answer(f"Регион: {avito.region_name(slug)} ✅\n\n" + text, reply_markup=kb)
    t = m.text.strip().lower()
    amount = parse_amount(t)
    if amount is None:
        return await m.answer("Не понял. Напиши число, например 5000.")
    pending.pop(uid, None)

    async def out(text, kb):
        await m.answer(text, reply_markup=kb)

    await av_finish(uid, sub, amount, out)


# ---------- любой текст = новый бренд

def _cmd(name: str) -> CommandObject:
    return CommandObject(prefix="/", command=name, mention=None, args=None)


@router.message(F.text.in_({BTN_FILTER, BTN_BRANDS, BTN_LIMIT, BTN_WHERE, BTN_PAUSE, BTN_RESUME, BTN_TEST, BTN_HELP}))
async def menu_buttons(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    pending.pop(m.from_user.id, None)  # нажали кнопку меню: отменяем ввод суммы
    t = m.text
    if t == BTN_FILTER:
        return await m.answer("Напиши название бренда (например <b>Nike</b> или <b>Stone Island</b>). "
                              "Потом я спрошу максимальную цену и на каких площадках искать.")
    if t == BTN_BRANDS:
        return await list_subs(m)
    if t == BTN_LIMIT:
        return await limit(m, _cmd("limit"))
    if t == BTN_WHERE:
        return await countries(m)
    if t == BTN_PAUSE:
        return await stop(m)
    if t == BTN_RESUME:
        return await resume(m)
    if t == BTN_TEST:
        return await test_sources(m)
    return await start(m, _cmd("help"))


@router.message(F.text & ~F.text.startswith("/"))
async def add_brand(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    uid = m.from_user.id
    if uid in pending and pending[uid].startswith(("avcity:", "avlim:")):
        return await av_text_input(m, pending[uid])
    if uid in pending:
        what = pending[uid]
        t = m.text.strip().lower()
        if not what.startswith("new:") or t in ("off", "выкл", "нет", "0") or parse_amount(t) is not None:
            return await apply_limit(m, what)
        pending.pop(uid, None)  # ждали сумму, а пришёл другой бренд: добавляем его
    brand = " ".join(m.text.lower().split())[:50]
    if db.add_sub(uid, brand, setup=True):
        sub = next(s for s in db.user_subs(uid) if s["brand"] == brand)
        pending[uid] = f"new:{sub['id']}"
        await m.answer(f"Добавил <b>{html.escape(brand)}</b> ✅\n\n" + step1_text(brand).split("\n\n", 1)[1],
                       reply_markup=step1_kb(sub["id"]))
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
    try:  # кнопка «Меню» слева от поля ввода: список команд
        await bot.set_my_commands([
            BotCommand(command="menu", description="Показать кнопки"),
            BotCommand(command="list", description="Мои бренды"),
            BotCommand(command="limit", description="Лимит цены"),
            BotCommand(command="countries", description="Страны и площадки"),
            BotCommand(command="stop", description="Пауза"),
            BotCommand(command="resume", description="Включить"),
            BotCommand(command="help", description="Помощь"),
        ])
    except Exception:
        pass
    asyncio.create_task(rates.loop())
    asyncio.create_task(run_poller(db, bot, rates, sources))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
