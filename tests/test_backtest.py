import numpy as np
import pandas as pd
import pytest

from tradegenius.shared.backtest import format_report_uz, merge_results, run_backtest
from tradegenius.shared.strategy_schema import validate_strategy, warmup_bars
from tradegenius.templates import TEMPLATES

POINT, DIGITS = 0.00001, 5


def strat(**over):
    s = {"symbols": ["EURUSD"], "timeframe": "M15",
         "entry": {"buy": [{"left": {"ind": "PRICE", "field": "close"}, "op": "crosses_above", "right": 1.105}]},
         "exit": {"sl": {"type": "pips", "pips": 10}, "tp": {"type": "rr", "ratio": 2}}}
    s.update(over)
    return validate_strategy(s)


def bars(rows):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["time"] = 1_700_000_000 + np.arange(len(df)) * 900
    return df


def flat_then(rows_after, warm):
    return bars([(1.10, 1.1001, 1.0999, 1.10)] * warm + rows_after)


def test_take_profit_gives_plus_ratio_r():
    s = strat()
    w = warmup_bars(s)
    df = flat_then([(1.10, 1.106, 1.10, 1.106),      # signal bar (cross above 1.105)
                    (1.106, 1.1061, 1.1059, 1.106),  # entry at open 1.106
                    (1.106, 1.1265, 1.1055, 1.126)],  # TP = 1.106 + 2*0.001 = 1.108 hit
                   w)
    res = run_backtest(df, s, POINT, DIGITS)
    assert res["trades"] == 1
    assert res["trade_list"][0]["reason"] == "tp"
    assert res["total_r"] == pytest.approx(2.0)


def test_sl_assumed_first_when_both_hit_in_same_bar():
    s = strat()
    w = warmup_bars(s)
    df = flat_then([(1.10, 1.106, 1.10, 1.106),
                    (1.106, 1.1061, 1.1059, 1.106),
                    (1.106, 1.2, 1.0, 1.106)], w)
    res = run_backtest(df, s, POINT, DIGITS)
    assert res["trade_list"][0]["reason"] == "sl"
    assert res["total_r"] == pytest.approx(-1.0)


def test_spread_makes_buy_entry_worse():
    s = strat()
    w = warmup_bars(s)
    df = flat_then([(1.10, 1.106, 1.10, 1.106),
                    (1.106, 1.1061, 1.1059, 1.106),
                    (1.106, 1.1265, 1.1055, 1.126)], w)
    res = run_backtest(df, s, POINT, DIGITS, spread_points=20)
    assert res["trade_list"][0]["entry"] == pytest.approx(1.1062)


def test_breakeven_turns_loser_into_scratch():
    s = strat(exit={"sl": {"type": "pips", "pips": 10}, "tp": {"type": "rr", "ratio": 3},
                    "breakeven_at_r": 1})
    w = warmup_bars(s)
    df = flat_then([(1.10, 1.106, 1.10, 1.106),
                    (1.106, 1.1061, 1.1059, 1.106),
                    (1.106, 1.1075, 1.1058, 1.107),    # +1.5R -> SL to entry
                    (1.107, 1.107, 1.1050, 1.105)],    # falls back -> exit at entry
                   w)
    res = run_backtest(df, s, POINT, DIGITS)
    assert res["trade_list"][0]["r"] == pytest.approx(0.0)


def test_templates_run_on_random_walk_and_report_formats():
    rng = np.random.default_rng(7)
    close = 1.1 + rng.normal(0, 0.0008, 4000).cumsum()
    high = close + np.abs(rng.normal(0, 0.0004, 4000))
    low = close - np.abs(rng.normal(0, 0.0004, 4000))
    df = bars(list(zip(np.r_[close[0], close[:-1]], high, low, close)))
    for t in TEMPLATES.values():
        res = run_backtest(df, t["spec"], POINT, DIGITS, spread_points=10)
        assert res["trades"] >= 0
        merged = merge_results({"EURUSD": res}, t["spec"])
        assert "Backtest" in format_report_uz(merged)
        # no overlapping trades on one symbol
        tl = res["trade_list"]
        assert all(a["exit_i"] <= b["entry_i"] for a, b in zip(tl, tl[1:]))
