import json
import logging
import threading
import time
import urllib.request
import os
from datetime import datetime
from urllib.parse import urlencode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.environ.get("BOT_TOKEN", "")
LAT, LON = 53.9006, 27.5590   # Минск
FORECAST_URL = "https://wttr.in/Minsk"   # страница с погодой для кнопки

DEFAULT_MORNING = 8    # время рассылки по умолчанию: утро, час
DEFAULT_EVENING = 20    # вечер, час
TIMEZONE = "Europe/Minsk"

SUBSCRIBERS_FILE = "subscribers.json"
SETTINGS_FILE = "settings.json"

API = f"https://api.telegram.org/bot{TOKEN}"

# коды погоды Open-Meteo (WMO) -> русские слова
CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "туман с изморозью",
    51: "морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "ледяная морость",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снег",
    80: "небольшие ливни", 81: "ливни", 82: "сильные ливни",
    85: "снежные ливни", 86: "сильные снежные ливни",
    95: "гроза", 96: "гроза с градом", 99: "гроза с градом",
}
RAINY_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
SNOWY_CODES = {71, 73, 75, 77, 85, 86}

PARTS = {"night": (0, 6), "morning": (6, 12), "day": (12, 18), "evening": (18, 24)}
PART_NAMES = {"night": "ночью", "morning": "утром", "day": "днём", "evening": "вечером"}

# ---------- текст для пользователя ----------

START_TEXT = (
    "Привет! 👋 Я присылаю погоду в Минске:\n\n"
    "☀️ Утром — погода на сегодня\n"
    "🌙 Вечером — прогноз на завтра\n\n"
    "Всё управляется кнопками 👇\n\n"
    "🌤 «Погода сейчас» — покажу погоду в этот момент\n\n"
    "⚙️ «Настройки»:\n"
    "• «Изменить время» — выбрать, во сколько приходят письма "
    "(например, 7 / 21 — в 7 утра и в 9 вечера)\n"
    "• «Тихий режим» — нажми, и вечером я буду писать только "
    "если завтра дождь или снег. Нажми ещё раз — вернётся как было\n\n"
    "❌ «Отписаться» — отключить уведомления совсем"
)

ERROR_TEXT = "😢 Не получилось узнать погоду. Посмотри прямо на сайте:"

# ---------- кнопки ----------

BTN_WEATHER = "🌤 Погода сейчас"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SUBSCRIBE = "✅ Подписаться"
BTN_UNSUBSCRIBE = "❌ Отписаться"
BTN_TIME = "🕐 Изменить время"
BTN_QUIET = "🔕 Тихий режим"
BTN_BACK = "⬅️ Назад"

TIME_PRESETS = ["7 / 21", "8 / 20", "9 / 22", "6 / 23"]

KB_MAIN = json.dumps({
    "keyboard": [[BTN_WEATHER], [BTN_SETTINGS], [BTN_UNSUBSCRIBE]],
    "resize_keyboard": True})
KB_UNSUBBED = json.dumps({
    "keyboard": [[BTN_SUBSCRIBE]],
    "resize_keyboard": True})
KB_SETTINGS = json.dumps({
    "keyboard": [[BTN_TIME], [BTN_QUIET], [BTN_BACK]],
    "resize_keyboard": True})
KB_TIMES = json.dumps({
    "keyboard": [[p] for p in TIME_PRESETS] + [[BTN_BACK]],
    "resize_keyboard": True})
# inline-кнопка со ссылкой — прикрепляется к сообщению об ошибке
KB_ERROR = json.dumps({
    "inline_keyboard": [[{"text": "🌧 Погода в Минске (сайт)", "url": FORECAST_URL}]]})

# ---------- API телеграма ----------

