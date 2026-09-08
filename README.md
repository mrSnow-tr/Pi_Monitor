# Pi_Monitor

Telegram admin bot for Raspberry Pi (and other Linux systems) that lets you monitor system status and control a managed service from your phone.

**Repository:** [https://github.com/mrSnow-tr/Pi_Monitor](https://github.com/mrSnow-tr/Pi_Monitor)

---

## Features

### Monitoring
| Command       | Description                              |
|---------------|------------------------------------------|
| `/status`     | Full system overview (temp, CPU, RAM, disk, network, services) |
| `/temp`       | CPU temperature                          |
| `/cpu`        | CPU usage                                |
| `/memory`     | RAM usage                                |
| `/disk`       | Disk usage (root filesystem)             |
| `/uptime`     | System uptime                            |
| `/network`    | Network RX/TX speed (1-second sample)    |
| `/services`   | Status of monitored systemd services     |

### Service Control
| Command             | Description                    |
|---------------------|--------------------------------|
| `/start_manager`    | Start `ftp-manager.service`    |
| `/stop_manager`     | Stop `ftp-manager.service`     |
| `/restart_manager`  | Restart `ftp-manager.service`  |

### Logs
| Command     | Description                              |
|-------------|------------------------------------------|
| `/logs`     | Last 40 lines of `ftp-manager` journal   |
| `/botlogs`  | Last 40 lines of today's bot log         |

### System Control
| Command      | Description                                      |
|--------------|--------------------------------------------------|
| `/reboot`    | Reboot (requires inline confirmation)            |
| `/shutdown`  | Power off (requires inline confirmation)         |

### Other
- Automatic **boot notification** (detects reboot / shutdown / power-loss)
- Daily rotating log files with automatic cleanup
- Strict allow-list for services and actions
- Every message & callback is checked against authorized chat IDs
- No `shell=True`, no arbitrary command execution, no hard-coded passwords

---

## Requirements

- Python **3.10+**
- A Linux system with `systemd` (Raspberry Pi OS recommended)
- `sudo` privileges for the bot user (passwordless for `systemctl` only)
- Membership in the `systemd-journal` group (for `/logs`)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/mrSnow-tr/Pi_Monitor.git
cd Pi_Monitor
```

### 2. Create a virtual environment (recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure the bot

Edit `admin_script.py` (or rename it if you prefer) and set the following values near the top:

```python
BOT_TOKEN = "123456:ABC-DEF..."          # Token from @BotFather

ADMIN_CHAT_IDS: set[int] = {
    123456789,                           # Your Telegram user / chat ID
    # 987654321,                         # Optional additional admins
}

ALLOWED_SERVICES: set[str] = {
    "ftp-manager.service",               # Services the bot is allowed to start/stop/restart
}

MONITORED_SERVICES: list[str] = [
    "pihole-FTL.service",                # Services shown in /status and /services
    # "your-other-service.service",
]

MANAGED_SERVICE = "ftp-manager.service"  # The single service controlled by /start_manager etc.
```

> **How to get your Chat ID**  
> Send a message to [@userinfobot](https://t.me/userinfobot) or start a conversation with your bot and look at the bot logs / use a temporary print statement.

### 5. Prepare the system

#### Passwordless sudo for systemctl (recommended)

Create a sudoers drop-in file:

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

Log out and back in (or reboot) for the group change to take effect.

### 6. Test run

```bash
source .venv/bin/activate
python admin_script.py
```

You should see a boot notification in Telegram. Try `/status` and `/help`.

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

## Usage

1. Open Telegram and start a chat with your bot.
2. Send `/start` or `/help`.
3. Use any of the commands listed above.

**Important security notes**
- Only chat IDs listed in `ADMIN_CHAT_IDS` can interact with the bot.
- Service start/stop/restart is restricted to the hard-coded allow-list.
- Reboot and shutdown always require an explicit confirmation button.

---

## File layout

```
Pi_Monitor/
├── admin_script.py      # Main bot script
├── requirements.txt
├── README.md
├── logs/                # Created automatically – daily YYYY-MM-DD.log files
└── .admin_state.json    # Temporary state file used for boot-notification detection
```

---

## Customization tips

- Change `MANAGED_SERVICE` / `ALLOWED_SERVICES` / `MONITORED_SERVICES` to suit your needs.
- Adjust `LOG_LINES` and `LOG_RETENTION_DAYS` if you want more/less log history.
- The bot works on most Linux systems that use systemd; temperature reading uses the Raspberry Pi thermal zone path and may return `N/A` on other hardware.

---

## Credits

Originally written for Raspberry Pi OS. Tested in Raspberry Pi 3 B.
Maintained by [mrSnow-tr](https://github.com/mrSnow-tr).
