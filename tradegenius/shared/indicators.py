"""
Indicator computation and rule evaluation on OHLC DataFrames.

Shared by the live engine (Windows Python under Wine) and the backtester.
Expects a DataFrame with float columns: open, high, low, close.
Signals are evaluated on CLOSED bars only, so they never repaint.
"""

import numpy as np
import pandas as pd

from .strategy_schema import operand_key, required_operands


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _rma(s, n):
    # Wilder smoothing, as used by MT5/TradingView for RSI and ATR.
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def rsi(close, n):
    delta = close.diff()
    gain = _rma(delta.clip(lower=0), n)
    loss = _rma(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # No losses in the window -> RSI 100; no movement at all -> 50.
    out = out.where(loss != 0, np.where(gain > 0, 100.0, 50.0))
    out.iloc[:n] = np.nan
    return out


def atr(df, n):
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                   axis=1).max(axis=1)
    out = _rma(tr, n)
    out.iloc[:n] = np.nan
    return out


def compute_operand(df, op):
    ind = op["ind"]
    c = df["close"]
    if ind == "PRICE":
        return df[op["field"]]
    if ind == "EMA":
        return _ema(c, op["period"])
    if ind == "SMA":
        return c.rolling(op["period"]).mean()
    if ind == "RSI":
        return rsi(c, op["period"])
    if ind == "ATR":
        return atr(df, op["period"])
    if ind == "MACD":
        macd = _ema(c, op["fast"]) - _ema(c, op["slow"])
        signal = _ema(macd, op["signal"])
        return {"macd": macd, "signal": signal, "hist": macd - signal}[op["line"]]
    if ind == "BB":
        mid = c.rolling(op["period"]).mean()
        sd = c.rolling(op["period"]).std(ddof=0)
        return {"upper": mid + op["std"] * sd, "middle": mid, "lower": mid - op["std"] * sd}[op["band"]]
    if ind == "STOCH":
        lo = df["low"].rolling(op["k"]).min()
        hi = df["high"].rolling(op["k"]).max()
        raw_k = 100 * (c - lo) / (hi - lo).replace(0, np.nan)
        k = raw_k.rolling(op["smooth"]).mean()
        return k if op["line"] == "k" else k.rolling(op["d"]).mean()
    raise ValueError(f"unknown indicator {ind}")


def compute_indicators(df, strategy):
    df = df.copy()
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    for op in required_operands(strategy):
        key = operand_key(op)
        if key not in df.columns:
            df[key] = compute_operand(df, op)
    return df


def _value(df, op, i):
    if isinstance(op, float):
        return op
    v = df[operand_key(op)].iat[i]
    return None if pd.isna(v) else float(v)


def condition_true(df, cond, i):
    """Evaluate one condition at row i. Missing data -> False (never trade on NaN)."""
    a, b = _value(df, cond["left"], i), _value(df, cond["right"], i)
    if a is None or b is None:
        return False
    op = cond["op"]
    if op == "<":
        return a < b
    if op == ">":
        return a > b
    if op == "<=":
        return a <= b
    if op == ">=":
        return a >= b
    if i < 1:
        return False
    pa, pb = _value(df, cond["left"], i - 1), _value(df, cond["right"], i - 1)
    if pa is None or pb is None:
        return False
    if op == "crosses_above":
        return pa <= pb and a > b
    if op == "crosses_below":
        return pa >= pb and a < b
    # Unknown operators are rejected by the schema; refuse rather than guess.
    return False


def side_signal(df, conditions, i):
    """True only if there is at least one condition and every condition holds."""
    return bool(conditions) and all(condition_true(df, c, i) for c in conditions)


def signal_at(df, strategy, i):
    """'buy', 'sell' or None for row i. Conflicting signals cancel out."""
    buy = side_signal(df, strategy["entry"]["buy"], i)
    sell = side_signal(df, strategy["entry"]["sell"], i)
    if buy and not sell:
        return "buy"
    if sell and not buy:
        return "sell"
    return None


def pip_size(point, digits):
    """Pip = 10 points on fractional-pip quotes (5/3-digit FX, 2-digit metals)."""
    return point * 10 if digits in (2, 3, 5) else point


def stop_levels(strategy, side, entry, atr_values, pip):
    """
    Return (sl, tp) prices. `atr_values` maps ATR period -> value at the signal bar.
    Returns None if a level can't be computed (e.g. ATR not ready).
    """
    sl_spec, tp_spec = strategy["exit"]["sl"], strategy["exit"]["tp"]
    if sl_spec["type"] == "atr":
        a = atr_values.get(sl_spec["period"])
        if not a or a <= 0:
            return None
        sl_dist = a * sl_spec["mult"]
    else:
        sl_dist = sl_spec["pips"] * pip

    if tp_spec["type"] == "rr":
        tp_dist = sl_dist * tp_spec["ratio"]
    elif tp_spec["type"] == "atr":
        a = atr_values.get(tp_spec["period"])
        if not a or a <= 0:
            return None
        tp_dist = a * tp_spec["mult"]
    else:
        tp_dist = tp_spec["pips"] * pip

    if sl_dist <= 0 or tp_dist <= 0:
        return None
    if side == "buy":
        return entry - sl_dist, entry + tp_dist
    return entry + sl_dist, entry - tp_dist


def atr_values_at(df, strategy, i):
    out = {}
    for name in ("sl", "tp"):
        spec = strategy["exit"][name]
        if spec["type"] == "atr":
            v = df[f"ATR_{spec['period']}"].iat[i]
            out[spec["period"]] = None if pd.isna(v) else float(v)
    return out


def in_session(strategy, hhmm):
    """hhmm: 'HH:MM' in broker server time. Sessions may wrap past midnight."""
    s = strategy.get("session")
    if not s:
        return True
    start, end = s["start"], s["end"]
    if start <= end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end
