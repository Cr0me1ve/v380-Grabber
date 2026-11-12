from __future__ import annotations

import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nvr_config
import nvr_worker
from runtime import source_selector


class RuntimeConfigurationTests(unittest.TestCase):
    def test_receiver_count_bounds_and_setting_preservation(self):
        for count in range(1, 5):
            with self.subTest(count=count):
                self.assertEqual(nvr_config.validate_setting("receivers", str(count)), count)
        for count in (0, 5):
            with self.subTest(invalid_count=count), self.assertRaises(ValueError):
                nvr_config.validate_setting("receivers", str(count))

        with tempfile.TemporaryDirectory() as temp, patch.object(nvr_config, "DATA", Path(temp)):
            nvr_config.save_settings(
                {
                    "duration": 45,
                    "pre_roll": 6,
                    "sensitivity": "high",
                    "retention_days": 14,
                    "storage_gb": 40,
                    "reserve_gb": 5,
                }
            )
            nvr_config.save_settings({"receivers": 4})
            saved = nvr_config.load_settings()

        self.assertEqual(saved["receivers"], 4)
        self.assertEqual(saved["duration"], 45)
        self.assertEqual(saved["pre_roll"], 6)
        self.assertEqual(saved["sensitivity"], "high")
        self.assertEqual(saved["retention_days"], 14)
        self.assertEqual(saved["storage_gb"], 40)
        self.assertEqual(saved["reserve_gb"], 5)

    def test_all_exposed_setting_ranges_and_enumerations(self):
        accepted = [
            ("duration", "5", 5),
            ("duration", "120", 120),
            ("pre_roll", "0", 0),
            ("pre_roll", "10", 10),
            ("retention_days", "1", 1),
            ("retention_days", "30", 30),
            ("storage_gb", "1", 1),
            ("storage_gb", "1000", 1000),
            ("reserve_gb", "0.5", 0.5),
            ("reserve_gb", "100", 100),
            ("sensitivity", "low", "low"),
            ("sensitivity", "medium", "medium"),
            ("sensitivity", "high", "high"),
            ("enabled", "on", True),
            ("enabled", "off", False),
            ("notifications", "on", True),
            ("notifications", "off", False),
            ("outage_alerts", "on", True),
            ("outage_alerts", "off", False),
        ]
        for name, value, expected in accepted:
            with self.subTest(name=name, value=value):
                self.assertEqual(nvr_config.validate_setting(name, value), expected)

        rejected = [
            ("duration", "4"),
            ("duration", "121"),
            ("pre_roll", "-1"),
            ("pre_roll", "11"),
            ("retention_days", "0"),
            ("retention_days", "31"),
            ("storage_gb", "0"),
            ("storage_gb", "1001"),
            ("reserve_gb", "0.49"),
            ("reserve_gb", "100.1"),
            ("sensitivity", "extreme"),
            ("enabled", "maybe"),
            ("notifications", "maybe"),
            ("outage_alerts", "maybe"),
        ]
        for name, value in rejected:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                nvr_config.validate_setting(name, value)


class DurableMotionQueueTests(unittest.TestCase):
    def test_send_failure_and_missing_ack_keep_captured_job_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spool = root / "recordings" / ".telegram-spool"
            spool.mkdir(parents=True)
            job_id = "a" * 32
            job_path = spool / (job_id + ".json")
            video_path = spool / (job_id + ".mp4")
            job_path.write_text(
                json.dumps(
                    {
                        "id": job_id,
                        "created": 1_800_000_000,
                        "captured": True,
                        "duration": 30,
                        "requested_duration": 30,
                        "pre_roll": 3,
                    }
                )
            )
            video_path.write_bytes(b"synthetic captured video")
            runtime = SimpleNamespace(
                data=root,
                settings=lambda: {"timezone": "UTC", "camera_name": "Test camera"},
                api=SimpleNamespace(_token="synthetic-token"),
                store=SimpleNamespace(owner={"chat_id": 100}),
            )

            with (
                patch.object(nvr_worker, "SPOOL", spool),
                patch.object(nvr_worker, "STATE", root / "telegram-state"),
                patch.object(nvr_worker, "RUNTIME", runtime),
                patch.object(nvr_worker, "C", {"token": "synthetic-token", "chat_id": 100}),
                patch.object(nvr_worker, "validate_media"),
            ):
                responses = iter(
                    [
                        RuntimeError("synthetic Telegram failure"),
                        {"ok": True},
                        {"message_id": 901},
                    ]
                )

                def send_video(*_args, **_kwargs):
                    result = next(responses)
                    if isinstance(result, Exception):
                        raise result
                    return result

                with patch.object(nvr_worker, "api", side_effect=send_video):
                    for expected_error in (RuntimeError, KeyError):
                        with self.subTest(error=expected_error.__name__), self.assertRaises(expected_error):
                            nvr_worker.process(job_path, "send")
                        state = json.loads(job_path.read_text())
                        self.assertTrue(state["captured"])
                        self.assertFalse(state.get("done", False))
                        self.assertTrue(video_path.is_file())

                    nvr_worker.process(job_path, "send")

            state = json.loads(job_path.read_text())
            self.assertTrue(state["captured"])
            self.assertTrue(state["done"])
            self.assertEqual(state["message_id"], 901)
            self.assertTrue(video_path.is_file())


class SelectorEventPlanTests(unittest.TestCase):
    def setUp(self):
        self.generation = "synthetic-generation"
        self.frames = deque(
            (
                sequence,
                timestamp,
                sequence / 15,
                self.generation,
                ("frame-%d" % sequence).encode(),
            )
            for sequence, timestamp in enumerate(
                (994.0, 995.0, 996.0, 997.0, 998.0, 999.0, 1000.0),
                start=1,
            )
        )
        self.patches = [
            patch.object(source_selector, "RING", self.frames),
            patch.object(source_selector, "LIVE_AT", 0.0),
            patch.object(source_selector, "settings", return_value={"duration": 45, "pre_roll": 4}),
            patch.object(
                source_selector,
                "status",
                return_value={"healthy": True, "generation": self.generation},
            ),
            patch.object(source_selector.time, "time", return_value=1000.0),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_event_plan_snapshots_configured_clip_length_and_pre_roll(self):
        plan = source_selector.event_plan(999.0, self.generation)
        self.assertEqual(plan["duration"], 45)
        self.assertEqual(plan["pre_roll"], 4.0)
        self.assertEqual(plan["recording_start"], 995.0)
        self.assertEqual(plan["generation"], self.generation)

    def test_event_plan_supports_per_event_snapshot_overrides(self):
        plan = source_selector.event_plan(1000.0, self.generation, duration=60, pre_roll=6)
        self.assertEqual(plan["duration"], 60)
        self.assertEqual(plan["pre_roll"], 6.0)
        self.assertEqual(plan["recording_start"], 994.0)

    def test_event_plan_rejects_invalid_duration_and_pre_roll(self):
        for duration, pre_roll in ((4, 3), (121, 3), (30, -1), (30, 11), (30, float("nan"))):
            with self.subTest(duration=duration, pre_roll=pre_roll), self.assertRaises(ValueError):
                source_selector.event_plan(999.0, self.generation, duration, pre_roll)

    def test_event_plan_rejects_a_generation_change(self):
        with self.assertRaises(ValueError):
            source_selector.event_plan(999.0, "stale-generation")


if __name__ == "__main__":
    unittest.main()
