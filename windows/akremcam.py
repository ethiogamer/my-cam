"""Akrem Cam for Windows — turns your iPhone (over the USB cable) into a webcam.

iPhone app  --USB cable (Apple usbmuxd)-->  this program  -->  "Akrem Cam" virtual webcam
"""

import io
import json
import logging
import os
import socket
import struct
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont

import usbmux
import virtualcam

APP_NAME = "Akrem Cam"
VERSION = "1.0"
PHONE_PORT = 7878
MAGIC = b"AKC1"
MSG_JPEG = 1
MSG_INFO = 2
MSG_COMMAND = 3
OUTPUT_SIZES = {"1080p": (1920, 1080), "720p": (1280, 720)}

log = logging.getLogger("akremcam")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings:
    def __init__(self):
        self.path = os.path.join(virtualcam.app_data_dir(), "settings.json")
        self.output = "1080p"
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("output") in OUTPUT_SIZES:
                self.output = data["output"]
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"output": self.output}, f)
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
            raise ConnectionError("iPhone disconnected")
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
        try:
            devices = usbmux.list_devices()
        except usbmux.ServiceNotFound:
            self._set("error", "Apple USB driver not found",
                      "Install iTunes from apple.com, then open Akrem Cam again.")
            time.sleep(2)
            return
        except (usbmux.UsbmuxError, OSError) as e:
            self._set("error", "Apple USB service problem", str(e))
            time.sleep(2)
            return

        usb_devices = [d for d in devices if d["usb"]]
        if not usb_devices:
            self._set("waiting", "Plug in your iPhone with the cable",
                      "Use a good cable, straight into the PC (no hub).")
            time.sleep(1)
            return

        sock, last_error = None, ""
        for dev in usb_devices:
            try:
                sock = usbmux.connect(dev["id"], self.port)
                break
            except (usbmux.UsbmuxError, OSError) as e:
                last_error = str(e)
        if sock is None:
            self._set("waiting", "iPhone found — open the Akrem Cam app on it",
                      "If the iPhone asks \"Trust This Computer?\", tap Trust. (%s)" % last_error)
            time.sleep(1)
            return

        self._sock = sock
        self._set("connected", "Connected to iPhone", "")
        try:
            self._receive(sock)
        except (OSError, ConnectionError) as e:
            log.info("link closed: %s", e)
        finally:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            with self.lock:
                self.info = {}
            if self.running:
                self._set("waiting", "iPhone disconnected — reconnecting…", "")
                time.sleep(0.5)

    def _receive(self, sock):
        sock.settimeout(1.0)
        idle_since = time.monotonic()
        while self.running:
            try:
                msg_type, payload = read_message(sock)
            except socket.timeout:
                if time.monotonic() - idle_since > 2:
                    self._set("connected", "Connected — waiting for video",
                              "Keep the Akrem Cam app open on the iPhone screen.")
                continue
            if msg_type == MSG_JPEG:
                idle_since = time.monotonic()
                self.slot.put(payload)
                if self.status != "Connected to iPhone":
                    self._set("connected", "Connected to iPhone", "")
            elif msg_type == MSG_INFO:
                try:
                    info = json.loads(payload.decode("utf-8"))
                    with self.lock:
                        self.info = info if isinstance(info, dict) else {}
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# Pictures -> virtual camera
# ---------------------------------------------------------------------------

