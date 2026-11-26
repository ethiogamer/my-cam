"""The CamBridge main window.

 ┌ top bar:  ☰  CamBridge  ● status            profile ▾   ⏸ Pause   ● Record   ☰ ┐
 │ left sidebar        │            live preview              │ right sidebar      │
 │ (iPhone camera:     │   drag = move, scroll = zoom,         │ (picture: filters, │
 │  lens, exposure,    │   double-click = reset framing        │  adjust, framing,  │
 │  focus, colour…)    │                                       │  output, app)      │
 └ bottom bar: hint · notices · in use · fps                                        ┘
"""

import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

import processing
import recorder

C = {
    "bg": "#0b0f17", "bar": "#101724", "side": "#111a29", "card": "#172235", "line": "#243149",
    "fg": "#eef2fa", "dim": "#8d99b2", "faint": "#5d6a85", "accent": "#f5c518", "accent_fg": "#111111",
    "red": "#ef4444", "green": "#22c55e", "orange": "#f59e0b", "button": "#22304a", "hover": "#2e3e5e",
}
FONT = "Segoe UI"
SIDE_W = 300

PROFILES = {
    "Meeting · 720p 30fps": ("720p", "720p", 30),
    "HD · 1080p 30fps": ("1080p", "1080p", 30),
    "Smooth · 1080p 60fps": ("1080p", "1080p", 60),
    "4K · 30fps": ("4K", "4K", 30),
    "4K · 60fps (OBS / recording)": ("4K", "4K", 60),
}


def shutter_text(seconds):
    if not seconds or seconds <= 0:
        return "—"
    if seconds >= 0.5:
        return "%.1fs" % seconds
    return "1/%d" % round(1.0 / seconds)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class ScrollColumn(tk.Frame):
    """A sidebar that scrolls with the mouse wheel."""

    def __init__(self, parent, width):
        super().__init__(parent, bg=C["side"], width=width)
        self.canvas = tk.Canvas(self, bg=C["side"], highlightthickness=0, bd=0, width=width)
        self.inner = tk.Frame(self.canvas, bg=C["side"])
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw", width=width)
        self.canvas.pack(fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))

    def contains(self, widget):
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def scroll(self, delta):
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")


class Segmented(tk.Frame):
    """Row of buttons where one is selected (like a phone's segmented control)."""

    def __init__(self, parent, options, variable, command=None, width=None):
        super().__init__(parent, bg=C["button"], padx=2, pady=2)
        self.variable = variable
        self.command = command
        self.buttons = {}
        self.enabled = True
        self.set_options(options, width)
        variable.trace_add("write", lambda *_: self.refresh())

    def set_options(self, options, width=None):
        for b in self.buttons.values():
            b.destroy()
        self.buttons = {}
        for value, text in options:
            b = tk.Label(self, text=text, font=(FONT, 9, "bold"), bg=C["button"], fg=C["fg"],
                         padx=8, pady=3, cursor="hand2")
            if width:
                b.configure(width=width)
            b.pack(side="left", fill="x", expand=True)
            b.bind("<Button-1>", lambda e, v=value: self._click(v))
            self.buttons[value] = b
        self.refresh()

    def _click(self, value):
        if not self.enabled:
            return
        self.variable.set(value)
        if self.command:
            self.command(value)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.refresh()

    def refresh(self):
        current = self.variable.get()
        for value, b in self.buttons.items():
            selected = str(value) == str(current)
            if not self.enabled:
                b.configure(bg=C["button"], fg=C["faint"], cursor="")
            elif selected:
                b.configure(bg=C["accent"], fg=C["accent_fg"], cursor="hand2")
            else:
                b.configure(bg=C["button"], fg=C["fg"], cursor="hand2")


class Switch(tk.Frame):
    """Text on the left, on/off switch on the right."""

    def __init__(self, parent, text, variable, command=None, bg=None):
        bg = bg or C["card"]
        super().__init__(parent, bg=bg)
        self.variable = variable
        self.command = command
        self.enabled = True
        self.label = tk.Label(self, text=text, font=(FONT, 9), bg=bg, fg=C["fg"], anchor="w")
        self.label.pack(side="left", fill="x", expand=True)
        self.canvas = tk.Canvas(self, width=34, height=18, bg=bg, highlightthickness=0, cursor="hand2")
        self.canvas.pack(side="right")
        for w in (self.canvas, self.label):
            w.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self.draw())
        self.draw()

    def _click(self, _e=None):
        if not self.enabled:
            return
        self.variable.set(not self.variable.get())
        if self.command:
            self.command(self.variable.get())

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.label.configure(fg=C["fg"] if enabled else C["faint"])
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        on = bool(self.variable.get())
        track = C["accent"] if on and self.enabled else (C["line"] if self.enabled else C["button"])
        c.create_oval(1, 1, 17, 17, fill=track, outline=track)
        c.create_oval(17, 1, 33, 17, fill=track, outline=track)
        c.create_rectangle(9, 1, 25, 17, fill=track, outline=track)
        knob = "#ffffff" if self.enabled else C["faint"]
        x = 17 if on else 1
        c.create_oval(x + 2, 3, x + 14, 15, fill=knob, outline=knob)


GRADIENTS = {
    "temperature": ["#3b82f6", "#e5e7eb", "#facc15"],
    "warmth": ["#3b82f6", "#e5e7eb", "#facc15"],
    "tint": ["#22c55e", "#e5e7eb", "#d946ef"],
    "hue": ["#ef4444", "#f59e0b", "#22c55e", "#06b6d4", "#3b82f6", "#a855f7", "#ef4444"],
    "saturation": ["#6b7280", "#ef4444"],
    "vibrance": ["#6b7280", "#f97316"],
    "brightness": ["#111111", "#f5f5f5"],
    "exposure": ["#111111", "#f5f5f5"],
    "gamma": ["#facc15", "#1f2937"],
    "contrast": ["#6b7280", "#111111"],
}


def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class Slider(tk.Frame):
    """Label + value on one line, a drawn slider underneath (optionally with a colour track)."""

    H = 22

    def __init__(self, parent, text, lo, hi, value, fmt, command, bg=None, colors=None, default=None):
        bg = bg or C["card"]
        super().__init__(parent, bg=bg)
        self.fmt = fmt
        self.command = command
        self.lo, self.hi = float(lo), float(hi)
        self.colors = colors
        self.default = value if default is None else default
        self.enabled = True
        self.var = tk.DoubleVar(value=value)
        self._echo = None
        top = tk.Frame(self, bg=bg)
        top.pack(fill="x")
        self.label = tk.Label(top, text=text, font=(FONT, 9), bg=bg, fg=C["dim"], anchor="w", cursor="hand2")
        self.label.pack(side="left")
        self.label.bind("<Double-Button-1>", lambda e: self._set_user(self.default))
        self.value_label = tk.Label(top, font=(FONT, 9, "bold"), bg=bg, fg=C["fg"], anchor="e")
        self.value_label.pack(side="right")
        self.canvas = tk.Canvas(self, height=self.H, bg=bg, highlightthickness=0, cursor="hand2")
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Button-1>", self._drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<Double-Button-1>", lambda e: self._set_user(self.default))
        self.var.trace_add("write", lambda *_: (self.show(), self.draw()))
        self.show()

    # Compatibility with the old ttk-based slider
    @property
    def scale(self):
        return self

    def configure_range(self, lo, hi):
        self.set_range(lo, hi)

    def show(self):
        try:
            self.value_label.configure(text=self.fmt(self.var.get()))
        except (tk.TclError, ValueError):
            pass

    def _x_range(self):
        w = max(40, self.canvas.winfo_width())
        return 9, w - 9

    def draw(self):
        c = self.canvas
        c.delete("all")
        x0, x1 = self._x_range()
        y = self.H // 2
        span = max(1e-9, self.hi - self.lo)
        t = (min(self.hi, max(self.lo, self.var.get())) - self.lo) / span
        kx = x0 + t * (x1 - x0)
        dim = not self.enabled
        if self.colors:
            stops = self.colors
            steps = 48
            for i in range(steps):
                f = i / (steps - 1)
                seg = f * (len(stops) - 1)
                j = min(len(stops) - 2, int(seg))
                color = _mix(stops[j], stops[j + 1], seg - j)
                if dim:
                    color = _mix(color, C["card"], 0.65)
                xa = x0 + (x1 - x0) * i / steps
                xb = x0 + (x1 - x0) * (i + 1) / steps + 1
                c.create_rectangle(xa, y - 3, xb, y + 3, fill=color, outline="")
        else:
            c.create_rectangle(x0, y - 2, x1, y + 2, fill=C["line"], outline="")
            if not dim:
                zero = x0 + (min(self.hi, max(self.lo, 0.0)) - self.lo) / span * (x1 - x0) if self.lo < 0 < self.hi else x0
                c.create_rectangle(min(zero, kx), y - 2, max(zero, kx), y + 2, fill=C["accent"], outline="")
        knob = C["faint"] if dim else "#ffffff"
        c.create_oval(kx - 8, y - 8, kx + 8, y + 8, fill=knob, outline=C["bg"], width=2)

    def _drag(self, e):
        if not self.enabled:
            return
        x0, x1 = self._x_range()
        t = min(1.0, max(0.0, (e.x - x0) / max(1, x1 - x0)))
        self._set_user(self.lo + t * (self.hi - self.lo))

    def _set_user(self, value):
        if not self.enabled:
            return
        self._echo = None
        self.var.set(value)
        self.command(float(value))

    def set_quietly(self, value):
        """Shows a value that came from the phone without sending it back."""
        if abs(self.var.get() - value) > 1e-6:
            self.var.set(value)

    def set_range(self, lo, hi):
        lo, hi = float(lo), float(hi)
        if (lo, hi) != (self.lo, self.hi):
            self.lo, self.hi = lo, hi
            self.draw()

    def set_enabled(self, enabled):
        if enabled == self.enabled:
            return
        self.enabled = enabled
        self.label.configure(fg=C["dim"] if enabled else C["faint"])
        self.value_label.configure(fg=C["fg"] if enabled else C["faint"])
        self.draw()


