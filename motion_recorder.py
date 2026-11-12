"""Small motion recorder with pre-roll and an acknowledged, durable clip queue."""
from __future__ import annotations
import collections
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
from PIL import Image, ImageChops, ImageStat

LOG = logging.getLogger('v380-recorder')
FPS = 15
CLIP_SECONDS = 30
PRE_ROLL_SECONDS = 3
MAX_CLIP_BYTES = 48 * 1024 * 1024


def _atomic(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _scene(jpeg):
    """Exclude camera OSD; reject effectively empty black/white pictures."""
    with Image.open(io.BytesIO(jpeg)) as image:
        image.load()
        if image.width > 4096 or image.height > 4096:
            raise ValueError('Oversized camera image')
        gray = image.convert('L')
        gray = gray.crop((0, int(gray.height * .1), gray.width, int(gray.height * .88)))
        gray = gray.resize((96, 54))
    histogram = gray.histogram()
    pixels = 96 * 54
    empty = sum(histogram[:10]) / pixels > .985 or sum(histogram[246:]) / pixels > .985
    return gray, not empty


def motion_fraction(previous, current):
    """Fraction of image pixels changing enough to indicate local motion."""
    hist = ImageChops.difference(previous, current).histogram()
    return sum(hist[20:]) / (96 * 54)


class MotionRecorder:
    def __init__(self, source_url, directory, slot, settings_callable):
        self.source_url = source_url
        self.slot = slot
        self.directory = Path(directory) / ('slot-' + str(slot))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.settings = settings_callable
        self.halt = threading.Event()
        self.thread = None
        self.lock = threading.RLock()
        self.state = {'recording': False, 'source_healthy': False, 'reason': 'starting', 'last_clip_at': None}
        self.response = None
        self.encoder = None
        self.encoding = False
        self.encoder_thread = None
        # An interrupted encoding was never placed in the durable queue.
        for temp in self.directory.glob('*.tmp'):
            temp.unlink(missing_ok=True)

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.halt.clear()
            self.thread = threading.Thread(target=self._run, daemon=True, name=f'recorder-{self.slot}')
            self.thread.start()

    def stop(self):
        self.halt.set()
        if self.response:
            self.response.close()
        if self.encoder and self.encoder.poll() is None:
            self.encoder.terminate()
        if self.thread:
            self.thread.join(timeout=12)
        if self.encoder_thread:
            self.encoder_thread.join(timeout=10)

    def _set(self, **values):
        with self.lock:
            self.state.update(values)

    def status(self):
        with self.lock:
            result = dict(self.state)
        result.update(enabled=bool(self.settings().get('enabled', False)), queued=len(self.pending()))
        return result

    def pending(self):
        output = []
        with self.lock:
            for metadata in sorted(self.directory.glob('*.json')):
                try:
                    item = json.loads(metadata.read_text())
                    if item.get('delivered'):
                        continue
                    path = self.directory / (metadata.stem + '.mp4')
                    if not path.is_file():
                        LOG.error('Queued clip media is missing for slot %s', self.slot)
                        continue
                    item['path'] = str(path)
                    output.append(item)
                except (OSError, ValueError):
                    LOG.error('Unable to read clip metadata for slot %s', self.slot)
        return sorted(output, key=lambda x: x['created_at'])

    def ack(self, clip_id):
        if not isinstance(clip_id, str) or len(clip_id) != 32 or any(c not in '0123456789abcdef' for c in clip_id):
            raise ValueError('Invalid clip identifier')
        with self.lock:
            path = self.directory / (clip_id + '.json')
            item = json.loads(path.read_text())
            item['delivered'] = True
            item['delivered_at'] = time.time()
            _atomic(path, json.dumps(item).encode())

    def _cleanup(self):
        days = max(1, min(30, int(self.settings().get('retention_days', 7))))
        cutoff = time.time() - days * 86400
        with self.lock:
            for metadata in self.directory.glob('*.json'):
                try:
                    item = json.loads(metadata.read_text())
                    if item.get('delivered') and item.get('created_at', time.time()) < cutoff:
                        (self.directory / (metadata.stem + '.mp4')).unlink(missing_ok=True)
                        metadata.unlink()
                except (OSError, ValueError):
                    LOG.warning('Recording cleanup failed for slot %s', self.slot)

    def _space_available(self):
        self._cleanup()
        total = sum(p.stat().st_size for p in self.directory.glob('*.mp4') if p.is_file())
        return shutil.disk_usage(self.directory).free > 512 * 1024 * 1024 and total < 1024 * 1024 * 1024

    def _save(self, frames, trigger_at):
        if not self._space_available():
            self._set(reason='recording disk budget reached')
            LOG.error('Recording disk budget reached for slot %s; pending clips retained', self.slot)
            return
        clip_id = uuid.uuid4().hex
        temporary = self.directory / (clip_id + '.mp4.tmp')
        final = self.directory / (clip_id + '.mp4')
        # Normalize wall-clock sampling to 15 fps, so stream jitter cannot shorten the clip.
        first = frames[0][0]
        selected = []
        index = 0
        for n in range(CLIP_SECONDS * FPS):
            stamp = first + n / FPS
            while index + 1 < len(frames) and frames[index + 1][0] <= stamp:
                index += 1
            selected.append(frames[index][1])
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                   '-f', 'image2pipe', '-vcodec', 'mjpeg', '-framerate', str(FPS), '-i', 'pipe:0',
                   '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '25', '-threads', '1',
                   '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-f', 'mp4', '-y', str(temporary)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.encoder = process
        try:
            for frame in selected:
                if self.halt.is_set():
                    raise RuntimeError('Recorder is stopping')
                process.stdin.write(frame)
            process.stdin.close()
            if process.wait(timeout=45) != 0:
                raise RuntimeError('Video encoder failed')
            if not 1024 < temporary.stat().st_size < 45 * 1024 * 1024:
                raise RuntimeError('Encoded video size is invalid')
            os.chmod(temporary, 0o600)
            with temporary.open('rb') as media:
                os.fsync(media.fileno())
            os.replace(temporary, final)
            item = {'id': clip_id, 'slot': self.slot, 'created_at': trigger_at,
                    'duration': CLIP_SECONDS, 'frames': len(selected),
                    'pre_roll_seconds': max(0, trigger_at - frames[0][2]), 'delivered': False}
            with self.lock:
                _atomic(self.directory / (clip_id + '.json'), json.dumps(item).encode())
            self._set(last_clip_at=trigger_at, reason='ok')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            self.encoder = None
            temporary.unlink(missing_ok=True)

    def _queue_encoding(self, frames, trigger):
        # Keep draining live frames while FFmpeg finalizes the previous clip.
        self.encoding = True
        def encode():
            try:
                self._save(frames, trigger)
            except Exception as error:
                LOG.error('Clip encoding failed for slot %s: %s', self.slot, type(error).__name__)
                self._set(reason='clip encoding failed')
            finally:
                self.encoding = False
        self.encoder_thread = threading.Thread(target=encode, daemon=True, name=f'encoder-{self.slot}')
        self.encoder_thread.start()

    def _stream(self):
        request = urllib.request.Request(self.source_url, headers={'Connection': 'close'})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.response = response
            while not self.halt.is_set():
                line = response.readline(256)
                if not line:
                    raise EOFError('Camera stream ended')
                if not line.startswith(b'--'):
                    continue
                size = None
                for _ in range(12):
                    line = response.readline(256)
                    if line in (b'\r\n', b'\n'):
                        break
                    if line.lower().startswith(b'content-length:'):
                        size = int(line.split(b':', 1)[1])
                if size is None or not 4 <= size <= 1024 * 1024:
                    raise ValueError('Invalid camera frame size')
                frame = response.read(size)
                if len(frame) != size or not frame.startswith(b'\xff\xd8') or not frame.endswith(b'\xff\xd9'):
                    raise ValueError('Truncated camera frame')
                yield frame
        self.response = None

    def _run(self):
        while not self.halt.is_set():
            ring = collections.deque()
            previous = None
            active = None
            size = 0
            hits = 0
            valid_since = None
            last = None
            try:
                for jpeg in self._stream():
                    now = time.monotonic()
                    wall = time.time()
                    config = self.settings()
                    scene, usable = _scene(jpeg)
                    if not usable or (last is not None and now - last > .8):
                        ring.clear(); active = None; previous = None; hits = 0; valid_since = None
                    last = now
                    if not usable:
                        self._set(recording=False, source_healthy=True, reason='empty camera scene')
                        continue
                    self._set(source_healthy=True)
                    if valid_since is None:
                        valid_since = now
                    change = motion_fraction(previous, scene) if previous is not None else 0
                    previous = scene
                    threshold = {'low': .035, 'medium': .012, 'high': .004}.get(config.get('sensitivity'), .012)
                    hits = hits + 1 if threshold < change < .6 else 0
                    entry = (now, jpeg, wall)
                    ring.append(entry)
                    while ring and (now - ring[0][0] > PRE_ROLL_SECONDS or sum(len(x[1]) for x in ring) > 8 * 1024 * 1024):
                        ring.popleft()
                    if not config.get('enabled', False):
                        active = None
                        self._set(recording=False, reason='recording disabled')
                        continue
                    if active is None and not self.encoding and hits >= 2 and now - valid_since >= 1:
                        active = list(ring)
                        trigger = wall
                        size = sum(len(x[1]) for x in active)
                        self._set(recording=True, reason='motion detected')
                    elif active is not None:
                        active.append(entry)
                        size += len(jpeg)
                        if size > MAX_CLIP_BYTES:
                            raise ValueError('Recording memory budget reached')
                        if now - active[0][0] >= CLIP_SECONDS:
                            self._queue_encoding(active, trigger)
                            active = None
                            hits = 0
                            ring.clear()
                            self._set(recording=False)
                    if self.halt.is_set():
                        break
            except Exception as error:
                LOG.warning('Recorder stream interrupted for slot %s: %s', self.slot, type(error).__name__)
                self._set(recording=False, source_healthy=False, reason='camera stream unavailable')
            finally:
                if self.response:
                    self.response.close()
                    self.response = None
            self.halt.wait(2)
