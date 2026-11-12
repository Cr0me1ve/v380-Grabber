from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from auth_backend import AuthorizationBackend, BootstrapValidationError, validate_bootstrap
from bot import BotError, StateStore, TelegramAPI, V380Bot


def valid_bootstrap() -> dict:
    return {
        "token": "a" * 64,
        "device_id": 17,
        "device_public_key": base64.b64encode(b"k" * 32).decode("ascii"),
        "signature_suffix": "synthetic-test-sdk-suffix",
        "secret_ip": "192.0.2.14",
        "relays": [
            {
                "ip": "relay.example.invalid",
                "tp": {"call_port": 80, "trans_port": 32100},
            }
        ],
        "discarded_private_field": "must-not-be-copied",
    }


class FakeAPI:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.documents: dict[str, bytes] = {}
        self.photos: list[tuple[int, bytes, str]] = []

    def send_message(self, chat_id: int, text: str, menu: bool = False, reply_markup=None):
        self.messages.append((chat_id, text))

    def send_photo(self, chat_id: int, image: bytes, caption: str):
        self.photos.append((chat_id, image, caption))
        return {"message_id": 900}

    def delete_message(self, chat_id: int, message_id: int):
        self.deleted.append((chat_id, message_id))

    def download_document(self, file_id: str, reported_size: int | None = None):
        raw = self.documents[file_id]
        if len(raw) > 64 * 1024:
            raise BotError("Bootstrap file is too large; maximum size is 64 KiB.")
        return raw


class FakeManager:
    def __init__(self):
        self.started: list[int] = []
        self.restarted: list[int] = []
        self.stopped: list[int] = []
        self.health_values: dict[int, dict] = {}

    def start(self, slot: int) -> bool:
        self.started.append(slot)
        return True

    def restart(self, slot: int) -> bool:
        self.restarted.append(slot)
        return True

    def stop(self, slot: int) -> None:
        self.stopped.append(slot)

    def health(self, slot: int):
        return self.health_values.get(slot, {"process": "running", "healthy": True, "fps": 15.0})


class FakeRuntime:
    def __init__(self):
        from nvr_config import DEFAULTS

        self.values = dict(DEFAULTS)
        self.changes: list[tuple[str, object]] = []
        self.web_chats: list[int] = []
        self.web_passwords: list[str] = []

    def settings(self):
        return dict(self.values)

    def set_setting(self, name, value):
        self.changes.append((name, value))
        self.values[name] = value
        return True

    def web(self, chat_id):
        self.web_chats.append(chat_id)

    def web_password(self, password):
        if not 12 <= len(password) <= 128:
            raise ValueError("Use a web password containing 12 to 128 characters")
        self.web_passwords.append(password)


class FakeRecordingSettings:
    def __init__(self):
        self.values = {"enabled": True, "sensitivity": "medium", "retention_days": 7}
        self.changes: list[tuple[int, str, object]] = []

    def set_setting(self, slot, name, value):
        self.changes.append((slot, name, value))
        self.values[name] = value
        return True


class FakeAuth(AuthorizationBackend):
    def __init__(self):
        self.token = "b" * 64
        self.built_camera = None

    def start_qr(self):
        return {"payload": "V380LOGIN:synthetic-key"}

    def poll_qr(self, session):
        return {"state": "confirmed", "account": {"token": self.token}}

    def list_cameras(self, token):
        return [{"device_id": 29, "device_public_key": base64.b64encode(b"p" * 32).decode(), "name": "Entry", "model": "A2K1310S"}]

    def build_camera_credentials(self, token, camera):
        self.built_camera = camera
        config = valid_bootstrap()
        config["token"] = token
        config["device_id"] = camera["device_id"]
        config["device_public_key"] = camera["device_public_key"]
        return config


def update(message: dict) -> dict:
    return {"update_id": 1, "message": message}


def private_message(text: str = "", message_id: int = 1, chat_id: int = 100, user_id: int = 100, document=None) -> dict:
    result = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False},
        "text": text,
    }
    if document is not None:
        result["document"] = document
    return result


