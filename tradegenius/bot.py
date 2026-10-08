"""Telegram bot: onboarding, strategy creation (text / screenshot / PDF), accounts, billing, admin."""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape as esc

from . import billing
from .ai_parser import SUPPORTED_MIME, parse_strategy
from .crypto import encrypt
from .shared.strategy_schema import SUPPORTED_SUMMARY, describe_strategy
from .telegram_api import TelegramError, kb
from .templates import TEMPLATES

log = logging.getLogger(__name__)

MEDIA_GROUP_WAIT = 2.5   # seconds to collect all photos of an album

TERMS = (
    "📜 <b>Foydalanish shartlari</b>\n\n"
    "1. TradeGenius — avtomatlashtirish vositasi. Bu <b>investitsiya maslahati emas</b>.\n"
    "2. Forex va CFD savdosi yuqori riskli: siz kiritgan pulning hammasini yo'qotishingiz mumkin.\n"
    "3. Bot faqat <b>siz bergan qoidalarni</b> bajaradi. Strategiya natijasi uchun javobgarlik sizda.\n"
    "4. O'tmishdagi (backtest) natijalar kelajakni kafolatlamaydi.\n"
    "5. Avval <b>DEMO hisobda</b> sinab ko'rishni qat'iy tavsiya qilamiz. Real hisob faqat "
    "alohida ruxsatingiz bilan ishlaydi.\n"
    "6. MT5 parolingiz shifrlangan holda saqlanadi va faqat savdo uchun ishlatiladi.\n\n"
    "Davom etish uchun shartlarga rozilik bildiring."
)

HELP = (
    "❓ <b>Qanday ishlaydi?</b>\n\n"
    "1️⃣ <b>Strategiya</b> — o'z strategiyangizni oddiy so'z bilan yozing, yoki "
    "<b>skrinshot</b> (grafik, indikator sozlamalari) yoki <b>PDF</b> yuboring. AI uni aniq "
    "qoidalarga aylantiradi va sizga tasdiqlash uchun ko'rsatadi.\n"
    "2️⃣ <b>MT5 hisob</b> — login, parol va server nomini kiriting.\n"
    "3️⃣ <b>Ishga tushirish</b> — bot hisobingizga ulanadi, avval backtest natijasini yuboradi, "
    "keyin 24/7 savdo qiladi. Har bir ochilgan/yopilgan savdo haqida xabar keladi.\n\n"
    "<b>Misol:</b>\n<i>«H1 da EMA 9 EMA 21 ni pastdan kesib o'tsa va narx EMA 200 dan yuqori "
    "bo'lsa BUY, teskarisi SELL. SL 2 ATR, TP 1:2. Oltin va EURUSD.»</i>\n\n"
    f"<b>Qo'llab-quvvatlanadi:</b> {esc(SUPPORTED_SUMMARY)}.\n\n"
    "Buyruqlar: /menu — asosiy menyu, /status — botlar holati, /cancel — bekor qilish"
)

STATE_LABEL = {
    "running": "🟢 Ishlamoqda", "halted": "🟠 Kunlik limit — kutmoqda", "starting": "🟡 Ulanmoqda",
    "error": "🔴 Xatolik", "stopped": "⚪️ To'xtatilgan",
}


def main_menu():
    return kb([
        [("🧠 Yangi strategiya", "menu:new"), ("📚 Tayyor shablonlar", "menu:templates")],
        [("📋 Strategiyalarim", "menu:strategies"), ("🔗 MT5 hisoblarim", "menu:accounts")],
        [("📊 Holat", "menu:status"), ("💳 Obuna", "menu:sub")],
        [("❓ Yordam", "menu:help")],
    ])


def back(target="menu:home", label="⬅️ Orqaga"):
    return [(label, target)]


