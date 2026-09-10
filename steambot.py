import os
import time
import random
import requests
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ============================================================
# НАСТРОЙКИ (берутся из Environment Variables на Render)
# ============================================================
TOKEN   = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
APP_ID  = "1492070"          # Total War: ROME REMASTERED
CC      = "RU"               # Регион цен

# Интервалы (в секундах)
STEAM_CHECK_INTERVAL   = 3600          # проверка скидки — раз в час
HEARTBEAT_MIN_INTERVAL = 4 * 3600      # минимум 4 часа между heartbeat
HEARTBEAT_MAX_INTERVAL = 6 * 3600      # максимум 6 часов

# Секретный путь для ручной проверки:
#   GET https://<твой-сервис>.onrender.com/ping?key=<HEARTBEAT_KEY>
HEARTBEAT_KEY = os.environ.get("HEARTBEAT_KEY", "")

# ============================================================
# СОСТОЯНИЕ (в памяти процесса)
# ============================================================
_state = {
    "last_discount": 0,
    "last_heartbeat": 0.0,
    "started_at": time.time(),
    "steam_checks": 0,
    "steam_errors": 0,
}

# ============================================================
# HTTP-СЕРВЕР
# ============================================================
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        # --- Ручной heartbeat: /ping?key=XXXX ---
        if path == "/ping":
            if not HEARTBEAT_KEY:
                self._send(403, b"Manual ping disabled (HEARTBEAT_KEY not set)")
                return
            key = ""
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("key="):
                        key = part[4:]
                        break
            if key != HEARTBEAT_KEY:
                self._send(403, b"Forbidden")
                return
            Thread(target=send_heartbeat, args=("manual",), daemon=True).start()
            self._send(200, b"Heartbeat triggered")
            return

        # --- Корень и всё остальное: health-check для Render ---
        uptime = int(time.time() - _state["started_at"])
        body = (
            f"Bot is running successfully!\n"
            f"Uptime: {uptime}s\n"
            f"Steam checks: {_state['steam_checks']} ok / "
            f"{_state['steam_errors']} errors\n"
            f"Last discount sent: -{_state['last_discount']}%\n"
        ).encode()
        self._send(200, body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()

    def log_message(self, *args):
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[web] HTTP-сервер запущен на 0.0.0.0:{port}", flush=True)
    server.serve_forever()


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message: str, silent: bool = False) -> bool:
    if not TOKEN or not CHAT_ID:
        print("[tg] TOKEN или CHAT_ID не заданы", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[tg] error {r.status_code}: {r.text}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"[tg] exception: {e}", flush=True)
        return False


# ============================================================
# HEARTBEAT
# ============================================================
def _format_uptime(seconds: int) -> str:
    d, seconds = divmod(seconds, 86400)
    h, seconds = divmod(seconds, 3600)
    m, s = divmod(seconds, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    parts.append(f"{s}с")
    return " ".join(parts)


def send_heartbeat(reason: str = "scheduled"):
    now = time.time()
    uptime = _format_uptime(int(now - _state["started_at"]))
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    if reason == "manual":
        title = "🔔 Ручная проверка"
    elif reason == "startup":
        title = "🟢 Бот запущен"
    else:
        title = "✅ Бот всё ещё работает"

    msg = (
        f"{title}\n"
        f"🕒 {ts}\n"
        f"⏱ Аптайм: {uptime}\n"
        f"🔎 Проверок Steam: {_state['steam_checks']} "
        f"(ошибок: {_state['steam_errors']})\n"
        f"💸 Текущая отслеживаемая скидка: "
        f"{('-' + str(_state['last_discount']) + '%') if _state['last_discount'] else 'нет'}"
    )
    if send_telegram(msg, silent=(reason != "manual")):
        _state["last_heartbeat"] = now
        print(f"[hb] heartbeat отправлен ({reason})", flush=True)


# ============================================================
# STEAM
# ============================================================
def check_steam_discount():
    url = (
        f"https://store.steampowered.com/api/appdetails"
        f"?appids={APP_ID}&cc={CC}&l=russian"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        payload = r.json()
        _state["steam_checks"] += 1
    except Exception as e:
        _state["steam_errors"] += 1
        print(f"[steam] exception: {e}", flush=True)
        return

    app = payload.get(APP_ID)
    if not app or not app.get("success"):
        print("[steam] success=false", flush=True)
        return

    data = app["data"]
    name = data.get("name", f"App {APP_ID}")

    if "price_overview" not in data:
        print("[steam] цены нет (F2P или недоступна в регионе)", flush=True)
        if _state["last_discount"] != 0:
            _state["last_discount"] = 0
        return

    p = data["price_overview"]
    disc = p["discount_percent"]
    final_price   = p.get("final_formatted", "?")
    initial_price = p.get("initial_formatted", "?")

    if disc > 0 and disc != _state["last_discount"]:
        msg = (
            f"🔥 Скидка на {name}!\n"
            f"📉 Размер: -{disc}%\n"
            f"💰 {final_price} (было {initial_price})\n"
            f"🔗 https://store.steampowered.com/app/{APP_ID}"
        )
        if send_telegram(msg):
            _state["last_discount"] = disc
            print(f"[steam] уведомление отправлено: -{disc}%", flush=True)

    elif disc == 0 and _state["last_discount"] != 0:
        _state["last_discount"] = 0
        send_telegram(f"ℹ️ Скидка на {name} закончилась.", silent=True)
        print("[steam] скидка закончилась", flush=True)

    else:
        print(f"[steam] без изменений (discount={disc}%)", flush=True)


# ============================================================
# ФОНОВЫЕ ЦИКЛЫ
# ============================================================
def bot_loop():
    print("[bot] цикл проверки Steam запущен", flush=True)
    time.sleep(10)

    while True:
        try:
            check_steam_discount()
        except Exception as e:
            print(f"[bot] unexpected error: {e}", flush=True)
        time.sleep(STEAM_CHECK_INTERVAL)


def heartbeat_loop():
    print("[hb] цикл heartbeat запущен", flush=True)
    time.sleep(60)
    send_heartbeat(reason="startup")

    while True:
        delay = random.randint(HEARTBEAT_MIN_INTERVAL, HEARTBEAT_MAX_INTERVAL)
        print(f"[hb] следующий heartbeat через {delay // 60} мин", flush=True)
        time.sleep(delay)
        send_heartbeat(reason="scheduled")


# ============================================================
# ТОЧКА ВХОДА
# ============================================================
if __name__ == "__main__":
    print("[main] запуск...", flush=True)

    Thread(target=bot_loop,       daemon=True).start()
    Thread(target=heartbeat_loop, daemon=True).start()

    run_web_server()
