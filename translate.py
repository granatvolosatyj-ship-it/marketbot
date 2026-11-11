"""Названия брендов на японском и корейском. Если бренда нет в словаре,
пробуем бесплатный переводчик Google; если не вышло, ищем только латиницей."""
import logging

import httpx

import config

log = logging.getLogger("translate")

BRANDS = {
    "nike": ("ナイキ", "나이키"),
    "adidas": ("アディダス", "아디다스"),
    "supreme": ("シュプリーム", "슈프림"),
    "stone island": ("ストーンアイランド", "스톤아일랜드"),
    "comme des garcons": ("コムデギャルソン", "꼼데가르숑"),
    "yohji yamamoto": ("ヨウジヤマモト", "요지야마모토"),
    "issey miyake": ("イッセイミヤケ", "이세이미야케"),
    "undercover": ("アンダーカバー", "언더커버"),
    "bape": ("ベイプ", "베이프"),
    "the north face": ("ノースフェイス", "노스페이스"),
    "patagonia": ("パタゴニア", "파타고니아"),
    "carhartt": ("カーハート", "칼하트"),
    "ralph lauren": ("ラルフローレン", "랄프로렌"),
    "gucci": ("グッチ", "구찌"),
    "prada": ("プラダ", "프라다"),
    "louis vuitton": ("ルイヴィトン", "루이비통"),
    "chanel": ("シャネル", "샤넬"),
    "balenciaga": ("バレンシアガ", "발렌시아가"),
    "dior": ("ディオール", "디올"),
    "burberry": ("バーバリー", "버버리"),
    "maison margiela": ("メゾンマルジェラ", "메종마르지엘라"),
    "rick owens": ("リックオウエンス", "릭오웬스"),
    "stussy": ("ステューシー", "스투시"),
    "new balance": ("ニューバランス", "뉴발란스"),
    "converse": ("コンバース", "컨버스"),
    "vans": ("バンズ", "반스"),
    "asics": ("アシックス", "아식스"),
    "levis": ("リーバイス", "리바이스"),
    "diesel": ("ディーゼル", "디젤"),
    "moncler": ("モンクレール", "몽클레어"),
    "fear of god": ("フィアオブゴッド", "피어오브갓"),
    "salomon": ("サロモン", "살로몬"),
    "arcteryx": ("アークテリクス", "아크테릭스"),
    "celine": ("セリーヌ", "셀린느"),
    "hermes": ("エルメス", "에르메스"),
}

RU = {'nike': 'найк', 'adidas': 'адидас', 'supreme': 'суприм', 'stone island': 'стоун айленд', 'comme des garcons': 'комм де гарсон', 'yohji yamamoto': 'йоджи ямамото', 'issey miyake': 'иссей мияке', 'undercover': 'андеркавер', 'bape': 'бейп', 'the north face': 'норт фейс', 'patagonia': 'патагония', 'carhartt': 'кархартт', 'ralph lauren': 'ральф лорен', 'gucci': 'гуччи', 'prada': 'прада', 'louis vuitton': 'луи виттон', 'chanel': 'шанель', 'balenciaga': 'баленсиага', 'dior': 'диор', 'burberry': 'барберри', 'maison margiela': 'мезон марджела', 'rick owens': 'рик оуэнс', 'stussy': 'стюси', 'new balance': 'нью баланс', 'converse': 'конверс', 'vans': 'вэнс', 'asics': 'асикс', 'levis': 'левис', 'diesel': 'дизель', 'moncler': 'монклер', 'fear of god': 'фир оф гад', 'salomon': 'саломон', 'arcteryx': 'арктерикс', 'celine': 'селин', 'hermes': 'эрмес'}

# обратный словарь: любое написание (ru / ja / ko) -> латиница
REVERSE = {}
for _lat, (_ja, _ko) in BRANDS.items():
    for _name in (_ja, _ko, RU.get(_lat)):
        if _name:
            REVERSE[_name.lower()] = _lat

_cache: dict[tuple[str, str, str], str | None] = {}


async def _google(text: str, lang: str, sl: str = "en") -> str | None:
    key = (text, lang, sl)
    if key in _cache:
        return _cache[key]
    result = None
    try:
        async with httpx.AsyncClient(timeout=10, proxy=config.PROXY_URL) as c:
            r = await c.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": sl, "tl": lang, "dt": "t", "q": text},
            )
            result = r.json()[0][0][0]
            if result.lower() == text.lower():
                result = None
    except Exception as e:
        log.warning("Перевод '%s' не удался: %s", text, e)
    _cache[key] = result
    return result


async def variants(brand: str, lang: str) -> list[str]:
    """Дополнительные варианты запроса на местном языке (lang: 'ja' или 'ko')."""
    b = brand.lower().strip()
    if b in BRANDS and lang in ("ja", "ko"):
        return [BRANDS[b][0 if lang == "ja" else 1]]
    if b in RU and lang == "ru":
        return [RU[b]]
    t = await _google(brand, lang)
    return [t] if t else []


def _is_latin(text: str) -> bool:
    return all(ord(ch) < 0x250 for ch in text)


async def latin(brand: str) -> str:
    """Бренд в латинице: «найк», «ナイキ», «나이키» -> «nike»."""
    b = brand.lower().strip()
    if _is_latin(b):
        return b
    if b in REVERSE:
        return REVERSE[b]
    t = await _google(b, "en", sl="auto")
    return t.lower() if t else b


async def queries(brand: str, lang: str | None) -> list[str]:
    """Все варианты запроса для площадки: латиница + язык площадки (ja / ko / zh).
    Можно вводить бренд на любом языке."""
    lat = await latin(brand)
    out = [lat]
    if brand.lower().strip() != lat and not _is_latin(brand):
        out.append(brand.lower().strip())  # как написал сам человек
    if lang:
        out += await variants(lat, lang)
    seen, res = set(), []
    for q in out:
        if q and q.lower() not in seen:
            seen.add(q.lower())
            res.append(q)
    return res
