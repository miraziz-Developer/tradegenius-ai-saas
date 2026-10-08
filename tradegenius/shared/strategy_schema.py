"""
Strategy schema v1: validation and normalization.

Pure stdlib on purpose: imported both by the Linux bot process and by the
Windows Python (Wine) engine process.

A strategy is a JSON object:

{
  "name": "EMA trend + RSI pullback",
  "symbols": ["EURUSD", "XAUUSD"],
  "timeframe": "M15",
  "entry": {
    "buy":  [ {"left": {"ind": "RSI", "period": 14}, "op": "<", "right": 30}, ... ],   # AND
    "sell": [ ... ]                                                                       # AND
  },
  "exit": {
    "sl": {"type": "atr", "mult": 1.5} | {"type": "pips", "pips": 20},
    "tp": {"type": "rr", "ratio": 2.0} | {"type": "atr", "mult": 3} | {"type": "pips", "pips": 40},
    "breakeven_at_r": 1.0 | null,
    "close_on_opposite": false
  },
  "risk": {"risk_percent": 1.0, "max_open_trades": 3, "max_daily_loss_percent": 5.0},
  "session": null | {"start": "07:00", "end": "20:00"}      # broker server time
}

Operands: a number, or one of
  {"ind": "PRICE", "field": "close|open|high|low"}
  {"ind": "EMA"|"SMA"|"RSI"|"ATR", "period": int}
  {"ind": "MACD", "line": "macd|signal|hist", "fast": 12, "slow": 26, "signal": 9}
  {"ind": "BB", "band": "upper|middle|lower", "period": 20, "std": 2.0}
  {"ind": "STOCH", "line": "k|d", "k": 14, "d": 3, "smooth": 3}
  {"ind": "ADX", "line": "adx|plus_di|minus_di", "period": 14}
"""

import copy
import re

SCHEMA_VERSION = 1

TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
OPS = ["<", ">", "<=", ">=", "crosses_above", "crosses_below"]
PRICE_FIELDS = ["close", "open", "high", "low"]

MAX_SYMBOLS = 20
MAX_CONDITIONS = 8
MAX_PERIOD = 500
MAX_RISK_PERCENT = 5.0

