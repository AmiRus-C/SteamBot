import os
import time
import random
import threading
from threading import Thread, Lock
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
import requests

# ============================================================
# НАСТРОЙКИ (берутся из Environment Variables на Render)
# ============================================================
TOKEN   = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
APP_ID  = os.environ.get("STEAM_APP_ID", "1492070")  # Total War: ROME REMASTERED
CC      = os.environ.get("STEAM_CC", "RU")            # Регион цен

# Интервалы (в секундах)
STEAM_CHECK_INTERVAL   = 3600          # проверка скидки — раз в час
HEARTBEAT_MIN_INTERVAL = 4 * 3600      # минимум 4 часа между heartbeat
HEARTBEAT_MAX_INTERVAL = 6 * 3600      # максимум 6 часов
TELEGRAM_POLL_INTERVAL  = 3            # опрос Long Polling для Telegram

# Секретный путь для ручной проверки по HTTP
HEARTBEAT_KEY = os.environ.get("HEARTBEAT_KEY", "")

# ============================================================
# СОСТОЯНИЕ И СИНХРОНИЗАЦИЯ (Thread-Safe State)
# ============================================================
_state_lock = Lock()
_state = {
    "last_discount": 0,
    "last_heartbeat": 0.0,
    "started_at": time.time(),
    "steam_checks": 0,
    "steam_errors": 0,
    "last_error": None,
    "last_successful_check": 0.0,
    "last_telemetry_check": 0.0,
}

def update_state(**kwargs):
    """Потокобезопасное обновление состояния."""
    with _state_lock:
        for key, value in kwargs.items():
            if key in _state:
                _state[key] = value

def get_state_snapshot() -> dict:
    """Потокобезопасное чтение копии состояния."""
    with _state_lock:
        return _state.copy()


# ============================================================
# HTTP-СЕРВЕР (Health Checks для Render / UptimeRobot)
# ============================================================
class Handler(BaseHTTPRequestHandler):
    def _build_status_text(self) -> bytes:
        state = get_state_snapshot()
        uptime = int(time.time() - state["started_at"])
        last_err = state["last_error"] or "Нет"
        text = (
            f"Bot is running successfully!\n"
            f"Uptime: {uptime}s\n"
            f"Steam checks: {state['steam_checks']} ok / {state['steam_errors']} errors\n"
            f"Last discount sent: -{state['last_discount']}%\n"
            f"Last error: {last_err}\n"
        )
        return text.encode("utf-8")

    def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
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

        # --- Корень и остальные пути: health-check ---
        self._send(200, self._build_status_text())

    def do_HEAD(self):
        # Отвечаем корректными заголовками без передачи тела ответа
        body = self._build_status_text()
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, format, *args):
        # Подавление стандартных логов подключения в консоль Render
        pass


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[web] HTTP-сервер запущен на 0.0.0.0:{port}", flush=True)
    server.serve_forever()


