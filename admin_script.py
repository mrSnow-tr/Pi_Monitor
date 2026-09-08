#!/usr/bin/env python3
"""
admin_script.py — Telegram admin bot for Raspberry Pi 3.

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
• Security    : every message and callback verified against ADMIN_CHAT_IDS
• No shell=True, no arbitrary command execution, no hard-coded sudo password
"""

import asyncio
import datetime
import html
import json
import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION — edit these values before first run
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN = "<your-bot-token-here>"   # Token from @BotFather

# Authorised Telegram chat IDs.  Add more as needed:
ADMIN_CHAT_IDS: set[int] = {
    123456789, # real telegram chat id here 
    #987654321,
} 

# Services the bot may start / stop / restart (strict allowlist):
ALLOWED_SERVICES: set[str] = {
    "ftp-manager.service",
}

# Services shown in /services and /status:
MONITORED_SERVICES: list[str] = [
    #"<the-service-you-want-to-momitor>",
    "pihole-FTL.service", #it's pi hole default status monitor in raspberry pi. 
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
            self._current_date  = today
            self.close()
            self.baseFilename   = os.path.abspath(
                str(self._log_dir / f"{today}.log")
            )
            self.stream         = self._open()
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
                # Use root logger directly to avoid potential re-entry issues
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


def _default_iface() -> str | None:
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
        "/shutdown — Shut down Pi (confirmation required)",
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


def main() -> None:
    if BOT_TOKEN == "PUT_NEW_BOT_TOKEN_HERE":
        raise ValueError(
            "Set BOT_TOKEN in the CONFIGURATION section before running."
        )
    if not ADMIN_CHAT_IDS:
        raise ValueError(
            "ADMIN_CHAT_IDS must contain at least one integer chat ID."
        )

    logger.info("Pi Admin Bot starting")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

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