_SYMBOL_RE = re.compile(r"^[A-Z0-9._#]{2,20}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# indicator -> {param: (type, default, min, max)}
INDICATORS = {
    "PRICE": {},
    "EMA": {"period": (int, None, 1, MAX_PERIOD)},
    "SMA": {"period": (int, None, 1, MAX_PERIOD)},
    "RSI": {"period": (int, 14, 2, 100)},
    "ATR": {"period": (int, 14, 1, 100)},
    "MACD": {
        "fast": (int, 12, 2, 100),
        "slow": (int, 26, 3, 200),
        "signal": (int, 9, 2, 100),
    },
    "BB": {"period": (int, 20, 2, 200), "std": (float, 2.0, 0.5, 5.0)},
    "STOCH": {"k": (int, 14, 2, 100), "d": (int, 3, 1, 20), "smooth": (int, 3, 1, 20)},
    "ADX": {"period": (int, 14, 2, 100)},
}

# indicator -> (selector key, allowed values, default)
SELECTORS = {
    "PRICE": ("field", PRICE_FIELDS, "close"),
    "MACD": ("line", ["macd", "signal", "hist"], "macd"),
    "BB": ("band", ["upper", "middle", "lower"], "middle"),
    "STOCH": ("line", ["k", "d"], "k"),
    "ADX": ("line", ["adx", "plus_di", "minus_di"], "adx"),
}

SUPPORTED_SUMMARY = (
    "EMA, SMA, RSI, ATR, MACD (macd/signal/hist), Bollinger Bands (upper/middle/lower), "
    "Stochastic (%K/%D), ADX (+DI/-DI), narx (open/high/low/close); solishtirish: <, >, <=, >=, "
    "kesib o'tish (crosses_above / crosses_below)"
)


class StrategyError(ValueError):
    """Raised when a strategy fails validation. Message is user-facing (Uzbek)."""


def _num(value, name, lo=None, hi=None, kind=float):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyError(f"'{name}' son bo'lishi kerak")
    if kind is int:
        if float(value) != int(value):
            raise StrategyError(f"'{name}' butun son bo'lishi kerak")
        value = int(value)
    else:
        value = float(value)
    if lo is not None and value < lo:
        raise StrategyError(f"'{name}' kamida {lo} bo'lishi kerak")
    if hi is not None and value > hi:
        raise StrategyError(f"'{name}' ko'pi bilan {hi} bo'lishi kerak")
    return value


def normalize_operand(op, where):
    if isinstance(op, bool):
        raise StrategyError(f"{where}: noto'g'ri qiymat")
    if isinstance(op, (int, float)):
        return float(op)
    if not isinstance(op, dict):
        raise StrategyError(f"{where}: indikator yoki son bo'lishi kerak")

    ind = str(op.get("ind", "")).upper()
    if ind not in INDICATORS:
        raise StrategyError(f"{where}: '{op.get('ind')}' indikatori qo'llab-quvvatlanmaydi")

    out = {"ind": ind}
    for param, (kind, default, lo, hi) in INDICATORS[ind].items():
        raw = op.get(param, default)
        if raw is None:
            raise StrategyError(f"{where}: {ind} uchun '{param}' ko'rsatilmagan")
        out[param] = _num(raw, f"{ind}.{param}", lo, hi, kind)

    if ind in SELECTORS:
        key, allowed, default = SELECTORS[ind]
        val = str(op.get(key, default)).lower()
        if val not in allowed:
            raise StrategyError(f"{where}: {ind}.{key} = '{val}' noto'g'ri ({', '.join(allowed)})")
        out[key] = val

    if ind == "MACD" and out["fast"] >= out["slow"]:
        raise StrategyError(f"{where}: MACD fast < slow bo'lishi kerak")
    return out


def operand_key(op):
    """Stable column name for an operand, e.g. 'EMA_50', 'MACD_12_26_9_hist'."""
    if isinstance(op, float):
        return None
    ind = op["ind"]
    if ind == "PRICE":
        return op["field"]
    if ind in ("EMA", "SMA", "RSI", "ATR"):
        return f"{ind}_{op['period']}"
    if ind == "MACD":
        return f"MACD_{op['fast']}_{op['slow']}_{op['signal']}_{op['line']}"
    if ind == "BB":
        return f"BB_{op['period']}_{op['std']:g}_{op['band']}"
    if ind == "STOCH":
        return f"STOCH_{op['k']}_{op['d']}_{op['smooth']}_{op['line']}"
    if ind == "ADX":
        return f"ADX_{op['period']}_{op['line']}"
    raise StrategyError(f"noma'lum indikator {ind}")


_FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=",
         "crosses_above": "crosses_below", "crosses_below": "crosses_above"}


def _normalize_conditions(conds, side):
    if conds is None:
        return []
    if not isinstance(conds, list):
        raise StrategyError(f"entry.{side} ro'yxat bo'lishi kerak")
    if len(conds) > MAX_CONDITIONS:
        raise StrategyError(f"entry.{side}: ko'pi bilan {MAX_CONDITIONS} ta shart")
    out = []
    for i, c in enumerate(conds, 1):
        where = f"{side.upper()} shart #{i}"
        if not isinstance(c, dict):
            raise StrategyError(f"{where}: noto'g'ri format")
        op = c.get("op")
        if op not in OPS:
            raise StrategyError(f"{where}: '{op}' operatori qo'llab-quvvatlanmaydi")
        left = normalize_operand(c.get("left"), where + " (chap)")
        right = normalize_operand(c.get("right"), where + " (o'ng)")
        if isinstance(left, float) and isinstance(right, float):
            raise StrategyError(f"{where}: ikkala tomon ham son bo'lishi mumkin emas")
        if isinstance(left, float):
            # Keep an indicator on the left so evaluation has one shape.
            left, right, op = right, left, _FLIP[op]
        out.append({"left": left, "op": op, "right": right})
    return out


