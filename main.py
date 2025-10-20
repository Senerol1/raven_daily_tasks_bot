import os
import json
from datetime import datetime, time
from dateutil import tz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# === Конфигурация и значения по умолчанию ===
CONFIG_PATH = "config.json"
DEFAULT_TEMPLATE = (
    "Список задач на {date} ({weekday}):\n"
    "— [ ] Приоритет дня\n"
    "— [ ] 2\n"
    "— [ ] 3\n"
)
DEFAULT_TZ = os.getenv("BOT_TZ", "Asia/Nicosia")


# === Работа с конфигом ===
def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {
            "chat_id": None,
            "thread_id": None,
            "time": "09:00",
            "tz": DEFAULT_TZ,
            "template": DEFAULT_TEMPLATE,
            "owner_id": None,
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# === Вспомогательные ===
def parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(hour=int(hh), minute=int(mm))


def require_owner(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cfg = load_config()
        owner = cfg.get("owner_id")
        user_id = update.effective_user.id if update.effective_user else None
        if owner is None and user_id:
            cfg["owner_id"] = user_id
            save_config(cfg)
        elif owner is not None and owner != user_id:
            await update.message.reply_text("Только владелец бота может это делать.")
            return
        return await func(update, context)
    return wrapper


# === Джоб: отправка задач ===
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

    if not chat_id or thread_id is None:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=payload,
        message_thread_id=thread_id,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# === Команды ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я буду присылать ежедневный список задач в привязанный топик.\n"
        "Команды: /bind, /settime HH:MM, /settemplate <текст>, /preview, /postnow, /settz <IANA tz>, /whereami"
    )


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
    reschedule_jobs(context.application)


@require_owner
async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /settime HH:MM")
        return
    hhmm = context.args[0]
    _ = parse_hhmm(hhmm)  # валидация
    cfg = load_config()
    cfg["time"] = hhmm
    save_config(cfg)
    await update.message.reply_text(f"Время обновлено: {hhmm}")
    reschedule_jobs(context.application)


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
    reschedule_jobs(context.application)


@require_owner
async def settemplate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Формат: /settemplate <текст шаблона>\n"
            "Доступные плейсхолдеры: {date}, {weekday}, {time}, {username}"
        )
        return
    template = parts[1]
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
    await update.message.reply_text(
        payload, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


@require_owner
async def postnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_tasks(context)
    await update.message.reply_text("Отправлено в привязанный топик.")


async def whereami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await update.message.reply_text(
        f"chat_id = {msg.chat_id}\nthread_id = {getattr(msg, 'message_thread_id', None)}"
    )


# === Планировщик ===
def reschedule_jobs(app: Application):
    if app.job_queue is None:
        return
    cfg = load_config()
    tzinfo = tz.gettz(cfg.get("tz", DEFAULT_TZ))
    # Правильный способ поставить таймзону в PTB v21:
    app.job_queue.scheduler.configure(timezone=tzinfo)

    # Снимаем старые задания с этим именем
    for job in app.job_queue.get_jobs_by_name("daily_tasks"):
        job.schedule_removal()

    when = parse_hhmm(cfg.get("time", "09:00"))
    app.job_queue.run_daily(
        callback=send_tasks,
        time=when,
        days=(0, 1, 2, 3, 4, 5, 6),
        name="daily_tasks",
    )


# === post_init для подготовки перед стартом ===
async def post_init(app: Application):
    # На всякий случай: убираем вебхук и старые апдейты
    await app.bot.delete_webhook(drop_pending_updates=True)
    # Планируем ежедневную отправку
    reschedule_jobs(app)


# === Точка входа (СИНХРОННАЯ) ===
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установите переменную окружения BOT_TOKEN")

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)  # хук выполнится перед запуском polling
        .build()
    )

    # Хендлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("bind", bind_cmd))
    application.add_handler(CommandHandler("settime", settime_cmd))
    application.add_handler(CommandHandler("settemplate", settemplate_cmd))
    application.add_handler(CommandHandler("preview", preview_cmd))
    application.add_handler(CommandHandler("postnow", postnow_cmd))
    application.add_handler(CommandHandler("whereami", whereami_cmd))
    application.add_handler(CommandHandler("settz", settz_cmd))

    print("Starting polling ...", flush=True)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    print("Polling stopped", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e!r}", flush=True)
        raise
