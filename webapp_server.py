"""HTTP-сервер Telegram Mini App (aiohttp): страница, API и раздача медиа.

Запускается в том же процессе, что и бот (см. main.py).
Все запросы /api/* требуют заголовок X-Init-Data с подписанными данными Telegram.
"""
import asyncio
import glob
import hashlib
import html
import json
import logging
import os
import uuid
from urllib.parse import quote

from aiohttp import web
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from shop_logic import (
    LOCAL_NAME_RE, MAX_MEDIA, STAGES, normalize_media, validate_init_data,
)

log = logging.getLogger("webapp")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def jerr(status: int, message: str):
    return web.json_response({"error": message}, status=status)


def create_app(*, bot, token, admin_ids, shop, manager_username, store_name,
               notify_admins, static_dir, upload_dir, cache_dir,
               tagline="Одежда и обувь из Китая", currency="₽", webapp_url=""):
    os.makedirs(upload_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------ middleware
    @web.middleware
    async def guard(request, handler):
        try:
            if request.path.startswith("/api/"):
                user = validate_init_data(request.headers.get("X-Init-Data", ""), token)
                if not user:
                    return jerr(401, "Откройте приложение через Telegram-бота")
                request["user"] = user
                request["is_admin"] = user["id"] in admin_ids
                if request.path.startswith("/api/admin/") and not request["is_admin"]:
                    return jerr(403, "Нет доступа")
            return await handler(request)
        except web.HTTPException:
            raise
        except ValueError as e:
            return jerr(400, str(e))
        except Exception:
            log.exception("Ошибка в обработчике %s", request.path)
            return jerr(500, "Внутренняя ошибка сервера")

    app = web.Application(client_max_size=250 * 1024 * 1024, middlewares=[guard])

    # ------------------------------------------------------------- utilities
    def full_name(user: dict) -> str:
        return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)

    def user_label(user_id, username, name) -> str:
        label = f'<a href="tg://user?id={user_id}">{html.escape(name or "Без имени")}</a>'
        if username:
            label += f" (@{html.escape(username)})"
        return label

    def remove_local(items):
        for item in items:
            if item.startswith(("lp:", "lv:")):
                name = item.split(":", 1)[1]
                if LOCAL_NAME_RE.match(name):
                    try:
                        os.remove(os.path.join(upload_dir, name))
                    except OSError:
                        pass

    async def save_upload(part):
        ctype = (part.headers.get("Content-Type") or "").lower()
        ext = os.path.splitext(part.filename or "")[1].lower()
        if ctype.startswith("image/"):
            kind = "p"
            if ext not in IMAGE_EXT:
                ext = ".jpg"
        elif ctype.startswith("video/"):
            kind = "v"
            if ext not in VIDEO_EXT:
                ext = ".mp4"
        else:
            await part.read()  # неподдерживаемый тип: просто пропускаем
            return None
        name = uuid.uuid4().hex + ext
        with open(os.path.join(upload_dir, name), "wb") as f:
            while True:
                chunk = await part.read_chunk(512 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        return f"l{kind}:{name}"

    async def read_form(request):
        """Читает multipart-форму: текстовые поля и загруженные файлы (поле files)."""
        reader = await request.multipart()
        fields, saved = {}, []
        try:
            async for part in reader:
                if part.name == "files" and part.filename:
                    item = await save_upload(part)
                    if item:
                        saved.append(item)
                else:
                    fields[part.name] = await part.text()
        except Exception:
            remove_local(saved)
            raise
        return fields, saved

    # ---------------------------------------------------- рассылка «Новый дроп»
    bc_state = {"running": False}

    def drop_caption(p: dict, header: str) -> str:
        lines = [f"<b>{html.escape(header)}</b>", "", f"<b>{html.escape(p['name'])}</b>", html.escape(p["price_text"])]
        if p["sizes"]:
            lines.append("Размеры: " + html.escape(", ".join(p["sizes"])))
        if p["description"]:
            lines += ["", html.escape(p["description"][:300])]
        lines += ["", "Нажмите кнопку, чтобы открыть товар в приложении 👇"]
        return "\n".join(lines)

    def drop_markup():
        if not webapp_url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🛍 Зайти в магазин", web_app=WebAppInfo(url=webapp_url))
        ]])

    async def send_drop(uid: int, p: dict, header: str):
        """Фото (или видео) товара с подписью и кнопкой. Если медиа не отправилось, шлём текст."""
        caption, kb = drop_caption(p, header), drop_markup()
        keys = p.get("media_keys") or []
        if keys:
            kind, ref = keys[0].split(":", 1)
            try:
                if kind in ("lp", "lv"):
                    if not LOCAL_NAME_RE.match(ref):
                        raise ValueError("bad name")
                    source = FSInputFile(os.path.join(upload_dir, ref))
                else:
                    source = ref
                if kind in ("p", "lp"):
                    await bot.send_photo(uid, source, caption=caption, parse_mode="HTML", reply_markup=kb)
                else:
                    await bot.send_video(uid, source, caption=caption, parse_mode="HTML", reply_markup=kb)
                return
            except (TelegramForbiddenError, TelegramRetryAfter):
                raise
            except Exception as e:
                log.warning("Медиа для рассылки не отправилось, шлём текст: %s", e)
        await bot.send_message(uid, caption, parse_mode="HTML", reply_markup=kb)

    def note(bid, uid, status):
        """Запись в журнал «кому ушло». Для тестовой отправки журнал не ведём."""
        if bid:
            try:
                shop.log_delivery(bid, uid, status)
            except Exception:
                log.exception("Не удалось записать в журнал рассылки")

    async def deliver(ids, p: dict, header: str, bid=None):
        """Отправляет всем из списка. Возвращает (доставлено, заблокировали бота, ошибок)."""
        sent = blocked = failed = 0
        for uid in ids:
            for _ in range(3):
                try:
                    await send_drop(uid, p, header)
                    sent += 1
                    note(bid, uid, "sent")
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except TelegramForbiddenError:
                    blocked += 1
                    shop.mark_blocked(uid)
                    note(bid, uid, "blocked")
                    break
                except Exception as e:
                    log.warning("Рассылка: не доставлено %s: %s", uid, e)
                    failed += 1
                    note(bid, uid, "failed")
                    break
            else:
                failed += 1
                note(bid, uid, "failed")
            await asyncio.sleep(0.05)  # ~20 сообщений в секунду, в пределах лимитов Telegram
        return sent, blocked, failed

    async def run_broadcast(ids, p: dict, admin_id: int, test: bool):
        header = "⏳ Скоро в магазине" if p["state"] == "soon" else "🔥 Новый дроп"
        bid = None
        if not test:
            bc_state["running"] = True
            bid = shop.create_broadcast(p["id"], p["name"], "drop", len(ids))
        try:
            sent, blocked, failed = await deliver(ids, p, header, bid)
        except Exception:
            log.exception("Ошибка рассылки")
            sent = blocked = failed = 0
        finally:
            if not test:
                bc_state["running"] = False
            if bid:
                shop.finish_broadcast(bid)
        try:
            if test:
                text = "🧪 Тест отправлен. Так увидят рассылку получатели." if sent else "⚠️ Тест не отправился. Откройте чат с ботом и нажмите /start."
            else:
                text = (f"📣 Рассылка «{html.escape(p['name'])}» завершена\n"
                        f"✅ Доставлено: {sent}\n🚫 Заблокировали бота: {blocked}\n⚠️ Не доставлено: {failed}")
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception:
            pass

    async def notify_waiting(pid: int):
        """Товар из «Скоро» стал доступен: пишем всем, кто оставил заявку."""
        row = shop.get_product(pid)
        if not row or not row["active"] or row["state"] == "sold":
            return
        ids = shop.waiting_users(pid)
        if ids:
            bid = shop.create_broadcast(pid, row["name"], "waiting", len(ids))
            try:
                await deliver(ids, shop.product_dict(row), "🔔 Товар, которого вы ждали, уже в магазине", bid)
            finally:
                shop.finish_broadcast(bid)
        shop.clear_waiting(pid)

    def product_id(request) -> int:
        try:
            return int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()

    # ------------------------------------------------------------ page / media
    async def index(request):
        return web.FileResponse(
            os.path.join(static_dir, "index.html"),
            headers={"Cache-Control": "no-cache"},
        )

    async def health(request):
        return web.Response(text="ok")

    async def media(request):
        key = request.match_info["key"]
        if key.startswith(("lp:", "lv:")):
            name = key.split(":", 1)[1]
            path = os.path.join(upload_dir, name)
            if not LOCAL_NAME_RE.match(name) or not os.path.isfile(path):
                raise web.HTTPNotFound()
            return web.FileResponse(path, headers=IMMUTABLE)
        if key.startswith(("p:", "v:")):
            file_id = key.split(":", 1)[1]
            base = hashlib.sha1(file_id.encode()).hexdigest()
            found = glob.glob(os.path.join(cache_dir, base + ".*"))
            if not found:
                try:
                    tg_file = await bot.get_file(file_id)
                    ext = os.path.splitext(tg_file.file_path or "")[1] or (".mp4" if key[0] == "v" else ".jpg")
                    dest = os.path.join(cache_dir, base + ext)
                    tmp = dest + ".part"
                    await bot.download_file(tg_file.file_path, destination=tmp)
                    os.replace(tmp, dest)
                    found = [dest]
                except Exception as e:
                    # Бот не может скачать файлы больше 20 МБ — такое видео остаётся только в чате
                    log.warning("Не удалось получить файл из Telegram: %s", e)
                    raise web.HTTPNotFound()
            return web.FileResponse(found[0], headers=IMMUTABLE)
        raise web.HTTPNotFound()

    # ------------------------------------------------------------- client API
    async def me(request):
        u = request["user"]
        try:
            shop.touch_user(u["id"], u.get("username"), full_name(u))
        except Exception:
            log.exception("Не удалось запомнить пользователя")
        return web.json_response({
            "id": u["id"],
            "name": full_name(u) or u.get("username") or "Гость",
            "username": u.get("username") or "",
            "is_admin": request["is_admin"],
            "manager": manager_username,
            "store": store_name,
            "tagline": tagline,
            "currency": currency,
        })

    async def products(request):
        include_hidden = request["is_admin"] and request.query.get("all") == "1"
        return web.json_response({"items": shop.list_products(include_hidden)})

    async def lead(request):
        data = await request.json()
        try:
            pid = int(data.get("product_id"))
        except (TypeError, ValueError):
            raise ValueError("Не указан товар")
        u = request["user"]
        name = full_name(u)
        found = shop.add_lead(u["id"], u.get("username"), name, pid, str(data.get("size") or ""))
        if not found:
            return jerr(404, "Этого товара уже нет в наличии")
        row, label = found
        soon = row["state"] == "soon"
        if soon:
            if not request["is_admin"]:
                await notify_admins(
                    f"⏳ Ждут поступления «{html.escape(row['name'])}»:\n{user_label(u['id'], u.get('username'), name)}",
                    parse_mode="HTML",
                )
            return web.json_response({"waiting": True})
        hello = f"Здравствуйте! Меня интересует: {label} ({shop.money(row['price'])})"
        url = f"https://t.me/{manager_username}?text={quote(hello)}"
        if not request["is_admin"]:
            await notify_admins(
                f"👀 Заинтересовались товаром «{html.escape(label)}»:\n"
                f"{user_label(u['id'], u.get('username'), name)}",
                parse_mode="HTML",
            )
        return web.json_response({"url": url})

    async def view_product(request):
        """Клиент открыл товар: запоминаем интерес (админов не считаем)."""
        data = await request.json()
        try:
            pid = int(data.get("product_id"))
        except (TypeError, ValueError):
            raise ValueError("Не указан товар")
        if not request["is_admin"]:
            u = request["user"]
            shop.add_view(u["id"], u.get("username"), full_name(u), pid)
        return web.json_response({"ok": True})

    async def tracking(request):
        q = request.query.get("q", "").strip()[:64]
        own_id = str(request["user"]["id"])
        own = not q
        if own:
            q = own_id
        # Чужие посылки по ID находит только админ; обычный клиент ищет по коду заказа или по своему ID
        by_user = request["is_admin"] or q == own_id
        return web.json_response({"items": shop.search_tracking(q, by_user), "own": own})

    # -------------------------------------------------------------- admin API
    async def product_create(request):
        fields, saved = await read_form(request)
        try:
            row = shop.create_product(fields.get("name"), fields.get("description"),
                                      fields.get("price"), fields.get("sizes"), saved, fields.get("category"), fields.get("state"))
        except Exception:
            remove_local(saved)
            raise
        return web.json_response(shop.product_dict(row))

    async def product_update(request):
        pid = product_id(request)
        fields, saved = await read_form(request)
        try:
            old = shop.get_product(pid)
            if not old:
                raise web.HTTPNotFound()
            old_items = normalize_media(old["photo_id"])
            try:
                wanted = json.loads(fields.get("keep") or "[]")
            except ValueError:
                wanted = []
            keep = []
            for k in wanted:
                if k in old_items and k not in keep:
                    keep.append(k)
            media_items = keep + saved
            if len(media_items) > MAX_MEDIA:
                raise ValueError(f"Максимум {MAX_MEDIA} фото и видео на товар")
            row = shop.update_product(pid, fields.get("name"), fields.get("description"),
                                      fields.get("price"), fields.get("sizes"), media_items, fields.get("category"), fields.get("state"))
        except Exception:
            remove_local(saved)
            raise
        remove_local([i for i in old_items if i not in media_items])
        if old["state"] == "soon" and row["state"] != "soon":
            asyncio.create_task(notify_waiting(pid))
        return web.json_response(shop.product_dict(row))

    async def product_toggle(request):
        row = shop.toggle_product(product_id(request))
        if not row:
            raise web.HTTPNotFound()
        return web.json_response(shop.product_dict(row))

    async def product_delete(request):
        items = shop.delete_product(product_id(request))
        if items is None:
            raise web.HTTPNotFound()
        remove_local(items)
        return web.json_response({"ok": True})

    async def product_broadcast(request):
        pid = product_id(request)
        try:
            d = await request.json()
        except Exception:
            d = {}
        row = shop.get_product(pid)
        if not row:
            raise web.HTTPNotFound()
        if not row["active"]:
            raise ValueError("Сначала покажите товар в каталоге")
        if row["state"] == "sold":
            raise ValueError("Проданный товар рассылать нельзя")
        p = shop.product_dict(row)
        me_id = request["user"]["id"]
        if d.get("mode") == "test":
            ids, test = [me_id], True
        else:
            if bc_state["running"]:
                raise ValueError("Рассылка уже идёт, подождите её завершения")
            ids, test = shop.list_audience(), False
            if not ids:
                raise ValueError("Пока некому отправлять: бота ещё никто не открывал")
        asyncio.create_task(run_broadcast(ids, p, me_id, test))
        return web.json_response({"started": True, "total": len(ids)})

    async def broadcasts_list(request):
        return web.json_response({"items": shop.list_broadcasts()})

    async def broadcast_detail(request):
        b = shop.get_broadcast(product_id(request))
        if not b:
            raise web.HTTPNotFound()
        return web.json_response({"broadcast": b, "items": shop.broadcast_recipients(b["id"])})

    async def orders_list(request):
        return web.json_response({"items": shop.list_orders()})

    async def notify_client(user_id: str, text: str) -> bool:
        if not user_id or user_id == "0":
            return False
        try:
            await bot.send_message(int(user_id), text)
            return True
        except Exception:
            return False

    async def order_add(request):
        d = await request.json()
        row = shop.add_order(d.get("order_id"), d.get("user_id"), d.get("item_name"), d.get("status"), d.get("stage"))
        notified = await notify_client(
            row["user_id"],
            f"🔔 Новости по вашему заказу!\n🛸 Код: {row['order_id']}\n📦 Товар: {row['item_name']}\n"
            f"🚚 Этап: {STAGES[row['stage']]}\n📍 Новое местоположение: {row['status']}\n\nПосылка уже в пути! 🚀💨",
        )
        return web.json_response({"order": row, "notified": notified})

    async def order_update(request):
        rowid = product_id(request)
        d = await request.json()
        if not shop.get_order(rowid):
            raise web.HTTPNotFound()
        row = shop.update_order_status(rowid, d.get("status"), d.get("stage"))
        notified = await notify_client(
            row["user_id"],
            f"🔔 Статус вашего заказа обновился!\n🛸 Код: {row['order_id']}\n📦 Товар: {row['item_name']}\n"
            f"🚚 Этап: {STAGES[row['stage']]}\n📍 Новое местоположение: {row['status']}",
        )
        return web.json_response({"order": row, "notified": notified})

    async def order_delete(request):
        if not shop.delete_order(product_id(request)):
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def orders_delete_q(request):
        q = request.query.get("q", "").strip()[:64]
        if not q:
            raise ValueError("Укажите код заказа или ID клиента")
        return web.json_response({"deleted": shop.delete_orders(q)})

    async def leads_list(request):
        return web.json_response({"items": shop.list_leads()})

    r = app.router
    r.add_get("/", index)
    r.add_get("/health", health)
    r.add_get("/media/{key}", media)
    r.add_get("/api/me", me)
    r.add_get("/api/products", products)
    r.add_post("/api/lead", lead)
    r.add_post("/api/view", view_product)
    r.add_get("/api/tracking", tracking)
    r.add_post("/api/admin/product", product_create)
    r.add_post("/api/admin/product/{id}", product_update)
    r.add_post("/api/admin/product/{id}/toggle", product_toggle)
    r.add_delete("/api/admin/product/{id}", product_delete)
    r.add_post("/api/admin/product/{id}/broadcast", product_broadcast)
    r.add_get("/api/admin/broadcasts", broadcasts_list)
    r.add_get("/api/admin/broadcast/{id}", broadcast_detail)
    r.add_get("/api/admin/orders", orders_list)
    r.add_post("/api/admin/orders", order_add)
    r.add_delete("/api/admin/orders", orders_delete_q)
    r.add_post("/api/admin/order/{id}", order_update)
    r.add_delete("/api/admin/order/{id}", order_delete)
    r.add_get("/api/admin/leads", leads_list)
    return app
