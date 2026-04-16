import os
import re
import asyncio
import io
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_IDS = set(
    int(uid.strip())
    for uid in os.getenv("AUTHORIZED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
)

USER_CWD: dict[int, str] = {}
DEFAULT_CWD = os.getcwd()

CMD_TIMEOUT      = 120    # מקסימום זמן לפקודה רגילה (שניות)
OUTPUT_IDLE_WAIT = 0.4    # המתנה אחרי קריאה מוצלחת לבדיקת פלט נוסף
PROGRESS_INTERVAL = 10.0  # כל כמה שניות שולחים עדכון התקדמות

INTERACTIVE_SHELLS = {
    "powershell", "powershell.exe",
    "cmd", "cmd.exe",
    "python", "python3", "python.exe",
    "node", "node.exe",
    "wsl", "bash", "sh",
    "ftp", "sftp", "telnet",
    "ollama",  # נוסף
}

# ──────────────────────────────────────────────
#  הגדרות משתמש
# ──────────────────────────────────────────────

MAX_OUTPUT_OPTIONS = [500, 1_000, 2_000, 4_000, 8_000]
USER_SETTINGS: dict[int, dict] = {}


def get_settings(user_id: int) -> dict:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = {"max_output_chars": 2_000}
    return USER_SETTINGS[user_id]


# ──────────────────────────────────────────────
#  Utils
# ──────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_IDS


def log(msg: str):
    print(msg, flush=True)


def get_cwd(user_id: int) -> str:
    return USER_CWD.get(user_id, DEFAULT_CWD)


TELEGRAM_MAX = 4_000


async def send_chunks(message, text: str, parse_mode: str = "Markdown"):
    """שולח טקסט ארוך בכמה הודעות נפרדות."""
    while text:
        if len(text) <= TELEGRAM_MAX:
            await message.reply_text(text, parse_mode=parse_mode)
            return
        split = text.rfind("\n", 0, TELEGRAM_MAX)
        if split < TELEGRAM_MAX // 2:
            split = TELEGRAM_MAX
        await message.reply_text(text[:split], parse_mode=parse_mode)
        text = text[split:]


async def send_output(
    message,
    user_id: int,
    output: str,
    header: str = "",
    suffix: str = "",
):
    """
    שולח פלט למשתמש.
    אם הפלט עולה על הסף שנבחר בהגדרות — שולח קובץ .log.
    אחרת מחלק להודעות לפי הצורך.
    """
    settings = get_settings(user_id)
    max_chars = settings["max_output_chars"]

    if len(output) > max_chars:
        file_bytes = output.encode("utf-8")
        caption = f"{header}📄 פלט ארוך ({len(output):,} תווים > {max_chars:,}){suffix}"
        await message.reply_document(
            document=io.BytesIO(file_bytes),
            filename="output.log",
            caption=caption[:1_000],
        )
        return

    body = f"```\n{output}\n```" if output else ""
    await send_chunks(message, header + body + suffix)


# ══════════════════════════════════════════════
#  ShellSession — process מתמשך לכל משתמש
# ══════════════════════════════════════════════

class ShellSession:
    def __init__(self, proc: asyncio.subprocess.Process, shell_name: str):
        self.proc = proc
        self.shell_name = shell_name

    def is_alive(self) -> bool:
        return self.proc.returncode is None

    async def send_line(self, text: str):
        self.proc.stdin.write((text + "\n").encode("utf-8", errors="replace"))
        await self.proc.stdin.drain()

    async def read_burst(self, first_timeout: float = OUTPUT_IDLE_WAIT) -> str:
        """קורא פלט עד שאין תנועה, עם timeout ראשוני מותאם."""
        output = b""
        timeout = first_timeout
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self.proc.stdout.read(4_096), timeout=timeout
                )
                if not chunk:
                    break
                output += chunk
                timeout = OUTPUT_IDLE_WAIT  # אחרי קריאה ראשונה — פחות סבלנות
            except asyncio.TimeoutError:
                break
        return output.decode("utf-8", errors="replace")

    async def interrupt(self):
        """שולח Ctrl+C (SIGINT) לתהליך."""
        import signal
        try:
            self.proc.send_signal(signal.SIGINT)
        except Exception:
            pass

    async def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass


USER_SESSIONS: dict[int, ShellSession] = {}
USER_TASKS: dict[int, asyncio.Task] = {}   # task פעיל של forward_to_session לכל משתמש


