"""Personnel onboarding: full name and department."""

from __future__ import annotations

from telegram import ReplyKeyboardRemove, Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.models.database import get_database
from bot.handlers.dashboard import MAIN_KEYBOARD, show_dashboard
from bot.utils.logging import get_logger

logger = get_logger(__name__)

STATE_NAME, STATE_DEPARTMENT = range(2)

VERIFY_REQUIRED_MESSAGE = (
    "برای ثبت گزارش هفتگی ابتدا هویت سازمانی را تکمیل کنید. "
    "دستور /start را بزنید و نام و دپارتمان را وارد کنید."
)


def onboarding_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            STATE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, name_received)
            ],
            STATE_DEPARTMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, department_received)
            ],
        },
        fallbacks=[CommandHandler("start", start_command)],
        allow_reentry=True,
        name="onboarding",
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user is None or update.message is None:
        return ConversationHandler.END

    user = update.effective_user
    db = get_database()
    telegram_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    db.get_or_create_user(user.id, user.username, telegram_name or None)

    if db.is_user_verified(user.id):
        await show_dashboard(update, context)
        logger.info("Verified user started bot telegram_id=%s", user.id)
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 خوش آمدید!\n\n"
        "برای شروع فقط دو مورد را وارد کنید:\n"
        "1️⃣ نام و نام خانوادگی\n"
        "2️⃣ دپارتمان / واحد\n\n"
        "⏱ کمتر از ۳۰ ثانیه\n\n"
        "لطفاً نام و نام خانوادگی کامل خود را بفرستید:",
        reply_markup=ReplyKeyboardRemove(),
    )
    logger.info("Onboarding started telegram_id=%s", user.id)
    return STATE_NAME


async def name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None or not update.message.text:
        return STATE_NAME
    name = update.message.text.strip()
    if len(name) < 3:
        await update.message.reply_text("لطفاً نام و نام خانوادگی معتبر وارد کنید:")
        return STATE_NAME
    context.user_data["onboarding_name"] = name
    await update.message.reply_text(
        f"متشکرم {name}.\n"
        "نام دپارتمان یا واحد کاری را وارد کنید "
        "(مثال: توسعه نرم‌افزار، هوش مصنوعی، منابع انسانی):"
    )
    return STATE_DEPARTMENT


async def department_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user is None or update.message is None or not update.message.text:
        return STATE_DEPARTMENT
    department = update.message.text.strip()
    if len(department) < 2:
        await update.message.reply_text("لطفاً نام دپارتمان را وارد کنید:")
        return STATE_DEPARTMENT

    user = update.effective_user
    name = context.user_data.get("onboarding_name")
    if not name:
        await update.message.reply_text(
            "اطلاعات ناقص است. لطفاً دوباره /start را بزنید."
        )
        return ConversationHandler.END

    db = get_database()
    db.complete_onboarding(
        user.id,
        full_name=str(name),
        department=department,
        username=user.username,
    )
    context.user_data.pop("onboarding_name", None)

    await update.message.reply_text(
        "پروفایل سازمانی ثبت شد.",
        reply_markup=MAIN_KEYBOARD,
    )
    await show_dashboard(update, context)
    logger.info("Onboarding completed telegram_id=%s", user.id)
    return ConversationHandler.END
