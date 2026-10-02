# Telegram AI Weekly Report Bot

Production-oriented Telegram bot that accepts **typed Persian text or Persian voice messages**, transcribes voice with **Gemini via AvalAI**, extracts a structured weekly report, asks the user to confirm/edit, then fills an Excel template with **openpyxl**.

Local development uses **Telegram long polling** (no webhook, no public domain, no Docker required).

> **Live integrations:** Unit tests mock Telegram and AvalAI. Successful live Telegram or AvalAI operation is **not** claimed until you configure real credentials and verify with a voice message / `python -m bot.scripts.check_avalai`.

---

## Features

1. Accept a weekly report as Persian text or a Telegram voice message (OGG/Opus)  
2. Transcribe Persian speech with Gemini `gemini-3.1-pro-preview` through AvalAI native GenAI; typed text is processed directly  
3. Extract structured JSON (Pydantic-validated) for weekly activities  
4. Inline confirmation: تأیید / ویرایش / لغو  
5. Text corrections applied without discarding unrelated fields  
6. Current Shamsi work week as the default report period, with optional custom start/end dates  
7. Activities with unclear categories default to planned work and remain editable  
8. Add typed notes or voice messages to an existing week's draft over time; duplicate activities are not re-added  
9. Populate a configurable Excel mapping (demo template included)  
10. SQLite persistence (PostgreSQL-ready via `DATABASE_URL`)  
11. Restart-safe pending reports  

---

## AvalAI integration

Verified against [AvalAI docs](https://docs.avalai.ir/en/libraries):

| Purpose | SDK | Base URL | Notes |
|--------|-----|----------|--------|
| Audio transcription / multimodal | Google GenAI (`google-genai`) | `https://api.avalai.ir` (**no** `/v1`) | Native `generateContent` with audio `Part.from_bytes` |
| Structured text extraction | OpenAI-compatible (`openai`) | `https://api.avalai.ir/v1` | Chat completions + JSON schema when supported |

Environment variables keep these bases **separate** so paths are never double-appended.

- Model default: `gemini-3.1-pro-preview` (listed on AvalAI v1beta Gemini models; supports text + audio).  
- Text extraction backend: `TEXT_GENERATION_BACKEND=auto` (default) tries OpenAI-compatible first, then falls back to **native GenAI** on the same model if the OpenAI surface returns a provider notice or fails instruction-following. Set `native` or `openai` to force one surface.  
- Fallback transcription is **off** by default. Enabling it requires explicit `ENABLE_FALLBACK_TRANSCRIPTION=true` and separate `FALLBACK_TRANSCRIPTION_*` settings. The bot will **not** silently switch to another paid provider.

Connectivity probe (needs real key):

```bash
python -m bot.scripts.check_avalai
```

---

## Project layout

```text
bot/
  config.py
  main.py
  handlers/          # Telegram handlers (Persian UX)
  services/          # AvalAI, STT, extraction, Excel
  models/            # Pydantic + SQLAlchemy
  utils/
templates/           # Demo Excel + cell mapping JSON
outputs/             # Generated reports
tests/
deploy/              # Example systemd unit
run.py
```

---

## Local setup

### 1. Python 3.11+

```bash
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:

```bash
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. FFmpeg (optional but recommended)

Used only when audio conversion is needed.

- Windows: install from https://ffmpeg.org/download.html and add to `PATH`  
- macOS: `brew install ffmpeg`  
- Linux: `sudo apt install ffmpeg`

### 4. Configure environment

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```

Set at least:

- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)  
- `AVALAI_API_KEY` — from AvalAI  

### 5. Start the bot (long polling)

```bash
python run.py
```

### 6. Test with a text or voice report

1. Open the bot in Telegram → `/start` and enter your full name and department (no phone number is requested)  
2. Use the dashboard's **📝 ثبت گزارش هفتگی** button; the current Shamsi work week is selected by default. Type your report in Persian or send a Persian voice message; you can also choose another start/end date first. Later, select that week from **📋 گزارش‌های من** or choose its date range again to append more typed text or voice messages to the same report  
3. Review the formatted draft, add more text or voice notes over time, and when ready confirm/edit/cancel it  
4. On confirm, use **📥 دریافت Excel** on the success screen; use **📋 گزارش‌های من** to reopen previous reports and files  

### 7. Logs

Logs go to stdout:

```text
2026-09-26 | INFO | bot.main | Bot is polling…
```

Transcription text is **not** logged by default (`LOG_TRANSCRIPTIONS=false`).

### 8. Tests (no real API keys required)

```bash
pytest
```

Covered without network:

- Pydantic validation / missing fields  
- Extraction + repair with mocked AvalAI  
- User correction merge behavior  
- Demo Excel preservation (headers, formula, column widths, merges)  
- Multi-user report isolation  
- Confirmation keyboard / cancellation persistence  

**Requires real credentials (not in CI unit suite):** live Telegram polling, AvalAI audio/text calls.

---

## Excel template adaptation

The authoritative workbook is a project copy of `FRN..Work-Report.xlsx` at
`templates/weekly_report.xlsx` with cell mapping in `templates/weekly_report_mapping.json`.

Never edit the desktop original in place. Re-copy it into `templates/` if the form changes.

Cell mapping:

| Cell / range | Field |
|---|---|
| B6 | Person name |
| F7 | Report date/range |
| B7 | Department/project |
| B11:B25 / C11:C25 | Planned completed + hours |
| D11:D25 / E11:E25 | Unplanned completed + hours |
| F11:F25 / G11:G25 | Self-initiated + hours |
| H11:H25 / I11:I25 | Next week planned + hours |
| J11:J25 | Problems and suggestions |

Missing values are written as blank (`None`). No placeholders are inserted.
Completed activities whose category cannot be determined default to `planned_completed` (طبق برنامه); users can move them to another category through conversational editing. More than 15 items in a column blocks generation with a Persian review message.

---

## Database

Default: `DATABASE_URL=sqlite:///bot.db`

Later PostgreSQL example:

```env
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/weekly_reports
```

Models: `users`, `reports` with statuses  
`received → transcribed → extracted → awaiting_confirmation → confirmed → completed`  
(plus `failed`, `cancelled`).

Pending `awaiting_confirmation` reports survive process restarts.

---

## Linux deployment (example)

1. Copy the project to `/opt/weekly-report-bot`.  
2. Create venv, install requirements, configure `.env`.  
3. Install FFmpeg on the host.  
4. Adjust `deploy/weekly-report-bot.service` (`User`, paths).  
5. Enable:

```bash
sudo cp deploy/weekly-report-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weekly-report-bot
sudo systemctl status weekly-report-bot
```

`run_polling` handles **SIGTERM** for graceful shutdown. SQLite/pending reports make restarts safe for in-progress confirmations.

Webhooks / reverse proxies are optional later; long polling is enough for a VPS start.

---

## Security notes

- Credentials only via environment / `.env` (gitignored).  
- Users only access their own reports (Telegram user id checks on callbacks).  
- Callback data is validated for ownership and status.  
- Output filenames are deterministic and filesystem-safe; users never supply paths.  
- Temporary audio files are deleted after processing.  
- API calls use timeouts and bounded exponential backoff.  

---

## Development phases (implemented)

| Phase | Status |
|-------|--------|
| 1 Telegram connect + text/voice report input | Implemented |
| 2 AvalAI transcription | Implemented (Gemini native audio) |
| 3 Structured extraction | Implemented |
| 4 Confirm / correct | Implemented |
| 5 Excel demo + mapping | Implemented |
| 6 Persistence, tests, deploy docs | Implemented |
