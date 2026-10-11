"""HTTP-сервер Telegram Mini App (aiohttp): страница, API и раздача медиа.

Запускается в том же процессе, что и бот (см. main.py).
Все запросы /api/* требуют заголовок X-Init-Data с подписанными данными Telegram.
"""
import glob
import hashlib
import html
import json
import logging
import os
import uuid
from urllib.parse import quote

from aiohttp import web

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
               tagline="Одежда и обувь из Китая", currency="₽"):
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
        hello = f"Здравствуйте! Меня интересует: {label} ({shop.money(row['price'])})"
        url = f"https://t.me/{manager_username}?text={quote(hello)}"
        if not request["is_admin"]:
            await notify_admins(
                f"👀 Заинтересовались товаром «{html.escape(label)}»:\n"
                f"{user_label(u['id'], u.get('username'), name)}",
                parse_mode="HTML",
            )
        return web.json_response({"url": url})

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
                                      fields.get("price"), fields.get("sizes"), saved, fields.get("category"))
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
                                      fields.get("price"), fields.get("sizes"), media_items, fields.get("category"))
        except Exception:
            remove_local(saved)
            raise
        remove_local([i for i in old_items if i not in media_items])
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
    r.add_get("/api/tracking", tracking)
    r.add_post("/api/admin/product", product_create)
    r.add_post("/api/admin/product/{id}", product_update)
    r.add_post("/api/admin/product/{id}/toggle", product_toggle)
    r.add_delete("/api/admin/product/{id}", product_delete)
    r.add_get("/api/admin/orders", orders_list)
    r.add_post("/api/admin/orders", order_add)
    r.add_delete("/api/admin/orders", orders_delete_q)
    r.add_post("/api/admin/order/{id}", order_update)
    r.add_delete("/api/admin/order/{id}", order_delete)
    r.add_get("/api/admin/leads", leads_list)
    return app
