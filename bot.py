import os
import json
import logging
import asyncpg
from aiohttp import web
from datetime import datetime, timezone
from urllib.parse import urlencode
from dotenv import load_dotenv
from notion_client import AsyncClient as NotionClient
from telegram import Update, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN          = os.environ["BOT_TOKEN"]
ORGANIZERS_CHAT_ID = os.environ["ORGANIZERS_CHAT_ID"]
WEBAPP_URL         = os.environ["WEBAPP_URL"]
NOTION_TOKEN       = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
DATABASE_URL       = os.environ["DATABASE_URL"]
ADMIN_IDS          = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())

CATEGORIES = {"Новички": "🟢", "Любители": "🟡", "Продвинутые": "🔴"}

notion  = NotionClient(auth=NOTION_TOKEN)
db_pool = None


# ── DB ───────────────────────────────────────────────────────────────

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                user_id           BIGINT PRIMARY KEY,
                name              TEXT,
                category          TEXT,
                burpees           INTEGER,
                video             TEXT,
                telegram_username TEXT,
                notion_page_id    TEXT,
                submitted_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        for col, col_type in [
            ("category", "TEXT"),
            ("burpees", "INTEGER"),
            ("video", "TEXT"),
            ("telegram_username", "TEXT"),
            ("notion_page_id", "TEXT"),
        ]:
            await conn.execute(
                f"ALTER TABLE submissions ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )
    logger.info("Database ready")


async def has_submitted(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM submissions WHERE user_id = $1", user_id)
        return row is not None


async def get_submission(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM submissions WHERE user_id = $1", user_id)


async def save_submission(user_id: int, data: dict, username: str, notion_page_id: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO submissions
               (user_id, name, category, burpees, video, telegram_username, notion_page_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            user_id, data.get("name", ""), data.get("category", ""),
            int(data.get("burpees", 0)), data.get("video", ""),
            username, notion_page_id,
        )


async def update_submission(user_id: int, data: dict):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """UPDATE submissions
               SET name=$2, category=$3, burpees=$4, video=$5, submitted_at=NOW()
               WHERE user_id=$1""",
            user_id, data.get("name", ""), data.get("category", ""),
            int(data.get("burpees", 0)), data.get("video", ""),
        )


async def delete_submission(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM submissions WHERE user_id = $1", user_id)


async def get_stats():
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM submissions")
        rows  = await conn.fetch(
            "SELECT category, COUNT(*) as cnt FROM submissions GROUP BY category ORDER BY cnt DESC"
        )
        return total, rows


# ── NOTION ───────────────────────────────────────────────────────────

async def add_to_notion(data: dict, user) -> str:
    username = f"@{user.username}" if user.username else user.full_name
    page = await notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "ФИО":       {"title": [{"text": {"content": data.get("name", "")}}]},
            "Категория": {"multi_select": [{"name": data.get("category", "")}]},
            "Берпи":     {"number": int(data.get("burpees", 0))},
            "Видео":     {"url": data.get("video", "")},
            "Telegram":  {"rich_text": [{"text": {"content": f"{username} (id: {user.id})"}}]},
            "Дата":      {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        },
    )
    return page["id"]


async def update_notion_page(page_id: str, data: dict) -> None:
    await notion.pages.update(
        page_id=page_id,
        properties={
            "ФИО":       {"title": [{"text": {"content": data.get("name", "")}}]},
            "Категория": {"multi_select": [{"name": data.get("category", "")}]},
            "Берпи":     {"number": int(data.get("burpees", 0))},
            "Видео":     {"url": data.get("video", "")},
        },
    )


# ── KEYBOARDS ────────────────────────────────────────────────────────

# Секрет: добавляется в адрес TMA только для админов → открывает доступ к результатам.
RESULTS_KEY = "hrs2026key"


def _webapp_url(is_admin: bool = False, extra: dict = None) -> str:
    params = dict(extra or {})
    if is_admin:
        params["key"] = RESULTS_KEY
    url = WEBAPP_URL
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode(params)}"
    return url


def main_keyboard(is_admin: bool = False):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="📋 Загрузить результаты", web_app=WebAppInfo(url=_webapp_url(is_admin)))]],
        resize_keyboard=True,
    )


def edit_keyboard(row, is_admin: bool = False):
    edit_url = _webapp_url(is_admin, extra={
        "mode":     "edit",
        "name":     row["name"],
        "category": row["category"],
        "burpees":  row["burpees"],
        "video":    row["video"],
    })
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="🏆 Посмотреть результаты", web_app=WebAppInfo(url=edit_url))]],
        resize_keyboard=True,
    )


