"""
Per-account trading engine. Runs under Windows Python inside Wine, one
process per MT5 account, started and supervised by tradegenius.worker_manager.

Exit codes: 0 = stopped on request, 2 = fatal (do not restart), other = crash (restart).

Environment:
  ACCOUNT_ID, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH (terminal64.exe, portable copy)
  STRATEGY_JSON, ALLOW_REAL (0/1), BACKTEST_BARS
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  STATUS_FILE, CONTROL_FILE, APP_ROOT (dir containing the tradegenius package)
"""

import hashlib
import html
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.getenv("APP_ROOT", r"Z:\app"))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from tradegenius.shared.backtest import format_report_uz, merge_results, run_backtest  # noqa: E402
from tradegenius.shared.indicators import (atr_values_at, compute_indicators, in_session,  # noqa: E402
                                           pip_size, signal_at, stop_levels)
from tradegenius.shared.strategy_schema import validate_strategy, warmup_bars  # noqa: E402

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover - only available on Windows/Wine
    mt5 = None

EXIT_OK, EXIT_FATAL, EXIT_CRASH = 0, 2, 1
LOOP_SECONDS = 5
HEARTBEAT_SECONDS = 20

ACCOUNT_ID = int(os.getenv("ACCOUNT_ID", "0"))
MAGIC = 880000 + ACCOUNT_ID % 100000
STATUS_FILE = os.getenv("STATUS_FILE", "status.json")
CONTROL_FILE = os.getenv("CONTROL_FILE", "control.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEFRAMES = {}
if mt5:
    TIMEFRAMES = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
                  "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
                  "D1": mt5.TIMEFRAME_D1}


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def esc(s):
    return html.escape(str(s), quote=False)


# ------------------------------------------------------------------ io

def notify(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    login = os.getenv("MT5_LOGIN", "")
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": f"<b>[{esc(login)}]</b> {text}",
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15)
    except Exception as e:
        log(f"telegram error: {e}")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default if default is not None else {}


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class Status:
    def __init__(self):
        self.data = read_json(STATUS_FILE, {})
        self.data.update({"pid": os.getpid(), "state": "starting", "message": "Ishga tushmoqda"})
        self.last_write = 0

    def set(self, **kw):
        self.data.update(kw)

    def flush(self, force=False):
        if force or time.time() - self.last_write >= HEARTBEAT_SECONDS:
            self.data["ts"] = int(time.time())
            write_json(STATUS_FILE, self.data)
            self.last_write = time.time()


def stop_requested():
    return read_json(CONTROL_FILE, {}).get("state") == "stop"


def fatal(status, message):
    log(f"FATAL: {message}")
    notify(f"⛔ {message}")
    status.set(state="error", message=message)
    status.flush(force=True)
    if mt5:
        mt5.shutdown()
    sys.exit(EXIT_FATAL)


# ------------------------------------------------------------- mt5 helpers

def connect(status):
    login = int(os.environ["MT5_LOGIN"])
    kwargs = dict(path=os.environ["MT5_PATH"], login=login, password=os.environ["MT5_PASSWORD"],
                  server=os.environ["MT5_SERVER"], timeout=120000, portable=True)
    last = None
    for attempt in range(1, 4):
        if mt5.initialize(**kwargs):
            acc = mt5.account_info()
            if acc and acc.login == login:
                return acc
            last = "hisob ma'lumoti olinmadi"
        else:
            last = mt5.last_error()
        log(f"connect attempt {attempt} failed: {last}")
        mt5.shutdown()
        time.sleep(15 * attempt)
    code = last[0] if isinstance(last, tuple) else None
    if code == -6:  # authorization failed
        fatal(status, "MT5 ga kirib bo'lmadi: login, parol yoki server nomi noto'g'ri. "
                      "Hisobni o'chirib, ma'lumotlarni qayta kiriting.")
    raise RuntimeError(f"MT5 connection failed: {last}")


def resolve_symbol(name):
    candidates = [name, name + "m", name + ".m", name + "c", name + ".a", name + "-ECN", name + "."]
    if name == "XAUUSD":
        candidates += ["GOLD", "GOLDm", "XAUUSDm"]
    for sym in candidates:
        info = mt5.symbol_info(sym)
        if info:
            if not info.visible:
                mt5.symbol_select(sym, True)
            return sym
    # Fall back to any broker symbol that starts with the requested name.
    for info in mt5.symbols_get(f"{name}*") or []:
        mt5.symbol_select(info.name, True)
        return info.name
    return None


