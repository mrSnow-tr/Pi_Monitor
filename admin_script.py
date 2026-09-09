#!/usr/bin/env python3
"""
admin_script.py — Telegram admin bot for Raspberry Pi 3.
v2.0 — adds persistent PTY shell subsystem.

Location : /home/Admin/admin_script.py
Run with : /home/Admin/.venv/bin/python /home/Admin/admin_script.py

Features
--------
• Monitoring  : /status /temp /cpu /memory /disk /uptime /network /services
• Service ctrl: /start_manager /stop_manager /restart_manager
• Logs        : /logs  (ftp-manager journal)
                /botlogs (daily admin bot log)
• System      : /reboot /shutdown  (both require inline-keyboard confirmation)
• Boot notify : Automatic on each startup; reboot/shutdown/power-loss aware
• Shell mode  : /shell_start — persistent PTY bash sessions via Telegram
• Security    : every message and callback verified against ADMIN_CHAT_IDS
• No shell=True, no arbitrary command execution, no hard-coded sudo password.

Security note
-------------
BOT_TOKEN is read from the TELEGRAM_BOT_TOKEN environment variable first.
If that variable is absent the hard-coded fallback below is used.  For any
production deployment set the environment variable so the token is not stored
in this source file.  Never commit a live token to version control.
"""

import asyncio
import collections
import datetime
import fcntl
import html
import json
import logging
import logging.handlers
import os
import pty
import pwd
import re
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION — edit these values before first run
# ═══════════════════════════════════════════════════════════════════════════════

# ⚠️  SECURITY: set TELEGRAM_BOT_TOKEN in the environment instead of storing
#     the token here.  The env-var takes precedence automatically.
BOT_TOKEN: str = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "<your-bot-token-here>",  # fallback — move to env var
)

# Authorised Telegram chat IDs.  Add more as needed:
ADMIN_CHAT_IDS: set[int] = {
    123456789, # Your Actual Chat Id 
    # 987654321,
}

# Services the bot may start / stop / restart (strict allowlist):
ALLOWED_SERVICES: set[str] = {
    "ftp-manager.service",
    "pihole-FTL.service", 
}

# Services shown in /services and /status:
MONITORED_SERVICES: list[str] = [
    "ftp-manager.service",
    "pihole-FTL.service",
    "insta-uploader.service",
]

# The single service managed by /start_manager, /stop_manager, /restart_manager:
MANAGED_SERVICE = "ftp-manager.service"

BASE_DIR = Path(__file__).resolve().parent
# Logging:
LOG_DIR            = BASE_DIR / "logs"
LOG_RETENTION_DAYS = 7     # Keep today + this many previous daily log files
LOG_LINES          = 40    # Lines returned by /logs and /botlogs

# Tiny JSON file used to detect reboot vs shutdown vs power-loss on next boot:
STATE_FILE = BASE_DIR / ".admin_state.json"

# ─── Shell subsystem ──────────────────────────────────────────────────────────

# Maximum simultaneous open shell sessions per Telegram chat.
# Each shell uses one PTY pair, one bash process, and one asyncio Task.
MAX_SHELLS: int = 8

# Lines stored in each shell's in-memory output history (bounded deque).
MAX_OUTPUT_HISTORY_LINES: int = 1000

# Incomplete-line buffer cap (PTY reads can split a logical line).
# Overflow is force-committed as history lines so the buffer stays bounded.
MAX_PARTIAL_LINE_CHARS: int = 4096

# Lines shown automatically when switching to a shell (/shell_one, etc.).
DEFAULT_PREVIOUS_LINES: int = 10

# Hard ceiling on /shell_previous_N requests.
MAX_PREVIOUS_LINES: int = 500

# Seconds between flushing accumulated PTY output to Telegram.
# Lower → quicker feedback; higher → fewer messages (easier on Pi 3 CPU).
OUTPUT_BATCH_INTERVAL: float = 0.3

# Maximum characters per Telegram shell-output message (Telegram cap: 4 096).
MAX_TELEGRAM_CHUNK: int = 3800

# English ordinal-word → shell-ID mapping.
# Extend BOTH dicts here if MAX_SHELLS is raised above 8.
_SHELL_WORD_TO_NUM: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
}
_SHELL_NUM_TO_WORD: dict[int, str] = {v: k for k, v in _SHELL_WORD_TO_NUM.items()}

# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGING  — daily dated files, no polling spam
# ═══════════════════════════════════════════════════════════════════════════════

class _DailyFileHandler(logging.FileHandler):
    """
    Writes to  /home/Admin/logs/YYYY-MM-DD.log .
    Checks the date on every emit and switches to a new dated file automatically
    at midnight — no background thread, no RAM buffer, no single unlimited file.
    Triggers log-file cleanup on each daily rollover.
    """

    def __init__(self, log_dir: Path, encoding: str = "utf-8") -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir      = log_dir
        self._current_date = datetime.date.today()
        super().__init__(
            filename=str(log_dir / f"{self._current_date}.log"),
            encoding=encoding,
        )

    def emit(self, record: logging.LogRecord) -> None:
        today = datetime.date.today()
        if today != self._current_date:
            self._current_date = today
            self.close()
            self.baseFilename  = os.path.abspath(
                str(self._log_dir / f"{today}.log")
            )
            self.stream        = self._open()
            _delete_old_logs(self._log_dir)   # clean up on rollover
        super().emit(record)


def _delete_old_logs(log_dir: Path) -> None:
    """Remove dated log files older than LOG_RETENTION_DAYS from log_dir."""
    cutoff = datetime.date.today() - datetime.timedelta(days=LOG_RETENTION_DAYS)
    for p in log_dir.glob("*.log"):
        try:
            file_date = datetime.date.fromisoformat(p.stem)
            if file_date < cutoff:
                p.unlink()
                logging.getLogger(__name__).info(
                    "Deleted old log file: %s", p.name
                )
        except (ValueError, OSError):
            pass    # skip files whose names aren't plain dates


def _setup_logging() -> None:
    """
    Configure logging:
    • Daily rotating files under LOG_DIR (one YYYY-MM-DD.log per day).
    • Stream handler so systemd/journald also captures output.
    • httpx / httpcore / apscheduler suppressed to WARNING to eliminate the
      routine "POST .../getUpdates 200 OK" lines that polluted the old log.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = _DailyFileHandler(LOG_DIR)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # Suppress routine Telegram/HTTP polling noise:
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Clean up any old log files present from previous runs:
    _delete_old_logs(LOG_DIR)


_setup_logging()
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  PERSISTENT STATE  — tiny JSON file for boot-event detection
# ═══════════════════════════════════════════════════════════════════════════════

def write_state(action: str) -> None:
    """
    Persist the upcoming action before issuing reboot or shutdown so the next
    boot notification can say the right thing.
    """
    try:
        STATE_FILE.write_text(json.dumps({"previous_action": action}))
    except Exception as exc:
        logger.error("write_state(%r): %s", action, exc)


def read_state() -> dict:
    """Return the state dict, or {} if the file is absent or unreadable."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception as exc:
        logger.error("read_state: %s", exc)
    return {}


def clear_state() -> None:
    """Delete the state file after reading it on boot."""
    try:
        STATE_FILE.unlink(missing_ok=True)
    except Exception as exc:
        logger.error("clear_state: %s", exc)

# ═══════════════════════════════════════════════════════════════════════════════
#  SECURITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_authorized(update: Update) -> bool:
    """Return True only when the sender's chat ID is in ADMIN_CHAT_IDS."""
    chat = update.effective_chat
    return chat is not None and int(chat.id) in ADMIN_CHAT_IDS


async def deny(update: Update) -> None:
    """Reject an unauthorised request, log the attempt, optionally reply."""
    uid = getattr(update.effective_user, "id", "?")
    cid = getattr(update.effective_chat,  "id", "?")
    logger.warning("Unauthorized access attempt: user_id=%s chat_id=%s", uid, cid)
    if update.message:
        await update.message.reply_text("Unauthorized.")
    elif update.callback_query:
        await update.callback_query.answer("Unauthorized.", show_alert=True)

# ═══════════════════════════════════════════════════════════════════════════════
#  SYSTEM HELPERS  (blocking — always call via asyncio.to_thread())
# ═══════════════════════════════════════════════════════════════════════════════

