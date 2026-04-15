import os
import sys
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_IDS = set(
    int(uid.strip())
    for uid in os.getenv("AUTHORIZED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
)


def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_IDS


def log(msg: str):
    print(msg, flush=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log(f"[IN ] {user.id} ({user.username}): /start")

    if not is_authorized(user.id):
        reply = "⛔ אין לך הרשאה להשתמש בבוט זה."
        log(f"[OUT] -> {reply}")
        await update.message.reply_text(reply)
        return

    reply = "👋 שלום! שלח פקודה (בלי /) והיא תרוץ על המחשב שלך."

    log(f"[OUT] -> {reply}")
    await update.message.reply_text(reply)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    log(f"[IN ] {user.id} ({user.username}): {text}")

    if not is_authorized(user.id):
        reply = "⛔ אין לך הרשאה."
        log(f"[OUT] -> {reply}")
        await update.message.reply_text(reply)
        return

    if not text or text.startswith("/"):
        return

    await execute_and_reply(update, text)


async def execute_and_reply(update: Update, cmd: str):
    log(f"[CMD] Running: {cmd}")

    await update.message.reply_text(f"⚙️ מריץ:\n`{cmd}`", parse_mode="Markdown")

    try:
        # 🔥 חשוב: מריץ דרך CMD
        proc = await asyncio.create_subprocess_exec(
            "cmd.exe", "/c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        stdout_text = (stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")

        log(f"[STDOUT]\n{stdout_text}")
        log(f"[STDERR]\n{stderr_text}")

        if not stdout_text and not stderr_text:
            stdout_text = "(אין פלט)"

        exit_emoji = "✅" if proc.returncode == 0 else "❌"

        message = f"{exit_emoji} Exit code: `{proc.returncode}`\n\n"

        if stdout_text:
            message += f"📤 STDOUT:\n```\n{stdout_text}\n```\n"

        if stderr_text:
            message += f"⚠️ STDERR:\n```\n{stderr_text}\n```"

        log(f"[EXIT] code={proc.returncode}")

        await update.message.reply_text(message, parse_mode="Markdown")

    except Exception as e:
        log(f"[ERROR] {repr(e)}")
        await update.message.reply_text(f"💥 שגיאה: `{e}`", parse_mode="Markdown")


def main():
    if not BOT_TOKEN:
        raise ValueError("חסר TELEGRAM_BOT_TOKEN ב-.env")
    if not AUTHORIZED_IDS:
        raise ValueError("חסר AUTHORIZED_USER_IDS ב-.env")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    log(f"🤖 הבוט רץ... משתמשים מורשים: {AUTHORIZED_IDS}")

    # 🔥 יצירת loop (בלי policy ישן!)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app.run_polling()


if __name__ == "__main__":
    main()