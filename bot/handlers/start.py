""" /start command handler. """

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.models.database import get_database
from bot.utils.logging import get_logger

logger = get_logger(__name__)

WELCOME_TEXT = (
    "سلام! به ربات گزارش هفتگی خوش آمدید.\n\n"
    "گزارش فعالیت‌های هفته‌تان را به صورت متن فارسی تایپ کنید یا پیام صوتی بفرستید.\n"
    "پس از استخراج اطلاعات، پیش‌نویس گزارش را "
    "برای تأیید یا ویرایش دریافت می‌کنید و در صورت تأیید، فایل Excel ارسال می‌شود."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return

    user = update.effective_user
    db = get_database()
    display_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    db.get_or_create_user(
        telegram_id=user.id,
        username=user.username,
        display_name=display_name or None,
    )
    logger.info("User started bot telegram_id=%s", user.id)
    await update.message.reply_text(WELCOME_TEXT)
