import os
import json
import logging
import asyncpg
import hmac
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlencode, parse_qsl
from dotenv import load_dotenv
from notion_client import AsyncClient as NotionClient
from aiohttp import web as aiohttp_web
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
API_URL            = os.environ["API_URL"]
ADMIN_IDS          = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())
API_PORT           = int(os.getenv("PORT", 8080))

CATEGORIES = {"Новички": "🟢", "Любители": "🟡", "Продвинутые": "🔴"}

notion     = NotionClient(auth=NOTION_TOKEN)
db_pool    = None
tg_bot     = None
web_runner = None


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

async def add_to_notion(data: dict, username: str, user_id: int) -> str:
    page = await notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "ФИО":       {"title": [{"text": {"content": data.get("name", "")}}]},
            "Категория": {"multi_select": [{"name": data.get("category", "")}]},
            "Берпи":     {"number": int(data.get("burpees", 0))},
            "Видео":     {"url": data.get("video", "")},
            "Telegram":  {"rich_text": [{"text": {"content": f"{username} (id: {user_id})"}}]},
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


# ── AUTH ─────────────────────────────────────────────────────────────

def parse_init_data(init_data_str: str) -> dict | None:
    if not init_data_str:
        logger.warning("initData is empty")
        return None
    try:
        parsed    = dict(parse_qsl(init_data_str, keep_blank_values=True))
        user_data = json.loads(parsed.get("user", "{}"))
        if not user_data.get("id"):
            logger.warning("No user.id in initData: %s", init_data_str[:100])
            return None
        return user_data
    except Exception as e:
        logger.warning("parse_init_data error: %s", e)
        return None


# ── HTTP API ──────────────────────────────────────────────────────────

CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


async def api_submit(request: aiohttp_web.Request) -> aiohttp_web.Response:
    if request.method == "OPTIONS":
        return aiohttp_web.Response(headers=CORS)

    try:
        body = await request.json()
    except Exception:
        return aiohttp_web.json_response({"ok": False, "error": "invalid_json"}, status=400, headers=CORS)

    user_info = parse_init_data(body.get("initData", ""))
    if not user_info:
        return aiohttp_web.json_response({"ok": False, "error": "unauthorized"}, status=401, headers=CORS)

    user_id  = user_info["id"]
    data     = body.get("data", {})
    is_edit  = data.get("mode") == "edit"

    if is_edit:
        await update_submission(user_id, data)
        row = await get_submission(user_id)
        if row and row["notion_page_id"]:
            try:
                await update_notion_page(row["notion_page_id"], data)
            except Exception as e:
                logger.error("Notion update error: %s", e)
        return aiohttp_web.json_response({"ok": True}, headers=CORS)

    if await has_submitted(user_id):
        return aiohttp_web.json_response({"ok": False, "error": "already_submitted"}, headers=CORS)

    username = f"@{user_info['username']}" if user_info.get("username") else user_info.get("first_name", str(user_id))

    notion_page_id = ""
    try:
        notion_page_id = await add_to_notion(data, username, user_id)
        logger.info("Saved to Notion: user_id=%s", user_id)
    except Exception as e:
        logger.error("Notion error: %s", e)

    await save_submission(user_id, data, username, notion_page_id)

    category_icon = CATEGORIES.get(data.get("category", ""), "⚪️")
    username_part = f"@{user_info['username']}" if user_info.get("username") else f"{user_info.get('first_name', '')} (id: {user_id})"
    try:
        await tg_bot.send_message(
            chat_id=ORGANIZERS_CHAT_ID,
            text=(
                "🏋️ *Новая заявка*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *ФИО:* {data.get('name', '—')}\n"
                f"{category_icon} *Категория:* {data.get('category', '—')}\n"
                f"🔥 *Количество берпи:* {data.get('burpees', '—')}\n"
                f"🎥 *Видео:* {data.get('video', '—')}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 *Участник:* {username_part} `(id: {user_id})`"
            ),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error("Organizers notification error: %s", e)

    return aiohttp_web.json_response({"ok": True}, headers=CORS)


# ── KEYBOARDS ────────────────────────────────────────────────────────

def _webapp_url(extra_params: dict = None) -> str:
    params = {"api_url": API_URL}
    if extra_params:
        params.update(extra_params)
    sep = "&" if "?" in WEBAPP_URL else "?"
    return f"{WEBAPP_URL}{sep}{urlencode(params)}"


def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="📋 Загрузить результаты", web_app=WebAppInfo(url=_webapp_url()))]],
        resize_keyboard=True,
    )


def edit_keyboard(row):
    edit_url = _webapp_url({
        "mode":     "edit",
        "name":     row["name"],
        "category": row["category"],
        "burpees":  row["burpees"],
        "video":    row["video"],
    })
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text="✏️ Редактировать заявку", web_app=WebAppInfo(url=edit_url))]],
        resize_keyboard=True,
    )


# ── HANDLERS ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row  = await get_submission(user.id)

    if row:
        category_icon = CATEGORIES.get(row["category"], "⚪️")
        submitted_at  = row["submitted_at"].strftime("%d.%m.%Y %H:%M")
        await update.message.reply_text(
            "📋 <b>Твоя заявка</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>ФИО:</b> {row['name']}\n"
            f"{category_icon} <b>Категория:</b> {row['category']}\n"
            f"🔥 <b>Берпи:</b> {row['burpees']}\n"
            f"🎥 <b>Видео:</b> {row['video']}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 Подана: {submitted_at}",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=edit_keyboard(row),
        )
        return

    await update.message.reply_text(
        "Привет! 🔥\nЧтобы загрузить твои результаты, нажми на кнопку ниже",
        reply_markup=main_keyboard(),
    )


async def mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row  = await get_submission(user.id)

    if not row:
        await update.message.reply_text(
            "📭 Вы ещё не подавали заявку.\n\n"
            "Нажмите кнопку <b>«Загрузить результаты»</b> чтобы подать.",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
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
        reply_markup=edit_keyboard(row),
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


# ── LIFECYCLE ────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    global tg_bot, web_runner
    tg_bot = app.bot
    await init_db()

    aio_app = aiohttp_web.Application()
    aio_app.router.add_route("OPTIONS", "/api/submit", api_submit)
    aio_app.router.add_route("POST",    "/api/submit", api_submit)

    web_runner = aiohttp_web.AppRunner(aio_app)
    await web_runner.setup()
    await aiohttp_web.TCPSite(web_runner, "0.0.0.0", API_PORT).start()
    logger.info("Web server started on port %d", API_PORT)


async def post_shutdown(app: Application) -> None:
    if web_runner:
        await web_runner.cleanup()


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("mystatus", mystatus))
    app.add_handler(CommandHandler("stats",    stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))
    app.job_queue.run_repeating(sync_with_notion, interval=600, first=60)
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
