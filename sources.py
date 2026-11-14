"""Площадки. Каждая отдаёт список новых объявлений (сначала самые свежие)."""
import base64
import json
import random
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
    created: float | None = None  # когда выставлено (unix-время), если сайт это отдаёт


def _ts(v) -> float | None:
    """Unix-время из строки/числа; отбрасывает явную ерунду."""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t > 1e12:  # миллисекунды
        t /= 1000
    return t if 1.4e9 < t < time.time() + 86400 else None


def _vinted_ts(photo: str | None) -> float | None:
    """Имя файла фото на Vinted = unix-время загрузки (приблизительно момент выставления)."""
    import re

    m = re.search(r"/(\d{10})\.(?:jpe?g|webp|png)", photo or "")
    return _ts(m.group(1)) if m else None


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
    interval = 75  # секунд между проверками (чаще Vinted блокирует: ошибка 429)
    _sem = None

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
        self._cool_until = 0.0
        self._cool_n = 0

    async def _refresh(self):
        (self.cffi or self.client).cookies.clear()
        r = await self.get(
            f"https://www.vinted.{self.tld}/",
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        r.raise_for_status()  # если главная страница закрыта, ошибка будет видна в /test
        self._has_cookie = True

    async def search(self, term: str) -> list[Item]:
        """Разбор HTML страницы каталога (внутренний API отдаёт 404, а страница открывается)."""
        import html as htmllib
        import logging
        import re

        import asyncio

        left = self._cool_until - time.monotonic()
        if left > 0:
            raise RuntimeError(f"Vinted просит не частить, пауза ещё {int(left)} с")
        if Vinted._sem is None:
            Vinted._sem = asyncio.Semaphore(3)  # не больше 3 запросов к Vinted одновременно
        url = f"https://www.vinted.{self.tld}/catalog"
        params = {"search_text": term, "order": "newest_first"}
        headers = {"Accept": "text/html,application/xhtml+xml"}
        async with Vinted._sem:
            await asyncio.sleep(random.uniform(0.2, 0.8))
            if not self._has_cookie:
                await self._refresh()
            r = await self.get(url, params=params, headers=headers)
            if r.status_code in (401, 403):
                await self._refresh()
                r = await self.get(url, params=params, headers=headers)
        if r.status_code == 429:
            self._cool_n += 1
            pause = min(60 * 2 ** (self._cool_n - 1), 600)  # 60, 120, 240, 480, 600 с
            self._cool_until = time.monotonic() + pause
            raise RuntimeError(f"HTTP 429: Vinted просит не частить, пауза {pause} с")
        r.raise_for_status()
        self._cool_n = 0
        page = r.text
        out, done = [], set()
        for m in re.finditer(r'<a\b[^>]*?href="(/items/(\d+)[^"]*)"[^>]*?>', page):
            tag, gid = m.group(0), m.group(2)
            if gid in done:
                continue
            tm = re.search(r'\btitle="([^"]*)"', tag)
            if not tm:
                continue
            full = htmllib.unescape(tm.group(1))
            parts = full.rsplit(", ", 2)
            if len(parts) < 3:
                continue
            name, price_s = parts[0], parts[1]
            pm = re.search(r"\d[\d\s\u00a0]*(?:[.,]\d+)?", price_s)
            if not pm:
                continue
            num = re.sub(r"[\s\u00a0]", "", pm.group(0)).replace(",", ".")
            try:
                price = float(num)
            except ValueError:
                continue
            done.add(gid)
            photo = None
            im = re.search(r'<img\b[^>]*product-item-id-%s--image--img[^>]*>' % gid, page)
            if im:
                sm = re.search(r'\bsrc="([^"]+)"', im.group(0))
                if sm:
                    photo = htmllib.unescape(sm.group(1))
            out.append(
                Item(
                    self.code,
                    gid,
                    name[:200],
                    price,
                    self.currency,
                    f"https://www.vinted.{self.tld}{m.group(1).split('?')[0]}",
                    photo,
                    _vinted_ts(photo),
                )
            )
        if not out:
            logging.getLogger("vinted").warning("%s '%s': ничего не найдено", self.tld, term)
        return out


# ---------------------------------------------------------------- Mercari JP
def _mercari_url(gid: str, item_type: str | None) -> str:
    """Обычные товары: /item/m123..., товары Mercari Shops (id без «m»+цифр): /shops/product/<id>."""
    import re

    if re.fullmatch(r"m\d+", gid) and "BEYOND" not in (item_type or ""):
        return f"https://jp.mercari.com/item/{gid}"
    return f"https://jp.mercari.com/shops/product/{gid}"


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
                    _mercari_url(str(it["id"]), it.get("itemType")),
                    thumbs[0] if thumbs else None,
                    _ts(it.get("created")),
                )
            )
        return out


