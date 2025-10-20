import os
import json
import asyncio
from datetime import time as dtime
from typing import List, Tuple, Optional
import pytz
from tzlocal import get_localzone
from aiohttp import web

from telegram import Update, Poll
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

DATA_FILE = "data.json"


# =======================
# Работа с данными
# =======================
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"tasks": [], "chat_id": None, "thread_id": None, "send_time": os.getenv("SEND_TIME", "09:00")}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tasks": [], "chat_id": None, "thread_id": None, "send_time": os.getenv("SEND_TIME", "09:00")}


def save_data(data: dict):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


# =======================
# Вспомогательные функции
# =======================
def parse_send_time(s: str) -> Tuple[int, int]:
    try:
        hh, mm = s.strip().split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except Exception:
        return 9, 0


def render_tasks(tasks: List[str]) -> str:
    if not tasks:
        return "Пока задач нет. Добавь через /addtask <текст>"
    text = ["📝 *Ваш список задач:*"]
    for i, t in enumerate(tasks, 1):
        text.append(f"{i}. {t}")
    return
