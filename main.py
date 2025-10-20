import os
import json
import asyncio
from datetime import time as dtime
from typing import List, Tuple

import pytz
from tzlocal import get_localzone
from aiohttp import web

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

DATA_FILE = "data.json"


# ---------- Хранилище ----------
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


# ---------- Утилиты ----------
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
    return "\n".join(text)


async def send_tasks_message(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = data.get("chat_id")
    thread_id = data.get("thread_id")
    if not chat_id:
        return
    text = render_tasks(data.get("tasks", []))
    await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


# ---------- Команды ----------
async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    await update.message.reply_text(f"chat_id = {chat_id}\nthread_id = {thread_id}")


async def cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    data["chat_id"] = update.effective_chat.id
    data["thread_id"] = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    save_data(data)
    await update.message.reply_text("Привязано! Ежедневная рассылка будет сюда.")


async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Использование: /addtask <текст>")
        return
    data = load_data()
    data["tasks"].append(parts[1].strip())
    save_data(data)
    await update.message.reply_text(f"Добавил задачу: {parts[1].strip()}")


async def cmd_listtasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    await update.message.reply_text(render_tasks(data["tasks"]), parse_mode=ParseMode.MARKDOWN)


async def cmd_postnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_tasks_message(context)
    await update.message.reply_text("Отправил текущий список задач.")


# ---------- Планировщик ----------
def schedule_daily_job(app, tzinfo):
    data = load_data()
    H, M = parse_send_time(data.get("send_time", os.getenv("SEND_TIME", "09:00")))
    # чистим старую джобу, если была
    for j in app.job_queue.jobs():
        if j.name == "daily_tasks":
            j.schedule_removal()
    app.job_queue.run_daily(send_tasks_message, time=dtime(hour=H, minute=M, tzinfo=tzinfo), name="daily_tasks")


# ---------- HTTP ----------
async def health(_request: web.Request):
    return web.Response(text="OK")

async def handle_update(request: web.Request):
    app = request.app["telegram_app"]
    try:
        payload = await request.json()
        update = Update.de_json(payload, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")
    except Exception as e:
        print("❌ Ошибка обработки вебхука:", repr(e))
        return web.Response(status=500, text="error")


# ---------- main ----------
async def main():
    token = os.getenv("TELEGRAM_TOKEN")
    base_url = os.getenv("BASE_URL")
    if not token or not base_url:
        raise RuntimeError("Нужны TELEGRAM_TOKEN и BASE_URL")

    tzinfo = pytz.timezone(os.getenv("TZ", str(get_localzone())))

    # Собираем приложение и регистрируем хендлеры
    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("whereami", cmd_whereami))
    application.add_handler(CommandHandler("bind", cmd_bind))
    application.add_handler(CommandHandler("addtask", cmd_addtask))
    application.add_handler(CommandHandler("listtasks", cmd_listtasks))
    application.add_handler(CommandHandler("postnow", cmd_postnow))

    # Планировщик
    schedule_daily_job(application, tzinfo)

    # ВАЖНО: инициализируем и запускаем PTB-приложение,
    # иначе process_update не будет реально обрабатывать апдейты.
    await application.initialize()
    await application.start()

    # Вебхук в Telegram
    webhook_url = f"{base_url.rstrip('/')}/{token}"
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=webhook_url, drop_pending_updates=True)

    # aiohttp-сервер
    web_app = web.Application()
    web_app["telegram_app"] = application
    web_app.add_routes([web.get("/", health), web.post(f"/{token}", handle_update)])

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()

    print(f"✅ Webhook сервер запущен и PTB запущен. URL: {webhook_url}")

    # держим процесс живым
    try:
        await asyncio.Event().wait()
    finally:
        # корректное выключение (на будущее)
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
