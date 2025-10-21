# main.py
import os
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, time as dtime, timezone

from tzlocal import get_localzone

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder,
    CommandHandler, MessageHandler, ContextTypes, filters,
)

# ---------------- ЛОГИ ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("bot")

STATE_PATH = "state.json"


# ---------------- СОСТОЯНИЕ ----------------
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"chat_id": None, "thread_id": None, "send_time": "09:00", "tasks": []}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


STATE = load_state()


def tzinfo():
    # Локальный TZ; фоллбэк — UTC
    try:
        return get_localzone()
    except Exception:
        return timezone.utc


# ---------------- УТИЛИТЫ ----------------
def parse_time_str(hhmm: str, tz) -> Optional[dtime]:
    try:
        hh, mm = hhmm.strip().split(":")
        return dtime(hour=int(hh), minute=int(mm), second=0, tzinfo=tz)
    except Exception:
        return None


def chunk(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i:i + n] for i in range(0, len(lst), n)]


# ---------------- ОТПРАВКА ОПРОСОВ ----------------
async def send_tasks_poll(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    thread_id: Optional[int],
) -> None:
    tasks: List[str] = STATE.get("tasks", [])
    if not tasks:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Список задач пуст.",
            message_thread_id=thread_id
        )
        return

    groups = chunk(tasks, 10)
    today = datetime.now(tzinfo()).strftime("%d.%m.%Y")

    for idx, group in enumerate(groups, start=1):
        q = f"Ежедневные задачи — {today}"
        if len(groups) > 1:
            q += f" ({idx}/{len(groups)})"
        await context.bot.send_poll(
            chat_id=chat_id,
            question=q,
            options=group,
            is_anonymous=False,
            allows_multiple_answers=True,
            message_thread_id=thread_id
        )


# ---------------- ПЛАНИРОВЩИК ----------------
def reschedule_daily_job(app: Application) -> None:
    try:
        app.job_queue.scheduler.remove_all_jobs()
    except Exception:
        pass

    tz = tzinfo()
    send_time = parse_time_str(str(STATE.get("send_time", "09:00")), tz)
    c_id = STATE.get("chat_id")
    if not (send_time and c_id):
        log.info("[reschedule] пропуск: нет chat_id или времени")
        return

    th_id = STATE.get("thread_id")
    # В PTB 21.7 timezone задаётся через tz-aware time; параметра timezone= НЕТ
    app.job_queue.run_daily(
        callback=lambda ctx: send_tasks_poll(ctx, chat_id=c_id, thread_id=th_id),
        time=send_time,
        name="daily_tasks_poll",
    )
    log.info(f"[reschedule] ежедневная отправка в {STATE['send_time']} (TZ={tz})")


# ---------------- КОМАНДЫ ----------------
async def bind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    thread_id = msg.message_thread_id if getattr(msg, "is_topic_message", False) else None

    STATE["chat_id"] = chat.id
    STATE["thread_id"] = thread_id
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


async def cleartasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    STATE["tasks"] = []
    save_state(STATE)
    await update.effective_message.reply_text("Готово: список задач очищен.")


async def settime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Использование: /settime HH:MM (локальное время сервера)")
        return
    t = parse_time_str(context.args[0], tzinfo())
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


async def any_command_echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Фоллбэк на неизвестные команды
    cmd = update.effective_message.text or ""
    await update.effective_message.reply_text(f"Команда получена: {cmd}")


# ---------------- ОШИБКИ ----------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error: %s", context.error)


# ---------------- MAIN ----------------
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    base_url = os.getenv("BASE_URL", "")
    if not token or not base_url:
        raise RuntimeError("Нужны переменные окружения TELEGRAM_TOKEN и BASE_URL")

    base_url = base_url.rstrip("/")
    port = int(os.getenv("PORT", "10000"))

    app: Application = ApplicationBuilder().token(token).build()

    # Команды
    app.add_handler(CommandHandler("bind", bind_cmd))
    app.add_handler(CommandHandler("whereami", whereami_cmd))
    app.add_handler(CommandHandler("addtask", addtask_cmd))
    app.add_handler(CommandHandler("listtasks", listtasks_cmd))
    app.add_handler(CommandHandler("deltask", deltask_cmd))
    app.add_handler(CommandHandler("cleartasks", cleartasks_cmd))
    app.add_handler(CommandHandler("settime", settime_cmd))
    app.add_handler(CommandHandler("postnow", postnow_cmd))

    # Фоллбэк на любые неизвестные /команды
    app.add_handler(MessageHandler(filters.COMMAND, any_command_echo))

    # Глобальный обработчик ошибок
    app.add_error_handler(on_error)

    # Запланировать ежедневную отправку, если chat_id/время уже сохранены
    reschedule_daily_job(app)

    log.info("Стартую webhook на порту %s | webhook=%s/%s", port, base_url, token)
    # ВАЖНО: метод синхронный и сам управляет event loop
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_url=f"{base_url}/{token}",
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.exception("Fatal error on startup")
