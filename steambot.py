import time
import requests

# --- НАСТРОЙКИ ---
TOKEN = "ВАШ_ТОКЕН_БОТА"
CHAT_ID = "ВАШ_CHAT_ID"
APP_ID = "1492070"  # Total War: ROME REMASTERED
CC = "RU"  # Код страны


def send_telegram(message):
    url = f"https://telegram.org{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"Ошибка отправки в TG: {response.text}")
    except Exception as e:
        print(f"Ошибка Telegram: {e}")


def check_steam_discount():
    # ИСПРАВЛЕНО: Правильный слэш и формат ссылки API
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
        else:
            print("Не удалось получить данные от Steam.")
    except Exception as e:
        print(f"Ошибка запроса к Steam: {e}")


if __name__ == "__main__":
    print("Бот успешно запущен и проверяет скидки...")
    while True:
        check_steam_discount()
        time.sleep(3600)  # Проверка каждый час
