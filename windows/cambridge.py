"""CamBridge for Windows — turns your iPhone (over the USB cable) into a webcam.

iPhone app  --USB cable (Apple usbmuxd)-->  this program  -->  "CamBridge" virtual webcam
"""

import collections
import io
import json
import logging
import os
import queue
import socket
import struct
import sys
import threading
import time

from PIL import Image

import adb
import processing
import recorder
import usbmux
import video
import virtualcam
import wifi

APP_NAME = "CamBridge"
VERSION = "1.0"
PHONE_PORT = 7878
MAGIC = b"CBG1"
MSG_JPEG = 1
MSG_INFO = 2
MSG_COMMAND = 3
MSG_H264 = 7
MSG_HEVC = 8
MSG_AUDIO = 9
OUTPUT_SIZES = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "4K": (3840, 2160)}

log = logging.getLogger("cambridge")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings:
    """Remembers PC-side choices between runs (settings.json in AppData)."""

    DEFAULTS = {
        "output": "1080p",
        "adjust": dict(processing.DEFAULT_ADJUST),
        "filter": "None",
        "framing": dict(processing.DEFAULT_FRAMING),
        "close_to_tray": True,
        "tray_tip_shown": False,
        "left_open": True,
        "right_open": True,
        "pc_id": "",
        "pair_key": "",
        "trusted": {},
        "audio_source": "none",
        "audio_send_to": "",
        "audio_fx": {"gain_db": 0.0, "limiter": True, "gate": False, "clarity": False},
        "high_performance": False,
        "adaptive": True,
    }

    def __init__(self):
        self.path = os.path.join(virtualcam.app_data_dir(), "settings.json")
        self.data = json.loads(json.dumps(self.DEFAULTS))
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key, value in saved.items():
                if key in self.data and isinstance(value, type(self.data[key])):
                    if isinstance(value, dict):
                        self.data[key].update(value)
                    else:
                        self.data[key] = value
        except (OSError, ValueError):
            pass
        if self.data["output"] not in OUTPUT_SIZES:
            self.data["output"] = "1080p"

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Newest-frame mailbox (old frames are thrown away to keep delay low)
# ---------------------------------------------------------------------------

class FrameSlot:
    def __init__(self):
        self._cond = threading.Condition()
        self._jpeg = None
        self._seq = 0

    def put(self, jpeg):
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()

    def wait_newer(self, seq, timeout):
        with self._cond:
            if self._seq == seq:
                self._cond.wait(timeout)
            return self._jpeg, self._seq

    def latest(self):
        with self._cond:
            return self._jpeg, self._seq

    def poke(self):
        """Wakes the camera thread (video packets arrived)."""
        with self._cond:
            self._cond.notify_all()


class VideoQueue:
    """H.264/HEVC packets in arrival order (they must ALL be decoded, in order)."""

    def __init__(self, slot):
        self.slot = slot
        self.items = collections.deque()
        self.lock = threading.Lock()

    def put(self, codec, payload):
        with self.lock:
            self.items.append((codec, payload))
        self.slot.poke()

    def take_all(self):
        with self.lock:
            items = list(self.items)
            self.items.clear()
        return items

    def reset(self):
        with self.lock:
            self.items.clear()

    def __len__(self):
        return len(self.items)


# ---------------------------------------------------------------------------
# iPhone connection
# ---------------------------------------------------------------------------

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("phone disconnected")
        buf += chunk
    return bytes(buf)


def read_message(sock):
    header = recv_exact(sock, 12)
    if header[:4] != MAGIC:
        raise ConnectionError("unexpected data from iPhone")
    msg_type, length = struct.unpack(">II", header[4:])
    if length > 32 * 1024 * 1024:
        raise ConnectionError("frame too large")
    return msg_type, recv_exact(sock, length)


