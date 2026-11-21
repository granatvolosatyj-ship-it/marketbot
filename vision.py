"""Сравнение фото: CLIP (через fastembed, бесплатно, без внешних сервисов).
Модель скачивается при первом использовании и работает на процессоре."""
import asyncio
import io
import logging
import threading

import httpx

log = logging.getLogger("vision")
_model = None
_failed = False
_lock = threading.Lock()
_sem = None
_cache: dict[str, object] = {}  # url -> вектор (или None), чтобы не считать дважды


def failed() -> bool:
    """True, если модель не загрузилась (нет библиотеки или нет сети)."""
    return _failed


def _load():
    global _model, _failed
    with _lock:
        if _model is None and not _failed:
            try:
                from fastembed import ImageEmbedding

                _model = ImageEmbedding("Qdrant/clip-ViT-B-32-vision")
            except Exception as e:
                _failed = True
                log.warning("Модель сравнения фото не загрузилась: %r", e)
        return _model


def _embed(data: bytes):
    model = _load()
    if model is None:
        return None
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        v = np.asarray(next(iter(model.embed([img]))), dtype="float32")
        n = float(np.linalg.norm(v))
        return v / n if n else None
    except Exception as e:
        log.warning("Не удалось разобрать фото: %r", e)
        return None


async def embed_bytes(data: bytes):
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(2)
    async with _sem:
        return await asyncio.to_thread(_embed, data)


async def embed_url(url: str):
    if url in _cache:
        return _cache[url]
    vec = None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
        if r.status_code == 200 and len(r.content) < 6_000_000:
            vec = await embed_bytes(r.content)
    except Exception as e:
        log.warning("Фото объявления не скачалось: %r", e)
    if len(_cache) > 2000:
        _cache.clear()
    _cache[url] = vec
    return vec


def from_bytes(b: bytes):
    import numpy as np

    return np.frombuffer(b, dtype="float32")


def similarity(a, b) -> float:
    import numpy as np

    return float(np.dot(a, b))