def filling_mode(info):
    # symbol_info.filling_mode is a bitmask: 1 = FOK allowed, 2 = IOC allowed.
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def calc_lot(info, balance, risk_pct, sl_dist):
    """Largest lot whose SL loss is <= risk. None if even the minimum lot risks more."""
    tick_size = info.trade_tick_size or info.point
    tick_value = info.trade_tick_value
    if tick_size <= 0 or tick_value <= 0 or sl_dist <= 0:
        return None
    loss_per_lot = sl_dist / tick_size * tick_value
    risk_money = balance * risk_pct / 100.0
    step = info.volume_step or 0.01
    lot = math.floor(risk_money / loss_per_lot / step + 1e-9) * step
    lot = min(lot, info.volume_max)
    if lot < info.volume_min:
        return None
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(lot, decimals)


def server_dt(ts):
    # MT5 timestamps are broker server time expressed as epoch seconds.
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def my_positions(symbol=None):
    pos = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    return [p for p in (pos or []) if p.magic == MAGIC]


def close_position(p, comment):
    tick = mt5.symbol_info_tick(p.symbol)
    info = mt5.symbol_info(p.symbol)
    if not tick or not info:
        return False
    is_buy = p.type == mt5.POSITION_TYPE_BUY
    req = {"action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol,
           "volume": p.volume, "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
           "price": tick.bid if is_buy else tick.ask, "deviation": 30, "magic": MAGIC,
           "comment": comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_mode(info)}
    res = mt5.order_send(req)
    return bool(res and res.retcode == mt5.TRADE_RETCODE_DONE)


# --------------------------------------------------------------- engine

