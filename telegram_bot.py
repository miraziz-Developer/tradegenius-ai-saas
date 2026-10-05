import os
import json
import time
import requests
import urllib3
from docker_manager import start_client_container, stop_client_container, get_client_container_status

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Master Platform Telegram Bot Token
MASTER_BOT_TOKEN = os.getenv("MASTER_BOT_TOKEN", "8666134052:AAHngxXJsc_0eE1-V8WPCmmh_qWjvfr3g0Q")

# User Session State Cache
user_sessions = {}

def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{MASTER_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, data=payload, verify=False, timeout=10)
    except Exception as e:
        print(f"[-] Send error: {e}")

def parse_strategy_text_to_json(user_text):
    """
    AI Parsing Simulation (can be connected to OpenAI/Claude API).
    Converts user's natural language into strict JSON schema.
    """
    # Simple intelligent rule extraction fallback
    symbols = []
    for s in ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD", "GOLD"]:
        if s.lower() in user_text.lower():
            symbols.append("XAUUSD" if s == "GOLD" else s)
    if not symbols:
        symbols = ["EURUSD", "GBPUSD"]

    timeframe = "M15"
    if "1h" in user_text.lower() or "soat" in user_text.lower():
        timeframe = "H1"
    elif "4h" in user_text.lower():
        timeframe = "H4"
    elif "5m" in user_text.lower() or "minut" in user_text.lower():
        timeframe = "M5"

    return {
        "strategy_name": "Mijozning Shaxsiy Strategiyasi",
        "symbols": symbols,
        "timeframe": timeframe,
        "magic_number": int(time.time()) % 1000000,
        "risk_percent": 1.0,
        "rules": {
            "buy": [
                {"indicator": "RSI_14", "op": "<", "val": 35},
                {"indicator": "EMA_50", "op": ">", "val": "EMA_200"}
            ],
            "sell": [
                {"indicator": "RSI_14", "op": ">", "val": 65},
                {"indicator": "EMA_50", "op": "<", "val": "EMA_200"}
            ]
        },
        "risk_management": {
            "sl_type": "ATR",
            "sl_multiplier": 1.5,
            "tp_ratio": 2.5,
            "breakeven_trigger_ratio": 0.5
        }
    }

def handle_update(update):
    message = update.get("message")
    if not message or "text" not in message:
        return

    chat_id = message["chat"]["id"]
    text = message["text"].strip()
    session = user_sessions.setdefault(chat_id, {"step": "START"})

    # 1. Start Command
    if text == "/start":
        session["step"] = "AWAITING_STRATEGY"
        welcome = (
            "👋 *Assalomu alaykum! TradeGenius AI platformasiga xush kelibsiz.*\n\n"
            "Bu platforma orqali siz o'zingizning shaxsiy trading strategiyangizni "
            "hech qanday dasturlashsiz **avtomatlashtirilgan 24/7 savdo botiga** aylantirishingiz mumkin!\n\n"
            "📝 *1-qadam:* Iltimos, o'z strategiyangizni erkin tilda yozib qoldiring "
            "(masalan: _'M15 da RSI 30 dan past bo'lsa va 50 EMA dan tepada tursa BUY ochilsin, Oltin va EURUSD da ishlasin'_)."
        )
        send_message(chat_id, welcome)
        return

    # 2. Strategy Received -> Parse into JSON
    if session["step"] == "AWAITING_STRATEGY":
        session["strategy_config"] = parse_strategy_text_to_json(text)
        session["step"] = "CONFIRM_STRATEGY"

        cfg = session["strategy_config"]
        summary = (
            "🧠 *Strategiyangiz qoidalari aniqlashtirildi:*\n\n"
            f"📊 Aktivlar: `{', '.join(cfg['symbols'])}`\n"
            f"⏳ Vaqt oralig'i: `{cfg['timeframe']}`\n"
            f"📈 Kirish: `RSI korreksiyasi + EMA trend yo'nalishi`\n"
            f"🛡️ Risk: Har bir savdoga `1%` (SL: 1.5x ATR, TP: 2.5x ATR)\n\n"
            "Barchasi to'g'rimi? Tarifni tanlang:\n"
            "1️⃣ **1** - O'z kompyuterimga ulayman ($19/oy)\n"
            "2️⃣ **2** - Bulutda 24/7 ishlasin ($39/oy - To'liq avtopilot)"
        )
        send_message(chat_id, summary)
        return

    # 3. Plan Selection
    if session["step"] == "CONFIRM_STRATEGY":
        if "1" in text:
            session["plan"] = "SELF_HOSTED"
            msg = (
                "✅ *Siz 1-tarifni tanladingiz ($19/oy).*\n\n"
                "Sizga o'z MT5 ingizga o'rnatish uchun yengil `WebhookReceiver.ex5` fayli "
                "va shaxsiy kalitingiz beriladi. Sizning server xarajatingiz $0 bo'ladi!"
            )
            send_message(chat_id, msg)
            session["step"] = "DONE"
        elif "2" in text:
            session["plan"] = "CLOUD_MANAGED"
            session["step"] = "AWAITING_CREDENTIALS"
            msg = (
                "☁️ *Siz 2-tarifni tanladingiz ($39/oy - Bulutli Avtopilot).*\n\n"
                "Endi bot siz uchun serverimizda 24/7 ishlashi uchun MT5 ma'lumotlaringizni "
                "quyidagi formatda yuboring:\n\n"
                "`LOGIN, PAROL, SERVER_NOMI`\n\n"
                "*(Masalan: `106738937, mypassword123, FBS-Demo`)*"
            )
            send_message(chat_id, msg)
        else:
            send_message(chat_id, "Iltimos, `1` yoki `2` raqamini yuboring.")
        return

    # 4. Credential Collection & Docker Spawning
    if session["step"] == "AWAITING_CREDENTIALS":
        parts = [p.strip() for p in text.split(",")]
        if len(parts) != 3:
            send_message(chat_id, "⚠️ Iltimos, ma'lumotlarni vergul bilan ajratib yuboring: `LOGIN, PAROL, SERVER`")
            return

        login, password, server = parts
        send_message(chat_id, "⚙️ *Serverda shaxsiy Docker konteyneringiz yaratilmoqda...* (Taxminan 5 soniya)")

        res = start_client_container(
            user_id=chat_id,
            login=login,
            password=password,
            server=server,
            strategy_config=session.get("strategy_config", {}),
            telegram_chat_id=chat_id
        )

        if res["status"] in ["success", "already_running"]:
            success_msg = (
                f"🎉 *TABRIKLAYMIZ! Botingiz muvaffaqiyatli ishga tushdi!*\n\n"
                f"📦 Konteyner: `{res.get('container_name')}`\n"
                f"👤 Hisob: `{login}` ({server})\n"
                f"⏱️ Holat: `24/7 Faol`\n\n"
                "_Har bir ochilgan va yopilgan savdo bo'yicha hisobotlar to'g'ridan-to'g'ri shu yerga keladi!_"
            )
            send_message(chat_id, success_msg)
            session["step"] = "RUNNING"
        else:
            send_message(chat_id, f"[-] Xatolik yuz berdi: {res.get('error')}")

def main():
    print(f"[*] Starting Master Platform Telegram Bot...")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{MASTER_BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            r = requests.get(url, verify=False, timeout=30)
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)
            time.sleep(1)
        except Exception as e:
            time.sleep(5)

if __name__ == "__main__":
    main()
