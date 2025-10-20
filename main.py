import asyncio
import json
import os
from datetime import datetime, time
from dateutil import tz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes
)

CONFIG_PATH = "config.json"
DEFAULT_TEMPLATE = (
    "Список задач на {date} ({weekday}):\n"
    "— [ ] Приоритет дня\n"
    "— [ ] 2\n"
    "— [ ] 3\n"
)

# Часовой пояс бота по умолчанию (можно сменить командой)
DEFAULT_TZ = os.getenv("BOT_TZ", "Asia/Nicosia")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {
            "chat_id": None,
            "thread_id": None,
            "time": "09:00",  # HH:MM
            "tz": DEFAULT_TZ,
            "template": DEFAULT_TEMPLATE,
            "owner_id": None,
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


async def send_tasks(context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    chat_id = cfg.get("chat_id")
    thread_id = cfg.get("thread_id")
    template = cfg.get("template", DEFAULT_TEMPLATE)
    tzname = cfg.get("tz", DEFAULT_TZ)
    tzinfo = tz.gettz(tzname)

    now = datetime.now(tzinfo)
    payload = template.format(
        date=now.strftime("%d.%m.%Y"),
        weekday=now.strftime("%A"),
        time=now.strftime("%H:%M"),
        username=context.bot.name,
    )

    if chat_id is None or (thread_id is None):
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=payload,
        message_thread_id=thread_id,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


def parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(hour=int(hh), minute=int(mm))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я буду присылать ежедневный список задач в привязанный топик.\n"
        "Команды: /bind, /settime HH:MM, /settemplate <текст>, /preview, /postnow, /settz <IANA tz>, /whereami"
    )


def require_owner(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cfg = load_config()
        owner = cfg.get("owner_id")
        user_id = update.effective_user.id if update.effective_user else None
        # Первый вызов любым пользователем назначает owner
        if owner is None and user_id:
            cfg["owner_id"] = user_id
            save_config(cfg)
        elif owner is not None and owner != user_id:
            await update.message.reply_text("Только владелец бота может это делать.")
            return
        return await func(update, context)
    return wrapper


@require_owner
async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = update.effective_message
    if not msg:
        return
    cfg["chat_id"] = msg.chat_id
    cfg["thread_id"] = getattr(msg, "message_thread_id", None)
    save_config(cfg)
    await update.message.reply_text(
        f"Привязано!\nchat_id = {cfg['chat_id']}\nthread_id = {cfg['thread_id']}"
    )
    await reschedule_jobs(context.application)


@require_owner
async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /settime HH:MM")
        return
    hhmm = context.args[0]
    _ = parse_hhmm(hhmm)
    cfg = load_config()
    cfg["time"] = hhmm
    save_config(cfg)
    await update.message.reply_text(f"Время обновлено: {hhmm}")
    await reschedule_jobs(context.application)


@require_owner
async def settz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /settz Europe/Moscow")
        return
    tzname = context.args[0]
    if tz.gettz(tzname) is None:
        await update.message.reply_text("Неверный IANA tz. Пример: Asia/Nicosia")
        return
    cfg = load_config()
    cfg["tz"] = tzname
    save_config(cfg)
    await update.message.reply_text(f"Часовой пояс обновлён: {tzname}")
    await reschedule_jobs(context.application)


@require_owner
async def settemplate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    # Обрезаем команду
    without_cmd = text.split(" ", 1)
    if len(without_cmd) < 2:
        await update.message.reply_text(
            "Формат: /settemplate <текст шаблона>\nДоступные плейсхолдеры: {date}, {weekday}, {time}, {username}"
        )
        return
    template = without_cmd[1]
    cfg = load_config()
    cfg["template"] = template
    save_config(cfg)
    await update.message.reply_text("Шаблон обновлён. /preview для проверки")


async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    tzinfo = tz.gettz(cfg.get("tz", DEFAULT_TZ))
    now = datetime.now(tzinfo)
    payload = cfg.get("template", DEFAULT_TEMPLATE).format(
        date=now.strftime("%d.%m.%Y"),
        weekday=now.strftime("%A"),
        time=now.strftime("%H:%M"),
        username=context.bot.name,
    )
    await update.message.reply_text(payload, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@require_owner
async def postnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_tasks(context)
    await update.message.reply_text("Отправлено в привязанный топик.")


async def whereami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await update.message.reply_text(
        f"chat_id = {msg.chat_id}\nthread_id = {getattr(msg, 'message_thread_id', None)}"
    )


async def reschedule_jobs(app: Application):
    # После application.initialize() job_queue уже должен быть
    if app.job_queue is None:
        return

    # Считываем настройки
    cfg = load_config()
    tzinfo = tz.gettz(cfg.get("tz", DEFAULT_TZ))
    hhmm = cfg.get("time", "09:00")
    when = parse_hhmm(hhmm)

    # Обновляем таймзону у планировщика (APSCHEDULER) — ГЛАВНОЕ ИЗМЕНЕНИЕ
    app.job_queue.scheduler.configure(timezone=tzinfo)

    # Удаляем предыдущие задания с таким именем
    for job in app.job_queue.get_jobs_by_name("daily_tasks"):
        job.schedule_removal()

    # Ставим ежедневную задачу БЕЗ tzinfo в аргументах (его теперь нет в v21)
    app.job_queue.run_daily(
        callback=send_tasks,
        time=when,
        days=(0, 1, 2, 3, 4, 5, 6),
        name="daily_tasks",
    )


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установите переменную окружения BOT_TOKEN")

    application = Application.builder().token(token).build()

    # ВАЖНО: сначала инициализируем, чтобы появился job_queue
    await application.initialize()

    # Регистрируем команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("bind", bind_cmd))
    application.add_handler(CommandHandler("settime", settime_cmd))
    application.add_handler(CommandHandler("settemplate", settemplate_cmd))
    application.add_handler(CommandHandler("preview", preview_cmd))
    application.add_handler(CommandHandler("postnow", postnow_cmd))
    application.add_handler(CommandHandler("whereami", whereami_cmd))
    application.add_handler(CommandHandler("settz", settz_cmd))

    # Планируем ежедневную задачу (уже после initialize)
    await reschedule_jobs(application)

    # Стартуем бота
    await application.start()
    try:
        await application.updater.start_polling(drop_pending_updates=True)
        print("Bot started")
        await asyncio.Event().wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