def flat_button(parent, text, command, bg=None, fg=None, bold=False, padx=10, pady=4):
    bg = bg or C["button"]
    b = tk.Label(parent, text=text, font=(FONT, 9, "bold" if bold else "normal"), bg=bg,
                 fg=fg or C["fg"], padx=padx, pady=pady, cursor="hand2")
    b.bind("<Button-1>", lambda e: command())
    b.bind("<Enter>", lambda e: b.configure(bg=C["hover"]) if b.cget("bg") == bg else None)
    b.bind("<Leave>", lambda e: b.configure(bg=bg) if b.cget("bg") == C["hover"] else None)
    return b


def apply_priority(high):
    """High performance mode: raise CamBridge's priority on Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        HIGH, NORMAL = 0x00000080, 0x00000020
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),
                                                HIGH if high else NORMAL)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tray icon (next to the clock) — keeps the camera running with the window closed
# ---------------------------------------------------------------------------

def start_tray(app, icon_path):
    try:
        import pystray
    except Exception:
        return None
    try:
        image = Image.open(icon_path).convert("RGBA").resize((64, 64))
    except Exception:
        image = Image.new("RGBA", (64, 64), (30, 100, 210, 255))

    def act(name):
        return lambda icon, item: app.ui_queue.put(name)

    menu = pystray.Menu(
        pystray.MenuItem("Open CamBridge", act("show"), default=True),
        pystray.MenuItem(lambda item: "Resume camera" if app.pump.paused else "Pause camera", act("pause")),
        pystray.MenuItem(lambda item: "Stop recording" if app.pump.recorder else "Start recording", act("record")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit CamBridge", act("quit")),
    )
    icon = pystray.Icon("CamBridge", image, "CamBridge", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App:
    def __init__(self, root, settings, slot, link, pump, core=None, wifi=None, audio=None, start_minimized=False):
        self.root = root
        self.settings = settings
        self.slot = slot
        self.link = link
        self.pump = pump
        self.core = core or sys.modules.get("__main__")
        self.wifi = wifi
        self.audio = audio
        self._perf_tick = 0.0
        self.wifi_window = None
        self.qr_photo = None
        self.ui_queue = queue.Queue()
        self.touched = {}
        self.pending = {}
        self.phone_controls = []
        self.info = {}
        self.connected = False
        self.photo = None
        self.preview_key = None
        self.lens_names = None
        self.toast_until = 0
        self.drag_from = None
        self.quitting = False

        root.title("CamBridge")
        root.configure(bg=C["bg"])
        root.geometry("1320x800")
        root.minsize(1000, 620)
        try:
            root.iconbitmap(self._resource("icon.ico"))
        except Exception:
            pass
        self._styles()

        self._build_topbar()
        self._build_bottombar()
        self.body = tk.Frame(root, bg=C["bg"])
        self.body.pack(fill="both", expand=True)
        self.left = ScrollColumn(self.body, SIDE_W)
        self.right = ScrollColumn(self.body, SIDE_W)
        self.center = tk.Frame(self.body, bg=C["bg"])
        self._build_left(self.left.inner)
        self._build_right(self.right.inner)
        self._build_center(self.center)
        self._layout_sidebars()

        root.bind_all("<MouseWheel>", self._on_wheel)
        root.bind("<Control-p>", lambda e: self.toggle_pause())
        root.bind("<Control-r>", lambda e: self.toggle_record())
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.tray = start_tray(self, self._resource("icon.ico"))
        if start_minimized and self.tray:
            root.withdraw()
        else:
            root.deiconify()
            if start_minimized:
                root.iconify()
        self._tick()

    # ---- setup helpers -----------------------------------------------------

    def _resource(self, name):
        if self.core and hasattr(self.core, "resource"):
            return self.core.resource(name)
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)

    def _styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("CamBridge.Horizontal.TScale", background=C["accent"], troughcolor=C["line"],
                        bordercolor=C["card"], lightcolor=C["accent"], darkcolor=C["accent"],
                        sliderlength=14, sliderthickness=14)
        style.map("CamBridge.Horizontal.TScale", background=[("disabled", C["faint"])])
        style.configure("CamBridge.TCombobox", fieldbackground=C["button"], background=C["button"],
                        foreground=C["fg"], arrowcolor=C["fg"], bordercolor=C["line"],
                        lightcolor=C["button"], darkcolor=C["button"], padding=4)
        style.map("CamBridge.TCombobox", fieldbackground=[("readonly", C["button"])],
                  foreground=[("readonly", C["fg"]), ("disabled", C["faint"])],
                  selectbackground=[("readonly", C["button"])], selectforeground=[("readonly", C["fg"])])
        self.root.option_add("*TCombobox*Listbox.background", C["card"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", C["accent_fg"])

    def _card(self, parent, title, subtitle=None):
        outer = tk.Frame(parent, bg=C["card"], padx=12, pady=10)
        outer.pack(fill="x", padx=10, pady=(0, 8))
        head = tk.Frame(outer, bg=C["card"])
        head.pack(fill="x")
        tk.Label(head, text=title.upper(), font=(FONT, 8, "bold"), bg=C["card"], fg=C["dim"]).pack(side="left")
        live = tk.Label(head, text=subtitle or "", font=(FONT, 8), bg=C["card"], fg=C["faint"])
        live.pack(side="right")
        body = tk.Frame(outer, bg=C["card"])
        body.pack(fill="x", pady=(8, 0))
        body.live = live
        return body

    def _section_title(self, parent, text):
        tk.Label(parent, text=text, font=(FONT, 11, "bold"), bg=C["side"], fg=C["fg"],
                 anchor="w").pack(fill="x", padx=14, pady=(14, 8))

    def _gap(self, parent, h=6):
        tk.Frame(parent, bg=parent.cget("bg"), height=h).pack(fill="x")

    def _phone(self, widget):
        self.phone_controls.append(widget)
        return widget

    # ---- top bar -------------------------------------------------------------

    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=C["bar"], height=52)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        flat_button(bar, "☰", lambda: self.toggle_side("left_open"), bg=C["bar"], padx=12,
                    pady=6).pack(side="left", padx=(8, 4))
        logo = tk.Canvas(bar, width=22, height=22, bg=C["bar"], highlightthickness=0)
        logo.create_oval(2, 2, 20, 20, fill="#1f6fe0", outline="")
        logo.create_oval(7, 7, 15, 15, fill="#ffffff", outline="")
        logo.pack(side="left", padx=(4, 6))
        tk.Label(bar, text="CamBridge", font=(FONT, 13, "bold"), bg=C["bar"], fg=C["fg"]).pack(side="left")

        pill = tk.Frame(bar, bg=C["button"], padx=10, pady=4)
        pill.pack(side="left", padx=16)
        self.status_dot = tk.Canvas(pill, width=10, height=10, bg=C["button"], highlightthickness=0)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_dot_id = self.status_dot.create_oval(1, 1, 9, 9, fill=C["orange"], outline="")
        self.status_text = tk.Label(pill, text="Starting…", font=(FONT, 9, "bold"), bg=C["button"], fg=C["fg"])
        self.status_text.pack(side="left")

        flat_button(bar, "☰", lambda: self.toggle_side("right_open"), bg=C["bar"], padx=12,
                    pady=6).pack(side="right", padx=(4, 8))
        self.record_btn = flat_button(bar, "●  Record", self.toggle_record, bold=True, padx=14, pady=6)
        self.record_btn.configure(fg=C["red"])
        self.record_btn.pack(side="right", padx=4)
        self.pause_btn = flat_button(bar, "❚❚  Pause", self.toggle_pause, bold=True, padx=14, pady=6)
        self.pause_btn.pack(side="right", padx=4)

        flat_button(bar, "((·))  Wi-Fi", self.open_wifi, bold=True, padx=14, pady=6).pack(side="right", padx=4)

        self.profile_var = tk.StringVar(value="Profile")
        prof = ttk.Combobox(bar, textvariable=self.profile_var, values=list(PROFILES), state="readonly",
                            width=22, style="CamBridge.TCombobox")
        prof.pack(side="right", padx=10)
        prof.bind("<<ComboboxSelected>>", lambda e: self.apply_profile(self.profile_var.get()))

    # ---- bottom bar ----------------------------------------------------------

    def _build_bottombar(self):
        bar = tk.Frame(self.root, bg=C["bar"], height=30)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        tk.Label(bar, text="In Zoom, Telegram, OBS or Meet, choose the camera “CamBridge”.",
                 font=(FONT, 9), bg=C["bar"], fg=C["dim"]).pack(side="left", padx=12)
        self.toast = tk.Label(bar, text="", font=(FONT, 9, "bold"), bg=C["bar"], fg=C["accent"], cursor="hand2")
        self.toast.pack(side="left", padx=12)
        self.toast_action = None
        self.toast.bind("<Button-1>", lambda e: self.toast_action and self.toast_action())
        self.out_label = tk.Label(bar, text="", font=(FONT, 9), bg=C["bar"], fg=C["dim"])
        self.out_label.pack(side="right", padx=12)
        self.use_label = tk.Label(bar, text="", font=(FONT, 9, "bold"), bg=C["bar"], fg=C["dim"])
        self.use_label.pack(side="right", padx=12)

    def show_toast(self, text, seconds=6, action=None):
        self.toast.configure(text=text)
        self.toast_action = action
        self.toast_until = time.monotonic() + seconds

    # ---- centre: preview -----------------------------------------------------

    def _build_center(self, parent):
        self.preview = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, cursor="fleur")
        self.preview.pack(fill="both", expand=True, padx=12, pady=12)
        self.preview_img_id = self.preview.create_image(0, 0, anchor="center")
        self.rec_badge = self.preview.create_text(0, 0, text="", anchor="nw", fill=C["red"],
                                                  font=(FONT, 11, "bold"))
        self.preview.bind("<ButtonPress-1>", self._drag_start)
        self.preview.bind("<B1-Motion>", self._drag_move)
        self.preview.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag_from", None))
        self.preview.bind("<Double-Button-1>", lambda e: self.reset_framing())
        self.warning = tk.Label(parent, text="", font=(FONT, 9, "bold"), bg=C["bg"], fg=C["orange"])
        self.warning.pack(fill="x", padx=12, pady=(0, 8))

    def _preview_box(self):
        cw = max(10, self.preview.winfo_width())
        ch = max(10, self.preview.winfo_height())
        w, h = self.pump.size
        scale = min(cw / w, ch / h)
        return cw, ch, max(1, int(w * scale)), max(1, int(h * scale))

    def _drag_start(self, e):
        self.drag_from = (e.x, e.y)

    def _drag_move(self, e):
        f = self.settings["framing"]
        if not self.drag_from or f["zoom"] <= 1.001:
            return
        _, _, dw, dh = self._preview_box()
        dx, dy = e.x - self.drag_from[0], e.y - self.drag_from[1]
        self.drag_from = (e.x, e.y)
        k = f["zoom"] - 1.0
        f["pan_x"] = max(-1.0, min(1.0, f["pan_x"] - dx * 2.0 / (dw * k)))
        f["pan_y"] = max(-1.0, min(1.0, f["pan_y"] - dy * 2.0 / (dh * k)))
        self._framing_changed()

    def _on_wheel(self, e):
        widget = self.root.winfo_containing(e.x_root, e.y_root)
        if widget is None:
            return
        if self.left.contains(widget):
            self.left.scroll(e.delta)
        elif self.right.contains(widget):
            self.right.scroll(e.delta)
        elif widget is self.preview:
            f = self.settings["framing"]
            f["zoom"] = round(max(1.0, min(4.0, f["zoom"] + (0.1 if e.delta > 0 else -0.1))), 2)
            if f["zoom"] <= 1.0:
                f["pan_x"] = f["pan_y"] = 0.0
            self._framing_changed()

    def _render_preview(self):
        cw, ch, dw, dh = self._preview_box()
        if self.connected or self.pump.paused:
            img, seq = self.pump.last_output, self.pump.output_seq
            key = ("live", seq, dw, dh)
        else:
            img, key = None, ("idle", self.status_text.cget("text"), dw, dh)
        if key == self.preview_key:
            return
        self.preview_key = key
        if img is None:
            img = processing.make_placeholder(dw, dh, "No video yet", self.status_text.cget("text"))
        else:
            img = img.resize((dw, dh), Image.BILINEAR, reducing_gap=2.0)
        self.photo = ImageTk.PhotoImage(img)
        self.preview.itemconfigure(self.preview_img_id, image=self.photo)
        self.preview.coords(self.preview_img_id, cw // 2, ch // 2)
        self.preview.coords(self.rec_badge, (cw - dw) // 2 + 14, (ch - dh) // 2 + 10)

    # ---- left sidebar: the iPhone camera -------------------------------------

    def _build_left(self, p):
        self._section_title(p, "Phone camera")

        dev = self._card(p, "Device")
        self.device_name = tk.Label(dev, text="No phone connected", font=(FONT, 11, "bold"),
                                    bg=C["card"], fg=C["fg"], anchor="w")
        self.device_name.pack(fill="x")
        self.device_sub = tk.Label(dev, text="Plug in the cable and open the app", font=(FONT, 9),
                                   bg=C["card"], fg=C["dim"], anchor="w", justify="left", wraplength=SIDE_W - 50)
        self.device_sub.pack(fill="x", pady=(0, 8))
        self.side_var = tk.StringVar(value="Back")
        tk.Label(dev, text="Camera", font=(FONT, 9), bg=C["card"], fg=C["dim"], anchor="w").pack(fill="x")
        self._phone(Segmented(dev, [("Back", "Back"), ("Front", "Front")], self.side_var,
                              lambda v: self.send("side", side=v))).pack(fill="x", pady=(2, 8))
        tk.Label(dev, text="Lens", font=(FONT, 9), bg=C["card"], fg=C["dim"], anchor="w").pack(fill="x")
        self.lens_var = tk.StringVar(value="1x")
        self.lens_seg = self._phone(Segmented(dev, [("1x", "1x")], self.lens_var,
                                              lambda v: self.send("lens", lens=v)))
        self.lens_seg.pack(fill="x", pady=(2, 8))
        self.zoom = self._phone(Slider(dev, "Zoom", 1.0, 6.0, 1.0, lambda v: "%.1fx" % v,
                                       lambda v: self.send("zoom", throttle=True, zoom=round(v, 2))))
        self.zoom.pack(fill="x")

        exp = self._card(p, "Exposure")
        self.exposure_live = exp.live
        self.exp_mode = tk.BooleanVar(value=False)
        self.exp_seg = self._phone(Segmented(exp, [(False, "Auto"), (True, "Manual")], self.exp_mode,
                                             lambda v: self._mode("exposureManual", v)))
        self.exp_seg.pack(fill="x", pady=(0, 8))
        self.exp_auto = tk.Frame(exp, bg=C["card"])
        self.bias = self._phone(Slider(self.exp_auto, "Brightness", -2.0, 2.0, 0.0, lambda v: "%+.1f" % v,
                                       lambda v: self.send("exposure", throttle=True, exposure=round(v, 2))))
        self.bias.pack(fill="x")
        self.exp_manual = tk.Frame(exp, bg=C["card"])
        self.iso = self._phone(Slider(self.exp_manual, "ISO", 25, 1600, 100, lambda v: "%d" % v,
                                      lambda v: self.send("iso", throttle=True, iso=round(v))))
        self.iso.pack(fill="x", pady=(0, 6))
        self.shutter = self._phone(Slider(self.exp_manual, "Shutter speed", 0.0, 1.0, 0.5,
                                          lambda v: shutter_text(self._pos_to_shutter(v)),
                                          lambda v: self.send("shutter", throttle=True,
                                                              shutter=self._pos_to_shutter(v))))
        self.shutter.pack(fill="x")
        self.lowlight_var = tk.BooleanVar(value=True)
        self.lowlight_sw = self._phone(Switch(exp, "Low light boost", self.lowlight_var,
                                              lambda v: self.send("lowLight", lowLight=v)))
        self.lowlight_sw.pack(fill="x", side="bottom", pady=(8, 0))

        foc = self._card(p, "Focus")
        self.focus_live = foc.live
        self.focus_mode = tk.BooleanVar(value=False)
        self.focus_seg = self._phone(Segmented(foc, [(False, "Auto"), (True, "Manual")], self.focus_mode,
                                               lambda v: self._mode("focusManual", v)))
        self.focus_seg.pack(fill="x", pady=(0, 8))
        self.focus_manual = tk.Frame(foc, bg=C["card"])
        self.lens_pos = self._phone(Slider(self.focus_manual, "Distance  (near ↔ far)", 0.0, 1.0, 0.5,
                                           lambda v: "%d%%" % round(v * 100),
                                           lambda v: self.send("lensPosition", throttle=True,
                                                               lensPosition=round(v, 3))))
        self.lens_pos.pack(fill="x", pady=(0, 6))
        self.focus_auto = tk.Frame(foc, bg=C["card"])
        self._phone(flat_button(self.focus_auto, "Focus on centre",
                                lambda: self.send("focus", focusCenter=True))).pack(anchor="w")

        wb = self._card(p, "White balance")
        self.wb_live = wb.live
        self.wb_mode = tk.BooleanVar(value=False)
        self.wb_seg = self._phone(Segmented(wb, [(False, "Auto"), (True, "Manual")], self.wb_mode,
                                            lambda v: self._mode("whiteBalanceManual", v)))
        self.wb_seg.pack(fill="x", pady=(0, 8))
        self.wb_manual = tk.Frame(wb, bg=C["card"])
        self.temp = self._phone(Slider(self.wb_manual, "Temperature", 2500, 9000, 5000, lambda v: "%dK" % v,
                                       lambda v: self.send("temperature", throttle=True, temperature=round(v)),
                                       colors=GRADIENTS["temperature"], default=5500))
        self.temp.pack(fill="x", pady=(0, 6))
        self.tint = self._phone(Slider(self.wb_manual, "Tint  (green ↔ magenta)", -100, 100, 0,
                                       lambda v: "%+d" % v,
                                       lambda v: self.send("tint", throttle=True, tint=round(v)),
                                       colors=GRADIENTS["tint"], default=0))
        self.tint.pack(fill="x")

        light = self._card(p, "Light")
        self.torch_var = tk.BooleanVar(value=False)
        self.torch_sw = self._phone(Switch(light, "Phone flash light", self.torch_var,
                                           lambda v: self.send("torch", torch=v)))
        self.torch_sw.pack(fill="x", pady=(0, 6))
        self.torch_level = self._phone(Slider(light, "Light strength", 0.05, 1.0, 0.5,
                                              lambda v: "%d%%" % round(v * 100),
                                              lambda v: self.send("torchLevel", throttle=True,
                                                                  torchLevel=round(v, 2))))
        self.torch_level.pack(fill="x")

        opt = self._card(p, "Video from phone")
        tk.Label(opt, text="Quality", font=(FONT, 9), bg=C["card"], fg=C["dim"], anchor="w").pack(fill="x")
        self.quality_var = tk.StringVar(value="1080p")
        self.quality_seg = self._phone(Segmented(opt, [("720p", "720p"), ("1080p", "1080p"), ("4K", "4K")],
                                                 self.quality_var, lambda v: self.send("quality", quality=v)))
        self.quality_seg.pack(fill="x", pady=(2, 8))
        tk.Label(opt, text="Frame rate", font=(FONT, 9), bg=C["card"], fg=C["dim"], anchor="w").pack(fill="x")
        self.fps_var = tk.StringVar(value="30")
        self._phone(Segmented(opt, [("30", "30 fps"), ("60", "60 fps")], self.fps_var,
                              lambda v: self.send("fps", fps=int(v)))).pack(fill="x", pady=(2, 8))
        tk.Label(opt, text="Video format", font=(FONT, 9), bg=C["card"], fg=C["dim"], anchor="w").pack(fill="x")
        self.codec_var = tk.StringVar(value="h264")
        self.codec_seg = self._phone(Segmented(opt, [("h264", "H.264"), ("hevc", "HEVC"), ("jpeg", "JPEG")],
                                               self.codec_var, lambda v: self.send("codec", codec=v)))
        self.codec_seg.pack(fill="x", pady=(2, 2))
        tk.Label(opt, text="H.264: best for 4K 60 and low delay. HEVC: same quality with less data "
                           "(needs Windows' HEVC extension). JPEG: simplest, uses more data.",
                 font=(FONT, 8), bg=C["card"], fg=C["faint"], justify="left", anchor="w",
                 wraplength=SIDE_W - 50).pack(fill="x", pady=(0, 8))
        tk.Label(opt, text="Bitrate (picture detail)", font=(FONT, 9), bg=C["card"], fg=C["dim"],
                 anchor="w").pack(fill="x")
        self.bitrate_var = tk.StringVar(value="auto")
        self._phone(Segmented(opt, [("auto", "Auto"), ("low", "Low"), ("high", "High"), ("max", "Max")],
                              self.bitrate_var, lambda v: self.send("bitrate", bitrate=v))).pack(fill="x", pady=(2, 8))
        tk.Label(opt, text="Phone orientation", font=(FONT, 9), bg=C["card"], fg=C["dim"],
                 anchor="w").pack(fill="x")
        self.orient_var = tk.StringVar(value="Auto")
        orient = self._phone(ttk.Combobox(opt, textvariable=self.orient_var, state="readonly",
                                          style="CamBridge.TCombobox",
                                          values=["Auto", "Landscape", "Landscape (flipped)", "Portrait"]))
        orient.pack(fill="x", pady=(2, 8))
        orient.bind("<<ComboboxSelected>>", lambda e: self.send("orientation", orientation=self.orient_var.get()))
        self.stab_var = tk.BooleanVar(value=False)
        self.stab_sw = self._phone(Switch(opt, "Stabilization (adds a little delay)", self.stab_var,
                                          lambda v: self.send("stabilization", stabilization=v)))
        self.stab_sw.pack(fill="x", pady=2)
        self.hdr_var = tk.BooleanVar(value=False)
        self.hdr_sw = self._phone(Switch(opt, "HDR video", self.hdr_var, lambda v: self.send("hdr", hdr=v)))
        self.hdr_sw.pack(fill="x", pady=2)
        self.dark_var = tk.BooleanVar(value=False)
        self._phone(Switch(opt, "Dark phone screen (saves battery)", self.dark_var,
                           lambda v: self.send("screenOff", screenOff=v))).pack(fill="x", pady=2)
        self._gap(opt)
        self._phone(flat_button(opt, "Reset camera to automatic", self.reset_camera)).pack(anchor="w")
        self._gap(p, 10)

        # info key -> how to show it
        self.syncers = {
            "camera": lambda v: self.side_var.set(v),
            "lens": lambda v: self.lens_var.set(v),
            "zoom": lambda v: self.zoom.set_quietly(round(float(v), 2)),
            "exposureManual": lambda v: self.exp_mode.set(bool(v)),
            "exposure": lambda v: self.bias.set_quietly(round(float(v), 2)),
            "iso": lambda v: self.iso.set_quietly(round(float(v))),
            "shutter": lambda v: self.shutter.set_quietly(self._shutter_to_pos(float(v))),
            "focusManual": lambda v: self.focus_mode.set(bool(v)),
            "lensPosition": lambda v: self.lens_pos.set_quietly(round(float(v), 3)),
            "whiteBalanceManual": lambda v: self.wb_mode.set(bool(v)),
            "temperature": lambda v: self.temp.set_quietly(round(float(v))),
            "tint": lambda v: self.tint.set_quietly(round(float(v))),
            "torch": lambda v: self.torch_var.set(bool(v)),
            "torchLevel": lambda v: self.torch_level.set_quietly(round(float(v), 2)),
            "quality": lambda v: self.quality_var.set(v),
            "fps": lambda v: self.fps_var.set(str(v)),
            "orientation": lambda v: self.orient_var.set(v),
            "stabilization": lambda v: self.stab_var.set(bool(v)),
            "hdr": lambda v: self.hdr_var.set(bool(v)),
            "screenOff": lambda v: self.dark_var.set(bool(v)),
            "codec": lambda v: self.codec_var.set(v),
            "bitrate": lambda v: self.bitrate_var.set(v),
            "lowLight": lambda v: self.lowlight_var.set(bool(v)),
        }
        self._show_modes()

    def _pos_to_shutter(self, pos):
        lo = float(self.info.get("shutterMin", 1 / 8000.0))
        hi = float(self.info.get("shutterMax", 1 / 30.0))
        if hi <= lo:
            return hi
        return math.exp(math.log(lo) + pos * (math.log(hi) - math.log(lo)))

    def _shutter_to_pos(self, seconds):
        lo = float(self.info.get("shutterMin", 1 / 8000.0))
        hi = float(self.info.get("shutterMax", 1 / 30.0))
        if hi <= lo or seconds <= 0:
            return 0.5
        return max(0.0, min(1.0, (math.log(seconds) - math.log(lo)) / (math.log(hi) - math.log(lo))))

    def _mode(self, key, manual):
        self.send(key, **{key: bool(manual)})
        self._show_modes()

    def _show_modes(self):
        for var, manual, auto in ((self.exp_mode, self.exp_manual, self.exp_auto),
                                  (self.focus_mode, self.focus_manual, self.focus_auto),
                                  (self.wb_mode, self.wb_manual, None)):
            if var.get():
                if auto:
                    auto.pack_forget()
                manual.pack(fill="x")
            else:
                manual.pack_forget()
                if auto:
                    auto.pack(fill="x")

    def reset_camera(self):
        self.send("resetPro", resetPro=True)
        self.exp_mode.set(False)
        self.focus_mode.set(False)
        self.wb_mode.set(False)
        self.bias.set_quietly(0.0)
        self._show_modes()

    # ---- right sidebar: the picture on this PC -------------------------------

    def _build_right(self, p):
        self._section_title(p, "Picture")

        flt = self._card(p, "Filters")
        self.filter_var = tk.StringVar(value=self.settings["filter"])
        names = list(processing.FILTERS)
        grid = tk.Frame(flt, bg=C["card"])
        grid.pack(fill="x")
        for i in range(0, len(names), 4):
            Segmented(grid, [(n, n) for n in names[i:i + 4]], self.filter_var,
                      self.apply_filter).pack(fill="x", pady=(0, 4))

        adj = self._card(p, "Adjust")
        a = self.settings["adjust"]
        self.adjust_sliders = {}
        for key, label, lo, hi in (("brightness", "Brightness", -100, 100), ("contrast", "Contrast", -100, 100),
                                   ("saturation", "Saturation", -100, 100), ("vibrance", "Vibrance", -100, 100),
                                   ("warmth", "Warmth", -100, 100), ("tint", "Tint", -100, 100),
                                   ("hue", "Hue", -180, 180), ("gamma", "Gamma", -100, 100),
                                   ("sharpness", "Sharpness", 0, 100),
                                   ("backlight", "Backlight compensation", 0, 100)):
            fmt = (lambda v: "%+d°" % round(v)) if key == "hue" else (
                (lambda v: "%d" % round(v)) if lo == 0 else (lambda v: "%+d" % round(v)))
            s = Slider(adj, label, lo, hi, a.get(key, 0), fmt, lambda v, k=key: self.set_adjust(k, v),
                       colors=GRADIENTS.get(key), default=0)
            s.pack(fill="x", pady=(0, 6))
            self.adjust_sliders[key] = s
        tk.Label(adj, text="Tip: double-click a slider to reset it.", font=(FONT, 8), bg=C["card"],
                 fg=C["faint"], anchor="w").pack(fill="x")
        flat_button(adj, "Reset", self.reset_adjust).pack(anchor="w", pady=(2, 0))

        fr = self._card(p, "Framing", "drag preview to move")
        f = self.settings["framing"]
        self.dzoom = Slider(fr, "Digital zoom", 1.0, 4.0, f["zoom"], lambda v: "%.1fx" % v,
                            lambda v: self.set_framing("zoom", round(v, 2)))
        self.dzoom.pack(fill="x", pady=(0, 6))
        self.pan_x = Slider(fr, "Move left / right", -1.0, 1.0, f["pan_x"], lambda v: "%+d" % round(v * 100),
                            lambda v: self.set_framing("pan_x", v))
        self.pan_x.pack(fill="x", pady=(0, 6))
        self.pan_y = Slider(fr, "Move up / down", -1.0, 1.0, f["pan_y"], lambda v: "%+d" % round(v * 100),
                            lambda v: self.set_framing("pan_y", v))
        self.pan_y.pack(fill="x", pady=(0, 8))
        rot = tk.Frame(fr, bg=C["card"])
        rot.pack(fill="x", pady=(0, 6))
        tk.Label(rot, text="Rotate", font=(FONT, 9), bg=C["card"], fg=C["dim"]).pack(side="left")
        flat_button(rot, "⟳ 90°", lambda: self.rotate(90)).pack(side="right")
        flat_button(rot, "⟲ 90°", lambda: self.rotate(-90)).pack(side="right", padx=4)
        self.rot_label = tk.Label(rot, text="%d°" % f["rotate"], font=(FONT, 9, "bold"), bg=C["card"], fg=C["fg"])
        self.rot_label.pack(side="right", padx=8)
        self.mirror_var = tk.BooleanVar(value=f["mirror"])
        Switch(fr, "Mirror (flip left ↔ right)", self.mirror_var,
               lambda v: self.set_framing("mirror", v)).pack(fill="x", pady=2)
        self.flip_var = tk.BooleanVar(value=f["flip"])
        Switch(fr, "Flip upside down", self.flip_var,
               lambda v: self.set_framing("flip", v)).pack(fill="x", pady=2)
        self._gap(fr)
        flat_button(fr, "Reset framing", self.reset_framing).pack(anchor="w")

        out = self._card(p, "Webcam output")
        tk.Label(out, text="Size that apps receive", font=(FONT, 9), bg=C["card"], fg=C["dim"],
                 anchor="w").pack(fill="x")
        self.output_var = tk.StringVar(value=self.settings["output"])
        Segmented(out, [("720p", "720p"), ("1080p", "1080p"), ("1440p", "1440p"), ("4K", "4K")],
                  self.output_var, self.set_output).pack(fill="x", pady=(2, 0))
        tk.Label(out, text="Zoom, Teams and Meet send at most 1080p. Use 4K for OBS and recordings.",
                 font=(FONT, 8), bg=C["card"], fg=C["faint"], justify="left", anchor="w",
                 wraplength=SIDE_W - 50).pack(fill="x", pady=(4, 0))

        self._build_sound(p)
        self._build_performance(p)

        app = self._card(p, "App")
        self.autostart_var = tk.BooleanVar(value=self._core("autostart_enabled", False))
        Switch(app, "Start with Windows", self.autostart_var, self.set_autostart).pack(fill="x", pady=2)
        self.tray_var = tk.BooleanVar(value=self.settings["close_to_tray"])
        Switch(app, "Keep running when window is closed", self.tray_var,
               self.set_close_to_tray).pack(fill="x", pady=2)
        self._gap(app)
        row = tk.Frame(app, bg=C["card"])
        row.pack(fill="x")
        flat_button(row, "Recordings folder", self.open_recordings).pack(side="left")
        flat_button(row, "Repair camera", self.repair).pack(side="left", padx=6)
        flat_button(app, "License & credits", self.show_licenses).pack(anchor="w", pady=(6, 0))
        tk.Label(app, text="CamBridge %s · free to use · © 2026 ethiogamer" % self._core("VERSION", ""), font=(FONT, 8), bg=C["card"],
                 fg=C["faint"], anchor="w").pack(fill="x", pady=(8, 0))
        self._gap(p, 10)

    # ---- sound ----------------------------------------------------------------

    def _audio_sources(self):
        import audio as audiomod
        choices = [("none", "No sound"), ("phone", "Phone microphone")]
        choices += [("pc:" + n, "PC: " + n) for n in audiomod.list_devices("input")]
        return choices

    def _build_sound(self, p):
        import audio as audiomod
        self._section_title(p, "Sound")
        mic = self._card(p, "Microphone")
        self.source_choices = self._audio_sources()
        current = self.audio.source if self.audio else "none"
        labels = [label for _, label in self.source_choices]
        self.source_var = tk.StringVar(value=dict(self.source_choices).get(current, "No sound"))
        row = tk.Frame(mic, bg=C["card"])
        row.pack(fill="x")
        self.source_box = ttk.Combobox(row, textvariable=self.source_var, values=labels, state="readonly",
                                       style="CamBridge.TCombobox")
        self.source_box.pack(side="left", fill="x", expand=True)
        self.source_box.bind("<<ComboboxSelected>>", lambda e: self.set_audio_source())
        flat_button(row, "↻", self.refresh_audio_devices, padx=8).pack(side="left", padx=(6, 0))

        self.meter = tk.Canvas(mic, height=10, bg=C["card"], highlightthickness=0)
        self.meter.pack(fill="x", pady=(8, 6))
        self.audio_note = tk.Label(mic, text="", font=(FONT, 8), bg=C["card"], fg=C["faint"], anchor="w",
                                   justify="left", wraplength=SIDE_W - 50)
        self.audio_note.pack(fill="x")
        fx = self.settings["audio_fx"]
        Slider(mic, "Volume", -20, 20, fx.get("gain_db", 0.0), lambda v: "%+d dB" % round(v),
               lambda v: self.audio and self.audio.set_effect("gain_db", round(v, 1)), default=0).pack(fill="x", pady=(4, 6))
        self.mute_var = tk.BooleanVar(value=False)
        Switch(mic, "Mute", self.mute_var, lambda v: setattr(self.audio, "muted", bool(v))).pack(fill="x", pady=2)
        for key, text in (("limiter", "Limiter (no distortion on loud sounds)"),
                          ("gate", "Noise gate (silence between words)"),
                          ("clarity", "Voice clarity (removes low rumble)")):
            var = tk.BooleanVar(value=bool(fx.get(key, key == "limiter")))
            Switch(mic, text, var, lambda v, k=key: self.audio and self.audio.set_effect(k, bool(v))).pack(fill="x", pady=2)
            setattr(self, "fx_%s_var" % key, var)
        tk.Label(mic, text="Sound is saved in recordings. For calls, your PC mic can be picked directly in "
                           "Zoom / Telegram.", font=(FONT, 8), bg=C["card"], fg=C["faint"], justify="left",
                 anchor="w", wraplength=SIDE_W - 50).pack(fill="x", pady=(6, 0))

        call = self._card(p, "Use this sound in calls")
        outputs = ["Off"] + audiomod.list_devices("output")
        self.send_var = tk.StringVar(value=(self.audio.send_to if self.audio and self.audio.send_to else "Off"))
        self.send_box = ttk.Combobox(call, textvariable=self.send_var, values=outputs, state="readonly",
                                     style="CamBridge.TCombobox")
        self.send_box.pack(fill="x")
        self.send_box.bind("<<ComboboxSelected>>", lambda e: self.audio and self.audio.set_send_to(
            "" if self.send_var.get() == "Off" else self.send_var.get()))
        tk.Label(call, text="To use the phone mic in Zoom / Telegram: install the free VB-CABLE "
                            "(vb-audio.com), choose “CABLE Input” here, then pick “CABLE Output” as the "
                            "microphone in the call app.", font=(FONT, 8), bg=C["card"], fg=C["faint"],
                 justify="left", anchor="w", wraplength=SIDE_W - 50).pack(fill="x", pady=(6, 0))

    def set_audio_source(self):
        label = self.source_var.get()
        for value, text in self.source_choices:
            if text == label and self.audio:
                self.audio.set_source(value)
                self.show_toast("Sound: " + text)

    def refresh_audio_devices(self):
        import audio as audiomod
        self.source_choices = self._audio_sources()
        self.source_box.configure(values=[label for _, label in self.source_choices])
        self.send_box.configure(values=["Off"] + audiomod.list_devices("output"))
        self.show_toast("Sound devices refreshed")

    def _update_sound(self):
        if not self.audio:
            return
        hub = self.audio
        self.meter.delete("all")
        w = max(20, self.meter.winfo_width())
        level = hub.level if hub.active() else 0.0
        hub.level *= 0.8
        self.meter.create_rectangle(0, 2, w, 8, fill=C["line"], outline="")
        if level > 0.001:
            color = C["green"] if level < 0.7 else (C["orange"] if level < 0.92 else C["red"])
            self.meter.create_rectangle(0, 2, int(w * min(1.0, level)), 8, fill=color, outline="")
        if hub.error:
            note = hub.error
        elif hub.source == "none":
            note = "Recordings will have no sound."
        elif hub.active():
            note = "%s · %d Hz %s" % ("Phone mic" if hub.source == "phone" else "PC mic", hub.rate,
                                      "stereo" if hub.channels == 2 else "mono")
        elif hub.source == "phone":
            note = "Waiting for sound from the phone…"
        else:
            note = "Waiting for the microphone…"
        self.audio_note.configure(text=note)

    # ---- performance ------------------------------------------------------------

    def _build_performance(self, p):
        card = self._card(p, "Performance")
        self.perf_info = tk.Label(card, text="", font=(FONT, 9), bg=C["card"], fg=C["fg"], anchor="w",
                                  justify="left", wraplength=SIDE_W - 50)
        self.perf_info.pack(fill="x")
        self.perf_stats = tk.Label(card, text="", font=("Consolas", 8), bg=C["card"], fg=C["dim"], anchor="w",
                                   justify="left")
        self.perf_stats.pack(fill="x", pady=(6, 8))
        self.highperf_var = tk.BooleanVar(value=self.settings["high_performance"])
        Switch(card, "High performance mode", self.highperf_var, self.set_high_performance).pack(fill="x", pady=2)
        tk.Label(card, text="Gives CamBridge priority over other programs. Use it for 4K 60 or when "
                            "games / streaming software run at the same time.", font=(FONT, 8), bg=C["card"],
                 fg=C["faint"], justify="left", anchor="w", wraplength=SIDE_W - 50).pack(fill="x")
        self.adaptive_var = tk.BooleanVar(value=self.settings["adaptive"])
        self._phone(Switch(card, "Keep it smooth (lower detail when the link is busy)", self.adaptive_var,
                           self.set_adaptive)).pack(fill="x", pady=(6, 2))
        apply_priority(self.settings["high_performance"])

    def set_high_performance(self, on):
        self.settings["high_performance"] = bool(on)
        self.settings.save()
        apply_priority(on)
        self.show_toast("High performance mode " + ("on" if on else "off"))

    def set_adaptive(self, on):
        self.settings["adaptive"] = bool(on)
        self.settings.save()
        self.link.on_connect = dict(getattr(self.link, "on_connect", {}), adaptive=bool(on))
        self.send("adaptive", adaptive=bool(on))

    def _update_performance(self):
        pump = self.pump
        st = pump.stats
        cores = os.cpu_count() or 1
        self.perf_info.configure(text="Decoding: %s · %s\nProcessing: %d CPU threads%s" % (
            getattr(pump, "decoder_name", "—"), getattr(pump, "codec", "—"), cores,
            " · high priority" if self.settings["high_performance"] else ""))
        w, h = pump.size
        lines = [
            "Incoming   %6.1f Mbit/s" % st.mbps,
            "Output     %dx%d @ %d fps" % (w, h, pump.fps),
            "Decode     %6.1f ms" % st.decode_ms,
            "Effects    %6.1f ms" % st.process_ms,
            "Dropped    %6d frames" % st.dropped,
        ]
        if self.pump.recorder is not None and getattr(self.pump.recorder, "dropped", 0):
            lines.append("Rec drops  %6d frames" % self.pump.recorder.dropped)
        self.perf_stats.configure(text="\n".join(lines))

    def _core(self, name, default):
        value = getattr(self.core, name, default)
        return value() if callable(value) else value

    # ---- sidebars --------------------------------------------------------------

    def _layout_sidebars(self):
        for w in (self.left, self.center, self.right):
            w.pack_forget()
        if self.settings["left_open"]:
            self.left.pack(side="left", fill="y")
        if self.settings["right_open"]:
            self.right.pack(side="right", fill="y")
        self.center.pack(side="left", fill="both", expand=True)
        self.preview_key = None

    def toggle_side(self, key):
        self.settings[key] = not self.settings[key]
        self.settings.save()
        self._layout_sidebars()

    # ---- sending to the iPhone -------------------------------------------------

    def send(self, key, throttle=False, **values):
        self.touched[key] = time.monotonic()
        if not throttle:
            self.link.send_command(**values)
            return
        first = key not in self.pending
        self.pending[key] = values
        if first:
            self.root.after(60, lambda: self._flush(key))

    def _flush(self, key):
        values = self.pending.pop(key, None)
        if values:
            self.link.send_command(**values)

    def _recent(self, key):
        return time.monotonic() - self.touched.get(key, 0) < 1.5

    def apply_profile(self, name):
        if name not in PROFILES:
            return
        output, quality, fps = PROFILES[name]
        self.output_var.set(output)
        self.set_output(output)
        self.quality_var.set(quality)
        self.fps_var.set(str(fps))
        self.send("quality", quality=quality)
        self.send("fps", fps=fps)
        self.show_toast("Profile: " + name)

    # ---- PC picture settings ---------------------------------------------------

    def set_adjust(self, key, value):
        self.settings["adjust"][key] = round(value)
        self.settings["filter"] = "Custom"
        self.filter_var.set("Custom")
        self.pump.set_adjust(self.settings["adjust"])
        self._save_later()

    def apply_filter(self, name):
        values = dict(processing.DEFAULT_ADJUST)
        values.update(processing.FILTERS.get(name, {}))
        self.settings["adjust"] = values
        self.settings["filter"] = name
        for key, s in self.adjust_sliders.items():
            s.set_quietly(values[key])
        self.pump.set_adjust(values)
        self._save_later()

    def reset_adjust(self):
        self.filter_var.set("None")
        self.apply_filter("None")

    def set_framing(self, key, value):
        f = self.settings["framing"]
        f[key] = value
        if key == "zoom" and value <= 1.0:
            f["pan_x"] = f["pan_y"] = 0.0
        self._framing_changed(from_slider=key)

    def _framing_changed(self, from_slider=None):
        f = self.settings["framing"]
        for key, s in (("zoom", self.dzoom), ("pan_x", self.pan_x), ("pan_y", self.pan_y)):
            if key != from_slider:
                s.set_quietly(f[key])
        self.rot_label.configure(text="%d°" % f["rotate"])
        self.pump.set_framing(f)
        self._save_later()

    def rotate(self, degrees):
        f = self.settings["framing"]
        f["rotate"] = (f["rotate"] + degrees) % 360
        self._framing_changed()

    def reset_framing(self):
        self.settings["framing"] = dict(processing.DEFAULT_FRAMING)
        self.mirror_var.set(False)
        self.flip_var.set(False)
        self._framing_changed()

    def set_output(self, value):
        sizes = self._core("OUTPUT_SIZES", {"1080p": (1920, 1080), "720p": (1280, 720)})
        if value in sizes:
            self.settings["output"] = value
            self.pump.resize(sizes[value])
            self._save_later()

    def _save_later(self):
        if not getattr(self, "_save_pending", False):
            self._save_pending = True
            self.root.after(800, self._save_now)

    def _save_now(self):
        self._save_pending = False
        self.settings.save()

    # ---- app settings ----------------------------------------------------------

    def set_autostart(self, enabled):
        fn = getattr(self.core, "set_autostart", None)
        if not fn or not fn(enabled):
            self.autostart_var.set(self._core("autostart_enabled", False))
            self.show_toast("“Start with Windows” works in the installed CamBridge.exe")

    def set_close_to_tray(self, enabled):
        self.settings["close_to_tray"] = bool(enabled)
        self.settings.save()

    def open_recordings(self):
        path = recorder.recordings_dir()
        try:
            if sys.platform == "win32":
                os.startfile(path)
            else:
                self.show_toast(path)
        except OSError:
            self.show_toast(path)

    def show_licenses(self):
        texts = []
        for name in ["LICENSE", "THIRD_PARTY_NOTICES.md"]:
            path = self._resource(name)
            if not os.path.exists(path):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", name)
            try:
                with open(path, encoding="utf-8") as f:
                    texts.append(f.read())
            except OSError:
                pass
        folder = self._resource("licenses")
        if not os.path.isdir(folder):
            folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "licenses")
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                try:
                    with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as f:
                        texts.append("licenses/%s\n\n%s" % (name, f.read()))
                except OSError:
                    pass
        win = tk.Toplevel(self.root, bg=C["bg"])
        win.title("CamBridge — license and open-source credits")
        win.geometry("760x620")
        text = tk.Text(win, wrap="word", bg=C["card"], fg=C["fg"], font=("Consolas", 9), bd=0,
                       padx=14, pady=12, insertbackground=C["fg"])
        bar = ttk.Scrollbar(win, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", ("\n\n" + "=" * 70 + "\n\n").join(texts) or "License files not found.")
        text.configure(state="disabled")

    def repair(self):
        fn = getattr(self.core, "install_camera", None)
        if fn and fn(force=True):
            messagebox.showinfo("CamBridge", "Camera repaired. Restart Zoom / Telegram / OBS so they see it.")
        else:
            messagebox.showwarning("CamBridge", "The camera could not be installed. Click Repair again "
                                                "and choose “Yes” when Windows asks.")

    # ---- pause / record ----------------------------------------------------------

    def toggle_pause(self):
        paused = not self.pump.paused
        self.pump.set_paused(paused)
        self.preview_key = None
        self.pause_btn.configure(text="▶  Resume" if paused else "❚❚  Pause",
                                 bg=C["accent"] if paused else C["button"],
                                 fg=C["accent_fg"] if paused else C["fg"])
        self.show_toast("Camera paused — others see a pause screen" if paused else "Camera resumed")

    def toggle_record(self):
        if self.pump.recorder is None:
            self.pump.start_recording()
            self.show_toast("Recording started")
        else:
            rec = self.pump.recorder
            self.pump.stop_recording()
            folder = os.path.dirname(rec.path) if rec else recorder.recordings_dir()
            self.show_toast("Recording saved (MP4) — click to open folder", 12, self.open_recordings)
            self._last_record_folder = folder

    # ---- window / tray -----------------------------------------------------------

    def on_close(self):
        if self.tray and self.settings["close_to_tray"]:
            self.root.withdraw()
            if not self.settings["tray_tip_shown"]:
                self.settings["tray_tip_shown"] = True
                self.settings.save()
                try:
                    self.tray.notify("CamBridge is still running. Right-click its icon next to the clock "
                                     "to open or quit.", "CamBridge")
                except Exception:
                    pass
        else:
            self.quit()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        self.settings.save()
        self.link.stop()
        self.pump.stop()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.after(500, self.root.destroy)

    # ---- Wi-Fi ---------------------------------------------------------------------

    def open_wifi(self):
        import wifi as wifimod
        if self.wifi_window is not None and self.wifi_window.winfo_exists():
            self.wifi_window.lift()
            return
        win = tk.Toplevel(self.root, bg=C["bg"], padx=22, pady=18)
        win.title("Connect a phone over Wi-Fi")
        win.resizable(False, False)
        self.wifi_window = win
        try:
            win.iconbitmap(self._resource("icon.ico"))
        except Exception:
            pass

        tk.Label(win, text="Connect a phone over Wi-Fi", font=(FONT, 15, "bold"), bg=C["bg"],
                 fg=C["fg"]).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(win, text="The phone and this PC must be on the same Wi-Fi network.", font=(FONT, 9),
                 bg=C["bg"], fg=C["dim"]).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))

        qr_box = tk.Label(win, bg="#ffffff", bd=0)
        qr_box.grid(row=2, column=0, sticky="n")

        steps = tk.Frame(win, bg=C["bg"])
        steps.grid(row=2, column=1, sticky="nw", padx=(20, 0))
        for i, (title, text) in enumerate((
            ("Scan the code", "Open CamBridge on the phone, tap Wi-Fi → Scan QR code.\n"
                              "On iPhone you can also scan it with the normal Camera app."),
            ("…or just pick this PC", "In CamBridge on the phone, tap Wi-Fi and choose “%s”.\n"
                                      "The first time, click Allow here on the PC." % wifimod.pc_name()),
            ("Next time", "Remembered phones connect by themselves — just open the app."),
        )):
            tk.Label(steps, text="%d.  %s" % (i + 1, title), font=(FONT, 10, "bold"), bg=C["bg"],
                     fg=C["fg"], anchor="w").pack(fill="x", pady=(0 if i == 0 else 10, 2))
            tk.Label(steps, text=text, font=(FONT, 9), bg=C["bg"], fg=C["dim"], justify="left",
                     anchor="w").pack(fill="x")
        addr = tk.Label(steps, text="", font=(FONT, 8), bg=C["bg"], fg=C["faint"], justify="left", anchor="w")
        addr.pack(fill="x", pady=(14, 0))
        if self.wifi is not None and self.wifi.error:
            tk.Label(steps, text=self.wifi.error, font=(FONT, 9, "bold"), bg=C["bg"], fg=C["red"],
                     anchor="w").pack(fill="x", pady=(6, 0))

        trusted_card = tk.Frame(win, bg=C["card"], padx=12, pady=10)
        trusted_card.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        tk.Label(trusted_card, text="REMEMBERED PHONES", font=(FONT, 8, "bold"), bg=C["card"],
                 fg=C["dim"], anchor="w").pack(fill="x")
        trusted_list = tk.Frame(trusted_card, bg=C["card"])
        trusted_list.pack(fill="x", pady=(6, 0))

        def refresh():
            url = wifimod.pairing_url(self.settings)
            self.qr_photo = ImageTk.PhotoImage(wifimod.qr_image(url, 260))
            qr_box.configure(image=self.qr_photo)
            ips = wifimod.local_ipv4s()
            addr.configure(text="This PC: %s  ·  port %d" % (", ".join(ips) or "no network found", wifimod.WIFI_PORT))
            for w in trusted_list.winfo_children():
                w.destroy()
            trusted = self.settings["trusted"]
            if not trusted:
                tk.Label(trusted_list, text="None yet.", font=(FONT, 9), bg=C["card"], fg=C["faint"],
                         anchor="w").pack(fill="x")
            for device_id, name in list(trusted.items()):
                row = tk.Frame(trusted_list, bg=C["card"])
                row.pack(fill="x", pady=1)
                tk.Label(row, text=name, font=(FONT, 9), bg=C["card"], fg=C["fg"], anchor="w").pack(side="left")
                flat_button(row, "Forget", lambda d=device_id: forget(d), pady=2).pack(side="right")

        def forget(device_id):
            self.settings["trusted"].pop(device_id, None)
            self.settings.save()
            refresh()

        def new_code():
            self.settings["pair_key"] = ""
            self.settings["trusted"] = {}
            wifimod.ensure_identity(self.settings)
            refresh()
            self.show_toast("New QR code made — phones must pair again")

        bottom = tk.Frame(win, bg=C["bg"])
        bottom.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        flat_button(bottom, "Make a new code (forget all phones)", new_code).pack(side="left")
        flat_button(bottom, "Close", win.destroy, bg=C["accent"], fg=C["accent_fg"], bold=True).pack(side="right")
        refresh()

    def ask_approval(self, request):
        self.show()
        win = tk.Toplevel(self.root, bg=C["bg"], padx=24, pady=20)
        win.title("New phone wants to connect")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        tk.Label(win, text="Allow “%s” to connect?" % request.device, font=(FONT, 13, "bold"),
                 bg=C["bg"], fg=C["fg"]).pack(anchor="w")
        tk.Label(win, text="It wants to send its camera to this PC over Wi-Fi.\n"
                           "Only allow phones you own. Allowed phones are remembered.",
                 font=(FONT, 9), bg=C["bg"], fg=C["dim"], justify="left").pack(anchor="w", pady=(6, 14))
        row = tk.Frame(win, bg=C["bg"])
        row.pack(fill="x")

        def decide(allowed):
            request.answer(allowed)
            if win.winfo_exists():
                win.destroy()
            self.show_toast(("Allowed " if allowed else "Blocked ") + request.device)

        flat_button(row, "Allow", lambda: decide(True), bg=C["accent"], fg=C["accent_fg"], bold=True,
                    padx=18, pady=6).pack(side="right")
        flat_button(row, "Deny", lambda: decide(False), padx=18, pady=6).pack(side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", lambda: decide(False))
        win.after(58000, lambda: win.winfo_exists() and decide(False))
        if self.tray:
            try:
                self.tray.notify("“%s” wants to connect over Wi-Fi." % request.device, "CamBridge")
            except Exception:
                pass

    # ---- refresh loop ------------------------------------------------------------

    def _tick(self):
        if self.quitting:
            return
        while True:
            try:
                action = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            {"show": self.show, "pause": self.toggle_pause, "record": self.toggle_record,
             "quit": self.quit}.get(action, lambda: None)()
        if self.wifi is not None:
            try:
                request = self.wifi.requests.get_nowait()
            except queue.Empty:
                request = None
            if request is not None:
                self.ask_approval(request)

        state, status, detail, info = self.link.snapshot()
        if self.pump.error:
            state, status, detail = "error", "Camera problem", self.pump.error
        self.info = info or {}
        self.connected = state == "connected" and bool(info)

        colors = {"connected": C["green"], "waiting": C["orange"], "error": C["red"]}
        self.status_dot.itemconfigure(self.status_dot_id, fill=colors.get(state, C["orange"]))
        self.status_text.configure(text=status)

        if self.connected:
            self.device_name.configure(text=str(info.get("device", "iPhone")))
            parts = []
            if info.get("width"):
                parts.append("%s×%s" % (info["width"], info["height"]))
            parts.append("%d fps" % self.pump.fps)
            self.device_sub.configure(text="  ·  ".join(parts))
            self._sync(info)
        else:
            self.device_name.configure(text="No phone connected")
            self.device_sub.configure(text=detail or status)
        for w in self.phone_controls:
            self._enable(w, self.connected)
        self._enable_supported(info)

        warnings = []
        if info.get("hot"):
            warnings.append("Phone is hot — take it out of its case or choose 30 fps.")
        if info.get("fps") and info.get("actualFps") and info["actualFps"] < info["fps"]:
            warnings.append("This lens can't do %s fps; using %s." % (info["fps"], info["actualFps"]))
        if self.pump.record_error:
            warnings.append("Recording problem: %s" % self.pump.record_error)
        if not self.connected and detail and state != "connected":
            warnings.append(detail)
        self.warning.configure(text="   ".join(warnings))

        rec = self.pump.recorder
        if rec is not None:
            t = int(rec.elapsed())
            label = "■  Stop  %d:%02d" % (t // 60, t % 60)
            self.record_btn.configure(text=label, bg=C["red"], fg="#ffffff")
            self.preview.itemconfigure(self.rec_badge, text="● REC %d:%02d" % (t // 60, t % 60))
        else:
            self.record_btn.configure(text="●  Record", bg=C["button"], fg=C["red"])
            self.preview.itemconfigure(self.rec_badge, text="")

        in_use = self.pump.in_use
        self.use_label.configure(text="● In use by an app" if in_use else "○ Not in use by any app",
                                 fg=C["green"] if in_use else C["dim"])
        w, h = self.pump.size
        self.out_label.configure(text="Output %d×%d · %d fps" % (w, h, self.pump.fps))
        if self.toast_until and time.monotonic() > self.toast_until:
            self.toast.configure(text="")
            self.toast_action = None
            self.toast_until = 0

        self._update_sound()
        if time.monotonic() - self._perf_tick > 0.5:
            self._perf_tick = time.monotonic()
            self._update_performance()
        self._render_preview()
        self.root.after(50, self._tick)

    def _enable(self, w, enabled):
        if isinstance(w, (Segmented, Switch, Slider)):
            w.set_enabled(enabled)
        elif isinstance(w, ttk.Combobox):
            w.configure(state="readonly" if enabled else "disabled")
        elif isinstance(w, tk.Label):
            w.configure(fg=C["fg"] if enabled else C["faint"])

    def _enable_supported(self, info):
        if not self.connected:
            return
        for widget, key in ((self.exp_seg, "manualExposureSupported"), (self.focus_seg, "manualFocusSupported"),
                            (self.wb_seg, "manualWhiteBalanceSupported"), (self.torch_sw, "torchAvailable"),
                            (self.torch_level, "torchAvailable"), (self.stab_sw, "stabilizationSupported"),
                            (self.hdr_sw, "hdrSupported"), (self.torch_level, "torchLevelSupported"),
                            (self.tint, "tintSupported")):
            if key in info and not info[key]:
                widget.set_enabled(False)
        if info.get("exposureManual"):
            self.bias.set_enabled(False)

    def _sync(self, info):
        lenses = info.get("lenses") or ["1x"]
        if lenses != self.lens_names:
            self.lens_names = lenses
            self.lens_seg.set_options([(n, n) for n in lenses])
        low = float(info.get("minZoom") or 1.0)
        self.zoom.set_range(low, max(low + 0.1, float(info.get("maxZoom") or 1.0)))
        if info.get("isoMin") and info.get("isoMax"):
            self.iso.set_range(float(info["isoMin"]), float(info["isoMax"]))
        modes_before = (self.exp_mode.get(), self.focus_mode.get(), self.wb_mode.get())
        for key, show in self.syncers.items():
            if key in info and not self._recent(key):
                try:
                    show(info[key])
                except (tk.TclError, ValueError, TypeError):
                    pass
        if modes_before != (self.exp_mode.get(), self.focus_mode.get(), self.wb_mode.get()):
            self._show_modes()

        if info.get("liveISO"):
            self.exposure_live.configure(text="now ISO %d · %s" % (info["liveISO"], shutter_text(info.get("liveShutter"))))
        if info.get("liveLens", -1) >= 0:
            self.focus_live.configure(text="now %d%%" % round(info["liveLens"] * 100))
        if info.get("liveTemperature"):
            self.wb_live.configure(text="now %dK" % info["liveTemperature"])
