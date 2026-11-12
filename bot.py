#!/usr/bin/env python3
"""Private Telegram setup bot for local V380 receiver child processes."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from auth_backend import AccountBackendError, AuthorizationBackend, BootstrapValidationError


MAX_CAMERAS = 1
MAX_BOOTSTRAP_BYTES = 64 * 1024
CLAIM_TTL_SECONDS = 15 * 60
LONG_POLL_SECONDS = 20
HEALTH_TIMEOUT_SECONDS = 1.5
QR_LOGIN_TIMEOUT_SECONDS = 180
QR_POLL_INTERVAL_SECONDS = 3

MENU = {
    "keyboard": [
        [{"text": "Login with V380 QR"}],
        [{"text": "Add camera"}, {"text": "Status"}],
        [{"text": "Check stream"}, {"text": "Edit bootstrap"}],
        [{"text": "Restart camera"}, {"text": "Recording"}],
        [{"text": "Settings"}, {"text": "Web viewer"}, {"text": "Help"}],
    ],
    "resize_keyboard": True,
}

MENU_ACTIONS = {
    "login with v380 qr": "/login",
    "add camera": "/add",
    "status": "/status",
    "check stream": "/check",
    "ping": "/ping",
    "edit bootstrap": "/edit",
    "restart receiver": "/restart",
    "restart camera": "/restart",
    "recording": "/recording",
    "help": "/help",
    "settings": "/settings",
    "web viewer": "/web",
    "cancel": "/cancel",
}


class BotError(RuntimeError):
    """An API or local operation failed; messages must never contain secrets."""


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class StateStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.camera_dir = self.data_dir / "cameras"
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.camera_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        os.chmod(self.camera_dir, 0o700)
        self.state_path = self.data_dir / "bot-state.json"
        self.claim_path = self.data_dir / "claim.json"
        if self.state_path.exists():
            os.chmod(self.state_path, 0o600)
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raise BotError("Local bot state is unreadable. Preserve the data directory and inspect it locally.") from None
            if not isinstance(self.state, dict):
                raise BotError("Local bot state is invalid. Preserve the data directory and inspect it locally.")
        else:
            self.state = {"owner": None, "cameras": [], "offset": 0}
            self.save()

    def save(self) -> None:
        raw = json.dumps(self.state, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        atomic_write(self.state_path, raw)

    @property
    def owner(self) -> dict[str, int] | None:
        value = self.state.get("owner")
        return value if isinstance(value, dict) else None

    @property
    def cameras(self) -> list[dict[str, Any]]:
        value = self.state.get("cameras", [])
        return value if isinstance(value, list) else []

    @property
    def offset(self) -> int:
        value = self.state.get("offset", 0)
        return value if isinstance(value, int) and value >= 0 else 0

    def set_offset(self, value: int) -> None:
        self.state["offset"] = max(0, int(value))
        self.save()

    def issue_claim(self, now: float | None = None) -> str:
        if self.owner:
            raise BotError("An owner is already paired. Local owner reset is required before pairing again.")
        code = secrets.token_urlsafe(24)
        claim = {
            "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "expires_at": int((now if now is not None else time.time()) + CLAIM_TTL_SECONDS),
        }
        atomic_write(self.claim_path, json.dumps(claim, separators=(",", ":")).encode("utf-8"))
        return code

    def claim_owner(self, code: str, chat_id: int, user_id: int, now: float | None = None) -> bool:
        if self.owner or not self.claim_path.exists():
            return False
        try:
            claim = json.loads(self.claim_path.read_text(encoding="utf-8"))
            expires = int(claim["expires_at"])
            expected = str(claim["sha256"])
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        current = int(now if now is not None else time.time())
        if current > expires:
            try:
                self.claim_path.unlink()
            except OSError:
                pass
            return False
        actual = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual, expected):
            return False
        self.state["owner"] = {"chat_id": int(chat_id), "user_id": int(user_id)}
        self.save()
        try:
            self.claim_path.unlink()
        except OSError:
            pass
        return True

    def camera_config_path(self, slot: int) -> Path:
        if slot not in (1, 2):
            raise BotError("Camera slot is invalid.")
        return self.camera_dir / f"camera-{slot}.json"

    def save_camera_config(self, slot: int, config: dict[str, Any]) -> None:
        raw = json.dumps(config, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        atomic_write(self.camera_config_path(slot), raw)

    def load_camera_config(self, slot: int) -> dict[str, Any] | None:
        path = self.camera_config_path(slot)
        try:
            os.chmod(path, 0o600)
            result = json.loads(path.read_text(encoding="utf-8"))
            return result if isinstance(result, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def remove_camera(self, slot: int) -> None:
        path = self.camera_config_path(slot)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.state["cameras"] = [camera for camera in self.cameras if camera.get("slot") != slot]
        self.save()


class TelegramAPI:
    """Small Bot API client. It deliberately suppresses exception details and URLs."""

    def __init__(self, token: str, opener: Callable[..., Any] | None = None):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{10,}", token):
            raise BotError("Telegram bot token is missing or malformed.")
        self._token = token
        self._opener = opener or urllib.request.urlopen

    @classmethod
    def from_secret_file(cls, path: str | Path) -> "TelegramAPI":
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            raise BotError("Telegram token file is missing or unreadable.") from None
        return cls(token)

    def _json_call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=LONG_POLL_SECONDS + 10) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except Exception:
            raise BotError("Telegram API request failed.") from None
        if len(raw) > 2 * 1024 * 1024:
            raise BotError("Telegram API response was too large.")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BotError("Telegram API returned an invalid response.") from None
        if not isinstance(envelope, dict) or not envelope.get("ok"):
            raise BotError("Telegram API rejected the request.")
        return envelope.get("result")

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        result = self._json_call(
            "getUpdates",
            {"offset": offset, "timeout": LONG_POLL_SECONDS, "allowed_updates": ["message"]},
        )
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: int,
        text: str,
        menu: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        elif menu:
            payload["reply_markup"] = MENU
        self._json_call("sendMessage", payload)

    def send_photo(self, chat_id: int, image: bytes, caption: str) -> dict[str, Any]:
        boundary = "----v380grabber" + secrets.token_hex(16)
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{('frame.jpg' if image.startswith(bytes([255,216])) else 'login.png')}\"\r\nContent-Type: {('image/jpeg' if image.startswith(bytes([255,216])) else 'image/png')}\r\n\r\n".encode(),
            image,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendPhoto",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except Exception:
            raise BotError("Telegram could not send the image.") from None
        if len(raw) > 2 * 1024 * 1024:
            raise BotError("Telegram returned an oversized response.")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BotError("Telegram returned an invalid response.") from None
        if not isinstance(envelope, dict) or not envelope.get("ok") or not isinstance(envelope.get("result"), dict):
            raise BotError("Telegram rejected the login QR image.")
        return envelope["result"]

    def send_video(self, chat_id: int, path: str | Path, caption: str) -> dict[str, Any]:
        video_path = Path(path)
        try:
            size = video_path.stat().st_size
            if not 1024 < size <= 48 * 1024 * 1024:
                raise BotError("The queued clip is outside the supported size limit.")
            video = video_path.read_bytes()
        except BotError:
            raise
        except OSError:
            raise BotError("The queued clip could not be read.") from None
        boundary = "----v380grabber" + secrets.token_hex(16)
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"supports_streaming\"\r\n\r\ntrue\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"motion-clip.mp4\"\r\nContent-Type: video/mp4\r\n\r\n".encode(),
            video,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendVideo",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=120) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except Exception:
            raise BotError("Telegram could not deliver the motion clip.") from None
        if len(raw) > 2 * 1024 * 1024:
            raise BotError("Telegram returned an oversized response.")
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BotError("Telegram returned an invalid response.") from None
        if not isinstance(envelope, dict) or not envelope.get("ok") or not isinstance(envelope.get("result"), dict):
            raise BotError("Telegram rejected the motion clip.")
        return envelope["result"]

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self._json_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except BotError:
            pass

    def download_document(self, file_id: str, reported_size: int | None = None) -> bytes:
        if reported_size is not None and reported_size > MAX_BOOTSTRAP_BYTES:
            raise BotError("Bootstrap file is too large; maximum size is 64 KiB.")
        file_info = self._json_call("getFile", {"file_id": file_id})
        if not isinstance(file_info, dict):
            raise BotError("Could not retrieve the bootstrap file.")
        file_path = file_info.get("file_path")
        if not isinstance(file_path, str) or not file_path or ".." in file_path.split("/"):
            raise BotError("Could not retrieve the bootstrap file.")
        quoted_path = urllib.parse.quote(file_path, safe="/")
        url = f"https://api.telegram.org/file/bot{self._token}/{quoted_path}"
        try:
            with self._opener(url, timeout=30) as response:
                raw = response.read(MAX_BOOTSTRAP_BYTES + 1)
        except Exception:
            raise BotError("Could not retrieve the bootstrap file.") from None
        if len(raw) > MAX_BOOTSTRAP_BYTES:
            raise BotError("Bootstrap file is too large; maximum size is 64 KiB.")
        return raw


from nvr_runtime import ReceiverManager, RecordingManager


class V380Bot:
    def __init__(
        self,
        store: StateStore,
        api: Any,
        manager: Any,
        auth_backend: AuthorizationBackend | None = None,
        recordings: Any | None = None,
    ):
        self.store = store
        self.api = api
        self.manager = manager
        self.auth_backend = auth_backend or AuthorizationBackend()
        self.recordings = recordings
        self.dialogs: dict[int, tuple[str, int | None]] = {}
        self.qr_events: dict[int, threading.Event] = {}
        self.qr_pending: dict[int, dict[str, Any]] = {}
        self.qr_lock = threading.RLock()

    @staticmethod
    def _private_message(message: dict[str, Any]) -> tuple[int, int] | None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot"):
            return None
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return None
        return chat_id, user_id

    def _send(
        self,
        chat_id: int,
        text: str,
        menu: bool = False,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        try:
            self.api.send_message(chat_id, text, menu=menu, reply_markup=reply_markup)
            return True
        except BotError:
            return False
        except Exception:
            return False

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        private = self._private_message(message)
        if private is None:
            return
        chat_id, user_id = private
        owner = self.store.owner
        text = message.get("text") if isinstance(message.get("text"), str) else ""
        document = message.get("document") if isinstance(message.get("document"), dict) else None
        command_text = text.strip()
        is_claim = command_text.lower().startswith("/claim")
        owns_chat = owner is None or (owner.get("chat_id") == chat_id and owner.get("user_id") == user_id)

        try:
            if owner is None:
                self._handle_unpaired(chat_id, user_id, command_text)
                return
            if not owns_chat:
                return
            if document is not None:
                self._handle_document(chat_id, message, document)
            else:
                self._handle_text(chat_id, command_text)
        finally:
            message_id = message.get("message_id")
            should_delete = is_claim and owner is None
            should_delete = should_delete or (owns_chat and (document is not None or is_claim or command_text.lower().startswith("/web_password ")))
            if should_delete and isinstance(message_id, int):
                try:
                    self.api.delete_message(chat_id, message_id)
                except BotError:
                    pass

    def _handle_unpaired(self, chat_id: int, user_id: int, text: str) -> None:
        if text == "/start" or text == "/help":
            self._send(
                chat_id,
                "This bot has no paired owner yet. On the server, generate a one-time code with "
                "./scripts/compose run --rm app issue-claim, start the service, then send /claim CODE here.",
            )
            return
        pieces = text.split(maxsplit=1)
        if pieces and pieces[0].split("@", 1)[0].lower() == "/claim" and len(pieces) == 2:
            if self.store.claim_owner(pieces[1].strip(), chat_id, user_id):
                self.dialogs.pop(chat_id, None)
                self._send(chat_id, "Owner paired. All bot controls are restricted to this private chat.", menu=True)
            else:
                self._send(chat_id, "That claim code is invalid or expired. Generate a new code on the server.")

    def _handle_text(self, chat_id: int, text: str) -> None:
        if not text:
            return
        selection = re.fullmatch(r"select camera\s+(\d{1,2})", text, re.IGNORECASE)
        mapped = f"/select {selection.group(1)}" if selection else MENU_ACTIONS.get(text.casefold(), text)
        parts = mapped.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].casefold()
        argument = parts[1].strip() if len(parts) == 2 else ""

        if command in ("/start", "/help"):
            self._send(chat_id, self._help_text(), menu=True)
        elif command == "/add":
            if len(self.store.cameras) >= MAX_CAMERAS:
                self._send(chat_id, "A camera is already configured. Use /login 1 to replace or refresh it. Edit or remove a camera first.")
            else:
                self.dialogs[chat_id] = ("add", None)
                self._send(chat_id, self._import_prompt())
        elif command == "/edit":
            slot = self._resolve_slot(argument)
            if slot is None or not any(camera.get("slot") == slot for camera in self.store.cameras):
                self._send(chat_id, "Use /edit 1 or /edit 2 for an existing camera.")
            else:
                self.dialogs[chat_id] = ("edit", slot)
                self._send(chat_id, f"Upload the replacement authorized bootstrap JSON for camera {slot} as a document.")
        elif command in ("/status", "/check", "/ping"):
            if hasattr(self.manager,"ping"):
                self.manager.ping(chat_id)
                return
            self._send_status(chat_id, command in ("/check", "/ping"), argument)
        elif command == "/restart":
            slot = self._resolve_slot(argument)
            if slot is None:
                self._send(chat_id, "Use /restart 1 or /restart 2.")
            elif not any(camera.get("slot") == slot for camera in self.store.cameras):
                self._send(chat_id, f"Camera {slot} is not configured.")
            elif self.manager.restart(slot):
                self._send(chat_id, f"Camera {slot} receiver restarted. Use /status {slot} to verify frames.")
            else:
                self._send(chat_id, f"Camera {slot} receiver could not be started.")
        elif command == "/remove":
            slot = self._resolve_slot(argument)
            if slot is None or not any(camera.get("slot") == slot for camera in self.store.cameras):
                self._send(chat_id, "Use /remove 1 or /remove 2 for a configured camera.")
            else:
                self.dialogs[chat_id] = ("remove", slot)
                self._send(chat_id, f"Remove camera {slot} and its local credential file? Send /confirm or /cancel.")
        elif command == "/confirm":
            dialog = self.dialogs.get(chat_id)
            if not dialog or dialog[0] != "remove" or dialog[1] not in (1, 2):
                self._send(chat_id, "There is no pending removal. Use /remove 1 or /remove 2 first.")
            else:
                slot = dialog[1]
                if self.recordings is not None:
                    self.recordings.stop_slot(slot)
                self.manager.stop(slot)
                self.store.remove_camera(slot)
                self.dialogs.pop(chat_id, None)
                self._send(chat_id, f"Camera {slot} was removed from the local configuration.", menu=True)
        elif command == "/cancel":
            self._cancel_login(chat_id)
            self.dialogs.pop(chat_id, None)
            self._send(chat_id, "Pending action cancelled.", menu=True)
        elif command in ("/settings","/duration","/clip_seconds","/receivers","/pre_roll","/storage_gb","/reserve_gb","/notifications","/outage_alerts","/web","/web_password"):
            self._settings_command(chat_id,command,argument)
        elif command == "/recording":
            self._handle_recording(chat_id, argument)
        elif command == "/sensitivity":
            self._handle_sensitivity(chat_id, argument)
        elif command == "/retention":
            self._handle_retention(chat_id, argument)
        elif command == "/login":
            if argument and self._parse_slot(argument) is None:
                self._send(chat_id, "Use /login to add a camera or /login 1 to refresh or replace that receiver.")
            else:
                target_slot = self._parse_slot(argument) if argument else None
                if target_slot and not any(camera.get("slot") == target_slot for camera in self.store.cameras):
                    self._send(chat_id, f"Camera slot {target_slot} is not configured. Use /login to add a camera.")
                elif target_slot is None and len(self.store.cameras) >= MAX_CAMERAS:
                    self._send(chat_id, "A camera is already configured. Use /login 1 to replace or refresh it. Use /login 1 or /login 2 to refresh one, or remove a camera first.")
                else:
                    self._start_qr_login(chat_id, target_slot)
        elif command == "/select":
            self._select_qr_camera(chat_id, argument)
        else:
            self._send(chat_id, self._help_text(), menu=True)

    def _handle_document(self, chat_id: int, message: dict[str, Any], document: dict[str, Any]) -> None:
        dialog = self.dialogs.get(chat_id)
        if not dialog or dialog[0] not in ("add", "edit"):
            self._send(chat_id, "Start with /add or /edit 1 before uploading a bootstrap file.")
            return
        file_id = document.get("file_id")
        size = document.get("file_size")
        if not isinstance(file_id, str) or not file_id:
            self._send(chat_id, "Telegram did not provide a usable file reference. Try uploading the JSON again.")
            return
        try:
            raw = self.api.download_document(file_id, size if isinstance(size, int) else None)
            config = self.auth_backend.import_bootstrap(raw)
        except BootstrapValidationError as exc:
            names = ", ".join(exc.fields)
            self._send(chat_id, f"Bootstrap validation failed for: {names}. Values are not shown. Upload a corrected authorized JSON file.")
            return
        except BotError as exc:
            self._send(chat_id, str(exc))
            return
        except Exception:
            self._send(chat_id, "The bootstrap file could not be read. Upload a UTF-8 JSON document under 64 KiB.")
            return

        self._commit_camera_config(chat_id, dialog[0], dialog[1], config, source="bootstrap import")

    def _commit_camera_config(
        self,
        chat_id: int,
        action: str,
        requested_slot: int | None,
        config: dict[str, Any],
        source: str,
    ) -> None:
        cameras = list(self.store.cameras)
        if action == "add":
            slot = next((candidate for candidate in range(1,MAX_CAMERAS+1) if all(c.get("slot") != candidate for c in cameras)), None)
            if slot is None:
                self.dialogs.pop(chat_id, None)
                self._send(chat_id, "A camera is already configured. Use /login 1 to replace or refresh it.")
                return
        else:
            slot = requested_slot
            if slot not in (1, 2) or not any(c.get("slot") == slot for c in cameras):
                self.dialogs.pop(chat_id, None)
                self._send(chat_id, "That camera is no longer configured. Start /edit again.")
                return

        for existing in cameras:
            other_slot = existing.get("slot")
            if other_slot == slot:
                continue
            other_config = self.store.load_camera_config(other_slot) if other_slot in (1, 2) else None
            if other_config and other_config.get("device_id") == config.get("device_id"):
                self._send(chat_id, "That device is already configured in the other receiver slot.")
                return

        try:
            self.store.save_camera_config(slot, config)
            if action == "add":
                cameras.append(
                    {
                        "slot": slot,
                        "name": f"Camera {slot}",
                                            }
                )
                self.store.state["cameras"] = sorted(cameras, key=lambda camera: camera["slot"])
                self.store.save()
                started = self.manager.start(slot)
            else:
                started = self.manager.restart(slot)
        except Exception:
            self._send(chat_id, "Configuration could not be saved safely. Existing setup was left unchanged where possible.")
            return

        self.dialogs.pop(chat_id, None)
        source_text = "Bootstrap import" if source == "bootstrap import" else source
        if started:
            if self.recordings is not None:
                try:
                    self.recordings.ensure_slot(slot)
                except Exception:
                    pass
            self._send(
                chat_id,
                f"{source_text} completed for camera {slot}; the receiver process started. Use /check {slot} to verify a live frame.",
                menu=True,
            )
        else:
            self._send(chat_id, f"{source_text} completed for camera {slot}, but the receiver process could not start. Use /restart {slot} after checking the server.", menu=True)

    def _start_qr_login(self, chat_id: int, target_slot: int | None = None) -> None:
        with self.qr_lock:
            active = self.qr_events.get(chat_id)
            if active is not None and not active.is_set():
                self._send(chat_id, "A V380 login QR is already active. Scan it or send /cancel.")
                return
            event = threading.Event()
            self.qr_events[chat_id] = event
            self.qr_pending.pop(chat_id, None)
        self.dialogs.pop(chat_id, None)
        self._send(chat_id, "Preparing a short-lived V380 login QR. Keep the photo private.")
        worker = threading.Thread(
            target=self._qr_login_worker,
            args=(chat_id, event, target_slot),
            daemon=True,
            name="v380-qr-login",
        )
        worker.start()

    def _cancel_login(self, chat_id: int) -> None:
        with self.qr_lock:
            event = self.qr_events.get(chat_id)
            if event is not None:
                event.set()
            self.qr_pending.pop(chat_id, None)

    @staticmethod
    def _qr_png(payload: str) -> bytes:
        if not isinstance(payload, str) or not payload.startswith("V380LOGIN:") or len(payload) > 512:
            raise BotError("The account backend returned an invalid QR payload.")
        try:
            import qrcode
            from io import BytesIO

            image = qrcode.make(payload, box_size=8, border=4)
            output = BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except Exception:
            raise BotError("The login QR image could not be created.") from None

    @staticmethod
    def _safe_device_label(value: Any, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        cleaned = " ".join("".join(ch if ord(ch) >= 32 and ord(ch) != 127 else " " for ch in value).split())
        return cleaned[:56] or fallback

    def _qr_login_worker(self, chat_id: int, cancel: threading.Event, target_slot: int | None = None) -> None:
        qr_message_id: int | None = None
        try:
            session = self.auth_backend.start_qr()
            payload = session.get("payload") if isinstance(session, dict) else None
            png = self._qr_png(payload)
            photo = self.api.send_photo(chat_id, png, "Scan this code with your authorized V380 app. The code expires shortly.")
            if isinstance(photo, dict) and isinstance(photo.get("message_id"), int):
                qr_message_id = photo["message_id"]
            deadline = time.monotonic() + QR_LOGIN_TIMEOUT_SECONDS
            last_notice = None
            while not cancel.is_set() and time.monotonic() < deadline:
                result = self.auth_backend.poll_qr(session)
                state = result.get("state") if isinstance(result, dict) else None
                if state == "confirmed":
                    account = result.get("account")
                    token = account.get("token") if isinstance(account, dict) else None
                    if not isinstance(token, str):
                        raise AccountBackendError("The account login response was incomplete.")
                    devices = self.auth_backend.list_cameras(token)
                    choices: list[dict[str, Any]] = []
                    for device in devices:
                        if not isinstance(device, dict):
                            continue
                        device_id = device.get("device_id")
                        public_key = device.get("device_public_key")
                        if not isinstance(device_id, int) or not isinstance(public_key, str):
                            continue
                        choices.append(
                            {
                                "device_id": device_id,
                                "device_public_key": public_key,
                                "name": self._safe_device_label(device.get("name"), "Camera"),
                                "model": self._safe_device_label(device.get("model"), ""),
                            }
                        )
                    if not choices:
                        self._send(chat_id, "V380 login succeeded, but no compatible camera was returned for this account.")
                        return
                    with self.qr_lock:
                        if cancel.is_set():
                            return
                        self.qr_pending[chat_id] = {"token": token, "cameras": choices, "target_slot": target_slot}
                    rows = []
                    lines = ["V380 login approved. Select a camera to configure:"]
                    more_cameras = len(choices) > 20
                    choices = choices[:20]
                    for index, device in enumerate(choices, start=1):
                        label = device["name"]
                        if device["model"]:
                            label += " — " + device["model"]
                        lines.append(f"{index}. {label}")
                        rows.append([{"text": f"Select camera {index}"}])
                    if more_cameras:
                        lines.append("Only the first 20 cameras are shown.")
                    rows.append([{"text": "Cancel"}])
                    self._send(
                        chat_id,
                        "\n".join(lines),
                        reply_markup={"keyboard": rows, "resize_keyboard": True, "one_time_keyboard": True},
                    )
                    return
                if state == "expired":
                    self._send(chat_id, "The V380 login QR expired. Send /login to try again.")
                    return
                if state not in ("pending", "scanned"):
                    raise AccountBackendError("The V380 login service returned an unexpected state.")
                if state != last_notice:
                    if state == "scanned":
                        self._send(chat_id, "The QR was scanned. Approve the login in your V380 app.")
                    last_notice = state
                cancel.wait(QR_POLL_INTERVAL_SECONDS)
            if not cancel.is_set():
                self._send(chat_id, "The V380 login QR timed out. Send /login to try again.")
        except AccountBackendError as exc:
            self._send(chat_id, f"V380 login could not continue: {exc}")
        except BotError as exc:
            self._send(chat_id, str(exc))
        except Exception:
            self._send(chat_id, "V380 login could not continue. No account credential was saved.")
        finally:
            if qr_message_id is not None:
                try:
                    self.api.delete_message(chat_id, qr_message_id)
                except Exception:
                    pass
            with self.qr_lock:
                if self.qr_events.get(chat_id) is cancel:
                    self.qr_events.pop(chat_id, None)

    def _select_qr_camera(self, chat_id: int, argument: str) -> None:
        try:
            index = int(argument)
        except (TypeError, ValueError):
            index = 0
        with self.qr_lock:
            pending = self.qr_pending.get(chat_id)
        if not pending or not 1 <= index <= len(pending.get("cameras", [])):
            self._send(chat_id, "There is no pending V380 camera selection. Send /login to start again.")
            return
        camera = pending["cameras"][index - 1]
        token = pending["token"]
        try:
            raw_config = self.auth_backend.build_camera_credentials(token, camera)
            config = self.auth_backend.import_bootstrap(json.dumps(raw_config).encode("utf-8"))
        except BootstrapValidationError as exc:
            self._send(chat_id, "The selected camera bootstrap failed validation for: " + ", ".join(exc.fields) + ".")
            return
        except AccountBackendError as exc:
            self._send(chat_id, f"V380 could not prepare this camera: {exc}")
            return
        except Exception:
            self._send(chat_id, "V380 could not prepare this camera. The account token was not saved.")
            return
        with self.qr_lock:
            self.qr_pending.pop(chat_id, None)
        target_slot = pending.get("target_slot")
        action = "edit" if target_slot in (1, 2) else "add"
        self._commit_camera_config(chat_id, action, target_slot, config, source="V380 QR setup")

    def _target_slots(self, argument: str) -> list[int]:
        if argument:
            slot = self._parse_slot(argument)
            return [slot] if slot is not None and any(camera.get("slot") == slot for camera in self.store.cameras) else []
        return sorted(
            camera["slot"]
            for camera in self.store.cameras
            if camera.get("slot") in (1, 2)
        )

    def _settings_command(self,chat_id,command,argument):
        runtime=getattr(self.manager,"runtime",None)
        if runtime is None:
            self._send(chat_id,"NVR runtime is unavailable.");return
        if command=="/settings":
            values=runtime.settings()
            self._send(chat_id,"\n".join(["Camera settings:",f"/duration {values['duration']} — clip seconds (5–120)",f"/receivers {values['receivers']} — parallel receivers (1–4)",f"/pre_roll {values['pre_roll']} — history seconds (0–10)",f"/sensitivity {values['sensitivity']} — low, medium, high",f"/retention {values['retention_days']} — archive days (1–30)",f"/storage_gb {values['storage_gb']} — archive size limit",f"/reserve_gb {values['reserve_gb']} — free disk reserve",f"/recording {'on' if values['enabled'] else 'off'}",f"/notifications {'on' if values['notifications'] else 'off'} — motion videos",f"/outage_alerts {'on' if values['outage_alerts'] else 'off'}","/web — viewer and login","/web_password PASSWORD — change web password"]));return
        if command=="/web":runtime.web(chat_id);return
        if command=="/web_password":
            try:runtime.web_password(argument)
            except ValueError as error:self._send(chat_id,str(error));return
            self._send(chat_id,"Web password updated. The viewer is reconnecting.");return
        from nvr_config import validate_setting
        key=command[1:];key='duration' if key=='clip_seconds' else key
        try:value=validate_setting(key,argument)
        except ValueError as error:self._send(chat_id,str(error));return
        try:runtime.set_setting(key,value)
        except ValueError as error:self._send(chat_id,str(error));return
        self._send(chat_id,key+" updated to "+str(value)+". Existing queued clips keep their original settings.")

    def _handle_recording(self, chat_id: int, argument: str) -> None:
        if self.recordings is None:
            self._send(chat_id, "Motion recording is unavailable in this build.")
            return
        parts = argument.split()
        if not parts:
            slots = self._target_slots("")
            if not slots:
                self._send(chat_id, "No cameras are configured. Use /login or /add first.")
                return
            lines = []
            for slot in slots:
                status = self.recordings.status(slot)
                setting = self.recordings.settings(slot)
                enabled = "on" if setting["enabled"] else "off"
                source = "stream healthy" if status.get("source_healthy") else "waiting for stream"
                recording = ", motion detected" if status.get("recording") else ""
                lines.append(
                    f"Camera {slot}: {enabled}; sensitivity {setting['sensitivity']}; "
                    f"retention {setting['retention_days']} days; {source}{recording}; "
                    f"queued clips {status.get('queued', 0)}."
                )
            self._send(chat_id, "\n".join(lines), menu=True)
            return
        action = parts[0].casefold()
        slot_argument = parts[1] if len(parts) == 2 else ""
        if action not in ("on", "off") or len(parts) > 2:
            self._send(chat_id, "Use /recording on [1], /recording off [1], or /recording to view status.")
            return
        slots = self._target_slots(slot_argument)
        if not slots:
            self._send(chat_id, "No matching camera is configured. Use /recording on, /recording off, or include camera 1 or 2.")
            return
        enabled = action == "on"
        for slot in slots:
            self.recordings.set_setting(slot, "enabled", enabled)
        state = "enabled" if enabled else "disabled; any unfinished clip will be cancelled"
        self._send(chat_id, f"Motion recording {state} for camera(s) {', '.join(map(str, slots))}. Pending clips remain queued for delivery.", menu=True)

    def _handle_sensitivity(self, chat_id: int, argument: str) -> None:
        if self.recordings is None:
            self._send(chat_id, "Motion recording is unavailable in this build.")
            return
        parts = argument.split()
        if not parts or parts[0].casefold() not in ("low", "medium", "high") or len(parts) > 2:
            self._send(chat_id, "Use /sensitivity low|medium|high [1]. With no camera number, the setting applies to all cameras.")
            return
        slots = self._target_slots(parts[1] if len(parts) == 2 else "")
        if not slots:
            self._send(chat_id, "No matching camera is configured.")
            return
        value = parts[0].casefold()
        for slot in slots:
            self.recordings.set_setting(slot, "sensitivity", value)
        self._send(chat_id, f"Motion sensitivity set to {value} for camera(s) {', '.join(map(str, slots))}.", menu=True)

    def _handle_retention(self, chat_id: int, argument: str) -> None:
        if self.recordings is None:
            self._send(chat_id, "Motion recording is unavailable in this build.")
            return
        parts = argument.split()
        if not parts or len(parts) > 2 or not parts[0].isdigit():
            self._send(chat_id, "Use /retention DAYS [1], where DAYS is from 1 to 30. With no camera number, the setting applies to all cameras.")
            return
        days = int(parts[0])
        if not 1 <= days <= 30:
            self._send(chat_id, "Retention must be from 1 to 30 days.")
            return
        slots = self._target_slots(parts[1] if len(parts) == 2 else "")
        if not slots:
            self._send(chat_id, "No matching camera is configured.")
            return
        for slot in slots:
            self.recordings.set_setting(slot, "retention_days", days)
        self._send(chat_id, f"Motion clip retention set to {days} days for camera(s) {', '.join(map(str, slots))}.", menu=True)

    @staticmethod
    def _parse_slot(argument: str) -> int | None:
        if argument in ("1", "2"):
            return int(argument)
        return None

    def _resolve_slot(self, argument: str) -> int | None:
        if argument:
            return self._parse_slot(argument)
        configured = [camera.get("slot") for camera in self.store.cameras if camera.get("slot") in (1, 2)]
        return configured[0] if len(configured) == 1 else None

    def _send_status(self, chat_id: int, check: bool, argument: str) -> None:
        cameras = self.store.cameras
        if not cameras:
            self._send(chat_id, "No cameras are configured. Send /login to connect one with a V380 QR.")
            return
        slot = self._parse_slot(argument) if argument else None
        if argument and slot is None:
            self._send(chat_id, "Use /status or /status 1.")
            return
        selected = [camera for camera in cameras if slot is None or camera.get("slot") == slot]
        lines: list[str] = []
        for camera in selected:
            current = camera.get("slot")
            if current not in (1, 2):
                continue
            info = self.manager.health(current)
            if info.get("healthy"):
                state = f"live frames, {info.get('fps', 0):g} fps"
            elif info.get("process") == "running":
                state = "receiver running; waiting for live frames"
            elif info.get("process") == "exited":
                state = "receiver process exited; use /restart"
            else:
                state = "receiver stopped"
            label = "Check" if check else "Status"
            lines.append(f"{label} camera {current}: {state}. Local stream: http://127.0.0.1:{8080 + current}/snapshot.jpg")
        self._send(chat_id, "\n".join(lines) if lines else "No camera receiver was found.", menu=True)

    @staticmethod
    def _import_prompt() -> str:
        return (
            "Interactive setup is available with /login. To use an existing authorized bootstrap instead, "
            "upload its JSON as a document. It must include the account token, "
            "device ID, device public key, SDK signature suffix, regional controller IP, and cached relays. "
            "The bot checks fields without printing values, saves the normalized file locally, then starts a receiver. "
            "The bot attempts to delete the uploaded message after processing."
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "Private V380 NVR controls:\n"
            "/settings — all camera and recording settings\n"
            "/web — web viewer and login\n"
            "/add — import an authorized bootstrap for a camera\n"
            "/login — sign in with a one-time V380 QR and select a camera\n"
            "/login 1 — refresh or replace that receiver by QR\n"
            "/status [1] — show receiver and frame health\n"
            "/check or /ping [1] — check the local health endpoint\n"
            "/edit 1 — replace a camera bootstrap\n"
            "/restart [1] — restart a receiver\n"
            "/remove 1 — remove after /confirm\n"
            "/recording [on|off] [1] — control motion recording\n"
            "/sensitivity low|medium|high [1] — tune motion detection\n"
            "/retention DAYS [1] — keep clips for 1 to 30 days\n"
            "/cancel — cancel a pending dialog"
        )


def _monitor_receivers(manager: ReceiverManager, stop_event: threading.Event) -> None:
    manager.ensure_all(stop_event)


def run_bot(
    api: TelegramAPI,
    store: StateStore,
    manager: ReceiverManager,
    auth_backend: AuthorizationBackend | None = None,
    recordings: RecordingManager | None = None,
) -> None:
    from nvr_runtime import NvrRuntime
    runtime=NvrRuntime(api,store)
    manager=runtime.manager
    recordings=runtime.recordings
    bot = V380Bot(store, api, manager, auth_backend, recordings)
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    manager.start_all()
    recordings.start_all()
    runtime.start()
    workers = [
        threading.Thread(target=_monitor_receivers, args=(manager, stop_event), daemon=True),
    ]
    for worker in workers:
        worker.start()
    try:
        api._json_call("setMyCommands",{"commands":[{"command":command,"description":description} for command,description in [("ping","Fresh camera frame and recording health"),("settings","All camera and recording settings"),("duration","Video length in seconds, 5 to 120"),("receivers","Parallel receivers, 1 to 4"),("pre_roll","Video history, 0 to 10 seconds"),("recording","Motion recording on or off"),("sensitivity","Motion sensitivity: low, medium, high"),("retention","Archive retention in days"),("storage_gb","Archive storage limit in GB"),("reserve_gb","Free disk reserve in GB"),("notifications","Motion videos on or off"),("outage_alerts","Connection alerts on or off"),("web","Web viewer and login"),("login","Configure or refresh camera through V380 QR"),("restart","Reconnect the camera receivers")]]})
    except Exception:pass
    print("V380 Grabber bot started.")
    while not stop_event.is_set():
        try:
            updates = api.get_updates(store.offset)
        except BotError:
            stop_event.wait(3)
            continue
        for update in updates:
            if stop_event.is_set():
                break
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            try:
                bot.handle_update(update)
            except Exception:
                message = update.get("message")
                private = bot._private_message(message) if isinstance(message, dict) else None
                if private and store.owner and private[0] == store.owner.get("chat_id") and private[1] == store.owner.get("user_id"):
                    bot._send(private[0], "The request failed. No credential values were recorded in application logs.")
            if isinstance(update_id, int):
                try:
                    store.set_offset(update_id + 1)
                except Exception:
                    stop_event.set()
                    break
    for cancel in list(bot.qr_events.values()):
        cancel.set()
    runtime.shutdown()
    recordings.stop_all()
    manager.shutdown()
    for worker in workers:
        worker.join(timeout=1)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    from nvr_config import DATA
    data_dir = DATA
    try:
        store = StateStore(data_dir)
        if args == ["issue-claim"]:
            if store.owner:
                print("An owner is already paired.");return 0
            code = store.issue_claim()
            print("One-time owner claim code (valid for 15 minutes):")
            print(code)
            return 0
        if args not in ([], ["serve"]):
            print("Supported commands: serve, issue-claim")
            return 2
        token_path = os.environ.get("TELEGRAM_TOKEN_FILE",str(Path(__file__).resolve().parent/"secrets/telegram_bot_token"))
        api = TelegramAPI.from_secret_file(token_path)
        run_bot(api, store, None)
        return 0
    except BotError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
