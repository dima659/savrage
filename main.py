import asyncio
import html
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo,
    WebAppInfo, MenuButtonWebApp, FSInputFile, ReplyKeyboardRemove,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# Мини-приложение (необязательная часть): файлы shop_logic.py, webapp_server.py и index.html
# должны лежать рядом с main.py. Без них бот работает как раньше, только без приложения.
try:
    from aiohttp import web
    from shop_logic import Shop
    from webapp_server import create_app
    WEBAPP_MODULES = True
except ImportError as _e:
    WEBAPP_MODULES = False
    print(f"⚠️ Мини-приложение отключено: не найден модуль ({_e}). Бот работает без него.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- НАСТРОЙКИ ---
# Токен: лучше задать переменной окружения BOT_TOKEN на хостинге.
# Если переменных нет — вставьте токен ВНУТРИ кавычек вместо ВСТАВЬ_ТОКЕН_СЮДА.
BOT_TOKEN = os.getenv("BOT_TOKEN") or "ВСТАВЬ_ТОКЕН_СЮДА"
if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
    print("❌ Не указан токен бота. Задайте BOT_TOKEN на хостинге или вставьте токен в код (в кавычках).")
    sys.exit(1)

# Общий список ID всех администраторов
ADMIN_IDS = [5486146974, 1391679215, 8175798403]

# Менеджер, на которого переводит кнопка под товаром
MANAGER_USERNAME = "LCmanager"

CURRENCY_SIGN = os.getenv("CURRENCY_SIGN", "₽")  # знак валюты в карточках товаров

# --- МИНИ-ПРИЛОЖЕНИЕ ---
# Адрес, по которому приложение открывается из интернета (обязательно https://...).
# Пока адрес не задан, приложение выключено, а бот работает как обычно.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")
if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
    print("⚠️ WEBAPP_URL должен начинаться с https:// — приложение выключено.")
    WEBAPP_URL = ""
PORT = int(os.getenv("PORT", "8080"))                       # порт веб-сервера (хостинг обычно задаёт сам)
SHOP_NAME = os.getenv("SHOP_NAME", "LC Store")              # название в шапке приложения
SHOP_TAGLINE = os.getenv("SHOP_TAGLINE", "Одежда и обувь из Китая")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PAGE_SIZE = 6
MAX_PHOTOS = 10  # лимит Telegram на один альбом
MSK = timezone(timedelta(hours=3))
DONE_TEXTS = ("✅ Готово", "-")
PHOTO_LOCK = asyncio.Lock()

# Тексты кнопок
BTN_ADD_PRODUCT = "➕ Добавить товар"
BTN_ADD_PRODUCT_OLD = "➕ Добавить товар в магазин"
BTN_MANAGE = "🛠 Управление товарами"
BTN_LEADS = "👀 Кто интересовался"
BTN_LEADS_OLD = "🧾 Заказы магазина"
BTN_APP = "📲 Открыть приложение"


# --- СОСТОЯНИЯ (FSM) ---
class OrderStates(StatesGroup):
    waiting_for_order_id = State()
    waiting_for_user_id = State()
    waiting_for_item_name = State()
    waiting_for_status = State()


class DeleteStates(StatesGroup):
    waiting_for_delete_id = State()


class SearchStates(StatesGroup):
    waiting_for_search_id = State()


class ProductStates(StatesGroup):
    name = State()
    description = State()
    price = State()
    sizes = State()
    photo = State()


class EditProductStates(StatesGroup):
    waiting_value = State()


# --- ВСПОМОГАТЕЛЬНОЕ ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_db_path():
    return "/data/orders.db" if os.path.exists("/data") else "orders.db"


def db():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def money(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return f"{s} {CURRENCY_SIGN}"


def parse_price(text: str) -> float:
    t = text.replace(",", ".").replace(" ", "").replace(CURRENCY_SIGN, "")
    value = float(t)
    if value <= 0:
        raise ValueError
    return round(value, 2)


def split_sizes(sizes: str):
    return [s.strip() for s in (sizes or "").split(",") if s.strip()]


MEDIA_PREFIXES = ("p:", "v:", "lp:", "lv:")


def data_dir() -> str:
    return os.path.dirname(get_db_path()) or "."


def upload_dir() -> str:
    return os.path.join(data_dir(), "uploads")


def media_items(p):
    """Медиа товара списком: p:/v: — фото и видео из Telegram (file_id),
    lp:/lv: — файлы, загруженные через приложение. Записи без префикса считаются фото."""
    items = []
    for x in (p['photo_id'] or "").split(","):
        x = x.strip()
        if not x:
            continue
        items.append(x if x.startswith(MEDIA_PREFIXES) else "p:" + x)
    return items


def decode_media(item: str):
    """Возвращает (тип, источник). Источник: file_id, файл на диске или None, если файла уже нет."""
    tag, ref = item.split(":", 1)
    kind = "video" if tag in ("v", "lv") else "photo"
    if tag in ("lp", "lv"):
        path = os.path.join(upload_dir(), os.path.basename(ref))
        return kind, (FSInputFile(path) if os.path.isfile(path) else None)
    return kind, ref


def remove_local_media(items):
    """Удаляет с диска файлы, загруженные через приложение (lp:/lv:), когда они больше не нужны."""
    for item in items:
        if item.startswith(("lp:", "lv:")):
            try:
                os.remove(os.path.join(upload_dir(), os.path.basename(item.split(":", 1)[1])))
            except OSError:
                pass


def get_product(pid: int):
    conn = db()
    p = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    conn.close()
    return p


def user_label(user_id, username, full_name) -> str:
    name = html.escape(full_name or "Без имени")
    text = f'<a href="tg://user?id={user_id}">{name}</a>'
    if username:
        text += f" (@{html.escape(username)})"
    return text


async def notify_admins(text: str, parse_mode=None):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode=parse_mode)
        except Exception:
            pass


# --- КНОПКИ ---
def webapp_inline_kb(text="🛍 Открыть магазин"):
    # Приложение открываем именно inline-кнопкой: из обычной клавиатуры Telegram не передаёт данные входа
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=WEBAPP_URL))]])


