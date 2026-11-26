"""CamBridge for Windows — turns your iPhone (over the USB cable) into a webcam.

iPhone app  --USB cable (Apple usbmuxd)-->  this program  -->  "CamBridge" virtual webcam
"""

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
import virtualcam
import wifi

APP_NAME = "CamBridge"
VERSION = "1.0"
PHONE_PORT = 7878
MAGIC = b"CBG1"
MSG_JPEG = 1
MSG_INFO = 2
MSG_COMMAND = 3
OUTPUT_SIZES = {"1080p": (1920, 1080), "720p": (1280, 720)}

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
            if msg_type == MSG_JPEG:
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
    """Takes the newest iPhone picture, applies framing/adjustments, and feeds the
    virtual camera. Also handles the pause screen and recording."""

    def __init__(self, slot, link, size, adjust=None, framing=None):
        super().__init__(daemon=True)
        self.slot = slot
        self.link = link
        self.size = size
        self.running = True
        self.error = None
        self.fps = 0
        self.in_use = False
        self.paused = False
        self.last_output = None      # newest finished picture (for the preview)
        self.output_seq = 0
        self.recorder = None
        self.record_error = None
        self.cam = None
        self._lock = threading.Lock()
        self._new_size = None
        self._framing = dict(framing or processing.DEFAULT_FRAMING)
        self._lut = processing.build_lut(adjust or {})
        self._pause_frame = None
        self._want_record = None     # True = start, False = stop

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
            self._close_recorder()
            if self.cam:
                self.cam.close()

    def _close_recorder(self):
        rec, self.recorder = self.recorder, None
        if rec:
            try:
                files = rec.close()
                log.info("recording saved: %s", files)
            except OSError as e:
                log.error("recording close failed: %s", e)

    def _publish(self, img):
        self.last_output = img
        self.output_seq += 1

    def _loop(self):
        seq = 0
        last_frame = 0.0
        last_filler = 0.0
        filler = None
        filler_key = None
        count, count_start = 0, time.monotonic()

        while self.running:
            with self._lock:
                new_size, self._new_size = self._new_size, None
                framing, lut, paused = self._framing, self._lut, self.paused
                want_record, self._want_record = self._want_record, None
            if new_size and new_size != self.size:
                self._close_recorder()
                self.cam.close()
                self.size = new_size
                self.cam = virtualcam.VirtualCamera(*self.size)
                filler_key = None
            w, h = self.size

            if want_record is True and self.recorder is None:
                try:
                    self.recorder = recorder.AviRecorder(w, h)
                    self.record_error = None
                except OSError as e:
                    self.record_error = str(e)
            elif want_record is False:
                self._close_recorder()

            jpeg, new_seq = self.slot.wait_newer(seq, 0.2)
            now = time.monotonic()
            out = None

            if paused:
                if self._pause_frame is None or self._pause_frame.size != (w, h):
                    self._pause_frame = processing.make_pause_screen(self.last_output, w, h)
                if now - last_filler > 0.2:
                    out = self._pause_frame
                    last_filler = now
                seq = new_seq
            elif new_seq != seq and jpeg:
                seq = new_seq
                try:
                    phone_rot = int(self.link.info.get("frameRotation", 0) or 0)
                    if phone_rot:
                        framing = dict(framing, rotate=(int(framing.get("rotate", 0)) + phone_rot) % 360)
                    out = processing.process(jpeg, w, h, framing, lut)
                    last_frame = now
                    count += 1
                except Exception as e:  # a broken picture: skip it
                    log.warning("bad frame: %s", e)
            elif now - last_frame > 1.5 and now - last_filler > 0.2:
                _, status, _, _ = self.link.snapshot()
                key = (w, h, status)
                if key != filler_key:
                    filler = processing.make_placeholder(w, h, "CamBridge", status)
                    filler_key = key
                out = filler
                last_filler = now

            if out is not None:
                self.cam.send(out.tobytes("raw", "BGR"))
                if out is not filler:
                    self._publish(out)
                if self.recorder is not None:
                    try:
                        buf = io.BytesIO()
                        out.save(buf, "JPEG", quality=85)
                        self.recorder.add_jpeg(buf.getvalue())
                    except OSError as e:
                        self.record_error = str(e)
                        self._close_recorder()

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
    if sys.platform == "win32":
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
    link.start()
    wifi.ensure_identity(settings)
    wifi_server = wifi.WifiServer(settings, link)
    wifi_server.start()

    ui.App(root, settings, slot, link, pump, core=sys.modules[__name__], wifi=wifi_server,
           start_minimized="--minimized" in args)
    root.mainloop()
    wifi_server.stop()
    log.info("closed")


if __name__ == "__main__":
    main()