def tg_send(chat_id: int, text: str, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = reply_markup
    data = urlencode(params).encode()
    req = urllib.request.Request(f"{API}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def tg_get_updates(offset=None):
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset
    url = f"{API}/getUpdates?{urlencode(params)}"
    with urllib.request.urlopen(url, timeout=40) as resp:
        return json.loads(resp.read())

# ---------- хранилище ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def get_settings(chat_id):
    """Настройки подписчика: часы рассылки и тихий режим."""
    return load_json(SETTINGS_FILE, {}).get(str(chat_id), {
        "morning": DEFAULT_MORNING, "evening": DEFAULT_EVENING, "quiet": False})


def set_settings(chat_id, st):
    settings = load_json(SETTINGS_FILE, {})
    settings[str(chat_id)] = st
    save_json(SETTINGS_FILE, settings)

# ---------- погода (Open-Meteo, без ключа) ----------

def fetch_weather():
    """
    Один запрос: текущая погода + прогноз по часам на 2 дня.
    Лимит ~10 000 запросов/день, ключ не нужен.
    """
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
        "latitude": LAT, "longitude": LON,
        "current_weather": "true",
        "hourly": "temperature_2m,weathercode,precipitation_probability",
        "forecast_days": 2,
        "timezone": TIMEZONE,   # даты/часы сразу в минском времени
    })
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def decode(code):
    return CODES.get(code, f"погода (код {code})")


def day_summary(data, day_index):
    """Сводка по дню: каждая часть суток — средняя температура и тип погоды."""
    daily = data["hourly"]
    date = daily["time"][day_index * 24][:10]      # YYYY-MM-DD
    lines = []
    for part, (h_from, h_to) in PARTS.items():
        temps, votes = [], {}
        for h in range(h_from, h_to):
            i = day_index * 24 + h
            temps.append(daily["temperature_2m"][i])
            c = daily["weathercode"][i]
            votes[c] = votes.get(c, 0) + 1
        main_code = max(votes, key=votes.get)       # самый частый код за часть суток
        t = round(sum(temps) / len(temps))
        lines.append(f"{PART_NAMES[part].capitalize()}: {t}°C, {decode(main_code)}")
    return date, lines


def tomorrow_is_rainy(data):
    """Будут ли завтра осадки: код погоды или вероятность осадков >= 50%."""
    daily = data["hourly"]
    probs = daily["precipitation_probability"][24:48]
    return (any(c in RAINY_CODES or c in SNOWY_CODES
                for c in daily["weathercode"][24:48])
            or (probs and max(probs) >= 50))

# ---------- форматирование ----------

def format_today(data):
    now = data["current_weather"]
    date, lines = day_summary(data, 0)
    text = (f"☀️ Доброе утро! Погода в Минске сегодня, {date}:\n\n"
            f"Сейчас: {round(now['temperature'])}°C, "
            f"{decode(now['weathercode'])}, ветер {now['windspeed']} км/ч\n")
    return text + "\n".join(lines)


def format_tomorrow(data):
    date, lines = day_summary(data, 1)
    return f"🌙 Прогноз на завтра, {date}:\n\n" + "\n".join(lines)

# ---------- рассылка ----------

def send_to(chat_id, text, markup=None):
    try:
        tg_send(chat_id, text, markup)
    except Exception:
        logging.exception("Не удалось отправить сообщение %s", chat_id)