class PhoneLink(threading.Thread):
    """Keeps looking for the iPhone and receives its pictures."""

    def __init__(self, slot, port=PHONE_PORT):
        super().__init__(daemon=True)
        self.slot = slot
        self.port = port
        self.running = True
        self.lock = threading.Lock()
        self.status = "Starting…"
        self.detail = ""
        self.state = "waiting"  # waiting | connected | error
        self.info = {}
        self._sock = None
        self._send_lock = threading.Lock()
        self._last_launch = 0.0
        self.video = VideoQueue(slot)
        self.audio = None          # AudioHub
        self.audio_wanted = False
        self.on_connect = {}       # extra settings sent to every phone that connects
        self.bytes_in = 0
        self._wifi = queue.Queue()
        self.transport = ""

    def offer_wifi(self, sock, name):
        """A phone on Wi-Fi was accepted; use it on the next loop."""
        self._wifi.put((sock, name))

    def is_busy(self):
        return self._sock is not None

    def send_command(self, **command):
        """Sends a setting change to the iPhone, e.g. send_command(zoom=2.0)."""
        sock = self._sock
        if sock is None:
            return False
        payload = json.dumps(command).encode("utf-8")
        data = MAGIC + struct.pack(">II", MSG_COMMAND, len(payload)) + payload
        try:
            with self._send_lock:
                sock.sendall(data)
            return True
        except OSError:
            return False

    def _set(self, state, status, detail=""):
        with self.lock:
            changed = (self.state, self.status, self.detail) != (state, status, detail)
            self.state, self.status, self.detail = state, status, detail
        if changed:
            log.info("%s: %s %s", state, status, detail)

    def snapshot(self):
        with self.lock:
            return self.state, self.status, self.detail, dict(self.info)

    def stop(self):
        self.running = False
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def run(self):
        while self.running:
            try:
                self._attempt()
            except Exception:  # never let the thread die
                log.exception("connection loop error")
                time.sleep(1)

    def _attempt(self):
        # 0) A phone that just connected over Wi-Fi
        try:
            sock, name = self._wifi.get_nowait()
        except queue.Empty:
            pass
        else:
            self.transport = "wifi"
            self._session(sock, "%s over Wi-Fi" % name, first_message_timeout=5)
            return

        # 1) iPhone (Apple's USB service, comes with iTunes)
        iphone_hint = ""
        try:
            iphones = [d for d in usbmux.list_devices() if d["usb"]]
        except usbmux.ServiceNotFound:
            iphones, iphone_hint = [], "iPhone: install iTunes from apple.com."
        except (usbmux.UsbmuxError, OSError):
            iphones = []
        if iphones:
            sock, last_error = None, ""
            for dev in iphones:
                try:
                    sock = usbmux.connect(dev["id"], self.port)
                    break
                except (usbmux.UsbmuxError, OSError) as e:
                    last_error = str(e)
            if sock is None:
                self._set("waiting", "iPhone found — open the CamBridge app on it",
                          "If the iPhone asks \"Trust This Computer?\", tap Trust. (%s)" % last_error)
                time.sleep(1)
                return
            self.transport = "usb"
            self._session(sock, "iPhone")
            return

        # 2) Android (USB debugging, through adb)
        try:
            phones = adb.devices()
        except adb.AdbError:
            phones = []
        ready = [p for p in phones if p["state"] == "device"]
        if ready:
            self._android(ready[0])
            return
        if any(p["state"] == "unauthorized" for p in phones):
            self._set("waiting", "Tap \"Allow\" on your Android phone",
                      "Allow USB debugging from this computer (tick \"Always allow\").")
            time.sleep(1)
            return
        if phones:
            self._set("waiting", "Android phone is connecting…", "")
            time.sleep(1)
            return

        hints = ["Android: turn on USB debugging (Settings → Developer options)."]
        if iphone_hint:
            hints.append(iphone_hint)
        self._set("waiting", "Plug in your phone with the cable", "  ".join(hints))
        time.sleep(1)

    def _android(self, phone):
        serial = phone["serial"]
        name = phone.get("model") or "Android phone"
        apk = adb.bundled_apk()
        if apk and not adb.installed(serial):
            self._set("waiting", "Installing CamBridge on %s…" % name,
                      "If the phone asks to allow the install, tap Allow.")
            try:
                adb.install(serial, apk)
                adb.launch_app(serial)
            except adb.AdbError as e:
                self._set("error", "Couldn't install the app on %s" % name,
                          "Install CamBridge.apk on the phone yourself. (%s)" % str(e)[:120])
                time.sleep(5)
                return
        try:
            pc_port = adb.forward(serial, self.port)
        except adb.AdbError as e:
            self._set("error", "Android connection problem", str(e)[:160])
            time.sleep(2)
            return
        try:
            sock = socket.create_connection(("127.0.0.1", pc_port), timeout=3)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.transport = "usb"
            self._session(sock, name, first_message_timeout=2.5)
        except NoApp:
            now = time.monotonic()
            if now - self._last_launch > 8:
                self._last_launch = now
                adb.launch_app(serial)
            self._set("waiting", "Opening CamBridge on %s…" % name,
                      "Unlock the phone. If it doesn't open, open the CamBridge app yourself.")
            time.sleep(1)
        except OSError as e:
            self._set("error", "Android connection problem", str(e)[:160])
            time.sleep(1)
        finally:
            adb.remove_forward(serial, pc_port)

    def _session(self, sock, name, first_message_timeout=None):
        """Receives from a connected phone until it disconnects."""
        self._sock = sock
        try:
            if first_message_timeout:
                # adb accepts the connection even when the app isn't running; then it closes at once.
                sock.settimeout(first_message_timeout)
                try:
                    first = read_message(sock)
                except (socket.timeout, OSError, ConnectionError):
                    raise NoApp()
                self._on_message(*first)
            self._set("connected", "Connected to %s" % name if name != "iPhone" else "Connected to iPhone", "")
            self.video.reset()
            self.send_command(keyframe=True, audio=self.audio_wanted, **self.on_connect)
            self._receive(sock)
        except NoApp:
            if "Wi-Fi" in name:
                log.info("Wi-Fi phone sent nothing; closed")
                return
            raise
        except (OSError, ConnectionError) as e:
            log.info("link closed: %s", e)
        finally:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            with self.lock:
                was_connected = self.state == "connected"
                self.info = {}
            if self.running and was_connected:
                self._set("waiting", "Phone disconnected — reconnecting…", "")
                time.sleep(0.5)

    def _on_message(self, msg_type, payload):
        self.bytes_in += len(payload) + 12
        if msg_type == MSG_H264 or msg_type == MSG_HEVC:
            self.video.put("hevc" if msg_type == MSG_HEVC else "h264", payload)
            return
        if msg_type == MSG_AUDIO:
            if self.audio is not None:
                self.audio.phone_audio(payload)
            return
        if msg_type == MSG_JPEG:
            self.slot.put(payload)
        elif msg_type == MSG_INFO:
            try:
                info = json.loads(payload.decode("utf-8"))
                with self.lock:
                    self.info = info if isinstance(info, dict) else {}
            except ValueError:
                pass

    def _receive(self, sock):
        sock.settimeout(1.0)
        idle_since = time.monotonic()
        connected_status = self.status
        while self.running:
            try:
                msg_type, payload = read_message(sock)
            except socket.timeout:
                if time.monotonic() - idle_since > 2:
                    self._set("connected", "Connected — waiting for video",
                              "Keep the CamBridge app open on the phone screen.")
                continue
            self._on_message(msg_type, payload)
            if msg_type in (MSG_JPEG, MSG_H264, MSG_HEVC):
                idle_since = time.monotonic()
                if self.status != connected_status:
                    self._set("connected", connected_status, "")


