import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import nvr_config
import nvr_worker
from runtime.queue_movie import queue_movie


class MotionMovieTests(unittest.TestCase):
    def test_completed_movies_queue_each_chunk_and_short_tail_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spool = root / ".telegram-spool"
            spool.mkdir()
            (spool / "enabled").touch()
            (spool / "settings.json").write_text('{"duration": 30}')
            archive = root / "camera"
            first = archive / "2026-11-27/19-00-00.mp4"
            next_movie = archive / "2026-11-27/19-01-08.mp4"
            first.parent.mkdir(parents=True)
            first.touch()
            next_movie.touch()

            with patch("runtime.queue_movie.subprocess.check_output", return_value="68.125"):
                queue_movie(spool, archive, first)
                jobs = sorted(spool.glob("????????????????????????????????.json"))
                delivered_job = json.loads(jobs[0].read_text())
                delivered_job.update(done=True, message_id=100)
                jobs[0].write_text(json.dumps(delivered_job))
                queue_movie(spool, archive, first)
                self.assertEqual(json.loads(jobs[0].read_text())["message_id"], 100)
            with patch("runtime.queue_movie.subprocess.check_output", return_value="11.5"):
                queue_movie(spool, archive, next_movie)

            queued = [
                json.loads(path.read_text())
                for path in sorted(spool.glob("????????????????????????????????.json"), key=nvr_worker.job_order)
            ]
            self.assertEqual([job["archive_offset"] for job in queued], [0, 30, 60, 0])
            self.assertEqual([job["created"] - queued[0]["created"] for job in queued], [0, 30, 60, 68])

    def test_closed_movie_capture_uses_exact_file_and_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spool = root / "recordings/.telegram-spool"
            spool.mkdir(parents=True)
            job_id = "a" * 32
            relative = "2026-11-27/19-00-00.mp4"
            movie = root / "recordings/camera" / relative
            movie.parent.mkdir(parents=True)
            movie.touch()
            path = spool / (job_id + ".json")
            path.write_text(json.dumps(dict(id=job_id, created=time.time(), archive_source=relative,
                                            archive_offset=30, requested_duration=30)))
            runtime = SimpleNamespace(data=root, settings=lambda: {"archive_subdir": "camera", "reserve_gb": 0.5})
            usage = SimpleNamespace(free=4 * 1024**3)
            with (
                patch.object(nvr_worker, "SPOOL", spool),
                patch.object(nvr_worker, "RUNTIME", runtime),
                patch.object(nvr_worker.shutil, "disk_usage", return_value=usage),
                patch.object(nvr_worker, "export_movie", return_value={"duration": 12.5}) as export,
                patch.object(nvr_worker, "recover_archive") as recover,
            ):
                nvr_worker.process(path, "capture")
            export.assert_called_once()
            self.assertEqual(export.call_args.args[1:], (relative, 30))
            recover.assert_not_called()
            job = json.loads(path.read_text())
            self.assertTrue(job["captured"])
            self.assertEqual(job["duration"], 12.5)

    def test_existing_camera_config_switches_hooks_and_keeps_custom_settings(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(nvr_config, "DATA", Path(temp)):
            root = Path(temp)
            config = root / "motioneye"
            config.mkdir()
            (config / "motion.conf").write_text("# existing private configuration\n")
            camera = config / "camera-1.conf"
            camera.write_text(
                'camera_name Test\nmovie_max_time 300\non_event_start relay; python3 /helpers/queue_motion.py\n'
                'on_movie_end relay %f\n'
            )
            (root / "runtime.json").write_text('{"duration": 45}')

            nvr_config.initialize()
            updated = camera.read_text()
            self.assertIn("camera_name Test\n", updated)
            self.assertIn("movie_max_time 45\n", updated)
            self.assertIn('queue_movie.py "%f"', updated)
            self.assertNotIn("queue_motion.py", updated)
            nvr_config.initialize()
            self.assertEqual(camera.read_text(), updated)
