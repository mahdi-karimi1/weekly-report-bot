"""Admin commands and access control for registered Telegram users."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from bot.handlers.onboarding import VERIFY_REQUIRED_MESSAGE
from bot.models.database import get_database
from bot.utils.logging import get_logger

logger = get_logger(__name__)

ADMIN_COMMANDS = frozenset({"/verify", "/unverify", "/users"})


async def access_control(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop

    if get_database().is_user_verified(user.id):
        return

    message = update.effective_message
    text = message.text if message is not None else None
    if text:
        command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
        if command == "/start" or command in ADMIN_COMMANDS:
            return
        if context.user_data.get("onboarding_in_progress") and not text.startswith("/"):
            return

    if update.callback_query is not None:
        await update.callback_query.answer(
            "دسترسی شما هنوز توسط مدیر تأیید نشده است.",
            show_alert=True,
        )
    elif message is not None:
        await message.reply_text(
            VERIFY_REQUIRED_MESSAGE.format(chat_id=user.id),
        )
    raise ApplicationHandlerStop


def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    settings = context.application.bot_data["settings"]
    return chat is not None and chat.id in settings.admin_ids


async def verify_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _set_verification(update, context, verified=True)


async def unverify_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await _set_verification(update, context, verified=False)


async def _set_verification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    verified: bool,
) -> None:
    message = update.effective_message
    if message is None:
        return
    if not _is_admin(update, context):
        await message.reply_text("این دستور فقط برای مدیران مجاز است.")
        return

    args = context.args
    if len(args) != 1:
        command = "verify" if verified else "unverify"
        await message.reply_text(f"روش استفاده: /{command} <chat_id>")
        return
    try:
        chat_id = int(args[0])
    except ValueError:
        await message.reply_text("شناسهٔ چت باید یک عدد صحیح باشد.")
        return
    if chat_id <= 0:
        await message.reply_text("برای تأیید کاربر، chat_id خصوصی و مثبت لازم است.")
        return

    db = get_database()
    user = db.set_user_verification(chat_id, verified)
    state = "تأیید شد" if verified else "تأیید او لغو شد"
    profile_note = (
        ""
        if user.display_name and user.department
        else "\nکاربر باید ابتدا در گفت‌وگوی خصوصی ربات دستور /start را اجرا و پروفایلش را تکمیل کند."
    )
    await message.reply_text(f"کاربر {chat_id} {state}.{profile_note}")
    logger.info("Admin changed user verification chat_id=%s verified=%s", chat_id, verified)


async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    if not _is_admin(update, context):
        await message.reply_text("این دستور فقط برای مدیران مجاز است.")
        return

    users = get_database().list_users(limit=20)
    if not users:
        await message.reply_text("هنوز کاربری در ربات ثبت نشده است.")
        return
    lines = ["کاربران اخیر (حداکثر ۲۰ مورد):"]
    for user in users:
        label = (user.display_name or user.username or "بدون نام")[:60]
        status = "تأییدشده" if user.is_verified else "در انتظار تأیید"
        lines.append(f"{user.telegram_id} | {label} | {status}")
    await message.reply_text("\n".join(lines))
