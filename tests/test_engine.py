"""Engine trading logic against a fake MetaTrader5 module (no Wine/MT5 needed)."""

import importlib.util
import os
import sys
import types
from types import SimpleNamespace as NS

import numpy as np
import pytest

from tradegenius.shared.strategy_schema import validate_strategy, warmup_bars

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeMT5(types.ModuleType):
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
    TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_OUT = 1
    ACCOUNT_TRADE_MODE_DEMO = 0

    def __init__(self):
        super().__init__("MetaTrader5")
        self.orders, self.positions, self.rates = [], [], None
        self.balance = 10_000.0
        self.equity = 10_000.0
        self.info = NS(name="EURUSD", point=0.00001, digits=5, trade_tick_size=0.00001,
                       trade_tick_value=1.0, volume_min=0.01, volume_max=100, volume_step=0.01,
                       filling_mode=2, trade_stops_level=0, spread=10, visible=True)
        self.tick = NS(bid=1.1060, ask=1.1061, time=1_700_000_000 + 12 * 3600)

    def symbol_info(self, sym):
        return self.info if sym == "EURUSD" else None

    def symbol_info_tick(self, sym):
        return self.tick

    def symbols_get(self, pattern):
        return []

    def symbol_select(self, sym, flag):
        return True

    def account_info(self):
        return NS(balance=self.balance, equity=self.equity, currency="USD", login=1)

    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def copy_rates_from_pos(self, sym, tf, start, count):
        return self.rates[-count:]

    def order_send(self, req):
        self.orders.append(req)
        return NS(retcode=self.TRADE_RETCODE_DONE, price=req.get("price"), comment="done")

    deals = ()

    def history_deals_get(self, a, b):
        return list(self.deals)

    def last_error(self):
        return (0, "ok")


