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

CATEGORIES = {"Новички": "🟢", "Любители": "🟡", "Продвинутые": "🔴"}

notion = NotionClient(auth=NOTION_TOKEN)
db_pool = None


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                user_id BIGINT PRIMARY KEY,
                name    TEXT,
                submitted_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    logger.info("Database ready")


async def has_submitted(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM submissions WHERE user_id = $1", user_id)
        return row is not None


async def save_submission(user_id: int, name: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO submissions (user_id, name) VALUES ($1, $2)",
            user_id, name
        )


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
    await save_submission(user.id, data.get("name", ""))

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
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