# ------------------------------------------- Общий разбор HTML-страниц с карточками
class CardSource(Source):
    """Площадки, у которых страница поиска сразу отдаёт товары в HTML (Bunjang, Fruits Family).
    Ищем ссылки на товары, рядом цену в вонах, название и картинку."""

    impersonate = True
    currency = "KRW"
    lang = "ko"
    link_re = None  # group(1) = ссылка, group(2) = id товара
    base = ""

    async def fetch(self, term: str):
        raise NotImplementedError

    def full_url(self, href: str) -> str:
        import html as htmllib

        href = htmllib.unescape(href)
        return href if href.startswith("http") else self.base + href

    def parse(self, page: str) -> list[Item]:
        import html as htmllib
        import re

        matches = [(max(0, page.rfind("<", 0, m.start())), m.group(1), m.group(2)) for m in self.link_re.finditer(page)]
        out, done = [], set()
        for i, (pos, href, gid) in enumerate(matches):
            if gid in done:
                continue
            done.add(gid)
            j = i + 1
            while j < len(matches) and matches[j][2] == gid:
                j += 1
            end = matches[j][0] if j < len(matches) else pos + 3500
            chunk = page[pos : min(end, pos + 3500)]
            pm = re.search(r"(\d{1,3}(?:,\d{3})+|\d{3,})\s*원", chunk)
            if not pm:
                continue
            price = float(pm.group(1).replace(",", ""))
            title = ""
            for alt in re.findall(r'alt="([^"]*)"', chunk):
                alt = htmllib.unescape(alt).strip()
                if len(alt) >= 3 and "상품 이미지" not in alt:
                    title = alt
                    break
            if not title:
                txt = " ".join(re.sub(r"<[^>]+>", " ", chunk[: pm.start()]).split())
                txt = htmllib.unescape(txt.replace("상품 이미지", " "))
                words = txt.split()
                n = len(words)
                if n % 2 == 0 and n and words[: n // 2] == words[n // 2 :]:
                    words = words[: n // 2]  # название часто написано дважды
                title = " ".join(words)[:200]
            photo = None
            for im in re.finditer(r"<img\b[^>]*>", chunk):
                tag = im.group(0)
                sm = re.search(r'(?:data-src|src)="([^"]+)"', tag)
                if not sm or sm.group(1).startswith("data:") or ".svg" in sm.group(1):
                    continue
                u = htmllib.unescape(sm.group(1))
                if "/_next/image" in u:
                    from urllib.parse import parse_qs, unquote, urlparse

                    q = parse_qs(urlparse(u).query).get("url")
                    if q:
                        u = unquote(q[0])
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith("http"):
                    photo = u
                    break
            out.append(Item(self.code, gid, title or "Товар", price, "KRW", self.full_url(href), photo))
        return out

    async def search(self, term: str) -> list[Item]:
        import logging

        r = await self.fetch(term)
        if r.status_code == 404:
            return []  # такого бренда/запроса на площадке нет
        r.raise_for_status()
        out = self.parse(r.text)
        if not out:
            logging.getLogger(self.code).warning("'%s': ничего не найдено (вёрстка могла измениться)", term)
        return out

    async def diag(self, term: str) -> str:
        r = await self.fetch(term)
        t = r.text
        links = len(self.link_re.findall(t))
        items = self.parse(t)
        lines = [f"HTTP {r.status_code}, {len(t)} симв., ссылок на товары: {links}, разобрано: {len(items)}"]
        for it in items[:3]:
            lines.append(f"• {it.title[:60]} | {it.price:,.0f} ₩ | фото: {'есть' if it.photo else 'нет'} | {it.url}")
        m = self.link_re.search(t)
        if m:
            lines.append("Фрагмент: " + " ".join(t[max(0, m.start() - 100): m.start() + 700].split()))
        else:
            lines.append("Начало страницы: " + " ".join(t[:500].split()))
        return "\n".join(lines)


# ---------------------------------------------------------------- Bunjang
class Bunjang(CardSource):
    code = "bunjang"
    label = "Bunjang"
    flag = "🇰🇷"
    base = "https://m.bunjang.co.kr"
    link_re = __import__("re").compile(r'href="((?:https://m\.bunjang\.co\.kr)?/products/(\d+)[^"]*)"')

    async def fetch(self, term: str):
        """Сначала поиск с сортировкой «новые», запасной вариант: страница ключевого слова."""
        from urllib.parse import quote

        h = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "ko,en;q=0.8"}
        r = await self.get(f"{self.base}/search/products", params={"q": term, "order": "date"}, headers=h)
        if r.status_code == 200 and self.parse(r.text):
            return r
        r2 = await self.get(f"{self.base}/keywords/{quote(term)}", headers=h)
        return r2 if r2.status_code == 200 or r.status_code != 200 else r


    async def diag(self, term: str) -> str:
        """Что видит бот на страницах Bunjang + поиск адресов API в коде сайта."""
        import re
        from urllib.parse import quote

        h = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "ko,en;q=0.8"}
        lines = []
        shell = ""
        for name, url, kw in (
            ("поиск", f"{self.base}/search/products", {"q": term, "order": "date"}),
            ("ключевое слово", f"{self.base}/keywords/{quote(term)}", {}),
        ):
            r = await self.get(url, params=kw or None, headers=h)
            t = r.text
            lines.append(
                f"{name}: HTTP {r.status_code}, {len(t)} симв., ссылок /products/: {len(self.link_re.findall(t))}, "
                f"разобрано: {len(self.parse(t))}, __NEXT_DATA__: {'__NEXT_DATA__' in t}"
            )
            if not shell and "<script" in t:
                shell = t
        scripts = re.findall(r'<script[^>]+src="([^"]+)"', shell)
        lines.append(f"скриптов на странице: {len(scripts)}")
        found = []
        for src in scripts[:6]:
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = self.base + src
            try:
                js = (await self.get(src, headers={"Referer": self.base + "/"})).text
            except Exception:
                continue
            for u in re.findall(r"https?://[\w.-]*bunjang[\w.-]*/[\w./%-]{0,70}", js):
                if "api" in u and u not in found:
                    found.append(u)
            for u in re.findall(r"[\"'`](/api/[\w./-]{0,60})[\"'`]", js):
                if u not in found:
                    found.append(u)
        lines.append("адреса API в коде сайта: " + (" | ".join(found[:12]) or "не нашёл"))
        return "\n".join(lines)


    API_HOST = "https://api.bunjang.co.kr"
    API_PATH = "/api/search/v8/web/search"
    _ok = None  # параметры, которые сработали
    _policy: list[str] | None = None  # значения policyKey, найденные в скриптах сайта

    async def _scripts(self, term: str) -> list[str]:
        """Тексты скриптов сайта (в них прописаны параметры поиска)."""
        import asyncio
        import re
        from urllib.parse import urljoin

        page_url = f"{self.base}/search/products?q={term}&order=date"
        r = await self.get(page_url, headers={"Accept": "text/html", "Accept-Language": "ko,en;q=0.8"})
        srcs = []
        for u in re.findall(r'(?:src|href)="([^"]+\.js[^"]*)"', r.text):
            u = urljoin(page_url, u)
            if u not in srcs:
                srcs.append(u)

        async def grab(u):
            try:
                return (await self.get(u, headers={"Accept": "*/*"})).text
            except Exception:
                return ""

        return list(await asyncio.gather(*(grab(u) for u in srcs[:10])))

    @staticmethod
    def _policy_keys(bodies: list[str]) -> list[str]:
        import re

        keys = []
        for body in bodies:
            for m in re.finditer(r"""policyKey\s*[:=]\s*["'`]([A-Za-z0-9_.:\-]{2,80})["'`]""", body):
                keys.append(m.group(1))
            for m in re.finditer(r"""policyKey\s*:\s*\w+\s*\?\s*["']([^"']{2,80})["']\s*:\s*["']([^"']{2,80})["']""", body):
                keys += [m.group(1), m.group(2)]
        return list(dict.fromkeys(keys))

    @staticmethod
    def _variants(term: str, pk: str):
        return [
            {"policyKey": pk, "q": term, "size": 60},
            {"policyKey": pk, "keyword": term, "size": 60},
            {"policyKey": pk, "q": term, "size": 60, "order": "date"},
            {"policyKey": pk, "query": term, "size": 60},
        ]

    @staticmethod
    def _find_products(obj) -> list[dict]:
        """Ищет в любом JSON список товаров (словари с pid / id, названием и ценой)."""
        found = []

        def walk(o):
            if isinstance(o, dict):
                if (o.get("pid") or o.get("id")) and (o.get("name") or o.get("title")) and o.get("price") is not None:
                    found.append(o)
                    return
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(obj)
        return found

    def _to_items(self, data) -> list[Item]:
        out, seen = [], set()
        for d in self._find_products(data):
            if d.get("ad") or d.get("is_ad"):
                continue
            pid = str(d.get("pid") or d.get("id"))
            if pid in seen or not pid.isdigit():
                continue
            seen.add(pid)
            try:
                price = float(str(d.get("price")).replace(",", ""))
            except ValueError:
                continue
            img = d.get("product_image") or d.get("image") or d.get("imageUrl") or d.get("thumbnail") or ""
            if isinstance(img, list):
                img = img[0] if img else ""
            if isinstance(img, dict):
                img = img.get("url") or ""
            img = str(img).replace("{cnt}", "1").replace("{res}", "300") or None
            if img and img.startswith("//"):
                img = "https:" + img
            out.append(Item(self.code, pid, str(d.get("name") or d.get("title")), price, "KRW",
                            f"{self.base}/products/{pid}", img))
        return out

    async def _api(self, term: str):
        """Пробует новый поиск Bunjang; возвращает (товары, отчёт по попыткам)."""
        hdr = {"Accept": "application/json, text/plain, */*", "Referer": f"{self.base}/", "Origin": self.base,
               "Accept-Language": "ko,en;q=0.8"}
        report = []
        if Bunjang._policy is None:
            Bunjang._policy = self._policy_keys(await self._scripts(term))
        tries = [Bunjang._ok] if Bunjang._ok else []
        for pk in Bunjang._policy[:4]:
            tries += [(pk, i) for i in range(4)]
        for pk, i in tries[:10]:
            params = self._variants(term, pk)[i]
            try:
                r = await self.get(self.API_HOST + self.API_PATH, params=params, headers=hdr)
            except Exception as e:
                report.append(f"{pk[:20]} #{i}: ошибка {type(e).__name__}")
                continue
            items = []
            if r.status_code == 200:
                try:
                    items = self._to_items(r.json())
                except Exception:
                    pass
            report.append(f"{pk[:25]} #{i} {[k for k in params if k != 'policyKey']}: HTTP {r.status_code}, товаров {len(items)}, {' '.join(r.text.split())[:150]!r}")
            if items:
                Bunjang._ok = (pk, i)
                return items, report
        return [], report

    async def search(self, term: str) -> list[Item]:
        items, _ = await self._api(term)
        if items:
            return items
        return await super().search(term)  # запасной вариант: разбор HTML

    async def diag(self, term: str) -> str:
        """Показывает, откуда берётся policyKey, и что отвечает API."""
        import re

        bodies = await self._scripts(term)
        Bunjang._policy = self._policy_keys(bodies)
        lines = [f"policyKey в скриптах: {Bunjang._policy[:8] or 'не найден'}"]
        shown = 0
        for body in bodies:
            for m in re.finditer(r"policyKey", body):
                ctx = " ".join(body[max(0, m.start() - 120): m.start() + 200].split())
                if re.search(r"policyKey\s*[:=]\s*[\"'`]", ctx) or shown == 0:
                    lines.append("JS: " + ctx)
                    shown += 1
                if shown >= 3:
                    break
            if shown >= 3:
                break
        items, report = await self._api(term)
        lines.append(f"Итого товаров: {len(items)}")
        for it in items[:3]:
            lines.append(f"• {it.title[:50]} | {it.price:,.0f} ₩ | {it.url}")
        lines += report[:6]
        return "\n".join(lines)


# ---------------------------------------------------------------- Fruits Family
class FruitsFamily(CardSource):
    code = "fruits_kr"
    label = "Fruits Family"
    flag = "🇰🇷"
    base = "https://fruitsfamily.com"
    link_re = __import__("re").compile(r'href="((?:https://fruitsfamily\.com)?/product/([A-Za-z0-9]+)[^"]*)"')

    async def fetch(self, term: str):
        from urllib.parse import quote

        # страница бренда с сортировкой «новые»; название бренда пишется с заглавных букв
        return await self.get(
            f"{self.base}/brand/{quote(term.title())}",
            params={"sort": "NEW"},
            headers={"Accept": "text/html,application/xhtml+xml", "Accept-Language": "ko,en;q=0.8"},
        )


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
    s += [MercariJP(), Bunjang(), FruitsFamily(), SecondStreet()]
    # В разработке: запросы этих площадок ещё не изучены
    s += [
        NotReady("95_cn", "95分", "🇨🇳", "CNY"),
    ]
    return s
