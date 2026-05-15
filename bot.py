import os
import json
import logging
from dotenv import load_dotenv
from telegram import Update, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ORGANIZERS_CHAT_ID = os.environ["ORGANIZERS_CHAT_ID"]
WEBAPP_URL = os.environ["WEBAPP_URL"]

CATEGORIES = {"Новички": "🟢", "Любители": "🟡", "Продвинутые": "🔴"}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[
        KeyboardButton(
            text="📋 Загрузить результаты",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    ]]
    await update.message.reply_text(
        "Битва за Херсонес 2026 🔥n\n"
        "Нажмите кнопку Загрузить результаты, чтобы перейти к заполнению данных отборочного комплекса.",
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
    username_part = f"@{user.username}" if user.username else f"[{user.full_name}](tg://user?id={user.id})"
    category_icon = CATEGORIES.get(data.get("category", ""), "⚪️")

    organizer_msg = (
        "🏋️ *Новая заявка на соревнование*\n"
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

    await update.message.reply_text(
        "✅ Заявка принята! Организаторы свяжутся с вами в ближайшее время.",
    )


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
