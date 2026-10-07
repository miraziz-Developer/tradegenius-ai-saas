import pytest

from tradegenius.shared.strategy_schema import (StrategyError, describe_strategy, operand_key,
                                                required_operands, validate_strategy, warmup_bars)
from tradegenius.templates import TEMPLATES


def base(**over):
    s = {
        "name": "t",
        "symbols": ["eurusd", "GOLD"],
        "timeframe": "h1",
        "entry": {"buy": [{"left": {"ind": "RSI", "period": 14}, "op": "<", "right": 30}], "sell": []},
        "exit": {"sl": {"type": "atr", "mult": 1.5}, "tp": {"type": "rr", "ratio": 2}},
    }
    s.update(over)
    return s


def test_normalizes_symbols_timeframe_and_defaults():
    s = validate_strategy(base())
    assert s["symbols"] == ["EURUSD", "XAUUSD"]
    assert s["timeframe"] == "H1"
    assert s["risk"] == {"risk_percent": 1.0, "max_open_trades": 3, "max_daily_loss_percent": 5.0}
    assert s["exit"]["sl"]["period"] == 14


def test_all_templates_are_valid_and_describable():
    for t in TEMPLATES.values():
        assert describe_strategy(t["spec"])


@pytest.mark.parametrize("cond", [
    {"left": {"ind": "ICHIMOKU"}, "op": ">", "right": 1},
    {"left": {"ind": "RSI", "period": 14}, "op": "==", "right": 30},
    {"left": {"ind": "EMA"}, "op": ">", "right": 1},                       # missing period
    {"left": 1, "op": ">", "right": 2},                                    # two numbers
    {"left": {"ind": "MACD", "fast": 30, "slow": 10}, "op": ">", "right": 0},
    {"left": {"ind": "RSI", "period": True}, "op": ">", "right": 0},       # bool is not a number
])
def test_rejects_unexecutable_conditions(cond):
    with pytest.raises(StrategyError):
        validate_strategy(base(entry={"buy": [cond]}))


def test_rejects_excessive_risk_and_empty_entry():
    with pytest.raises(StrategyError):
        validate_strategy(base(risk={"risk_percent": 10}))
    with pytest.raises(StrategyError):
        validate_strategy(base(entry={"buy": [], "sell": []}))


def test_number_on_left_is_flipped():
    s = validate_strategy(base(entry={"buy": [
        {"left": 30, "op": "crosses_above", "right": {"ind": "RSI", "period": 14}}]}))
    c = s["entry"]["buy"][0]
    assert c["left"] == {"ind": "RSI", "period": 14}
    assert c["op"] == "crosses_below" and c["right"] == 30.0


def test_required_operands_include_atr_for_stops_and_warmup_covers_ema():
    s = validate_strategy(base(entry={"buy": [
        {"left": {"ind": "EMA", "period": 200}, "op": "<", "right": {"ind": "PRICE", "field": "close"}}]}))
    keys = {operand_key(o) for o in required_operands(s)}
    assert {"EMA_200", "close", "ATR_14"} <= keys
    assert warmup_bars(s) >= 600


def test_session_validation():
    assert validate_strategy(base(session={"start": "22:00", "end": "06:00"}))["session"]
    with pytest.raises(StrategyError):
        validate_strategy(base(session={"start": "25:00", "end": "06:00"}))