class NoApp(Exception):
    """The phone is connected but the CamBridge app isn't answering."""


# ---------------------------------------------------------------------------
# Pictures -> virtual camera
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pictures -> virtual camera
# ---------------------------------------------------------------------------

class CameraPump(threading.Thread):
    """Takes the newest phone picture (JPEG, or decoded H.264/HEVC video), applies framing
    and adjustments, and feeds the virtual camera. Also: pause screen, recording, stats."""

    PREVIEW = (960, 540)

    def __init__(self, slot, link, size, adjust=None, framing=None):
        super().__init__(daemon=True)
        self.slot = slot
        self.link = link
        self.size = size
        self.audio = None             # AudioHub
        self.running = True
        self.error = None
        self.fps = 0
        self.in_use = False
        self.paused = False
        self.last_output = None       # newest finished picture (for the preview)
        self.output_seq = 0
        self.recorder = None          # RecordingWorker
        self.record_error = None
        self.last_recording = None
        self.stats = video.Stats()
        self.codec = "—"
        self.decoder_name = video.backend_name()
        self.cam = None
        self._lock = threading.Lock()
        self._new_size = None
        self._framing = dict(framing or processing.DEFAULT_FRAMING)
        self._lut = processing.build_lut(adjust or {})
        self._pause_frame = None
        self._want_record = None
        self._decoder = None
        self._decoder_codec = None
        self._need_key = True
        self._last_preview = 0.0
        self._last_key_request = 0.0

    # ---- called from the window
    def resize(self, size):
        with self._lock:
            self._new_size = size

    def set_adjust(self, adjust):
        lut = processing.build_lut(adjust)
        with self._lock:
            self._lut = lut

    def set_framing(self, framing):
        with self._lock:
            self._framing = dict(framing)

    def set_paused(self, paused):
        with self._lock:
            self.paused = paused
            self._pause_frame = None

    def start_recording(self):
        with self._lock:
            self._want_record = True

    def stop_recording(self):
        with self._lock:
            self._want_record = False

    def stop(self):
        self.running = False

    # ---- thread
    def run(self):
        try:
            self.cam = virtualcam.VirtualCamera(*self.size)
            self._loop()
        except Exception as e:
            log.exception("camera error")
            self.error = str(e)
        finally:
            self._stop_recorder(wait=True)
            self._close_decoder()
            if self.cam:
                self.cam.close()

    def _close_decoder(self):
        if self._decoder is not None:
            try:
                self._decoder.close()
            except Exception:
                pass
        self._decoder = None
        self._decoder_codec = None
        self._need_key = True

    def _request_keyframe(self):
        now = time.monotonic()
        if now - self._last_key_request > 0.5:
            self._last_key_request = now
            self.stats.keyframe_requests += 1
            self.link.send_command(keyframe=True)

    def _start_recorder(self, w, h):
        rate, channels = self.audio.recording_format() if self.audio else (0, 0)
        fps = int(self.link.info.get("actualFps") or self.link.info.get("fps") or 30)
        try:
            worker = recorder.RecordingWorker(w, h, fps, rate, channels)
        except OSError as e:
            self.record_error = str(e)
            return
        self.record_error = None
        self.recorder = worker
        if self.audio is not None:
            self.audio.recorder = worker

    def _stop_recorder(self, wait=False):
        worker, self.recorder = self.recorder, None
        if self.audio is not None:
            self.audio.recorder = None
        if worker is None:
            return

        def finish():
            try:
                self.last_recording = worker.stop()
                log.info("recording saved: %s", self.last_recording)
            except Exception as e:
                self.record_error = str(e)

        if wait:
            finish()
        else:
            threading.Thread(target=finish, daemon=True).start()

    def _publish(self, img):
        self.last_output = img
        self.output_seq += 1

    def _emit(self, bgr, now, preview=None):
        """bgr: bytes or ctypes buffer of exactly the output size."""
        self.cam.send(bgr)
        if self.recorder is not None:
            self.recorder.put_video(bytes(bgr), now)
        if preview is not None:
            self._publish(preview)

    @staticmethod
    def _finish(img, look, fast_sharp, w, h):
        """RGB picture -> BGR bytes for the webcam, sharpened with the fast code if needed."""
        data = img.tobytes("raw", "BGR")
        if fast_sharp and look and look.sharpness > 0:
            data = bytearray(data)
            video.sharpen(data, w, h, look.sharpness)
        return data

    # ---- video (H.264 / HEVC) -------------------------------------------------

    def _video_packets(self, packets, w, h, framing, lut, now):
        """Decodes every packet in order; shows only the newest picture."""
        if len(packets) > 90:  # seconds behind: jump to the newest keyframe
            keys = [i for i, (_, p) in enumerate(packets) if p and p[0] & 1]
            if keys:
                self.stats.dropped += keys[-1]
                packets = packets[keys[-1]:]
            else:
                self.stats.dropped += len(packets)
                self._need_key = True
                self._request_keyframe()
                return False
        got = False
        for codec, payload in packets:
            if len(payload) < 2:
                continue
            self.stats.add_bytes(len(payload))
            key, data = bool(payload[0] & 1), payload[1:]
            if self._decoder is None or self._decoder_codec != codec:
                self._close_decoder()
                try:
                    self._decoder = video.make_decoder(codec)
                    self._decoder_codec = codec
                    self.decoder_name = self._decoder.backend
                    self.codec = codec.upper().replace("H264", "H.264")
                except RuntimeError as e:
                    self.error_note = str(e)
                    self.codec = "%s (no decoder)" % codec.upper()
                    return False
                self._request_keyframe()
            if self._need_key and not key:
                self.stats.dropped += 1
                self._request_keyframe()
                continue
            self._need_key = False
            t0 = time.perf_counter()
            try:
                n = self._decoder.decode(data)
            except RuntimeError as e:
                log.warning("decode error: %s", e)
                self._close_decoder()
                self._request_keyframe()
                return got
            self.stats.decode_ms = self.stats.smooth(self.stats.decode_ms, (time.perf_counter() - t0) * 1000)
            got = got or n > 0
        if not got or self._decoder is None:
            return False

        t0 = time.perf_counter()
        dec = self._decoder
        preview = None
        if processing.is_plain(framing, lut):
            buf = dec.to_bgr(w, h)
            if buf is None:
                return False
            if now - self._last_preview > 0.1:
                self._last_preview = now
                pw, ph = self.PREVIEW
                pbuf = dec.to_bgr(pw, ph, slot="preview")
                if pbuf is not None:
                    preview = Image.frombuffer("RGB", (pw, ph), bytes(pbuf), "raw", "BGR", 0, 1)
            self._emit(buf, now, preview)
        else:
            src = dec.size() or (w, h)
            zoom = max(1.0, float(framing.get("zoom", 1.0)))
            scale = min(1.0, max(w, h) * zoom / max(src))
            sw, sh = max(2, int(src[0] * scale) & ~1), max(2, int(src[1] * scale) & ~1)
            buf = dec.to_bgr(sw, sh)
            if buf is None:
                return False
            img = Image.frombuffer("RGB", (sw, sh), bytes(buf), "raw", "BGR", 0, 1)
            fast_sharp = video.native_sharpen_available()
            out = processing.process_image(img, w, h, framing, lut, sharpen=not fast_sharp)
            self._emit(self._finish(out, lut, fast_sharp, w, h), now, out)
        self.stats.process_ms = self.stats.smooth(self.stats.process_ms, (time.perf_counter() - t0) * 1000)
        return True

    # ---- main loop --------------------------------------------------------------

    def _loop(self):
        seq = 0
        last_frame = 0.0
        last_filler = 0.0
        filler = None
        filler_key = None
        count, count_start = 0, time.monotonic()
        self.error_note = None

        while self.running:
            with self._lock:
                new_size, self._new_size = self._new_size, None
                framing, lut, paused = self._framing, self._lut, self.paused
                want_record, self._want_record = self._want_record, None
            if new_size and new_size != self.size:
                self._stop_recorder()
                self.cam.close()
                self.size = new_size
                self.cam = virtualcam.VirtualCamera(*self.size)
                filler_key = None
            w, h = self.size

            if want_record is True and self.recorder is None:
                self._start_recorder(w, h)
            elif want_record is False:
                self._stop_recorder()

            jpeg, new_seq = self.slot.wait_newer(seq, 0.2)
            packets = self.link.video.take_all()
            now = time.monotonic()
            emitted = False

            if paused:
                if self._pause_frame is None or self._pause_frame.size != (w, h):
                    self._pause_frame = processing.make_pause_screen(self.last_output, w, h)
                if packets:  # keep the decoder in step so resuming is instant
                    self._video_packets(packets, *self.PREVIEW, processing.DEFAULT_FRAMING, None, now)
                if now - last_filler > 0.2:
                    self._emit(self._pause_frame.tobytes("raw", "BGR"), now)
                    last_filler = now
                seq = new_seq
                continue

            if packets:
                if self._video_packets(packets, w, h, framing, lut, now):
                    last_frame = now
                    count += 1
                    emitted = True
                seq = new_seq
            elif new_seq != seq and jpeg:
                seq = new_seq
                self.codec = "JPEG"
                t0 = time.perf_counter()
                try:
                    phone_rot = int(self.link.info.get("frameRotation", 0) or 0)
                    f = framing
                    if phone_rot:
                        f = dict(framing, rotate=(int(framing.get("rotate", 0)) + phone_rot) % 360)
                    fast_sharp = video.native_sharpen_available()
                    out = processing.process(jpeg, w, h, f, lut, sharpen=not fast_sharp)
                    self.stats.add_bytes(len(jpeg))
                    self._emit(self._finish(out, lut, fast_sharp, w, h), now, out)
                    self.stats.process_ms = self.stats.smooth(self.stats.process_ms, (time.perf_counter() - t0) * 1000)
                    last_frame = now
                    count += 1
                    emitted = True
                except Exception as e:  # a broken picture: skip it
                    log.warning("bad frame: %s", e)

            if not emitted and now - last_frame > 1.5 and now - last_filler > 0.2:
                _, status, _, _ = self.link.snapshot()
                key = (w, h, status)
                if key != filler_key:
                    filler = processing.make_placeholder(w, h, "CamBridge", status)
                    filler_key = key
                self.cam.send(filler.tobytes("raw", "BGR"))
                last_filler = now
                if self._decoder is not None and last_frame > 0 and now - last_frame > 3:
                    self._close_decoder()

            if now - count_start >= 1.0:
                self.fps = round(count / (now - count_start))
                count, count_start = 0, now
                self.in_use = self.cam.in_use()