def _temperature() -> str:
    """Read CPU temperature from sysfs (millidegrees → °C)."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return f"{int(raw) / 1000:.1f}°C"
    except Exception as exc:
        logger.error("temperature: %s", exc)
        return "N/A"


def _cpu_usage(interval: float = 0.5) -> str:
    """Calculate CPU usage by reading /proc/stat twice with a short sleep."""
    def _stat() -> tuple[int, int]:
        with open("/proc/stat") as fh:
            vals = list(map(int, fh.readline().split()[1:]))
        return vals[3], sum(vals)   # (idle_jiffies, total_jiffies)

    try:
        i1, t1 = _stat()
        time.sleep(interval)
        i2, t2 = _stat()
        dt = t2 - t1
        return f"{100 * (1 - (i2 - i1) / dt):.0f}%" if dt else "0%"
    except Exception as exc:
        logger.error("cpu_usage: %s", exc)
        return "N/A"


def _memory() -> tuple[int, int]:
    """Parse /proc/meminfo. Returns (used_mb, total_mb)."""
    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, *rest = line.split()
                mem[k.rstrip(":")] = int(rest[0])
        total = mem["MemTotal"]
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        return (total - avail) // 1024, total // 1024
    except Exception as exc:
        logger.error("memory: %s", exc)
        return 0, 0


def _disk() -> tuple[float, float]:
    """Returns (used_gb, total_gb) for the root filesystem via shutil."""
    try:
        u = shutil.disk_usage("/")
        return u.used / 1024 ** 3, u.total / 1024 ** 3
    except Exception as exc:
        logger.error("disk: %s", exc)
        return 0.0, 0.0


def _uptime() -> str:
    """Human-readable uptime from /proc/uptime."""
    try:
        secs = int(float(Path("/proc/uptime").read_text().split()[0]))
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        parts: list[str] = []
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        return " ".join(parts) or f"{s}s"
    except Exception as exc:
        logger.error("uptime: %s", exc)
        return "N/A"


def _ip_address() -> str:
    """
    Determine the primary LAN IP via a non-routable UDP socket.
    No packets are actually sent; this just lets the kernel pick the
    source address for the default route.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "N/A"


def _default_iface() -> Optional[str]:
    """Detect the default-route interface from /proc/net/route."""
    try:
        with open("/proc/net/route") as fh:
            next(fh)                            # skip header row
            for line in fh:
                cols = line.split()
                if cols[1] == "00000000":       # Destination == 0.0.0.0
                    return cols[0]              # e.g. "eth0", "wlan0", "end0"
    except Exception as exc:
        logger.error("default_iface: %s", exc)
    return None


def _network() -> str:
    """One-second RX/TX sample on the default-route interface."""
    iface = _default_iface()
    if not iface:
        return "No default interface found"
    base = Path(f"/sys/class/net/{iface}/statistics")
    try:
        def _read() -> tuple[int, int]:
            return (
                int((base / "rx_bytes").read_text()),
                int((base / "tx_bytes").read_text()),
            )
        rx1, tx1 = _read()
        time.sleep(1)
        rx2, tx2 = _read()
        return (
            f"{iface}: ↓ {(rx2 - rx1) / 1024:.1f} KB/s  "
            f"↑ {(tx2 - tx1) / 1024:.1f} KB/s"
        )
    except Exception as exc:
        logger.error("network: %s", exc)
        return "N/A"


def _service_status(name: str) -> str:
    """Return a single-line emoji status for a systemd service (no sudo needed)."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5,
        )
        s = r.stdout.strip()
        if s == "active":
            return f"🟢 {name}: RUNNING"
        return f"🔴 {name}: {s.upper()}"
    except Exception as exc:
        logger.error("service_status(%s): %s", name, exc)
        return f"⚠️ {name}: ERROR"


def _run_systemctl(action: str, service: str) -> str:
    """
    Execute  sudo /usr/bin/systemctl <action> <service>  safely.

    Both arguments come from hard-coded constants — never from user input.
    • action  is validated against an explicit set.
    • service is validated against ALLOWED_SERVICES.
    • shell=False (default when passing a list to subprocess).
    • No sudo password anywhere in this file.
    """
    assert action  in {"start", "stop", "restart"},  f"Forbidden action: {action!r}"
    assert service in ALLOWED_SERVICES,              f"Service not in allowlist: {service!r}"
    try:
        r = subprocess.run(
            ["sudo", "/usr/bin/systemctl", action, service],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            logger.info("systemctl %s %s: OK", action, service)
            return f"✅ {action.capitalize()} {service}: OK"
        err = (r.stderr or r.stdout).strip()
        logger.error("systemctl %s %s rc=%d: %s", action, service, r.returncode, err)
        return f"❌ Failed (rc={r.returncode}): {err}"
    except subprocess.TimeoutExpired:
        logger.error("systemctl %s %s: timeout", action, service)
        return "❌ Command timed out."
    except Exception as exc:
        logger.error("systemctl %s %s: %s", action, service, exc)
        return f"❌ Error: {exc}"


def _journal(unit: str, n: int = LOG_LINES) -> str:
    """
    Retrieve recent journal entries for a systemd unit.
    Both arguments come from hard-coded constants — no user input reaches here.
    Requires the bot user to be in the systemd-journal group (see README).
    """
    try:
        r = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(n),
             "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip() or "(no entries found)"
    except Exception as exc:
        logger.error("journalctl(%s): %s", unit, exc)
        return f"Error reading journal: {exc}"


def _tail_file(path: str, n: int) -> str:
    """Return the last n lines of a text file."""
    try:
        with open(path) as fh:
            lines = fh.readlines()
        return "".join(lines[-n:]).strip() or "(log file is empty)"
    except Exception as exc:
        logger.error("tail(%s): %s", path, exc)
        return f"Error reading file: {exc}"


def _tail_botlog(n: int) -> str:
    """Return the last n lines of today's admin bot log file."""
    today    = datetime.date.today().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"{today}.log"
    if not log_file.exists():
        return f"(No log file found for today: {log_file.name})"
    return _tail_file(str(log_file), n)

# ═══════════════════════════════════════════════════════════════════════════════
#  BOOT NOTIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

async def send_boot_notification(bot: Bot) -> None:
    """
    Read the state file to determine why the Pi is booting, then send the
    correct notification to all authorised chat IDs and clear the state.

    This is called from post_init() — after the Telegram connection is usable
    but before polling starts.  Network availability is guaranteed by systemd's
    After=network-online.target in the service unit.
    """
    state = read_state()
    prev  = state.get("previous_action", "")
    clear_state()

    if prev == "reboot":
        header = "🟢 Raspberry Pi rebooted and is online again."
        logger.info("Boot notification: previous action was REBOOT")
    elif prev == "shutdown":
        header = "🟢 Raspberry Pi is online again (after shutdown)."
        logger.info("Boot notification: previous action was SHUTDOWN")
    else:
        # No state → cold boot or unexpected power loss; never claim shutdown.
        header = "🟢 Raspberry Pi booted successfully."
        logger.info("Boot notification: no previous action recorded (cold boot or power loss)")

    hostname   = socket.gethostname()
    now        = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    uptime_str = _uptime()
    temp_str   = _temperature()
    ip_str     = _ip_address()

    msg = (
        f"{header}\n\n"
        f"🏠 <b>Host:</b> {html.escape(hostname)}\n"
        f"🕐 <b>Time:</b> {html.escape(now)}\n"
        f"⏱ <b>Uptime:</b> {html.escape(uptime_str)}\n"
        f"🌡 <b>Temp:</b> {html.escape(temp_str)}\n"
        f"🌐 <b>IP:</b> {html.escape(ip_str)}"
    )

    for chat_id in ADMIN_CHAT_IDS:
        try:
            await bot.send_message(
                chat_id    = chat_id,
                text       = msg,
                parse_mode = ParseMode.HTML,
            )
        except Exception as exc:
            logger.error("Boot notification failed for chat_id=%s: %s", chat_id, exc)

# ═══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_MSG_LIMIT = 4000   # Telegram hard cap is 4096; stay safely under it


def _trim(text: str) -> str:
    return text if len(text) <= _MSG_LIMIT else text[:_MSG_LIMIT] + "\n…(truncated)"


async def _send_pre(update: Update, text: str) -> None:
    """Send monospaced pre-formatted output, HTML-escaped and length-capped."""
    await update.message.reply_text(
        f"<pre>{html.escape(_trim(text))}</pre>",
        parse_mode=ParseMode.HTML,
    )


# ─── Shell output helpers ─────────────────────────────────────────────────────

# Matches ANSI/VT100 escape sequences produced by bash and common utilities.
_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"          # CSI sequences  (colors, cursor, etc.)
    r"|[@-Z\\-_]"                    # 2-char ESC sequences
    r"|\][^\x07]*(?:\x07|\x1b\\)"   # OSC sequences  (window title, etc.)
    r")"
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI/VT100 terminal escape sequences from *text*."""
    return _ANSI_RE.sub("", text)


def _clean_output(raw: str) -> str:
    """Strip ANSI escapes and normalize CR+LF / lone CR to LF."""
    text = _strip_ansi(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _chunk_text(text: str, max_len: int) -> list[str]:
    """
    Split *text* into chunks of at most *max_len* characters,
    preserving line boundaries where possible.
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        # +1 accounts for the newline between lines
        needed = len(line) + (1 if current else 0)
        if current_len + needed > max_len and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += needed
    if current:
        chunks.append("\n".join(current))
    return chunks


def _shorten_path(path: str) -> str:
    """Replace the home-directory prefix with ~ for display."""
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


async def _send_shell_output(bot: Bot, chat_id: int, text: str) -> None:
    """
    Send *text* as monospaced Telegram output, split into chunks if necessary.
    All text is HTML-escaped before wrapping in <pre> tags so shell output can
    never inject Telegram HTML markup.
    """
    text = text.strip("\n")
    if not text.strip():
        return
    for chunk in _chunk_text(text, MAX_TELEGRAM_CHUNK):
        if not chunk.strip():
            continue
        try:
            await bot.send_message(
                chat_id    = chat_id,
                text       = f"<pre>{html.escape(chunk)}</pre>",
                parse_mode = ParseMode.HTML,
            )
        except Exception as exc:
            logger.error("Shell output send failed chat_id=%d: %s", chat_id, exc)


