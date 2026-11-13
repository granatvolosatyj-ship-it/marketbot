import sqlite3


class DB:
    def __init__(self, path: str):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, name TEXT, allowed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS subs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, brand TEXT, UNIQUE(user_id, brand));
            CREATE TABLE IF NOT EXISTS disabled(
                user_id INTEGER, source TEXT, PRIMARY KEY(user_id, source));
            CREATE TABLE IF NOT EXISTS seen(
                source TEXT, item_id TEXT, PRIMARY KEY(source, item_id));
            CREATE TABLE IF NOT EXISTS sub_disabled(
                sub_id INTEGER, source TEXT, PRIMARY KEY(sub_id, source));
            CREATE TABLE IF NOT EXISTS sent(
                user_id INTEGER, key TEXT, PRIMARY KEY(user_id, key));
            CREATE TABLE IF NOT EXISTS baselined(
                source TEXT, term TEXT, PRIMARY KEY(source, term));
            """
        )
        for stmt in (
            "ALTER TABLE users ADD COLUMN paused INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN max_rub REAL",
            "ALTER TABLE subs ADD COLUMN max_rub REAL",
        ):
            try:  # миграция старой базы: если колонка уже есть, это нормально
                self.c.execute(stmt)
            except sqlite3.OperationalError:
                pass
        self.c.commit()

    # users
    def ensure_user(self, uid: int, name: str = "", allowed: bool | None = None):
        self.c.execute("INSERT OR IGNORE INTO users(id, name, allowed) VALUES(?,?,0)", (uid, name))
        if allowed is not None:
            self.c.execute("UPDATE users SET allowed=? WHERE id=?", (int(allowed), uid))
        self.c.commit()

    def is_allowed(self, uid: int) -> bool:
        r = self.c.execute("SELECT allowed FROM users WHERE id=?", (uid,)).fetchone()
        return bool(r and r["allowed"])

    def users(self):
        return self.c.execute("SELECT id, name, allowed FROM users ORDER BY id").fetchall()

    # subs
    def add_sub(self, uid: int, brand: str) -> bool:
        cur = self.c.execute("INSERT OR IGNORE INTO subs(user_id, brand) VALUES(?,?)", (uid, brand))
        self.c.commit()
        return cur.rowcount > 0

    def del_sub(self, uid: int, sub_id: int):
        self.c.execute("DELETE FROM sub_disabled WHERE sub_id IN (SELECT id FROM subs WHERE id=? AND user_id=?)", (sub_id, uid))
        self.c.execute("DELETE FROM subs WHERE id=? AND user_id=?", (sub_id, uid))
        self.c.commit()

    # площадки отдельно для каждого бренда
    def get_sub(self, uid: int, sub_id: int):
        return self.c.execute(
            "SELECT id, brand, max_rub FROM subs WHERE id=? AND user_id=?", (sub_id, uid)
        ).fetchone()

    def brand_disabled(self, sub_id: int) -> set[str]:
        rows = self.c.execute("SELECT source FROM sub_disabled WHERE sub_id=?", (sub_id,)).fetchall()
        return {r["source"] for r in rows}

    def set_brand_sources(self, sub_id: int, off: set[str]):
        """Сохраняет, на каких площадках этот бренд ВЫКЛЮЧЕН."""
        self.c.execute("DELETE FROM sub_disabled WHERE sub_id=?", (sub_id,))
        self.c.executemany("INSERT INTO sub_disabled(sub_id, source) VALUES(?,?)", [(sub_id, c) for c in off])
        self.c.commit()

    def user_subs(self, uid: int):
        return self.c.execute("SELECT id, brand, max_rub FROM subs WHERE user_id=? ORDER BY id", (uid,)).fetchall()

    # пауза и лимиты цены (в рублях)
    def set_paused(self, uid: int, paused: bool):
        self.c.execute("UPDATE users SET paused=? WHERE id=?", (int(paused), uid))
        self.c.commit()

    def is_paused(self, uid: int) -> bool:
        r = self.c.execute("SELECT paused FROM users WHERE id=?", (uid,)).fetchone()
        return bool(r and r["paused"])

    def set_global_limit(self, uid: int, rub: float | None):
        self.c.execute("UPDATE users SET max_rub=? WHERE id=?", (rub, uid))
        self.c.commit()

    def global_limit(self, uid: int) -> float | None:
        r = self.c.execute("SELECT max_rub FROM users WHERE id=?", (uid,)).fetchone()
        return r["max_rub"] if r else None

    def set_brand_limit(self, uid: int, brand: str, rub: float | None) -> bool:
        cur = self.c.execute("UPDATE subs SET max_rub=? WHERE user_id=? AND brand=?", (rub, uid, brand))
        self.c.commit()
        return cur.rowcount > 0

    # sources on/off
    def disabled_sources(self, uid: int) -> set[str]:
        rows = self.c.execute("SELECT source FROM disabled WHERE user_id=?", (uid,)).fetchall()
        return {r["source"] for r in rows}

    def set_source(self, uid: int, source: str, enabled: bool):
        if enabled:
            self.c.execute("DELETE FROM disabled WHERE user_id=? AND source=?", (uid, source))
        else:
            self.c.execute("INSERT OR IGNORE INTO disabled(user_id, source) VALUES(?,?)", (uid, source))
        self.c.commit()

    # polling helpers
    def active_terms(self, source: str) -> list[str]:
        rows = self.c.execute(
            """SELECT DISTINCT s.brand FROM subs s JOIN users u ON u.id=s.user_id
               WHERE u.allowed=1 AND COALESCE(u.paused,0)=0
               AND NOT EXISTS (SELECT 1 FROM disabled d WHERE d.user_id=s.user_id AND d.source=?)
               AND NOT EXISTS (SELECT 1 FROM sub_disabled sd WHERE sd.sub_id=s.id AND sd.source=?)""",
            (source, source),
        ).fetchall()
        return [r["brand"] for r in rows]

    def subscribers(self, brand: str, source: str) -> list[tuple[int, float | None]]:
        """(id пользователя, лимит цены в ₽ или None). Остановленные (/stop) не включаются."""
        rows = self.c.execute(
            """SELECT s.user_id, COALESCE(s.max_rub, u.max_rub) AS lim
               FROM subs s JOIN users u ON u.id=s.user_id
               WHERE s.brand=? AND u.allowed=1 AND COALESCE(u.paused,0)=0
               AND NOT EXISTS (SELECT 1 FROM disabled d WHERE d.user_id=s.user_id AND d.source=?)
               AND NOT EXISTS (SELECT 1 FROM sub_disabled sd WHERE sd.sub_id=s.id AND sd.source=?)""",
            (brand, source, source),
        ).fetchall()
        return [(r["user_id"], r["lim"]) for r in rows]

    # seen / baseline
    def is_baselined(self, source: str, term: str) -> bool:
        return self.c.execute(
            "SELECT 1 FROM baselined WHERE source=? AND term=?", (source, term)
        ).fetchone() is not None

    def set_baselined(self, source: str, term: str):
        self.c.execute("INSERT OR IGNORE INTO baselined(source, term) VALUES(?,?)", (source, term))
        self.c.commit()

    def is_seen(self, source: str, item_id: str) -> bool:
        return self.c.execute(
            "SELECT 1 FROM seen WHERE source=? AND item_id=?", (source, item_id)
        ).fetchone() is not None

    def mark_seen(self, source: str, item_id: str):
        self.c.execute("INSERT OR IGNORE INTO seen(source, item_id) VALUES(?,?)", (source, item_id))
        self.c.commit()

    def claim_sent(self, user_id: int, key: str) -> bool:
        """True, если это объявление этому человеку ещё не отправляли (и сразу помечает)."""
        cur = self.c.execute("INSERT OR IGNORE INTO sent(user_id, key) VALUES(?,?)", (user_id, key))
        self.c.commit()
        return cur.rowcount == 1
