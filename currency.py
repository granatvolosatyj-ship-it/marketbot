import asyncio
import logging

import httpx

import config

log = logging.getLogger("rates")
URL = "https://www.cbr-xml-daily.ru/daily_json.json"


class Rates:
    def __init__(self):
        self.rub: dict[str, float] = {"RUB": 1.0}

    async def refresh(self):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                data = (await c.get(URL)).json()
            for code, v in data["Valute"].items():
                self.rub[code] = v["Value"] / v["Nominal"]
            log.info("Курсы ЦБ обновлены")
        except Exception as e:  # оставляем старые курсы
            log.warning("Не удалось обновить курсы: %s", e)

    async def loop(self):
        while True:
            await asyncio.sleep(3600)
            await self.refresh()

    def to_rub(self, amount: float, cur: str) -> float | None:
        rate = self.rub.get(cur)
        if rate is None:
            return None
        return amount * rate * config.RATE_MARKUP