def get_client_kb(user_id=None):
    # Кнопки под полем ввода больше не нужны: всё в мини-приложении
    return ReplyKeyboardRemove()


def get_admin_kb():
    return ReplyKeyboardRemove()


def done_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Готово")]], resize_keyboard=True)


# --- БАЗА ДАННЫХ НА ХОСТИНГЕ ---
def init_db():
    if not os.path.exists("/data"):
        try:
            os.makedirs("/data")
        except Exception:
            pass
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    # Старая таблица трекинга посылок (не меняется)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            user_id TEXT,
            item_name TEXT,
            status TEXT
        )
    ''')
    # Товары магазина (photo_id хранит один или несколько file_id через запятую)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL,
            sizes TEXT DEFAULT '',
            photo_id TEXT DEFAULT '',
            active INTEGER DEFAULT 1
        )
    ''')
    # Кто нажимал кнопку «Написать менеджеру» под товаром
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            full_name TEXT,
            product_id INTEGER,
            product_name TEXT,
            created TEXT
        )
    ''')
    conn.commit()
    conn.close()


# =====================================================================
#                        СТАРЫЙ ФУНКЦИОНАЛ
# =====================================================================

# --- ОБЩИЕ КОМАНДЫ ---
@dp.message(Command("start"))
@dp.message(F.text == "🪐 Админ-панель")
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()

    if is_admin(message.from_user.id):
        text = (
            "👑 <b>Logistic Center</b>\n"
            "Панель администратора\n\n"
            "Товары, посылки и интерес клиентов — всё в приложении 👇"
        )
    else:
        text = (
            "📦 <b>Logistic Center</b>\n"
            "Одежда и обувь из Китая 🇨🇳\n\n"
            "Каталог, трекинг посылок и связь с менеджером — "
            "всё в одном приложении 👇"
        )

    avatar = os.path.join(BASE_DIR, "avatar.jpg")
    # ReplyKeyboardRemove в самом приветствии: старые кнопки под полем ввода исчезают
    if os.path.isfile(avatar):
        await message.answer_photo(FSInputFile(avatar), caption=text, parse_mode="HTML",
                                   reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    if WEBAPP_URL:
        await message.answer("Нажмите, чтобы открыть магазин 👇", reply_markup=webapp_inline_kb("🛍 Открыть магазин"))


@dp.message(F.text == BTN_APP)
async def open_app(message: Message, state: FSMContext):
    await state.clear()
    if not WEBAPP_URL:
        await message.answer("Приложение пока не подключено.")
        return
    await message.answer("Откройте магазин в приложении 👇", reply_markup=webapp_inline_kb())


@dp.message(F.text == "📱 Клиентское меню")
async def client_menu_switch(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Переключено на меню клиента. Смотрим глазами покупателя! 👀🍿", reply_markup=get_client_kb(message.from_user.id))


@dp.message(F.text == "🆔 Узнать мой ID")
async def show_my_id(message: Message):
    await message.answer(f"🔒 Ваш личный Telegram ID: {message.from_user.id}\nПередайте его администратору.")


@dp.message(F.text == "🛒 Корзина")
async def cart_removed(message: Message, state: FSMContext):
    # Старая кнопка могла остаться у клиентов в клавиатуре
    await state.clear()
    await message.answer(
        "Корзину мы убрали. Откройте «🛍 Каталог» и нажмите «Написать менеджеру» под нужным товаром 👇",
        reply_markup=get_client_kb(message.from_user.id),
    )


# --- ПОИСК ---
@dp.message(F.text == "🔍 Проверить статус")
async def ask_search_id(message: Message, state: FSMContext):
    await message.answer("📥 Отправьте мне ваш номер заказа (код) ИЛИ ваш личный Telegram ID:")
    await state.set_state(SearchStates.waiting_for_search_id)


@dp.message(SearchStates.waiting_for_search_id)
async def perform_search(message: Message, state: FSMContext):
    search_query = message.text.strip()
    await state.clear()
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("SELECT order_id, item_name, status FROM orders WHERE order_id = ? OR user_id = ?", (search_query, search_query))
    results = cursor.fetchall()
    conn.close()
    if results:
        text = f"✨ РЕЗУЛЬТАТЫ ПОИСКА [{search_query}] ✨\n📝 ───────────────────────\n\n"
        for i, row in enumerate(results, start=1):
            order_id, item_name, status = row
            text += f"Посылка №{i}\n📦 Товар: {item_name}\n🆔 Код заказа: {order_id}\n📍 Текущий статус:\n┌───────────────────────┐\n  {status}\n└───────────────────────┘\n🔹 ─────────────────────── 🔹\n\n"
        text += "Спасибо, что выбрали нас! Посылки уже летят! 🚀💨"
        await message.answer(text, reply_markup=get_client_kb(message.from_user.id))
    else:
        await message.answer("❌ ОЙ, ЗАКАЗ НЕ НАЙДЕН ❌\n───────────────────────\nК сожалению, товаров по этому запросу пока нет в нашей системе.\n\nУбедитесь, что код верный и администратор его уже внёс.", reply_markup=get_client_kb(message.from_user.id))


# --- ПРОСМОТР ВСЕХ ЗАКАЗОВ (ДЛЯ АДМИНА) ---
@dp.message(F.text == "📋 Список всех заказов")
async def show_all_orders(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("SELECT order_id, user_id, item_name, status FROM orders")
    results = cursor.fetchall()
    conn.close()
    if not results:
        await message.answer("📭 База данных пуста. Заказов пока нет.", reply_markup=get_admin_kb())
        return
    text = "🗂 СПИСОК ВСЕХ ТОВАРОВ В БАЗЕ:\n\n"
    for row in results:
        order_id, user_id, item_name, status = row
        text += f"🆔 Код: {order_id}\n👤 ID клиента: {user_id}\n📦 Вещь: {item_name}\n📍 Путь: {status}\n──────────────────\n"
    # Telegram не принимает сообщения длиннее 4096 символов, поэтому режем на части
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000], reply_markup=get_admin_kb())


# --- УДАЛЕНИЕ ВСЕХ ТОВАРОВ ПО КОДУ (ДЛЯ АДМИНА) ---
@dp.message(F.text == "❌ Удалить товары по коду")
async def start_delete_order(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer("🗑 Введите Код заказа (или ID клиента), чтобы стереть связанные с ним товары:")
    await state.set_state(DeleteStates.waiting_for_delete_id)


@dp.message(DeleteStates.waiting_for_delete_id)
async def process_delete(message: Message, state: FSMContext):
    order_id = message.text.strip()
    await state.clear()
    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute("SELECT order_id FROM orders WHERE order_id = ? OR user_id = ?", (order_id, order_id))
    if cursor.fetchone():
        cursor.execute("DELETE FROM orders WHERE order_id = ? OR user_id = ?", (order_id, order_id))
        conn.commit()
        await message.answer(f"🗑 Все товары по вашему запросу {order_id} успешно удалены! 🧹💥", reply_markup=get_admin_kb())
    else:
        await message.answer(f"⚠️ Данные по запросу {order_id} не найдены. 🤷‍♂️", reply_markup=get_admin_kb())
    conn.close()


# --- ДОБАВЛЕНИЕ ЗАКАЗА (ТРЕКИНГ, ДЛЯ АДМИНА) ---
@dp.message(F.text == "✍️ Добавить заказ")
async def start_add_order(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await message.answer("1️⃣ Введите Код или Номер заказа (например: CN-77):")
    await state.set_state(OrderStates.waiting_for_order_id)


@dp.message(OrderStates.waiting_for_order_id)
async def process_order_id(message: Message, state: FSMContext):
    await state.update_data(order_id=message.text.strip())
    await message.answer("2️⃣ Введите Telegram ID клиента (если ID нет, введите цифру 0):")
    await state.set_state(OrderStates.waiting_for_user_id)


@dp.message(OrderStates.waiting_for_user_id)
async def process_user_id(message: Message, state: FSMContext):
    await state.update_data(user_id=message.text.strip())
    await state.set_state(OrderStates.waiting_for_item_name)
    conn = db()
    products = conn.execute("SELECT id, name FROM products ORDER BY id DESC LIMIT 40").fetchall()
    conn.close()
    if not products:
        await message.answer("3️⃣ Введите Название нового товара (в каталоге пока пусто, например: Крутая куртка):")
        return
    rows = [[InlineKeyboardButton(text=p['name'][:60], callback_data=f"op:{p['id']}")] for p in products]
    rows.append([InlineKeyboardButton(text="✏️ Ввести название вручную", callback_data="opm")])
    await message.answer(
        "3️⃣ Выберите товар из каталога 👇\n(или просто напишите название текстом, если его нет в каталоге)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(OrderStates.waiting_for_item_name, F.data.startswith("op:"))
async def order_pick_product(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    p = get_product(int(call.data.split(":")[1]))
    if not p:
        await call.answer("Товар не найден (возможно, удалён)", show_alert=True); return
    await state.update_data(item_name=p['name'])
    await state.set_state(OrderStates.waiting_for_status)
    try:
        await call.message.edit_text(f"✅ Выбран товар: {p['name']}")
    except Exception:
        pass
    await call.message.answer("4️⃣ Введите Текущий статус пути товара (например: Прибыло на склад в Гуанчжоу):")
    await call.answer()


@dp.callback_query(OrderStates.waiting_for_item_name, F.data == "opm")
async def order_manual_name(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    try:
        await call.message.edit_text("✏️ Введите название товара сообщением:")
    except Exception:
        pass
    await call.answer()


@dp.message(OrderStates.waiting_for_item_name)
async def process_item_name(message: Message, state: FSMContext):
    await state.update_data(item_name=message.text.strip())
    await message.answer("4️⃣ Введите Текущий статус пути товара (например: Прибыло на склад в Гуанчжоу):")
    await state.set_state(OrderStates.waiting_for_status)


@dp.message(OrderStates.waiting_for_status)
async def process_status_and_save(message: Message, state: FSMContext):
    status = message.text.strip()
    data = await state.get_data()
    await state.clear()

    order_id = data['order_id']
    user_id = data['user_id']
    item_name = data['item_name']

    conn = sqlite3.connect(get_db_path())
    cursor = conn.cursor()
    cursor.execute('INSERT INTO orders (order_id, user_id, item_name, status) VALUES (?, ?, ?, ?)', (order_id, user_id, item_name, status))
    conn.commit()
    conn.close()

    await message.answer(f"✅ Готово! Новый товар добавлен к коду {order_id}!\n📦 Товар: {item_name}\n📍 Статус: {status}", reply_markup=get_admin_kb())
    if user_id != "0":
        try:
            await bot.send_message(chat_id=int(user_id), text=f"🔔 Новости по вашему заказу!\n🛸 Код: {order_id}\n📦 Товар: {item_name}\n📍 Новое местоположение: {status}\n\nПосылка уже в пути! 🚀💨")
            await message.answer("⚡ Клиент получил автоматическое уведомление в ЛС!")
        except Exception:
            pass


# =====================================================================
#                   МАГАЗИН: КАРТОЧКИ ТОВАРОВ
# =====================================================================

def product_caption(p, admin_view=False) -> str:
    text = f"🏷 {p['name']}\n"
    if p['description']:
        text += f"\n{p['description']}\n"
    sizes = split_sizes(p['sizes'])
    if sizes:
        text += f"\n📏 Размеры: {', '.join(sizes)}"
    text += f"\n💰 Цена: {money(p['price'])}"
    if admin_view and not p['active']:
        text += "\n\n🙈 Скрыт от покупателей"
    return text


async def send_product_card(chat_id: int, p, kb=None, admin_view=False):
    caption = product_caption(p, admin_view)
    resolved = []
    for item in media_items(p)[:MAX_PHOTOS]:
        kind, src = decode_media(item)
        if src is not None:
            resolved.append((kind, src))
    try:
        if len(resolved) == 1:
            kind, src = resolved[0]
            if kind == "video":
                await bot.send_video(chat_id, src, caption=caption, reply_markup=kb)
            else:
                await bot.send_photo(chat_id, src, caption=caption, reply_markup=kb)
            return
        if len(resolved) > 1:
            media = []
            for i, (kind, src) in enumerate(resolved):
                cap = caption if i == 0 else None
                if kind == "video":
                    media.append(InputMediaVideo(media=src, caption=cap))
                else:
                    media.append(InputMediaPhoto(media=src, caption=cap))
            await bot.send_media_group(chat_id, media)
            # К альбому нельзя прикрепить кнопку, поэтому она идёт отдельным сообщением
            if kb:
                await bot.send_message(chat_id, f"👆 {p['name']}", reply_markup=kb)
            return
    except Exception:
        pass
    await bot.send_message(chat_id, caption, reply_markup=kb)


# =====================================================================
#                   МАГАЗИН: УПРАВЛЕНИЕ ТОВАРАМИ (АДМИН)
# =====================================================================

async def collect_photo(message: Message, state: FSMContext):
    """Складывает присланные фото и видео в список. На альбом отвечает один раз."""
    async with PHOTO_LOCK:
        data = await state.get_data()
        photos = list(data.get('photos', []))
        if message.photo:
            item = "p:" + message.photo[-1].file_id
        else:
            item = "v:" + message.video.file_id
        full = len(photos) >= MAX_PHOTOS
        if item not in photos and not full:
            photos.append(item)
        gid = message.media_group_id
        notify = not (gid and data.get('last_group') == gid)
        await state.update_data(photos=photos, last_group=gid)
    if notify:
        text = "📸 Получено. Отправьте ещё фото или видео либо нажмите «✅ Готово»."
        if full:
            text = f"⚠️ Максимум {MAX_PHOTOS} фото и видео на товар. Нажмите «✅ Готово»."
        await message.answer(text, reply_markup=done_kb())


# --- ДОБАВЛЕНИЕ ТОВАРА В МАГАЗИН ---
@dp.message(F.text.in_({BTN_ADD_PRODUCT, BTN_ADD_PRODUCT_OLD}))
async def product_add_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    await message.answer("1️⃣ Введите название товара (например: Пуховик зимний):")
    await state.set_state(ProductStates.name)


@dp.message(ProductStates.name, F.text)
async def product_add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:100])
    await message.answer("2️⃣ Введите описание (или «-», чтобы пропустить):")
    await state.set_state(ProductStates.description)


@dp.message(ProductStates.description, F.text)
async def product_add_description(message: Message, state: FSMContext):
    desc = message.text.strip()
    await state.update_data(description="" if desc == "-" else desc[:600])
    await message.answer(f"3️⃣ Введите цену числом в {CURRENCY_SIGN} (например: 4500):")
    await state.set_state(ProductStates.price)


@dp.message(ProductStates.price, F.text)
async def product_add_price(message: Message, state: FSMContext):
    try:
        price = parse_price(message.text)
    except ValueError:
        await message.answer("⚠️ Не похоже на цену. Введите число больше нуля, например: 4500")
        return
    await state.update_data(price=price)
    await message.answer("4️⃣ Введите размеры через запятую (например: S, M, L, XL или 40, 41, 42) или «-», если размеров нет:")
    await state.set_state(ProductStates.sizes)


@dp.message(ProductStates.sizes, F.text)
async def product_add_sizes(message: Message, state: FSMContext):
    sizes = message.text.strip()
    await state.update_data(sizes="" if sizes == "-" else ", ".join(split_sizes(sizes)), photos=[], last_group=None)
    await message.answer(
        f"5️⃣ Отправьте фото и/или видео товара. Можно несколько сразу (до {MAX_PHOTOS} файлов): выделите их в галерее и отправьте.\n"
        "Когда закончите, нажмите «✅ Готово». Если медиа не нужно, тоже нажмите «✅ Готово».",
        reply_markup=done_kb(),
    )
    await state.set_state(ProductStates.photo)


async def save_product(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    photo_ids = ",".join(data.get('photos', []))
    conn = db()
    cur = conn.execute(
        "INSERT INTO products (name, description, price, sizes, photo_id, active) VALUES (?, ?, ?, ?, ?, 1)",
        (data['name'], data['description'], data['price'], data['sizes'], photo_ids),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    p = get_product(pid)
    await message.answer("✅ Товар добавлен в каталог! Вот как он выглядит:", reply_markup=get_admin_kb())
    await send_product_card(message.chat.id, p, admin_view=True)


@dp.message(ProductStates.photo, F.photo | F.video)
async def product_add_photo(message: Message, state: FSMContext):
    await collect_photo(message, state)


@dp.message(ProductStates.photo, F.text)
async def product_add_finish(message: Message, state: FSMContext):
    if message.text.strip() in DONE_TEXTS:
        await save_product(message, state)
    else:
        await message.answer("Отправьте фото или видео либо нажмите «✅ Готово».", reply_markup=done_kb())


# --- СПИСОК ТОВАРОВ ДЛЯ УПРАВЛЕНИЯ ---
def admin_product_kb(p) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{p['id']}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delask:{p['id']}")],
        [InlineKeyboardButton(text="👁 Показать" if not p['active'] else "🙈 Скрыть", callback_data=f"tog:{p['id']}")],
    ])


@dp.message(F.text == BTN_MANAGE)
async def manage_products(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    conn = db()
    products = conn.execute("SELECT * FROM products ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    if not products:
        await message.answer(f"📭 В магазине пока нет товаров. Нажмите «{BTN_ADD_PRODUCT}».", reply_markup=get_admin_kb())
        return
    await message.answer(f"🛠 Товары магазина (последние {len(products)}):", reply_markup=get_admin_kb())
    for p in products:
        await send_product_card(message.chat.id, p, admin_product_kb(p), admin_view=True)


@dp.callback_query(F.data.startswith("tog:"))
async def toggle_product(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    pid = int(call.data.split(":")[1])
    conn = db()
    conn.execute("UPDATE products SET active = 1 - active WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    p = get_product(pid)
    if not p:
        await call.answer("Товар не найден", show_alert=True); return
    caption = product_caption(p, admin_view=True)
    try:
        if call.message.photo or call.message.video:
            await call.message.edit_caption(caption=caption, reply_markup=admin_product_kb(p))
        else:
            await call.message.edit_text(caption, reply_markup=admin_product_kb(p))
    except Exception:
        pass
    await call.answer("Скрыт от покупателей" if not p['active'] else "Снова виден в каталоге")


@dp.callback_query(F.data.startswith("delask:"))
async def delete_product_ask(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    pid = int(call.data.split(":")[1])
    p = get_product(pid)
    if not p:
        await call.answer("Товар уже удалён", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del:{pid}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel"),
    ]])
    await call.message.answer(f"Удалить товар «{p['name']}» навсегда?", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data == "cancel")
async def cancel_action(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Отменено")


@dp.callback_query(F.data.startswith("del:"))
async def delete_product(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    pid = int(call.data.split(":")[1])
    p = get_product(pid)
    name = p['name'] if p else "Товар"
    conn = db()
    conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    if p:
        remove_local_media(media_items(p))
    try:
        await call.message.edit_text(f"🗑 «{name}» удалён.")
    except Exception:
        pass
    await call.answer("Удалено")


# --- РЕДАКТИРОВАНИЕ ТОВАРА ---
FIELD_TITLES = {
    "name": "название",
    "description": "описание",
    "price": "цену",
    "sizes": "размеры (через запятую, «-» чтобы убрать)",
}


@dp.callback_query(F.data.startswith("edit:"))
async def edit_product_menu(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    pid = int(call.data.split(":")[1])
    p = get_product(pid)
    if not p:
        await call.answer("Товар не найден", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Название", callback_data=f"ef:{pid}:name"),
         InlineKeyboardButton(text="Описание", callback_data=f"ef:{pid}:description")],
        [InlineKeyboardButton(text="Цена", callback_data=f"ef:{pid}:price"),
         InlineKeyboardButton(text="Размеры", callback_data=f"ef:{pid}:sizes")],
        [InlineKeyboardButton(text="Фото и видео", callback_data=f"ef:{pid}:photo")],
    ])
    await call.message.answer(f"Что изменить в товаре «{p['name']}»?", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("ef:"))
async def edit_product_field(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    _, pid, field = call.data.split(":")
    if field == "photo":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить к текущим", callback_data=f"em:{pid}:add")],
            [InlineKeyboardButton(text="🔄 Заменить все", callback_data=f"em:{pid}:replace")],
        ])
        await call.message.answer("Что сделать с фото и видео товара?", reply_markup=kb)
        await call.answer()
        return
    await state.set_state(EditProductStates.waiting_value)
    await state.update_data(pid=int(pid), field=field, photos=[], last_group=None)
    await call.message.answer(f"Введите новое значение: {FIELD_TITLES[field]}")
    await call.answer()


@dp.callback_query(F.data.startswith("em:"))
async def edit_product_media(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True); return
    _, pid, mode = call.data.split(":")
    p = get_product(int(pid))
    if not p:
        await call.answer("Товар не найден", show_alert=True); return
    current = media_items(p) if mode == "add" else []
    await state.set_state(EditProductStates.waiting_value)
    await state.update_data(pid=int(pid), field="photo", photos=current, last_group=None)
    if mode == "add":
        text = (f"Сейчас у товара файлов: {len(current)} (максимум {MAX_PHOTOS}). "
                "Отправьте новые фото и/или видео, они добавятся к текущим.")
    else:
        text = "Отправьте новые фото и/или видео: все старые будут заменены."
    await call.message.answer(text + "\nКогда закончите, нажмите «✅ Готово».", reply_markup=done_kb())
    await call.answer()


async def apply_product_edit(message: Message, state: FSMContext, value):
    data = await state.get_data()
    pid, field = data['pid'], data['field']
    await state.clear()
    column = "photo_id" if field == "photo" else field
    old = get_product(pid)
    conn = db()
    conn.execute(f"UPDATE products SET {column} = ? WHERE id = ?", (value, pid))
    conn.commit()
    conn.close()
    if field == "photo" and old:
        # Удаляем с диска файлы приложения, которые заменили новыми
        remove_local_media([i for i in media_items(old) if i not in value.split(",")])
    p = get_product(pid)
    if not p:
        await message.answer("Товар не найден (возможно, уже удалён).", reply_markup=get_admin_kb())
        return
    await message.answer("✅ Изменено! Теперь товар выглядит так:", reply_markup=get_admin_kb())
    await send_product_card(message.chat.id, p, admin_product_kb(p), admin_view=True)


@dp.message(EditProductStates.waiting_value, F.photo | F.video)
async def edit_value_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get('field') != "photo":
        await message.answer("Сейчас нужен текст, а не фото или видео.")
        return
    await collect_photo(message, state)


@dp.message(EditProductStates.waiting_value, F.text)
async def edit_value_text(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get('field')
    text = message.text.strip()
    if field == "photo":
        if text == "✅ Готово":
            photos = data.get('photos', [])
            if not photos:
                await message.answer("Сначала отправьте хотя бы одно фото или видео.", reply_markup=done_kb())
                return
            await apply_product_edit(message, state, ",".join(photos))
        else:
            await message.answer("Отправьте фото или видео либо нажмите «✅ Готово».", reply_markup=done_kb())
        return
    if field == "price":
        try:
            value = parse_price(text)
        except ValueError:
            await message.answer("⚠️ Введите число больше нуля, например: 4500")
            return
    elif field == "sizes":
        value = "" if text == "-" else ", ".join(split_sizes(text))
    elif field == "description":
        value = "" if text == "-" else text[:600]
    else:
        value = text[:100]
    await apply_product_edit(message, state, value)


# =====================================================================
#              МАГАЗИН: КАТАЛОГ И КНОПКА «НАПИСАТЬ МЕНЕДЖЕРУ»
# =====================================================================

async def send_catalog_page(chat_id: int, offset: int):
    conn = db()
    products = conn.execute(
        "SELECT * FROM products WHERE active = 1 ORDER BY id DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE + 1, offset),
    ).fetchall()
    conn.close()
    if not products and offset == 0:
        await bot.send_message(chat_id, "🛍 Каталог пока пуст. Загляните позже!")
        return
    has_more = len(products) > PAGE_SIZE
    for p in products[:PAGE_SIZE]:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="💬 Написать менеджеру", callback_data=f"ask:{p['id']}")
        ]])
        await send_product_card(chat_id, p, kb)
    if has_more:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬇️ Показать ещё", callback_data=f"cat:{offset + PAGE_SIZE}")
        ]])
        await bot.send_message(chat_id, "Это не все товары 👇", reply_markup=kb)


@dp.message(F.text == "🛍 Каталог")
async def show_catalog(message: Message, state: FSMContext):
    await state.clear()
    await send_catalog_page(message.chat.id, 0)


@dp.callback_query(F.data.startswith("cat:"))
async def catalog_more(call: CallbackQuery):
    offset = int(call.data.split(":")[1])
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_catalog_page(call.message.chat.id, offset)
    await call.answer()


@dp.callback_query(F.data.startswith("ask:"))
async def ask_manager(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    p = get_product(pid)
    if not p or not p['active']:
        await call.answer("Этого товара уже нет в наличии 😔", show_alert=True)
        return
    u = call.from_user
    full_name = " ".join(x for x in (u.first_name, u.last_name) if x)
    when = datetime.now(MSK).strftime("%d.%m %H:%M")

    conn = db()
    conn.execute(
        "INSERT INTO leads (user_id, username, full_name, product_id, product_name, created) VALUES (?, ?, ?, ?, ?, ?)",
        (u.id, u.username or "", full_name, pid, p['name'], when),
    )
    conn.commit()
    conn.close()

    # Ссылка на чат с менеджером с уже написанным сообщением про товар
    hello = f"Здравствуйте! Меня интересует: {p['name']} ({money(p['price'])})"
    url = f"https://t.me/{MANAGER_USERNAME}?text={quote(hello)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 Открыть чат с менеджером", url=url)]])
    await call.message.answer(
        f"Отлично! Нажмите кнопку ниже, чтобы написать менеджеру по товару «{p['name']}» 👇",
        reply_markup=kb,
    )
    await call.answer()

    # Сообщаем админам, кто заинтересовался (самих админов не беспокоим)
    if not is_admin(u.id):
        await notify_admins(
            f"👀 Заинтересовались товаром «{html.escape(p['name'])}»:\n{user_label(u.id, u.username, full_name)}",
            parse_mode="HTML",
        )


# --- КТО ИНТЕРЕСОВАЛСЯ (АДМИН) ---
@dp.message(F.text.in_({BTN_LEADS, BTN_LEADS_OLD}))
async def show_leads(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    conn = db()
    rows = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    if not rows:
        await message.answer("📭 Пока никто не нажимал «Написать менеджеру».", reply_markup=get_admin_kb())
        return
    text = "👀 КТО ИНТЕРЕСОВАЛСЯ (последние 30):\n\n"
    for r in rows:
        text += (
            f"🏷 {html.escape(r['product_name'])}\n"
            f"👤 {user_label(r['user_id'], r['username'], r['full_name'])}\n"
            f"🕒 {r['created']}\n──────────────────\n"
        )
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800], parse_mode="HTML", reply_markup=get_admin_kb())


# --- МИНИ-ПРИЛОЖЕНИЕ: ЗАПУСК ВЕБ-СЕРВЕРА ---
async def start_webapp():
    """Поднимает веб-сервер приложения рядом с ботом. Возвращает runner или None, если приложение выключено."""
    if not WEBAPP_URL:
        return None
    if not WEBAPP_MODULES:
        print("⚠️ Приложение не запущено: рядом с main.py нет shop_logic.py и webapp_server.py.")
        return None
    sub = os.path.join(BASE_DIR, "webapp")
    static_dir = sub if os.path.isfile(os.path.join(sub, "index.html")) else BASE_DIR
    if not os.path.isfile(os.path.join(static_dir, "index.html")):
        print("⚠️ Приложение не запущено: не найден файл index.html рядом с main.py.")
        return None
    app = create_app(
        bot=bot, token=BOT_TOKEN, admin_ids=ADMIN_IDS,
        shop=Shop(get_db_path(), CURRENCY_SIGN),
        manager_username=MANAGER_USERNAME, store_name=SHOP_NAME, tagline=SHOP_TAGLINE, currency=CURRENCY_SIGN,
        notify_admins=notify_admins, static_dir=static_dir,
        upload_dir=upload_dir(), cache_dir=os.path.join(data_dir(), "cache"),
    )
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    try:
        # Кнопка «Магазин» слева от поля ввода во всех чатах с ботом
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        print(f"⚠️ Не удалось установить кнопку меню: {e}")
    return runner


# --- ЗАПУСК ---
async def main():
    init_db()
    runner = await start_webapp()
    print("==================================================")
    print(" БОТ-МАГАЗИН УСПЕШНО ЗАПУЩЕН! 👑")
    print(f" Менеджер: @{MANAGER_USERNAME}")
    if runner:
        print(f" Приложение: {WEBAPP_URL} (порт {PORT})")
    else:
        print(" Приложение: выключено (задайте WEBAPP_URL, чтобы включить)")
    print("==================================================")
    sys.stdout.flush()
    try:
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
