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


def test_retired_model_falls_back_to_alias(monkeypatch):
    urls = []

    class NotFound:
        status_code = 404
        text = "model not found"

    def fake_post(url, json=None, timeout=None, headers=None):
        urls.append(url)
        return NotFound() if len(urls) == 1 else FakeResp(GOOD)

    monkeypatch.setattr(ai_parser.settings, "gemini_api_key", "k")
    monkeypatch.setattr(ai_parser.settings, "gemini_model", "gemini-old")
    monkeypatch.setattr(ai_parser.requests, "post", fake_post)
    assert parse_strategy("RSI 30 dan past bo'lsa BUY").status == "ok"
    assert "gemini-old" in urls[0] and ai_parser.FALLBACK_MODEL in urls[1]


def test_too_large_files_rejected(monkeypatch):
    monkeypatch.setattr(ai_parser.settings, "gemini_api_key", "k")
    res = parse_strategy("", files=[("image/png", b"x" * (16 * 1024 * 1024))])
    assert res.status == "error"


def test_without_key_returns_error(monkeypatch):
    monkeypatch.setattr(ai_parser.settings, "gemini_api_key", "")
    assert parse_strategy("anything").status == "error"
