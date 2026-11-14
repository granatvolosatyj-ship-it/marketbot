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
    "Напиши название бренда (например <b>Nike</b>). Я спрошу максимальную цену и на каких площадках "
    "искать, и начну присылать новые объявления с этого момента.\n\n"
    "/list: мои бренды: выбрать, на каких площадках искать каждый бренд, или удалить\n"
    "/countries: включить или выключить площадки сразу для всех брендов\n"
    "/limit: поставить лимит цены (бот спросит сумму в рублях)\n"
    "/stop: поставить уведомления на паузу\n"
    "/resume: снова включить\n"
    "/help: эта подсказка"
)
ADMIN_HELP = "\n\nАдмин:\n/allow ID: дать доступ\n/deny ID: забрать доступ\n/users: список"


def has_access(uid: int) -> bool:
    return uid == config.ADMIN_ID or db.is_allowed(uid)


async def deny(m: Message):
    db.ensure_user(m.from_user.id, m.from_user.full_name or "")
    await m.answer(f"Нет доступа. Твой ID: <code>{m.from_user.id}</code>. Отправь его админу.")


@router.message(Command("start", "help"))
async def start(m: Message, command: CommandObject):
    if not has_access(m.from_user.id):
        return await deny(m)
    if command.command == "start" and db.is_paused(m.from_user.id):
        db.set_paused(m.from_user.id, False)
        await m.answer("▶️ Уведомления снова включены.")
    await m.answer(HELP + (ADMIN_HELP if m.from_user.id == config.ADMIN_ID else ""))


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
        "Чтобы убрать лимит, напиши <b>off</b>. Отмена: /cancel"
    )


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
    lim = f"до {sub['max_rub']:,.0f} ₽".replace(",", " ") if sub["max_rub"] else "без лимита цены"
    await c.message.edit_text(
        f"Готово ✅ Ищу <b>{html.escape(sub['brand'])}</b>: {lim}, площадки: {where_text(db.brand_disabled(sub['id']))}.\n"
        "Пришлю только то, что появится с этого момента. Изменить: /list"
    )
    await c.answer("Готово")


@router.message(Command("cancel"))
async def cancel(m: Message):
    was = pending.pop(m.from_user.id, None)
    await m.answer(
        "Отменено. Бренд остался без лимита цены и на всех площадках (изменить: /list)."
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
    await m.answer(
        f"Лимит для {who} снят ✅" if off else f"Для {who}: не дороже {amount:,.0f} ₽ ✅".replace(",", " ")
    )


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
        text = f"⚙️ {s['brand']}{lim} · {where_text(db.brand_disabled(s['id']))}"
        rows.append(
            [
                InlineKeyboardButton(text=text[:60], callback_data=f"bs:{s['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"del:{s['id']}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


LIST_TEXT = "Твои бренды. ⚙️ выбрать, где искать (страны и площадки), 🗑 удалить:"


def brand_kb(sub_id: int, done: bool = False) -> InlineKeyboardMarkup:
    off = db.brand_disabled(sub_id)
    rows, row = [], []
    for s in ready:
        mark = "❌" if s.code in off else "✅"
        row.append(InlineKeyboardButton(text=f"{mark} {s.flag} {s.label}", callback_data=f"bt:{sub_id}:{s.code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="🌍 Везде", callback_data=f"bp:{sub_id}:all"),
            InlineKeyboardButton(text="🇪🇺 Только Vinted", callback_data=f"bp:{sub_id}:eu"),
            InlineKeyboardButton(text="🇯🇵🇰🇷 Только Азия", callback_data=f"bp:{sub_id}:asia"),
        ]
    )
    if done:
        rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"nd:{sub_id}")])
    else:
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
            code = f"HTTP {resp.status_code}" if resp is not None else (str(e)[:90] or type(e).__name__)
            line = f"❌ {s.label}: {code}"
            if resp is not None and s.code in ("vinted_fr", "bunjang", "2ndstreet_jp"):
                body = " ".join(resp.text.split())[:160]
                server = resp.headers.get("server", "?")
                line += f"\n   сервер: {html.escape(server)}\n   ответ: {html.escape(body)}"
            return line

    lines = await asyncio.gather(*(check(s) for s in ready))
    await wait.edit_text("Результат проверки (запрос «nike»):\n" + "\n".join(lines))


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
    for s in ready:
        if key and key in s.code and hasattr(s, "diag"):
            await m.answer("Смотрю, подожди до 40 секунд...")
            try:
                text = await asyncio.wait_for(s.diag("nike"), timeout=40)
            except Exception as e:
                text = f"Ошибка: {e!r}"
            return await m.answer(html.escape(text)[:3800])
    await m.answer("Формат: /peek bunjang, /peek fruits, /peek vinted или /peek 2nd")


# ---------- любой текст = новый бренд
@router.message(F.text & ~F.text.startswith("/"))
async def add_brand(m: Message):
    if not has_access(m.from_user.id):
        return await deny(m)
    uid = m.from_user.id
    if uid in pending:
        what = pending[uid]
        t = m.text.strip().lower()
        if not what.startswith("new:") or t in ("off", "выкл", "нет", "0") or parse_amount(t) is not None:
            return await apply_limit(m, what)
        pending.pop(uid, None)  # ждали сумму, а пришёл другой бренд: добавляем его
    brand = " ".join(m.text.lower().split())[:50]
    if db.add_sub(uid, brand):
        sub = next(s for s in db.user_subs(uid) if s["brand"] == brand)
        pending[uid] = f"new:{sub['id']}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="♾ Без лимита", callback_data=f"nl:{sub['id']}")]]
        )
        await m.answer(
            f"Добавил <b>{html.escape(brand)}</b> ✅\n\n"
            "Шаг 1 из 2. До какой цены искать? Напиши максимальную цену в рублях (например <b>5000</b>) "
            "или нажми «Без лимита».",
            reply_markup=kb,
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