# ============================================================
# TELEGRAM API & COMMANDS
# ============================================================
def send_telegram(message: str, silent: bool = False) -> bool:
    if not TOKEN or not CHAT_ID:
        print("[tg] TOKEN или CHAT_ID не заданы", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
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


def generate_status_report() -> str:
    """Генерирует форматированный отчёт о текущем состоянии для Telegram."""
    state = get_state_snapshot()
    now = time.time()
    uptime_str = _format_uptime(int(now - state["started_at"]))
    
    # Диагностика работоспособности
    time_since_check = now - state["last_successful_check"] if state["last_successful_check"] > 0 else None
    
    # Если просрочено более 2 с половиной циклов (2.5 часа) — считаем, что сервис застрял
    is_stale = time_since_check is not None and time_since_check > (STEAM_CHECK_INTERVAL * 2.5)
    
    if is_stale:
        status_header = "🔴 <b>Не работает: задержка выполнения проверок</b>"
    elif state["steam_errors"] > 0 and state["steam_checks"] == 0:
        status_header = "🔴 <b>Не работает: критическая ошибка подключения</b>"
    elif state["last_error"] and (state["steam_errors"] / max(1, (state["steam_checks"] + state["steam_errors"]))) > 0.5:
        status_header = "⚠️ <b>Работает нестабильно (высокий % ошибок)</b>"
    else:
        status_header = "🟢 <b>Работает штатно</b>"

    last_check_str = datetime.fromtimestamp(state["last_successful_check"], tz=timezone.utc).strftime("%H:%M:%S UTC") if state["last_successful_check"] > 0 else "Еще не было"
    err_desc = f"\n❌ <b>Последняя ошибка:</b> <code>{state['last_error']}</code>" if state["last_error"] else ""
    discount_str = f"-{state['last_discount']}%" if state["last_discount"] > 0 else "нет скидки"

    report = (
        f"Статус системы:\n"
        f"{status_header}\n\n"
        f"⏱ <b>Uptime:</b> {uptime_str}\n"
        f"🔎 <b>Успешных проверок:</b> {state['steam_checks']}\n"
        f"⚠️ <b>Ошибок подключения:</b> {state['steam_errors']}\n"
        f"🕒 <b>Последняя проверка:</b> {last_check_str}\n"
        f"🏷 <b>Активная скидка:</b> {discount_str}"
        f"{err_desc}"
    )
    return report


def telegram_polling_loop():
    """Обработка команд /status в Telegram в автономном цикле."""
    if not TOKEN:
        print("[tg_poll] TOKEN не задан. Команды отключены.", flush=True)
        return

    offset = 0
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    print("[tg_poll] Цикл Telegram Long Polling запущен", flush=True)

    while True:
        try:
            params = {"offset": offset, "timeout": 20}
            response = requests.get(url, params=params, timeout=25)
            if response.status_code == 200:
                data = response.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    text = message.get("text", "").strip()
                    chat = message.get("chat", {})
                    incoming_chat_id = str(chat.get("id"))

                    # Отвечаем только на указанный CHAT_ID во избежание постороннего доступа
                    if CHAT_ID and incoming_chat_id != str(CHAT_ID):
                        continue

                    if text in ["/status", "/status@YourBot"]:
                        report = generate_status_report()
                        send_telegram(report)
                    elif text in ["/start", "/help"]:
                        send_telegram("👋 Бот на связи. Используйте /status для проверки состояния.")
        except Exception as e:
            # Скрываем временные сбои таймаута сети, логгируем остальные
            time.sleep(5)
        time.sleep(TELEGRAM_POLL_INTERVAL)


# ============================================================
# HEARTBEAT
# ============================================================
def send_heartbeat(reason: str = "scheduled"):
    state = get_state_snapshot()
    uptime = _format_uptime(int(time.time() - state["started_at"]))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if reason == "manual":
        title = "🔔 Ручная проверка"
    elif reason == "startup":
        title = "🟢 Бот запущен"
    else:
        title = "✅ Бот всё ещё работает"

    disc_text = f"-{state['last_discount']}%" if state['last_discount'] else "нет"
    msg = (
        f"<b>{title}</b>\n"
        f"🕒 {ts}\n"
        f"⏱ Аптайм: {uptime}\n"
        f"🔎 Проверок Steam: {state['steam_checks']} (ошибок: {state['steam_errors']})\n"
        f"💸 Скидка: {disc_text}"
    )
    if send_telegram(msg, silent=(reason != "manual")):
        update_state(last_heartbeat=time.time())
        print(f"[hb] heartbeat отправлен ({reason})", flush=True)


# ============================================================
# STEAM MONITORING
# ============================================================
def check_steam_discount():
    url = f"https://store.steampowered.com/api/appdetails?appids={APP_ID}&cc={CC}&l=russian"
    state = get_state_snapshot()

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        payload = r.json()
        
        # Обновляем счётчик успешных вызовов
        update_state(
            steam_checks=state["steam_checks"] + 1,
            last_successful_check=time.time(),
            last_error=None
        )
    except Exception as e:
        err_msg = str(e)
        update_state(
            steam_errors=state["steam_errors"] + 1,
            last_error=err_msg
        )
        print(f"[steam] exception: {err_msg}", flush=True)
        return

    app = payload.get(str(APP_ID))
    if not app or not app.get("success"):
        print("[steam] API вернуло success=false", flush=True)
        update_state(last_error="Steam API success=false")
        return

    data = app.get("data", {})
    name = data.get("name", f"App {APP_ID}")

    if "price_overview" not in data:
        print("[steam] Цена не найдена (F2P или не доступно в регионе)", flush=True)
        if state["last_discount"] != 0:
            update_state(last_discount=0)
        return

    p = data["price_overview"]
    disc = p.get("discount_percent", 0)
    final_price   = p.get("final_formatted", "?")
    initial_price = p.get("initial_formatted", "?")

    # --- Новая или изменившаяся скидка ---
    if disc > 0 and disc != state["last_discount"]:
        msg = (
            f"🔥 <b>Скидка на {name}!</b>\n"
            f"📉 <b>Размер:</b> -{disc}%\n"
            f"💰 <b>Цена:</b> {final_price} (было {initial_price})\n"
            f"🔗 <a href='https://store.steampowered.com/app/{APP_ID}'>Открыть в Steam</a>"
        )
        if send_telegram(msg):
            update_state(last_discount=disc)
            print(f"[steam] Уведомление отправлено: -{disc}%", flush=True)

    # --- Скидка закончилась ---
    elif disc == 0 and state["last_discount"] != 0:
        update_state(last_discount=0)
        send_telegram(f"ℹ️ Скидка на <b>{name}</b> закончилась.", silent=True)
        print("[steam] Скидка закончилась", flush=True)
    else:
        print(f"[steam] Без изменений (discount={disc}%)", flush=True)


# ============================================================
# ФОНОВЫЕ ЦИКЛЫ
# ============================================================
def bot_loop():
    print("[bot] Цикл проверки Steam запущен", flush=True)
    time.sleep(10)

    while True:
        try:
            check_steam_discount()
        except Exception as e:
            print(f"[bot] unexpected error: {e}", flush=True)
            update_state(last_error=str(e))
        time.sleep(STEAM_CHECK_INTERVAL)


def heartbeat_loop():
    print("[hb] Цикл heartbeat запущен", flush=True)
    time.sleep(60)
    send_heartbeat(reason="startup")

    while True:
        delay = random.randint(HEARTBEAT_MIN_INTERVAL, HEARTBEAT_MAX_INTERVAL)
        print(f"[hb] Следующий heartbeat через {delay // 60} мин", flush=True)
        time.sleep(delay)
        send_heartbeat(reason="scheduled")


# ============================================================
# ТОЧКА ВХОДА
# ============================================================
if __name__ == "__main__":
    print("[main] Запуск сервиса...", flush=True)

    # Запуск фоновых потоков
    Thread(target=bot_loop, daemon=True).start()
    Thread(target=heartbeat_loop, daemon=True).start()
    Thread(target=telegram_polling_loop, daemon=True).start()

    # Веб-сервер блокирует главный поток для поддержания работы процесса Render
    run_web_server()
