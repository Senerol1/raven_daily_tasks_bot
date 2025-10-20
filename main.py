# main.py
import os
import json
import asyncio
import logging
from datetime import datetime, time as dt_time
from typing import List, Optional

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    Defaults,       # <-- здесь зададим tzinfo
)

# ----------------------------
# Конфиг/хранилище
# ----------------------------
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

DEFAULT_CFG = {
    "tasks": [],                 # список задач (строки)
    "target_chat_id": None,      # куда слать ежедневно (chat_id)
    "target_thread_id": None,    # в какую ветку (message_thread_id)
    "send_time": "09:00",        # время ежедневной отправки (HH:MM) в заданной tz
    "post_on_start": False       # отправлять ли один раз при старте
}

def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return DEFAULT_CFG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    merged = DEFAULT_CFG.copy()
    merged.update(cfg or {})
    return merged

def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def get_tzinfo():
    tz = os.getenv("TIMEZONE", "Europe/Moscow")
    try:
        return pytz.timezone(tz)
    except Exception:
        return pytz.timezone("Europe/Moscow")

# ----------------------------
# Утилиты
# ----------------------------
def format_tasks(tasks: List[str]) -> str:
    if not tasks:
        return "Список задач пуст. Добавьте: /addtask <текст>"
    return "\n".join(f"{i+1}. {t}" for i, t in enumerate(tasks))

async def send_poll_to(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    tasks: List[str],
    thread_id: Optional[int] = None,
    title_prefix: Optional[str] = None
):
    """Отправить опрос (Poll) с чекбоксами."""
    if not tasks:
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text="Список задач пуст. Добавьте: /addtask <текст>"
        )
        return

    today = datetime.now(get_tzinfo()).strftime("%Y-%m-%d")
    question = f"{title_prefix + ' - ' if title_prefix else ''}Задачи на {today}"
    options = tasks[:10]  # максимум 10 вариантов у Poll

    await context.bot.send_poll(
        chat_id=chat_id,
        question=question,
        options=options,
        allows_multiple_answers=True,
        is_anonymous=False,
        message_thread_id=thread_id
    )

async def send_tasks(context: ContextTypes.DEFAULT_TYPE, force: bool = False):
    """Отправка опроса в привязанный чат/ветку по расписанию или по /postnow."""
    cfg = load_config()
    chat_id = cfg.get("target_chat_id")
    thread_id = cfg.get("target_thread_id")
    tasks = cfg.get("tasks", [])

    if not chat_id:
        print("[send_tasks] target_chat_id not set; skip", flush=True)
        return

    try:
        await send_poll_to(
            context=context,
            chat_id=int(chat_id),
            tasks=tasks,
            thread_id=thread_id,
            title_prefix="Ежедневный чек-лист"
        )
    except Exception as e:
        print(f"[send_tasks] error: {e!r}", flush=True)

# ----------------------------
# Команды
# ----------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот ежедневных задач.\n\n"
        "Команды (в личке со мной):\n"
        "/addtask <текст> — добавить задачу\n"
        "/deltask <номер> — удалить задачу по номеру\n"
        "/cleartasks — очистить все задачи\n"
        "/listtasks — показать список задач\n"
        "/tasks — то же что /listtasks\n"
        "/preview — показать превью опроса здесь\n\n"
        "Команды для группы/ветки:\n"
        "/bind — привязать этот чат/ветку как место ежедневной отправки\n"
        "/settime HH:MM — настроить ежедневное время (по TIMEZONE)\n"
        "/postnow — отправить опрос сейчас в привязанную ветку\n"
        "/whereami — показать chat_id и thread_id текущего чата"
    )
    await update.effective_chat.send_message(text)

async def whereami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    tid = update.effective_message.message_thread_id if update.effective_message else None
    await update.effective_chat.send_message(f"chat_id = {cid}\nthread_id = {tid}")

async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    cid = update.effective_chat.id
    tid = update.effective_message.message_thread_id if update.effective_message else None

    cfg["target_chat_id"] = cid
    cfg["target_thread_id"] = tid
    save_config(cfg)

    reschedule_jobs(context.application)
    await update.effective_chat.send_message(f"Привязано!\nchat_id = {cid}\nthread_id = {tid}")