async def start_session(shell_cmd: str, cwd: str) -> ShellSession:
    """פותח process אינטראקטיבי, תומך בפקודות עם ארגומנטים (כגון ollama run tinyllama)."""
    parts = shell_cmd.split()
    proc = await asyncio.create_subprocess_exec(
        *parts,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    return ShellSession(proc, shell_cmd)


# ══════════════════════════════════════════════
#  Command Handlers
# ══════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log(f"[IN ] {user.id} ({user.username}): /start")
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ אין לך הרשאה להשתמש בבוט זה.")
        return

    cwd = get_cwd(user.id)
    session_info = ""
    sess = USER_SESSIONS.get(user.id)
    if sess and sess.is_alive():
        session_info = f"\n🔗 Session פעיל: `{sess.shell_name}`"

    await update.message.reply_text(
        f"👋 שלום! שלח פקודה והיא תרוץ על המחשב שלך.\n"
        f"📁 נתיב נוכחי: `{cwd}`{session_info}\n\n"
        f"/exit — סגור session אינטראקטיבי\n"
        f"/stop — שלח Ctrl+C לתהליך הפעיל\n"
        f"/settings — הגדרות\n"
        f"/help — כל הפקודות",
        parse_mode="Markdown",
    )


async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        return
    # בטל את לולאת ההמתנה אם פעילה
    task = USER_TASKS.pop(user.id, None)
    if task and not task.done():
        task.cancel()
    session = USER_SESSIONS.pop(user.id, None)
    if session:
        await session.kill()
        await update.message.reply_text(
            f"🔌 Session `{session.shell_name}` נסגר.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("אין session פעיל לסגירה.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """שולח Ctrl+C לתהליך הפעיל מבלי לסגור את ה-session."""
    user = update.effective_user
    if not is_authorized(user.id):
        return
    session = USER_SESSIONS.get(user.id)
    if not session or not session.is_alive():
        await update.message.reply_text("אין session פעיל להפסיק.")
        return
    # בטל את לולאת ההמתנה כדי לשחרר את הבוט
    task = USER_TASKS.pop(user.id, None)
    if task and not task.done():
        task.cancel()
    await session.interrupt()
    await update.message.reply_text(
        f"🛑 נשלח Ctrl+C ל-`{session.shell_name}`.\n"
        f"Session עדיין פתוח — המשך לשלוח פקודות, או `/exit` לסגירה.",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        return
    await update.message.reply_text(
        "📖 *פקודות הבוט*\n\n"
        "🔧 *ניהול session*\n"
        "`/exit` — סגור session אינטראקטיבי (kill)\n"
        "`/stop` — שלח Ctrl+C לתהליך הפעיל (SIGINT) מבלי לסגור\n\n"
        "⚙️ *הגדרות*\n"
        "`/settings` — שנה את סף הפלט (500–8,000 תווים)\n\n"
        "ℹ️ *כללי*\n"
        "`/start` — הצג מצב נוכחי ונתיב עבודה\n"
        "`/help` — הצג הודעה זו\n\n"
        "💡 *טיפים*\n"
        "• פקודות כגון `python`, `node`, `bash` פותחות session אינטראקטיבי\n"
        "• כשרואים ⏳ *ממתין לתגובה* — ייתכן שהתהליך מחכה לקלט: שלח את הקלט, "
        "או `/stop` לביטול, או `/exit` לסגירה\n"
        "• ניתן לשלוח מספר פקודות בהודעה אחת (שורה לכל פקודה)",
        parse_mode="Markdown",
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        return
    await _render_settings(update.message, user.id, edit=False)


async def _render_settings(message, user_id: int, edit: bool):
    settings = get_settings(user_id)
    current = settings["max_output_chars"]

    rows, row = [], []
    for opt in MAX_OUTPUT_OPTIONS:
        label = f"{'✅ ' if opt == current else ''}{opt:,}"
        row.append(InlineKeyboardButton(label, callback_data=f"set_max:{opt}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    text = (
        "⚙️ *הגדרות*\n\n"
        "📏 *סף פלט לקובץ*\n"
        f"פלט מעל `{current:,}` תווים יישלח כקובץ `.log`\n\n"
        "בחר ערך:"
    )
    markup = InlineKeyboardMarkup(rows)
    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def callback_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()
    if not is_authorized(user.id):
        return
    if query.data.startswith("set_max:"):
        new_val = int(query.data.split(":")[1])
        get_settings(user.id)["max_output_chars"] = new_val
        await _render_settings(query.message, user.id, edit=True)


# ══════════════════════════════════════════════
#  Message Handler
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    log(f"[IN ] {user.id} ({user.username}): {text}")

    if not is_authorized(user.id):
        await update.message.reply_text("⛔ אין לך הרשאה.")
        return
    if not text or text.startswith("/"):
        return

    session = USER_SESSIONS.get(user.id)
    if session:
        if not session.is_alive():
            del USER_SESSIONS[user.id]
            await update.message.reply_text(
                f"⚠️ Session `{session.shell_name}` נסגר (process יצא).\n"
                "ממשיך במצב רגיל...",
                parse_mode="Markdown",
            )
        else:
            # הרץ את לולאת ההמתנה כ-task נפרד — ה-handler מסתיים מיד
            task = asyncio.create_task(
                forward_to_session(update, user.id, session, text)
            )
            USER_TASKS[user.id] = task
            return

    first_token = text.strip().split()[0].lower()
    if first_token in INTERACTIVE_SHELLS:
        await open_interactive_session(update, user.id, text.strip())
        return

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    await execute_lines(update, user.id, lines)


# ══════════════════════════════════════════════
#  Interactive session
# ══════════════════════════════════════════════

async def forward_to_session(
    update: Update, user_id: int, session: ShellSession, text: str
):
    """
    שולח קלט ל-session.
    כל PROGRESS_INTERVAL שניות ללא פלט — שולח עדכון למשתמש.
    """
    log(f"[SESSION:{session.shell_name}] <- {text}")

    try:
        await session.send_line(text)
    except Exception as e:
        await update.message.reply_text(f"[שגיאה בשליחה: {e}]")
        return

    accumulated = ""
    total_elapsed = 0.0

    while total_elapsed < CMD_TIMEOUT:
        burst = await session.read_burst(first_timeout=PROGRESS_INTERVAL)

        if burst:
            accumulated += burst
            total_elapsed = 0.0  # אפס את שעון הדממה — קיבלנו פלט

        if not session.is_alive():
            break

        if not burst:
            # עבר PROGRESS_INTERVAL שלם בלי פלט — שלח עדכון
            total_elapsed += PROGRESS_INTERVAL

            if accumulated:
                # יש פלט שנצבר — הצג אותו עם כפתורי עזרה
                notice = (
                    f"⏳ *עדיין עובד...* ({int(total_elapsed)}s ללא פלט חדש)\n"
                    f"📋 פלט עד כה:\n```\n{accumulated[-1_000:]}\n```\n"
                    f"• `/stop` — Ctrl+C  •  `/exit` — סגור session"
                )
            else:
                # אין פלט בכלל — כנראה ממתין לקלט
                notice = (
                    f"⏳ *ממתין לתגובה...* ({int(total_elapsed)}s)\n"
                    f"💬 ייתכן שהתהליך ממתין לקלט שלך.\n"
                    f"• שלח קלט כרגיל, או\n"
                    f"• `/stop` לשליחת Ctrl+C, או\n"
                    f"• `/exit` לסגירת ה-session"
                )

            await send_chunks(update.message, notice)
    else:
        await update.message.reply_text(f"⏱️ Timeout ({int(CMD_TIMEOUT)}s) — שלח `/exit` לסגירה.")

    suffix = ""
    if not session.is_alive():
        USER_SESSIONS.pop(user_id, None)
        suffix = "\n\n🔌 Session נסגר (process יצא)."

    USER_TASKS.pop(user_id, None)   # נקה את ה-task
    log(f"[SESSION:{session.shell_name}] -> {accumulated!r}")
    await send_output(update.message, user_id, accumulated or "(אין פלט)", suffix=suffix)


async def open_interactive_session(update: Update, user_id: int, shell_cmd: str):
    cwd = get_cwd(user_id)
    old = USER_SESSIONS.pop(user_id, None)
    if old:
        await old.kill()

    log(f"[SESSION] Opening: {shell_cmd} in {cwd}")
    await update.message.reply_text(
        f"🔗 פותח session: `{shell_cmd}`\nשלח `/exit` לסגירה.",
        parse_mode="Markdown",
    )

    try:
        session = await start_session(shell_cmd, cwd)
        USER_SESSIONS[user_id] = session
        opening = await session.read_burst(first_timeout=2.0)
        if opening.strip():
            await send_output(update.message, user_id, opening)
    except Exception as e:
        log(f"[ERROR] {repr(e)}")
        await update.message.reply_text(
            f"💥 שגיאה בפתיחת session: `{e}`", parse_mode="Markdown"
        )


# ══════════════════════════════════════════════
#  Non-interactive commands
# ══════════════════════════════════════════════

async def run_command_with_progress(
    cmd: str, cwd: str, on_progress
) -> tuple[str, str, int | None]:
    """
    מריץ פקודה דרך cmd.exe.
    כל PROGRESS_INTERVAL שניות שולח עדכון דרך on_progress(stdout_so_far, elapsed).
    """
    proc = await asyncio.create_subprocess_exec(
        "cmd.exe", "/c", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    start = asyncio.get_event_loop().time()

    async def read_stdout():
        while True:
            chunk = await proc.stdout.read(4_096)
            if not chunk:
                break
            stdout_buf.append(chunk.decode("utf-8", errors="replace"))

    async def read_stderr():
        while True:
            chunk = await proc.stderr.read(4_096)
            if not chunk:
                break
            stderr_buf.append(chunk.decode("utf-8", errors="replace"))

    async def progress_loop():
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            if proc.returncode is not None:
                break
            elapsed = asyncio.get_event_loop().time() - start
            await on_progress("".join(stdout_buf), elapsed)

    t_out  = asyncio.create_task(read_stdout())
    t_err  = asyncio.create_task(read_stderr())
    t_prog = asyncio.create_task(progress_loop())

    try:
        await asyncio.wait_for(
            asyncio.gather(t_out, t_err, proc.wait()),
            timeout=CMD_TIMEOUT,
        )
        returncode = proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        returncode = None
    finally:
        t_prog.cancel()
        try:
            await t_prog
        except asyncio.CancelledError:
            pass

    return "".join(stdout_buf), "".join(stderr_buf), returncode


async def execute_lines(update: Update, user_id: int, lines: list[str]):
    cwd = get_cwd(user_id)

    for cmd in lines:
        log(f"[CMD] cwd={cwd} | {cmd}")

        # ─── CD ───
        cd_match = re.match(r"^cd\s+(.*)", cmd, re.IGNORECASE)
        if cd_match:
            target = cd_match.group(1).strip().strip('"').strip("'")
            new_path = os.path.normpath(os.path.join(cwd, target))
            if os.path.isdir(new_path):
                cwd = new_path
                USER_CWD[user_id] = cwd
                await update.message.reply_text(f"📁 `cd` → `{cwd}`", parse_mode="Markdown")
            else:
                await update.message.reply_text(
                    f"❌ `cd {target}` — נתיב לא קיים: `{new_path}`",
                    parse_mode="Markdown",
                )
            continue

        # ─── progress callback (capture cmd in default arg) ───
        async def on_progress(current_out: str, elapsed: float, _cmd: str = cmd):
            msg = f"⏳ `{_cmd}` — עובד... ({int(elapsed)}s)"
            if current_out.strip():
                msg += f"\n```\n{current_out[-500:]}\n```"
            await send_chunks(update.message, msg)

        stdout_text, stderr_text, returncode = await run_command_with_progress(
            cmd, cwd, on_progress
        )

        exit_emoji = "⏱️" if returncode is None else ("✅" if returncode == 0 else "❌")
        exit_label = "Timeout" if returncode is None else f"Exit: {returncode}"
        header = f"{exit_emoji} `{cmd}` — {exit_label}\n"

        full_output = stdout_text or ""
        if stderr_text:
            full_output += ("\n" if full_output else "") + f"⚠️ STDERR:\n{stderr_text}"

        await send_output(
            update.message, user_id,
            full_output or "(אין פלט)",
            header=header,
            suffix=f"\n📁 `{cwd}`",
        )

    USER_CWD[user_id] = cwd


# ══════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise ValueError("חסר TELEGRAM_BOT_TOKEN ב-.env")
    if not AUTHORIZED_IDS:
        raise ValueError("חסר AUTHORIZED_USER_IDS ב-.env")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("exit", cmd_exit))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(callback_settings, pattern=r"^set_max:"))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    log(f"🤖 הבוט רץ... משתמשים מורשים: {AUTHORIZED_IDS}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling()


if __name__ == "__main__":
    main()