import os
from pathlib import Path


def _load_env():
    p = Path(__file__).with_name(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

BOT_TOKEN = (__import__("re").findall(r"\d{6,}:[A-Za-z0-9_-]{30,}", os.environ.get("BOT_TOKEN", "")) or [""])[0]
ADMIN_ID = int(os.environ.get("ADMIN_ID") or 0)
RATE_MARKUP = 1 + float(os.environ.get("RATE_MARKUP_PERCENT") or 8) / 100
PROXY_URL = os.environ.get("PROXY_URL") or None
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL") or 45)
DB_PATH = os.environ.get("DB_PATH", "bot.db")
