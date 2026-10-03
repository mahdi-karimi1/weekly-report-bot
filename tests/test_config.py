"""Config and AvalAI client unit tests (no network)."""

from __future__ import annotations

import pytest

from bot.config import Settings
from bot.services.avalai_client import _openai_compatible_schema


def test_openai_base_url_gets_v1() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        AVALAI_API_KEY="y",
        AVALAI_BASE_URL="https://api.avalai.ir/",
        AVALAI_OPENAI_BASE_URL="https://api.avalai.ir",
    )
    assert settings.avalai_base_url == "https://api.avalai.ir"
    assert settings.avalai_openai_base_url == "https://api.avalai.ir/v1"


def test_openai_base_url_not_double_v1() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="x",
        AVALAI_API_KEY="y",
        AVALAI_OPENAI_BASE_URL="https://api.avalai.ir/v1/",
    )
    assert settings.avalai_openai_base_url == "https://api.avalai.ir/v1"


def test_require_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AVALAI_API_KEY", raising=False)
    settings = Settings(_env_file=None)
    with pytest.raises(ValueError):
        settings.require_telegram_token()
    with pytest.raises(ValueError):
        settings.require_avalai_key()


def test_admin_chat_ids_are_parsed_and_validated() -> None:
    settings = Settings(ADMIN_CHAT_IDS="123456, -100123 ")
    assert settings.admin_ids == frozenset({123456, -100123})
    assert settings.require_admin_ids() == frozenset({123456, -100123})

    with pytest.raises(ValueError):
        Settings(ADMIN_CHAT_IDS="not-a-chat-id")
    with pytest.raises(ValueError, match="ADMIN_CHAT_IDS is missing"):
        Settings().require_admin_ids()


def test_schema_normalization_sets_additional_properties() -> None:
    schema = {
        "type": "object",
        "properties": {
            "employee_name": {"type": ["string", "null"]},
            "activities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"activity_description": {"type": "string"}},
                },
            },
        },
    }
    normalized = _openai_compatible_schema(schema)
    assert normalized["additionalProperties"] is False
    assert "employee_name" in normalized["required"]
    assert normalized["properties"]["activities"]["items"]["additionalProperties"] is False
