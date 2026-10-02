"""Regression tests for Python 3.14 event-loop startup behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from telegram.ext import CallbackQueryHandler, ConversationHandler

from bot.config import Settings
from bot.main import create_application, ensure_event_loop, run_polling


@pytest.fixture(autouse=True)
def _restore_event_loop() -> None:
    """Keep the process event-loop state stable across tests."""
    previous: asyncio.AbstractEventLoop | None
    try:
        previous = asyncio.get_event_loop()
        if previous.is_closed():
            previous = None
    except RuntimeError:
        previous = None
    yield
    try:
        current = asyncio.get_event_loop()
    except RuntimeError:
        current = None
    if current is not None and current is not previous and not current.is_closed():
        current.close()
    if previous is not None and not previous.is_closed():
        asyncio.set_event_loop(previous)
    else:
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_ensure_event_loop_creates_loop_when_missing() -> None:
    """Simulate Python 3.14 main-thread behavior: no current event loop."""
    asyncio.set_event_loop(None)  # type: ignore[arg-type]

    loop, created = ensure_event_loop()
    assert created is True
    assert asyncio.get_event_loop() is loop
    assert not loop.is_closed()
    assert not loop.is_running()


def test_ensure_event_loop_reuses_existing_open_loop() -> None:
    existing = asyncio.new_event_loop()
    asyncio.set_event_loop(existing)
    loop, created = ensure_event_loop()
    assert created is False
    assert loop is existing


def test_ensure_event_loop_replaces_closed_loop() -> None:
    closed = asyncio.new_event_loop()
    asyncio.set_event_loop(closed)
    closed.close()

    loop, created = ensure_event_loop()
    assert created is True
    assert loop is not closed
    assert not loop.is_closed()


def test_run_polling_installs_event_loop_before_ptb(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_polling must set a loop before Application.run_polling is invoked."""
    seen: dict[str, object] = {}

    def fake_run_polling(*args: object, **kwargs: object) -> None:
        loop = asyncio.get_event_loop()
        seen["loop"] = loop
        seen["close_loop"] = kwargs.get("close_loop")
        assert not loop.is_closed()

    app_mock = MagicMock()
    app_mock.run_polling = MagicMock(side_effect=fake_run_polling)

    monkeypatch.setattr(
        "bot.main.get_settings",
        lambda: MagicMock(
            model="gemini-3.1-pro-preview",
            avalai_openai_base_url="https://api.avalai.ir/v1",
            avalai_base_url="https://api.avalai.ir",
            log_level="INFO",
        ),
    )
    monkeypatch.setattr("bot.main.setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr("bot.main.create_application", lambda settings: app_mock)

    asyncio.set_event_loop(None)  # type: ignore[arg-type]
    run_polling()

    assert seen["close_loop"] is False
    app_mock.run_polling.assert_called_once()
    loop = seen["loop"]
    assert isinstance(loop, asyncio.AbstractEventLoop)
    assert loop.is_closed()


def test_application_registers_onboarding_and_suggestion_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bot.main.setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("bot.main.init_database", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("bot.main.build_services", lambda _settings: {})
    settings = Settings(TELEGRAM_BOT_TOKEN="test-token", AVALAI_API_KEY="test-key")

    application = create_application(settings)
    handlers = application.handlers[0]

    assert any(isinstance(handler, ConversationHandler) for handler in handlers)
    patterns = [
        handler.pattern.pattern
        for handler in handlers
        if isinstance(handler, CallbackQueryHandler)
    ]
    assert "^sugg:" in patterns
    assert "^dashboard:" in patterns
