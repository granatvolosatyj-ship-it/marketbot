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

_cache: dict[tuple[str, str], str | None] = {}


async def _google(text: str, lang: str) -> str | None:
    key = (text, lang)
    if key in _cache:
        return _cache[key]
    result = None
    try:
        async with httpx.AsyncClient(timeout=10, proxy=config.PROXY_URL) as c:
            r = await c.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "en", "tl": lang, "dt": "t", "q": text},
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
    if b in BRANDS:
        return [BRANDS[b][0 if lang == "ja" else 1]]
    t = await _google(brand, lang)
    return [t] if t else []
