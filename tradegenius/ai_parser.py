"""
Natural language strategy -> validated strategy JSON, via Gemini.

Gemini only proposes; `validate_strategy` is the authority. Anything the
engine can't execute is reported back to the user instead of being dropped.
"""

import base64
import json
import logging
import time

import requests

from .config import settings
from .shared.strategy_schema import (SUPPORTED_SUMMARY, TIMEFRAMES, StrategyError,
                                     validate_strategy)

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BUSY_STATUSES = (429, 500, 503)   # overloaded / rate limited: retry, then try the next model
BUSY_RETRIES = 2

SUPPORTED_MIME = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
MAX_FILES = 5
MAX_FILES_BYTES = 15 * 1024 * 1024  # stays under Gemini's ~20 MB inline request limit

SYSTEM_PROMPT = f"""
You convert a retail trader's strategy description (Uzbek, Russian or English) into a strict JSON
object for an automated MetaTrader 5 engine. Reply with JSON only.

Reply shape:
{{
  "status": "ok" | "needs_clarification" | "unsupported",
  "strategy": <strategy object or null>,
  "question": <one short question in Uzbek (Latin script) if status is needs_clarification, else null>,
  "unsupported_features": [<short Uzbek descriptions of requested things the engine cannot do>],
  "assumptions": [<short Uzbek descriptions of defaults you filled in>]
}}

Strategy object:
{{
  "name": string (short, Uzbek),
  "symbols": [MT5 symbols, e.g. "EURUSD", "GBPUSD", "XAUUSD" (gold/oltin), "BTCUSD", "USDJPY"],
  "timeframe": one of {TIMEFRAMES},
  "entry": {{
    "buy":  [condition, ...],   // ALL must be true to open BUY. [] if no buys.
    "sell": [condition, ...]    // ALL must be true to open SELL. [] if no sells.
  }},
  "exit": {{
    "sl": {{"type": "atr", "mult": number, "period": 14}} | {{"type": "pips", "pips": number}},
    "tp": {{"type": "rr", "ratio": number}} | {{"type": "atr", "mult": number, "period": 14}} | {{"type": "pips", "pips": number}},
    "breakeven_at_r": number | null,      // move SL to entry when profit reaches this many R
    "close_on_opposite": boolean
  }},
  "risk": {{"risk_percent": number (0.01-5), "max_open_trades": int, "max_daily_loss_percent": number}},
  "session": null | {{"start": "HH:MM", "end": "HH:MM"}}   // broker server time
}}

condition = {{"left": operand, "op": "<" | ">" | "<=" | ">=" | "crosses_above" | "crosses_below", "right": operand}}
operand = number
        | {{"ind": "PRICE", "field": "close" | "open" | "high" | "low"}}
        | {{"ind": "EMA" | "SMA", "period": int}}
        | {{"ind": "RSI", "period": int}}
        | {{"ind": "ATR", "period": int}}
        | {{"ind": "MACD", "line": "macd" | "signal" | "hist", "fast": 12, "slow": 26, "signal": 9}}
        | {{"ind": "BB", "band": "upper" | "middle" | "lower", "period": 20, "std": 2}}
        | {{"ind": "STOCH", "line": "k" | "d", "k": 14, "d": 3, "smooth": 3}}
        | {{"ind": "ADX", "line": "adx" | "plus_di" | "minus_di", "period": 14}}

Supported: {SUPPORTED_SUMMARY}.

Rules:
- "price above EMA 50" => {{"left": {{"ind":"PRICE","field":"close"}}, "op": ">", "right": {{"ind":"EMA","period":50}}}}.
- "EMA 9 crosses EMA 21 upward" => crosses_above. "MACD crosses signal" => MACD line macd vs MACD line signal.
- If the trader describes only BUY rules and says "and the opposite for sell", mirror them for SELL.
- Defaults when not specified (list each one in "assumptions"): timeframe M15, SL 1.5×ATR(14),
  TP rr 2, risk_percent 1, max_open_trades 3, max_daily_loss_percent 5, breakeven null, session null.
- If the trader asks for risk above 5% per trade, set 5 and mention it in assumptions.
- Do NOT invent entry conditions. If there is no usable entry rule, status "needs_clarification".
- If the core idea needs something unsupported (news, chart patterns, candlestick patterns, order
  flow, Fibonacci, support/resistance drawing, martingale/grid, multiple timeframes, AI prediction),
  status "unsupported" and list them; if only a minor part is unsupported, build the rest with
  status "ok" and still list the dropped parts in unsupported_features.
- Martingale, grid and averaging-down are never allowed.
- Pine Script / MQL code: translate its entry filters faithfully (e.g. "adx > adxMin" =>
  ADX line adx > number). Settings such as leverage caps, commission or pyramiding that the engine
  cannot express go into unsupported_features, never silently dropped.
- All human-readable text in Uzbek Latin script.
""".strip()


