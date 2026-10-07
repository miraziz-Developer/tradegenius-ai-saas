"""Minimal Telegram Bot API client (long polling, HTML messages, files, payments)."""

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # Bot API getFile limit


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()

    def call(self, method, _http_timeout=30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for k in ("reply_markup", "prices", "allowed_updates"):
            if k in params and not isinstance(params[k], str):
                params[k] = json.dumps(params[k])
        for attempt in range(3):
            try:
                r = self.session.post(f"{self.base}/{method}", data=params, timeout=_http_timeout)
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    raise TelegramError(f"{method}: {e}") from e
                time.sleep(1 + attempt)
                continue
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry and attempt < 2:
                time.sleep(min(int(retry), 30))
                continue
            raise TelegramError(f"{method}: {data.get('description')}")

    # ------------------------------------------------------------ helpers
    def get_updates(self, offset, poll_seconds=25):
        return self.call("getUpdates", _http_timeout=poll_seconds + 10, offset=offset,
                         timeout=poll_seconds,
                         allowed_updates=["message", "callback_query", "pre_checkout_query"])

    def send(self, chat_id, text, kb=None):
        try:
            return self.call("sendMessage", chat_id=chat_id, text=text[:4096], parse_mode="HTML",
                             disable_web_page_preview="true", reply_markup=kb)
        except TelegramError as e:
            log.warning("send to %s failed: %s", chat_id, e)
            return None

    def edit(self, chat_id, message_id, text, kb=None):
        try:
            return self.call("editMessageText", chat_id=chat_id, message_id=message_id,
                             text=text[:4096], parse_mode="HTML", disable_web_page_preview="true",
                             reply_markup=kb)
        except TelegramError as e:
            if "message is not modified" in str(e):
                return None
            return self.send(chat_id, text, kb)

    def answer_callback(self, callback_id, text=None, alert=False):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text,
                      show_alert="true" if alert else None)
        except TelegramError:
            pass

    def delete(self, chat_id, message_id):
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except TelegramError:
            return False

    def typing(self, chat_id):
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass

    def download(self, file_id):
        """Return file bytes (kept in memory only, never written to disk)."""
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > MAX_DOWNLOAD_BYTES:
            raise TelegramError("file too large")
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.content

    def send_invoice(self, chat_id, title, description, payload, provider_token, currency, amount):
        return self.call("sendInvoice", chat_id=chat_id, title=title, description=description,
                         payload=payload, provider_token=provider_token, currency=currency,
                         prices=[{"label": title, "amount": amount}])

    def answer_pre_checkout(self, query_id, ok=True, error=None):
        self.call("answerPreCheckoutQuery", pre_checkout_query_id=query_id,
                  ok="true" if ok else "false", error_message=error)


def kb(rows):
    """rows: [[(text, callback_data), ...], ...] -> inline keyboard markup."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
