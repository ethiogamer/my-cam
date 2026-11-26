"""Fast H.264 / HEVC decoding for CamBridge.

On Windows this uses cbvideo.dll (our small C++ decoder on top of Windows' own
Media Foundation decoder). For development on other systems it can fall back to PyAV
if it happens to be installed (PyAV is NOT shipped with CamBridge).
"""

import ctypes
import os
import sys
import time

import virtualcam


def _dll_path():
    for base in (virtualcam.bundled_dir(), os.path.dirname(os.path.abspath(__file__)),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")):
        path = os.path.join(base, "cbvideo.dll")
        if os.path.exists(path):
            return path
    return None


class _NativeDecoder:
    """cbvideo.dll"""
    backend = "Windows decoder"

    _lib = None

    @classmethod
    def available(cls):
        if sys.platform != "win32":
            return False
        if cls._lib is None:
            path = _dll_path()
            if not path:
                return False
            lib = ctypes.CDLL(path)
            lib.cb_create.argtypes = [ctypes.c_int]
            lib.cb_create.restype = ctypes.c_void_p
            lib.cb_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            lib.cb_decode.restype = ctypes.c_int
            lib.cb_frame_size.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
            lib.cb_frame_size.restype = ctypes.c_int
            lib.cb_to_bgr.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            lib.cb_to_bgr.restype = ctypes.c_int
            lib.cb_last_error.argtypes = [ctypes.c_void_p]
            lib.cb_last_error.restype = ctypes.c_char_p
            lib.cb_destroy.argtypes = [ctypes.c_void_p]
            lib.cb_destroy.restype = None
            lib.cb_sharpen.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            lib.cb_sharpen.restype = None
            cls._lib = lib
        return True

    def __init__(self, codec):
        self.lib = self._lib
        self.handle = self.lib.cb_create(1 if codec == "hevc" else 0)
        if not self.handle:
            if codec == "hevc":
                raise RuntimeError("HEVC decoder not found — install \"HEVC Video Extensions\" "
                                   "from the Microsoft Store, or choose H.264")
            raise RuntimeError("Windows H.264 decoder not available")
        self._bufs = {}

    def decode(self, data):
        n = self.lib.cb_decode(self.handle, data, len(data))
        if n < 0:
            raise RuntimeError((self.lib.cb_last_error(self.handle) or b"decode failed").decode("utf-8", "replace"))
        return n

    def size(self):
        w, h = ctypes.c_int(), ctypes.c_int()
        if self.lib.cb_frame_size(self.handle, ctypes.byref(w), ctypes.byref(h)):
            return w.value, h.value
        return None

    def to_bgr(self, width, height, slot="main"):
        need = width * height * 3
        buf = self._bufs.get(slot)
        if buf is None or len(buf) != need:
            buf = self._bufs[slot] = ctypes.create_string_buffer(need)
        if not self.lib.cb_to_bgr(self.handle, buf, width, height):
            return None
        return buf  # ctypes buffer (no copy); valid until the next call with this slot

    def close(self):
        if self.handle:
            self.lib.cb_destroy(self.handle)
            self.handle = None


class _PyAVDecoder:
    """Development fallback (Linux/macOS tests)."""
    backend = "PyAV (dev)"

    @staticmethod
    def available():
        try:
            import av  # noqa: F401
            return True
        except ImportError:
            return False

    def __init__(self, codec):
        import av
        self.ctx = av.CodecContext.create("hevc" if codec == "hevc" else "h264", "r")
        self.ctx.thread_type = "AUTO"
        self.frame = None

    def decode(self, data):
        import av
        frames = self.ctx.decode(av.Packet(data))
        if frames:
            self.frame = frames[-1]
        return len(frames)

    def size(self):
        return (self.frame.width, self.frame.height) if self.frame is not None else None

    def to_bgr(self, width, height, slot="main"):
        if self.frame is None:
            return None
        from PIL import Image
        from processing import fit_to
        img = fit_to(self.frame.to_image(), width, height)
        return img.tobytes("raw", "BGR")

    def close(self):
        self.frame = None


def sharpen(bgr, width, height, amount):
    """Sharpens a BGR picture (bytearray / ctypes buffer) in place. Returns False if it
    can't (then the slower Pillow sharpening is used)."""
    if amount <= 0 or not _NativeDecoder.available():
        return False
    buf = (ctypes.c_char * len(bgr)).from_buffer(bgr)
    _NativeDecoder._lib.cb_sharpen(buf, width, height, int(amount))
    return True


def native_sharpen_available():
    return _NativeDecoder.available()


def make_decoder(codec):
    if _NativeDecoder.available():
        return _NativeDecoder(codec)
    if _PyAVDecoder.available():
        return _PyAVDecoder(codec)
    raise RuntimeError("no video decoder available")


def backend_name():
    if _NativeDecoder.available():
        return _NativeDecoder.backend
    if _PyAVDecoder.available():
        return _PyAVDecoder.backend
    return "none"


class Stats:
    """Rolling performance numbers for the Performance panel."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.decode_ms = 0.0
        self.process_ms = 0.0
        self.mbps = 0.0
        self.dropped = 0
        self.keyframe_requests = 0
        self._bytes = 0
        self._t0 = time.monotonic()

    def add_bytes(self, n):
        self._bytes += n
        now = time.monotonic()
        if now - self._t0 >= 1.0:
            self.mbps = self._bytes * 8 / 1e6 / (now - self._t0)
            self._bytes = 0
            self._t0 = now

    @staticmethod
    def smooth(old, new):
        return new if old == 0 else old * 0.85 + new * 0.15
