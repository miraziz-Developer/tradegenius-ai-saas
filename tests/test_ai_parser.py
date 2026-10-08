import json

from tradegenius import ai_parser
from tradegenius.ai_parser import interpret_reply, parse_strategy

GOOD = {
    "status": "ok",
    "strategy": {"name": "x", "symbols": ["EURUSD"], "timeframe": "M15",
                 "entry": {"buy": [{"left": {"ind": "RSI", "period": 14}, "op": "<", "right": 30}], "sell": []},
                 "exit": {"sl": {"type": "atr", "mult": 1.5}, "tp": {"type": "rr", "ratio": 2}}},
    "assumptions": ["SL 1.5 ATR"],
    "unsupported_features": [],
}


def test_interpret_ok_needs_clarification_unsupported_and_invalid():
    assert interpret_reply(GOOD).status == "ok"
    r = interpret_reply({"status": "needs_clarification", "question": "Qaysi aktiv?"})
    assert r.status == "needs_clarification" and r.question == "Qaysi aktiv?"
    assert interpret_reply({"status": "unsupported", "unsupported_features": ["news"]}).unsupported == ["news"]
    bad = dict(GOOD, strategy=dict(GOOD["strategy"], timeframe="W1"))
    assert interpret_reply(bad).status == "error"
    assert interpret_reply([]).status == "error"


class FakeResp:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return {"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True},
            {"text": json.dumps(self.payload)}]}}]}


def test_files_are_sent_inline_and_unsupported_mime_dropped(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        sent["body"], sent["headers"] = json, headers
        return FakeResp(GOOD)

    monkeypatch.setattr(ai_parser.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_parser.requests, "post", fake_post)
    res = parse_strategy("rasmga qarang", files=[("image/png", b"\x89PNG"), ("application/pdf", b"%PDF"),
                                                 ("application/zip", b"PK")])
    assert res.status == "ok"
    parts = sent["body"]["contents"][0]["parts"]
    mimes = [p["inlineData"]["mimeType"] for p in parts if "inlineData" in p]
    assert mimes == ["image/png", "application/pdf"]
    assert "skrinshot" in parts[-1]["text"]
    assert sent["headers"]["x-goog-api-key"] == "test-key"
    assert sent["body"]["generationConfig"]["responseMimeType"] == "application/json"


class Status:
    def __init__(self, code):
        self.status_code, self.text = code, f"HTTP {code}"


def _gemini(monkeypatch, replies):
    urls = []

    def fake_post(url, json=None, timeout=None, headers=None):
        urls.append(url.split("/models/")[1].split(":")[0])
        return replies[len(urls) - 1]

    monkeypatch.setattr(ai_parser.settings, "gemini_api_key", "k")
    monkeypatch.setattr(ai_parser.settings, "gemini_model", "main")
    monkeypatch.setattr(ai_parser.settings, "gemini_fallback_models", ["backup1", "backup2"])
    monkeypatch.setattr(ai_parser.requests, "post", fake_post)
    monkeypatch.setattr(ai_parser.time, "sleep", lambda s: None)
    return urls


def test_busy_model_is_retried_then_next_model_used(monkeypatch):
    urls = _gemini(monkeypatch, [Status(503), Status(503), Status(404), FakeResp(GOOD)])
    assert parse_strategy("RSI 30 dan past bo'lsa BUY").status == "ok"
    assert urls == ["main", "main", "backup1", "backup2"]


def test_bad_key_does_not_cycle_models(monkeypatch):
    urls = _gemini(monkeypatch, [Status(403)] * 6)
    assert parse_strategy("RSI 30 dan past bo'lsa BUY").status == "error"
    assert set(urls) == {"main"}


def test_too_large_files_rejected(monkeypatch):
    monkeypatch.setattr(ai_parser.settings, "gemini_api_key", "k")
    res = parse_strategy("", files=[("image/png", b"x" * (16 * 1024 * 1024))])
    assert res.status == "error"


def test_without_key_returns_error(monkeypatch):
    monkeypatch.setattr(ai_parser.settings, "gemini_api_key", "")
    assert parse_strategy("anything").status == "error"
