# TradeGenius AI

Telegram bot that turns a trader's strategy — written in plain Uzbek/Russian/English, or sent as a
**screenshot** or **PDF** — into a validated rule set, backtests it on the trader's own MT5 history,
and runs it 24/7 on their MetaTrader 5 account.

## How it works

```
Telegram user ──► bot.py ──► ai_parser.py (Gemini: text + images + PDF ──► strategy JSON)
                    │                 │
                    │                 └─► strategy_schema.validate_strategy  (the authority)
                    │
                    ├─► db.py (SQLite on /data: users, strategies, accounts, payments)
                    │
                    └─► worker_manager.py ── one Wine process per account ──►  engine/engine.py
                                                                               (Windows Python + MT5)
                                                                                 │  backtest first
                                                                                 │  then trade on
                                                                                 │  every closed bar
                                                                                 ▼
                                                                         Telegram notifications
```

* **Strategy schema** (`tradegenius/shared/strategy_schema.py`): EMA, SMA, RSI, ATR, MACD, Bollinger,
  Stochastic, price; `< > <= >=`, `crosses_above/below`; SL by ATR or pips; TP by R:R, ATR or pips;
  breakeven; close on opposite signal; risk % per trade (max 5%), max open trades, daily loss limit,
  trading session. Anything else is rejected and the user is told what can't be automated.
* **Signals** are evaluated on closed bars only (no repainting); the backtester and the live engine
  share the same code (`tradegenius/shared/indicators.py`).
* **Safety**: unknown rules never trade; lots are floored so a stop never risks more than the
  configured %; real accounts only trade after the user explicitly allows it; MT5 passwords are
  Fernet-encrypted at rest and the Telegram message containing the password is deleted.
* **Billing**: `BILLING_ENABLED=false` (default) = free test mode for everyone; admins (`ADMIN_IDS`)
  are always free. Turning it on requires an active subscription, sold through Telegram Payments.

## Deploy on Render

1. **Revoke the old bot token.** The previous version of this repo committed a token in plain text;
   it is still in git history. In @BotFather: `/revoke` → pick the bot → use the new token below.
2. Generate an encryption key (keep it safe — changing it makes stored passwords unreadable):
   ```bash
   pip install cryptography && python -m tradegenius.crypto
   ```
3. Get a Gemini API key: https://aistudio.google.com/apikey
4. Render Dashboard → **New → Blueprint** → select this repo. `render.yaml` creates a Docker
   **Background Worker** (plan `standard`, 2 GB) with a 10 GB disk at `/data`.
5. Fill in the secret env vars when prompted: `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS` (your numeric
   Telegram id — send `/id` to the bot after first boot, or ask @userinfobot), `ENCRYPTION_KEY`,
   `GEMINI_API_KEY`. Optional: `ALLOWED_USER_IDS` for a closed beta.
6. First boot installs Windows Python and MetaTrader 5 into the disk (~5–10 min, see logs:
   `[setup] done`). Later deploys skip this.

Capacity: each MT5 terminal under Wine uses roughly 400–600 MB. `MAX_ACTIVE_ENGINES=3` matches the
2 GB plan; raise it together with the plan. Accounts beyond capacity are queued, not dropped.
Run **one instance only** (Telegram polling + SQLite).

### Turning on paid subscriptions later

1. @BotFather → your bot → **Payments** → connect a provider (e.g. Click / Payme / Stripe) and copy
   the provider token.
2. In Render set `PAYMENT_PROVIDER_TOKEN`, `PRICE_AMOUNT` (minor units, `3900` = 39.00),
   `PRICE_CURRENCY`, optionally `TRIAL_DAYS`, then `BILLING_ENABLED=true`.
3. Admin commands: `/admin`, `/users`, `/accounts`, `/grant <user_id> <days>`, `/block <id>`,
   `/unblock <id>`, `/logs <account_id>`.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Run only the bot locally (no Wine/MT5; accounts can be added but engines won't start):

```bash
cp .env.example .env   # fill in values, set DATA_DIR=./data and ENGINES_ENABLED=false
set -a; . ./.env; set +a; .venv/bin/python -m tradegenius.main
```

## Layout

| Path | Role |
| --- | --- |
| `tradegenius/bot.py` | Telegram conversation: terms, strategy (text/photo/PDF/albums), accounts, billing, admin |
| `tradegenius/ai_parser.py` | Gemini prompt + reply validation |
| `tradegenius/shared/` | Schema, indicators, backtester — imported by both bot and engine |
| `tradegenius/worker_manager.py` | Starts/stops/restarts engine processes, crash backoff, capacity |
| `tradegenius/billing.py` | Access rules, subscriptions |
| `engine/engine.py` | Per-account MT5 trading loop (runs in Wine) |
| `docker/` | Entrypoint and one-time Wine/MT5 setup |
| `render.yaml` | Render Blueprint |

## Known limits

* Verified: unit tests (incl. engine logic against a fake MT5 module), Docker image build for
  linux/amd64, Windows Python 3.11 + MetaTrader5 + pandas installing and importing under Wine.
  Not yet verified: the MT5 terminal installer (`mt5setup.exe`) — it crashes under Apple Silicon
  emulation, so its first real run is on Render. Check the first boot logs for `[setup] done`,
  then test end-to-end with a demo account before inviting users.
* Backtests ignore commission and slippage (spread is included).
* Single timeframe per strategy; no candlestick/chart patterns, news, grid or martingale.
