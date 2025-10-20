import os
import json
import asyncio
from datetime import time as dtime
from typing import List, Tuple, Optional

from aiohttp import web
from tzlocal import get_localzone
import pytz

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
# Хранилище (файл data.json)
# =======================
def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {
            "tasks": [],
            "chat_id": None,
            "thread_id": None,
            "send_time": os.getenv("SEND_TIME", "09:00"),  # HH:MM
        }
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "tasks": [],
            "chat_id": None,
            "thread_id": None,
            "send_time": os.getenv("SEND_TIME", "09:00"),
        }


def save_data(data: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


# =======================
# Вспомогалки
# =======================
def parse_send_time(s: str) -> Tuple[int, int]:
    """ 'HH:MM' -> (H, M) с валидацией и дефолтами """
    try:
        hh, mm = s.strip().split(":")
        H = max(0, min(23, int(hh)))
        M = max(0, min(59, int(mm)))
        return H, M
    except Exception:
        return 9, 0  # 09:00 по умолчанию


def render_tasks(tasks: List[str]) -> str:
    if not tasks:
        return "Пока задач нет. Добавь через /addtask <текст>"
    lines = [f"📝 *Ваш список задач* ({len(tasks)}):"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t}")
    return "\n".join(lines)


async def send_tasks_message(context: ContextTypes.DEFAULT_TYPE, *, force_chat=None, force_thread=None):
    data = load_data()
    tasks = data.get("tasks", [])
    chat_id = force_chat if force_chat is not None else data.get("chat_id")
    thread_id = force_thread if force_thread is not None else data.get("thread_id")

    if not chat_id:
        return  # некуда слать

    text = render_tasks(tasks)
    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def send_tasks_poll(context: ContextTypes.DEFAULT_TYPE, *, force_chat=None, force_thread=None):
    data = load_data()
    tasks = data.get("tasks", [])
    chat_id = force_chat if force_chat is not None else data.get("chat_id")
    thread_id = force_thread if force_thread is not None else data.get("thread_id")

    if not chat_id:
        return
    if not tasks:
        # если пусто — кинем текст вместо опроса
        await send_tasks_message(context, force_chat=chat_id, force_thread=thread_id)
        return

    # Множественный выбор (checkboxes)
    await context.bot.send_poll(
        chat_id=chat_id,
        message_thread_id=thread_id,
        question="Ежедневный чек-лист",
        options=tasks,
        allows_multiple_answers=True,
        is_anonymous=False,
    )


# =======================
# Команды
# =======================
async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    thread_id = update.message.message_thread_id if (update.message and update.message.is_topic_message) else None
    await update.message.reply_text(f"chat_id = {chat_id}\nthread_id = {thread_id}")


async def cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id if update.effective_chat else None
    thread_id = update.message.message_thread_id if (update.message and update.message.is_topic_message) else None

    data = load_data()
    data["chat_id"] = chat_id
    data["thread_id"] = thread_id
    save_data(data)

    await update.message.reply_text(f"Привязано!\nchat_id = {chat_id}\nthread_id = {thread_id or 'None'}")


async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await update.message.reply_text("Использование: /addtask <текст задачи>")
        return
    task = text[1].strip()

    data = load_data()
    tasks = data.get("tasks", [])
    tasks.append(task)
    data["tasks"] = tasks
    save_data(data)

    await update.message.reply_text(f"Добавил: “{task}”\nВсего задач: {len(tasks)}")


async def cmd_listtasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    await update.message.reply_text(
        render_tasks(data.get("tasks", [])),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def cmd_postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # шлём туда, куда привязано
    await send_tasks_message(context)
    await update.message.reply_text("Отправил текущий список задач.")


async def cmd_pollnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_tasks_poll(context)
    await update.message.reply_text("Опрос отправлен.")


# =======================
# Планировщик: ежедневная отправка
# =======================
def schedule_daily_job(app: Application, tzinfo):
    data = load_data()
    H, M = parse_send_time(data.get("send_time", os.getenv("SEND_TIME", "09:00")))
    # Удалим старые джобы с тем же именем
    for j in app.job_queue.jobs():
        if j.name == "daily_tasks":
            j.schedule_removal()
    # Планируем новую
    app.job_queue.run_daily(
        lambda ctx: send_tasks_message(ctx),
        time=dtime(hour=H, minute=M, tzinfo=tzinfo),
        name="daily_tasks",
    )


# =======================
# Aiohttp health-ручки
# =======================
async def health(request: web.Request):
    return web.Response(text="OK")


# =======================
# main: Webhook
# =======================
async def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    base_url = os.environ.get("BASE_URL")  # например https://raven-daily-tasks-bot.onrender.com
    if not token or not base_url:
        raise RuntimeError("Нужны переменные окружения TELEGRAM_TOKEN и BASE_URL")

    # Таймзона
    tz_name = os.environ.get("TZ")
    if tz_name:
        tzinfo = pytz.timezone(tz_name)
    else:
        try:
            tzinfo = get_localzone()
        except Exception:
            tzinfo = pytz.UTC

    application: Application = (
        ApplicationBuilder()
        .token(token)
        .build()
    )

    # Хэндлеры
    application.add_handler(CommandHandler("whereami", cmd_whereami))
    application.add_handler(CommandHandler("bind", cmd_bind))
    application.add_handler(CommandHandler("addtask", cmd_addtask))
    application.add_handler(CommandHandler("listtasks", cmd_listtasks))
    application.add_handler(CommandHandler("postnow", cmd_postnow))
    application.add_handler(CommandHandler("pollnow", cmd_pollnow))

    # Планировщик
    schedule_daily_job(application, tzinfo)

    # Подготовим webhook сервер
    port = int(os.environ.get("PORT", "10000"))
    url_path = token  # секретный путь
    webhook_url = f"{base_url.rstrip('/')}/{url_path}"

    # Aiohttp app с healthcheck
    web_app = web.Application()
    web_app.add_routes(
        [
            web.get("/", health),
            web.get("/healthz", health),
        ]
    )

    # Сбрасываем старый вебхук и ставим новый
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)

    # Запуск вебхука (PTB поднимет сервер, а наш web_app добавит / и /healthz)
    await application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=url_path,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        web_app=web_app,
    )


if __name__ == "__main__":
    asyncio.run(main())