def _normalize_level(spec, name, allowed):
    if not isinstance(spec, dict):
        raise StrategyError(f"exit.{name} ko'rsatilmagan")
    t = str(spec.get("type", "")).lower()
    if t not in allowed:
        raise StrategyError(f"exit.{name}.type: {', '.join(allowed)} dan biri bo'lishi kerak")
    if t == "atr":
        return {"type": "atr", "mult": _num(spec.get("mult"), f"{name}.mult", 0.1, 20),
                "period": _num(spec.get("period", 14), f"{name}.period", 1, 100, int)}
    if t == "pips":
        return {"type": "pips", "pips": _num(spec.get("pips"), f"{name}.pips", 0.5, 5000)}
    return {"type": "rr", "ratio": _num(spec.get("ratio"), f"{name}.ratio", 0.2, 20)}


def validate_strategy(raw):
    """Return a normalized deep copy of `raw` or raise StrategyError."""
    if not isinstance(raw, dict):
        raise StrategyError("Strategiya obyekt bo'lishi kerak")
    s = copy.deepcopy(raw)

    name = str(s.get("name") or "Mening strategiyam").strip()[:60]

    symbols = s.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise StrategyError("Kamida bitta aktiv (masalan EURUSD) ko'rsating")
    norm_symbols = []
    for sym in symbols:
        sym = str(sym).upper().strip().replace("/", "")
        if sym == "GOLD":
            sym = "XAUUSD"
        if not _SYMBOL_RE.match(sym):
            raise StrategyError(f"Aktiv nomi noto'g'ri: {sym}")
        if sym not in norm_symbols:
            norm_symbols.append(sym)
    if len(norm_symbols) > MAX_SYMBOLS:
        raise StrategyError(f"Ko'pi bilan {MAX_SYMBOLS} ta aktiv")

    tf = str(s.get("timeframe", "")).upper()
    if tf not in TIMEFRAMES:
        raise StrategyError(f"Vaqt oralig'i quyidagilardan biri bo'lishi kerak: {', '.join(TIMEFRAMES)}")

    entry = s.get("entry") or {}
    buy = _normalize_conditions(entry.get("buy"), "buy")
    sell = _normalize_conditions(entry.get("sell"), "sell")
    if not buy and not sell:
        raise StrategyError("Kamida bitta BUY yoki SELL kirish sharti kerak")

    ex = s.get("exit") or {}
    sl = _normalize_level(ex.get("sl"), "sl", ["atr", "pips"])
    tp = _normalize_level(ex.get("tp"), "tp", ["rr", "atr", "pips"])
    be = ex.get("breakeven_at_r")
    be = None if be in (None, 0, False) else _num(be, "breakeven_at_r", 0.1, 10)
    close_opp = bool(ex.get("close_on_opposite", False))

    risk = s.get("risk") or {}
    risk_pct = _num(risk.get("risk_percent", 1.0), "risk_percent", 0.01, MAX_RISK_PERCENT)
    max_open = _num(risk.get("max_open_trades", 3), "max_open_trades", 1, 20, int)
    max_dd = _num(risk.get("max_daily_loss_percent", 5.0), "max_daily_loss_percent", 0.5, 50)

    session = s.get("session")
    if session:
        if not isinstance(session, dict):
            raise StrategyError("session noto'g'ri")
        start, end = str(session.get("start", "")), str(session.get("end", ""))
        if not (_TIME_RE.match(start) and _TIME_RE.match(end)):
            raise StrategyError("session vaqti HH:MM formatida bo'lishi kerak")
        session = {"start": start, "end": end}
    else:
        session = None

    return {
        "version": SCHEMA_VERSION,
        "name": name,
        "symbols": norm_symbols,
        "timeframe": tf,
        "entry": {"buy": buy, "sell": sell},
        "exit": {"sl": sl, "tp": tp, "breakeven_at_r": be, "close_on_opposite": close_opp},
        "risk": {"risk_percent": risk_pct, "max_open_trades": max_open,
                 "max_daily_loss_percent": max_dd},
        "session": session,
    }


def required_operands(strategy):
    """All indicator operands (deduplicated by key) a strategy needs computed."""
    seen = {}
    for side in ("buy", "sell"):
        for c in strategy["entry"][side]:
            for op in (c["left"], c["right"]):
                if isinstance(op, dict):
                    seen.setdefault(operand_key(op), op)
    for name in ("sl", "tp"):
        lvl = strategy["exit"][name]
        if lvl["type"] == "atr":
            op = {"ind": "ATR", "period": lvl["period"]}
            seen.setdefault(operand_key(op), op)
    return list(seen.values())


