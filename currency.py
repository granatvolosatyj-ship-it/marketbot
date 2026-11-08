import asyncio
import logging

import httpx

import config

log = logging.getLogger("rates")
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.json"
BACKUP_URL = "https://open.er-api.com/v6/latest/RUB"  # резервный источник


class Rates:
    def __init__(self):
        self.rub: dict[str, float] = {"RUB": 1.0}

    async def _from_cbr(self, c: httpx.AsyncClient):
        data = (await c.get(CBR_URL)).json()
        for code, v in data["Valute"].items():
            self.rub[code] = v["Value"] / v["Nominal"]

    async def _from_backup(self, c: httpx.AsyncClient):
        data = (await c.get(BACKUP_URL)).json()
        for code, per_rub in data["rates"].items():
            if per_rub:
                self.rub[code] = 1 / per_rub

    async def refresh(self):
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            for name, fn in (("ЦБ", self._from_cbr), ("резервный", self._from_backup)):
                try:
                    await fn(c)
                    log.warning("Курсы обновлены (%s): EUR=%.2f JPY=%.3f KRW=%.4f",
                                name, self.rub.get("EUR", 0), self.rub.get("JPY", 0), self.rub.get("KRW", 0))
                    return
                except Exception as e:
                    log.warning("Курсы (%s) не получены: %s", name, e)
        log.warning("Курсы валют недоступны, цены в рублях не показываются")

    async def loop(self):
        while True:
            await asyncio.sleep(3600)
            await self.refresh()

    def to_rub(self, amount: float, cur: str) -> float | None:
        rate = self.rub.get(cur)
        if rate is None:
            return None
        return amount * rate * config.RATE_MARKUP