def scheduler_loop():
    """Раз в минуту: если кому-то пора — делаем один запрос и рассылаем."""
    sent = {}  # chat_id -> (дата утреннего письма, дата вечернего)
    while True:
        now = datetime.now()  # даты в ответе API уже в минском времени
        local_hour = now.hour
        today = now.date().isoformat()

        due = []
        for chat_id in load_json(SUBSCRIBERS_FILE, []):
            st = get_settings(chat_id)
            m_date, e_date = sent.get(chat_id, (None, None))
            morning_due = local_hour == st["morning"] and m_date != today
            evening_due = local_hour == st["evening"] and e_date != today
            if morning_due or evening_due:
                due.append((chat_id, st, morning_due, evening_due))

        if due:
            try:
                data = fetch_weather()            # один запрос на всех
                weather_ok = True
            except Exception:
                logging.exception("Open-Meteo недоступен")
                data = None
                weather_ok = False

            for chat_id, st, morning_due, evening_due in due:
                m_date, e_date = sent.get(chat_id, (None, None))
                if morning_due:
                    send_to(chat_id, format_today(data) if weather_ok
                            else ERROR_TEXT, None if weather_ok else KB_ERROR)
                    m_date = today
                if evening_due:
                    if weather_ok:
                        if not st["quiet"] or tomorrow_is_rainy(data):
                            send_to(chat_id, format_tomorrow(data))
                            e_date = today
                    else:
                        send_to(chat_id, ERROR_TEXT, KB_ERROR)
                        e_date = today
                sent[chat_id] = (m_date, e_date)

        time.sleep(60)

# ---------- экран настроек ----------

def settings_text(st):
    quiet = ("включён — вечером пишу только про дождь/снег" if st["quiet"]
             else "выключен — пишу каждый вечер")
    return (f"⚙️ Настройки:\n\n"
            f"🕐 Письма приходят в {st['morning']}:00 и {st['evening']}:00 "
            f"(время минское)\n"
            f"🔕 Тихий режим: {quiet}\n\n"
            f"«Изменить время» — другие часы рассылки\n"
            f"«Тихий режим» — включить или выключить")

# ---------- основной цикл ----------

def main():
    threading.Thread(target=scheduler_loop, daemon=True).start()
    offset = None
    while True:
        try:
            updates = tg_get_updates(offset)
        except Exception:
            logging.exception("Ошибка getUpdates, повтор через 5 сек")
            time.sleep(5)
            continue
        for upd in updates.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg or "text" not in msg:
                continue
            chat_id = msg["chat"]["id"]
            text = msg["text"].strip()
            subs = set(load_json(SUBSCRIBERS_FILE, []))
            st = get_settings(chat_id)
            try:
                if text == BTN_WEATHER or text == "/weather":
                    try:
                        tg_send(chat_id, format_today(fetch_weather()))
                    except Exception:
                        logging.exception("Open-Meteo недоступен")
                        tg_send(chat_id, ERROR_TEXT, KB_ERROR)

                elif text == BTN_SETTINGS or text == "/settings":
                    tg_send(chat_id, settings_text(st), KB_SETTINGS)

                elif text == BTN_SUBSCRIBE or text == "/start":
                    if chat_id in subs:
                        tg_send(chat_id, "Вы уже подписаны 🙂", KB_MAIN)
                    else:
                        subs.add(chat_id)
                        save_json(SUBSCRIBERS_FILE, list(subs))
                        tg_send(chat_id, START_TEXT, KB_MAIN)

                elif text == BTN_UNSUBSCRIBE or text == "/unsubscribe":
                    subs.discard(chat_id)
                    save_json(SUBSCRIBERS_FILE, list(subs))
                    tg_send(chat_id, "Вы отписаны 😢", KB_UNSUBBED)

                elif text == BTN_BACK:
                    tg_send(chat_id, "Главное меню:", KB_MAIN)

                elif text == BTN_TIME:
                    tg_send(chat_id,
                            "Выберите время рассылки (утро / вечер, по Минску):",
                            KB_TIMES)

                elif text == BTN_QUIET:
                    st["quiet"] = not st["quiet"]
                    set_settings(chat_id, st)
                    tg_send(chat_id, settings_text(st), KB_SETTINGS)

                elif text in TIME_PRESETS:  # кнопка вида "7 / 21"
                    m, e = (int(x) for x in text.split(" / "))
                    st["morning"], st["evening"] = m, e
                    set_settings(chat_id, st)
                    tg_send(chat_id, f"Готово ✅ Рассылка: {m}:00 и {e}:00",
                            KB_SETTINGS)

            except Exception:
                logging.exception("Ошибка обработки сообщения")


if __name__ == "__main__":
    main()