class CredentialValidationTests(unittest.TestCase):
    def test_valid_bootstrap_is_normalized_and_unknown_values_are_dropped(self):
        normalized = validate_bootstrap(valid_bootstrap())
        self.assertEqual(normalized["device_id"], 17)
        self.assertEqual(normalized["secret_host"], "secret.av380.net")
        self.assertEqual(normalized["call_path"], "/v1/app/call")
        self.assertNotIn("discarded_private_field", normalized)

    def test_validated_failover_addresses_are_preserved_and_bounded(self):
        raw = valid_bootstrap()
        raw["secret_ips"] = ["192.0.2.15", "192.0.2.16"]
        raw["secret_hosts"] = ["controller.example.invalid"]
        raw["dispatch_host"] = "regional-dispatch.example.invalid"
        raw["dispatch_hosts"] = ["backup-dispatch.example.invalid"]
        raw["dispatch_port"] = 8123
        normalized = validate_bootstrap(raw)
        self.assertEqual(normalized["secret_ips"], ["192.0.2.15", "192.0.2.16"])
        self.assertEqual(normalized["secret_hosts"], ["controller.example.invalid"])
        self.assertEqual(normalized["dispatch_hosts"], ["backup-dispatch.example.invalid"])
        self.assertEqual(normalized["dispatch_port"], 8123)

    def test_invalid_secret_fields_are_named_without_echoing_values(self):
        raw = valid_bootstrap()
        raw["token"] = "SENSITIVE-FAKE-TOKEN"
        raw["secret_ip"] = "not-an-ip"
        with self.assertRaises(BootstrapValidationError) as raised:
            validate_bootstrap(raw)
        message = str(raised.exception)
        self.assertIn("token", message)
        self.assertIn("secret_ip", message)
        self.assertNotIn("SENSITIVE-FAKE-TOKEN", message)
        self.assertNotIn("not-an-ip", message)

    def test_native_qr_status_mapping_handles_pending_expired_and_scanned(self):
        import account_auth

        session = {"mac": "02:00:00:00:00:01", "key": "a" * 32}
        for code, expected in ((30001, "pending"), (30002, "expired"), (30003, "scanned"), (30004, "scanned")):
            with self.subTest(code=code), patch.object(
                account_auth, "_account_request", return_value={"result": 10022, "error_code": code}
            ):
                self.assertEqual(account_auth.poll_qr(session)["state"], expected)


class PairingAndBotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "data")
        self.api = FakeAPI()
        self.manager = FakeManager()
        self.bot = V380Bot(self.store, self.api, self.manager)

    def tearDown(self):
        self.temp.cleanup()

    def test_claim_code_is_hashed_and_pairs_only_private_owner(self):
        code = self.store.issue_claim()
        claim_text = self.store.claim_path.read_text()
        self.assertNotIn(code, claim_text)
        self.assertNotIn(code, self.store.state_path.read_text())

        self.bot.handle_update(update(private_message(f"/claim {code}", message_id=42)))

        self.assertEqual(self.store.owner, {"chat_id": 100, "user_id": 100})
        self.assertFalse(self.store.claim_path.exists())
        self.assertIn((100, 42), self.api.deleted)
        self.assertTrue(any("Owner paired" in text for _, text in self.api.messages))

    def test_group_messages_and_non_owner_commands_are_ignored(self):
        code = self.store.issue_claim()
        group = private_message(f"/claim {code}")
        group["chat"] = {"id": -100, "type": "group"}
        self.bot.handle_update(update(group))
        self.assertIsNone(self.store.owner)

        self.store.claim_owner(code, 100, 100)
        before = list(self.api.messages)
        stranger = private_message("/add", chat_id=200, user_id=200)
        self.bot.handle_update(update(stranger))
        self.assertEqual(before, self.api.messages)

    def test_bootstrap_upload_is_deleted_and_saved_with_private_modes(self):
        code = self.store.issue_claim()
        self.store.claim_owner(code, 100, 100)
        payload = valid_bootstrap()
        self.api.documents["file-id"] = json.dumps(payload).encode("utf-8")
        self.bot.dialogs[100] = ("add", None)

        message = private_message(message_id=77, document={"file_id": "file-id", "file_size": 512})
        self.bot.handle_update(update(message))

        config_path = self.store.camera_config_path(1)
        self.assertTrue(config_path.is_file())
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.camera_dir.stat().st_mode), 0o700)
        saved = json.loads(config_path.read_text())
        self.assertEqual(saved["device_id"], 17)
        self.assertNotIn("discarded_private_field", saved)
        self.assertNotIn("discarded_private_field", self.store.state_path.read_text())
        self.assertEqual(self.manager.started, [1])
        self.assertIn((100, 77), self.api.deleted)
        self.assertFalse(any("a" * 64 in text for _, text in self.api.messages))

    def test_invalid_bootstrap_is_not_written_or_echoed(self):
        code = self.store.issue_claim()
        self.store.claim_owner(code, 100, 100)
        payload = valid_bootstrap()
        payload["token"] = "DO-NOT-ECHO-THIS-FAKE-VALUE"
        self.api.documents["bad-file"] = json.dumps(payload).encode("utf-8")
        self.bot.dialogs[100] = ("add", None)

        message = private_message(message_id=78, document={"file_id": "bad-file", "file_size": 512})
        self.bot.handle_update(update(message))

        self.assertFalse(self.store.camera_config_path(1).exists())
        self.assertTrue(any("token" in text for _, text in self.api.messages))
        self.assertFalse(any("DO-NOT-ECHO-THIS-FAKE-VALUE" in text for _, text in self.api.messages))

    def test_camera_status_uses_health_fields_without_showing_device_id(self):
        code = self.store.issue_claim()
        self.store.claim_owner(code, 100, 100)
        self.store.state["cameras"] = [{"slot": 1, "name": "Camera 1"}]
        self.store.save()
        self.bot.handle_update(update(private_message("/status")))
        response = self.api.messages[-1][1]
        self.assertIn("15 fps", response)
        self.assertNotIn("device_id", response)

    def test_empty_status_points_to_qr_login(self):
        code = self.store.issue_claim()
        self.store.claim_owner(code, 100, 100)
        self.bot.handle_update(update(private_message("/status")))
        response = self.api.messages[-1][1]
        self.assertIn("/login", response)
        self.assertNotIn("/add", response)
        self.assertNotIn("17", response)

    def test_qr_login_selects_camera_without_sending_or_persisting_account_token_in_state(self):
        auth = FakeAuth()
        bot = V380Bot(self.store, self.api, self.manager, auth)
        cancel = threading.Event()
        with patch.object(V380Bot, "_qr_png", return_value=b"synthetic-png"):
            bot._qr_login_worker(100, cancel)
        self.assertEqual(len(self.api.photos), 1)
        self.assertIn((100, 900), self.api.deleted)
        self.assertEqual(len(bot.qr_pending[100]["cameras"]), 1)
        self.assertNotIn(auth.token, self.store.state_path.read_text())
        self.assertFalse(any(auth.token in text for _, text in self.api.messages))

        bot._handle_text(100, "Select camera 1")

        saved = json.loads(self.store.camera_config_path(1).read_text())
        self.assertEqual(saved["token"], auth.token)
        self.assertEqual(self.manager.started, [1])
        self.assertNotIn(auth.token, self.store.state_path.read_text())
        self.assertNotIn(auth.token, "\n".join(text for _, text in self.api.messages))

    def test_qr_login_can_refresh_an_existing_receiver_slot(self):
        auth = FakeAuth()
        self.store.state["cameras"] = [
            {"slot": 1, "name": "Camera 1", "recording": {"enabled": False}}
        ]
        self.store.save()
        self.store.save_camera_config(1, valid_bootstrap())
        bot = V380Bot(self.store, self.api, self.manager, auth)
        with patch.object(V380Bot, "_qr_png", return_value=b"synthetic-png"):
            bot._qr_login_worker(100, threading.Event(), target_slot=1)

        bot._handle_text(100, "Select camera 1")

        saved = json.loads(self.store.camera_config_path(1).read_text())
        self.assertEqual(saved["token"], auth.token)
        self.assertEqual(self.manager.restarted, [1])
        self.assertEqual(self.store.cameras[0]["recording"], {"enabled": False})

    def test_runtime_settings_commands_validate_and_route(self):
        import nvr_config

        runtime = FakeRuntime()
        self.manager.runtime = runtime
        bot = V380Bot(self.store, self.api, self.manager)
        accepted = [
            ("/duration 120", "duration", 120),
            ("/receivers 4", "receivers", 4),
            ("/pre_roll 0", "pre_roll", 0),
            ("/storage_gb 1000", "storage_gb", 1000),
            ("/reserve_gb 0.5", "reserve_gb", 0.5),
            ("/notifications off", "notifications", False),
            ("/outage_alerts on", "outage_alerts", True),
        ]
        for command, key, expected in accepted:
            with self.subTest(command=command):
                bot._handle_text(100, command)
                self.assertEqual(runtime.values[key], expected)
                self.assertEqual(runtime.changes[-1], (key, expected))

        changes_before_invalid = len(runtime.changes)
        for command in (
            "/duration 4",
            "/duration 121",
            "/receivers 0",
            "/receivers 5",
            "/pre_roll 11",
            "/storage_gb 0",
            "/reserve_gb 100.5",
            "/notifications maybe",
            "/outage_alerts maybe",
        ):
            with self.subTest(invalid_command=command):
                bot._handle_text(100, command)
        self.assertEqual(len(runtime.changes), changes_before_invalid)

        self.assertEqual(nvr_config.validate_setting("duration", "5"), 5)
        self.assertEqual(nvr_config.validate_setting("receivers", "1"), 1)
        self.assertEqual(nvr_config.validate_setting("pre_roll", "10"), 10)

    def test_settings_summary_and_web_commands_route_to_runtime(self):
        runtime = FakeRuntime()
        self.manager.runtime = runtime
        bot = V380Bot(self.store, self.api, self.manager)

        bot._handle_text(100, "/settings")
        summary = self.api.messages[-1][1]
        for command in ("/duration", "/receivers", "/pre_roll", "/storage_gb", "/reserve_gb", "/outage_alerts"):
            self.assertIn(command, summary)

        bot._handle_text(100, "/web")
        self.assertEqual(runtime.web_chats, [100])
        bot._handle_text(100, "/web_password shortsecret")
        self.assertEqual(runtime.web_passwords, [])
        bot._handle_text(100, "/web_password synthetic-password")
        self.assertEqual(runtime.web_passwords, ["synthetic-password"])

    def test_recording_commands_validate_and_route_to_runtime_manager(self):
        self.store.state["cameras"] = [{"slot": 1, "name": "Camera 1"}]
        self.store.save()
        recordings = FakeRecordingSettings()
        bot = V380Bot(self.store, self.api, self.manager, recordings=recordings)

        for command in ("/recording off", "/sensitivity high", "/retention 14"):
            bot._handle_text(100, command)
        self.assertEqual(
            recordings.changes,
            [(1, "enabled", False), (1, "sensitivity", "high"), (1, "retention_days", 14)],
        )

        changes_before_invalid = len(recordings.changes)
        for command in ("/recording maybe", "/sensitivity extreme", "/retention 31", "/retention 0"):
            bot._handle_text(100, command)
        self.assertEqual(len(recordings.changes), changes_before_invalid)


class TelegramClientTests(unittest.TestCase):
    def test_api_errors_never_echo_the_fake_bot_token(self):
        fake_token = "123456:dummy_test_token_0123456789"

        def failing_opener(request, timeout=0):
            raise URLError("synthetic failure containing " + fake_token)

        api = TelegramAPI(fake_token, opener=failing_opener)
        with self.assertRaises(BotError) as raised:
            api.get_updates(0)
        self.assertNotIn(fake_token, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
