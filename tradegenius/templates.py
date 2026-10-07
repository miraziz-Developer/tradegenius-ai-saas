"""Ready-made strategies so users can test without writing one (and without AI)."""

from .shared.strategy_schema import validate_strategy

_RAW = {
    "trend_rsi": {
        "title": "📈 Trend + RSI pullback",
        "spec": {
            "name": "Trend + RSI pullback",
            "symbols": ["EURUSD", "GBPUSD", "XAUUSD"],
            "timeframe": "M15",
            "entry": {
                "buy": [
                    {"left": {"ind": "EMA", "period": 50}, "op": ">", "right": {"ind": "EMA", "period": 200}},
                    {"left": {"ind": "RSI", "period": 14}, "op": "crosses_above", "right": 35},
                ],
                "sell": [
                    {"left": {"ind": "EMA", "period": 50}, "op": "<", "right": {"ind": "EMA", "period": 200}},
                    {"left": {"ind": "RSI", "period": 14}, "op": "crosses_below", "right": 65},
                ],
            },
            "exit": {"sl": {"type": "atr", "mult": 1.5}, "tp": {"type": "rr", "ratio": 2},
                     "breakeven_at_r": 1.0},
            "risk": {"risk_percent": 1.0, "max_open_trades": 3, "max_daily_loss_percent": 5},
        },
    },
    "ema_cross": {
        "title": "✂️ EMA 9/21 kesishuvi",
        "spec": {
            "name": "EMA 9/21 kesishuvi",
            "symbols": ["EURUSD", "USDJPY"],
            "timeframe": "H1",
            "entry": {
                "buy": [
                    {"left": {"ind": "EMA", "period": 9}, "op": "crosses_above", "right": {"ind": "EMA", "period": 21}},
                    {"left": {"ind": "PRICE", "field": "close"}, "op": ">", "right": {"ind": "EMA", "period": 200}},
                ],
                "sell": [
                    {"left": {"ind": "EMA", "period": 9}, "op": "crosses_below", "right": {"ind": "EMA", "period": 21}},
                    {"left": {"ind": "PRICE", "field": "close"}, "op": "<", "right": {"ind": "EMA", "period": 200}},
                ],
            },
            "exit": {"sl": {"type": "atr", "mult": 2}, "tp": {"type": "rr", "ratio": 2},
                     "close_on_opposite": True},
            "risk": {"risk_percent": 1.0, "max_open_trades": 2, "max_daily_loss_percent": 4},
        },
    },
    "bb_reversal": {
        "title": "🎯 Bollinger qaytishi",
        "spec": {
            "name": "Bollinger qaytishi",
            "symbols": ["EURUSD", "GBPUSD"],
            "timeframe": "M30",
            "entry": {
                "buy": [
                    {"left": {"ind": "PRICE", "field": "close"}, "op": "crosses_above",
                     "right": {"ind": "BB", "band": "lower", "period": 20, "std": 2}},
                    {"left": {"ind": "RSI", "period": 14}, "op": "<", "right": 40},
                ],
                "sell": [
                    {"left": {"ind": "PRICE", "field": "close"}, "op": "crosses_below",
                     "right": {"ind": "BB", "band": "upper", "period": 20, "std": 2}},
                    {"left": {"ind": "RSI", "period": 14}, "op": ">", "right": 60},
                ],
            },
            "exit": {"sl": {"type": "atr", "mult": 1.2}, "tp": {"type": "rr", "ratio": 1.5}},
            "risk": {"risk_percent": 0.5, "max_open_trades": 2, "max_daily_loss_percent": 3},
            "session": {"start": "08:00", "end": "20:00"},
        },
    },
}

TEMPLATES = {k: {"title": v["title"], "spec": validate_strategy(v["spec"])} for k, v in _RAW.items()}
