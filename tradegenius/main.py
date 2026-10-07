"""Worker entry point: Telegram bot + engine supervisor in one process."""

import json
import logging
import signal
import sys

from .bot import Bot
from .config import settings
from .db import DB
from .telegram_api import Telegram, TelegramError
from .worker_manager import WorkerManager

COMMANDS = [
    {"command": "menu", "description": "Asosiy menyu"},
    {"command": "status", "description": "Botlarim holati"},
    {"command": "help", "description": "Yordam"},
    {"command": "cancel", "description": "Bekor qilish"},
]


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("tradegenius")

    problems = settings.problems()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        sys.exit(1)
    if not settings.admin_ids:
        log.warning("ADMIN_IDS is empty: send /id to the bot, then set ADMIN_IDS and redeploy")

    db = DB(settings.db_path)
    tg = Telegram(settings.telegram_bot_token)
    try:
        me = tg.call("getMe")
        tg.call("setMyCommands", commands=json.dumps(COMMANDS))
    except TelegramError as e:
        log.error("Telegram token check failed: %s", e)
        sys.exit(1)
    log.info("bot @%s ready; billing=%s engines=%s", me.get("username"),
             settings.billing_enabled, settings.engines_enabled)

    manager = WorkerManager(db, tg, settings)

    def shutdown(signum, _frame):
        # Render sends SIGTERM on every deploy. Stop engines cleanly; open positions keep their
        # SL/TP on the broker side, and engines restart automatically on the next boot.
        log.info("signal %s: stopping engines", signum)
        manager.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    manager.start()
    if settings.setup_failed:
        boot = ("⚠️ TradeGenius ishga tushdi, lekin Wine/MT5 o'rnatilmadi — savdo botlari "
                "ishlamaydi. Render loglarida [setup] qatorlarini tekshiring va qayta deploy qiling.")
    elif not settings.engines_enabled:
        boot = "ℹ️ TradeGenius ishga tushdi (ENGINES_ENABLED=false — savdo botlari o'chiq)."
    else:
        boot = "✅ TradeGenius ishga tushdi."
    boot += f" Billing: {'yoqilgan' if settings.billing_enabled else 'ochiq (bepul sinov)'}."
    for admin in settings.admin_ids:
        tg.send(admin, boot)
    Bot(db, tg, manager, settings).run()


if __name__ == "__main__":
    main()
