import os
import sys
import json
import time
import requests
import urllib3
from datetime import datetime
import pandas as pd
import numpy as np

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Try loading MT5 library (runs inside Wine or Windows)
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None
    print("[!] Warning: MetaTrader5 package not available in current environment (runs in Wine/Windows)")

# Load Configuration from environment or local strategy.json
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8666134052:AAHngxXJsc_0eE1-V8WPCmmh_qWjvfr3g0Q")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6220938104")
STRATEGY_JSON_STR = os.getenv("STRATEGY_JSON", "")

def load_strategy():
    if STRATEGY_JSON_STR:
        try:
            return json.loads(STRATEGY_JSON_STR)
        except Exception as e:
            print(f"[-] Error parsing STRATEGY_JSON env: {e}")
    if os.path.exists("strategy.json"):
        with open("strategy.json", "r", encoding="utf-8") as f:
            return json.load(f)
    
    # Default fallback strategy config
    return {
        "strategy_name": "Adaptive Multi-Regime Bot",
        "symbols": ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD"],
        "timeframe": "M15",
        "magic_number": 888111,
        "risk_percent": 1.0,
        "rules": {
            "buy": [
                {"indicator": "RSI_14", "op": "<", "val": 35},
                {"indicator": "EMA_50", "op": ">", "val": "EMA_200"}
            ],
            "sell": [
                {"indicator": "RSI_14", "op": ">", "val": 65},
                {"indicator": "EMA_50", "op": "<", "val": "EMA_200"}
            ]
        },
        "risk_management": {
            "sl_type": "ATR",
            "sl_multiplier": 1.5,
            "tp_ratio": 2.5,
            "breakeven_trigger_ratio": 0.5
        }
    }

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }
        requests.post(url, data=payload, verify=False, timeout=10)
    except Exception as e:
        print(f"[-] Telegram error: {e}")

def resolve_symbol(name):
    if not mt5:
        return name
    candidates = [name, name + "m", name + "c"]
    if name == "XAUUSD":
        candidates.extend(["GOLD", "XAUUSD.a", "XAUUSD.m"])
    elif name == "BTCUSD":
        candidates.extend(["BTCUSDm", "BITCOIN", "BTCUSD.a"])
    for sym in candidates:
        if sym and mt5.symbol_info(sym):
            mt5.symbol_select(sym, True)
            return sym
    return None

def compute_indicators(df):
    df = df.copy()
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)

    # EMAs
    df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()

    # ATR
    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift(1)).abs()
    lc = (df['low'] - df['close'].shift(1)).abs()
    df['tr'] = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df['ATR'] = df['tr'].rolling(14).mean()

    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    df['RSI_14'] = 100 - (100 / (1 + rs))

    return df

def calculate_lot(symbol, entry, sl, risk_percent):
    if not mt5:
        return 0.01
    acc = mt5.account_info()
    sym_info = mt5.symbol_info(symbol)
    risk_usd = acc.balance * (risk_percent / 100.0)
    price_diff = abs(entry - sl)
    tick_size = sym_info.trade_tick_size if sym_info.trade_tick_size > 0 else sym_info.point
    loss_per_lot = (price_diff / tick_size) * sym_info.trade_tick_value
    if loss_per_lot <= 0:
        return sym_info.volume_min
    raw_lot = risk_usd / loss_per_lot
    step = sym_info.volume_step
    lot = round(raw_lot / step) * step
    return max(sym_info.volume_min, min(lot, sym_info.volume_max))

def evaluate_rules(conditions, df):
    last = df.iloc[-1]
    for cond in conditions:
        ind = cond.get("indicator")
        op = cond.get("op")
        val = cond.get("val")

        if ind not in last:
            continue
        left_val = last[ind]
        
        # Compare to either numerical value or another indicator
        if isinstance(val, str) and val in last:
            right_val = last[val]
        else:
            try:
                right_val = float(val)
            except:
                continue

        if op == "<" and not (left_val < right_val):
            return False
        elif op == ">" and not (left_val > right_val):
            return False
        elif op == "<=" and not (left_val <= right_val):
            return False
        elif op == ">=" and not (left_val >= right_val):
            return False
    return True

def manage_open_positions(symbol, magic, breakeven_ratio):
    if not mt5:
        return
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return
    for pos in positions:
        if pos.magic == magic:
            tick = mt5.symbol_info_tick(symbol)
            if not tick:
                continue
            if pos.type == mt5.ORDER_TYPE_BUY:
                target = pos.tp - pos.price_open
                if (tick.bid - pos.price_open) >= (target * breakeven_ratio) and pos.sl < pos.price_open:
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": pos.ticket,
                        "symbol": symbol,
                        "sl": pos.price_open,
                        "tp": pos.tp
                    }
                    mt5.order_send(req)
                    send_telegram(f"🛡️ *{symbol} Breakeven!*\nStop Loss kirish narxiga ko'chirildi.")
            elif pos.type == mt5.ORDER_TYPE_SELL:
                target = pos.price_open - pos.tp
                if (pos.price_open - tick.ask) >= (target * breakeven_ratio) and pos.sl > pos.price_open:
                    req = {
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": pos.ticket,
                        "symbol": symbol,
                        "sl": pos.price_open,
                        "tp": pos.tp
                    }
                    mt5.order_send(req)
                    send_telegram(f"🛡️ *{symbol} Breakeven!*\nStop Loss kirish narxiga ko'chirildi.")

