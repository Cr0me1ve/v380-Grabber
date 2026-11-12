#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

if [ "$(id -u)" -eq 0 ]; then
    printf '%s\n' "Run this script as the same non-root user that will run Docker Compose."
    exit 1
fi

mkdir -p secrets data/cameras
chmod 700 secrets data data/cameras

TOKEN_FILE=secrets/telegram_bot_token
if [ -e "$TOKEN_FILE" ]; then
    printf '%s\n' "The Telegram token file already exists; it was left unchanged."
    exit 1
fi

printf '%s' "Telegram bot token: "
stty -echo < /dev/tty
trap 'stty echo < /dev/tty 2>/dev/null || true' EXIT HUP INT TERM
IFS= read -r TOKEN_VALUE < /dev/tty || true
stty echo < /dev/tty
trap - EXIT HUP INT TERM
printf '\n'

case "$TOKEN_VALUE" in
    *:*) ;;
    *) unset TOKEN_VALUE; printf '%s\n' "The token format is invalid; no file was created."; exit 1 ;;
esac

umask 077
if ! (set -C; printf '%s' "$TOKEN_VALUE" > "$TOKEN_FILE"); then
    unset TOKEN_VALUE
    printf '%s\n' "Could not create the token file; no existing file was changed."
    exit 1
fi
unset TOKEN_VALUE
chmod 600 "$TOKEN_FILE"
printf '%s\n' "Token stored in secrets/telegram_bot_token. This path is ignored by Git."
