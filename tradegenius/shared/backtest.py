"""
Bar-by-bar backtester sharing signal logic with the live engine.

Model (deliberately conservative):
  * signal on closed bar i -> entry at open of bar i+1
  * bar prices are bid; buys pay the spread on entry, sells on exit
  * if SL and TP are both inside the same bar, SL is assumed to hit first
  * one position per symbol at a time (same as the live engine)
Results are expressed in R (multiples of the initial risk), so they do not
depend on account size; % figures assume fixed `risk_percent` per trade.
"""

from datetime import datetime, timezone

import pandas as pd

from .indicators import (atr_values_at, compute_indicators, in_session, pip_size, signal_at,
                         stop_levels)
from .strategy_schema import warmup_bars


def _hhmm(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%H:%M")


def run_backtest(rates, strategy, point, digits, spread_points=0):
    """
    rates: DataFrame or MT5 structured array with columns time, open, high, low, close.
    Returns dict with summary stats and list of trades.
    """
    df = compute_indicators(pd.DataFrame(rates), strategy)
    n = len(df)
    start = warmup_bars(strategy)
    spread = spread_points * point
    pip = pip_size(point, digits)
    be_r = strategy["exit"]["breakeven_at_r"]
    close_opp = strategy["exit"]["close_on_opposite"]
    has_time = "time" in df.columns

    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    trades = []
    pos = None

    def close(i, price, reason):
        nonlocal pos
        if pos["side"] == "buy":
            r = (price - pos["entry"]) / pos["risk"]
        else:
            r = (pos["entry"] - price) / pos["risk"]
        trades.append({
            "side": pos["side"], "entry_i": pos["i"], "exit_i": i,
            "entry": pos["entry"], "exit": price, "r": r, "reason": reason,
            "time": int(df["time"].iat[pos["i"]]) if has_time else pos["i"],
        })
        pos = None

    for i in range(start, n):
        if pos is not None:
            if pos["side"] == "buy":
                if l[i] <= pos["sl"]:
                    close(i, pos["sl"], "sl")
                elif h[i] >= pos["tp"]:
                    close(i, pos["tp"], "tp")
                elif be_r and pos["sl"] < pos["entry"] and h[i] - pos["entry"] >= be_r * pos["risk"]:
                    pos["sl"] = pos["entry"]
            else:
                ask_h, ask_l = h[i] + spread, l[i] + spread
                if ask_h >= pos["sl"]:
                    close(i, pos["sl"], "sl")
                elif ask_l <= pos["tp"]:
                    close(i, pos["tp"], "tp")
                elif be_r and pos["sl"] > pos["entry"] and pos["entry"] - ask_l >= be_r * pos["risk"]:
                    pos["sl"] = pos["entry"]

        if i >= n - 1:
            break
        sig = signal_at(df, strategy, i)

        if pos is not None and close_opp and sig and sig != pos["side"]:
            exit_px = c[i] if pos["side"] == "buy" else c[i] + spread
            close(i, exit_px, "opposite")

        if pos is None and sig:
            if has_time and not in_session(strategy, _hhmm(df["time"].iat[i + 1])):
                continue
            entry = o[i + 1] + spread if sig == "buy" else o[i + 1]
            levels = stop_levels(strategy, sig, entry, atr_values_at(df, strategy, i), pip)
            if levels is None:
                continue
            sl, tp = levels
            pos = {"side": sig, "entry": entry, "sl": sl, "tp": tp,
                   "risk": abs(entry - sl), "i": i + 1}

    return summarize(trades, strategy, df, start)


def summarize(trades, strategy, df=None, start=0):
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    risk_pct = strategy["risk"]["risk_percent"]
    gross_loss = -sum(losses)
    out = {
        "trades": len(rs),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(rs) * 100) if rs else 0.0,
        "total_r": sum(rs),
        "avg_r": (sum(rs) / len(rs)) if rs else 0.0,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else (float("inf") if wins else 0.0),
        "max_dd_r": max_dd,
        "return_pct": sum(rs) * risk_pct,
        "max_dd_pct": max_dd * risk_pct,
        "bars": 0 if df is None else max(0, len(df) - start),
        "trade_list": trades,
    }
    if df is not None and "time" in df.columns and len(df) > start:
        out["from"] = int(df["time"].iat[start])
        out["to"] = int(df["time"].iat[-1])
    return out


def merge_results(per_symbol, strategy):
    """Combine per-symbol backtests into one portfolio summary (trades in time order)."""
    trades = []
    for sym, res in per_symbol.items():
        for t in res["trade_list"]:
            trades.append(dict(t, symbol=sym))
    trades.sort(key=lambda t: t["time"])
    out = summarize(trades, strategy)
    froms = [r["from"] for r in per_symbol.values() if "from" in r]
    tos = [r["to"] for r in per_symbol.values() if "to" in r]
    if froms:
        out["from"], out["to"] = min(froms), max(tos)
    out["per_symbol"] = {s: {k: v for k, v in r.items() if k != "trade_list"}
                         for s, r in per_symbol.items()}
    return out


def format_report_uz(res):
    def d(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    pf = res["profit_factor"]
    pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
    lines = ["📈 <b>Backtest natijasi</b>"]
    if "from" in res:
        lines.append(f"Davr: {d(res['from'])} → {d(res['to'])}")
    lines += [
        f"Savdolar: {res['trades']} (✅ {res['wins']} / ❌ {res['losses']})",
        f"Yutuq foizi: {res['win_rate']:.1f}%",
        f"Jami natija: {res['total_r']:+.1f}R  (≈ {res['return_pct']:+.1f}%)",
        f"Profit factor: {pf_txt}",
        f"Maks. pasayish: {res['max_dd_r']:.1f}R  (≈ {res['max_dd_pct']:.1f}%)",
    ]
    for sym, r in (res.get("per_symbol") or {}).items():
        lines.append(f"  • {sym}: {r['trades']} savdo, {r['total_r']:+.1f}R")
    if res["trades"] < 20:
        lines.append("\n⚠️ Savdolar soni kam — natija statistik jihatdan ishonchli emas.")
    lines.append("<i>O'tmishdagi natija kelajakni kafolatlamaydi. Spred hisobga olingan, "
                 "komissiya va slippage hisobga olinmagan.</i>")
    return "\n".join(lines)
