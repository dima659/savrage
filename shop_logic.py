"""Логика магазина без зависимостей от aiohttp и aiogram.

Здесь: проверка подписи Telegram Mini App (initData), работа с медиа товаров
и все операции с базой SQLite (товары, отслеживание заказов, «кто интересовался»).
"""
import hashlib
import hmac
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote

MSK = timezone(timedelta(hours=3))
MAX_MEDIA = 10
LOCAL_NAME_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,5}$")
MEDIA_PREFIXES = ("p:", "v:", "lp:", "lv:")
CATEGORIES = ["Обувь", "Одежда", "Аксессуары"]
STAGES = ["Китай", "Склад", "В пути", "У вас"]
STATES = {"": "В наличии", "soon": "Скоро", "sold": "Продан"}


def clean_state(value) -> str:
    value = (value or "").strip()
    return value if value in STATES else ""


def parse_stage(value, default: int = 2) -> int:
    """Номер этапа посылки (0..3). При ошибке возвращает default."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if 0 <= n < len(STAGES) else default


def clean_category(value) -> str:
    value = (value or "").strip()
    return value if value in CATEGORIES else ""


# ---------------------------------------------------------------- Telegram auth
def validate_init_data(init_data: str, token: str, max_age: int = 86400):
    """Проверяет подпись initData из Telegram Mini App.
    Возвращает словарь пользователя или None, если подпись неверна или устарела."""
    if not init_data or not token:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received_hash):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(parsed.get("user", "{}"))
        if not isinstance(user, dict) or "id" not in user:
            return None
        return user
    except Exception:
        return None


# ------------------------------------------------------------------- helpers
def split_sizes(sizes: str):
    return [s.strip()[:20] for s in (sizes or "").replace(";", ",").split(",") if s.strip()]


def parse_price(value) -> float:
    try:
        t = str(value).replace(",", ".").replace(" ", "").replace("\u00a0", "")
        price = float(t)
    except (TypeError, ValueError):
        raise ValueError("Введите цену числом больше нуля")
    if not (0 < price < 100_000_000):
        raise ValueError("Введите цену числом больше нуля")
    return round(price, 2)


def normalize_media(raw: str):
    """Строка из БД -> список элементов.
    p:/v: = фото/видео из Telegram (file_id), lp:/lv: = файлы, загруженные через приложение.
    Старые записи без префикса считаются фото из Telegram."""
    items = []
    for x in (raw or "").split(","):
        x = x.strip()
        if not x:
            continue
        items.append(x if x.startswith(MEDIA_PREFIXES) else "p:" + x)
    return items


def media_kind(item: str) -> str:
    return "video" if item.startswith(("v:", "lv:")) else "photo"


def media_json(item: str) -> dict:
    return {"type": media_kind(item), "url": "/media/" + quote(item, safe="")}


def now_msk() -> str:
    return datetime.now(MSK).strftime("%d.%m %H:%M")


# ---------------------------------------------------------------------- Shop
class Shop:
    def __init__(self, db_path: str, currency_sign: str = "₽"):
        self.db_path = db_path
        self.currency_sign = currency_sign

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self):
        conn = self.connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT, user_id TEXT, item_name TEXT, status TEXT
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                price REAL NOT NULL,
                sizes TEXT DEFAULT '',
                photo_id TEXT DEFAULT '',
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, username TEXT, full_name TEXT,
                product_id INTEGER, product_name TEXT, created TEXT
            );
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', "
            "full_name TEXT DEFAULT '', first_seen TEXT, last_seen TEXT, blocked INTEGER DEFAULT 0)"
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER, product_name TEXT, kind TEXT DEFAULT 'drop', created TEXT,
                total INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0, done INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS broadcast_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER, user_id INTEGER, status TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bclog ON broadcast_log (broadcast_id);
            """
        )
        # после перезапуска ни одна рассылка не продолжается: помечаем незавершённые как завершённые
        conn.execute("UPDATE broadcasts SET done = 1 WHERE done = 0")
        # Миграции для уже существующих баз: добавляем новые поля, если их ещё нет
        cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
        if "category" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN category TEXT DEFAULT ''")
        if "state" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN state TEXT DEFAULT ''")
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
        if "waiting" not in lcols:
            conn.execute("ALTER TABLE leads ADD COLUMN waiting INTEGER DEFAULT 0")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        if "stage" not in cols:
            # старые посылки считаем «в пути»
            conn.execute("ALTER TABLE orders ADD COLUMN stage INTEGER DEFAULT 2")
        # Аудитория рассылки: подтягиваем тех, кого бот уже знает по заявкам и посылкам
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, first_seen, last_seen) "
            "SELECT user_id, MAX(username), MAX(full_name), MIN(created), MAX(created) FROM leads "
            "WHERE user_id IS NOT NULL GROUP BY user_id"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen, last_seen) "
            "SELECT DISTINCT CAST(user_id AS INTEGER), '', '' FROM orders "
            "WHERE user_id GLOB '[0-9]*' AND user_id != '0'"
        )
        conn.commit()
        conn.close()

    # ----------------------------------------------------------- products
    def money(self, x: float) -> str:
        s = f"{x:.2f}".rstrip("0").rstrip(".")
        return f"{s} {self.currency_sign}"

    def product_dict(self, row) -> dict:
        keys = normalize_media(row["photo_id"])
        return {
            "id": row["id"],
            "name": row["name"],
            "category": row["category"] or "",
            "state": row["state"] or "",
            "description": row["description"] or "",
            "price": row["price"],
            "price_text": self.money(row["price"]),
            "sizes": split_sizes(row["sizes"]),
            "media": [media_json(i) for i in keys],
            "media_keys": keys,
            "active": bool(row["active"]),
        }

    def get_product(self, pid: int):
        conn = self.connect()
        row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        conn.close()
        return row

    def list_products(self, include_hidden: bool = False):
        conn = self.connect()
        sql = "SELECT * FROM products" + ("" if include_hidden else " WHERE active = 1") + " ORDER BY id DESC"
        rows = conn.execute(sql).fetchall()
        conn.close()
        return [self.product_dict(r) for r in rows]

    @staticmethod
    def clean_fields(name, description, price, sizes):
        name = (name or "").strip()[:100]
        if not name:
            raise ValueError("Введите название товара")
        description = (description or "").strip()[:600]
        price = parse_price(price)
        sizes = ", ".join(split_sizes(sizes or ""))
        return name, description, price, sizes

    def create_product(self, name, description, price, sizes, media_items, category="", state=""):
        name, description, price, sizes = self.clean_fields(name, description, price, sizes)
        if len(media_items) > MAX_MEDIA:
            raise ValueError(f"Максимум {MAX_MEDIA} фото и видео на товар")
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO products (name, description, price, sizes, photo_id, active, category, state) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (name, description, price, sizes, ",".join(media_items), clean_category(category), clean_state(state)),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.close()
        return row

    def update_product(self, pid, name, description, price, sizes, media_items, category="", state=""):
        name, description, price, sizes = self.clean_fields(name, description, price, sizes)
        if len(media_items) > MAX_MEDIA:
            raise ValueError(f"Максимум {MAX_MEDIA} фото и видео на товар")
        conn = self.connect()
        conn.execute(
            "UPDATE products SET name=?, description=?, price=?, sizes=?, photo_id=?, category=?, state=? WHERE id=?",
            (name, description, price, sizes, ",".join(media_items), clean_category(category), clean_state(state), pid),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        conn.close()
        return row

    def toggle_product(self, pid):
        conn = self.connect()
        conn.execute("UPDATE products SET active = 1 - active WHERE id = ?", (pid,))
        conn.commit()
        row = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        conn.close()
        return row

    def delete_product(self, pid):
        """Возвращает список медиа удалённого товара или None, если товара нет."""
        row = self.get_product(pid)
        if not row:
            return None
        conn = self.connect()
        conn.execute("DELETE FROM products WHERE id = ?", (pid,))
        conn.commit()
        conn.close()
        return normalize_media(row["photo_id"])

    # -------------------------------------------------------------- leads
    def add_lead(self, user_id, username, full_name, product_id, size=""):
        """Записывает, что клиент нажал «Написать менеджеру» (или «Сообщить, когда появится»).
        Возвращает (товар, название с размером) или None, если товар недоступен."""
        row = self.get_product(product_id)
        if not row or not row["active"] or row["state"] == "sold":
            return None
        soon = row["state"] == "soon"
        size = (size or "").strip()
        if size and size not in split_sizes(row["sizes"]):
            size = ""
        label = row["name"] + (f" (размер {size})" if size else "") + (" — ждёт поступления" if soon else "")
        conn = self.connect()
        if soon and conn.execute(
            "SELECT 1 FROM leads WHERE user_id = ? AND product_id = ? AND waiting = 1", (user_id, product_id)
        ).fetchone():
            conn.close()
            return row, label  # заявка уже есть, не дублируем
        conn.execute(
            "INSERT INTO leads (user_id, username, full_name, product_id, product_name, created, waiting) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username or "", full_name or "", product_id, label, now_msk(), 1 if soon else 0),
        )
        conn.commit()
        conn.close()
        return row, label

    def waiting_users(self, product_id):
        conn = self.connect()
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM leads WHERE product_id = ? AND waiting = 1", (product_id,)
        ).fetchall()
        conn.close()
        return [r["user_id"] for r in rows]

    def clear_waiting(self, product_id):
        conn = self.connect()
        conn.execute("UPDATE leads SET waiting = 0 WHERE product_id = ?", (product_id,))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------- users
    def touch_user(self, user_id, username="", full_name=""):
        """Запоминает человека, открывшего бота или приложение (для рассылки)."""
        conn = self.connect()
        conn.execute(
            "INSERT INTO users (user_id, username, full_name, first_seen, last_seen) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, full_name = excluded.full_name, "
            "last_seen = excluded.last_seen, blocked = 0",
            (int(user_id), username or "", full_name or "", now_msk(), now_msk()),
        )
        conn.commit()
        conn.close()

    # --------------------------------------------------------- рассылки
    def create_broadcast(self, product_id, product_name, kind, total):
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO broadcasts (product_id, product_name, kind, created, total) VALUES (?, ?, ?, ?, ?)",
            (product_id, product_name, kind, now_msk(), total),
        )
        conn.commit()
        conn.close()
        return cur.lastrowid

    def log_delivery(self, broadcast_id, user_id, status):
        """status: sent | blocked | failed. Счётчики обновляются сразу, чтобы прогресс было видно."""
        if status not in ("sent", "blocked", "failed"):
            return
        conn = self.connect()
        conn.execute("INSERT INTO broadcast_log (broadcast_id, user_id, status) VALUES (?, ?, ?)",
                     (broadcast_id, int(user_id), status))
        conn.execute(f"UPDATE broadcasts SET {status} = {status} + 1 WHERE id = ?", (broadcast_id,))
        conn.commit()
        conn.close()

    def finish_broadcast(self, broadcast_id):
        conn = self.connect()
        conn.execute("UPDATE broadcasts SET done = 1 WHERE id = ?", (broadcast_id,))
        conn.commit()
        conn.close()

    def list_broadcasts(self, limit: int = 30):
        conn = self.connect()
        rows = conn.execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_broadcast(self, broadcast_id):
        conn = self.connect()
        row = conn.execute("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def broadcast_recipients(self, broadcast_id):
        """Кому ушла рассылка: имя берём из таблицы users."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT l.user_id, l.status, COALESCE(u.username, '') AS username, COALESCE(u.full_name, '') AS full_name "
            "FROM broadcast_log l LEFT JOIN users u ON u.user_id = l.user_id "
            "WHERE l.broadcast_id = ? ORDER BY CASE l.status WHEN 'sent' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END, l.id",
            (broadcast_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def list_audience(self):
        conn = self.connect()
        rows = conn.execute("SELECT user_id FROM users WHERE blocked = 0").fetchall()
        conn.close()
        return [r["user_id"] for r in rows]

    def mark_blocked(self, user_id):
        conn = self.connect()
        conn.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (int(user_id),))
        conn.commit()
        conn.close()

    def list_leads(self, limit: int = 100):
        conn = self.connect()
        rows = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- tracking
    @staticmethod
    def _order_dict(r):
        return {"id": r["id"], "order_id": r["order_id"], "user_id": r["user_id"],
                "item_name": r["item_name"], "status": r["status"],
                "stage": parse_stage(r["stage"]), "stage_name": STAGES[parse_stage(r["stage"])]}

    def search_tracking(self, q: str, by_user: bool = True):
        """Поиск посылок по коду заказа и (если by_user) по ID клиента."""
        conn = self.connect()
        if by_user:
            rows = conn.execute(
                "SELECT * FROM orders WHERE order_id = ? COLLATE NOCASE OR user_id = ? ORDER BY id DESC", (q, q)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders WHERE order_id = ? COLLATE NOCASE ORDER BY id DESC", (q,)
            ).fetchall()
        conn.close()
        return [self._order_dict(r) for r in rows]

    def list_orders(self, limit: int = 500):
        conn = self.connect()
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [self._order_dict(r) for r in rows]

    def get_order(self, rowid: int):
        conn = self.connect()
        r = conn.execute("SELECT * FROM orders WHERE id = ?", (rowid,)).fetchone()
        conn.close()
        return self._order_dict(r) if r else None

    def add_order(self, order_id, user_id, item_name, status, stage=0):
        order_id = (order_id or "").strip()[:40]
        user_id = (str(user_id) if user_id is not None else "").strip() or "0"
        item_name = (item_name or "").strip()[:120]
        stage = parse_stage(stage, 0)
        # Уточнение необязательно: если пусто, показываем название этапа
        status = (status or "").strip()[:200] or STAGES[stage]
        if not order_id:
            raise ValueError("Введите код заказа")
        if not user_id.isdigit():
            raise ValueError("ID клиента — это число (или 0, если ID нет)")
        if not item_name:
            raise ValueError("Выберите товар или введите название")
        conn = self.connect()
        cur = conn.execute(
            "INSERT INTO orders (order_id, user_id, item_name, status, stage) VALUES (?, ?, ?, ?, ?)",
            (order_id, user_id, item_name, status, stage),
        )
        conn.commit()
        conn.close()
        return self.get_order(cur.lastrowid)

    def update_order_status(self, rowid, status, stage=None):
        cur_row = self.get_order(rowid)
        if not cur_row:
            return None
        new_stage = cur_row["stage"] if stage is None or stage == "" else parse_stage(stage, cur_row["stage"])
        status = (status or "").strip()[:200] or STAGES[new_stage]
        conn = self.connect()
        conn.execute("UPDATE orders SET status = ?, stage = ? WHERE id = ?", (status, new_stage, rowid))
        conn.commit()
        conn.close()
        return self.get_order(rowid)

    def delete_order(self, rowid) -> bool:
        conn = self.connect()
        cur = conn.execute("DELETE FROM orders WHERE id = ?", (rowid,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0

    def delete_orders(self, q: str) -> int:
        if not q:
            return 0
        conn = self.connect()
        cur = conn.execute("DELETE FROM orders WHERE order_id = ? OR user_id = ?", (q, q))
        conn.commit()
        conn.close()
        return cur.rowcount
