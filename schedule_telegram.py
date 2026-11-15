#!/usr/bin/env python3
"""
Telegram-бот расписания группы с https://timetable.pbk.kz

Режимы запуска (первый аргумент):
  notify   (по умолчанию) основная ежедневная отправка расписания на завтра.
           В пятницу вместо «завтра» проверяется ПОНЕДЕЛЬНИК.
  recheck  тихая перепроверка понедельника в пт/сб/вс: отправляет только
           если расписание опубликовано и ещё не отправлялось (или изменилось).
  poll     обработка нажатий кнопок и сообщений (один проход, либо
           POLL_SECONDS секунд long-polling).
  serve    то же, но бесконечно (для запуска на своём сервере/VPS).

Переменные окружения:
  TG_TOKEN       токен бота от @BotFather
  TG_CHAT_ID     ваш chat id (бот отвечает только вам)
  GROUP          (необязательно) по умолчанию ПО-265
  FORCE          "true" -> отправить, даже если такое расписание уже уходило
  POLL_SECONDS   (необязательно) длительность long-polling в режиме poll
"""

import hashlib
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = "https://timetable.pbk.kz/api/schedule"
GROUP = os.environ.get("GROUP", "ПО-265")
TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
TZ = ZoneInfo("Asia/Almaty")
STATE_FILE = Path(__file__).with_name("state.json")
FORCE = os.environ.get("FORCE", "").lower() == "true"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "0") or 0)

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье"]
SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт"]


# ───────────────────────── сайт расписания ─────────────────────────

def fetch(day: str) -> dict | None:
    """Расписание на дату. Если сайт ответил 404 (не опубликовано) -> None."""
    r = requests.get(API_URL, params={"data": GROUP, "date": day}, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def fetch_times(day: str) -> dict[int, str]:
    """Время пар на дату: {номер: '12:55–14:25'}. При ошибке вернёт пустой словарь."""
    try:
        r = requests.get("https://timetable.pbk.kz/api/lesson-times",
                         params={"date": day}, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return {t["number"]: f"{t['startsAt']}–{t['endsAt']}"
                for t in r.json().get("times", [])}
    except (requests.RequestException, KeyError, ValueError):
        return {}


def build_message(data: dict, tomorrow: str, times: dict[int, str],
                  updated: bool = False) -> str:
    esc = html.escape
    day = datetime.strptime(data["date"], "%Y-%m-%d")
    lines = []

    if updated:
        lines.append("🔄 <b>Расписание обновлено</b>\n")

    if data["date"] != tomorrow:
        lines.append(f"⚠️ Расписание на {tomorrow} ещё не опубликовано. "
                     f"На сайте сейчас данные на {data['date']}.\n")

    lines.append(f"📅 <b>{day.strftime('%d.%m.%Y')}, {WEEKDAYS[day.weekday()]}</b>")
    lines.append(f"Группа {esc(data['group'])} · неделя: {esc(data.get('variant', '—'))}\n")

    lessons = sorted(data.get("lessons", []), key=lambda x: x["number"])
    if not lessons:
        lines.append("Пар нет 🎉")
    for les in lessons:
        time_ = times.get(les["number"])
        head = f"<b>{les['number']} пара</b>" + (f" ({time_})" if time_ else "")
        if les.get("changed"):
            head += " ❗ЗАМЕНА"
        lines.append(head)
        lines.append(f"📚 {esc(les['subject'])}")
        lines.append(f"👤 {esc(les['teacher'])}")
        lines.append(f"🚪 {esc(les['room'])}\n")

    return "\n".join(lines)


# ───────────────────────── Telegram ─────────────────────────

def tg(method: str, timeout: int = 20, **payload) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                      json=payload, timeout=timeout)
    if not r.ok:
        print("Telegram ответил:", r.text)
    r.raise_for_status()
    return r.json()


BTN_BELLS = "🔔 Расписание звонков"
BTN_TOMORROW = "📅 Расписание на завтра"


def reply_keyboard() -> dict:
    """Постоянная клавиатура внизу чата (те же две кнопки, что и раньше)."""
    return {"keyboard": [[{"text": BTN_BELLS}], [{"text": BTN_TOMORROW}]],
            "resize_keyboard": True, "is_persistent": True}


def inline_keyboard() -> dict:
    """Выбор дня. Значения разрешаются в момент нажатия,
    поэтому работают в любой день, включая субботу и воскресенье."""
    return {"inline_keyboard": [
        [{"text": "📅 Сегодня", "callback_data": "today"},
         {"text": "📅 Завтра", "callback_data": "tomorrow"}],
        [{"text": n, "callback_data": f"wd:{i}"} for i, n in enumerate(SHORT)],
    ]}


def send(text: str, markup: bool = True, inline: bool = False) -> None:
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    # Клавиатуру прикрепляем только если включено KEYBOARD=true (режим poll),
    # чтобы не трогать кнопки, настроенные вами в другом месте.
    if inline:
        payload["reply_markup"] = inline_keyboard()
    elif markup and os.environ.get("KEYBOARD", "").lower() == "true":
        payload["reply_markup"] = reply_keyboard()
    tg("sendMessage", **payload)


# ───────────────────────── состояние (антидубли) ─────────────────────────

def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text("utf-8"))
        st.setdefault("sent", {})
        return st
    except (OSError, ValueError):
        return {"sent": {}}


