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

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": UA},
            proxy=config.PROXY_URL,
        )

    async def search(self, term: str) -> list[Item]:
        raise NotImplementedError


# ---------------------------------------------------------------- Vinted
class Vinted(Source):
    def __init__(self, tld: str, currency: str, flag: str):
        super().__init__()
        self.tld = tld
        self.code = f"vinted_{tld}"
        self.label = f"Vinted {tld.upper()}"
        self.flag = flag
        self.currency = currency
        self._has_cookie = False

    async def _refresh(self):
        self.client.cookies.clear()
        await self.client.get(
            f"https://www.vinted.{self.tld}/",
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        self._has_cookie = True

    async def search(self, term: str) -> list[Item]:
        if not self._has_cookie:
            await self._refresh()
        url = f"https://www.vinted.{self.tld}/api/v2/catalog/items"
        params = {"search_text": term, "order": "newest_first", "per_page": 30, "page": 1}
        headers = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
        for attempt in (1, 2):
            r = await self.client.get(url, params=params, headers=headers)
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
    s += [MercariJP(), Bunjang()]
    # В разработке: страницы/запросы ещё не изучены
    s += [
        NotReady("fruits_kr", "Fruits Family", "🇰🇷", "KRW", "ko"),
        NotReady("2ndstreet_jp", "2nd STREET", "🇯🇵", "JPY", "ja"),
        NotReady("95_cn", "95分", "🇨🇳", "CNY"),
    ]
    return s
