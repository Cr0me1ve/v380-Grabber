"""Install the service from this checkout without copying application source."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
if ' ' in str(root):raise SystemExit('Install under a path without spaces, such as /opt/v380-grabber')
unit=f'''[Unit]
Description=Telegram-managed V380 Grabber NVR
After=network-online.target docker.service
Wants=network-online.target docker.service
[Service]
Type=simple
WorkingDirectory={root}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=DATA_DIR={root}/data
EnvironmentFile=-{root}/.env
ExecStart={root}/.venv/bin/python {root}/bot.py serve
Restart=always
RestartSec=5
TimeoutStopSec=90
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={root}/data
[Install]
WantedBy=multi-user.target
'''
Path('/etc/systemd/system/v380-grabber.service').write_text(unit)
