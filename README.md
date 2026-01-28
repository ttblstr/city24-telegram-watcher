# City24 Telegram Watcher

Simple Python+Playwright script to monitor City24 search results and notify a Telegram chat for new listings.

## Setup (local)
1. Copy `config.example.yaml` to `config.yaml` and fill `search_url`, `telegram.bot_token`, `telegram.chat_id`.
2. `python -m venv .venv && source .venv/bin/activate`
3. `pip install -r requirements.txt`
4. `playwright install`
5. Run: `python check_city24.py`

## GitHub Actions
- Add secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in repo Settings → Secrets.
- Push to GitHub. The workflow runs every 30 minutes by default; you can trigger manually.

## Notes
- Respect City24 terms and rate limits.
- If scraping fails, tweak the CSS selectors in `check_city24.py`.