def make_rates(closes):
    n = len(closes)
    arr = np.zeros(n, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")])
    arr["time"] = 1_700_000_000 + np.arange(n) * 900
    arr["close"] = closes
    arr["open"] = np.r_[closes[0], closes[:-1]]
    arr["high"] = np.maximum(arr["open"], arr["close"]) + 0.0001
    arr["low"] = np.minimum(arr["open"], arr["close"]) - 0.0001
    return arr


@pytest.fixture
def engine(monkeypatch, tmp_path):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setenv("APP_ROOT", ROOT)
    monkeypatch.setenv("ACCOUNT_ID", "7")
    monkeypatch.setenv("STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setenv("CONTROL_FILE", str(tmp_path / "control.json"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    spec = importlib.util.spec_from_file_location("tg_engine", os.path.join(ROOT, "engine", "engine.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, fake


def strategy(**risk):
    return validate_strategy({
        "symbols": ["EURUSD"], "timeframe": "M15",
        "entry": {"buy": [{"left": {"ind": "PRICE", "field": "close"}, "op": "crosses_above", "right": 1.105}]},
        "exit": {"sl": {"type": "pips", "pips": 10}, "tp": {"type": "rr", "ratio": 2}},
        "risk": {"risk_percent": 1.0, **risk},
    })


def build(mod, fake, s):
    eng = mod.Engine(s, mod.Status())
    eng.setup()
    n = warmup_bars(s) + 50
    # flat, then the last CLOSED bar crosses above 1.105; the last bar is still forming
    fake.rates = make_rates(np.r_[np.full(n - 2, 1.10), 1.106, 1.106])
    return eng


def test_opens_risk_sized_buy_once_per_bar(engine):
    mod, fake = engine
    eng = build(mod, fake, strategy())
    eng.process_symbol("EURUSD", halted=False)
    assert len(fake.orders) == 1
    o = fake.orders[0]
    assert o["type"] == fake.ORDER_TYPE_BUY and o["magic"] == mod.MAGIC
    assert o["sl"] == pytest.approx(1.1061 - 0.0010)
    assert o["tp"] == pytest.approx(1.1061 + 0.0020)
    # 1% of 10,000 = 100 USD; 10 pips on EURUSD = 100 USD per lot -> 1.00 lot
    assert o["volume"] == pytest.approx(1.0)
    assert o["type_filling"] == fake.ORDER_FILLING_IOC

    eng.process_symbol("EURUSD", halted=False)   # same closed bar -> no duplicate
    assert len(fake.orders) == 1


def test_no_trade_when_halted_or_max_trades_reached(engine):
    mod, fake = engine
    eng = build(mod, fake, strategy())
    eng.process_symbol("EURUSD", halted=True)
    assert fake.orders == []

    eng = build(mod, fake, strategy(max_open_trades=1))
    fake.positions = [NS(symbol="GBPUSD", magic=mod.MAGIC, type=0, ticket=1, volume=1, price_open=1,
                         sl=0.9, tp=1.2, profit=0)]
    eng.process_symbol("EURUSD", halted=False)
    assert fake.orders == []


def test_foreign_positions_are_ignored(engine):
    mod, fake = engine
    eng = build(mod, fake, strategy(max_open_trades=1))
    fake.positions = [NS(symbol="EURUSD", magic=12345, type=0, ticket=1, volume=1, price_open=1,
                         sl=0.9, tp=1.2, profit=0)]
    eng.process_symbol("EURUSD", halted=False)
    assert len(fake.orders) == 1


def test_lot_never_exceeds_risk(engine):
    mod, fake = engine
    info = fake.info
    assert mod.calc_lot(info, 10_000, 1.0, 0.0010) == pytest.approx(1.0)
    assert mod.calc_lot(info, 10_000, 1.0, 0.0013) == pytest.approx(0.76)   # floored, not rounded up
    assert mod.calc_lot(info, 50, 1.0, 0.0010) is None                       # min lot would over-risk


def test_breakeven_moves_sl(engine):
    mod, fake = engine
    s = validate_strategy({**strategy(), "exit": {"sl": {"type": "pips", "pips": 10},
                                                  "tp": {"type": "rr", "ratio": 3}, "breakeven_at_r": 1}})
    eng = build(mod, fake, s)
    fake.positions = [NS(symbol="EURUSD", magic=mod.MAGIC, type=0, ticket=9, volume=1,
                         price_open=1.1000, sl=1.0990, tp=1.1030, profit=10)]
    fake.tick = NS(bid=1.1012, ask=1.1013, time=fake.tick.time)
    eng.manage_breakeven()
    assert fake.orders[-1]["action"] == fake.TRADE_ACTION_SLTP and fake.orders[-1]["sl"] == 1.1000


def test_closed_deals_reported_once_and_old_ones_skipped(engine, monkeypatch):
    mod, fake = engine
    sent = []
    monkeypatch.setattr(mod, "notify", sent.append)
    eng = build(mod, fake, strategy())

    def deal(ticket, profit, magic=None):
        return NS(ticket=ticket, magic=mod.MAGIC if magic is None else magic, entry=fake.DEAL_ENTRY_OUT,
                  symbol="EURUSD", profit=profit, commission=-1.0, swap=0.0)

    fake.deals = [deal(1, 50)]                       # closed before the engine started
    eng.report_closed_deals()
    assert sent == []

    fake.deals = [deal(1, 50), deal(2, -30), deal(3, 99, magic=1)]
    eng.last_deal_check = 0
    eng.report_closed_deals()
    assert len(sent) == 1 and "-31.00" in sent[0]
    eng.last_deal_check = 0
    eng.report_closed_deals()
    assert len(sent) == 1


def test_daily_loss_limit_halts(engine):
    mod, fake = engine
    eng = build(mod, fake, strategy(max_daily_loss_percent=2))
    assert eng.check_daily_loss(fake.account_info()) is False
    fake.equity = 9_790
    assert eng.check_daily_loss(fake.account_info()) is True
