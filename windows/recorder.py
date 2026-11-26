"""Recording.

* Mp4Recorder: H.264 + AAC .mp4 using Windows' own encoders (cbvideo.dll) — the normal one.
* AviRecorder: Motion-JPEG .avi without sound — fallback when the Windows encoder is missing.
* RecordingWorker: runs the recorder on its own thread so recording never slows the webcam.
"""

import collections
import ctypes
import os
import struct
import threading
import time

_LIMIT = 1_900_000_000
_MOVI = 220  # position of the 'movi' fourcc in our fixed header


def recordings_dir():
    home = os.path.expanduser("~")
    for base in (os.path.join(home, "Videos"), home):
        if os.path.isdir(base):
            path = os.path.join(base, "CamBridge")
            os.makedirs(path, exist_ok=True)
            return path
    return home


class AviRecorder:
    def __init__(self, width, height, folder=None):
        self.width, self.height = width, height
        self.folder = folder or recordings_dir()
        self.stamp = time.strftime("%Y-%m-%d %H-%M-%S")
        self.part = 0
        self.files = []
        self.started = time.monotonic()
        self._f = None
        self._open()

    @property
    def path(self):
        return self.files[-1] if self.files else ""

    def _open(self):
        self.part += 1
        suffix = "" if self.part == 1 else " (part %d)" % self.part
        path = os.path.join(self.folder, "CamBridge %s%s.avi" % (self.stamp, suffix))
        self.files.append(path)
        self._f = open(path, "wb")
        self._index = []
        self._frames = 0
        self._max_chunk = 0
        self._t0 = time.monotonic()
        self._f.write(self._header(0, 30.0))

    def _header(self, frames, fps):
        w, h = self.width, self.height
        usec = int(round(1_000_000 / fps))
        rate = int(round(fps * 1000))
        avih = struct.pack("<IIIIIIIIII4I", usec, int(w * h * 3 * fps), 0, 0x10, frames, 0, 1,
                           self._max_chunk, w, h, 0, 0, 0, 0)
        strh = struct.pack("<4s4sIHHIIIIIIIIhhhh", b"vids", b"MJPG", 0, 0, 0, 0, 1000, rate, 0,
                           frames, self._max_chunk, 0xFFFFFFFF, 0, 0, 0, w, h)
        strf = struct.pack("<IiiHH4sIiiII", 40, w, h, 1, 24, b"MJPG", w * h * 3, 0, 0, 0, 0)
        strl = b"strl" + b"strh" + struct.pack("<I", 56) + strh + b"strf" + struct.pack("<I", 40) + strf
        hdrl = b"hdrl" + b"avih" + struct.pack("<I", 56) + avih + b"LIST" + struct.pack("<I", len(strl)) + strl
        head = b"RIFF" + struct.pack("<I", 0) + b"AVI " + b"LIST" + struct.pack("<I", len(hdrl)) + hdrl
        head += b"LIST" + struct.pack("<I", 0) + b"movi"
        assert len(head) == _MOVI + 4
        return head

    def add_jpeg(self, data):
        if self._f is None:
            return
        if self._f.tell() + len(data) + 16 * (len(self._index) + 2) > _LIMIT:
            self._finish()
            self._open()
        pos = self._f.tell()
        self._f.write(b"00dc" + struct.pack("<I", len(data)) + data)
        if len(data) % 2:
            self._f.write(b"\0")
        self._index.append((pos - _MOVI, len(data)))
        self._frames += 1
        self._max_chunk = max(self._max_chunk, len(data))

    def elapsed(self):
        return time.monotonic() - self.started

    def _finish(self):
        f = self._f
        if f is None:
            return
        idx_pos = f.tell()
        f.write(b"idx1" + struct.pack("<I", 16 * len(self._index)))
        for offset, size in self._index:
            f.write(b"00dc" + struct.pack("<III", 0x10, offset, size))
        end = f.tell()
        duration = max(0.001, time.monotonic() - self._t0)
        fps = min(120.0, max(1.0, self._frames / duration)) if self._frames > 1 else 30.0
        f.seek(0)
        f.write(self._header(self._frames, fps))
        f.seek(4)
        f.write(struct.pack("<I", end - 8))
        f.seek(_MOVI - 4)
        f.write(struct.pack("<I", idx_pos - _MOVI))
        f.close()
        self._f = None

    def close(self):
        self._finish()
        return self.files


# ---------------------------------------------------------------------------
# MP4 (H.264 + AAC) through Windows Media Foundation
# ---------------------------------------------------------------------------

