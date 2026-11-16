"""Авито: поиск объявлений и опрос по настройкам каждого бренда (регион, категория, цена)."""
import asyncio
import html as htmllib
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote

import config
from poller import MAX_AGE, MAX_PER_TERM, format_caption, send
from sources import Item

log = logging.getLogger("avito")
MSK = timezone(timedelta(hours=3))
PROXY = os.environ.get("AVITO_PROXY_URL") or config.PROXY_URL
INTERVAL = 90  # секунд между кругами опроса (Авито быстро блокирует частые запросы)
SOURCE = SimpleNamespace(code="avito", label="Авито", flag="🇷🇺")

# slug региона в адресе avito.ru -> название
REGIONS = {
    "moskva": "Москва", "sankt-peterburg": "Санкт-Петербург", "novosibirsk": "Новосибирск",
    "ekaterinburg": "Екатеринбург", "kazan": "Казань", "nizhniy_novgorod": "Нижний Новгород",
    "chelyabinsk": "Челябинск", "samara": "Самара", "omsk": "Омск", "rostov-na-donu": "Ростов-на-Дону",
    "ufa": "Уфа", "krasnoyarsk": "Красноярск", "perm": "Пермь", "voronezh": "Воронеж",
    "volgograd": "Волгоград", "krasnodar": "Краснодар", "saratov": "Саратов", "tyumen": "Тюмень",
    "tolyatti": "Тольятти", "izhevsk": "Ижевск", "barnaul": "Барнаул", "irkutsk": "Иркутск",
    "ulyanovsk": "Ульяновск", "habarovsk": "Хабаровск", "vladivostok": "Владивосток",
    "yaroslavl": "Ярославль", "mahachkala": "Махачкала", "tomsk": "Томск", "orenburg": "Оренбург",
    "kemerovo": "Кемерово", "novokuznetsk": "Новокузнецк", "ryazan": "Рязань", "astrahan": "Астрахань",
    "penza": "Пенза", "lipetsk": "Липецк", "kirov": "Киров", "tula": "Тула", "kaliningrad": "Калининград",
    "kursk": "Курск", "sochi": "Сочи", "stavropol": "Ставрополь", "tver": "Тверь", "ivanovo": "Иваново",
    "belgorod": "Белгород", "arhangelsk": "Архангельск", "vladimir": "Владимир", "surgut": "Сургут",
    "cheboksary": "Чебоксары", "sevastopol": "Севастополь", "simferopol": "Симферополь",
    "moskovskaya_oblast": "Московская область", "leningradskaya_oblast": "Ленинградская область",
}
# кнопки быстрого выбора
TOP_REGIONS = ["moskva", "sankt-peterburg", "ekaterinburg", "novosibirsk", "kazan", "krasnodar",
               "nizhniy_novgorod", "samara", "rostov-na-donu", "ufa", "chelyabinsk", "perm"]

# slug категории -> название
CATEGORIES = {
    "odezhda_obuv_aksessuary": "👕 Одежда, обувь, аксессуары",
    "chasy_i_ukrasheniya": "⌚ Часы и украшения",
    "telefony": "📱 Телефоны",
    "bytovaya_elektronika": "💻 Электроника",
    "detskaya_odezhda_i_obuv": "🧸 Детская одежда и обувь",
    "hobbi_i_otdyh": "🎸 Хобби и отдых",
    "dlya_doma_i_dachi": "🏠 Для дома и дачи",
    "krasota_i_zdorove": "💄 Красота и здоровье",
}


def region_by_text(text: str) -> str | None:
    t = text.strip().lower().replace("ё", "е")
    for slug, name in REGIONS.items():
        if t == name.lower().replace("ё", "е") or t == slug:
            return slug
    for slug, name in REGIONS.items():  # частичное совпадение, например «питер»
        if len(t) >= 4 and t in name.lower().replace("ё", "е"):
            return slug
    aliases = {"питер": "sankt-peterburg", "спб": "sankt-peterburg", "мск": "moskva", "екб": "ekaterinburg",
               "нск": "novosibirsk", "нижний": "nizhniy_novgorod", "ростов": "rostov-na-donu"}
    return aliases.get(t)


def region_name(slug: str | None) -> str:
    return "вся Россия" if not slug or slug == "rossiya" else REGIONS.get(slug, slug)


def cat_name(slug: str | None) -> str:
    return "все категории" if not slug else CATEGORIES.get(slug, slug)


def search_url(term: str, region: str | None, cat: str | None) -> str:
    path = region or "rossiya"
    if cat:
        path += "/" + cat
    return f"https://www.avito.ru/{path}?q={quote(term)}&s=104"  # s=104: сначала новые


def _rel_time(text: str) -> float | None:
    """«5 минут назад», «Вчера, 12:30», «2 дня назад» -> unix-время."""
    t = text.lower().replace("\xa0", " ").strip()
    now = datetime.now(MSK)
    m = re.search(r"(\d+)\s*(минут|мин)", t)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).timestamp()
    if re.search(r"минуту назад", t):
        return (now - timedelta(minutes=1)).timestamp()
    m = re.search(r"(\d+)\s*час", t)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).timestamp()
    if re.search(r"час назад", t):
        return (now - timedelta(hours=1)).timestamp()
    m = re.search(r"(\d+)\s*(дн|день|дня)", t)
    if m:
        return (now - timedelta(days=int(m.group(1)))).timestamp()
    m = re.search(r"(сегодня|вчера)[, ]+(\d{1,2}):(\d{2})", t)
    if m:
        d = now - timedelta(days=1 if m.group(1) == "вчера" else 0)
        return d.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0).timestamp()
    return None