class Engine:
    def __init__(self, strategy, status):
        self.s = strategy
        self.status = status
        self.tf = TIMEFRAMES[strategy["timeframe"]]
        self.bars = warmup_bars(strategy) + 50
        self.symbols = []
        self.last_bar = {}
        self.halted_day = None
        self.error_bar = {}
        self.last_deal_check = 0

    def setup(self):
        missing = []
        for name in self.s["symbols"]:
            sym = resolve_symbol(name)
            if sym:
                self.symbols.append(sym)
            else:
                missing.append(name)
        if missing:
            notify(f"⚠️ Brokeringizda topilmadi: {esc(', '.join(missing))}")
        if not self.symbols:
            fatal(self.status, "Strategiyadagi birorta aktiv brokeringizda topilmadi.")

    def backtest(self, n_bars):
        per_symbol = {}
        for sym in self.symbols:
            rates = mt5.copy_rates_from_pos(sym, self.tf, 1, n_bars)
            info = mt5.symbol_info(sym)
            if rates is None or len(rates) < warmup_bars(self.s) + 50 or not info:
                continue
            per_symbol[sym] = run_backtest(rates, self.s, info.point, info.digits, info.spread)
        if not per_symbol:
            return None
        return merge_results(per_symbol, self.s)

    def account_snapshot(self):
        acc = mt5.account_info()
        if not acc:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        positions = [{"symbol": p.symbol, "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                      "volume": p.volume, "profit": round(p.profit, 2)} for p in my_positions()]
        self.status.set(balance=acc.balance, equity=acc.equity, currency=acc.currency,
                        positions=positions)
        return acc

    def check_daily_loss(self, acc):
        tick_time = None
        for sym in self.symbols:
            t = mt5.symbol_info_tick(sym)
            if t:
                tick_time = t.time
                break
        day = server_dt(tick_time or time.time()).strftime("%Y-%m-%d")
        if self.status.data.get("day") != day:
            self.status.set(day=day, day_start_balance=acc.balance)
            self.halted_day = None
        start = self.status.data.get("day_start_balance") or acc.balance
        limit = self.s["risk"]["max_daily_loss_percent"]
        if self.halted_day != day and acc.equity <= start * (1 - limit / 100.0):
            self.halted_day = day
            notify(f"🛑 Kunlik zarar limiti ({limit:g}%) ga yetildi. Bugun yangi savdo ochilmaydi.")
        return self.halted_day == day

    def manage_breakeven(self):
        be = self.s["exit"]["breakeven_at_r"]
        if not be:
            return
        for p in my_positions():
            if not p.sl:
                continue
            tick = mt5.symbol_info_tick(p.symbol)
            if not tick:
                continue
            # Until moved, the current SL is the original SL, so this is the initial risk.
            risk = abs(p.price_open - p.sl)
            if p.type == mt5.POSITION_TYPE_BUY:
                if p.sl >= p.price_open or risk <= 0 or tick.bid - p.price_open < be * risk:
                    continue
            else:
                if p.sl <= p.price_open or risk <= 0 or p.price_open - tick.ask < be * risk:
                    continue
            res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket,
                                  "symbol": p.symbol, "sl": p.price_open, "tp": p.tp})
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                notify(f"🛡️ {esc(p.symbol)}: Stop Loss kirish narxiga ko'chirildi (breakeven).")

    def _closing_deals(self):
        # Deal times are broker server time, which can be hours away from UTC, so query a wide
        # window and dedupe by ticket instead of trusting a narrow time range.
        now = time.time()
        deals = mt5.history_deals_get(datetime.fromtimestamp(now - 2 * 86400, tz=timezone.utc),
                                      datetime.fromtimestamp(now + 2 * 86400, tz=timezone.utc)) or []
        return [d for d in deals if d.magic == MAGIC and d.entry == mt5.DEAL_ENTRY_OUT]

    def report_closed_deals(self):
        if time.time() - self.last_deal_check < 15:
            return
        self.last_deal_check = time.time()
        deals = self._closing_deals()
        if "reported_deals" not in self.status.data:
            # First run on this account: don't announce trades that closed before we started.
            self.status.set(reported_deals=sorted(d.ticket for d in deals)[-500:])
            return
        seen = set(self.status.data["reported_deals"])
        for d in deals:
            if d.ticket in seen:
                continue
            seen.add(d.ticket)
            pnl = d.profit + d.commission + d.swap
            icon = "✅" if pnl >= 0 else "❌"
            notify(f"{icon} {esc(d.symbol)} savdosi yopildi: <b>{pnl:+.2f}</b>")
        self.status.set(reported_deals=sorted(seen)[-500:])

    def process_symbol(self, sym, halted):
        rates = mt5.copy_rates_from_pos(sym, self.tf, 0, self.bars)
        if rates is None or len(rates) < self.bars - 5:
            return
        closed_time = int(rates[-2]["time"])
        if self.last_bar.get(sym) == closed_time:
            return
        self.last_bar[sym] = closed_time

        df = compute_indicators(pd.DataFrame(rates), self.s)
        i = len(df) - 2  # last CLOSED bar
        sig = signal_at(df, self.s, i)
        if not sig:
            return

        open_here = my_positions(sym)
        if self.s["exit"]["close_on_opposite"]:
            for p in open_here:
                side = "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell"
                if side != sig and close_position(p, "TG opposite"):
                    notify(f"↩️ {esc(sym)}: qarama-qarshi signal — pozitsiya yopildi.")
            open_here = my_positions(sym)

        if open_here or halted:
            return
        if len(my_positions()) >= self.s["risk"]["max_open_trades"]:
            return
        tick = mt5.symbol_info_tick(sym)
        info = mt5.symbol_info(sym)
        if not tick or not info:
            return
        if not in_session(self.s, server_dt(tick.time).strftime("%H:%M")):
            return
        self.open_trade(sym, sig, df, i, tick, info)

    def open_trade(self, sym, side, df, i, tick, info):
        entry = tick.ask if side == "buy" else tick.bid
        levels = stop_levels(self.s, side, entry, atr_values_at(df, self.s, i),
                             pip_size(info.point, info.digits))
        if not levels:
            return
        sl, tp = levels
        min_dist = (info.trade_stops_level or 0) * info.point
        if min_dist and (abs(entry - sl) < min_dist or abs(tp - entry) < min_dist):
            notify(f"⚠️ {esc(sym)}: SL/TP brokerning minimal masofasidan yaqin — savdo o'tkazib yuborildi.")
            return
        acc = mt5.account_info()
        lot = calc_lot(info, acc.balance, self.s["risk"]["risk_percent"], abs(entry - sl))
        if lot is None:
            if self.error_bar.get((sym, "lot")) != self.last_bar.get(sym):
                self.error_bar[(sym, "lot")] = self.last_bar.get(sym)
                notify(f"⚠️ {esc(sym)}: balans bu risk uchun juda kichik (minimal lot ham "
                       f"{self.s['risk']['risk_percent']:g}% dan ko'p risk qiladi). Savdo ochilmadi.")
            return
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": lot,
               "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
               "price": entry, "sl": round(sl, info.digits), "tp": round(tp, info.digits),
               "deviation": 30, "magic": MAGIC, "comment": "TradeGenius",
               "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling_mode(info)}
        res = mt5.order_send(req)
        if res is None:
            notify(f"❗ {esc(sym)}: buyruq yuborilmadi ({esc(mt5.last_error())})")
            return
        if res.retcode != mt5.TRADE_RETCODE_DONE:
            hint = " — MT5 da AutoTrading o'chiq." if res.retcode == 10027 else ""
            notify(f"❗ {esc(sym)}: broker rad etdi, kod {res.retcode} ({esc(res.comment)}){hint}")
            return
        icon = "🟢 BUY" if side == "buy" else "🔴 SELL"
        notify(f"{icon} <b>{esc(sym)}</b> ochildi\nNarx: {res.price or entry} | Lot: {lot}\n"
               f"SL: {sl:.{info.digits}f} | TP: {tp:.{info.digits}f}")

    def run(self):
        while True:
            if stop_requested():
                self.status.set(state="stopped", message="To'xtatildi")
                self.status.flush(force=True)
                log("stop requested")
                return
            acc = self.account_snapshot()
            halted = self.check_daily_loss(acc)
            self.manage_breakeven()
            self.report_closed_deals()
            for sym in self.symbols:
                try:
                    self.process_symbol(sym, halted)
                except Exception as e:
                    log(f"{sym}: {e}\n{traceback.format_exc()}")
            self.status.set(state="halted" if halted else "running",
                            message="Kunlik limit — kutmoqda" if halted else "Ishlamoqda")
            self.status.flush()
            time.sleep(LOOP_SECONDS)


