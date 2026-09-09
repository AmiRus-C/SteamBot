import os
import time
import requests
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- НАСТРОЙКИ (БЕРУТСЯ ИЗ НАСТРОЕК RENDER) ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
APP_ID = "1492070"  # Total War: ROME REMASTERED
CC = "RU"  # Код страны

# ОБМАНКА ДЛЯ БЕСПЛАТНОГО ТАРИФА RENDER
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running successfully!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    print(f"Веб-сервер запущен на порту {port}")
    server.serve_forever()

# --- ОСНОВНАЯ ЛОГИКА БОТА ---
def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Ошибка: Токен или Chat ID не заданы в настройках Render!")
        return
    url = f"https://telegram.org{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Ошибка Telegram: {e}")

def check_steam_discount():
    url = f"https://steampowered.com{APP_ID}&cc={CC}"
    try:
        response = requests.get(url).json()
        if response and response[APP_ID]["success"]:
            data = response[APP_ID]["data"]
            name = data["name"]

            if "price_overview" in data:
                price_info = data["price_overview"]
                discount_percent = price_info["discount_percent"]
                final_price = price_info["final_formatted"]
                initial_price = price_info["initial_formatted"]

                if discount_percent > 0:
                    message = (
                        f"🔥 *Скидка на {name}!*\n"
                        f"📉 Размер скидки: -{discount_percent}%\n"
                        f"💰 Новая цена: {final_price} (вместо {initial_price})\n"
                        f"🔗 [Страница в Steam](https://steampowered.com{APP_ID})"
                    )
                    send_telegram(message)
                    print("Скидка есть, уведомление отправлено!")
                else:
                    print("Скидки пока нет.")
            else:
                print("Игра сейчас бесплатна или цена не указана.")
    except Exception as e:
        print(f"Ошибка запроса к Steam: {e}")

def bot_loop():
    print("Бот успешно запущен на Render и проверяет скидки...")
    while True:
        check_steam_discount()
        time.sleep(3600)  # Проверка каждый час

if __name__ == "__main__":
    # ТЕПЕРЬ ИСПРАВЛЕНО: Бот уходит в фон, а веб-сервер держит главный поток
    bot_thread = Thread(target=bot_loop)
    bot_thread.daemon = True
    bot_thread.start()

    # Веб-сервер запускается мгновенно, Render сразу увидит порт
    run_web_server()
