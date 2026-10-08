"""
Supervises one Wine engine process per running MT5 account.

The DB column accounts.desired_state is the source of truth; the bot only
flips it and calls wake(). A reconcile loop starts, stops and restarts
engine processes to match, with crash backoff and capacity limits.
"""

import html
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time

from . import billing
from .crypto import decrypt

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FATAL = 0, 2
CRASH_WINDOW = 900
MAX_CRASHES = 3
STOP_GRACE_SECONDS = 30
STATUS_STALE_SECONDS = 120

# Only these variables from the worker's environment reach engine processes;
# ENCRYPTION_KEY, GEMINI_API_KEY etc. must never be inherited.
PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "WINEPREFIX", "WINEDEBUG", "WINEARCH", "DISPLAY",
                   "WINEDLLOVERRIDES", "XDG_RUNTIME_DIR")


# Never handed to engine processes, whatever the platform.
SECRET_ENV = ("ENCRYPTION_KEY", "GEMINI_API_KEY", "PAYMENT_PROVIDER_TOKEN")
IS_WINDOWS = os.name == "nt"


def is_native(cfg):
    """True when engines run as plain processes (Windows) rather than under Wine."""
    return not cfg.wine_cmd


def engine_env(cfg, environ):
    """Base environment for an engine process, without any of the worker's secrets."""
    if is_native(cfg):
        # Windows programs need SYSTEMROOT, TEMP, APPDATA, ...; pass everything except secrets.
        return {k: v for k, v in environ.items() if k not in SECRET_ENV}
    return {k: environ[k] for k in PASSTHROUGH_ENV if k in environ}


def to_win(path):
    return "Z:" + os.path.abspath(path).replace("/", "\\")


def set_ini_values(path, section, values):
    """Set keys in an MT5 ini file (UTF-16 LE with BOM), preserving everything else."""
    lines = []
    if os.path.exists(path):
        with open(path, "rb") as f:
            raw = f.read()
        for enc in ("utf-16", "utf-8-sig", "latin-1"):
            try:
                lines = raw.decode(enc).splitlines()
                break
            except UnicodeDecodeError:
                continue
    header = f"[{section}]"
    out, in_section, found_section, done = [], False, False, set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                out += [f"{k}={v}" for k, v in values.items() if k not in done]
                done.update(values)
            in_section = stripped.lower() == header.lower()
            found_section = found_section or in_section
            out.append(line)
            continue
        if in_section and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                done.add(key)
                continue
        out.append(line)
    if in_section:
        out += [f"{k}={v}" for k, v in values.items() if k not in done]
    elif not found_section:
        out += [header] + [f"{k}={v}" for k, v in values.items()]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write("\r\n".join(out).encode("utf-16"))


