"""Records video to .avi files (Motion-JPEG), which play in VLC, Windows Media Player,
and open in video editors. A new file part starts automatically before 1.9 GB."""

import os
import struct
import time

_LIMIT = 1_900_000_000
_MOVI = 220  # position of the 'movi' fourcc in our fixed header


def recordings_dir():
    home = os.path.expanduser("~")
    for base in (os.path.join(home, "Videos"), home):
        if os.path.isdir(base):
            path = os.path.join(base, "Akrem Cam")
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
        path = os.path.join(self.folder, "Akrem Cam %s%s.avi" % (self.stamp, suffix))
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
