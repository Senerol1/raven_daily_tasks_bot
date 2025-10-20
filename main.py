import os
import json
from datetime import datetime, time
from dateutil import tz
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# === Конфигурация ===
CONFIG_PATH = "config.json"
DEFAULT_TZ = os.getenv("BOT_TZ", "Asia/Nicosia")
DEFAULT_TEMPLATE = (
    "Список задач на {date} ({weekday}):\n"
    "— [ ] Приоритет дня\n"
    "— [ ] 2\n"
    "— [ ] 3\n"
)

# === Хранилище конфигурации ===
def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {
            "chat_id": None,
            "thread_id": None,
            "time": "09:00",
            "tz": DEFAULT_TZ,
            "template": DEFAULT_TEMPLATE,
            "owner_id": None,
            "tasks": [],          # <--- список задач
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("tasks", [])
    return cfg

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

# --- Форматирование вспомогательного текста ---
def make_header(tzname: str, botname: str) -> str:
    tzinfo = tz.gettz(tzname)
    now = datetime.now(tzinfo)
    return f"Задачи на {now.strftime('%d.%m.%Y')} ({now.strftime('%A')})"

# === Основная отправка по расписанию ===
async def send_tasks(context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    chat_id = cfg.get("chat_id")
    thread_id = cfg.get("thread_id")
    tzname = cfg.get("tz", DEFAULT_TZ)
    tasks = list(cfg.get("tasks", []))  # копия
    botname = context.bot.name

    if not chat_id:
        return  # не привязано

    # Если задач нет — отправим шаблон текстом.
    if not tasks:
        tzinfo = tz.gettz(tzname)
        now = datetime.now(tzinfo)
        payload = cfg.get("template", DEFAULT_TEMPLATE).format(
            date=now.strftime("%d.%m.%Y"),
            weekday=now.strftime("%A"),
            time=now.strftime("%H:%M"),
            username=botname,
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=payload,
            message_thread_id=thread_id,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    # Telegram ограничивает кол-во опций в Poll (обычно до 10).
    # Разобьём список задач на пачки по 10 и отправим несколько опросов.
    header = make_header(tzname, botname)
    chunk_size = 10
    for i in range(0, len(tasks), chunk_size):
        chunk = tasks[i:i + chunk_size]
        suffix = "" if len(tasks) <= chunk_size else f" (часть {i // chunk_size + 1})"
        question = f"{header}{suffix}"
        await context.bot.send_poll(
            chat_id=chat_id,
            question=question[:300],  # лимит Telegram на длину вопроса
            options=chunk,
            allows_multiple_answers=True,
            is_anonymous=False,
            message_thread_id=thread_id,
        )

# === Команды ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я буду присылать ежедневный список задач в виде опроса с чекбоксами.\n"
        "Сначала привяжи чат/ветку: /bind\n\n"
        "Команды:\n"
        "/bind — привязать текущий чат/ветку для рассылки\n"
        "/settime HH:MM — время ежедневной рассылки\n"
        "/settz <IANA tz> — часовой пояс (например Europe/Moscow)\n"
        "/addtask <текст> — добавить задачу\n"
        "/deltask <номер> — удалить задачу по номеру\n"
        "/listtasks — показать текущие задачи\n"
        "/cleartasks — очистить все задачи\n"
        "/preview — показать превью сообщения на сегодня\n"
        "/postnow — отправить сейчас\n"
    )

@require_owner
async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = update.effective_message
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
        await update.message.reply_text("Неверный IANA tz. Пример: Europe/Moscow")
        return
    cfg = load_config()
    cfg["tz"] = tzname
    save_config(cfg)
    await update.message.reply_text(f"Часовой пояс обновлён: {tzname}")
    reschedule_jobs(context.application)

@require_owner
async def addtask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    parts = text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text("Формат: /addtask <текст задачи>")
        return
    task = parts[1].strip()
    cfg = load_config()
    tasks = cfg.get("tasks", [])
    # Защитимся от слишком длинных пунктов (ограничения Telegram)
    if len(task) > 100:
        task = task[:100]
    tasks.append(task)
    cfg["tasks"] = tasks
    save_config(cfg)
    await update.message.reply_text(f"✅ Добавлено: {task}\nВсего задач: {len(tasks)}")

@require_owner
async def deltask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: /deltask <номер> (смотрите /listtasks)")
        return
    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("Номер должен быть числом. Пример: /deltask 2")
        return
    cfg = load_config()
    tasks = cfg.get("tasks", [])
    if 0 <= idx < len(tasks):
        removed = tasks.pop(idx)
        cfg["tasks"] = tasks
        save_config(cfg)
        await update.message.reply_text(f"🗑 Удалено: {removed}\nОсталось: {len(tasks)}")
    else:
        await update.message.reply_text("Нет задачи с таким номером.")

async def listtasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    tasks = cfg.get("tasks", [])
    if not tasks:
        await update.message.reply_text("Список задач пуст. Добавьте: /addtask <текст>")
        return
    lines = [f"{i+1}. {t}" for i, t in enumerate(tasks)]
    await update.message.reply_text("Текущие задачи:\n" + "\n".join(lines))

@require_owner
async def cleartasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    cfg["tasks"] = []
    save_config(cfg)
    await update.message.reply_text("Список задач очищен.")

async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    tzname = cfg.get("tz", DEFAULT_TZ)
    tasks = cfg.get("tasks", [])
    header = make_header(tzname, context.bot.name)
    if tasks:
        lines = "\n".join(f"— [ ] {t}" for t in tasks)
        await update.message.reply_text(f"{header}\n{lines}")
    else:
        tzinfo = tz.gettz(tzname)
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
    await update.message.reply_text("Отправлено.")

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
    # Установим таймзону планировщику
    app.job_queue.scheduler.configure(timezone=tzinfo)

    # Удалим старые задания с этим именем
    for job in app.job_queue.get_jobs_by_name("daily_tasks"):
        job.schedule_removal()

    when = parse_hhmm(cfg.get("time", "09:00"))
    app.job_queue.run_daily(
        callback=send_tasks,
        time=when,
        days=(0, 1, 2, 3, 4, 5, 6),
        name="daily_tasks",
    )

# === post_init ===
async def post_init(app: Application):
    await app.bot.delete_webhook(drop_pending_updates=True)
    reschedule_jobs(app)

# === Точка входа (синхронная) ===
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Установите переменную окружения BOT_TOKEN")

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # Хендлеры
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("bind", bind_cmd))
    application.add_handler(CommandHandler("settime", settime_cmd))
    application.add_handler(CommandHandler("settz", settz_cmd))

    application.add_handler(CommandHandler("addtask", addtask_cmd))
    application.add_handler(CommandHandler("deltask", deltask_cmd))
    application.add_handler(CommandHandler("listtasks", listtasks_cmd))
    application.add_handler(CommandHandler("cleartasks", cleartasks_cmd))

    application.add_handler(CommandHandler("preview", preview_cmd))
    application.add_handler(CommandHandler("postnow", postnow_cmd))
    application.add_handler(CommandHandler("whereami", whereami_cmd))

    print("Starting polling ...", flush=True)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    print("Polling stopped", flush=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e!r}", flush=True)
        raise