async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_chat.send_message("Укажи время в формате HH:MM, например: /settime 09:00")
        return

    val = context.args[0].strip()
    try:
        hh, mm = map(int, val.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await update.effective_chat.send_message("Неверный формат. Пример: /settime 09:00")
        return

    cfg = load_config()
    cfg["send_time"] = f"{hh:02d}:{mm:02d}"
    save_config(cfg)

    reschedule_jobs(context.application)
    await update.effective_chat.send_message(f"Ок! Теперь отправляю ежедневно в {cfg['send_time']}.")

async def addtask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_chat.send_message("Использование: /addtask <текст задачи>")
        return
    cfg = load_config()
    tasks = cfg.get("tasks", [])
    tasks.append(text)
    cfg["tasks"] = tasks
    save_config(cfg)
    await update.effective_chat.send_message(f"Добавил задачу:\n• {text}")

async def deltask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_chat.send_message("Использование: /deltask <номер>")
        return
    try:
        idx = int(context.args[0]) - 1
    except Exception:
        await update.effective_chat.send_message("Номер должен быть числом. Пример: /deltask 2")
        return
    cfg = load_config()
    tasks = cfg.get("tasks", [])
    if not (0 <= idx < len(tasks)):
        await update.effective_chat.send_message("Нет такой задачи. Посмотри /listtasks")
        return
    removed = tasks.pop(idx)
    cfg["tasks"] = tasks
    save_config(cfg)
    await update.effective_chat.send_message(f"Удалил:\n• {removed}")

async def cleartasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    cfg["tasks"] = []
    save_config(cfg)
    await update.effective_chat.send_message("Список задач очищен.")

async def listtasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cfg = load_config()
        tasks = cfg.get("tasks", [])
        if not tasks:
            await update.effective_chat.send_message("Список задач пуст. Добавьте: /addtask <текст>")
            return
        await update.effective_chat.send_message("Текущие задачи:\n" + format_tasks(tasks))
    except Exception as e:
        await update.effective_chat.send_message(f"Ошибка /listtasks: {e!r}")
        print(f"/listtasks error: {e!r}", flush=True)

async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    tasks = cfg.get("tasks", [])
    await send_poll_to(
        context=context,
        chat_id=update.effective_chat.id,
        tasks=tasks,
        thread_id=(update.effective_message.message_thread_id if update.effective_message else None),
        title_prefix="ПРЕВЬЮ"
    )

async def postnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_tasks(context, force=True)
    await update.effective_chat.send_message("Отправил (или попытался отправить) ежедневный опрос в привязанную ветку.")

# ----------------------------
# Планирование
# ----------------------------
def reschedule_jobs(app: Application, *, first_run: bool = False):
    """
    Перепланировать ежедневную отправку.
    Таймзона берётся из Application.defaults.tzinfo.
    """
    cfg = load_config()

    # Удалим старую задачу, если есть
    try:
        for job in list(app.job_queue.jobs()):
            if job.name == "daily_tasks":
                job.schedule_removal()
    except Exception as e:
        print(f"[reschedule_jobs] cleanup jobs error: {e!r}", flush=True)

    chat_id = cfg.get("target_chat_id")
    send_time_str = cfg.get("send_time")
    if not chat_id or not send_time_str:
        print("[reschedule_jobs] no chat_id or no send_time -> skip scheduling", flush=True)
        return

    hh, mm = map(int, send_time_str.split(":"))
    # ВАЖНО: time без tzinfo — JobQueue применит tz из Defaults(tzinfo=...)
    send_time = dt_time(hour=hh, minute=mm)

    async def _job_callback(context: ContextTypes.DEFAULT_TYPE):
        await send_tasks(context, force=True)

    app.job_queue.run_daily(
        _job_callback,
        time=send_time,
        days=(0, 1, 2, 3, 4, 5, 6),
        name="daily_tasks",
        job_kwargs={"misfire_grace_time": 3600},
    )

    if first_run and cfg.get("post_on_start"):
        async def _once(context: ContextTypes.DEFAULT_TYPE):
            try:
                await send_tasks(context, force=True)
            except Exception as e:
                print(f"[reschedule_jobs] first_run send error: {e!r}", flush=True)
        app.job_queue.run_once(_once, when=1, name="daily_tasks_first_run")

    print("[reschedule_jobs] scheduled daily_tasks at", send_time_str,
          "for chat", chat_id, "thread", cfg.get("target_thread_id"), flush=True)

# ----------------------------
# Ошибки
# ----------------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Exception while handling an update:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message(f"Ошибка: {context.error!r}")
    except Exception:
        pass

# ----------------------------
# MAIN
# ----------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не установлена")

    tzinfo = get_tzinfo()
    defaults = Defaults(tzinfo=tzinfo)  # <-- так задаём таймзону для JobQueue

    application: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .concurrent_updates(True)
        .build()
    )

    # Команды
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("whereami", whereami_cmd))
    application.add_handler(CommandHandler("bind", bind_cmd))
    application.add_handler(CommandHandler("settime", settime_cmd))
    application.add_handler(CommandHandler("postnow", postnow_cmd))
    application.add_handler(CommandHandler("preview", preview_cmd))
    application.add_handler(CommandHandler("addtask", addtask_cmd))
    application.add_handler(CommandHandler("deltask", deltask_cmd))
    application.add_handler(CommandHandler("cleartasks", cleartasks_cmd))
    application.add_handler(CommandHandler("listtasks", listtasks_cmd))
    application.add_handler(CommandHandler("tasks", listtasks_cmd))  # алиас
    application.add_error_handler(error_handler)

    # Планирование
    reschedule_jobs(application, first_run=False)

    print("Starting polling ...", flush=True)
    await application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        stop_signals=None,
    )

if __name__ == "__main__":
    asyncio.run(main())