def main():
    status = Status()
    status.flush(force=True)
    if mt5 is None:
        fatal(status, "MetaTrader5 Python paketi topilmadi (engine faqat Wine/Windows ichida ishlaydi).")

    try:
        strategy = validate_strategy(json.loads(os.environ["STRATEGY_JSON"]))
    except Exception as e:
        fatal(status, f"Strategiya noto'g'ri: {e}")

    acc = connect(status)
    is_demo = acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    status.set(login=str(acc.login), is_demo=is_demo, server=acc.server)
    if not is_demo and os.getenv("ALLOW_REAL") != "1":
        fatal(status, "Bu REAL hisob. Real pulda savdo qilish uchun botda hisob sozlamasidan "
                      "\"Real hisobga ruxsat\" ni yoqing.")
    term = mt5.terminal_info()
    if term is not None and not term.trade_allowed:
        fatal(status, "MT5 terminalida AutoTrading o'chiq — savdo ochib bo'lmaydi. "
                      "Administratorga xabar bering.")
    if not acc.trade_expert:
        fatal(status, "Brokeringiz bu hisobda avtomatik savdoga ruxsat bermaydi.")

    engine = Engine(strategy, status)
    engine.setup()

    shash = hashlib.sha256(json.dumps(strategy, sort_keys=True).encode()).hexdigest()[:16]
    if status.data.get("backtest_hash") != shash:
        notify(f"🔗 Hisob ulandi ({'DEMO' if is_demo else '⚠️ REAL'}), balans: "
               f"<b>{acc.balance:.2f} {esc(acc.currency)}</b>\n"
               f"Aktivlar: {esc(', '.join(engine.symbols))}\n⏳ Backtest hisoblanmoqda...")
        try:
            res = engine.backtest(int(os.getenv("BACKTEST_BARS", "3000")))
            notify(format_report_uz(res) if res else "Backtest uchun tarixiy ma'lumot yetarli emas.")
        except Exception as e:
            log(f"backtest failed: {e}\n{traceback.format_exc()}")
            notify("Backtest hisoblab bo'lmadi, savdo baribir boshlanadi.")
        status.set(backtest_hash=shash)
        notify(f"🚀 <b>{esc(strategy['name'])}</b> ishga tushdi. Har bir yopilgan "
               f"{strategy['timeframe']} shamda signal tekshiriladi.")
    else:
        log("restart: skipping backtest")

    status.set(state="running", message="Ishlamoqda")
    status.flush(force=True)
    engine.run()
    mt5.shutdown()
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"crash: {e}\n{traceback.format_exc()}")
        if mt5:
            mt5.shutdown()
        sys.exit(EXIT_CRASH)