async def _send_shell_chunks(update: Update, text: str) -> None:
    """
    Like _send_shell_output but replies to a specific update (used for
    /shell_previous_N where we want to reply in the correct thread).
    """
    text = text.strip("\n")
    if not text.strip():
        await update.message.reply_text("ℹ️ (no output)")
        return
    for chunk in _chunk_text(text, MAX_TELEGRAM_CHUNK):
        if not chunk.strip():
            continue
        try:
            await update.message.reply_text(
                f"<pre>{html.escape(chunk)}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as exc:
            logger.error("Shell chunk send failed: %s", exc)

# ═══════════════════════════════════════════════════════════════════════════════
#  SHELL INFRASTRUCTURE
# ═══════════════════════════════════════════════════════════════════════════════

def _terminate_process_nowait(pid: int) -> None:
    """
    Send SIGTERM to *pid* and attempt a non-blocking reap.
    Does not wait — use for ordinary shell close from a handler.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _terminate_process_blocking(pid: int) -> None:
    """
    SIGTERM → wait up to 2 s → SIGKILL.  Blocking — run via asyncio.to_thread.
    Used during bot shutdown and individual shell close where a clean join
    matters.  If the child is already a zombie (e.g. after the PTY was
    closed), it is reaped immediately without waiting.
    """
    def _reap_nowait() -> bool:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            return waited == pid
        except ChildProcessError:
            return True
        except OSError:
            return False

    if _reap_nowait():
        return

    # Interactive bash ignores SIGTERM; SIGHUP is the normal tty-close signal.
    try:
        os.kill(pid, signal.SIGHUP)
    except (ProcessLookupError, OSError):
        pass
    if _reap_nowait():
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _reap_nowait()
        return
    except OSError as exc:
        logger.warning("SIGTERM pid=%d: %s", pid, exc)
        _reap_nowait()
        return

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _reap_nowait():
            return
        time.sleep(0.1)

    # Escalate
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, OSError):
        pass


class ShellSession:
    """
    A single persistent bash session backed by a POSIX pseudo-terminal (PTY).

    Lifecycle
    ---------
    1.  session = ShellSession(shell_id, chat_id)
    2.  session.spawn()                           # fork + exec /bin/bash
    3.  session.start_reader(loop, out_cb, die_cb)  # register asyncio reader
    4.  session.send_input("ls -la")             # send a command
    5.  session.close()                           # terminate gracefully

    The PTY master fd is set to O_NONBLOCK so reads never stall the event loop.
    Output is collected via loop.add_reader() and flushed to Telegram every
    OUTPUT_BATCH_INTERVAL seconds by a background Task.
    """

    def __init__(self, shell_id: int, chat_id: int) -> None:
        self.shell_id:       int                      = shell_id
        self.chat_id:        int                      = chat_id
        self.pid:            Optional[int]            = None
        self.pty_master_fd:  Optional[int]            = None
        self.output_history: collections.deque        = collections.deque(
            maxlen=MAX_OUTPUT_HISTORY_LINES
        )
        self.created_at:     datetime.datetime        = datetime.datetime.now()
        self.closed:         bool                     = False

        # Async plumbing — populated by start_reader()
        self._loop:               Optional[asyncio.AbstractEventLoop] = None
        self._output_callback                                         = None
        self._death_callback                                          = None
        self._pending_lines:      list[str]                           = []
        self._partial_line:       str                                 = ""
        self._flush_task:         Optional[asyncio.Task]              = None
        self._reap_task:          Optional[asyncio.Task]              = None
        self._reader_registered:  bool                                = False

    # ── Spawn ──────────────────────────────────────────────────────────────────

    def spawn(self) -> None:
        """
        Fork a child process with a PTY and exec /bin/bash.

        The child uses --norc --noprofile for a clean, predictable environment.
        PS1 is set to 'user@host:dir$ ' so prompts look like a real terminal.
        HISTFILE=/dev/null prevents writes to the SD card's bash history file.
        TERM=dumb reduces the number of ANSI sequences that programs emit.

        Raises OSError or PermissionError if the fork/exec fails.
        """
        env = os.environ.copy()
        env["TERM"]     = "dumb"
        env["PS1"]      = r"\u@\h:\w\$ "
        env["HISTFILE"] = "/dev/null"
        env["HISTSIZE"] = "0"

        pid, fd = pty.fork()   # blocking but microsecond-fast

        if pid == 0:
            # ── Child ─────────────────────────────────────────────────────────
            # After fork() only this thread exists in the child.
            # exec() replaces the image immediately — safe even in a
            # multithreaded parent process.
            try:
                os.execve("/bin/bash",
                          ["/bin/bash", "--norc", "--noprofile"],
                          env)
            except Exception:
                os._exit(127)

        # ── Parent ────────────────────────────────────────────────────────────
        self.pid            = pid
        self.pty_master_fd  = fd

        # Make the master fd non-blocking so reads in the event loop
        # never stall even on spurious wakeups.
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        logger.info("Shell %d created  pid=%d  chat_id=%d",
                    self.shell_id, pid, self.chat_id)

    # ── Async reader ───────────────────────────────────────────────────────────

    def start_reader(
        self,
        loop:             asyncio.AbstractEventLoop,
        output_callback,  # async (text: str) -> None
        death_callback,   # async (session: ShellSession) -> None
    ) -> None:
        """
        Register a reader callback on the PTY fd and start the flush task.
        Must be called from within the running event loop.
        """
        self._loop            = loop
        self._output_callback = output_callback
        self._death_callback  = death_callback
        self._reader_registered = True
        loop.add_reader(self.pty_master_fd, self._on_pty_readable)
        self._flush_task = loop.create_task(
            self._flush_loop(), name=f"shell-flush-{self.chat_id}-{self.shell_id}"
        )

    def _on_pty_readable(self) -> None:
        """
        Sync callback invoked by the event loop when the PTY master fd is
        readable.  Reads available bytes, cleans ANSI sequences, stores lines
        in history, and appends to the pending buffer for the flush task.
        All operations are in the event-loop thread — no locking needed.
        """
        if self.closed or self.pty_master_fd is None:
            return
        try:
            data = os.read(self.pty_master_fd, 4096)
        except BlockingIOError:
            return   # spurious wakeup — nothing to read
        except OSError:
            # EIO: slave PTY closed because bash exited
            self._handle_death()
            return

        if not data:
            self._handle_death()
            return

        text = _clean_output(data.decode("utf-8", errors="replace"))
        self._ingest_history_text(text)
        self._pending_lines.append(text)

    def _ingest_history_text(self, text: str) -> None:
        """
        Feed cleaned PTY text into output_history as complete logical lines.

        PTY reads are arbitrary chunks, so an incomplete trailing fragment is
        kept in _partial_line and prepended to the next read.  A fragment is
        only committed to history when a newline arrives (or when the buffer
        is force-capped / flushed on close).
        """
        if not text:
            return
        combined = self._partial_line + text
        if "\n" not in combined:
            self._partial_line = combined
            self._cap_partial_line()
            return
        if combined.endswith("\n"):
            complete = combined.split("\n")[:-1]
            self._partial_line = ""
        else:
            parts = combined.split("\n")
            complete = parts[:-1]
            self._partial_line = parts[-1]
            self._cap_partial_line()
        for line in complete:
            self.output_history.append(line)

    def _cap_partial_line(self) -> None:
        """Force-commit overflow so _partial_line cannot grow without bound."""
        while len(self._partial_line) > MAX_PARTIAL_LINE_CHARS:
            self.output_history.append(
                self._partial_line[:MAX_PARTIAL_LINE_CHARS]
            )
            self._partial_line = self._partial_line[MAX_PARTIAL_LINE_CHARS:]

    def _flush_partial_history(self) -> None:
        """Commit any leftover incomplete line (EOF / close / death)."""
        if self._partial_line:
            self.output_history.append(self._partial_line)
            self._partial_line = ""

    def _handle_death(self) -> None:
        """
        Called when the PTY signals that the shell process has exited.
        Marks the session closed, removes the fd reader, reaps the zombie,
        and schedules the death-notification coroutine.
        """
        if self.closed:
            return
        self.closed = True

        self._flush_partial_history()

        if self._reader_registered and self.pty_master_fd is not None \
                and self._loop is not None:
            try:
                self._loop.remove_reader(self.pty_master_fd)
            except Exception:
                pass
            self._reader_registered = False

        if self.pty_master_fd is not None:
            try:
                os.close(self.pty_master_fd)
            except OSError:
                pass
            self.pty_master_fd = None

        # Reap the child.  EIO/EOF means it has already exited, so waitpid
        # should return immediately; fall back to a background reap if not.
        if self.pid is not None:
            reaped = False
            try:
                waited, _ = os.waitpid(self.pid, os.WNOHANG)
                reaped = waited == self.pid
            except (ChildProcessError, OSError):
                reaped = True
            if not reaped:
                self._begin_reap(self.pid)

        logger.info("Shell %d exited  pid=%s  chat_id=%d",
                    self.shell_id, self.pid, self.chat_id)

        if self._loop is not None and self._death_callback is not None:
            self._loop.create_task(
                self._death_callback(self),
                name=f"shell-death-{self.chat_id}-{self.shell_id}",
            )

    async def _flush_loop(self) -> None:
        """
        Task that wakes every OUTPUT_BATCH_INTERVAL seconds and sends any
        accumulated PTY output to Telegram.  Exits naturally when self.closed
        is True, or on CancelledError (bot shutdown).
        """
        try:
            while not self.closed:
                await asyncio.sleep(OUTPUT_BATCH_INTERVAL)
                await self._do_flush()
        except asyncio.CancelledError:
            pass
        # Final flush: send any data that arrived just before close/cancel
        await self._do_flush()

    async def _do_flush(self) -> None:
        """Drain _pending_lines and send to Telegram if there is anything."""
        if not self._pending_lines:
            return
        chunk = "".join(self._pending_lines)
        self._pending_lines.clear()
        chunk = chunk.strip("\n")
        if chunk.strip() and self._output_callback is not None:
            try:
                await self._output_callback(chunk)
            except Exception as exc:
                logger.error("Shell %d output callback: %s", self.shell_id, exc)

    # ── Input / signals ────────────────────────────────────────────────────────

    def send_input(self, text: str) -> None:
        """Write *text* + newline to the shell's stdin via the PTY."""
        if self.pty_master_fd is None or self.closed:
            raise RuntimeError(f"Shell {self.shell_id} is not running")
        line = text if text.endswith("\n") else text + "\n"
        os.write(self.pty_master_fd, line.encode("utf-8", errors="replace"))

    def send_interrupt(self) -> None:
        """Write Ctrl+C (0x03) to the PTY, interrupting the foreground process."""
        if self.pty_master_fd is None or self.closed:
            raise RuntimeError(f"Shell {self.shell_id} is not running")
        os.write(self.pty_master_fd, b"\x03")

    # ── Introspection ──────────────────────────────────────────────────────────

    def is_alive(self) -> bool:
        """
        Return True if the bash process is still running.

        A zombie is treated as dead: os.kill(pid, 0) would still succeed for
        a zombie, so we reap with waitpid(WNOHANG) and also inspect
        /proc/<pid>/stat for state 'Z'.
        """
        if self.closed or self.pid is None:
            return False
        # Reap if the child has already exited (also converts zombie → gone).
        try:
            waited, _ = os.waitpid(self.pid, os.WNOHANG)
            if waited == self.pid:
                self.closed = True
                return False
        except ChildProcessError:
            self.closed = True
            return False
        except OSError:
            pass
        # /proc state: 'Z' means zombie — not a healthy shell.
        try:
            with open(f"/proc/{self.pid}/stat") as fh:
                stat = fh.read()
            rp = stat.rfind(")")
            if rp != -1 and rp + 2 < len(stat) and stat[rp + 2] == "Z":
                try:
                    os.waitpid(self.pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                self.closed = True
                return False
            return True
        except FileNotFoundError:
            self.closed = True
            return False
        except OSError:
            try:
                os.kill(self.pid, 0)   # signal 0: existence check, no actual signal
                return True
            except ProcessLookupError:
                self.closed = True
                return False
            except PermissionError:
                return True   # exists but we can't signal (shouldn't happen for our child)

    def get_cwd(self) -> str:
        """
        Read the shell's current working directory from /proc/<pid>/cwd.
        This reflects bash's CWD (updated by 'cd'), not any subshell's.
        """
        if self.pid is None:
            return "?"
        try:
            return os.readlink(f"/proc/{self.pid}/cwd")
        except OSError:
            return "?"

    def get_user(self) -> str:
        """
        Username of the account the shell process is actually running as,
        taken from the process real UID in /proc.  Falls back to the bot's
        own UID if the process is gone.
        """
        uid = os.getuid()
        if self.pid is not None:
            try:
                with open(f"/proc/{self.pid}/status") as fh:
                    for line in fh:
                        if line.startswith("Uid:"):
                            uid = int(line.split()[1])  # real UID
                            break
            except (OSError, ValueError, IndexError):
                pass
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return str(uid)

    def get_host(self) -> str:
        return socket.gethostname().split(".")[0]

    def status_str(self) -> str:
        if self.closed:
            return "CLOSED"
        if self.is_alive():
            return "RUNNING"
        return "DEAD"

    def recent_output(self, n: int) -> str:
        """Return the last *n* lines from output_history as a single string."""
        lines = list(self.output_history)[-n:]
        return "\n".join(lines)

    # ── Close ──────────────────────────────────────────────────────────────────

    def close(self) -> None:
        """
        Terminate the shell process and clean up all resources.

        Marks the session closed immediately so no further commands are
        accepted, closes the PTY, then SIGTERM→wait→SIGKILL-reaps the
        child without blocking the event loop.  The shell ID is left in
        place (never reused).
        """
        if self.closed:
            return
        self.closed = True

        logger.info("Shell %d closing  pid=%s  chat_id=%d",
                    self.shell_id, self.pid, self.chat_id)

        self._flush_partial_history()

        # Remove the fd reader so no more _on_pty_readable calls arrive
        if self._reader_registered and self.pty_master_fd is not None \
                and self._loop is not None:
            try:
                self._loop.remove_reader(self.pty_master_fd)
            except Exception:
                pass
            self._reader_registered = False

        # Close the PTY master fd (typically delivers SIGHUP to the child)
        if self.pty_master_fd is not None:
            try:
                os.close(self.pty_master_fd)
            except OSError:
                pass
            self.pty_master_fd = None

        # Terminate and reap the bash process
        pid = self.pid
        if pid is not None:
            self._begin_reap(pid)

    def _begin_reap(self, pid: int) -> None:
        """SIGTERM/SIGKILL + waitpid *pid* without blocking the event loop."""
        loop = self._loop
        if loop is not None and loop.is_running():
            self._reap_task = loop.create_task(
                self._reap_async(pid),
                name=f"shell-reap-{self.chat_id}-{self.shell_id}",
            )
        else:
            _terminate_process_blocking(pid)
            if self.pid == pid:
                self.pid = None

    async def _reap_async(self, pid: int) -> None:
        try:
            await asyncio.to_thread(_terminate_process_blocking, pid)
        except Exception as exc:
            logger.warning("Shell %d reap pid=%d: %s", self.shell_id, pid, exc)
        if self.pid == pid:
            self.pid = None


class ShellManager:
    """
    Manages ShellSession objects partitioned by Telegram chat ID.

    Shell state is fully isolated per chat: Chat A cannot access Chat B's
    sessions.  Shell IDs are monotonically increasing per chat and are never
    reused within a bot process lifetime.

    Public interface
    ----------------
    is_shell_mode(chat_id)             → bool
    set_shell_mode(chat_id, active)    → None
    create_shell(chat_id, loop, bot)   → ShellSession | str (error)
    close_shell(chat_id, shell_id)     → str (status message)
    switch_shell(chat_id, shell_id)    → ShellSession | str (error)
    get_current_shell(chat_id)         → ShellSession | None
    get_shell(chat_id, shell_id)       → ShellSession | None
    list_shells(chat_id)               → dict[int, ShellSession]
    stats(chat_id)                     → (opened, closed_count)
    cleanup_all()                      → None  (call on bot shutdown)
    """

    def __init__(self) -> None:
        # {chat_id: {shell_id: ShellSession}}
        self._shells:  dict[int, dict[int, ShellSession]] = {}
        # {chat_id: current_shell_id | None}
        self._current: dict[int, Optional[int]]           = {}
        # {chat_id: bool}
        self._mode:    dict[int, bool]                    = {}
        # {chat_id: total_ever_opened}
        self._opened:  dict[int, int]                     = {}

    def _ensure_chat(self, chat_id: int) -> None:
        if chat_id not in self._shells:
            self._shells[chat_id]  = {}
            self._current[chat_id] = None
            self._opened[chat_id]  = 0

    # ── Shell mode ─────────────────────────────────────────────────────────────

    def is_shell_mode(self, chat_id: int) -> bool:
        return self._mode.get(chat_id, False)

    def set_shell_mode(self, chat_id: int, active: bool) -> None:
        self._mode[chat_id] = active

    # ── Create ─────────────────────────────────────────────────────────────────

    def create_shell(
        self,
        chat_id: int,
        loop:    asyncio.AbstractEventLoop,
        bot:     Bot,
    ) -> "ShellSession | str":
        """
        Spawn a new bash session for *chat_id*.
        Returns the ShellSession on success, or a user-facing error string.
        """
        self._ensure_chat(chat_id)
        active_count = sum(
            1 for s in self._shells[chat_id].values() if not s.closed
        )
        if active_count >= MAX_SHELLS:
            words = ", ".join(
                f"/shell_{_SHELL_NUM_TO_WORD[n]}_close"
                for n in sorted(_SHELL_NUM_TO_WORD)
                if n <= MAX_SHELLS
            )
            return (
                f"❌ Cannot create another shell.\n\n"
                f"Maximum open shells: {MAX_SHELLS}\n\n"
                f"Close an existing shell first:\n{words}"
            )

        # Find the lowest available slot in 1..MAX_SHELLS.
        # A slot is available when absent from the dict OR its session is closed.
        shell_id = next(
            (sid for sid in range(1, MAX_SHELLS + 1)
             if (sid not in self._shells[chat_id]
                 or self._shells[chat_id][sid].closed)),
            None,
        )
        if shell_id is None:
            # Safety net — should not be reached because the active_count check
            # above already rejects when all MAX_SHELLS slots are live.
            return (
                f"❌ Cannot create another shell.\n\n"
                f"Maximum open shells: {MAX_SHELLS}"
            )

        session = ShellSession(shell_id, chat_id)
        try:
            session.spawn()
        except Exception as exc:
            logger.error("Shell %d spawn failed chat_id=%d: %s",
                         shell_id, chat_id, exc)
            return f"❌ Failed to create shell: {html.escape(str(exc))}"

        self._opened[chat_id] += 1   # Count only shells that spawned successfully

        # Build per-shell async callbacks (closures over bot & chat_id)
        async def output_cb(text: str) -> None:
            await _send_shell_output(bot, chat_id, text)

        async def death_cb(s: ShellSession) -> None:
            await _on_shell_death(bot, chat_id, s)

        session.start_reader(loop, output_cb, death_cb)

        self._shells[chat_id][shell_id] = session
        self._current[chat_id]          = shell_id
        return session

    # ── Close ──────────────────────────────────────────────────────────────────

    def close_shell(self, chat_id: int, shell_id: int) -> str:
        """
        Close *shell_id* for *chat_id*.
        Returns a human-readable status message (may be multi-line).
        """
        self._ensure_chat(chat_id)
        shells = self._shells[chat_id]

        if shell_id not in shells:
            return f"❌ Shell {shell_id} does not exist."
        session = shells[shell_id]
        if session.closed:
            return f"❌ Shell {shell_id} is already closed."

        session.close()
        msg = f"🔒 Shell {shell_id} closed.\n\n"

        if self._current.get(chat_id) == shell_id:
            # Pick another live shell automatically
            alt = next(
                (sid for sid, s in shells.items()
                 if sid != shell_id and not s.closed),
                None,
            )
            self._current[chat_id] = alt
            if alt is not None:
                msg += f"Switched to Shell {alt}."
            else:
                msg += "No active shell remains.\nUse /shell_new to create one."

        return msg

    # ── Switch ─────────────────────────────────────────────────────────────────

    def switch_shell(self, chat_id: int, shell_id: int) -> "ShellSession | str":
        """
        Make *shell_id* the current shell for *chat_id*.
        Returns the ShellSession on success, or an error string.
        """
        self._ensure_chat(chat_id)
        shells = self._shells[chat_id]

        if shell_id not in shells:
            return f"❌ Shell {shell_id} does not exist."
        session = shells[shell_id]
        if session.closed or not session.is_alive():
            session.closed = True
            return f"❌ Shell {shell_id} is closed."

        self._current[chat_id] = shell_id
        return session

    # ── Accessors ──────────────────────────────────────────────────────────────

    def get_current_shell(self, chat_id: int) -> Optional[ShellSession]:
        self._ensure_chat(chat_id)
        sid = self._current.get(chat_id)
        if sid is None:
            return None
        return self._shells[chat_id].get(sid)

    def get_shell(self, chat_id: int, shell_id: int) -> Optional[ShellSession]:
        self._ensure_chat(chat_id)
        return self._shells.get(chat_id, {}).get(shell_id)

    def list_shells(self, chat_id: int) -> dict[int, ShellSession]:
        self._ensure_chat(chat_id)
        return dict(self._shells[chat_id])

    def stats(self, chat_id: int) -> tuple[int, int]:
        """Return (total_opened, total_closed) for *chat_id*."""
        self._ensure_chat(chat_id)
        opened = self._opened[chat_id]
        closed = sum(1 for s in self._shells[chat_id].values() if s.closed)
        return opened, closed

    # ── Graceful shutdown ──────────────────────────────────────────────────────

    async def cleanup_all(self) -> None:
        """
        Terminate every live shell session across all chats.
        Called from post_shutdown() during bot shutdown.
        Flush tasks are cancelled; processes are terminated concurrently in
        background threads via asyncio.to_thread so the event loop is not
        blocked during the wait.
        """
        logger.info("ShellManager: shutting down all shell sessions")
        pids: list[int] = []

        for chat_shells in self._shells.values():
            for session in chat_shells.values():
                session.closed = True

                # Cancel the flush task
                if session._flush_task is not None \
                        and not session._flush_task.done():
                    session._flush_task.cancel()

                # Remove the fd reader
                if session._reader_registered and session._loop is not None \
                        and session.pty_master_fd is not None:
                    try:
                        session._loop.remove_reader(session.pty_master_fd)
                    except Exception:
                        pass
                    session._reader_registered = False

                # Close the PTY fd
                if session.pty_master_fd is not None:
                    try:
                        os.close(session.pty_master_fd)
                    except OSError:
                        pass
                    session.pty_master_fd = None

                if session.pid is not None:
                    pids.append(session.pid)
                    session.pid = None

        if pids:
            # Terminate all processes concurrently (each may wait up to 2 s)
            await asyncio.gather(
                *[asyncio.to_thread(_terminate_process_blocking, pid)
                  for pid in pids],
                return_exceptions=True,
            )

        logger.info("ShellManager: cleanup complete (%d process(es) terminated)",
                    len(pids))


# Global singleton — one ShellManager for the entire bot process.
# Shell state is isolated per chat_id inside ShellManager; this is not a
# global pool visible to multiple chats.
shell_manager = ShellManager()


async def _on_shell_death(bot: Bot, chat_id: int, session: ShellSession) -> None:
    """
    Async callback fired when a shell process exits unexpectedly (i.e. the user
    typed 'exit', or the process was killed from outside the bot).
    Updates the current-shell pointer and notifies the Telegram chat.
    """
    logger.info("Shell %d died unexpectedly  chat_id=%d", session.shell_id, chat_id)

    msg_suffix = ""
    if shell_manager._current.get(chat_id) == session.shell_id:
        alt = next(
            (sid for sid, s in shell_manager._shells.get(chat_id, {}).items()
             if sid != session.shell_id and not s.closed),
            None,
        )
        shell_manager._current[chat_id] = alt
        if alt is not None:
            msg_suffix = f"\n\nSwitched to Shell {alt}."

    try:
        await bot.send_message(
            chat_id = chat_id,
            text    = f"⚠️ Shell {session.shell_id} has exited.\n\n"
                      f"No longer running.{msg_suffix}",
        )
    except Exception as exc:
        logger.error("Death notification failed chat_id=%d: %s", chat_id, exc)

# ═══════════════════════════════════════════════════════════════════════════════
#  SHELL COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

async def _shell_mode_required(update: Update) -> bool:
    """
    Return True if shell mode is currently active for this chat.

    If shell mode is OFF, reply with a clear rejection message and return False.
    Every shell-operation handler that must only function inside shell mode must
    call this immediately after is_authorized() and return on False.

    /shell_start, /shell_stop, and /shell_help must NOT call this — they are
    intentionally available regardless of the current shell-mode state.
    """
    chat_id = update.effective_chat.id
    if not shell_manager.is_shell_mode(chat_id):
        await update.message.reply_text(
            "⚠️ Shell mode is not active.\n\n"
            "Use /shell_start first."
        )
        return False
    return True


async def cmd_shell_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_start — enter shell mode for this chat.

    If live shells already exist, the current one is displayed.
    If no shells exist, the user is prompted to create one with /shell_new.
    Shell sessions are NOT created automatically.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    logger.info("/shell_start  chat_id=%s", chat_id)

    shell_manager.set_shell_mode(chat_id, True)
    current = shell_manager.get_current_shell(chat_id)

    if current is None or current.closed:
        await update.message.reply_text(
            "🖥️ <b>SHELL MODE ENABLED</b>\n\n"
            "No active shell.\n"
            "Use /shell_new to create one.",
            parse_mode=ParseMode.HTML,
        )
    else:
        alive  = current.is_alive()
        cwd    = current.get_cwd() if alive else "?"
        status = "RUNNING" if alive else "DEAD"
        await update.message.reply_text(
            f"🖥️ <b>SHELL MODE ENABLED</b>\n\n"
            f"Current shell: Shell {current.shell_id}\n"
            f"Directory: {html.escape(cwd)}\n"
            f"Status: {status}\n\n"
            f"Use /shell_help for shell commands.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_shell_stop(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_stop — exit shell mode WITHOUT closing any live shell sessions.

    Normal admin commands (/temp, /status, etc.) become available again.
    Use /shell_start to resume and /shell_one_close etc. to actually terminate.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    logger.info("/shell_stop  chat_id=%s", chat_id)

    shell_manager.set_shell_mode(chat_id, False)

    shells = shell_manager.list_shells(chat_id)
    active = sum(1 for s in shells.values() if not s.closed)

    if active:
        await update.message.reply_text(
            f"🛑 Shell mode disabled.\n\n"
            f"{active} shell session{'s' if active != 1 else ''} remain open.\n"
            f"Use /shell_start to resume."
        )
    else:
        await update.message.reply_text(
            "🛑 Shell mode disabled.\n"
            "No shell sessions remain open."
        )


async def cmd_shell_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_help — show the shell-mode command reference.
    Available both inside and outside shell mode.
    """
    if not is_authorized(update):
        await deny(update); return
    logger.info("/shell_help  chat_id=%s", update.effective_chat.id)

    switch_lines = "\n".join(
        f"/shell_{_SHELL_NUM_TO_WORD[n]} — switch to Shell {n}"
        for n in sorted(_SHELL_NUM_TO_WORD)
        if n <= MAX_SHELLS
    )
    close_lines = "\n".join(
        f"/shell_{_SHELL_NUM_TO_WORD[n]}_close — close Shell {n}"
        for n in sorted(_SHELL_NUM_TO_WORD)
        if n <= MAX_SHELLS
    )

    await update.message.reply_text(
        "🖥️ <b>SHELL MODE</b>\n\n"
        "Type any normal terminal command directly.\n\n"
        "<b>Shell management:</b>\n"
        "/shell_start — enter shell mode\n"
        "/shell_new — create a new shell\n"
        "/shell_current — show current shell info\n"
        "/shell_status — show all shells\n"
        f"{switch_lines}\n"
        f"{close_lines}\n"
        "/shell_previous_N — show previous N output lines "
        "(e.g. /shell_previous_10 /shell_previous_50 /shell_previous_100)\n"
        "/shell_clear — clear stored output history\n"
        "/shell_interrupt — interrupt current command (≈ Ctrl+C)\n"
        "/shell_stop — exit shell mode (does not close shells)\n"
        "/shell_help — show this help\n\n"
        "<b>Notes:</b>\n"
        "• Shell sessions remain alive after /shell_stop.\n"
        "• /shell_stop does <b>not</b> close shells.\n"
        "• /shell_interrupt is similar to Ctrl+C.\n"
        "• Full-screen programs (vim, htop, less) may not work perfectly "
        "through Telegram.\n"
        f"• Maximum open shells: {MAX_SHELLS}.\n"
        "• Shells start with --norc --noprofile; user aliases are not loaded.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_shell_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_new — spawn a new persistent bash session and switch to it.
    Each call creates an independent shell with its own PTY, PID, and CWD.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    if not await _shell_mode_required(update):
        return
    logger.info("/shell_new  chat_id=%s", chat_id)

    loop   = asyncio.get_running_loop()
    result = shell_manager.create_shell(chat_id, loop, context.bot)

    if isinstance(result, str):
        await update.message.reply_text(result, parse_mode=ParseMode.HTML)
        return

    session = result
    await update.message.reply_text(
        f"🖥️ Shell {session.shell_id} created.\n"
        f"Switched to Shell {session.shell_id}.\n\n"
        f"(Initial prompt arriving shortly…)"
    )
    # The shell's first PS1 prompt arrives via the async flush loop within
    # OUTPUT_BATCH_INTERVAL seconds and is sent as a separate Telegram message.


async def cmd_shell_current(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_current — show details about the currently active shell session.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    if not await _shell_mode_required(update):
        return
    logger.info("/shell_current  chat_id=%s", chat_id)

    session = shell_manager.get_current_shell(chat_id)
    if session is None:
        await update.message.reply_text(
            "ℹ️ No current shell.\n\nUse /shell_new to create one."
        )
        return

    alive   = session.is_alive()
    cwd     = session.get_cwd() if alive else "?"
    status  = "RUNNING" if alive else ("CLOSED" if session.closed else "DEAD")
    pid_str = str(session.pid) if session.pid else "?"

    await update.message.reply_text(
        "🖥️ <b>Current Shell</b>\n\n"
        f"Shell: {session.shell_id}\n"
        f"PID: {pid_str}\n"
        f"User: {html.escape(session.get_user())}\n"
        f"Host: {html.escape(session.get_host())}\n"
        f"Directory: {html.escape(cwd)}\n"
        f"Status: {status}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_shell_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_status — show a summary of all shell sessions for this chat.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    if not await _shell_mode_required(update):
        return
    logger.info("/shell_status  chat_id=%s", chat_id)

    shells     = shell_manager.list_shells(chat_id)
    current    = shell_manager.get_current_shell(chat_id)
    current_id = current.shell_id if current else None
    opened, closed_count = shell_manager.stats(chat_id)

    if not shells:
        await update.message.reply_text(
            "🖥️ <b>SHELL STATUS</b>\n\n"
            "No shells created yet.\n"
            "Use /shell_new to create one.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines: list[str] = []
    for sid in sorted(shells):
        s = shells[sid]
        if s.closed:
            continue
        alive  = s.is_alive()
        status = "RUNNING" if alive else "DEAD"
        cwd    = s.get_cwd() if alive else "?"
        line   = f"• Shell {sid} — {status}  [{html.escape(cwd)}]"
        if sid == current_id:
            line += "  ← current"
        lines.append(line)

    active_block  = "\n".join(lines) if lines else "(none)"
    current_label = f"Shell {current_id}" if current_id is not None else "None"

    await update.message.reply_text(
        "🖥️ <b>SHELL STATUS</b>\n\n"
        f"Total opened: {opened}\n"
        f"Total closed: {closed_count}\n\n"
        f"Active shells:\n{active_block}\n\n"
        f"Current shell: {current_label}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_shell_clear(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_clear — clear the stored Telegram-visible output history of the
    current shell.  The shell process, its CWD, and its environment are
    completely unaffected.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    if not await _shell_mode_required(update):
        return
    logger.info("/shell_clear  chat_id=%s", chat_id)

    session = shell_manager.get_current_shell(chat_id)
    if session is None:
        await update.message.reply_text("ℹ️ No current shell.")
        return

    session.output_history.clear()
    session._pending_lines.clear()
    session._partial_line = ""
    await update.message.reply_text(
        f"🧹 Shell {session.shell_id} output history cleared."
    )


async def cmd_shell_interrupt(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_interrupt — send Ctrl+C to the current shell's foreground process.

    This writes 0x03 through the PTY, which the PTY line discipline converts
    to SIGINT for the foreground process group — the same as pressing Ctrl+C in
    a real terminal.  The bash session itself is not destroyed.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    if not await _shell_mode_required(update):
        return
    logger.info("/shell_interrupt  chat_id=%s", chat_id)

    session = shell_manager.get_current_shell(chat_id)
    if session is None:
        await update.message.reply_text("ℹ️ No current shell.")
        return

    if session.closed or not session.is_alive():
        await update.message.reply_text(
            f"❌ Shell {session.shell_id} is not running."
        )
        return

    try:
        session.send_interrupt()
        await update.message.reply_text(
            f"🛑 Interrupt signal sent to Shell {session.shell_id}."
        )
    except Exception as exc:
        logger.error("Shell interrupt failed: %s", exc)
        await update.message.reply_text(
            f"❌ Failed to interrupt: {html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_shell_previous(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /shell_previous_N — display the last N lines of the current shell's stored
    output history.

    N must be a positive integer and may not exceed MAX_PREVIOUS_LINES.
    This command works only while shell mode is active.
    """
    if not is_authorized(update):
        await deny(update); return
    chat_id = update.effective_chat.id
    if not await _shell_mode_required(update):
        return
    text    = update.message.text or ""

    # Strip optional @botname suffix that Telegram adds in group chats
    cmd_part = text.strip().split()[0]
    if "@" in cmd_part:
        cmd_part = cmd_part.split("@")[0]

    match = re.match(r"^/shell_previous_(\d+)$", cmd_part)
    if not match:
        await update.message.reply_text(
            "❌ Invalid format.\n\n"
            "Use: /shell_previous_<number>   e.g. /shell_previous_15"
        )
        return

    try:
        n = int(match.group(1))
    except ValueError:
        await update.message.reply_text("❌ Invalid line count.")
        return

    if n <= 0:
        await update.message.reply_text("❌ Line count must be a positive integer.")
        return

    if n > MAX_PREVIOUS_LINES:
        await update.message.reply_text(
            f"❌ Maximum previous-output request is {MAX_PREVIOUS_LINES} lines."
        )
        return

    session = shell_manager.get_current_shell(chat_id)
    if session is None:
        await update.message.reply_text(
            "ℹ️ No current shell.\n\nUse /shell_new to create one."
        )
        return

    if not session.output_history:
        await update.message.reply_text(
            f"ℹ️ Shell {session.shell_id} has no stored output yet."
        )
        return

    logger.info("/shell_previous_%d  chat_id=%s", n, chat_id)
    output = session.recent_output(n)
    if not output.strip():
        await update.message.reply_text(
            f"ℹ️ Shell {session.shell_id} has no stored output yet."
        )
        return

    await _send_shell_chunks(update, output)


def _make_switch_handler(shell_num: int):
    """
    Factory that creates the /shell_<word> handler for a specific shell number.
    Using a factory avoids the classic Python loop-closure late-binding bug.
    """
    async def handler(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update):
            await deny(update); return
        chat_id = update.effective_chat.id
        if not await _shell_mode_required(update):
            return
        logger.info("/shell_%s (switch to %d)  chat_id=%s",
                    _SHELL_NUM_TO_WORD.get(shell_num, str(shell_num)),
                    shell_num, chat_id)

        result = shell_manager.switch_shell(chat_id, shell_num)
        if isinstance(result, str):
            await update.message.reply_text(result, parse_mode=ParseMode.HTML)
            return

        session = result
        cwd     = session.get_cwd()
        user    = session.get_user()
        host    = session.get_host()
        prompt  = f"{user}@{host}:{_shorten_path(cwd)}$ "
        recent  = session.recent_output(DEFAULT_PREVIOUS_LINES)
        recent_block = (
            f"\n\nLast output:\n<pre>{html.escape(recent)}</pre>"
            if recent.strip() else ""
        )
        await update.message.reply_text(
            f"🔄 Switched to Shell {shell_num} successfully.\n\n"
            f"<pre>{html.escape(prompt)}</pre>"
            f"{recent_block}",
            parse_mode=ParseMode.HTML,
        )

    handler.__name__ = f"cmd_shell_switch_{shell_num}"
    return handler


def _make_close_handler(shell_num: int):
    """
    Factory that creates the /shell_<word>_close handler for a specific shell.
    """
    async def handler(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized(update):
            await deny(update); return
        chat_id = update.effective_chat.id
        if not await _shell_mode_required(update):
            return
        logger.info("/shell_%s_close  chat_id=%s",
                    _SHELL_NUM_TO_WORD.get(shell_num, str(shell_num)), chat_id)

        msg = shell_manager.close_shell(chat_id, shell_num)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    handler.__name__ = f"cmd_shell_close_{shell_num}"
    return handler

# ═══════════════════════════════════════════════════════════════════════════════
#  SHELL MODE INTERCEPTOR  (registered in handler group -1)
# ═══════════════════════════════════════════════════════════════════════════════

async def shell_mode_interceptor(
    update: Update, _: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Runs before all group-0 handlers.

    When shell mode is INACTIVE for this chat → return immediately so normal
    command handlers in group 0 take over.

    When shell mode is ACTIVE for this chat:
    • /shell_stop and all /shell_* commands → return without raising so the
      dedicated CommandHandlers in group 0 can handle them.
    • Any other /command → send a "you are in shell mode" notice and raise
      ApplicationHandlerStop to prevent normal handlers from firing.
    • Ordinary text → write it to the current shell's PTY and raise
      ApplicationHandlerStop.

    Authorization is checked here; unauthorized senders are passed through so
    the deny() in the group-0 handler fires as normal (no double response).
    """
    if update.message is None or update.message.text is None:
        return  # not a text message — let other handlers deal with it
    if not is_authorized(update):
        return  # don't deny here; let group-0 handler do it

    chat_id = update.effective_chat.id
    if not shell_manager.is_shell_mode(chat_id):
        return  # shell mode off — normal routing

    text     = update.message.text.strip()
    cmd_part = text.split()[0] if text else ""

    # Strip @botname suffix (Telegram adds this in group chats)
    if "@" in cmd_part:
        cmd_part = cmd_part.split("@")[0]

    # /shell_stop is the critical escape hatch — always let it through
    if cmd_part == "/shell_stop":
        return

    # All other /shell_* commands → pass through to their CommandHandlers
    if cmd_part.startswith("/shell_"):
        return

    # Any other slash command is blocked while in shell mode
    if text.startswith("/"):
        current = shell_manager.get_current_shell(chat_id)
        curr_name = f"Shell {current.shell_id}" if current else "None"
        await update.message.reply_text(
            "⚠️ You are currently in <b>SHELL MODE</b>.\n\n"
            "Normal admin commands are temporarily disabled.\n\n"
            f"Current shell: {curr_name}\n\n"
            "Type a terminal command directly, or use:\n"
            "/shell_help\n"
            "/shell_stop",
            parse_mode=ParseMode.HTML,
        )
        raise ApplicationHandlerStop

    # Ordinary text → send as shell input
    session = shell_manager.get_current_shell(chat_id)
    if session is None:
        await update.message.reply_text(
            "ℹ️ No active shell.\n\n"
            "Use /shell_new to create one, or /shell_stop to exit shell mode."
        )
        raise ApplicationHandlerStop

    if session.closed or not session.is_alive():
        session.closed = True
        await update.message.reply_text(
            f"❌ Shell {session.shell_id} is not running.\n\n"
            "Use /shell_new to create a new shell."
        )
        raise ApplicationHandlerStop

    try:
        session.send_input(text)
    except Exception as exc:
        logger.error("Shell %d input error: %s", session.shell_id, exc)
        await update.message.reply_text(
            f"❌ Failed to send input: {html.escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
    raise ApplicationHandlerStop

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS — monitoring
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/start  chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(
        "🍓 <b>Raspberry Pi 3 Admin Bot</b>\n\nSend /help to see available commands.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/help  chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(
        "<b>📋 Available Commands</b>\n\n"
        "<b>Monitoring</b>\n"
        "/status — Full system overview\n"
        "/temp — CPU temperature\n"
        "/cpu — CPU usage\n"
        "/memory — RAM usage\n"
        "/disk — Disk usage\n"
        "/uptime — System uptime\n"
        "/network — Network speed (1-second sample)\n"
        "/services — Systemd service states\n\n"
        "<b>Service Control</b>\n"
        "/start_manager — Start ftp-manager\n"
        "/stop_manager — Stop ftp-manager\n"
        "/restart_manager — Restart ftp-manager\n\n"
        "<b>Logs</b>\n"
        "/logs — ftp-manager journal (last 40 lines)\n"
        "/botlogs — Admin bot log (last 40 lines)\n\n"
        "<b>System</b>\n"
        "/reboot — Reboot Pi (confirmation required)\n"
        "/shutdown — Shut down Pi (confirmation required)\n\n"
        "<b>Shell</b>\n"
        "/shell_start — Enter interactive shell mode\n"
        "/shell_stop — Exit shell mode\n"
        "/shell_help — Shell command reference",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/status  chat_id=%s", update.effective_chat.id)

    placeholder = await update.message.reply_text("⏳ Gathering system status…")
    n = len(MONITORED_SERVICES)

    # All blocking calls run concurrently in threads.
    # network (1 s) and cpu (0.5 s) overlap → total wait ≈ 1 s instead of 1.5 s.
    results = await asyncio.gather(
        asyncio.to_thread(_temperature),
        asyncio.to_thread(_cpu_usage),
        asyncio.to_thread(_memory),
        asyncio.to_thread(_disk),
        asyncio.to_thread(_network),
        asyncio.to_thread(_ip_address),
        *[asyncio.to_thread(_service_status, s) for s in MONITORED_SERVICES],
        return_exceptions=True,
    )

    def _safe(v, fallback):
        return fallback if isinstance(v, Exception) else v

    temp            = _safe(results[0], "N/A")
    cpu             = _safe(results[1], "N/A")
    used_mb, tot_mb = _safe(results[2], (0, 0))
    used_gb, tot_gb = _safe(results[3], (0.0, 0.0))
    net             = _safe(results[4], "N/A")
    ip_str          = _safe(results[5], "N/A")
    svc_lines       = "\n".join(
        r if isinstance(r, str) else "⚠️ (error)"
        for r in results[6:6 + n]
    )
    hostname   = socket.gethostname()
    uptime_str = _uptime()
    mem_pct    = round(used_mb / tot_mb  * 100) if tot_mb  else 0
    disk_pct   = round(used_gb / tot_gb  * 100) if tot_gb  else 0

    await placeholder.edit_text(
        "🖥 <b>Raspberry Pi Status</b>\n\n"
        f"🏠 <b>Hostname:</b> {html.escape(hostname)}\n"
        f"⏱ <b>Uptime:</b> {html.escape(uptime_str)}\n"
        f"🌡 <b>Temperature:</b> {html.escape(str(temp))}\n"
        f"⚙️ <b>CPU:</b> {html.escape(str(cpu))}\n"
        f"🧠 <b>RAM:</b> {used_mb} / {tot_mb} MB ({mem_pct}%)\n"
        f"💾 <b>Disk:</b> {used_gb:.1f} / {tot_gb:.1f} GB ({disk_pct}%)\n"
        f"🌐 <b>IP:</b> {html.escape(str(ip_str))}\n"
        f"📶 <b>Network:</b> {html.escape(str(net))}\n\n"
        f"<b>Services:</b>\n{svc_lines}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_temp(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/temp  chat_id=%s", update.effective_chat.id)
    t = await asyncio.to_thread(_temperature)
    await update.message.reply_text(f"🌡 Temperature: {t}")


async def cmd_cpu(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/cpu  chat_id=%s", update.effective_chat.id)
    c = await asyncio.to_thread(_cpu_usage)
    await update.message.reply_text(f"⚙️ CPU usage: {c}")


async def cmd_memory(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/memory  chat_id=%s", update.effective_chat.id)
    used, total = await asyncio.to_thread(_memory)
    pct = round(used / total * 100) if total else 0
    await update.message.reply_text(f"🧠 RAM: {used} / {total} MB ({pct}%)")


async def cmd_disk(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/disk  chat_id=%s", update.effective_chat.id)
    used, total = await asyncio.to_thread(_disk)
    pct = round(used / total * 100) if total else 0
    await update.message.reply_text(f"💾 Disk: {used:.1f} / {total:.1f} GB ({pct}%)")


async def cmd_uptime(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/uptime  chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(f"⏱ Uptime: {_uptime()}")


async def cmd_network(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/network  chat_id=%s", update.effective_chat.id)
    placeholder = await update.message.reply_text("⏳ Sampling network speed…")
    net = await asyncio.to_thread(_network)
    await placeholder.edit_text(
        f"🌐 Network: {html.escape(net)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_services(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/services  chat_id=%s", update.effective_chat.id)
    statuses = await asyncio.gather(
        *[asyncio.to_thread(_service_status, s) for s in MONITORED_SERVICES]
    )
    await update.message.reply_text(
        "<b>Services:</b>\n" + "\n".join(statuses),
        parse_mode=ParseMode.HTML,
    )

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS — service control  (ftp-manager.service only)
# ═══════════════════════════════════════════════════════════════════════════════

async def _svc_action(update: Update, action: str) -> None:
    """Shared helper for start / stop / restart of the managed service."""
    if not is_authorized(update):
        await deny(update); return
    logger.info("/%s_manager  chat_id=%s", action, update.effective_chat.id)
    placeholder = await update.message.reply_text(
        f"⏳ {action.capitalize()}ing {MANAGED_SERVICE}…"
    )
    result = await asyncio.to_thread(_run_systemctl, action, MANAGED_SERVICE)
    await placeholder.edit_text(result)


async def cmd_start_manager(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _svc_action(update, "start")


async def cmd_stop_manager(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _svc_action(update, "stop")


async def cmd_restart_manager(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await _svc_action(update, "restart")

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS — logs
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_logs(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent ftp-manager journal entries."""
    if not is_authorized(update):
        await deny(update); return
    logger.info("/logs  chat_id=%s", update.effective_chat.id)
    output = await asyncio.to_thread(_journal, MANAGED_SERVICE, LOG_LINES)
    await _send_pre(update, output)


async def cmd_botlogs(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the last LOG_LINES lines of today's admin bot log file."""
    if not is_authorized(update):
        await deny(update); return
    logger.info("/botlogs  chat_id=%s", update.effective_chat.id)
    output = await asyncio.to_thread(_tail_botlog, LOG_LINES)
    await _send_pre(update, output)

# ═══════════════════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS — reboot / shutdown  (inline confirmation required)
# ═══════════════════════════════════════════════════════════════════════════════

def _confirm_kb(action: str) -> InlineKeyboardMarkup:
    """Return a two-button inline keyboard: [Confirm <action>]  [Cancel]."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"✅ Confirm {action.capitalize()}",
            callback_data=f"confirm_{action}",
        ),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]])


async def cmd_reboot(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/reboot requested  chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(
        "⚠️ <b>Reboot Raspberry Pi?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_confirm_kb("reboot"),
    )


async def cmd_shutdown(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await deny(update); return
    logger.info("/shutdown requested  chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(
        "⚠️ <b>SHUT DOWN Raspberry Pi?</b>\n\nThe Pi will power off completely.",
        parse_mode=ParseMode.HTML,
        reply_markup=_confirm_kb("shutdown"),
    )


async def callback_handler(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle inline keyboard button presses.

    Security model
    ──────────────
    Chat ID is verified BEFORE query.answer() and BEFORE any action.
    callback_data values are fixed strings from _confirm_kb(); they are never
    derived from user-supplied text and cannot be used for command injection.
    """
    query = update.callback_query

    # ── Security check FIRST ─────────────────────────────────────────────────
    if not is_authorized(update):
        await query.answer("Unauthorized.", show_alert=True)
        logger.warning(
            "Unauthorized callback: user_id=%s data=%r",
            getattr(update.effective_user, "id", "?"),
            query.data,
        )
        return

    await query.answer()    # acknowledge button — clears the loading spinner

    data = query.data

    if data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return

    # ── Confirm Reboot ────────────────────────────────────────────────────────
    if data == "confirm_reboot":
        logger.info("REBOOT confirmed  chat_id=%s", update.effective_chat.id)
        await query.edit_message_text(
            "🔄 Reboot command issued. "
            "Waiting for Raspberry Pi to come back online..."
        )
        write_state("reboot")
        await asyncio.sleep(2)   # let Telegram deliver the message before the Pi goes down
        try:
            # Non-blocking Popen; the system reboots shortly after.
            # Hard-coded command list — no shell=True, no user input.
            subprocess.Popen(["sudo", "/usr/bin/systemctl", "reboot"])
        except Exception as exc:
            logger.error("systemctl reboot failed: %s", exc)
            clear_state()   # remove stale state — the Pi did NOT reboot
            try:
                await query.edit_message_text(
                    f"❌ Reboot command failed: {html.escape(str(exc))}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        return

    # ── Confirm Shutdown ──────────────────────────────────────────────────────
    if data == "confirm_shutdown":
        logger.info("SHUTDOWN confirmed  chat_id=%s", update.effective_chat.id)
        await query.edit_message_text(
            "🔴 Shutdown command issued. Raspberry Pi is shutting down."
        )
        write_state("shutdown")
        await asyncio.sleep(2)   # let Telegram deliver the message before the Pi shuts down
        try:
            # Hard-coded command list — no shell=True, no user input.
            subprocess.Popen(["sudo", "/usr/bin/systemctl", "poweroff"])
        except Exception as exc:
            logger.error("systemctl poweroff failed: %s", exc)
            clear_state()   # remove stale state — the Pi did NOT shut down
            try:
                await query.edit_message_text(
                    f"❌ Shutdown command failed: {html.escape(str(exc))}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        return

    # Reached only if an unknown callback_data arrives — should never happen.
    logger.warning("Unknown callback_data: %r", data)
    await query.edit_message_text("❓ Unknown action.")

# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

async def post_init(application: Application) -> None:
    """
    Called automatically after Application.initialize() — the bot is connected
    and can make API calls.  Send the boot notification now.
    """
    await send_boot_notification(application.bot)


async def post_shutdown(application: Application) -> None:
    """
    Called during Application shutdown — clean up all live PTY shell sessions
    so no zombie processes or open file descriptors are left behind.
    """
    logger.info("post_shutdown: cleaning up shell sessions")
    try:
        await shell_manager.cleanup_all()
    except Exception as exc:
        logger.error("Shell cleanup during shutdown: %s", exc)


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PUT_NEW_BOT_TOKEN_HERE":
        raise ValueError(
            "Set TELEGRAM_BOT_TOKEN in the environment (or update BOT_TOKEN "
            "in the CONFIGURATION section) before running."
        )
    if not ADMIN_CHAT_IDS:
        raise ValueError(
            "ADMIN_CHAT_IDS must contain at least one integer chat ID."
        )

    logger.info("Pi Admin Bot v2.0 starting")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ── Shell mode interceptor (group -1) ──────────────────────────────────────
    # Runs before ALL group-0 handlers.  In shell mode it either routes text
    # to the current shell or blocks non-shell commands.  When shell mode is
    # off it returns without action, letting group-0 handlers take over.
    app.add_handler(
        MessageHandler(filters.TEXT, shell_mode_interceptor),
        group=-1,
    )

    # ── Shell management commands (group 0) ────────────────────────────────────
    app.add_handler(CommandHandler("shell_start",     cmd_shell_start))
    app.add_handler(CommandHandler("shell_stop",      cmd_shell_stop))
    app.add_handler(CommandHandler("shell_help",      cmd_shell_help))
    app.add_handler(CommandHandler("shell_new",       cmd_shell_new))
    app.add_handler(CommandHandler("shell_current",   cmd_shell_current))
    app.add_handler(CommandHandler("shell_status",    cmd_shell_status))
    app.add_handler(CommandHandler("shell_clear",     cmd_shell_clear))
    app.add_handler(CommandHandler("shell_interrupt", cmd_shell_interrupt))

    # ── Dynamic /shell_previous_N handler ─────────────────────────────────────
    # Catches /shell_previous_<anything> including invalid suffixes so the
    # handler can return a clear error rather than silently ignoring the message.
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/shell_previous_"),
            cmd_shell_previous,
        )
    )

    # ── Numbered switch / close handlers (/shell_one … /shell_eight_close) ────
    for num, word in sorted(_SHELL_NUM_TO_WORD.items()):
        if num <= MAX_SHELLS:
            app.add_handler(CommandHandler(f"shell_{word}",
                                           _make_switch_handler(num)))
            app.add_handler(CommandHandler(f"shell_{word}_close",
                                           _make_close_handler(num)))

    # ── Monitoring ─────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",           cmd_start))
    app.add_handler(CommandHandler("help",            cmd_help))
    app.add_handler(CommandHandler("status",          cmd_status))
    app.add_handler(CommandHandler("temp",            cmd_temp))
    app.add_handler(CommandHandler("cpu",             cmd_cpu))
    app.add_handler(CommandHandler("memory",          cmd_memory))
    app.add_handler(CommandHandler("disk",            cmd_disk))
    app.add_handler(CommandHandler("uptime",          cmd_uptime))
    app.add_handler(CommandHandler("network",         cmd_network))
    app.add_handler(CommandHandler("services",        cmd_services))

    # ── Service control ────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start_manager",   cmd_start_manager))
    app.add_handler(CommandHandler("stop_manager",    cmd_stop_manager))
    app.add_handler(CommandHandler("restart_manager", cmd_restart_manager))

    # ── Logs ───────────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("logs",            cmd_logs))
    app.add_handler(CommandHandler("botlogs",         cmd_botlogs))

    # ── System control ─────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("reboot",          cmd_reboot))
    app.add_handler(CommandHandler("shutdown",        cmd_shutdown))

    # ── Inline keyboard callbacks ──────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Polling started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()