class ParseResult:
    def __init__(self, status, strategy=None, question=None, unsupported=None, assumptions=None,
                 error=None):
        self.status = status            # ok | needs_clarification | unsupported | error
        self.strategy = strategy        # validated strategy dict when status == ok
        self.question = question
        self.unsupported = unsupported or []
        self.assumptions = assumptions or []
        self.error = error


def _call_gemini(user_text, files=(), timeout=60):
    parts = [{"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}
             for mime, data in files]
    parts.append({"text": user_text})
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
    }
    headers = {"x-goog-api-key": settings.gemini_api_key}
    models = [settings.gemini_model] + [m for m in settings.gemini_fallback_models
                                        if m != settings.gemini_model]
    last = None
    for model in models:
        status = None
        for attempt in range(BUSY_RETRIES):
            try:
                r = requests.post(GEMINI_URL.format(model=model), json=body, timeout=timeout,
                                  headers=headers)
            except requests.RequestException as e:
                last, status = f"{model}: {type(e).__name__}", None
                break  # slow or unreachable: move on to the next model instead of waiting again
            status = r.status_code
            if status == 200:
                return _reply_text(r.json())
            last = f"{model}: HTTP {status}: {r.text[:200]}"
            if status not in BUSY_STATUSES:
                break
            time.sleep(2 * (attempt + 1))
        if status is not None and status not in BUSY_STATUSES and status != 404:
            raise RuntimeError(f"Gemini {last}")  # e.g. bad key: other models won't help
        log.warning("Gemini %s unavailable (%s), trying next model", model, last[:120])
    raise RuntimeError(f"All Gemini models unavailable, last: {last}")


def _reply_text(data):
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:300]}")
    parts = (cands[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model reply")
    return json.loads(text[start:end + 1])


def interpret_reply(reply):
    """Validate a model reply dict into a ParseResult. Separated for testing."""
    if not isinstance(reply, dict):
        return ParseResult("error", error="AI noto'g'ri javob qaytardi")
    status = reply.get("status")
    unsupported = [str(x) for x in (reply.get("unsupported_features") or [])][:10]
    assumptions = [str(x) for x in (reply.get("assumptions") or [])][:10]
    if status == "needs_clarification":
        q = reply.get("question") or "Strategiyangizda qachon savdo ochilishini aniqroq yozing."
        return ParseResult("needs_clarification", question=str(q), unsupported=unsupported)
    if status == "unsupported":
        return ParseResult("unsupported", unsupported=unsupported)
    try:
        strategy = validate_strategy(reply.get("strategy"))
    except StrategyError as e:
        return ParseResult("error", error=str(e), unsupported=unsupported)
    return ParseResult("ok", strategy=strategy, unsupported=unsupported, assumptions=assumptions)


def parse_strategy(user_text, history=None, files=()):
    """
    history: optional list of (question, answer) clarification pairs.
    files: optional list of (mime_type, bytes) — screenshots (image/*) or PDF documents
           describing the strategy; Gemini reads them together with the text.
    """
    if not settings.gemini_api_key:
        return ParseResult("error", error="AI hali sozlanmagan (GEMINI_API_KEY). "
                                          "Hozircha tayyor shablonlardan foydalaning.")
    files = [(m, d) for m, d in files if m in SUPPORTED_MIME][:MAX_FILES]
    if sum(len(d) for _, d in files) > MAX_FILES_BYTES:
        return ParseResult("error", error="Fayllar juda katta (jami 15 MB gacha bo'lishi kerak).")

    prompt = f"Strategiya tavsifi:\n{(user_text or '').strip()[:4000] or '(matn yo`q)'}"
    if files:
        prompt += ("\n\nTreyder strategiyasini ilova qilingan skrinshot(lar) yoki PDF hujjat orqali "
                   "ham tushuntirgan. Rasmdagi grafik, indikator sozlamalari va yozuvlardan, "
                   "PDF dagi qoidalardan kirish/chiqish shartlarini aniqlang. Rasmdan aniq "
                   "ko'rinmagan qiymatlarni o'ylab topmang — ularni assumptions ga yozing yoki "
                   "needs_clarification qaytaring.")
    for q, a in history or []:
        prompt += f"\n\nAniqlashtiruvchi savol: {q}\nTreyder javobi: {a[:1000]}"

    for _ in range(2):
        try:
            return interpret_reply(_extract_json(_call_gemini(prompt, files)))
        except (ValueError, RuntimeError, requests.RequestException) as e:
            log.warning("Gemini parse attempt failed: %s", e)
    return ParseResult("error", error="AI javob bera olmadi, birozdan keyin qayta urinib ko'ring.")
