# Pi_Monitor

**Telegram admin bot for Raspberry Pi (and other Linux systems)**  
Version **2.0** — full remote terminal access via persistent PTY shells.

**Repository:** [https://github.com/mrSnow-tr/Pi_Monitor](https://github.com/mrSnow-tr/Pi_Monitor)

---

## Overview

Pi_Monitor turns your Raspberry Pi (or any systemd-based Linux machine) into a remote-administrable system controlled entirely from Telegram.

**v1** gave you monitoring, service control, logs and reboot/shutdown.  
**v2** adds a complete **interactive shell subsystem**:

- Persistent bash sessions backed by real POSIX PTYs
- Multiple concurrent shells (up to 8)
- Per-shell output history
- Switch, close, interrupt (Ctrl+C), clear history
- Clean separation between “admin mode” and “shell mode”

Once you enter shell mode, every normal message you type is executed in a real bash process on the Pi. It feels like having a terminal in your Telegram chat.

---

## Features

### Monitoring
| Command     | Description |
|-------------|-------------|
| `/status`   | Full system overview (temp, CPU, RAM, disk, network, services) |
| `/temp`     | CPU temperature |
| `/cpu`      | CPU usage |
| `/memory`   | RAM usage |
| `/disk`     | Disk usage (root filesystem) |
| `/uptime`   | System uptime |
| `/network`  | Network RX/TX speed (1-second sample) |
| `/services` | Status of monitored systemd services |

### Service Control
| Command            | Description |
|--------------------|-------------|
| `/start_manager`   | Start the managed service |
| `/stop_manager`    | Stop the managed service |
| `/restart_manager` | Restart the managed service |

### Logs
| Command    | Description |
|------------|-------------|
| `/logs`    | Last 40 lines of the managed service journal |
| `/botlogs` | Last 40 lines of today’s bot log |

### System
| Command     | Description |
|-------------|-------------|
| `/reboot`   | Reboot (requires inline confirmation) |
| `/shutdown` | Power off (requires inline confirmation) |

### Shell Mode (v2)
| Command                  | Description |
|--------------------------|-------------|
| `/shell_start`           | Enter shell mode |
| `/shell_stop`            | Exit shell mode (shells stay alive) |
| `/shell_help`            | Full shell command reference |
| `/shell_new`             | Create a new independent bash session |
| `/shell_current`         | Show info about the current shell |
| `/shell_status`          | List all open shells |
| `/shell_one` … `/shell_eight` | Switch to a specific shell |
| `/shell_one_close` … `/shell_eight_close` | Close a specific shell |
| `/shell_previous_N`      | Show last N lines of output history (e.g. `/shell_previous_50`) |
| `/shell_clear`           | Clear stored output history of current shell |
| `/shell_interrupt`       | Send Ctrl+C to the current shell |

**While in shell mode** any plain text message is sent directly to the current bash process.

---

## Requirements

- Python **3.10+**
- Linux with `systemd` (Raspberry Pi OS recommended)
- `sudo` privileges for the bot user (passwordless for `systemctl` only)
- Membership in the `systemd-journal` group (for `/logs`)
- Sufficient resources for up to 8 concurrent bash processes + PTYs (Pi 3 handles this fine)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/mrSnow-tr/Pi_Monitor.git
cd Pi_Monitor
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure the bot

**Strongly recommended:** store the bot token in an environment variable instead of the source file.

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
```

Edit `admin_script.py` and set:

```python
# Authorised Telegram chat IDs
ADMIN_CHAT_IDS: set[int] = {
    123456789,          # your Telegram user / chat ID
}

# Services the bot is allowed to start / stop / restart
ALLOWED_SERVICES: set[str] = {
    "pihole-FTL.service",
}

# Services shown in /status and /services
MONITORED_SERVICES: list[str] = [
    "ftp-manager.service",
    "pihole-FTL.service",
    # add more if needed
]

MANAGED_SERVICE = "pihole-FTL.service"
```

> **How to get your Chat ID**  
> Message [@userinfobot](https://t.me/userinfobot) or temporarily print `update.effective_chat.id`.

**Never commit a live bot token to GitHub.**

### 5. System preparation

#### Passwordless sudo for systemctl

```bash
sudo visudo -f /etc/sudoers.d/pi-monitor
```

Add (replace `pi` with the username that will run the bot):

```
pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl start *, /usr/bin/systemctl stop *, /usr/bin/systemctl restart *, /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff
```

#### Journal access

```bash
sudo usermod -aG systemd-journal pi
```

Log out / reboot for the group change to take effect.

### 6. Test run

```bash
source .venv/bin/activate
python admin_script.py
```

You should receive a boot notification in Telegram. Try `/status` and `/shell_help`.

---

## Running as a systemd service (recommended)

Create `/etc/systemd/system/pi-monitor.service`:

```ini
[Unit]
Description=Pi Monitor Telegram Admin Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/home/pi/Pi_Monitor
Environment=TELEGRAM_BOT_TOKEN=YOUR_TOKEN_HERE
ExecStart=/home/pi/Pi_Monitor/.venv/bin/python /home/pi/Pi_Monitor/admin_script.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

# Optional hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/pi/Pi_Monitor

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi-monitor.service
sudo systemctl status pi-monitor.service
```

---

## Using Shell Mode

1. Send `/shell_start`  
   → Bot replies that shell mode is enabled.

2. Send `/shell_new`  
   → A real bash process is spawned. You will see the prompt arrive shortly.

3. Type any normal command:  
   ```
   ls -la
   cd /var/log
   cat syslog | tail -20
   ```
   Output appears in Telegram as monospaced messages.

4. Useful commands while inside a shell:
   - `/shell_interrupt` → Ctrl+C
   - `/shell_previous_30` → last 30 lines of history
   - `/shell_status` → see all open shells
   - `/shell_two` → switch to shell 2
   - `/shell_one_close` → close shell 1

5. When finished:  
   `/shell_stop` — exits shell mode (sessions stay alive in the background).  
   You can later `/shell_start` again and continue where you left off.

### Important notes about the shell

- Shells are started with `--norc --noprofile` (clean environment, no user aliases).
- `HISTFILE=/dev/null` so bash history is not written to the SD card.
- `TERM=dumb` reduces ANSI noise; most colors are stripped.
- Full-screen TUI programs (`vim`, `htop`, `less`, `nano` in some modes) do **not** work well through Telegram. Prefer non-interactive tools.
- Maximum open shells: **8** (configurable via `MAX_SHELLS`).
- Each shell has its own independent CWD, environment and process tree.
- Output history is kept in memory (default 1000 lines per shell) and is lost on bot restart.

---

## Security model

- Every command and every callback is checked against `ADMIN_CHAT_IDS`.
- Service start/stop/restart is restricted to the hard-coded allow-list.
- Reboot and shutdown always require an explicit confirmation button.
- Shell input is written only to the PTY of a shell that belongs to the same chat ID.  
  Chat A can never see or control Chat B’s shells.
- No `shell=True`, no arbitrary command construction from user input for systemctl.
- The bot token should live in the environment (`TELEGRAM_BOT_TOKEN`), never in the repository.

**Shell access is powerful.** Anyone who can message the bot from an authorized chat ID effectively has a remote terminal as the bot’s user. Protect the chat ID list carefully.

---

## File layout

```
Pi_Monitor/
├── admin_script.py      # Main bot (v2.0)
├── requirements.txt
├── README.md
├── logs/                # Daily YYYY-MM-DD.log files (auto-created)
└── .admin_state.json    # Temporary state for boot-notification detection
```

---

## Configuration knobs (top of admin_script.py)

| Variable                  | Default | Meaning |
|---------------------------|---------|---------|
| `MAX_SHELLS`              | 8       | Max simultaneous open shells per chat |
| `MAX_OUTPUT_HISTORY_LINES`| 1000    | Lines kept in memory per shell |
| `DEFAULT_PREVIOUS_LINES`  | 10      | Lines shown when switching shells |
| `MAX_PREVIOUS_LINES`      | 500     | Hard limit for `/shell_previous_N` |
| `OUTPUT_BATCH_INTERVAL`   | 0.3 s   | How often PTY output is flushed to Telegram |
| `LOG_RETENTION_DAYS`      | 7       | How long daily log files are kept |
| `LOG_LINES`               | 40      | Lines returned by `/logs` and `/botlogs` |

---

## Limitations

- Interactive full-screen programs do not work well.
- Very large output bursts are chunked (Telegram 4096-character limit).
- Shells are lost on bot restart or system reboot (by design).
- Temperature reading uses the Raspberry Pi thermal zone path; other boards return `N/A`.

---

## Credits

Originally written for Raspberry Pi OS.  
Maintained by [mrSnow-tr](https://github.com/mrSnow-tr).
