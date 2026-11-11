"""Площадки. Каждая отдаёт список новых объявлений (сначала самые свежие)."""
import base64
import json
import time
import uuid
from dataclasses import dataclass

import httpx

import config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass
class Item:
    source: str
    id: str
    title: str
    price: float
    currency: str
    url: str
    photo: str | None = None


class Source:
    code = ""
    label = ""
    flag = ""
    currency = ""
    lang: str | None = None  # 'ja' / 'ko' для поиска на местном языке
    ready = True
    impersonate = False  # True: запросы с «отпечатком» настоящего Chrome (curl_cffi)

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": UA},
            proxy=config.PROXY_URL,
        )
        self.cffi = None
        if self.impersonate:
            try:
                from curl_cffi.requests import AsyncSession

                kw = {"impersonate": "chrome", "timeout": 25}
                if config.PROXY_URL:
                    kw["proxies"] = {"http": config.PROXY_URL, "https": config.PROXY_URL}
                self.cffi = AsyncSession(**kw)
            except Exception:  # нет библиотеки: работаем обычным клиентом
                self.cffi = None

    async def get(self, url: str, **kw):
        return await (self.cffi or self.client).get(url, **kw)

    async def search(self, term: str) -> list[Item]:
        raise NotImplementedError


# ---------------------------------------------------------------- Vinted
class Vinted(Source):
    impersonate = True

    async def diag(self, term: str) -> str:
        """Диагностика для команды /peek: что отвечает сайт."""
        out = []
        sess = self.cffi or self.client
        r = await self.get(f"https://www.vinted.{self.tld}/", headers={"Accept": "text/html"})
        try:
            names = ", ".join(k for k, _ in sess.cookies.items())
        except Exception:
            names = "?"
        out.append(f"Главная: HTTP {r.status_code}, cookie: {names or 'нет'}")
        api = f"https://www.vinted.{self.tld}/api/v2/catalog/items"
        r2 = await self.get(api, params={"search_text": term, "order": "newest_first", "per_page": 5},
                            headers={"Accept": "application/json"})
        out.append(f"API items: HTTP {r2.status_code}: {' '.join(r2.text.split())[:150]}")
        r3 = await self.get(f"https://www.vinted.{self.tld}/catalog",
                            params={"search_text": term, "order": "newest_first"},
                            headers={"Accept": "text/html"})
        t = r3.text
        i = t.find("/items/")
        out.append(f"Страница поиска: HTTP {r3.status_code}, {len(t)} симв., ссылок /items/: {t.count('/items/')}")
        if i >= 0:
            out.append("Фрагмент: " + " ".join(t[max(0, i - 150): i + 350].split()))
        return "\n".join(out)

    def __init__(self, tld: str, currency: str, flag: str):
        super().__init__()
        self.tld = tld
        self.code = f"vinted_{tld}"
        self.label = f"Vinted {tld.upper()}"
        self.flag = flag
        self.currency = currency
        self._has_cookie = False

    async def _refresh(self):
        (self.cffi or self.client).cookies.clear()
        r = await self.get(
            f"https://www.vinted.{self.tld}/",
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        r.raise_for_status()  # если главная страница закрыта, ошибка будет видна в /test
        self._has_cookie = True

    async def search(self, term: str) -> list[Item]:
        if not self._has_cookie:
            await self._refresh()
        url = f"https://www.vinted.{self.tld}/api/v2/catalog/items"
        params = {"search_text": term, "order": "newest_first", "per_page": 30, "page": 1}
        headers = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
        for attempt in (1, 2):
            r = await self.get(url, params=params, headers=headers)
            if r.status_code in (401, 403) and attempt == 1:
                await self._refresh()
                continue
            r.raise_for_status()
            break
        out = []
        for it in r.json().get("items", []):
            p = it.get("price")
            if isinstance(p, dict):
                amount, cur = float(p.get("amount", 0)), p.get("currency_code", self.currency)
            else:
                amount, cur = float(p or 0), self.currency
            title = it.get("title", "")
            extra = " · ".join(x for x in (it.get("brand_title"), it.get("size_title")) if x)
            if extra:
                title = f"{title} ({extra})"
            out.append(
                Item(
                    self.code,
                    str(it["id"]),
                    title,
                    amount,
                    cur,
                    it.get("url") or f"https://www.vinted.{self.tld}/items/{it['id']}",
                    (it.get("photo") or {}).get("url"),
                )
            )
        return out


# ---------------------------------------------------------------- Mercari JP
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class MercariJP(Source):
    code = "mercari_jp"
    label = "Mercari JP"
    flag = "🇯🇵"
    currency = "JPY"
    lang = "ja"
    API = "https://api.mercari.jp/v2/entities:search"

    def __init__(self):
        super().__init__()
        from cryptography.hazmat.primitives.asymmetric import ec

        self._key = ec.generate_private_key(ec.SECP256R1())
        n = self._key.public_key().public_numbers()
        self._jwk = {
            "crv": "P-256",
            "kty": "EC",
            "x": _b64(n.x.to_bytes(32, "big")),
            "y": _b64(n.y.to_bytes(32, "big")),
        }

    def _dpop(self, method: str, url: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._jwk}
        payload = {"iat": int(time.time()), "jti": str(uuid.uuid4()), "htu": url, "htm": method, "uuid": str(uuid.uuid4())}
        signing = _b64(json.dumps(header).encode()) + "." + _b64(json.dumps(payload).encode())
        r, s = decode_dss_signature(self._key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        return signing + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    async def search(self, term: str) -> list[Item]:
        body = {
            "userId": "",
            "pageSize": 60,
            "pageToken": "",
            "searchSessionId": uuid.uuid4().hex,
            "indexRouting": "INDEX_ROUTING_UNSPECIFIED",
            "thumbnailTypes": [],
            "searchCondition": {
                "keyword": term,
                "excludeKeyword": "",
                "sort": "SORT_CREATED_TIME",
                "order": "ORDER_DESC",
                "status": ["STATUS_ON_SALE"],
                "sizeId": [],
                "categoryId": [],
                "brandId": [],
                "sellerId": [],
                "priceMin": 0,
                "priceMax": 0,
                "itemConditionId": [],
                "shippingPayerId": [],
                "shippingFromArea": [],
                "shippingMethod": [],
                "colorId": [],
                "hasCoupon": False,
                "attributes": [],
                "itemTypes": [],
                "skuIds": [],
            },
            "defaultDatasets": ["DATASET_TYPE_MERCARI", "DATASET_TYPE_BEYOND"],
            "serviceFrom": "suruga",
            "withItemBrand": True,
        }
        headers = {
            "DPoP": self._dpop("POST", self.API),
            "X-Platform": "web",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Accept-Language": "ja",
        }
        r = await self.client.post(self.API, content=json.dumps(body), headers=headers)
        r.raise_for_status()
        out = []
        for it in r.json().get("items", []):
            thumbs = it.get("thumbnails") or []
            out.append(
                Item(
                    self.code,
                    str(it["id"]),
                    it.get("name", ""),
                    float(it.get("price") or 0),
                    "JPY",
                    f"https://jp.mercari.com/item/{it['id']}",
                    thumbs[0] if thumbs else None,
                )
            )
        return out


# ---------------------------------------------------------------- Bunjang
class Bunjang(Source):
    code = "bunjang"
    label = "Bunjang"
    flag = "🇰🇷"
    currency = "KRW"
    lang = "ko"

    async def search(self, term: str) -> list[Item]:
        r = await self.client.get(
            "https://api.bunjang.co.kr/api/1/find_v2.json",
            params={
                "q": term,
                "order": "date",
                "page": 0,
                "request_id": str(int(time.time() * 1000)),
                "stat_device": "w",
                "n": 100,
                "stat_category_required": 1,
                "req_ref": "search",
                "version": 4,
            },
            headers={"Referer": "https://m.bunjang.co.kr/"},
        )
        r.raise_for_status()
        out = []
        for it in r.json().get("list", []):
            if it.get("ad") or not it.get("pid"):
                continue
            img = it.get("product_image") or ""
            img = img.replace("{cnt}", "1").replace("{res}", "300") or None
            out.append(
                Item(
                    self.code,
                    str(it["pid"]),
                    it.get("name", ""),
                    float(it.get("price") or 0),
                    "KRW",
                    f"https://m.bunjang.co.kr/products/{it['pid']}",
                    img,
                )
            )
        return out


# ---------------------------------------------------------------- 2nd STREET JP
class SecondStreet(Source):
    """ЭКСПЕРИМЕНТАЛЬНО: разбор HTML без знания точной вёрстки сайта (ищем ссылки
    на товары /goods/detail, рядом цену в иенах и картинку). Если сайт устроен
    иначе, в логах будет предупреждение «ничего не найдено»."""

    code = "2ndstreet_jp"
    label = "2nd STREET (тест)"
    impersonate = True

    async def fetch(self, term: str):
        """Загружает страницу поиска; если Akamai показал проверку «не бот»,
        решает её (как это делает JS в браузере) и загружает страницу заново."""
        import re

        url = "https://www.2ndstreet.jp/search"
        kw = dict(params={"keyword": term, "sortBy": "arrival"}, headers={"Accept-Language": "ja,en;q=0.8"})
        self.trace = []
        r = await self.get(url, **kw)
        self.trace.append(f"1) страница: HTTP {r.status_code}, {len(r.text)} симв.")
        for _ in range(3):
            t = r.text
            if "_sec/verify" not in t:
                break
            mi = re.search(r"var i = (\d+);", t)
            mn = re.search(r'Number\("(\d+)"\s*\+\s*"(\d+)"\)', t)
            mt = re.search(r'"bm-verify":\s*"([^"]+)"', t)
            if not (mi and mn and mt):
                break
            pow_ = int(mi.group(1)) + int(mn.group(1) + mn.group(2))
            cli = self.cffi or self.client
            vr = await cli.post(
                "https://www.2ndstreet.jp/_sec/verify?provider=interstitial",
                json={"bm-verify": mt.group(1), "pow": pow_},
                headers={"Content-Type": "application/json", "Referer": str(url)},
            )
            self.trace.append(f"2) проверка: HTTP {vr.status_code}, ответ: {vr.text[:200]!r}")
            try:
                data = vr.json()
            except Exception:
                data = {}
            if isinstance(data, dict) and data.get("location"):
                r = await self.get(data["location"])
            else:
                r = await self.get(url, **kw)
            self.trace.append(f"3) после проверки: HTTP {r.status_code}, {len(r.text)} симв.")
        if r.status_code >= 400:
            self.trace.append(f"тело: {' '.join(r.text.split())[:300]!r}")
        return r

    async def diag(self, term: str) -> str:
        """Диагностика для команды /peek: что реально отдаёт страница поиска."""
        import re

        r = await self.fetch(term)
        t = r.text
        m = re.search(r"/goods/", t)
        if m:
            frag = " ".join(t[max(0, m.start() - 300): m.start() + 900].split())
        else:
            srcs = re.findall(r'(?:src|href)="([^"]+)"', t)
            inline = re.findall(r"<script[^>]*>(.*?)</script>", t, re.S)
            title = re.search(r"<title>(.*?)</title>", t, re.S)
            frag = (
                f"title: {' '.join(title.group(1).split()) if title else '-'}\n"
                f"ссылки/скрипты: {' | '.join(srcs[:12]) or 'нет'}\n"
                f"встроенный код: {' '.join(' '.join(inline).split())[:600] or 'нет'}"
            )
        return (
            "\n".join(getattr(self, "trace", [])) + "\n"
            f"HTTP {r.status_code}, {len(t)} симв.\n"
            f"goodsId: {t.count('goodsId')}, /goods/: {t.count('/goods/')}, "
            f"¥: {t.count('¥')}, 円: {t.count('円')}\n"
            f"__NEXT_DATA__: {'__NEXT_DATA__' in t}, ld+json: {'ld+json' in t}\n"
            f"Фрагмент: {frag}"
        )
    flag = "🇯🇵"
    currency = "JPY"
    lang = "ja"

    async def search(self, term: str) -> list[Item]:
        import html as htmllib
        import logging
        import re

        r = await self.fetch(term)
        r.raise_for_status()
        page = r.text
        matches = [
            (m.start(), m.group(1), m.group(2))
            for m in re.finditer(r'href="([^"]*?/goods/detail[^"]*?goodsId[=/](\w+)[^"]*)"', page)
        ]
        out, done = [], set()
        for i, (pos, href, gid) in enumerate(matches):
            if gid in done:
                continue
            done.add(gid)
            j = i + 1
            while j < len(matches) and matches[j][2] == gid:
                j += 1
            end = matches[j][0] if j < len(matches) else pos + 4000
            chunk = page[pos : min(end, pos + 4000)]
            pm = re.search(r"¥\s*([\d,]+)|([\d,]+)\s*円", chunk)
            if not pm:
                continue
            price = float((pm.group(1) or pm.group(2)).replace(",", ""))
            am = re.search(r'alt="([^"]{3,})"', chunk)
            if am:
                title = htmllib.unescape(am.group(1)).strip()
            else:
                txt = " ".join(re.sub(r"<[^>]+>", " ", chunk).split())
                txt = re.sub(r"(¥\s*[\d,]+|[\d,]+\s*円).*", "", txt).strip()
                title = htmllib.unescape(txt)[:150] or "Товар 2nd STREET"
            im = re.search(r'(?:data-src|src)="((?:https?:)?//[^"]+?\.(?:jpe?g|png|webp)[^"]*)"', chunk)
            photo = None
            if im:
                photo = im.group(1)
                if photo.startswith("//"):
                    photo = "https:" + photo
            href = htmllib.unescape(href)
            if href.startswith("/"):
                href = "https://www.2ndstreet.jp" + href
            out.append(Item(self.code, gid, title, price, "JPY", href, photo))
        if not out:
            logging.getLogger("2ndstreet").warning("'%s': ничего не найдено (вёрстка могла измениться)", term)
        return out


# ------------------------------------------- Площадки, которые ещё не готовы
class NotReady(Source):
    ready = False

    def __init__(self, code, label, flag, currency, lang=None):
        super().__init__()
        self.code, self.label, self.flag, self.currency, self.lang = code, label, flag, currency, lang


VINTED_DOMAINS = [
    ("fr", "EUR", "🇫🇷"), ("de", "EUR", "🇩🇪"), ("it", "EUR", "🇮🇹"), ("es", "EUR", "🇪🇸"),
    ("nl", "EUR", "🇳🇱"), ("be", "EUR", "🇧🇪"), ("at", "EUR", "🇦🇹"), ("pt", "EUR", "🇵🇹"),
    ("lu", "EUR", "🇱🇺"), ("lt", "EUR", "🇱🇹"), ("pl", "PLN", "🇵🇱"), ("cz", "CZK", "🇨🇿"),
]


def build_sources() -> list[Source]:
    s: list[Source] = [Vinted(*d) for d in VINTED_DOMAINS]
    s += [MercariJP(), Bunjang(), SecondStreet()]
    # В разработке: запросы этих площадок ещё не изучены
    s += [
        NotReady("fruits_kr", "Fruits Family", "🇰🇷", "KRW", "ko"),
        NotReady("95_cn", "95分", "🇨🇳", "CNY"),
    ]
    return s
