"""User verification and access-control tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from bot.config import Settings
from bot.handlers.user_management import access_control, verify_command
from bot.models.database import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'users.db'}")
    database.create_tables()
    return database


@pytest.mark.asyncio
async def test_unverified_user_is_blocked_with_chat_id(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.get_or_create_user(42, None, None)
    monkeypatch.setattr("bot.handlers.user_management.get_database", lambda: db)
    message = SimpleNamespace(
        text="please start my report",
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(user_data={})

    with pytest.raises(ApplicationHandlerStop):
        await access_control(update, context)

    message.reply_text.assert_awaited_once()
    assert "42" in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unverified_user_can_start_registration(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bot.handlers.user_management.get_database", lambda: db)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(text="/start", reply_text=AsyncMock()),
        callback_query=None,
    )

    await access_control(update, SimpleNamespace(user_data={}))


@pytest.mark.asyncio
async def test_admin_can_verify_by_chat_id(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bot.handlers.user_management.get_database", lambda: db)
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=100),
        effective_message=message,
    )
    context = SimpleNamespace(
        args=["42"],
        application=SimpleNamespace(
            bot_data={"settings": Settings(ADMIN_CHAT_IDS="100")}
        ),
    )

    await verify_command(update, context)

    user = db.get_user_by_telegram_id(42)
    assert user is not None and user.is_verified
    assert "42" in message.reply_text.await_args.args[0]


def test_existing_self_verified_users_require_admin_approval(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY, telegram_id BIGINT UNIQUE, username VARCHAR(255), "
            "display_name VARCHAR(255), phone_number VARCHAR(32), "
            "department VARCHAR(255), is_verified BOOLEAN DEFAULT 0, "
            "registered_at DATETIME)"
        )
        connection.execute(
            "INSERT INTO users (telegram_id, display_name, department, is_verified) "
            "VALUES (42, 'User', 'Department', 1)"
        )

    db = Database(f"sqlite:///{database_path}")
    db.create_tables()
    user = db.get_user_by_telegram_id(42)

    assert user is not None
    assert not user.is_verified
    assert not db.is_user_verified(42)