def save_state(state: dict) -> None:
    sent = state.setdefault("sent", {})
    for k in sorted(sent)[:-14]:          # храним последние 14 дат
        del sent[k]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2,
                                     sort_keys=True), "utf-8")


def digest(data: dict) -> str:
    raw = json.dumps(data.get("lessons", []), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def deliver(data: dict, target: str, force: bool = False) -> bool:
    """Отправляет расписание, если такое (по содержимому) ещё не отправлялось.
    Если расписание на эту дату уже уходило, но изменилось — шлёт с пометкой."""
    state = load_state()
    key, h = data["date"], digest(data)
    prev = state["sent"].get(key)
    if prev == h and not force:
        print(f"Расписание на {key} уже отправлено — пропуск.")
        return False
    send(build_message(data, target, fetch_times(key),
                       updated=prev is not None and prev != h))
    state["sent"][key] = h
    save_state(state)
    return True


# ───────────────────────── ежедневная отправка ─────────────────────────

def notify(now: datetime | None = None, force: bool = False) -> None:
    now = now or datetime.now(TZ)
    friday = now.weekday() == 4
    # В пятницу смотрим сразу понедельник, в остальные дни — завтра
    target_dt = now + timedelta(days=3 if friday else 1)
    target = target_dt.strftime("%Y-%m-%d")
    day_name = WEEKDAYS[target_dt.weekday()]
    date_str = target_dt.strftime("%d.%m.%Y")
    is_weekend = target_dt.weekday() >= 5

    data = fetch(target)
    published = data is not None and data.get("date") == target

    # Завтра выходной и пар на сайте нет
    if is_weekend and not (published and data.get("lessons")):
        send(f"🏖 Завтра {date_str} ({day_name}) выходной, пар нет.")
        return

    # Расписание ещё не выложили (в пятницу — строго на понедельник)
    if data is None or (friday and not published):
        if friday:
            send(f"⏳ Расписание на {date_str} ({day_name}) пока не опубликовано. "
                 f"Проверю ещё раз вечером и в выходные.")
        else:
            send(f"⏳ Расписание на {date_str} ({day_name}) пока не опубликовано.")
        return

    if published:
        deliver(data, target, force)
    else:  # как раньше: предупреждение + то, что сейчас на сайте
        send(build_message(data, target, fetch_times(data["date"])))


def recheck(now: datetime | None = None, force: bool = False) -> None:
    """Тихая проверка понедельника в пт/сб/вс. Молчит, если нечего слать."""
    now = now or datetime.now(TZ)
    if now.weekday() not in (4, 5, 6):
        return
    monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    target = monday.strftime("%Y-%m-%d")
    data = fetch(target)
    if data is None or data.get("date") != target:
        print(f"Расписание на {target} пока не опубликовано.")
        return
    deliver(data, target, force)


# ───────────────────────── кнопки / команды ─────────────────────────

def resolve(token: str, now: datetime) -> datetime | None:
    if token == "today":
        return now
    if token == "tomorrow":
        return now + timedelta(days=1)
    if token.startswith("wd:"):                       # ближайший Пн…Пт (включая сегодня)
        wd = int(token[3:])
        return now + timedelta(days=(wd - now.weekday()) % 7)
    return None


def schedule_text(dt: datetime) -> str:
    """Расписание на запрошенную дату — работает в любой день недели."""
    day = dt.strftime("%Y-%m-%d")
    date_str = dt.strftime("%d.%m.%Y")
    name = WEEKDAYS[dt.weekday()]
    data = fetch(day)
    published = data is not None and data.get("date") == day
    has_lessons = published and bool(data.get("lessons"))
    if dt.weekday() >= 5 and not has_lessons:
        return f"🏖 {date_str} ({name}) — выходной, пар нет."
    if not published:
        return f"⏳ Расписание на {date_str} ({name}) пока не опубликовано."
    return build_message(data, day, fetch_times(day))


def handle_update(u: dict) -> None:
    now = datetime.now(TZ)
    cb = u.get("callback_query")
    if cb:
        chat = (cb.get("message") or {}).get("chat", {}).get("id", cb["from"]["id"])
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        if str(chat) != str(CHAT_ID):
            return
        dt = resolve(cb.get("data", ""), now)
        if dt:
            send(schedule_text(dt))
        return

    msg = u.get("message")
    if not msg or str(msg["chat"]["id"]) != str(CHAT_ID):
        return
    text = (msg.get("text") or "").strip().lower()
    cmd = text.split()[0].split("@")[0] if text else ""
    if "звонков" in text or cmd == "/bells":
        send(bells_text(now))
    elif "на завтра" in text or cmd in ("/tomorrow", "завтра"):
        send(tomorrow_text(now))
    elif cmd in ("/today", "сегодня"):
        send(schedule_text(now))
    else:
        send("Выберите день:", inline=True)


def tomorrow_text(now: datetime) -> str:
    """Кнопка «на завтра». Если завтра выходной — сообщаем об этом и
    сразу показываем ближайший понедельник, чтобы кнопка была полезна в пт/сб/вс."""
    dt = now + timedelta(days=1)
    text = schedule_text(dt)
    if dt.weekday() >= 5 and text.startswith("🏖"):
        monday = dt + timedelta(days=7 - dt.weekday())
        text += "\n\n" + schedule_text(monday)
    return text


def bells_text(now: datetime) -> str:
    """Расписание звонков; в выходные берём ближайший учебный день."""
    dt = now
    while dt.weekday() >= 5:
        dt += timedelta(days=1)
    times = fetch_times(dt.strftime("%Y-%m-%d"))
    if not times:
        return "🔔 Не удалось получить расписание звонков, попробуйте позже."
    rows = "\n".join(f"<b>{n} пара</b> — {t}" for n, t in sorted(times.items()))
    return f"🔔 <b>Расписание звонков</b>\n\n{rows}"


def poll(duration: int = 0) -> None:
    """duration=0 — один проход; иначе long-polling указанное число секунд."""
    offset = None
    deadline = time.time() + duration
    while True:
        wait = 0 if duration == 0 else int(min(50, max(1, deadline - time.time())))
        params = {"timeout": wait, "allowed_updates": ["message", "callback_query"]}
        if offset:
            params["offset"] = offset
        updates = tg("getUpdates", timeout=wait + 20, **params)["result"]
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:  # одна ошибка не должна ронять остальные
                print("Ошибка обработки:", repr(e))
        if time.time() >= deadline:
            break
    if offset:  # подтверждаем обработанные обновления
        tg("getUpdates", offset=offset, limit=1, timeout=0)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "notify"
    if mode == "notify":
        notify(force=FORCE)
    elif mode == "recheck":
        recheck(force=FORCE)
    elif mode == "poll":
        poll(POLL_SECONDS)
    elif mode == "serve":
        while True:
            try:
                poll(55)
            except requests.RequestException as e:
                print("Сеть:", repr(e))
                time.sleep(5)
    else:
        sys.exit(f"Неизвестный режим: {mode}")


if __name__ == "__main__":
    main()
    main()
