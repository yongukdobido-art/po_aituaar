#!/usr/bin/env python3
"""
Отправляет в Telegram расписание группы с https://timetable.pbk.kz
на следующий день.

Переменные окружения:
  TG_TOKEN    токен бота от @BotFather
  TG_CHAT_ID  ваш chat id
  GROUP       (необязательно) по умолчанию ПО-265
"""

import html
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

API_URL = "https://timetable.pbk.kz/api/schedule"
GROUP = os.environ.get("GROUP", "ПО-265")
TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
TZ = ZoneInfo("Asia/Almaty")

# Время звонков (по желанию), например: {1: "08:30–10:00"}
BELLS: dict[int, str] = {}

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье"]


def fetch(day: str) -> dict:
    r = requests.get(API_URL, params={"data": GROUP, "date": day}, timeout=20,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.json()


def build_message(data: dict, tomorrow: str) -> str:
    esc = html.escape
    day = datetime.strptime(data["date"], "%Y-%m-%d")
    lines = []

    if data["date"] != tomorrow:
        lines.append(f"⚠️ Расписание на {tomorrow} ещё не опубликовано. "
                     f"На сайте сейчас данные на {data['date']}.\n")

    lines.append(f"📅 <b>{day.strftime('%d.%m.%Y')}, {WEEKDAYS[day.weekday()]}</b>")
    lines.append(f"Группа {esc(data['group'])} · неделя: {esc(data.get('variant', '—'))}\n")

    lessons = sorted(data.get("lessons", []), key=lambda x: x["number"])
    if not lessons:
        lines.append("Пар нет 🎉")
    for les in lessons:
        time = BELLS.get(les["number"])
        head = f"<b>{les['number']} пара</b>" + (f" ({time})" if time else "")
        if les.get("changed"):
            head += " ❗ЗАМЕНА"
        lines.append(head)
        lines.append(f"📚 {esc(les['subject'])}")
        lines.append(f"👤 {esc(les['teacher'])}")
        lines.append(f"🚪 {esc(les['room'])}\n")

    return "\n".join(lines)


def send(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if not r.ok:
        print("Telegram ответил:", r.text)
    r.raise_for_status()


def main() -> None:
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
    send(build_message(fetch(tomorrow), tomorrow))


if __name__ == "__main__":
    main()
