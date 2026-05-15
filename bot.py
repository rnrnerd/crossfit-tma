import os
import json
import logging
import asyncpg
from datetime import datetime, timezone
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

BOT_TOKEN           = os.environ["BOT_TOKEN"]
ORGANIZERS_CHAT_ID  = os.environ["ORGANIZERS_CHAT_ID"]
WEBAPP_URL          = os.environ["WEBAPP_URL"]
NOTION_TOKEN        = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID  = os.environ["NOTION_DATABASE_ID"]
DATABASE_URL        = os.environ["DATABASE_URL"]
ADMIN_IDS           = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())

CATEGORIES = {"Новички": "🟢", "Любители": "🟡", "Продвинутые": "🔴"}

notion = NotionClient(auth=NOTION_TOKEN)
db_pool = None


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                user_id      BIGINT PRIMARY KEY,
                name         TEXT,
                category     TEXT,
                burpees      INTEGER,
                video        TEXT,
                telegram_username TEXT,
                submitted_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Миграция: добавляем колонки если таблица уже существовала
        for col, col_type in [
            ("category", "TEXT"),
            ("burpees", "INTEGER"),
            ("video", "TEXT"),
            ("telegram_username", "TEXT"),
        ]:
            await conn.execute(f"""
                ALTER TABLE submissions ADD COLUMN IF NOT EXISTS {col} {col_type}
            """)
    logger.info("Database ready")


async def has_submitted(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM submissions WHERE user_id = $1", user_id)
        return row is not None


async def save_submission(user_id: int, data: dict, username: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO submissions (user_id, name, category, burpees, video, telegram_username)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            user_id,
            data.get("name", ""),
            data.get("category", ""),
            int(data.get("burpees", 0)),
            data.get("video", ""),
            username,
        )


async def get_submission(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM submissions WHERE user_id = $1", user_id
        )


async def get_stats():
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM submissions")
        rows = await conn.fetch(
            "SELECT category, COUNT(*) as cnt FROM submissions GROUP BY category ORDER BY cnt DESC"
        )
        return total, rows


async def add_to_notion(data: dict, user) -> None:
    username = f"@{user.username}" if user.username else user.full_name
    telegram_str = f"{username} (id: {user.id})"
    await notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties={
            "ФИО":       {"title": [{"text": {"content": data.get("name", "")}}]},
            "Категория": {"multi_select": [{"name": data.get("category", "")}]},
            "Берпи":     {"number": int(data.get("burpees", 0))},
            "Видео":     {"url": data.get("video", "")},
            "Telegram":  {"rich_text": [{"text": {"content": telegram_str}}]},
            "Дата":      {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        },
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[
        KeyboardButton(
            text="📋 Загрузить результаты",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]]
    await update.message.reply_text(
        "Привет!🔥\n"
        "Чтобы загрузить твои результаты, нажми на кнопку ниже",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode="Markdown",
    )


async def mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row = await get_submission(user.id)

    if not row:
        await update.message.reply_text(
            "📭 Вы ещё не подавали заявку.\n\n"
            "Нажмите кнопку <b>«Загрузить результаты»</b> чтобы подать.",
            parse_mode="HTML",
        )
        return

    category_icon = CATEGORIES.get(row["category"], "⚪️")
    submitted_at = row["submitted_at"].strftime("%d.%m.%Y %H:%M")

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

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.web_app_data.data
    logger.info("Received web_app_data: %s", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Invalid JSON from WebApp")
        await update.message.reply_text("Ошибка обработки заявки. Попробуйте ещё раз.")
        return

    user = update.effective_user
    username = f"@{user.username}" if user.username else user.full_name
    category_icon = CATEGORIES.get(data.get("category", ""), "⚪️")

    # Проверка дубля
    if await has_submitted(user.id):
        await update.message.reply_text(
            "⚠️ <b>Вы уже подали заявку.</b>\n\n"
            "Если хотите внести изменения — свяжитесь с организаторами @innasevastopol",
            parse_mode="HTML",
        )
        return

    # Сохранить в PostgreSQL
    await save_submission(user.id, data, username)

    # Сохранить в Notion
    try:
        await add_to_notion(data, user)
        logger.info("Saved to Notion: user_id=%s", user.id)
    except Exception as e:
        logger.error("Notion error: %s", e)

    # Отправить организаторам
    username_part = f"@{user.username}" if user.username else f"[{user.full_name}](tg://user?id={user.id})"
    organizer_msg = (
        "🏋️ *Новая заявка*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *ФИО:* {data.get('name', '—')}\n"
        f"{category_icon} *Категория:* {data.get('category', '—')}\n"
        f"🔥 *Количество берпи:* {data.get('burpees', '—')}\n"
        f"🎥 *Видео:* {data.get('video', '—')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *Участник:* {username_part} `(id: {user.id})`"
    )
    await context.bot.send_message(
        chat_id=ORGANIZERS_CHAT_ID,
        text=organizer_msg,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    # Подтверждение пользователю
    await update.message.reply_text(
        "✅ <b>Заявка принята!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>ФИО:</b> {data.get('name', '—')}\n"
        f"{category_icon} <b>Категория:</b> {data.get('category', '—')}\n"
        f"🔥 <b>Берпи:</b> {data.get('burpees', '—')}\n"
        f"🎥 <b>Видео:</b> {data.get('video', '—')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Удачи на старте! 💪",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def post_init(app: Application) -> None:
    await init_db()


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mystatus", mystatus))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
