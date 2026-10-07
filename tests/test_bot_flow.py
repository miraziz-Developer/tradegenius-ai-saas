"""End-to-end bot conversation with a fake Telegram client and fake worker manager."""

import pytest

from tradegenius import bot as bot_module
from tradegenius.ai_parser import ParseResult
from tradegenius.config import Settings
from tradegenius.crypto import decrypt
from tradegenius.db import DB
from tradegenius.templates import TEMPLATES

UID = 42


class FakeTG:
    def __init__(self):
        self.sent, self.deleted, self.downloads = [], [], []

    def send(self, chat_id, text, kb=None):
        self.sent.append((chat_id, text, kb))
        return {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text, kb=None):
        return self.send(chat_id, text, kb)

    def answer_callback(self, *a, **k):
        pass

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True

    def typing(self, chat_id):
        pass

    def download(self, file_id):
        self.downloads.append(file_id)
        return b"bytes-of-" + file_id.encode()

    @property
    def last(self):
        return self.sent[-1][1]


class FakeManager:
    def __init__(self):
        self.woken = 0

    def wake(self):
        self.woken += 1

    def status(self, aid):
        return {"alive": False}

    def is_alive(self, aid):
        return False

    def forget(self, aid):
        pass


@pytest.fixture
def env(tmp_path):
    cfg = Settings()
    cfg.admin_ids, cfg.allowed_user_ids, cfg.billing_enabled = {1}, set(), False
    db = DB(str(tmp_path / "t.db"))
    tg, mgr = FakeTG(), FakeManager()
    return bot_module.Bot(db, tg, mgr, cfg), db, tg, mgr


FROM = {"id": UID, "first_name": "Ali", "username": "ali"}
_mid = [100]


def msg(b, text=None, **extra):
    _mid[0] += 1
    m = {"message_id": _mid[0], "from": FROM, "chat": {"id": UID, "type": "private"}, **extra}
    if text is not None:
        m["text"] = text
    b.on_message(m)
    return m


def cb(b, data):
    b.on_callback({"id": "c", "from": FROM, "data": data, "message": {"message_id": 1}})


def test_full_onboarding_template_account_and_start(env):
    b, db, tg, mgr = env
    msg(b, "/start")
    assert "Foydalanish shartlari" in tg.last
    msg(b, "salom")                       # still must accept terms
    assert "Foydalanish shartlari" in tg.last
    cb(b, "terms:accept")
    assert "TradeGenius AI" in tg.last

    cb(b, "tpl:trend_rsi")
    cb(b, "st:save")
    assert "MT5" in tg.last
    assert db.list_strategies(UID)[0]["spec"] == TEMPLATES["trend_rsi"]["spec"]

    cb(b, "acc:add")
    msg(b, "abc")
    assert "raqam" in tg.last
    msg(b, "12345678")
    pw_msg = msg(b, "S3cret!pw")
    assert pw_msg["message_id"] in tg.deleted
    assert db.get_user(UID)["state"] == "awaiting_server"
    msg(b, "Exness-MT5Trial8")

    acc = db.list_accounts(UID)[0]
    assert acc["login"] == "12345678" and acc["server"] == "Exness-MT5Trial8"
    assert "S3cret" not in acc["password_enc"] and decrypt(acc["password_enc"]) == "S3cret!pw"

    sid = db.list_strategies(UID)[0]["id"]
    cb(b, f"acc:use:{acc['id']}:{sid}")
    cb(b, f"acc:start:{acc['id']}")
    acc = db.get_account(acc["id"])
    assert acc["desired_state"] == "running" and mgr.woken == 1

    cb(b, f"acc:setst:{acc['id']}")       # cannot change strategy while running
    assert "to'xtating" in tg.last
    cb(b, f"acc:stop:{acc['id']}")
    assert db.get_account(acc["id"])["desired_state"] == "stopped"


