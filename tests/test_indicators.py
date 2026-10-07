import numpy as np
import pandas as pd

from tradegenius.shared.indicators import (compute_indicators, condition_true, in_session, pip_size,
                                           rsi, signal_at, stop_levels)
from tradegenius.shared.strategy_schema import validate_strategy


def frame(close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({"open": close, "high": close + 0.0005, "low": close - 0.0005, "close": close})


def strat(buy, sell=None, **exit_):
    return validate_strategy({
        "symbols": ["EURUSD"], "timeframe": "M15",
        "entry": {"buy": buy, "sell": sell or []},
        "exit": {"sl": {"type": "pips", "pips": 10}, "tp": {"type": "rr", "ratio": 2}, **exit_},
    })


def test_rsi_bounds_and_monotonic_series():
    up = pd.Series(np.linspace(1, 2, 100))
    r = rsi(up, 14)
    assert r.iloc[:14].isna().all()
    assert (r.dropna() == 100).all()
    noisy = pd.Series(1 + np.random.default_rng(1).normal(0, 0.01, 500).cumsum())
    v = rsi(noisy, 14).dropna()
    assert ((v >= 0) & (v <= 100)).all()


def test_crosses_above_fires_only_on_the_cross_bar():
    close = [1.0] * 30 + [1.1] * 5
    s = strat([{"left": {"ind": "PRICE", "field": "close"}, "op": "crosses_above",
                "right": {"ind": "SMA", "period": 10}}])
    df = compute_indicators(frame(close), s)
    hits = [i for i in range(len(df)) if condition_true(df, s["entry"]["buy"][0], i)]
    assert hits == [30]


def test_nan_indicator_never_triggers():
    s = strat([{"left": {"ind": "SMA", "period": 50}, "op": "<", "right": 999}])
    df = compute_indicators(frame([1.0] * 20), s)
    assert not any(signal_at(df, s, i) for i in range(len(df)))


def test_conflicting_buy_and_sell_cancel():
    always = [{"left": {"ind": "PRICE", "field": "close"}, "op": ">", "right": 0}]
    s = strat(always, always)
    df = compute_indicators(frame([1.0] * 5), s)
    assert signal_at(df, s, 4) is None


def test_stop_levels_pips_and_rr():
    s = strat([{"left": {"ind": "PRICE", "field": "close"}, "op": ">", "right": 0}])
    sl, tp = stop_levels(s, "buy", 1.1000, {}, pip_size(0.00001, 5))
    assert round(sl, 5) == 1.0990 and round(tp, 5) == 1.1020
    sl, tp = stop_levels(s, "sell", 1.1000, {}, 0.0001)
    assert round(sl, 5) == 1.1010 and round(tp, 5) == 1.0980


def test_atr_stops_unavailable_returns_none():
    s = validate_strategy({"symbols": ["EURUSD"], "timeframe": "M15",
                           "entry": {"buy": [{"left": {"ind": "RSI", "period": 14}, "op": "<", "right": 30}]},
                           "exit": {"sl": {"type": "atr", "mult": 1}, "tp": {"type": "rr", "ratio": 2}}})
    assert stop_levels(s, "buy", 1.1, {14: None}, 0.0001) is None


def test_session_including_overnight_wrap():
    s = {"session": {"start": "22:00", "end": "06:00"}}
    assert in_session(s, "23:30") and in_session(s, "05:59")
    assert not in_session(s, "06:00") and not in_session(s, "12:00")
    assert in_session({"session": None}, "12:00")


def test_pip_size():
    assert pip_size(0.00001, 5) == 0.0001
    assert pip_size(0.001, 3) == 0.01
    assert pip_size(0.01, 2) == 0.1
    assert pip_size(1.0, 0) == 1.0