def load_font(size):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_placeholder(width, height, line1, line2=""):
    img = Image.new("RGB", (width, height), (18, 24, 38))
    draw = ImageDraw.Draw(img)
    big = load_font(max(20, height // 14))
    small = load_font(max(14, height // 30))
    for text, font, y, color in (
        (line1, big, height * 0.44, (235, 240, 250)),
        (line2, small, height * 0.56, (150, 165, 190)),
    ):
        if not text:
            continue
        box = draw.textbbox((0, 0), text, font=font)
        tw = box[2] - box[0]
        draw.text(((width - tw) / 2, y), text, font=font, fill=color)
    return img


def fit_to(img, width, height):
    """Scales the picture to fit the camera size, adding black bars if needed."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    iw, ih = img.size
    if (iw, ih) == (width, height):
        return img
    scale = min(width / iw, height / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    img = img.resize((nw, nh), Image.BILINEAR)
    if (nw, nh) == (width, height):
        return img
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(img, ((width - nw) // 2, (height - nh) // 2))
    return canvas


def jpeg_to_bgr(jpeg, width, height):
    img = Image.open(io.BytesIO(jpeg))
    iw, ih = img.size
    if iw >= width * 2 and ih >= height * 2:
        img.draft("RGB", (width, height))  # fast shrink while decoding
    img.load()
    return fit_to(img, width, height).tobytes("raw", "BGR")


class CameraPump(threading.Thread):
    """Feeds the newest iPhone picture into the virtual camera."""

    def __init__(self, slot, link, size):
        super().__init__(daemon=True)
        self.slot = slot
        self.link = link
        self.size = size
        self.running = True
        self.error = None
        self.fps = 0
        self.in_use = False
        self._new_size = None
        self._lock = threading.Lock()

    def resize(self, size):
        with self._lock:
            self._new_size = size

    def stop(self):
        self.running = False

    def run(self):
        self.cam = None
        try:
            self.cam = virtualcam.VirtualCamera(*self.size)
            self._loop()
        except Exception as e:
            log.exception("camera error")
            self.error = str(e)
        finally:
            if self.cam:
                self.cam.close()

    def _loop(self):
        seq = 0
        last_frame = 0.0
        last_placeholder = 0.0
        placeholder = None
        placeholder_key = None
        count, count_start = 0, time.monotonic()

        while self.running:
            with self._lock:
                new_size, self._new_size = self._new_size, None
            if new_size and new_size != self.size:
                self.cam.close()
                self.size = new_size
                self.cam = virtualcam.VirtualCamera(*self.size)
                placeholder_key = None
            cam = self.cam

            w, h = self.size
            jpeg, new_seq = self.slot.wait_newer(seq, 0.2)
            now = time.monotonic()

            if new_seq != seq and jpeg:
                seq = new_seq
                try:
                    cam.send(jpeg_to_bgr(jpeg, w, h))
                    last_frame = now
                    count += 1
                except Exception as e:  # a broken picture: skip it
                    log.warning("bad frame: %s", e)
            elif now - last_frame > 1.5 and now - last_placeholder > 0.2:
                state, status, _, _ = self.link.snapshot()
                key = (w, h, status)
                if key != placeholder_key:
                    placeholder = make_placeholder(w, h, "Akrem Cam", status).tobytes("raw", "BGR")
                    placeholder_key = key
                cam.send(placeholder)
                last_placeholder = now

            if now - count_start >= 1.0:
                self.fps = round(count / (now - count_start))
                count, count_start = 0, now
                self.in_use = cam.in_use()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

def resource(name):
    return os.path.join(virtualcam.bundled_dir() if getattr(sys, "frozen", False)
                        else os.path.dirname(os.path.abspath(__file__)), name)


class App:
    PREVIEW_W, PREVIEW_H = 560, 315
    BG, PANEL, FG, DIM, ACCENT = "#0f1623", "#172133", "#eef2fa", "#93a1bd", "#f5c518"

    def __init__(self, root, settings, slot, link, pump):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = root
        self.settings = settings
        self.slot = slot
        self.link = link
        self.pump = pump
        self.preview_seq = -1
        self.photo = None
        self.touched = {}        # control -> time the user last moved it
        self.pending = {}        # throttled commands waiting to be sent
        self.lens_buttons = []
        self.lens_names = []
        self.synced = {}         # slider -> value last shown from the iPhone

        root.title(APP_NAME)
        root.configure(bg=self.BG)
        root.resizable(False, False)
        try:
            root.iconbitmap(resource("icon.ico"))
        except Exception:
            pass

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", padding=3)
        style.configure("Akrem.Horizontal.TScale", background=self.ACCENT, troughcolor="#24324a",
                        bordercolor=self.PANEL, lightcolor=self.ACCENT, darkcolor=self.ACCENT,
                        sliderlength=16)

        BG, FG, DIM = self.BG, self.FG, self.DIM
        outer = tk.Frame(root, bg=BG, padx=18, pady=16)
        outer.pack(fill="both", expand=True)

        # ---- left: preview + status
        left = tk.Frame(outer, bg=BG)
        left.pack(side="left", fill="y")

        tk.Label(left, text=APP_NAME, font=("Segoe UI", 18, "bold"), bg=BG, fg=FG).pack(anchor="w")
        tk.Label(left, text="Your iPhone as a webcam — over the USB cable", font=("Segoe UI", 10),
                 bg=BG, fg=DIM).pack(anchor="w", pady=(0, 10))

        self.preview = tk.Label(left, bg="#000000", width=self.PREVIEW_W, height=self.PREVIEW_H)
        self.preview.pack()

        status_row = tk.Frame(left, bg=BG)
        status_row.pack(fill="x", pady=(12, 0))
        self.dot = tk.Canvas(status_row, width=14, height=14, bg=BG, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self.dot_id = self.dot.create_oval(2, 2, 13, 13, fill="#f0a020", outline="")
        self.status = tk.Label(status_row, text="Starting…", font=("Segoe UI", 12, "bold"),
                               bg=BG, fg=FG, anchor="w")
        self.status.pack(side="left", fill="x")

        self.detail = tk.Label(left, text="", font=("Segoe UI", 9), bg=BG, fg=DIM, anchor="w",
                               justify="left", wraplength=self.PREVIEW_W)
        self.detail.pack(fill="x", pady=(2, 0))
        self.stats = tk.Label(left, text="", font=("Segoe UI", 9), bg=BG, fg=DIM, anchor="w")
        self.stats.pack(fill="x", pady=(2, 0))
        self.warning = tk.Label(left, text="", font=("Segoe UI", 9, "bold"), bg=BG, fg="#f0a020", anchor="w")
        self.warning.pack(fill="x")

        help_text = ("1. Plug the iPhone into this PC with the cable.   "
                     "2. Open Akrem Cam on the iPhone.\n"
                     "3. In Zoom / Telegram / OBS / Meet, choose the camera \"Akrem Cam\".")
        tk.Label(left, text=help_text, font=("Segoe UI", 9), bg=BG, fg=DIM, justify="left",
                 anchor="w").pack(fill="x", pady=(10, 0))

        # ---- right: iPhone controls
        right = tk.Frame(outer, bg=self.PANEL, padx=14, pady=12)
        right.pack(side="left", fill="y", padx=(16, 0))
        self.controls_frame = right

        tk.Label(right, text="iPhone camera", font=("Segoe UI", 12, "bold"), bg=self.PANEL,
                 fg=FG).pack(anchor="w", pady=(0, 8))

        phone = tk.Frame(right, bg=self.PANEL)
        phone.pack(fill="x")
        self.phone_frame = phone

        self.side_var = tk.StringVar(value="Back")
        row = self._row(phone, "Camera")
        for name in ("Back", "Front"):
            self._toggle_button(row, name, self.side_var, name,
                                lambda n=name: self.command("side", side=n)).pack(side="left", padx=(0, 4))

        self.lens_row = self._row(phone, "Lens")
        self.lens_var = tk.StringVar(value="1x")

        self.zoom_var = tk.DoubleVar(value=1.0)
        self.zoom_scale = self._slider(phone, "Zoom", self.zoom_var, 1.0, 10.0, 0.1,
                                       lambda v: self.slider_moved("zoom", float(v)))

        self.exposure_var = tk.DoubleVar(value=0.0)
        self._slider(phone, "Brightness", self.exposure_var, -2.0, 2.0, 0.1,
                     lambda v: self.slider_moved("exposure", float(v)))

        self.quality_var = tk.StringVar(value="1080p")
        row = self._row(phone, "Quality")
        for q in ("720p", "1080p"):
            self._toggle_button(row, q, self.quality_var, q,
                                lambda q=q: self.command("quality", quality=q)).pack(side="left", padx=(0, 4))

        self.fps_var = tk.StringVar(value="30")
        row = self._row(phone, "Smoothness")
        for f in ("30", "60"):
            self._toggle_button(row, f + " fps", self.fps_var, f,
                                lambda f=f: self.command("fps", fps=int(f))).pack(side="left", padx=(0, 4))

        self.rotation_var = tk.StringVar(value="Auto")
        row = self._row(phone, "Rotation")
        rot = ttk.Combobox(row, textvariable=self.rotation_var, state="readonly", width=18,
                           values=["Auto", "Landscape", "Landscape (flipped)", "Portrait"])
        rot.pack(side="left")
        rot.bind("<<ComboboxSelected>>",
                 lambda e: self.command("orientation", orientation=self.rotation_var.get()))

        checks = tk.Frame(phone, bg=self.PANEL)
        checks.pack(fill="x", pady=(8, 0))
        self.mirror_var = tk.BooleanVar(value=False)
        self.torch_var = tk.BooleanVar(value=False)
        self.screen_var = tk.BooleanVar(value=False)
        self._check(checks, "Mirror picture", self.mirror_var,
                    lambda: self.command("mirror", mirror=self.mirror_var.get())).pack(anchor="w")
        self.torch_check = self._check(checks, "iPhone light (flash)", self.torch_var,
                                       lambda: self.command("torch", torch=self.torch_var.get()))
        self.torch_check.pack(anchor="w")
        self._check(checks, "Dark iPhone screen (saves battery)", self.screen_var,
                    lambda: self.command("screenOff", screenOff=self.screen_var.get())).pack(anchor="w")

        tk.Button(phone, text="Focus on centre", command=lambda: self.command("focus", focusCenter=True),
                  font=("Segoe UI", 9), bg="#24324a", fg=FG, activebackground="#31425f",
                  activeforeground=FG, relief="flat", padx=10, pady=3).pack(anchor="w", pady=(8, 0))

        # ---- PC settings
        tk.Frame(right, bg="#2a3850", height=1).pack(fill="x", pady=12)
        tk.Label(right, text="This PC", font=("Segoe UI", 12, "bold"), bg=self.PANEL,
                 fg=FG).pack(anchor="w", pady=(0, 6))
        row = self._row(right, "Webcam size")
        self.size_var = tk.StringVar(value=settings.output)
        combo = ttk.Combobox(row, textvariable=self.size_var, values=list(OUTPUT_SIZES),
                             state="readonly", width=8)
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", self.on_size)

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self._check(right, "Start with Windows", self.autostart_var, self.on_autostart).pack(anchor="w", pady=(6, 0))
        tk.Button(right, text="Repair camera", command=self.on_repair, font=("Segoe UI", 9),
                  bg="#24324a", fg=FG, activebackground="#31425f", activeforeground=FG,
                  relief="flat", padx=10, pady=3).pack(anchor="w", pady=(8, 0))

        self.phone_widgets = []
        self._collect_phone_widgets(phone)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    # ---- small widget helpers

    def _row(self, parent, label):
        tk = self.tk
        row = tk.Frame(parent, bg=self.PANEL)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, width=11, anchor="w", font=("Segoe UI", 9), bg=self.PANEL,
                 fg=self.DIM).pack(side="left")
        return row

    def _toggle_button(self, parent, text, var, value, command):
        return self.tk.Radiobutton(
            parent, text=text, variable=var, value=value, command=command, indicatoron=0,
            font=("Segoe UI", 9, "bold"), width=6, bg="#24324a", fg=self.FG, selectcolor=self.ACCENT,
            activebackground="#31425f", activeforeground=self.FG, relief="flat", bd=0, pady=3)

    def _slider(self, parent, label, var, lo, hi, step, command):
        from tkinter import ttk
        row = self._row(parent, label)
        value_label = self.tk.Label(row, width=5, anchor="e", font=("Segoe UI", 9), bg=self.PANEL, fg=self.FG)

        def show(*_):
            value_label.configure(text="%.1f" % var.get())

        def moved(v):
            v = round(float(v) / step) * step
            show()
            command(v)

        scale = ttk.Scale(row, variable=var, from_=lo, to=hi, orient="horizontal", length=170,
                          command=moved, style="Akrem.Horizontal.TScale")
        scale.pack(side="left")
        value_label.pack(side="left", padx=(6, 0))
        var.trace_add("write", show)
        show()
        return scale

    def _check(self, parent, text, var, command):
        return self.tk.Checkbutton(parent, text=text, variable=var, command=command, bg=self.PANEL,
                                   fg=self.FG, selectcolor="#24324a", activebackground=self.PANEL,
                                   activeforeground=self.FG, font=("Segoe UI", 9), bd=0,
                                   highlightthickness=0)

    def _collect_phone_widgets(self, frame):
        for child in frame.winfo_children():
            cls = child.winfo_class()
            if cls in ("Radiobutton", "TScale", "Checkbutton", "TCombobox", "Button"):
                self.phone_widgets.append(child)
            self._collect_phone_widgets(child)

    # ---- sending settings to the iPhone

    def command(self, key, throttle=False, **values):
        self.touched[key] = time.monotonic()
        if not throttle:
            self.link.send_command(**values)
            return
        first = key not in self.pending
        self.pending[key] = values
        if first:
            self.root.after(70, lambda: self._flush(key))

    def slider_moved(self, key, value):
        # Ignore the echo when we moved the slider ourselves to show the iPhone's value.
        if abs(self.synced.get(key, 1e9) - value) < 1e-6:
            return
        self.synced.pop(key, None)
        self.command(key, throttle=True, **{key: value})

    def _flush(self, key):
        values = self.pending.pop(key, None)
        if values:
            self.link.send_command(**values)

    def _recent(self, key):
        return time.monotonic() - self.touched.get(key, 0) < 1.5

    def sync_from_phone(self, info):
        """Shows the iPhone's current settings (unless the user is changing them right now)."""
        if not info:
            return
        if not self._recent("side") and info.get("camera"):
            self.side_var.set(info["camera"])
        lenses = info.get("lenses") or []
        if lenses != self.lens_names:
            self.lens_names = lenses
            for b in self.lens_buttons:
                b.destroy()
            self.lens_buttons = []
            for name in lenses:
                b = self._toggle_button(self.lens_row, name, self.lens_var, name,
                                        lambda n=name: self.command("lens", lens=n))
                b.pack(side="left", padx=(0, 4))
                self.lens_buttons.append(b)
        if not self._recent("lens") and info.get("lens"):
            self.lens_var.set(info["lens"])
        max_zoom = float(info.get("maxZoom") or 1.0)
        self.zoom_scale.configure(to=max(1.1, max_zoom))
        for key, var in (("zoom", self.zoom_var), ("exposure", self.exposure_var)):
            if not self._recent(key) and key in info:
                value = round(float(info[key]), 1)
                if abs(var.get() - value) > 1e-6:
                    self.synced[key] = value
                    var.set(value)
        if not self._recent("quality") and info.get("quality"):
            self.quality_var.set(info["quality"])
        if not self._recent("fps") and info.get("fps"):
            self.fps_var.set(str(info["fps"]))
        if not self._recent("orientation") and info.get("orientation"):
            self.rotation_var.set(info["orientation"])
        if not self._recent("mirror") and "mirror" in info:
            self.mirror_var.set(bool(info["mirror"]))
        if not self._recent("torch") and "torch" in info:
            self.torch_var.set(bool(info["torch"]))
        if not self._recent("screenOff") and "screenOff" in info:
            self.screen_var.set(bool(info["screenOff"]))
        self.torch_check.configure(state="normal" if info.get("torchAvailable") else "disabled")

    def set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for w in self.phone_widgets + self.lens_buttons:
            try:
                if w.winfo_class() == "TScale":
                    w.state(["!disabled"] if enabled else ["disabled"])
                else:
                    w.configure(state="readonly" if (enabled and w.winfo_class() == "TCombobox") else state)
            except Exception:
                pass

    # ---- PC settings

    def on_size(self, _event=None):
        value = self.size_var.get()
        if value in OUTPUT_SIZES:
            self.settings.output = value
            self.settings.save()
            self.pump.resize(OUTPUT_SIZES[value])

    def on_autostart(self):
        from tkinter import messagebox
        if not set_autostart(self.autostart_var.get()):
            self.autostart_var.set(autostart_enabled())
            messagebox.showinfo(APP_NAME, "This option only works in the installed AkremCam.exe.")

    def on_repair(self):
        from tkinter import messagebox
        ok = install_camera(force=True)
        if ok:
            messagebox.showinfo(APP_NAME, "Camera repaired. Restart Zoom / Telegram / OBS so they see it.")
        else:
            messagebox.showwarning(APP_NAME, "The camera could not be installed. "
                                             "Click Repair again and choose \"Yes\" when Windows asks.")

    def on_close(self):
        self.link.stop()
        self.pump.stop()
        self.root.after(300, self.root.destroy)

    # ---- refresh loop

    def tick(self):
        state, status, detail, info = self.link.snapshot()
        if self.pump.error:
            state, status, detail = "error", "Camera problem", self.pump.error

        colors = {"connected": "#27c46b", "waiting": "#f0a020", "error": "#e5484d"}
        self.dot.itemconfigure(self.dot_id, fill=colors.get(state, "#f0a020"))
        self.status.configure(text=status)
        self.detail.configure(text=detail)

        connected = state == "connected" and bool(info)
        parts = []
        if state == "connected":
            parts.append("%d fps" % self.pump.fps)
            if info.get("width"):
                parts.append("%s×%s from iPhone" % (info["width"], info["height"]))
            if info.get("device"):
                parts.append(str(info["device"]))
        parts.append("in use by an app" if self.pump.in_use else "not in use by any app yet")
        self.stats.configure(text="  •  ".join(parts))

        warnings = []
        if info.get("hot"):
            warnings.append("iPhone is hot — take it out of its case or choose 30 fps.")
        if info.get("fps") and info.get("actualFps") and info["actualFps"] < info["fps"]:
            warnings.append("This camera can't do %s fps; using %s." % (info["fps"], info["actualFps"]))
        self.warning.configure(text="  ".join(warnings))

        self.set_controls_enabled(connected)
        if connected:
            self.sync_from_phone(info)

        self.update_preview(state == "connected")
        self.root.after(100, self.tick)

    def update_preview(self, connected):
        from PIL import ImageTk
        jpeg, seq = self.slot.latest()
        if connected and jpeg and seq != self.preview_seq:
            self.preview_seq = seq
            try:
                img = Image.open(io.BytesIO(jpeg))
                img.draft("RGB", (self.PREVIEW_W, self.PREVIEW_H))
                img.load()
                img = fit_to(img, self.PREVIEW_W, self.PREVIEW_H)
            except Exception:
                return
        elif not connected and self.preview_seq != -2:
            self.preview_seq = -2
            img = make_placeholder(self.PREVIEW_W, self.PREVIEW_H, "No video yet",
                                   "Connect your iPhone")
        else:
            return
        self.photo = ImageTk.PhotoImage(img)
        self.preview.configure(image=self.photo, width=self.PREVIEW_W, height=self.PREVIEW_H)


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

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
            winreg.QueryValueEx(key, "AkremCam")
        return True
    except OSError:
        return False


def set_autostart(enabled):
    """Adds/removes AkremCam from programs that start with Windows (minimised)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, "AkremCam", 0, winreg.REG_SZ,
                                  '"%s" --minimized' % sys.executable)
            else:
                try:
                    winreg.DeleteValue(key, "AkremCam")
                except OSError:
                    pass
        return True
    except OSError:
        return False


def install_camera(force=False):
    """Makes sure the 'Akrem Cam' webcam is installed in Windows. Returns True if OK."""
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
    bgr = jpeg_to_bgr(buf.getvalue(), 1280, 720)
    assert len(bgr) == 1280 * 720 * 3, "wrong frame size"
    b, g, r = bgr[0], bgr[1], bgr[2]
    assert r > 150 and b < 80, "colour order wrong: %r" % ((b, g, r),)
    portrait = Image.new("RGB", (1080, 1920), (0, 200, 0))
    framed = fit_to(portrait, 1920, 1080)
    assert framed.size == (1920, 1080) and framed.getpixel((0, 0)) == (0, 0, 0)
    make_placeholder(1920, 1080, "Akrem Cam", "self test")
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

    root = tk.Tk()
    root.withdraw()

    if sys.platform == "win32" and not virtualcam.is_registered():
        messagebox.showinfo(APP_NAME, "Akrem Cam will now install its webcam (one time only).\n\n"
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
    pump = CameraPump(slot, link, OUTPUT_SIZES[settings.output])
    pump.start()
    time.sleep(0.2)
    if pump.error and "already running" in pump.error:
        messagebox.showinfo(APP_NAME, "Akrem Cam is already open.")
        return
    link.start()

    App(root, settings, slot, link, pump)
    root.deiconify()
    if "--minimized" in args:
        root.iconify()
    root.mainloop()
    log.info("closed")


if __name__ == "__main__":
    main()