def warmup_bars(strategy):
    """Bars needed before every indicator in the strategy is trustworthy."""
    need = 50
    for op in required_operands(strategy):
        ind = op["ind"]
        if ind in ("EMA", "RSI"):
            need = max(need, op["period"] * 3)
        elif ind in ("SMA", "ATR"):
            need = max(need, op["period"])
        elif ind == "MACD":
            need = max(need, (op["slow"] + op["signal"]) * 3)
        elif ind == "BB":
            need = max(need, op["period"])
        elif ind == "STOCH":
            need = max(need, op["k"] + op["d"] + op["smooth"])
        elif ind == "ADX":
            need = max(need, op["period"] * 6)  # double Wilder smoothing
    return need + 5


# ---------------------------------------------------------------- Uzbek text

_OP_UZ = {
    "<": "dan past", ">": "dan yuqori", "<=": "dan past yoki teng", ">=": "dan yuqori yoki teng",
    "crosses_above": "ni pastdan yuqoriga kesib o'tsa",
    "crosses_below": "ni yuqoridan pastga kesib o'tsa",
}


def describe_operand(op):
    if isinstance(op, float):
        return f"{op:g}"
    ind = op["ind"]
    if ind == "PRICE":
        return {"close": "Yopilish narxi", "open": "Ochilish narxi",
                "high": "Eng yuqori narx", "low": "Eng past narx"}[op["field"]]
    if ind in ("EMA", "SMA", "RSI", "ATR"):
        return f"{ind}({op['period']})"
    if ind == "MACD":
        return f"MACD({op['fast']},{op['slow']},{op['signal']}) {op['line']}"
    if ind == "BB":
        return f"Bollinger({op['period']},{op['std']:g}) {op['band']}"
    if ind == "STOCH":
        return f"Stochastic({op['k']},{op['d']},{op['smooth']}) %{op['line'].upper()}"
    if ind == "ADX":
        return {"adx": "ADX", "plus_di": "+DI", "minus_di": "-DI"}[op["line"]] + f"({op['period']})"
    return ind


def describe_condition(c):
    return f"{describe_operand(c['left'])} {describe_operand(c['right'])}{_OP_UZ[c['op']]}"


def describe_strategy(s):
    lines = [f"Nomi: {s['name']}",
             f"Aktivlar: {', '.join(s['symbols'])}",
             f"Vaqt oralig'i: {s['timeframe']}"]
    for side, label in (("buy", "BUY"), ("sell", "SELL")):
        conds = s["entry"][side]
        if conds:
            lines.append(f"{label} ochiladi, agar:")
            lines += [f"  • {describe_condition(c)}" for c in conds]
        else:
            lines.append(f"{label}: ishlatilmaydi")
    sl, tp = s["exit"]["sl"], s["exit"]["tp"]
    sl_txt = f"{sl['mult']:g} × ATR({sl['period']})" if sl["type"] == "atr" else f"{sl['pips']:g} pips"
    if tp["type"] == "rr":
        tp_txt = f"risk × {tp['ratio']:g}"
    elif tp["type"] == "atr":
        tp_txt = f"{tp['mult']:g} × ATR({tp['period']})"
    else:
        tp_txt = f"{tp['pips']:g} pips"
    lines.append(f"Stop Loss: {sl_txt}")
    lines.append(f"Take Profit: {tp_txt}")
    if s["exit"]["breakeven_at_r"]:
        lines.append(f"Breakeven: foyda {s['exit']['breakeven_at_r']:g}R ga yetganda SL kirish narxiga")
    if s["exit"]["close_on_opposite"]:
        lines.append("Qarama-qarshi signal bo'lsa pozitsiya yopiladi")
    r = s["risk"]
    lines.append(f"Risk: har savdoga balansning {r['risk_percent']:g}% i, "
                 f"bir vaqtda ko'pi bilan {r['max_open_trades']} ta savdo, "
                 f"kunlik zarar limiti {r['max_daily_loss_percent']:g}%")
    if s["session"]:
        lines.append(f"Savdo vaqti (server vaqti): {s['session']['start']}–{s['session']['end']}")
    return "\n".join(lines)
