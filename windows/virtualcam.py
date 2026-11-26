"""The "CamBridge" virtual webcam (a renamed build of the open-source Softcam library).

The DLL must be registered once (needs admin) so Zoom, Telegram, OBS, etc. can see the
camera. After that, this program just pushes pictures into it.
"""

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys

CAMERA_NAME = "CamBridge"
CLSID = "{58EA431C-1871-4D1E-B60C-5EA992F26670}"
DLL64 = "cambridge64.dll"
DLL32 = "cambridge32.dll"


def app_data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "CamBridge")
    os.makedirs(path, exist_ok=True)
    return path


def bundled_dir():
    """Folder that holds the DLLs shipped with the program."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")


def installed_dll(name):
    return os.path.join(app_data_dir(), name)


def _same_file(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return hashlib.sha256(fa.read()).digest() == hashlib.sha256(fb.read()).digest()
    except OSError:
        return False


def copy_dlls():
    """Copies the DLLs to a fixed folder (the camera driver must not move around)."""
    for name in (DLL64, DLL32):
        src = os.path.join(bundled_dir(), name)
        dst = installed_dll(name)
        if not os.path.exists(src):
            continue
        if os.path.exists(dst) and _same_file(src, dst):
            continue
        try:
            shutil.copyfile(src, dst)
        except OSError:
            # In use by another program (e.g. Zoom has the camera open) — keep the old copy.
            if not os.path.exists(dst):
                raise


def is_registered():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"CLSID\%s\InprocServer32" % CLSID) as key:
            value, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return False
    return os.path.normcase(os.path.abspath(value)) == os.path.normcase(installed_dll(DLL64))


def _regsvr32(system_dir, dll, unregister=False):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), system_dir, "regsvr32.exe")
    if not os.path.exists(exe) or not os.path.exists(dll):
        return 0
    args = [exe, "/s"] + (["/u"] if unregister else []) + [dll]
    return subprocess.call(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def register_now(unregister=False):
    """Runs inside the elevated (admin) copy of the program."""
    rc = _regsvr32("System32", installed_dll(DLL64), unregister)
    _regsvr32("SysWOW64", installed_dll(DLL32), unregister)  # for older 32-bit apps
    return rc


def request_registration(unregister=False):
    """Starts an admin copy of this program to register the camera. Shows the Windows
    'Do you want to allow…' prompt once. Returns True if it finished OK."""
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    flag = "--unregister" if unregister else "--register"
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, flag
    else:
        exe, params = sys.executable, '"%s" %s' % (os.path.abspath(sys.argv[0]), flag)

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.nShow = 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return False  # user pressed "No"
    ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, 60000)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(info.hProcess)
    return code.value == 0


class VirtualCamera:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        path = installed_dll(DLL64)
        if not os.path.exists(path):
            path = os.path.join(bundled_dir(), DLL64)
        self._dll = ctypes.CDLL(path)
        self._dll.scCreateCamera.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_float]
        self._dll.scCreateCamera.restype = ctypes.c_void_p
        self._dll.scDeleteCamera.argtypes = [ctypes.c_void_p]
        self._dll.scDeleteCamera.restype = None
        self._dll.scSendFrame.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._dll.scSendFrame.restype = None
        self._dll.scIsConnected.argtypes = [ctypes.c_void_p]
        self._dll.scIsConnected.restype = ctypes.c_bool
        # framerate 0 = deliver each picture as soon as it arrives (lowest delay)
        self._cam = self._dll.scCreateCamera(width, height, 0.0)
        if not self._cam:
            raise RuntimeError("CamBridge is already running (only one copy can run).")

    def send(self, bgr_bytes):
        """`bgr_bytes`: width*height*3 bytes (bytes or a ctypes buffer), top row first, BGR order."""
        if self._cam:
            self._dll.scSendFrame(self._cam, bgr_bytes)

    def in_use(self):
        """True when an app (Zoom, Telegram…) is currently using the camera."""
        return bool(self._cam) and bool(self._dll.scIsConnected(self._cam))

    def close(self):
        if self._cam:
            self._dll.scDeleteCamera(self._cam)
            self._cam = None