class WorkerManager:
    def __init__(self, db, tg, cfg):
        self.db, self.tg, self.cfg = db, tg, cfg
        self.procs = {}          # account id -> {"proc", "log", "started"}
        self.crashes = {}        # account id -> [timestamps]
        self.next_start = {}     # account id -> earliest restart time
        self.capacity_warned = set()
        self.stopping = set()
        self.lock = threading.RLock()
        self.wake_event = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ paths
    def client_dir(self, aid):
        return os.path.join(self.cfg.clients_dir, f"acc_{aid}")

    def _file(self, aid, name):
        return os.path.join(self.client_dir(aid), name)

    # ----------------------------------------------------------- public
    def start(self):
        os.makedirs(self.cfg.clients_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="worker-manager", daemon=True)
        self._thread.start()

    def wake(self):
        self.wake_event.set()

    def is_alive(self, aid):
        with self.lock:
            p = self.procs.get(aid)
            return bool(p and p["proc"].poll() is None)

    def status(self, aid):
        st = {}
        try:
            with open(self._file(aid, "status.json"), encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            pass
        st["alive"] = self.is_alive(aid)
        st["stale"] = bool(st.get("ts")) and time.time() - st["ts"] > STATUS_STALE_SECONDS
        return st

    def tail_log(self, aid, lines=30):
        try:
            with open(self._file(aid, "engine.log"), encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except OSError:
            return ""

    def forget(self, aid):
        """Remove all local files of a deleted account (terminal copy, logs, status)."""
        shutil.rmtree(self.client_dir(aid), ignore_errors=True)

    # ------------------------------------------------------------- loop
    def _loop(self):
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            self.wake_event.wait(10)
            self.wake_event.clear()

    def reconcile(self):
        if not self.cfg.engines_enabled:
            return
        desired = {a["id"]: a for a in self.db.running_accounts()}

        with self.lock:
            running_ids = list(self.procs)
        for aid in running_ids:
            if aid not in desired and aid not in self.stopping:
                self._stop_async(aid)

        for aid, acc in desired.items():
            user = self.db.get_user(acc["user_id"])
            allowed, reason = billing.access(user, self.cfg) if user else (False, "blocked")
            if not allowed:
                self.db.update_account(aid, desired_state="stopped",
                                       last_error="Obuna faol emas" if reason == "no_subscription"
                                       else "Kirish huquqi yo'q")
                self.tg.send(acc["user_id"], "⏸ Botingiz to'xtatildi: obuna faol emas. "
                                             "Davom ettirish uchun /menu → 💳 Obuna.")
                continue

            with self.lock:
                entry = self.procs.get(aid)
            if entry:
                code = entry["proc"].poll()
                if code is None:
                    continue
                self._on_exit(aid, acc, code)
                continue

            if time.time() < self.next_start.get(aid, 0) or aid in self.stopping:
                continue
            with self.lock:
                active = len(self.procs)
            if active >= self.cfg.max_active_engines:
                if aid not in self.capacity_warned:
                    self.capacity_warned.add(aid)
                    self.db.update_account(aid, last_error="Server band — navbatda")
                    self.tg.send(acc["user_id"], "⏳ Server hozir to'liq band. Botingiz navbatga "
                                                 "qo'yildi va joy bo'shashi bilan ishga tushadi.")
                continue
            self.capacity_warned.discard(aid)
            try:
                self._spawn(acc)
            except Exception as e:
                log.exception("spawn failed for account %s", aid)
                self.db.update_account(aid, desired_state="stopped", last_error=f"Ishga tushmadi: {e}")
                self.tg.send(acc["user_id"], "❗ Botni ishga tushirib bo'lmadi. Administrator "
                                             "xabardor qilindi.")
                self._notify_admins(f"spawn failed for account {aid}: {e}")

    def _on_exit(self, aid, acc, code):
        with self.lock:
            entry = self.procs.pop(aid, None)
        if entry:
            entry["log"].close()
        st = self.status(aid)
        if code == EXIT_FATAL:
            # The engine already told the user why; just persist it.
            self.db.update_account(aid, desired_state="stopped",
                                   last_error=st.get("message") or "Xatolik")
            return
        if code == EXIT_OK:
            return  # stopped on request; reconcile restarts it if still desired
        now = time.time()
        recent = [t for t in self.crashes.get(aid, []) if now - t < CRASH_WINDOW] + [now]
        self.crashes[aid] = recent
        log.warning("engine %s crashed (code %s), %d recent crashes", aid, code, len(recent))
        if len(recent) >= MAX_CRASHES:
            self.db.update_account(aid, desired_state="stopped",
                                   last_error="Ketma-ket xatoliklar sababli to'xtatildi")
            self.tg.send(acc["user_id"], "⛔ Bot bir necha marta xatolik bilan to'xtadi va "
                                         "xavfsizlik uchun o'chirildi. Administrator tekshiradi.")
            self._notify_admins(f"account {aid} crashed {len(recent)}x:\n{self.tail_log(aid, 15)}")
            self.crashes[aid] = []
            return
        self.next_start[aid] = now + 30 * len(recent)

    # ------------------------------------------------------ spawn/stop
    def _prepare_terminal(self, aid):
        base = self.cfg.mt5_base_dir
        if not base or not os.path.exists(os.path.join(base, "terminal64.exe")):
            raise RuntimeError(f"MT5 base install not found at {base!r}")
        term = os.path.join(self.client_dir(aid), "terminal")
        if not os.path.exists(os.path.join(term, "terminal64.exe")):
            shutil.copytree(base, term, dirs_exist_ok=True)
        # Python API trading requires the terminal's "Algo Trading" switch to be on.
        set_ini_values(os.path.join(term, "config", "common.ini"), "Experts",
                       {"Enabled": "1", "AllowLiveTrading": "1", "AllowDllImport": "0"})
        return term

    def _spawn(self, acc):
        aid = acc["id"]
        strategy = self.db.get_strategy(acc["strategy_id"]) if acc["strategy_id"] else None
        if not strategy:
            raise RuntimeError("strategiya tanlanmagan")
        os.makedirs(self.client_dir(aid), exist_ok=True)
        term = self._prepare_terminal(aid)
        control = self._file(aid, "control.json")
        if os.path.exists(control):
            os.remove(control)

        log_path = self._file(aid, "engine.log")
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5 * 1024 * 1024:
            os.replace(log_path, log_path + ".1")
        log_fh = open(log_path, "a", encoding="utf-8")

        native = is_native(self.cfg)
        path_for_engine = os.path.abspath if native else to_win
        env = engine_env(self.cfg, os.environ)
        env.update({
            "ACCOUNT_ID": str(aid),
            "MT5_LOGIN": acc["login"],
            "MT5_PASSWORD": decrypt(acc["password_enc"]),
            "MT5_SERVER": acc["server"],
            "MT5_PATH": path_for_engine(os.path.join(term, "terminal64.exe")),
            "STRATEGY_JSON": json.dumps(strategy["spec"]),
            "ALLOW_REAL": "1" if acc["allow_real"] else "0",
            "BACKTEST_BARS": str(self.cfg.backtest_bars),
            "TELEGRAM_BOT_TOKEN": self.cfg.telegram_bot_token,
            "TELEGRAM_CHAT_ID": str(acc["user_id"]),
            "STATUS_FILE": path_for_engine(self._file(aid, "status.json")),
            "CONTROL_FILE": path_for_engine(control),
            "APP_ROOT": path_for_engine(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "PYTHONIOENCODING": "utf-8",
        })
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS
                 else {"start_new_session": True})
        proc = subprocess.Popen(self.cfg.wine_cmd + [self.cfg.wine_python, self.cfg.engine_script],
                                cwd=self.client_dir(aid), env=env, stdout=log_fh,
                                stderr=subprocess.STDOUT, **group)
        with self.lock:
            self.procs[aid] = {"proc": proc, "log": log_fh, "started": time.time()}
        self.db.update_account(aid, last_error=None)
        log.info("started engine for account %s (pid %s)", aid, proc.pid)

    def _stop_async(self, aid):
        self.stopping.add(aid)
        threading.Thread(target=self._stop, args=(aid,), daemon=True).start()

    def _stop(self, aid):
        try:
            with open(self._file(aid, "control.json"), "w", encoding="utf-8") as f:
                json.dump({"state": "stop"}, f)
            with self.lock:
                entry = self.procs.get(aid)
            if entry:
                try:
                    entry["proc"].wait(STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        if IS_WINDOWS:
                            entry["proc"].kill()
                        else:
                            os.killpg(entry["proc"].pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
            # The MT5 terminal launched by the engine outlives it; kill it by its unique path.
            self._kill_terminal(aid)
            with self.lock:
                entry = self.procs.pop(aid, None)
            if entry:
                entry["log"].close()
            log.info("stopped engine for account %s", aid)
        finally:
            self.stopping.discard(aid)

    def _kill_terminal(self, aid):
        """The MT5 terminal launched by the engine outlives it; kill it by its unique path."""
        if IS_WINDOWS:
            term = os.path.join(self.client_dir(aid), "terminal", "terminal64.exe")
            ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq '%s' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % term.replace("'", "''"))
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.run(["pkill", "-f", rf"acc_{aid}[\\/]"], check=False)

    def stop_all(self):
        with self.lock:
            ids = list(self.procs)
        for aid in ids:
            self._stop(aid)

    def _notify_admins(self, text):
        for admin in self.cfg.admin_ids:
            self.tg.send(admin, f"🛠 <code>{html.escape(text[:3500])}</code>")