def main():
    cfg = load_strategy()
    print(f"[*] Starting Strategy Engine: {cfg.get('strategy_name')}")
    
    if mt5:
        if not mt5.initialize():
            print(f"[-] MT5 initialization failed: {mt5.last_error()}")
            return
        acc = mt5.account_info()
        start_msg = (
            f"🚀 *SaaS Bulutli Bot Faollashtirildi!*\n\n"
            f"👤 Hisob: `{acc.login}`\n"
            f"💰 Balans: `${acc.balance:.2f}`\n"
            f"🧠 Strategiya: *{cfg.get('strategy_name')}*\n"
            f"📊 Aktivlar: {', '.join([f'`{s}`' for s in cfg.get('symbols', [])])}"
        )
        send_telegram(start_msg)

    magic = cfg.get("magic_number", 888111)
    risk_pct = cfg.get("risk_percent", 1.0)
    risk_mgmt = cfg.get("risk_management", {})
    be_ratio = risk_mgmt.get("breakeven_trigger_ratio", 0.5)

    symbols = [resolve_symbol(s) for s in cfg.get("symbols", []) if resolve_symbol(s)]

    while True:
        try:
            if not mt5:
                time.sleep(10)
                continue

            for sym in symbols:
                manage_open_positions(sym, magic, be_ratio)

                # Check if position already open
                positions = mt5.positions_get(symbol=sym)
                has_pos = any(p.magic == magic for p in positions) if positions else False
                if has_pos:
                    continue

                rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 100)
                if rates is None or len(rates) < 40:
                    continue

                df = compute_indicators(pd.DataFrame(rates))
                tick = mt5.symbol_info_tick(sym)
                if not tick:
                    continue

                atr = df['ATR'].iloc[-1]
                if np.isnan(atr) or atr <= 0:
                    continue

                # Check BUY Rules
                buy_rules = cfg.get("rules", {}).get("buy", [])
                if buy_rules and evaluate_rules(buy_rules, df):
                    entry = tick.ask
                    sl = entry - (atr * risk_mgmt.get("sl_multiplier", 1.5))
                    tp = entry + (abs(entry - sl) * risk_mgmt.get("tp_ratio", 2.5))
                    lot = calculate_lot(sym, entry, sl, risk_pct)
                    req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": sym,
                        "volume": lot,
                        "type": mt5.ORDER_TYPE_BUY,
                        "price": entry,
                        "sl": sl,
                        "tp": tp,
                        "deviation": 25,
                        "magic": magic,
                        "comment": "SaaS Rule BUY",
                        "type_time": mt5.ORDER_TIME_GTC
                    }
                    res = mt5.order_send(req)
                    if res.retcode == mt5.TRADE_RETCODE_DONE:
                        send_telegram(f"🎯 *BUY Savdosi Ochildi!*\n🏷️ `{sym}` | Narx: `{entry}` | Lot: `{lot}` | SL: `{sl:.5f}` | TP: `{tp:.5f}`")

                # Check SELL Rules
                sell_rules = cfg.get("rules", {}).get("sell", [])
                if sell_rules and evaluate_rules(sell_rules, df):
                    entry = tick.bid
                    sl = entry + (atr * risk_mgmt.get("sl_multiplier", 1.5))
                    tp = entry - (abs(sl - entry) * risk_mgmt.get("tp_ratio", 2.5))
                    lot = calculate_lot(sym, entry, sl, risk_pct)
                    req = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": sym,
                        "volume": lot,
                        "type": mt5.ORDER_TYPE_SELL,
                        "price": entry,
                        "sl": sl,
                        "tp": tp,
                        "deviation": 25,
                        "magic": magic,
                        "comment": "SaaS Rule SELL",
                        "type_time": mt5.ORDER_TIME_GTC
                    }
                    res = mt5.order_send(req)
                    if res.retcode == mt5.TRADE_RETCODE_DONE:
                        send_telegram(f"🎯 *SELL Savdosi Ochildi!*\n🏷️ `{sym}` | Narx: `{entry}` | Lot: `{lot}` | SL: `{sl:.5f}` | TP: `{tp:.5f}`")

            time.sleep(15)
        except Exception as e:
            print(f"[-] Execution error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
