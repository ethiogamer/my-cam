"""Android phones over USB, using Google's adb tool (bundled with Akrem Cam).

The phone needs "USB debugging" turned on once (Settings → Developer options).
We then ask adb to forward a PC port to the Akrem Cam app's port inside the phone.
"""

import os
import shutil
import subprocess
import sys

import virtualcam

ADB_FILES = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_adb_path = None


class AdbError(Exception):
    pass


def adb_path():
    """Finds adb: our own copy in AppData (copied from the program), else one on PATH."""
    global _adb_path
    if _adb_path and os.path.exists(_adb_path):
        return _adb_path
    if sys.platform == "win32":
        src = os.path.join(virtualcam.bundled_dir(), "platform-tools")
        dst = os.path.join(virtualcam.app_data_dir(), "platform-tools")
        if os.path.exists(os.path.join(src, "adb.exe")):
            os.makedirs(dst, exist_ok=True)
            for name in ADB_FILES:
                s, d = os.path.join(src, name), os.path.join(dst, name)
                if os.path.exists(s) and (not os.path.exists(d) or os.path.getsize(s) != os.path.getsize(d)):
                    try:
                        shutil.copyfile(s, d)
                    except OSError:
                        pass  # in use by a running adb server — the existing copy is fine
            if os.path.exists(os.path.join(dst, "adb.exe")):
                _adb_path = os.path.join(dst, "adb.exe")
                return _adb_path
    found = shutil.which("adb")
    if found:
        _adb_path = found
    return _adb_path


def _run(args, timeout=8):
    exe = adb_path()
    if not exe:
        raise AdbError("adb not found")
    try:
        result = subprocess.run([exe] + args, capture_output=True, text=True, timeout=timeout,
                                creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AdbError(str(e)) from e
    if result.returncode != 0:
        raise AdbError((result.stderr or result.stdout).strip() or "adb failed")
    return result.stdout


def devices():
    """List of {"serial", "state", "model"}; state is 'device', 'unauthorized', 'offline'…"""
    out = _run(["devices", "-l"], timeout=12)
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or line.startswith("*"):
            continue
        info = {"serial": parts[0], "state": parts[1], "model": ""}
        for p in parts[2:]:
            if p.startswith("model:"):
                info["model"] = p[6:].replace("_", " ")
        # Skip phones connected over Wi-Fi (serial looks like 192.168.x.x:5555).
        if ":" in info["serial"] and info["serial"].count(".") == 3:
            continue
        found.append(info)
    return found


def forward(serial, phone_port):
    """Returns a PC port that leads to `phone_port` inside the phone."""
    out = _run(["-s", serial, "forward", "tcp:0", "tcp:%d" % phone_port]).strip()
    try:
        return int(out.splitlines()[-1])
    except (ValueError, IndexError):
        raise AdbError("adb forward gave: %r" % out)


def remove_forward(serial, pc_port):
    try:
        _run(["-s", serial, "forward", "--remove", "tcp:%d" % pc_port], timeout=4)
    except AdbError:
        pass


def app_running(serial):
    try:
        out = _run(["-s", serial, "shell", "pidof", "com.akrem.cam"], timeout=5)
        return bool(out.strip())
    except AdbError:
        return False


def launch_app(serial):
    """Opens the Akrem Cam app on the phone (works when the phone is unlocked)."""
    try:
        _run(["-s", serial, "shell", "monkey", "-p", "com.akrem.cam", "-c",
              "android.intent.category.LAUNCHER", "1"], timeout=8)
        return True
    except AdbError:
        return False


def installed(serial):
    try:
        out = _run(["-s", serial, "shell", "pm", "list", "packages", "com.akrem.cam"], timeout=8)
        return "package:com.akrem.cam" in out
    except AdbError:
        return True  # can't tell — assume yes rather than reinstalling


def install(serial, apk_path):
    _run(["-s", serial, "install", "-r", apk_path], timeout=180)


def bundled_apk():
    path = os.path.join(virtualcam.bundled_dir(), "AkremCam.apk")
    return path if os.path.exists(path) else None
