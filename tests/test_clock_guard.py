import io
import json
import unittest
from unittest.mock import Mock, patch

from runtime.clock_guard import ClockGuard


class ClockGuardTests(unittest.TestCase):
    def validate_samples(self, samples):
        guard = ClockGuard.__new__(ClockGuard)
        # Model the OSD recognizer's response to decoded frames with no private footage.
        guard.inspect = lambda image: (
            (True, bytes([image.getpixel((0, 0))]), 1.0)
            if image.getpixel((0, 0)) < 100
            else (False, None, 0.1)
        )
        raw = b"".join(bytes([sample]) * (640 * 360) for sample in samples)
        decoder = Mock(stdout=io.BytesIO(raw))
        decoder.wait.return_value = 0
        with patch("runtime.clock_guard.subprocess.Popen", return_value=decoder):
            return guard.validate_video("event.mp4", min_frames=len(samples))

    def test_short_exposure_change_between_verified_frames_is_kept(self):
        report = self.validate_samples([*range(9), 246, 245, 246, *range(9, 20)])

        self.assertTrue(report["valid"])
        self.assertEqual(report["frames"], 23)
        self.assertEqual(report["max_osd_dropout_frames"], 3)

    def test_unverified_flat_edges_and_sustained_screens_are_rejected(self):
        for samples, first_bad_frame in (
            ([246, *range(20)], 1),
            ([*range(20), 246], 21),
            ([*range(9), 246, 246, 246, 246, *range(9, 20)], 10),
            ([*range(9), 246, 9, 246, 10, 246, 11, 246, *range(12, 20)], 16),
        ):
            with self.subTest(samples=samples), self.assertRaises(ValueError) as error:
                self.validate_samples(samples)
            self.assertEqual(json.loads(str(error.exception))["first_bad_frame"], first_bad_frame)

    def test_brief_flash_does_not_make_a_frozen_clock_valid(self):
        with self.assertRaises(ValueError) as error:
            self.validate_samples([10] * 9 + [246] + [10] * 10)

        self.assertEqual(json.loads(str(error.exception))["clock_changes"], 1)
