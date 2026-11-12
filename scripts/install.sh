#!/bin/sh
set -eu
ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"
if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'Run with sudo: this installs a host service that supervises Docker.'
    exit 1
fi
command -v docker >/dev/null
docker compose version >/dev/null
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p secrets data
chmod 700 secrets data
if [ ! -f secrets/telegram_bot_token ]; then
    printf '%s' 'Telegram bot token: '
    stty -echo </dev/tty
    trap 'stty echo </dev/tty 2>/dev/null || true' EXIT HUP INT TERM
    IFS= read -r TOKEN_VALUE </dev/tty
    stty echo </dev/tty
    trap - EXIT HUP INT TERM
    printf '\n'
    (umask 077; printf '%s' "$TOKEN_VALUE" >secrets/telegram_bot_token)
    unset TOKEN_VALUE
fi
REVISION=$(git rev-parse HEAD)
.venv/bin/python - "$REVISION" <<'PYREV'
import sys
from pathlib import Path
p=Path('.env');lines=p.read_text().splitlines() if p.exists() else []
lines=[line for line in lines if not line.startswith('V380_REVISION=')]
lines.append('V380_REVISION='+sys.argv[1]);p.write_text('\n'.join(lines)+'\n')
PYREV
docker compose build
.venv/bin/python -c 'import nvr_config; nvr_config.initialize()'
.venv/bin/python scripts/install_unit.py
systemctl daemon-reload
systemctl enable --now v380-grabber.service
.venv/bin/python bot.py issue-claim
