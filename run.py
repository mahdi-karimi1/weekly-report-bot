"""Application entrypoint for the Telegram weekly report bot."""

from __future__ import annotations

import sys

from bot.main import run_polling


def main() -> None:
    try:
        run_polling()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
