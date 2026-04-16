import os
import re
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

# נתיב עבודה נוכחי לכל משתמש (למצב non-interactive)
USER_CWD: dict[int, str] = {}
DEFAULT_CWD = os.getcwd()

CMD_TIMEOUT = 30          # שניות לפקודות רגילות
OUTPUT_INITIAL_WAIT = 2.0  # המתנה ראשונית לפלט מ-session
OUTPUT_IDLE_WAIT = 0.4     # המתנה אחרי קריאה מוצלחת

# shells שנחשבים אינטראקטיביים — פותחים session מתמשך
INTERACTIVE_SHELLS = {
    "powershell", "powershell.exe",
    "cmd", "cmd.exe",
    "python", "python3", "python.exe",
    "node", "node.exe",
    "wsl", "bash", "sh",
    "ftp", "sftp", "telnet",
}


def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_IDS


def log(msg: str):
    print(msg, flush=True)


def get_cwd(user_id: int) -> str:
    return USER_CWD.get(user_id, DEFAULT_CWD)


# ══════════════════════════════════════════════
#  ShellSession — process מתמשך לכל משתמש
# ══════════════════════════════════════════════

class ShellSession:
    def __init__(self, proc: asyncio.subprocess.Process, shell_name: str):
        self.proc = proc
        self.shell_name = shell_name

    def is_alive(self) -> bool:
        return self.proc.returncode is None

    async def send(self, text: str) -> str:
        """שולח שורה ל-stdin ומחזיר את הפלט שהתקבל"""
        try:
            self.proc.stdin.write((text + "\n").encode("utf-8", errors="replace"))
            await self.proc.stdin.drain()
        except Exception as e:
            return f"[שגיאה בשליחה: {e}]"
        return await self._read_output()

    async def _read_output(self) -> str:
        """קורא פלט עד שאין תנועה"""
        output = b""
        timeout = OUTPUT_INITIAL_WAIT
        while True:
            try:
                chunk = await asyncio.wait_for(
                    self.proc.stdout.read(4096),
                    timeout=timeout,
                )
                if not chunk:
                    break
                output += chunk
                timeout = OUTPUT_IDLE_WAIT  # אחרי קריאה ראשונה — פחות סבלנות
            except asyncio.TimeoutError:
                break
        return output.decode("utf-8", errors="replace")

    async def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass


# sessions פעילים: user_id -> ShellSession
USER_SESSIONS: dict[int, ShellSession] = {}


async def start_session(shell_cmd: str, cwd: str) -> ShellSession:
    """פותח process אינטראקטיבי חדש"""
    proc = await asyncio.create_subprocess_exec(
        shell_cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # ערבוב stderr לתוך stdout
        cwd=cwd,
    )
    return ShellSession(proc, shell_cmd)


# ══════════════════════════════════════════════
#  Handlers
# ══════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log(f"[IN ] {user.id} ({user.username}): /start")

    if not is_authorized(user.id):
        await update.message.reply_text("⛔ אין לך הרשאה להשתמש בבוט זה.")
        return

    cwd = get_cwd(user.id)
    session_info = ""
    if user.id in USER_SESSIONS and USER_SESSIONS[user.id].is_alive():
        session_info = f"\n🔗 Session פעיל: `{USER_SESSIONS[user.id].shell_name}`"

    reply = (
        f"👋 שלום! שלח פקודה והיא תרוץ על המחשב שלך.\n"
        f"📁 נתיב נוכחי: `{cwd}`"
        f"{session_info}\n\n"
        f"כדי לסגור session אינטראקטיבי שלח `/exit`"
    )
    await update.message.reply_text(reply, parse_mode="Markdown")


