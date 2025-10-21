# main.py
import os
import json
import asyncio
from datetime import datetime, time as dtime
from typing import Dict, Any, List, Optional

from aiohttp import web
from pytz import timezone
from tzlocal import get_localzone

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, ContextTypes
)

STATE_PATH = "state.json"

# ---------- Хранилище ----------
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                pass
    return {
        "chat_id": None,
        "thread_id": None,
        "send_time": "09:00",    # HH:MM локальное время сервера
        "tasks": []              # список строк
    }

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

STATE = load_state()

def tzinfo():
    try:
        return get_localzone()
    except Exception:
        return timezone("UTC")

# ---------- Утилиты ----------
def parse_time_str(hhmm: str) -> Optional[dtime]:
    try:
        hh, mm = hhmm.strip().split(":")
        return dtime(hour=int(hh), minute=int(mm), second=0)
    except Exception:
        return None

def chunk(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]

# ---------- Отправка задач как POLL ----------
async def send_tasks_poll(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int, thread_id: Optional[int]) -> None:
    tasks: List[str] = STATE.get("tasks", [])
    if not tasks:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Список задач пуст.",
            message_thread_id=thread_id
        )
        return

    groups = chunk(tasks, 10)  # в одном Poll не более 10 опций
    today = datetime.now(tzinfo()).strftime("%d.%m.%Y")

    for idx, group in enumerate(groups, start=1):
        question = f"Ежедневные задачи — {today}"
        if len(groups) > 1:
            question += f" ({idx}/{len(groups)})"

        await context.bot.send_poll(
            chat_id=chat_id,
            question=question,
            options=group,
            is_anonymous=False,
            allows_multiple_answers=True,
            message_thread_id=thread_id
        )

# ---------- Планировщик ----------
def reschedule_daily_job(app: Application) -> None:
    try:
        app.job_queue.scheduler.remove_all_jobs()
    except Exception:
        pass

    send_time = parse_time_str(str(STATE.get("send_time", "09:00")))
    c_id = STATE.get("chat_id")
    if not (send_time and c_id):
        print("[reschedule] пропуск: нет chat_id или времени")
        return

    th_id = STATE.get("thread_id")
    app.job_queue.run_daily(
        callback=lambda ctx: send_tasks_poll(ctx, chat_id=c_id, thread_id=th_id),
        time=send_time,
        name="daily_tasks_poll",
        timezone=tzinfo()
    )
    print(f"[reschedule] ежедневная отправка в {STATE['send_time']} (TZ={tzinfo()})")

# ---------- Команды ----------
async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    STATE["chat_id"] = chat.id
    STATE["thread_id"] = msg.message_thread_id if getattr(msg, "is_topic_message", False) else None
    save_state(STATE)

    reschedule_daily_job(context.application)

    await msg.reply_text(
        f"Привязано!\nchat_id = {STATE['chat_id']}\nthread_id = {STATE['thread_id']}"
    )

async def whereami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    thread_id = msg.message_thread_id if getattr(msg, "is_topic_message", False) else None
    await msg.reply_text(f"chat_id = {update.effective_chat.id}\nthread_id = {thread_id}")

async def addtask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("Использование: /addtask Текст задачи")
        return
    STATE.setdefault("tasks", []).append(text)
    save_state(STATE)
    await update.effective_message.reply_text(f"Добавил: «{text}». Всего задач: {len(STATE['tasks'])}")

async def listtasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks: List[str] = STATE.get("tasks", [])
    if not tasks:
        await update.effective_message.reply_text("Список задач пуст.")
        return
    lines = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))
    await update.effective_message.reply_text(lines)

async def deltask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Использование: /deltask Номер")
        return
    try:
        idx = int(context.args[0]) - 1
    except Exception:
        await update.effective_message.reply_text("Номер должен быть числом.")
        return

    tasks: List[str] = STATE.get("tasks", [])
    if 0 <= idx < len(tasks):
        removed = tasks.pop(idx)
        save_state(STATE)
        await update.effective_message.reply_text(f"Удалил: «{removed}». Осталось: {len(tasks)}")
    else:
        await update.effective_message.reply_text("Нет задачи с таким номером.")

async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Использование: /settime HH:MM (локальное время сервера)")
        return
    t = parse_time_str(context.args[0])
    if not t:
        await update.effective_message.reply_text("Неверный формат. Пример: /settime 10:30")
        return
    STATE["send_time"] = context.args[0]
    save_state(STATE)
    reschedule_daily_job(context.application)
    await update.effective_message.reply_text(f"Ок, буду слать каждый день в {STATE['send_time']}.")

async def postnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    c_id = STATE.get("chat_id")
    th_id = STATE.get("thread_id")
    if not c_id:
        await update.effective_message.reply_text("Сначала привяжи чат командой /bind в нужном чате/топике.")
        return
    await send_tasks_poll(context, chat_id=c_id, thread_id=th_id)
    await update.effective_message.reply_text("Отправил задачи как опрос.")

# ---------- Webhook ----------
async def health(_request):
    return web.Response(text="ok")

def make_aiohttp_app(ptb_app: Application, token: str) -> web.Application:
    aio = web.Application()

    async def handle_update(request: web.Request):
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        # ВАЖНО: к этому моменту application уже initialize+start
        await ptb_app.process_update(update)
        return web.Response(text="ok")

    aio.add_routes([
        web.get("/", health),
        web.post(f"/{token}", handle_update),
    ])
    return aio

# ---------- main ----------
async def main():
    token = os.getenv("TELEGRAM_TOKEN")
    base_url = os.getenv("BASE_URL", "")
    if not token or not base_url:
        raise RuntimeError("Нужны переменные окружения TELEGRAM_TOKEN и BASE_URL")

    base_url = base_url.rstrip("/")  # на всякий случай
    port = int(os.getenv("PORT", "10000"))

    application: Application = ApplicationBuilder().token(token).build()

    # Команды
    application.add_handler(CommandHandler("bind", bind_cmd))
    application.add_handler(CommandHandler("whereami", whereami_cmd))
    application.add_handler(CommandHandler("addtask", addtask_cmd))
    application.add_handler(CommandHandler("listtasks", listtasks_cmd))
    application.add_handler(CommandHandler("deltask", deltask_cmd))
    application.add_handler(CommandHandler("settime", settime_cmd))
    application.add_handler(CommandHandler("postnow", postnow_cmd))

    # Планировщик (расписание привяжется после старта job_queue, но зададим сразу)
    reschedule_daily_job(application)

    # --- Правильный порядок запуска PTB ---
    await application.initialize()
    await application.start()

    # Ставим вебхук после старта
    await application.bot.set_webhook(
        url=f"{base_url}/{token}",
        allowed_updates=Update.ALL_TYPES
    )

    # Поднимаем aiohttp и начинаем принимать апдейты
    web_app = make_aiohttp_app(application, token)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Webhook сервер запущен: {base_url}/{token}")

    # Держим процесс живым
    try:
        await asyncio.Event().wait()
    finally:
        # Аккуратное сворачивание
        try:
            await application.bot.delete_webhook()
        except Exception:
            pass
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
