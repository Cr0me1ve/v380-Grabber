#!/usr/bin/env python3
"""Queue consecutive clips from a movie after Motion finishes writing it."""
import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid


MIN_CLIP_SECONDS = 2
MOVIE_PATH = re.compile(r"\d{4}-\d{2}-\d{2}/\d{2}-\d{2}-\d{2}\.mp4\Z")


def publish_job(spool, job):
    with tempfile.NamedTemporaryFile(mode="w", dir=spool, suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(job, file)
            file.flush()
            os.fsync(file.fileno())
            try:
                # A repeated Motion callback must not reset a delivered job.
                os.link(temporary, spool / (job["id"] + ".json"))
            except FileExistsError:
                return
            fd = os.open(spool, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def queue_movie(spool, archive_root, source):
    if not (spool / "enabled").is_file():
        return
    source = source.resolve(strict=True)
    if not source.is_relative_to(archive_root.resolve()):
        raise ValueError("Movie is outside the camera archive")
    relative = source.relative_to(archive_root.resolve()).as_posix()
    if not MOVIE_PATH.fullmatch(relative) or not source.is_file():
        raise ValueError("Unexpected camera movie path")
    started = datetime.datetime.strptime(relative, "%Y-%m-%d/%H-%M-%S.mp4").timestamp()
    settings = json.loads((spool / "settings.json").read_text())
    seconds = int(settings["duration"])
    if not 5 <= seconds <= 120:
        raise ValueError("Invalid clip length")
    stat = source.stat()
    duration = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(source)],
        text=True, timeout=10,
    ))
    if not math.isfinite(duration) or duration < MIN_CLIP_SECONDS:
        return
    marker = spool / "test-notification"
    test = marker.exists()
    marker.unlink(missing_ok=True)
    for offset in range(0, math.floor(duration - MIN_CLIP_SECONDS) + 1, seconds):
        identity = f"{relative}:{stat.st_ino}:{stat.st_mtime_ns}:{offset}"
        job = {
            "id": uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
            "created": started + offset,
            "camera": 1,
            "requested_duration": seconds,
            "archive_source": relative,
            "archive_offset": offset,
        }
        if test:
            job["test"] = True
        publish_job(spool, job)


if __name__ == "__main__":
    root = Path("/var/lib/motioneye")
    queue_movie(
        root / ".telegram-spool",
        root / os.environ.get("V380_ARCHIVE_SUBDIR", "camera"),
        Path(sys.argv[1]),
    )