def test_screenshot_and_pdf_are_passed_to_ai(env, monkeypatch):
    b, db, tg, _ = env
    db.upsert_user(UID)
    db.accept_terms(UID)
    seen = {}

    def fake_parse(text, history=None, files=()):
        seen["text"], seen["files"] = text, files
        return ParseResult("ok", strategy=TEMPLATES["ema_cross"]["spec"], assumptions=["TP 1:2"])

    monkeypatch.setattr(bot_module, "parse_strategy", fake_parse)
    msg(b, caption="mening strategiyam", photo=[{"file_id": "small"}, {"file_id": "big"}])
    assert seen["files"] == [("image/jpeg", b"bytes-of-big")]
    assert seen["text"] == "mening strategiyam"
    assert "shunday tushunildi" in tg.last and "TP 1:2" in tg.last

    msg(b, document={"file_id": "pdf1", "mime_type": "application/pdf"})
    assert seen["files"] == [("application/pdf", b"bytes-of-pdf1")]

    msg(b, document={"file_id": "z", "mime_type": "application/zip"})
    assert "fayl turini" in tg.last


def test_clarification_round_keeps_files_and_history(env, monkeypatch):
    b, db, tg, _ = env
    db.upsert_user(UID)
    db.accept_terms(UID)
    calls = []

    def fake_parse(text, history=None, files=()):
        calls.append((text, history, files))
        if len(calls) == 1:
            return ParseResult("needs_clarification", question="Qaysi vaqt oralig'i?")
        return ParseResult("ok", strategy=TEMPLATES["ema_cross"]["spec"])

    monkeypatch.setattr(bot_module, "parse_strategy", fake_parse)
    msg(b, photo=[{"file_id": "chart"}])
    assert "Qaysi vaqt" in tg.last
    msg(b, "H1")
    text, history, files = calls[1]
    assert history == [["Qaysi vaqt oralig'i?", "H1"]]
    assert files == [("image/jpeg", b"bytes-of-chart")]


def test_billing_on_blocks_non_admin_but_not_admin(env):
    b, db, tg, _ = env
    b.cfg.billing_enabled = True
    for uid in (UID, 1):
        db.upsert_user(uid)
        db.accept_terms(uid)
    cb(b, "menu:new")
    assert "obuna kerak" in tg.last
    b.on_callback({"id": "c", "from": {"id": 1}, "data": "menu:new", "message": {"message_id": 1}})
    assert "Strategiyangizni" in tg.last


def test_ai_daily_limit(env, monkeypatch):
    b, db, tg, _ = env
    b.cfg.ai_daily_limit = 2
    db.upsert_user(UID)
    db.accept_terms(UID)
    calls = []
    monkeypatch.setattr(bot_module, "parse_strategy",
                        lambda *a, **k: calls.append(1) or ParseResult("error", error="x"))
    for _ in range(3):
        msg(b, "M15 da RSI 30 dan past bo'lsa BUY ochilsin, EURUSD da ishlasin iltimos")
    assert len(calls) == 2 and "limiti" in tg.last


def test_start_while_engines_disabled_queues(env):
    b, db, tg, mgr = env
    b.cfg.engines_enabled = False
    db.upsert_user(UID)
    db.accept_terms(UID)
    sid = db.add_strategy(UID, TEMPLATES["ema_cross"]["spec"])
    aid = db.add_account(UID, "1", "S", "x")
    db.update_account(aid, strategy_id=sid)
    cb(b, f"acc:start:{aid}")
    assert any("texnik ishlarda" in t for _, t, _ in tg.sent)
    assert db.get_account(aid)["desired_state"] == "running"


def test_other_users_account_is_not_accessible(env):
    b, db, tg, _ = env
    db.upsert_user(7)
    other = db.add_account(7, "999", "Srv", "x")
    db.upsert_user(UID)
    db.accept_terms(UID)
    cb(b, f"acc:start:{other}")
    assert "topilmadi" in tg.last
    assert db.get_account(other)["desired_state"] == "stopped"
