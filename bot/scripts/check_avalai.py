"""Optional connectivity probe for AvalAI (requires real credentials).

Usage:
  python -m bot.scripts.check_avalai

This is intentionally separate from unit tests.
Success requires instruction-following (expected probe token), not HTTP 200
and not a provider deprecation/migration notice.
"""

from __future__ import annotations

import asyncio
import json
import sys

from bot.config import get_settings
from bot.services.avalai_client import AvalAIClient
from bot.utils.logging import setup_logging


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    client = AvalAIClient(settings)
    result = await client.connectivity_probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    openai_ok = bool(result.get("openai_compatible"))
    native_ok = bool(result.get("native_genai"))
    errors = result.get("errors") or {}
    diagnostics = result.get("diagnostics") or {}

    print(
        f"\nRequested model: {result.get('requested_model')}",
        file=sys.stderr,
    )
    print(
        f"Expected probe token: {result.get('probe_expected_token')}",
        file=sys.stderr,
    )
    print(
        f"Structured text recommendation: {result.get('structured_text_recommendation')}",
        file=sys.stderr,
    )

    if not openai_ok:
        print("\n[openai_compatible] FAILED", file=sys.stderr)
        if errors.get("openai_compatible"):
            print(f"  reason: {errors['openai_compatible']}", file=sys.stderr)
        diag = diagnostics.get("openai_compatible")
        if diag:
            print(
                "  diagnostics: "
                + json.dumps(
                    {
                        "requested_model": diag.get("requested_model"),
                        "response_model": diag.get("response_model"),
                        "is_provider_notice": diag.get("is_provider_notice"),
                        "instruction_followed": diag.get("instruction_followed"),
                        "content_preview": diag.get("content_preview"),
                        "finish_reason": diag.get("finish_reason"),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )

    if not native_ok:
        print("\n[native_genai] FAILED", file=sys.stderr)
        if errors.get("native_genai"):
            print(f"  reason: {errors['native_genai']}", file=sys.stderr)
        diag = diagnostics.get("native_genai")
        if diag:
            print(
                "  diagnostics: "
                + json.dumps(
                    {
                        "requested_model": diag.get("requested_model"),
                        "response_model": diag.get("response_model"),
                        "is_provider_notice": diag.get("is_provider_notice"),
                        "instruction_followed": diag.get("instruction_followed"),
                        "content_preview": diag.get("content_preview"),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )

    if openai_ok and native_ok:
        print(
            "\nBoth AvalAI surfaces followed the probe instruction.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    if native_ok and not openai_ok:
        print(
            "\nNative GenAI works; OpenAI-compatible does not reliably serve this model. "
            "TEXT_GENERATION_BACKEND=auto will fall back to native for structured extraction.",
            file=sys.stderr,
        )
        # Partial success is acceptable for operations when native can extract.
        raise SystemExit(0)

    raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