# ── HANDLERS ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user     = update.effective_user
    is_admin = user.id in ADMIN_IDS
    row      = await get_submission(user.id)

    if row:
        category_icon = CATEGORIES.get(row["category"], "⚪️")
        submitted_at  = row["submitted_at"].strftime("%d.%m.%Y %H:%M")
        text = (
            "📋 <b>Твоя заявка</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>ФИО:</b> {row['name']}\n"
            f"{category_icon} <b>Категория:</b> {row['category']}\n"
            f"🔥 <b>Берпи:</b> {row['burpees']}\n"
            f"🎥 <b>Видео:</b> {row['video']}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Подана: {submitted_at}"
        )
        try:
            await update.message.reply_text(
                text, parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=edit_keyboard(row, is_admin),
            )
        except Exception:
            await update.message.reply_text(
                text, parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=main_keyboard(is_admin),
            )
        return

    await update.message.reply_text(
        "Привет! 🔥\nЧтобы загрузить твои результаты, нажми на кнопку ниже",
        reply_markup=main_keyboard(is_admin),
    )


async def mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user     = update.effective_user
    is_admin = user.id in ADMIN_IDS
    row      = await get_submission(user.id)

    if not row:
        await update.message.reply_text(
            "📭 Вы ещё не подавали заявку.\n\n"
            "Нажмите кнопку <b>«Загрузить результаты»</b> чтобы подать.",
            parse_mode="HTML",
            reply_markup=main_keyboard(is_admin),
        )
        return

    category_icon = CATEGORIES.get(row["category"], "⚪️")
    submitted_at  = row["submitted_at"].strftime("%d.%m.%Y %H:%M")
    await update.message.reply_text(
        "📋 <b>Ваша заявка</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ФИО:</b> {row['name']}\n"
        f"{category_icon} <b>Категория:</b> {row['category']}\n"
        f"🔥 <b>Берпи:</b> {row['burpees']}\n"
        f"🎥 <b>Видео:</b> {row['video']}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 Подана: {submitted_at}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=edit_keyboard(row, is_admin),
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔️ У вас нет доступа к этой команде.")
        return

    total, rows = await get_stats()
    lines = [f"📊 <b>Статистика заявок</b>\n━━━━━━━━━━━━━━━━━━━━\n📝 Всего: <b>{total}</b>\n"]
    for row in rows:
        icon = CATEGORIES.get(row["category"], "⚪️")
        lines.append(f"{icon} {row['category']}: <b>{row['cnt']}</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.web_app_data.data
    logger.info("Received web_app_data: %s", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await update.message.reply_text("Ошибка обработки заявки. Попробуйте ещё раз.")
        return

    user          = update.effective_user
    username      = f"@{user.username}" if user.username else user.full_name
    category_icon = CATEGORIES.get(data.get("category", ""), "⚪️")
    is_edit       = data.get("mode") == "edit"

    if is_edit:
        await update_submission(user.id, data)
        row = await get_submission(user.id)
        if row and row["notion_page_id"]:
            try:
                await update_notion_page(row["notion_page_id"], data)
            except Exception as e:
                logger.error("Notion update error: %s", e)
        await update.message.reply_text(
            "✅ <b>Заявка обновлена!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>ФИО:</b> {data.get('name', '—')}\n"
            f"{category_icon} <b>Категория:</b> {data.get('category', '—')}\n"
            f"🔥 <b>Берпи:</b> {data.get('burpees', '—')}\n"
            f"🎥 <b>Видео:</b> {data.get('video', '—')}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Удачи на старте! 💪\n\n"
            "Посмотреть заявку: /mystatus",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=main_keyboard(),
        )
        return

    if await has_submitted(user.id):
        await update.message.reply_text(
            "⚠️ <b>Вы уже подали заявку.</b>\n\n"
            "Чтобы внести изменения — нажмите /mystatus",
            parse_mode="HTML",
        )
        return

    notion_page_id = ""
    try:
        notion_page_id = await add_to_notion(data, user)
        logger.info("Saved to Notion: user_id=%s", user.id)
    except Exception as e:
        logger.error("Notion error: %s", e)

    await save_submission(user.id, data, username, notion_page_id)

    username_part = f"@{user.username}" if user.username else f"{user.full_name} (id: {user.id})"
    try:
        await context.bot.send_message(
            chat_id=ORGANIZERS_CHAT_ID,
            text=(
                "🏋️ Новая заявка\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 ФИО: {data.get('name', '—')}\n"
                f"{category_icon} Категория: {data.get('category', '—')}\n"
                f"🔥 Количество берпи: {data.get('burpees', '—')}\n"
                f"🎥 Видео: {data.get('video', '—')}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 Участник: {username_part} (id: {user.id})"
            ),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error("Organizers notification error: %s", e)

    await update.message.reply_text(
        "✅ <b>Заявка принята!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ФИО:</b> {data.get('name', '—')}\n"
        f"{category_icon} <b>Категория:</b> {data.get('category', '—')}\n"
        f"🔥 <b>Берпи:</b> {data.get('burpees', '—')}\n"
        f"🎥 <b>Видео:</b> {data.get('video', '—')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Удачи на старте! 💪\n\n"
        "Посмотреть заявку: /mystatus",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def sync_with_notion(context: ContextTypes.DEFAULT_TYPE) -> None:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, name, notion_page_id FROM submissions "
            "WHERE notion_page_id IS NOT NULL AND notion_page_id != ''"
        )
    deleted = []
    for row in rows:
        try:
            page = await notion.pages.retrieve(row["notion_page_id"])
            if page.get("archived", False):
                await delete_submission(row["user_id"])
                deleted.append(row["name"])
        except Exception as e:
            logger.error("Sync error for %s: %s", row["notion_page_id"], e)
    if deleted:
        logger.info("Notion sync: deleted %d records: %s", len(deleted), deleted)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


# ── HTTP API (результаты для TMA) ────────────────────────────────────

def _cors(resp: web.StreamResponse) -> web.StreamResponse:
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _prop_title(prop) -> str:
    arr = prop.get("title", []) if prop else []
    return "".join(t.get("plain_text", "") for t in arr).strip()


def _prop_select(prop) -> str:
    sel = prop.get("select") if prop else None
    return (sel.get("name", "") if sel else "").strip()


def _prop_multi(prop) -> list:
    return [o.get("name", "").strip() for o in (prop.get("multi_select", []) if prop else [])]


def _prop_number(prop):
    return (prop.get("number") if prop else None)


def _prop_url(prop) -> str:
    return ((prop.get("url") or "") if prop else "").strip()


def _norm_gender(s: str) -> str:
    # убираем пробелы (включая неразрывные/zero-width) и приводим к нижнему регистру
    s = (s or "").replace(" ", "").replace("​", "").strip().lower()
    if not s:
        return ""
    if s.startswith("муж") or s in ("м", "m", "male"):
        return "М"
    if s.startswith("жен") or s in ("ж", "f", "female"):
        return "Ж"
    return ""


def _detect_gender(props: dict) -> str:
    """Ищет пол среди select/status/multi_select свойств — не зависит от названия колонки."""
    for prop in props.values():
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        candidates = []
        if ptype == "select" and prop.get("select"):
            candidates.append(prop["select"].get("name", ""))
        elif ptype == "status" and prop.get("status"):
            candidates.append(prop["status"].get("name", ""))
        elif ptype == "multi_select":
            candidates.extend(o.get("name", "") for o in prop.get("multi_select", []))
        for c in candidates:
            g = _norm_gender(c)
            if g:
                return g
    return ""


async def fetch_notion_participants() -> list:
    pages, cursor = [], None
    while True:
        kwargs = {"database_id": NOTION_DATABASE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = await notion.databases.query(**kwargs)
        pages.extend(resp.get("results", []))
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    return pages


async def handle_results(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return _cors(web.Response(status=204))

    category = request.query.get("category", "").strip()
    gender   = request.query.get("gender", "").strip()

    try:
        pages = await fetch_notion_participants()
    except Exception as e:
        logger.error("Results fetch error: %s", e)
        return _cors(web.json_response({"error": "fetch_failed"}, status=500))

    items = []
    for page in pages:
        props = page.get("properties", {})
        cats  = _prop_multi(props.get("Категория"))
        g     = _detect_gender(props)
        if category and category not in cats:
            continue
        if gender and g != gender:
            continue
        items.append({
            "name":    _prop_title(props.get("ФИО")),
            "burpees": _prop_number(props.get("Берпи")) or 0,
            "video":   _prop_url(props.get("Видео")),
            "gender":  g,
        })

    items.sort(key=lambda x: x["burpees"], reverse=True)
    return _cors(web.json_response(items))


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_web_server() -> None:
    web_app = web.Application()
    web_app.router.add_get("/", handle_health)
    web_app.router.add_get("/api/results", handle_results)
    web_app.router.add_options("/api/results", handle_results)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("HTTP server started on port %s", port)


# ── LIFECYCLE ────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await init_db()
    await start_web_server()


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("mystatus", mystatus))
    app.add_handler(CommandHandler("stats",    stats))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))
    app.job_queue.run_repeating(sync_with_notion, interval=600, first=60)
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