def parse(page: str) -> list[Item]:
    items, seen = [], set()
    starts = [m.start() for m in re.finditer(r'data-marker="item"', page)]
    for i, pos in enumerate(starts):
        chunk = page[max(0, page.rfind("<", 0, pos)): starts[i + 1] if i + 1 < len(starts) else pos + 6000]
        chunk = chunk[:7000]
        idm = re.search(r'data-item-id="(\d+)"', chunk) or re.search(r'_(\d{6,})(?:\?|")', chunk)
        hm = re.search(r'href="(/[^"?#]+_\d{6,})', chunk)
        pm = re.search(r'itemprop="price"[^>]*content="(\d+)"', chunk) or re.search(r'content="(\d+)"[^>]*itemprop="price"', chunk)
        if not (idm and hm and pm):
            continue
        iid = idm.group(1)
        if iid in seen:
            continue
        seen.add(iid)
        tm = (re.search(r'data-marker="item-title"[^>]*title="([^"]+)"', chunk)
              or re.search(r'itemprop="name"[^>]*>([^<]+)<', chunk)
              or re.search(r"<h3[^>]*>([^<]+)</h3>", chunk))
        title = htmllib.unescape(tm.group(1)).strip() if tm else "Объявление Авито"
        photo = None
        for im in re.finditer(r"<img\b[^>]*>", chunk):
            sm = re.search(r'(?:srcset|src)="([^"\s,]+)', im.group(0))
            if sm and sm.group(1).startswith("http") and ".svg" not in sm.group(1):
                photo = htmllib.unescape(sm.group(1))
                break
        dm = re.search(r'data-marker="item-date"[^>]*>(.*?)</', chunk, re.S)
        created = _rel_time(re.sub(r"<[^>]+>", "", dm.group(1))) if dm else None
        items.append(Item("avito", iid, title[:200], float(pm.group(1)), "RUB",
                          "https://www.avito.ru" + hm.group(1), photo, created))
    return items


_session = None


def _get_session():
    global _session
    if _session is None:
        from curl_cffi.requests import AsyncSession

        kw = {"impersonate": "chrome", "timeout": 25}
        if PROXY:
            kw["proxies"] = {"http": PROXY, "https": PROXY}
        _session = AsyncSession(**kw)
    return _session


async def fetch(term: str, region: str | None, cat: str | None):
    return await _get_session().get(search_url(term, region, cat), headers={"Accept-Language": "ru-RU,ru;q=0.9"})


def blocked(status: int, text: str) -> bool:
    low = text[:3000].lower()
    return status in (403, 429) or "доступ ограничен" in low or "firewall" in low or "captcha" in low


async def search(term: str, region: str | None = None, cat: str | None = None) -> list[Item]:
    r = await fetch(term, region, cat)
    if blocked(r.status_code, r.text):
        raise RuntimeError(f"Авито не пускает (HTTP {r.status_code}): нужен прокси с российским адресом")
    r.raise_for_status()
    return parse(r.text)


async def diag(term: str = "nike") -> str:
    r = await fetch(term, None, None)
    t = r.text
    items = parse(t)
    lines = [f"HTTP {r.status_code}, {len(t)} симв., блок: {'ДА' if blocked(r.status_code, t) else 'нет'}, "
             f"разобрано: {len(items)}"]
    for it in items[:3]:
        lines.append(f"• {it.title[:60]} | {it.price:,.0f} ₽ | фото: {'есть' if it.photo else 'нет'} | время: "
                     f"{'есть' if it.created else 'нет'} | {it.url}")
    if not items:
        lines.append("Начало страницы: " + " ".join(re.sub(r"<[^>]+>", " ", t[:1500]).split())[:400])
    return "\n".join(lines)


async def cycle(db, bot, rates):
    rows = db.avito_active()
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["brand"], r["region"], r["cat"]), []).append(r)
    for (brand, region, cat), subs in groups.items():
        key = f"{brand}|{region or ''}|{cat or ''}"
        try:
            items = await search(brand, region, cat)
        except Exception as e:
            log.warning("avito '%s': %s", brand, e)
            await asyncio.sleep(random.uniform(3, 6))
            continue
        src = f"avito:{key}"
        first = not db.is_baselined("avito", key)
        new = [i for i in items if not db.is_seen(src, i.id)]
        for i in new:
            db.mark_seen(src, i.id)
        if first:
            db.set_baselined("avito", key)
            for i in new:
                for s in subs:
                    db.claim_sent(s["user_id"], f"avito:{i.id}")
            await asyncio.sleep(random.uniform(3, 6))
            continue
        fresh = [i for i in new if not i.created or time.time() - i.created < MAX_AGE]
        for i in list(reversed(fresh))[-MAX_PER_TERM:]:
            caption = format_caption(i, SOURCE, brand, rates)
            for s in subs:
                if s["max_rub"] and i.price > s["max_rub"]:
                    continue
                if db.claim_sent(s["user_id"], f"avito:{i.id}"):
                    await send(bot, s["user_id"], i, caption)
        await asyncio.sleep(random.uniform(3, 6))


async def avito_loop(db, bot, rates, end: float | None = None):
    await asyncio.sleep(random.uniform(0, 10))
    while True:
        try:
            await cycle(db, bot, rates)
        except Exception as e:
            log.warning("avito: ошибка %r", e)
        if end is not None and time.monotonic() >= end:
            break
        await asyncio.sleep(INTERVAL + random.uniform(0, 5))