async def cmd_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """סוגר session אינטראקטיבי פעיל"""
    user = update.effective_user
    if not is_authorized(user.id):
        return

    session = USER_SESSIONS.pop(user.id, None)
    if session:
        await session.kill()
        await update.message.reply_text(
            f"🔌 Session `{session.shell_name}` נסגר.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("אין session פעיל לסגירה.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    log(f"[IN ] {user.id} ({user.username}): {text}")

    if not is_authorized(user.id):
        await update.message.reply_text("⛔ אין לך הרשאה.")
        return

    if not text or text.startswith("/"):
        return

    # ---- אם יש session פעיל — מעביר ישירות ----
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
            await forward_to_session(update, user.id, session, text)
            return

    # ---- בדוק אם הפקודה היא פתיחת shell ----
    first_token = text.strip().split()[0].lower()
    if first_token in INTERACTIVE_SHELLS:
        await open_interactive_session(update, user.id, text.strip())
        return

    # ---- פקודות רגילות (תומך בריבוי שורות) ----
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    await execute_lines(update, user.id, lines)


async def forward_to_session(update: Update, user_id: int, session: ShellSession, text: str):
    """מעביר הודעה ל-session אינטראקטיבי"""
    log(f"[SESSION:{session.shell_name}] <- {text}")
    output = await session.send(text)
    log(f"[SESSION:{session.shell_name}] -> {output!r}")

    if not session.is_alive():
        del USER_SESSIONS[user_id]
        suffix = "\n\n🔌 Session נסגר (process יצא)."
    else:
        suffix = ""

    reply = f"```\n{output[:3800] if output else '(אין פלט)'}\n```{suffix}"
    await update.message.reply_text(reply, parse_mode="Markdown")


async def open_interactive_session(update: Update, user_id: int, shell_cmd: str):
    """פותח session אינטראקטיבי חדש"""
    cwd = get_cwd(user_id)

    # סגור session קיים אם יש
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

        # קרא פלט פתיחה (banner, prompt וכו')
        opening = await session._read_output()
        if opening.strip():
            await update.message.reply_text(
                f"```\n{opening[:3800]}\n```", parse_mode="Markdown"
            )
    except Exception as e:
        log(f"[ERROR] {repr(e)}")
        await update.message.reply_text(
            f"💥 שגיאה בפתיחת session: `{e}`", parse_mode="Markdown"
        )


# ══════════════════════════════════════════════
#  פקודות רגילות (non-interactive)
# ══════════════════════════════════════════════

async def run_command(cmd: str, cwd: str) -> tuple[str, str, int | None]:
    proc = await asyncio.create_subprocess_exec(
        "cmd.exe", "/c", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CMD_TIMEOUT)
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            proc.returncode,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return ("", f"⏱️ timeout ({CMD_TIMEOUT}s)", None)


async def execute_lines(update: Update, user_id: int, lines: list[str]):
    cwd = get_cwd(user_id)
    results = []

    for cmd in lines:
        log(f"[CMD] cwd={cwd} | {cmd}")

        # CD
        cd_match = re.match(r'^cd\s+(.*)', cmd, re.IGNORECASE)
        if cd_match:
            target = cd_match.group(1).strip().strip('"').strip("'")
            new_path = os.path.normpath(os.path.join(cwd, target))
            if os.path.isdir(new_path):
                cwd = new_path
                USER_CWD[user_id] = cwd
                results.append(f"📁 `cd` → `{cwd}`")
            else:
                results.append(f"❌ `cd {target}` — נתיב לא קיים: `{new_path}`")
            continue

        stdout_text, stderr_text, returncode = await run_command(cmd, cwd)

        if not stdout_text and not stderr_text:
            stdout_text = "(אין פלט)"

        exit_emoji = "⏱️" if returncode is None else ("✅" if returncode == 0 else "❌")
        exit_line = "Timeout" if returncode is None else f"Exit: `{returncode}`"

        block = f"{exit_emoji} `{cmd}` — {exit_line}\n"
        if stdout_text:
            block += f"```\n{stdout_text[:1500]}\n```\n"
        if stderr_text:
            block += f"⚠️ STDERR:\n```\n{stderr_text[:500]}\n```"
        results.append(block)

    full_reply = "\n".join(results)
    full_reply += f"\n\n📁 `{cwd}`"

    if len(full_reply) > 4000:
        full_reply = full_reply[:4000] + "\n...(קוצץ)"

    await update.message.reply_text(full_reply, parse_mode="Markdown")


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
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    log(f"🤖 הבוט רץ... משתמשים מורשים: {AUTHORIZED_IDS}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app.run_polling()


if __name__ == "__main__":
    main()