def resource(name):
    base = virtualcam.bundled_dir() if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def setup_logging():
    path = os.path.join(virtualcam.app_data_dir(), "log.txt")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 2 * 1024 * 1024:
            os.remove(path)
    except OSError:
        pass
    handlers = [logging.FileHandler(path, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "CamBridge")
        return True
    except OSError:
        return False


def set_autostart(enabled):
    """Adds/removes CamBridge from programs that start with Windows (minimised)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, "CamBridge", 0, winreg.REG_SZ,
                                  '"%s" --minimized' % sys.executable)
            else:
                try:
                    winreg.DeleteValue(key, "CamBridge")
                except OSError:
                    pass
        return True
    except OSError:
        return False


def install_camera(force=False):
    """Makes sure the 'CamBridge' webcam is installed in Windows. Returns True if OK."""
    try:
        virtualcam.copy_dlls()
    except OSError as e:
        log.error("could not copy camera files: %s", e)
    if virtualcam.is_registered() and not force:
        return True
    log.info("registering camera (admin prompt)")
    virtualcam.request_registration()
    return virtualcam.is_registered()


def self_test():
    """Used by the GitHub build to check the program works."""
    test = Image.new("RGB", (1920, 1080), (200, 30, 30))
    buf = io.BytesIO()
    test.save(buf, "JPEG", quality=80)
    jpeg = buf.getvalue()
    bgr = processing.process(jpeg, 1280, 720).tobytes("raw", "BGR")
    assert len(bgr) == 1280 * 720 * 3, "wrong frame size"
    b, g, r = bgr[0], bgr[1], bgr[2]
    assert r > 150 and b < 80, "colour order wrong: %r" % ((b, g, r),)
    lut = processing.build_lut(processing.FILTERS["Mono"])
    framed = processing.process(jpeg, 1920, 1080, dict(processing.DEFAULT_FRAMING, rotate=90, zoom=2.0), lut)
    assert framed.size == (1920, 1080) and framed.getpixel((0, 0)) == (0, 0, 0)
    processing.make_placeholder(1920, 1080, "CamBridge", "self test")
    processing.make_pause_screen(framed, 1280, 720)
    import ui  # make sure the window code at least loads
    assert ui.App
    import wifi
    url = wifi.pairing_url({"pair_key": "k", "pc_id": "id"})
    assert url.startswith("cambridge://pair?")
    wifi.qr_image(url, 200)
    from zeroconf import Zeroconf  # discovery library is bundled
    assert Zeroconf
    import audio  # sound library is bundled
    audio.list_devices("input")
    if sys.platform == "win32":
        assert audio._sd() is not None, "sound library missing"
        assert audio.audioop is not None, "audioop missing"
        # The video DLL must be inside the app. (GitHub's build servers may not have
        # Windows' video codecs, so decoding/recording problems are only reported.)
        assert video._dll_path() and os.path.exists(video._dll_path()), "cbvideo.dll missing"
        print("video decoder:", video.backend_name())
        try:
            import tempfile
            rec = recorder.Mp4Recorder(640, 360, 30, 48000, 1, folder=tempfile.gettempdir())
            frame = bytes(640 * 360 * 3)
            for i in range(15):
                rec.add_video(frame, i / 30)
                rec.add_audio(bytes(1600 * 2), 1600, i / 30)
            files = rec.close()
            print("mp4 test:", files[0], os.path.getsize(files[0]))
            os.remove(files[0])
        except Exception as e:
            print("mp4 test skipped:", e)
        cam = virtualcam.VirtualCamera(1280, 720)
        for _ in range(10):
            cam.send(bgr)
            time.sleep(0.03)
        cam.close()
    print("SELFTEST OK")
    return 0


def main():
    args = sys.argv[1:]
    if "--register" in args or "--unregister" in args:
        sys.exit(virtualcam.register_now(unregister="--unregister" in args))
    if "--selftest" in args:
        sys.exit(self_test())

    setup_logging()
    log.info("%s %s starting", APP_NAME, VERSION)

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    import tkinter as tk
    from tkinter import messagebox
    import ui

    root = tk.Tk()
    root.withdraw()

    if sys.platform == "win32" and not virtualcam.is_registered():
        messagebox.showinfo(APP_NAME, "CamBridge will now install its webcam (one time only).\n\n"
                                      "Windows will ask for permission — click \"Yes\".")
        if not install_camera():
            messagebox.showwarning(APP_NAME, "The webcam was not installed, so apps won't see it yet.\n"
                                             "You can try again with the \"Repair camera\" button.")
    else:
        try:
            virtualcam.copy_dlls()
        except OSError:
            pass

    settings = Settings()
    slot = FrameSlot()
    link = PhoneLink(slot)
    pump = CameraPump(slot, link, OUTPUT_SIZES[settings["output"]],
                      adjust=settings["adjust"], framing=settings["framing"])
    pump.start()
    time.sleep(0.2)
    if pump.error and "already running" in pump.error:
        messagebox.showinfo(APP_NAME, "CamBridge is already open — look for its icon next to the clock.")
        return
    import audio as audiomod
    link.on_connect = {"adaptive": settings["adaptive"]}
    hub = audiomod.AudioHub(settings, link)
    link.audio = hub
    pump.audio = hub
    link.start()
    wifi.ensure_identity(settings)
    wifi_server = wifi.WifiServer(settings, link)
    wifi_server.start()

    ui.App(root, settings, slot, link, pump, core=sys.modules[__name__], wifi=wifi_server, audio=hub,
           start_minimized="--minimized" in args)
    root.mainloop()
    wifi_server.stop()
    hub.close()
    log.info("closed")


if __name__ == "__main__":
    main()