def _lib():
    import video
    if not video._NativeDecoder.available():
        return None
    lib = video._NativeDecoder._lib
    if not getattr(lib, "_rec_ready", False):
        lib.cb_rec_open.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.cb_rec_open.restype = ctypes.c_void_p
        lib.cb_rec_audio_format.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        lib.cb_rec_audio_format.restype = ctypes.c_int
        lib.cb_rec_video.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_longlong]
        lib.cb_rec_video.restype = ctypes.c_int
        lib.cb_rec_audio.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
        lib.cb_rec_audio.restype = ctypes.c_int
        lib.cb_rec_close.argtypes = [ctypes.c_void_p]
        lib.cb_rec_close.restype = ctypes.c_int
        lib._rec_ready = True
    return lib


def mp4_available():
    return _lib() is not None


def bitrate_kbps(width, height, fps):
    pixels = width * height
    base = 8000 if pixels <= 1280 * 720 else 16000 if pixels <= 1920 * 1080 else 24000 if pixels <= 2560 * 1440 else 45000
    return int(base * (1.5 if fps > 30 else 1.0))


class Mp4Recorder:
    def __init__(self, width, height, fps=30, audio_rate=0, audio_channels=0, folder=None):
        self.lib = _lib()
        if self.lib is None:
            raise OSError("MP4 recording needs Windows")
        self.width, self.height = width & ~1, height & ~1
        self.folder = folder or recordings_dir()
        self.started = time.monotonic()
        path = os.path.join(self.folder, "CamBridge %s.mp4" % time.strftime("%Y-%m-%d %H-%M-%S"))
        self.files = [path]
        self.handle = self.lib.cb_rec_open(path, self.width, self.height, int(fps),
                                           bitrate_kbps(width, height, fps), int(audio_rate), int(audio_channels))
        if not self.handle:
            raise OSError("could not start MP4 recording")
        rate, ch = ctypes.c_int(), ctypes.c_int()
        if self.lib.cb_rec_audio_format(self.handle, ctypes.byref(rate), ctypes.byref(ch)):
            self.audio_rate, self.audio_channels = rate.value, ch.value
        else:
            self.audio_rate, self.audio_channels = 0, 0

    @property
    def path(self):
        return self.files[-1]

    def elapsed(self):
        return time.monotonic() - self.started

    def add_video(self, bgr, t):
        self.lib.cb_rec_video(self.handle, bgr, int(t * 1e7))

    def add_audio(self, pcm, frames, t):
        if self.audio_rate:
            self.lib.cb_rec_audio(self.handle, pcm, frames, int(t * 1e7))

    def close(self):
        if self.handle:
            self.lib.cb_rec_close(self.handle)
            self.handle = None
        return self.files


class RecordingWorker(threading.Thread):
    """Feeds a recorder from its own thread. Video frames are dropped (never audio) if the
    encoder falls behind, so recording can't make the webcam stutter."""

    MAX_VIDEO = 3

    def __init__(self, width, height, fps, audio_rate, audio_channels):
        super().__init__(daemon=True)
        self.width, self.height = width, height
        self.error = None
        try:
            self.rec = Mp4Recorder(width, height, fps, audio_rate, audio_channels)
            self.kind = "mp4"
        except OSError:
            self.rec = AviRecorder(width, height)   # fallback: no sound
            self.kind = "avi"
        self.audio_rate = getattr(self.rec, "audio_rate", 0)
        self.audio_channels = getattr(self.rec, "audio_channels", 0)
        self.items = collections.deque()
        self.cond = threading.Condition()
        self.running = True
        self.dropped = 0
        self.start()

    @property
    def path(self):
        return self.rec.path

    def elapsed(self):
        return self.rec.elapsed()

    def put_video(self, bgr_bytes, t):
        with self.cond:
            videos = sum(1 for i in self.items if i[0] == "v")
            if videos >= self.MAX_VIDEO:
                for i, item in enumerate(self.items):
                    if item[0] == "v":
                        del self.items[i]
                        self.dropped += 1
                        break
            self.items.append(("v", bgr_bytes, t))
            self.cond.notify()

    def put_audio(self, pcm, frames, t):
        if not self.audio_rate:
            return
        with self.cond:
            self.items.append(("a", pcm, frames, t))
            self.cond.notify()

    def stop(self):
        with self.cond:
            self.running = False
            self.cond.notify()
        self.join(10)
        return self.rec.close()

    def run(self):
        while True:
            with self.cond:
                while self.running and not self.items:
                    self.cond.wait(0.5)
                if not self.items and not self.running:
                    return
                item = self.items.popleft()
            try:
                if item[0] == "v":
                    if self.kind == "mp4":
                        self.rec.add_video(item[1], item[2])
                    else:
                        from PIL import Image
                        import io
                        img = Image.frombuffer("RGB", (self.width, self.height), item[1], "raw", "BGR", 0, 1)
                        buf = io.BytesIO()
                        img.save(buf, "JPEG", quality=85)
                        self.rec.add_jpeg(buf.getvalue())
                else:
                    self.rec.add_audio(item[1], item[2], item[3])
            except Exception as e:
                self.error = str(e)