class Bot:
    def __init__(self, db, tg, manager, cfg):
        self.db, self.tg, self.manager, self.cfg = db, tg, manager, cfg
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bot")
        self.user_locks = {}
        self.locks_guard = threading.Lock()
        self.media_groups = {}
        self.media_lock = threading.Lock()

    # ------------------------------------------------------------ polling
    def run(self):
        offset = int(self.db.kv_get("tg_offset", "0"))
        log.info("bot polling started")
        while True:
            try:
                updates = self.tg.get_updates(offset)
            except TelegramError as e:
                log.warning("getUpdates failed: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self.pool.submit(self._safe_handle, u)
            if updates:
                self.db.kv_set("tg_offset", offset)

    def _user_lock(self, uid):
        with self.locks_guard:
            return self.user_locks.setdefault(uid, threading.Lock())

    def _safe_handle(self, u):
        uid = None
        try:
            src = u.get("message") or u.get("callback_query") or u.get("pre_checkout_query") or {}
            uid = (src.get("from") or {}).get("id")
            if uid is None:
                return
            with self._user_lock(uid):
                if "pre_checkout_query" in u:
                    self.on_pre_checkout(u["pre_checkout_query"])
                elif "callback_query" in u:
                    self.on_callback(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
        except Exception:
            log.exception("update handling failed")
            if uid:
                self.tg.send(uid, "❗ Kutilmagan xatolik. Qaytadan urinib ko'ring yoki /menu.")

    # ------------------------------------------------------------ helpers
    def _user(self, frm):
        return self.db.upsert_user(frm["id"], frm.get("username"), frm.get("first_name"))

    def _home(self, uid, edit_msg=None):
        user = self.db.get_user(uid)
        name = esc(user.get("first_name") or "")
        text = (f"🏠 <b>TradeGenius AI</b>\n\nSalom, {name}! Strategiyangizni yozing yoki "
                f"skrinshot/PDF yuboring — men uni 24/7 ishlaydigan MT5 botiga aylantiraman.\n\n"
                f"{billing.subscription_text(user, self.cfg)}")
        if edit_msg:
            self.tg.edit(uid, edit_msg, text, main_menu())
        else:
            self.tg.send(uid, text, main_menu())

    def _deny(self, uid, reason):
        if reason == "no_subscription":
            rows = []
            if self.cfg.payment_provider_token:
                rows.append([("💳 Obuna sotib olish", "pay:buy")])
            self.tg.send(uid, "🔒 Bu funksiya uchun faol obuna kerak.", kb(rows) if rows else None)
        elif reason == "not_allowed":
            self.tg.send(uid, "🔒 Bot hozir yopiq sinov rejimida. Kirish uchun administrator bilan "
                              f"bog'laning. Sizning ID: <code>{uid}</code>")
        else:
            self.tg.send(uid, "🔒 Kirish cheklangan.")

    # ------------------------------------------------------------ messages
    def on_message(self, m):
        if (m.get("chat") or {}).get("type") != "private":
            return
        user = self._user(m["from"])
        uid = user["id"]

        if "successful_payment" in m:
            return self.on_payment(uid, m["successful_payment"])

        text = (m.get("text") or m.get("caption") or "").strip()
        if text.startswith("/"):
            return self.on_command(user, text)

        if not user["accepted_terms"]:
            return self.tg.send(uid, TERMS, kb([[("✅ Roziman", "terms:accept")]]))
        allowed, reason = billing.access(user, self.cfg)
        if not allowed:
            return self._deny(uid, reason)

        state, data = user["state"], user["state_data"]
        if state == "awaiting_login":
            return self.step_login(uid, text)
        if state == "awaiting_password":
            return self.step_password(uid, m, text, data)
        if state == "awaiting_server":
            return self.step_server(uid, text, data)

        files = self._media_refs(m)
        if files is None:
            return self.tg.send(uid, "⚠️ Bu fayl turini o'qiy olmayman. Rasm (JPG/PNG) yoki PDF yuboring.")
        if m.get("media_group_id"):
            return self._collect_media_group(uid, m["media_group_id"], text, files)

        if state == "awaiting_clarify" and (text or files):
            return self._answer_clarification(uid, data, text, files)
        if state == "awaiting_strategy" or files or len(text) >= 40:
            if not text and not files:
                return self.tg.send(uid, "Strategiyangizni yozing yoki skrinshot/PDF yuboring.")
            return self.do_parse(uid, text, files, [])
        self._home(uid)

    def _media_refs(self, m):
        """[[mime, file_id], ...]; None if the message has an unsupported attachment."""
        if m.get("photo"):
            return [["image/jpeg", m["photo"][-1]["file_id"]]]  # largest size
        doc = m.get("document")
        if doc:
            mime = (doc.get("mime_type") or "").lower()
            if mime not in SUPPORTED_MIME:
                return None
            return [[mime, doc["file_id"]]]
        return []

    def _answer_clarification(self, uid, data, text, files):
        history = data.get("history", []) + [[data.get("question", ""), text or "(fayl yubordi)"]]
        return self.do_parse(uid, data.get("text", ""), data.get("files", []) + files, history)

    def _collect_media_group(self, uid, group_id, text, files):
        with self.media_lock:
            g = self.media_groups.get(group_id)
            if g:
                g["timer"].cancel()
            else:
                g = self.media_groups[group_id] = {"files": [], "text": ""}
            g["files"] += files
            g["text"] = g["text"] or text
            g["timer"] = threading.Timer(MEDIA_GROUP_WAIT, self._flush_media_group, (uid, group_id))
            g["timer"].start()

    def _flush_media_group(self, uid, group_id):
        with self.media_lock:
            g = self.media_groups.pop(group_id, None)
        if not g:
            return
        try:
            with self._user_lock(uid):
                user = self.db.get_user(uid)
                if user["state"] == "awaiting_clarify":
                    self._answer_clarification(uid, user["state_data"], g["text"], g["files"])
                else:
                    self.do_parse(uid, g["text"], g["files"], [])
        except Exception:
            log.exception("media group handling failed")
            self.tg.send(uid, "❗ Fayllarni qayta ishlashda xatolik. Qaytadan yuboring.")

    # ------------------------------------------------------------ strategy
    def _take_ai_quota(self, uid):
        """Count one AI analysis for today; False when the user's daily limit is used up."""
        if self.cfg.is_admin(uid):
            return True
        key = f"ai:{uid}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        used = int(self.db.kv_get(key, "0"))
        if used >= self.cfg.ai_daily_limit:
            return False
        self.db.kv_set(key, used + 1)
        return True

    def do_parse(self, uid, text, file_refs, history):
        if not self._take_ai_quota(uid):
            return self.tg.send(uid, f"⏳ Bugungi AI tahlil limiti ({self.cfg.ai_daily_limit} ta) tugadi. "
                                     "Ertaga davom eting yoki tayyor shablonlardan foydalaning.",
                                kb([back("menu:templates", "📚 Tayyor shablonlar")]))
        file_refs = file_refs[:5]
        note = f" ({len(file_refs)} ta fayl bilan)" if file_refs else ""
        self.tg.send(uid, f"🧠 Strategiyangiz tahlil qilinmoqda{note}... (10–30 soniya)")
        self.tg.typing(uid)
        try:
            files = [(mime, self.tg.download(fid)) for mime, fid in file_refs]
        except Exception as e:
            log.warning("download failed: %s", e)
            return self.tg.send(uid, "⚠️ Faylni yuklab bo'lmadi (20 MB dan katta bo'lmasin). Qayta yuboring.")

        res = parse_strategy(text, history=history, files=files)

        if res.status == "ok":
            self.db.set_state(uid, "strategy_preview", {"spec": res.strategy, "source": text[:2000]})
            parts = ["✅ <b>Strategiyangiz shunday tushunildi:</b>\n",
                     f"<pre>{esc(describe_strategy(res.strategy))}</pre>"]
            if res.assumptions:
                parts.append("\n📝 <b>Siz ko'rsatmagan, standart qiymat qo'yilganlar:</b>")
                parts += [f"• {esc(a)}" for a in res.assumptions]
            if res.unsupported:
                parts.append("\n⚠️ <b>Hozircha bajarib bo'lmaydigan qismlar (tashlab ketildi):</b>")
                parts += [f"• {esc(a)}" for a in res.unsupported]
            parts.append("\nHammasi to'g'rimi?")
            return self.tg.send(uid, "\n".join(parts), kb([
                [("✅ Saqlash", "st:save"), ("✏️ Qayta yozish", "st:redo")]]))

        if res.status == "needs_clarification":
            self.db.set_state(uid, "awaiting_clarify", {"text": text, "files": file_refs,
                                                        "history": history, "question": res.question})
            return self.tg.send(uid, f"❓ {esc(res.question)}\n\n<i>Javobingizni yozing yoki /cancel</i>")

        if res.status == "unsupported":
            self.db.set_state(uid, "awaiting_strategy")
            items = "\n".join(f"• {esc(x)}" for x in res.unsupported) or "• —"
            return self.tg.send(uid, "😔 Bu strategiyaning asosiy qismini hozircha avtomatlashtira olmayman:\n"
                                     f"{items}\n\n<b>Qo'llab-quvvatlanadi:</b> {esc(SUPPORTED_SUMMARY)}.\n\n"
                                     "Strategiyani shu indikatorlar bilan qayta yozib ko'ring.",
                                kb([back("menu:templates", "📚 Tayyor shablonlar")]))

        self.db.set_state(uid, "awaiting_strategy")
        self.tg.send(uid, f"⚠️ {esc(res.error or 'Xatolik')}\n\nStrategiyani aniqroq qilib qayta yozing.")

    # ------------------------------------------------------------ accounts flow
    def step_login(self, uid, text):
        login = text.replace(" ", "")
        if not re.fullmatch(r"\d{3,15}", login):
            return self.tg.send(uid, "⚠️ Login faqat raqamlardan iborat bo'ladi. Qayta kiriting yoki /cancel")
        self.db.set_state(uid, "awaiting_password", {"login": login})
        self.tg.send(uid, "🔑 Endi MT5 <b>savdo parolini</b> yuboring.\n"
                          "<i>Xabaringiz darhol o'chiriladi va parol shifrlangan holda saqlanadi.</i>")

    def step_password(self, uid, m, text, data):
        deleted = self.tg.delete(uid, m["message_id"])
        if not text or len(text) > 64:
            return self.tg.send(uid, "⚠️ Parol noto'g'ri. Qayta yuboring yoki /cancel")
        self.db.set_state(uid, "awaiting_server", {"login": data["login"], "pw": encrypt(text)})
        note = "🔒 Parol qabul qilindi va xabar o'chirildi." if deleted else \
            "🔒 Parol qabul qilindi. Xavfsizlik uchun yuborgan xabaringizni o'zingiz o'chiring."
        self.tg.send(uid, f"{note}\n\n🌐 Endi <b>server nomini</b> aynan MT5 dagidek yuboring "
                          "(masalan: <code>Exness-MT5Trial8</code>, <code>FBS-Demo</code>).\n"
                          "<i>MT5 → Fayl → Hisobga kirish oynasida ko'rinadi.</i>")

    def step_server(self, uid, text, data):
        server = text.strip()
        if not re.fullmatch(r"[\w.\- ]{3,64}", server):
            return self.tg.send(uid, "⚠️ Server nomi noto'g'ri. Qayta kiriting yoki /cancel")
        aid = self.db.add_account(uid, data["login"], server, data["pw"])
        self.db.set_state(uid, "idle")
        self.tg.send(uid, f"✅ Hisob saqlandi: <code>{esc(data['login'])}</code> @ {esc(server)}")
        self.choose_strategy_for(uid, aid)

    def choose_strategy_for(self, uid, aid, edit_msg=None):
        strategies = self.db.list_strategies(uid)
        if not strategies:
            text = ("Bu hisobga ulash uchun avval strategiya yarating: o'z strategiyangizni yozing "
                    "yoki tayyor shablonni tanlang.")
            markup = kb([[("🧠 Yangi strategiya", "menu:new"), ("📚 Shablonlar", "menu:templates")]])
        else:
            text = "🧠 Bu hisobda qaysi strategiya ishlasin?"
            markup = kb([[(f"{s['name']} · {s['spec']['timeframe']}", f"acc:use:{aid}:{s['id']}")]
                         for s in strategies[:10]] + [back(f"acc:view:{aid}")])
        if edit_msg:
            self.tg.edit(uid, edit_msg, text, markup)
        else:
            self.tg.send(uid, text, markup)

    def account_text(self, acc):
        st = self.manager.status(acc["id"])
        strategy = self.db.get_strategy(acc["strategy_id"]) if acc["strategy_id"] else None
        if acc["desired_state"] == "running":
            state = STATE_LABEL.get(st.get("state"), "🟡 Ulanmoqda") if st.get("alive") else "🟡 Ishga tushmoqda"
        else:
            state = STATE_LABEL["stopped"]
        lines = [f"🔗 <b>Hisob</b> <code>{esc(acc['login'])}</code> @ {esc(acc['server'])}",
                 f"Holat: {state}",
                 f"Strategiya: {esc(strategy['name']) if strategy else '— tanlanmagan'}",
                 f"Real hisob: {'✅ ruxsat berilgan' if acc['allow_real'] else '🚫 faqat DEMO'}"]
        if "is_demo" in st:
            lines.append(f"Turi: {'DEMO' if st['is_demo'] else '⚠️ REAL'}")
        if st.get("balance") is not None:
            lines.append(f"Balans: {st['balance']:.2f} | Equity: {st.get('equity', 0):.2f} "
                         f"{esc(st.get('currency', ''))}")
        for p in st.get("positions") or []:
            lines.append(f"  • {esc(p['symbol'])} {p['type'].upper()} {p['volume']} → {p['profit']:+.2f}")
        if st.get("stale") and acc["desired_state"] == "running":
            lines.append("⚠️ Engine'dan uzoq vaqt xabar kelmadi")
        if acc["last_error"]:
            lines.append(f"Oxirgi xabar: {esc(acc['last_error'])}")
        return "\n".join(lines)

    def account_kb(self, acc):
        aid = acc["id"]
        toggle = (("⏸ To'xtatish", f"acc:stop:{aid}") if acc["desired_state"] == "running"
                  else ("▶️ Ishga tushirish", f"acc:start:{aid}"))
        return kb([
            [toggle, ("🔄 Yangilash", f"acc:view:{aid}")],
            [("🧠 Strategiyani almashtirish", f"acc:setst:{aid}")],
            [("🔴 Real hisob ruxsati" if not acc["allow_real"] else "🟢 Faqat DEMO ga qaytarish",
              f"acc:real:{aid}")],
            [("🗑 Hisobni o'chirish", f"acc:del:{aid}")],
            back("menu:accounts"),
        ])

    # ------------------------------------------------------------ callbacks
    def on_callback(self, q):
        user = self._user(q["from"])
        uid = user["id"]
        data = q.get("data", "")
        msg_id = (q.get("message") or {}).get("message_id")
        self.tg.answer_callback(q["id"])

        if data == "terms:accept":
            self.db.accept_terms(uid)
            trial = billing.maybe_start_trial(self.db, self.db.get_user(uid), self.cfg)
            if trial:
                self.tg.send(uid, f"🎁 {self.cfg.trial_days} kunlik bepul sinov faollashtirildi.")
            return self._home(uid, msg_id)
        if not user["accepted_terms"]:
            return self.tg.send(uid, TERMS, kb([[("✅ Roziman", "terms:accept")]]))

        if data == "menu:help":
            return self.tg.edit(uid, msg_id, HELP, kb([back()]))
        if data == "menu:sub":
            return self.show_subscription(uid, msg_id)
        if data == "pay:buy":
            return self.send_invoice(uid)

        allowed, reason = billing.access(user, self.cfg)
        if not allowed:
            return self._deny(uid, reason)

        parts = data.split(":")
        head = ":".join(parts[:2])

        if data == "menu:home":
            self.db.set_state(uid, "idle")
            return self._home(uid, msg_id)
        if data == "menu:new":
            self.db.set_state(uid, "awaiting_strategy")
            return self.tg.send(uid, "🧠 Strategiyangizni erkin tilda yozing — yoki <b>skrinshot</b> "
                                     "(grafik, indikator sozlamalari) / <b>PDF</b> yuboring. "
                                     "Bir nechta rasmni birga ham yuborishingiz mumkin.\n\n"
                                     "Qachon BUY/SELL ochiladi, SL/TP qayerda, qaysi aktivlar va "
                                     "vaqt oralig'i — qancha aniq bo'lsa, shuncha yaxshi.\n\n"
                                     "<i>Bekor qilish: /cancel</i>")
        if data == "menu:templates":
            return self.tg.edit(uid, msg_id, "📚 <b>Tayyor shablonlar</b>\nTanlang — tafsilotlarini ko'rasiz:",
                                kb([[(t["title"], f"tpl:{k}")] for k, t in TEMPLATES.items()] + [back()]))
        if parts[0] == "tpl" and len(parts) == 2 and parts[1] in TEMPLATES:
            spec = TEMPLATES[parts[1]]["spec"]
            self.db.set_state(uid, "strategy_preview", {"spec": spec, "source": f"template:{parts[1]}"})
            return self.tg.edit(uid, msg_id, f"<pre>{esc(describe_strategy(spec))}</pre>",
                                kb([[("✅ Saqlash", "st:save")], back("menu:templates")]))
        if data == "st:save":
            return self.save_strategy(uid, user)
        if data == "st:redo":
            self.db.set_state(uid, "awaiting_strategy")
            return self.tg.send(uid, "✏️ Strategiyani qayta yozing yoki yangi skrinshot/PDF yuboring:")
        if data == "menu:strategies":
            return self.show_strategies(uid, msg_id)
        if head == "st:view" and len(parts) == 3:
            return self.show_strategy(uid, int(parts[2]), msg_id)
        if head == "st:del" and len(parts) == 3:
            if not self.db.delete_strategy(int(parts[2]), uid):
                return self.tg.send(uid, "⚠️ Bu strategiya hisobga ulangan. Avval hisobda boshqasini tanlang.")
            return self.show_strategies(uid, msg_id)

        if data == "menu:accounts":
            return self.show_accounts(uid, msg_id)
        if data == "menu:status":
            return self.show_status(uid)
        if data == "acc:add":
            limit = self.cfg.max_accounts_per_user
            if not self.cfg.is_admin(uid) and self.db.count_accounts(uid) >= limit:
                return self.tg.send(uid, f"⚠️ Bitta foydalanuvchiga {limit} ta hisob ruxsat etiladi.")
            self.db.set_state(uid, "awaiting_login")
            return self.tg.send(uid, "🔗 <b>MT5 hisob ulash</b>\n\n💡 Avval <b>DEMO</b> hisob bilan sinab "
                                     "ko'ring — real hisob alohida ruxsat bilan ishlaydi.\n\n"
                                     "MT5 <b>login</b> raqamingizni yuboring:\n<i>Bekor qilish: /cancel</i>")

        if parts[0] == "acc" and len(parts) >= 3 and parts[2].isdigit():
            acc = self.db.get_account(int(parts[2]), uid)
            if not acc:
                return self.tg.send(uid, "Hisob topilmadi.")
            return self.on_account_action(uid, acc, parts[1], parts[3:], msg_id)

    def _refresh_account(self, uid, aid, msg_id, suffix=""):
        acc = self.db.get_account(aid)
        self.tg.edit(uid, msg_id, self.account_text(acc) + suffix, self.account_kb(acc))

    def on_account_action(self, uid, acc, action, rest, msg_id):
        aid = acc["id"]
        if action == "view":
            return self._refresh_account(uid, aid, msg_id)
        if action == "setst":
            if acc["desired_state"] == "running":
                return self.tg.send(uid, "⚠️ Strategiyani almashtirish uchun avval botni to'xtating.")
            return self.choose_strategy_for(uid, aid, msg_id)
        if action == "use" and rest and rest[0].isdigit():
            strategy = self.db.get_strategy(int(rest[0]), uid)
            if not strategy:
                return self.tg.send(uid, "Strategiya topilmadi.")
            if acc["desired_state"] == "running":
                return self.tg.send(uid, "⚠️ Avval botni to'xtating.")
            self.db.update_account(aid, strategy_id=strategy["id"])
            return self._refresh_account(uid, aid, msg_id, "\n\nTayyor! ▶️ Ishga tushirishni bosing.")
        if action == "start":
            if not acc["strategy_id"]:
                return self.choose_strategy_for(uid, aid, msg_id)
            self.db.update_account(aid, desired_state="running", last_error=None)
            self.manager.wake()
            if not self.cfg.engines_enabled:
                self.tg.send(uid, "🛠 Savdo serveri hozir texnik ishlarda. Botingiz navbatga qo'yildi va "
                                  "server tayyor bo'lishi bilan avtomatik ishga tushadi.")
                return self._refresh_account(uid, aid, msg_id)
            self.tg.send(uid, "🚀 Bot ishga tushirilmoqda...\n\nMT5 ga ulanish 1–3 daqiqa olishi mumkin. "
                              "Ulangach, avval <b>backtest natijasi</b> keladi, keyin savdo boshlanadi.")
            return self._refresh_account(uid, aid, msg_id)
        if action == "stop":
            self.db.update_account(aid, desired_state="stopped")
            self.manager.wake()
            self.tg.send(uid, "⏸ Bot to'xtatilmoqda. Ochiq pozitsiyalar <b>yopilmaydi</b> — ular o'z "
                              "SL/TP lari bilan qoladi. Kerak bo'lsa, MT5 da qo'lda yoping.")
            return self._refresh_account(uid, aid, msg_id)
        if action == "real":
            if acc["allow_real"]:
                self.db.update_account(aid, allow_real=0)
                return self._refresh_account(uid, aid, msg_id)
            return self.tg.edit(uid, msg_id,
                                "⚠️ <b>Real pul bilan savdo</b>\n\nRuxsat bersangiz, bot bu hisob REAL "
                                "bo'lsa ham haqiqiy pul bilan savdo qiladi. Zarar ko'rish ehtimoli bor va "
                                "javobgarlik sizda. Strategiyani avval DEMO da sinaganingizga ishonch hosil "
                                "qiling.\n\nRostdan ham ruxsat berasizmi?",
                                kb([[("✅ Ha, tushunaman", f"acc:realok:{aid}")],
                                    back(f"acc:view:{aid}", "❌ Yo'q")]))
        if action == "realok":
            self.db.update_account(aid, allow_real=1)
            note = ("\n\n♻️ O'zgarish kuchga kirishi uchun botni to'xtatib, qayta ishga tushiring."
                    if acc["desired_state"] == "running" else "")
            return self._refresh_account(uid, aid, msg_id, note)
        if action == "del":
            return self.tg.edit(uid, msg_id, "🗑 Hisobni o'chirasizmi? Bot to'xtatiladi, saqlangan parol "
                                             "o'chiriladi. Ochiq pozitsiyalar MT5 da qoladi.",
                                kb([[("🗑 Ha, o'chirish", f"acc:delok:{aid}")],
                                    back(f"acc:view:{aid}", "❌ Yo'q")]))
        if action == "delok":
            self.db.update_account(aid, desired_state="stopped")
            self.manager.wake()
            threading.Thread(target=self._delete_account_later, args=(uid, aid), daemon=True).start()
            return self.tg.edit(uid, msg_id, "✅ Hisob o'chirildi.", kb([back("menu:accounts")]))

    def _delete_account_later(self, uid, aid):
        time.sleep(12)  # let the reconcile loop pick up the stop
        for _ in range(60):
            if not self.manager.is_alive(aid):
                break
            time.sleep(1)
        self.db.delete_account(aid, uid)
        self.manager.forget(aid)

    # ------------------------------------------------------------ views
    def save_strategy(self, uid, user):
        if user["state"] != "strategy_preview":
            return self.tg.send(uid, "Saqlanadigan strategiya topilmadi. /menu")
        data = user["state_data"]
        sid = self.db.add_strategy(uid, data["spec"], data.get("source"))
        self.db.set_state(uid, "idle")
        accounts = self.db.list_accounts(uid)
        if not accounts:
            return self.tg.send(uid, "💾 Strategiya saqlandi!\n\nEndi uni ishga tushirish uchun MT5 "
                                     "hisobingizni ulang.", kb([[("🔗 MT5 hisob ulash", "acc:add")], back()]))
        rows = [[(f"{a['login']} @ {a['server']}", f"acc:use:{a['id']}:{sid}")] for a in accounts
                if a["desired_state"] != "running"]
        if not rows:
            return self.tg.send(uid, "💾 Strategiya saqlandi! Hisobingizda bot ishlab turibdi — almashtirish "
                                     "uchun avval uni to'xtating.", kb([back("menu:accounts", "🔗 Hisoblarim")]))
        self.tg.send(uid, "💾 Strategiya saqlandi! Qaysi hisobga ulaymiz?", kb(rows + [back()]))

    def show_strategies(self, uid, msg_id):
        items = self.db.list_strategies(uid)
        if not items:
            return self.tg.edit(uid, msg_id, "📋 Hali strategiya yo'q.",
                                kb([[("🧠 Yangi strategiya", "menu:new"), ("📚 Shablonlar", "menu:templates")],
                                    back()]))
        rows = [[(f"{s['name']} · {s['spec']['timeframe']}", f"st:view:{s['id']}")] for s in items[:20]]
        self.tg.edit(uid, msg_id, "📋 <b>Strategiyalaringiz</b>", kb(rows + [back()]))

    def show_strategy(self, uid, sid, msg_id):
        s = self.db.get_strategy(sid, uid)
        if not s:
            return self.tg.send(uid, "Strategiya topilmadi.")
        self.tg.edit(uid, msg_id, f"<pre>{esc(describe_strategy(s['spec']))}</pre>",
                     kb([[("🗑 O'chirish", f"st:del:{sid}")], back("menu:strategies")]))

    def show_accounts(self, uid, msg_id):
        accounts = self.db.list_accounts(uid)
        rows = []
        for a in accounts:
            icon = "🟢" if a["desired_state"] == "running" else "⚪️"
            rows.append([(f"{icon} {a['login']} @ {a['server']}", f"acc:view:{a['id']}")])
        rows.append([("➕ Hisob qo'shish", "acc:add")])
        rows.append(back())
        self.tg.edit(uid, msg_id, "🔗 <b>MT5 hisoblaringiz</b>" if accounts else
                     "🔗 Hali MT5 hisob ulanmagan.", kb(rows))

    def show_status(self, uid):
        accounts = self.db.list_accounts(uid)
        if not accounts:
            return self.tg.send(uid, "Hali hisob ulanmagan.", kb([[("➕ Hisob qo'shish", "acc:add")]]))
        for a in accounts:
            self.tg.send(uid, self.account_text(a), self.account_kb(a))

    def show_subscription(self, uid, msg_id):
        user = self.db.get_user(uid)
        text = f"💳 <b>Obuna</b>\n\n{billing.subscription_text(user, self.cfg)}"
        rows = []
        if self.cfg.billing_enabled and not self.cfg.is_admin(uid):
            price = self.cfg.price_amount / 100
            text += (f"\n\nTarif: <b>Bulutli avtopilot</b> — {price:g} {esc(self.cfg.price_currency)} / "
                     f"{self.cfg.subscription_days} kun\n• 24/7 bulutda ishlaydigan bot\n"
                     "• AI strategiya tahlili (matn, skrinshot, PDF)\n• Backtest va real vaqt xabarlari")
            if self.cfg.payment_provider_token:
                rows.append([("💳 Sotib olish", "pay:buy")])
        rows.append(back())
        self.tg.edit(uid, msg_id, text, kb(rows))

    # ------------------------------------------------------------ payments
    def send_invoice(self, uid):
        if not (self.cfg.billing_enabled and self.cfg.payment_provider_token):
            return self.tg.send(uid, "Hozircha to'lov talab qilinmaydi 🙂")
        try:
            self.tg.send_invoice(uid, "TradeGenius — bulutli avtopilot",
                                 f"{self.cfg.subscription_days} kunlik obuna", f"sub:{uid}",
                                 self.cfg.payment_provider_token, self.cfg.price_currency,
                                 self.cfg.price_amount)
        except TelegramError as e:
            log.error("sendInvoice failed: %s", e)
            self.tg.send(uid, "⚠️ To'lov oynasini ochib bo'lmadi. Keyinroq urinib ko'ring.")

    def on_pre_checkout(self, q):
        ok = (q.get("invoice_payload") == f"sub:{q['from']['id']}"
              and q.get("currency") == self.cfg.price_currency
              and q.get("total_amount") == self.cfg.price_amount)
        self.tg.answer_pre_checkout(q["id"], ok, None if ok else "To'lov ma'lumotlari mos kelmadi")

    def on_payment(self, uid, p):
        if not self.db.add_payment(uid, p["total_amount"], p["currency"], p["telegram_payment_charge_id"]):
            return
        exp = billing.extend_subscription(self.db, uid, self.cfg.subscription_days)
        self.tg.send(uid, f"🎉 To'lov qabul qilindi! Obuna {exp.strftime('%Y-%m-%d')} gacha faol.", main_menu())
        for admin in self.cfg.admin_ids:
            self.tg.send(admin, f"💰 Yangi to'lov: {uid}, {p['total_amount'] / 100:g} {esc(p['currency'])}")

    # ------------------------------------------------------------ commands
    def on_command(self, user, text):
        uid = user["id"]
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()

        if cmd in ("/start", "/menu"):
            self.db.set_state(uid, "idle")
            if not user["accepted_terms"]:
                return self.tg.send(uid, TERMS, kb([[("✅ Roziman", "terms:accept")]]))
            return self._home(uid)
        if cmd == "/help":
            return self.tg.send(uid, HELP, main_menu())
        if cmd == "/cancel":
            self.db.set_state(uid, "idle")
            return self.tg.send(uid, "Bekor qilindi.", main_menu())
        if cmd == "/status":
            allowed, reason = billing.access(user, self.cfg)
            return self.show_status(uid) if allowed else self._deny(uid, reason)
        if cmd == "/id":
            return self.tg.send(uid, f"Sizning Telegram ID: <code>{uid}</code>")
        if self.cfg.is_admin(uid):
            return self.on_admin_command(uid, cmd, args)
        self.tg.send(uid, "Noma'lum buyruq. /menu")

    def on_admin_command(self, uid, cmd, args):
        if cmd == "/admin":
            s = self.db.stats()
            return self.tg.send(uid, (
                "🛠 <b>Admin</b>\n"
                f"Foydalanuvchilar: {s['users']} | Strategiyalar: {s['strategies']}\n"
                f"Hisoblar: {s['accounts']} | Ishlayotgan: {s['running']}/{self.cfg.max_active_engines}\n"
                f"To'lovlar: {s['payments']}\n"
                f"Billing: {'YOQILGAN' if self.cfg.billing_enabled else 'ochiq (bepul sinov)'}\n"
                f"Gemini: {'✅' if self.cfg.gemini_api_key else '❌'} ({esc(self.cfg.gemini_model)})\n"
                f"Savdo serveri (Wine/MT5): "
                f"{'❌ o`rnatilmadi' if self.cfg.setup_failed else '✅' if self.cfg.engines_enabled else '⏸ o`chiq'}\n\n"
                "/users — oxirgi foydalanuvchilar\n/grant &lt;id&gt; &lt;kun&gt; — obuna berish\n"
                "/block &lt;id&gt;, /unblock &lt;id&gt;\n/accounts — barcha hisoblar\n"
                "/logs &lt;account_id&gt; — engine logi"))
        if cmd == "/users":
            rows = [f"<code>{u['id']}</code> @{esc(u['username'] or '-')} "
                    f"{'⛔' if u['is_blocked'] else ''} sub:{(u['sub_expires_at'] or '-')[:10]}"
                    for u in self.db.list_users(30)]
            return self.tg.send(uid, "\n".join(rows) or "—")
        if cmd == "/accounts":
            rows = [f"#{a['id']} user {a['user_id']} {esc(a['login'])}@{esc(a['server'])} "
                    f"{a['desired_state']} {'ALIVE' if self.manager.is_alive(a['id']) else ''} "
                    f"{esc(a['last_error'] or '')}" for a in self.db.all_accounts()]
            return self.tg.send(uid, "\n".join(rows) or "—")
        if cmd == "/grant" and len(args) == 2 and args[0].isdigit() and args[1].isdigit():
            target = int(args[0])
            if not self.db.get_user(target):
                return self.tg.send(uid, "Foydalanuvchi topilmadi (u avval botga /start yozishi kerak).")
            exp = billing.extend_subscription(self.db, target, int(args[1]))
            self.tg.send(target, f"🎁 Sizga obuna berildi: {exp.strftime('%Y-%m-%d')} gacha.")
            return self.tg.send(uid, f"✅ {target}: {exp.isoformat()} gacha")
        if cmd in ("/block", "/unblock") and len(args) == 1 and args[0].isdigit():
            target = int(args[0])
            self.db.set_blocked(target, cmd == "/block")
            if cmd == "/block":
                for a in self.db.list_accounts(target):
                    self.db.update_account(a["id"], desired_state="stopped")
                self.manager.wake()
            return self.tg.send(uid, "✅ Bajarildi")
        if cmd == "/logs" and len(args) == 1 and args[0].isdigit():
            tail = self.manager.tail_log(int(args[0]), 40) or "(log bo'sh)"
            return self.tg.send(uid, f"<pre>{esc(tail[-3800:])}</pre>")
        self.tg.send(uid, "Noma'lum admin buyrug'i. /admin")
