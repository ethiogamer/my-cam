"""Talks to Apple's USB service (usbmuxd / Apple Mobile Device Service).

On Windows this service comes with iTunes (or the Apple Devices app) and listens on
127.0.0.1:27015. We ask it for the list of plugged-in iPhones, then ask it to open a
TCP tunnel over the USB cable to a port inside the iPhone app.
"""

import plistlib
import socket
import struct
import sys

USBMUXD_HOST = "127.0.0.1"
USBMUXD_PORT = 27015
USBMUXD_UNIX = "/var/run/usbmuxd"  # macOS / Linux

_PLIST_VERSION = 1
_MSG_PLIST = 8


class UsbmuxError(Exception):
    pass


class ServiceNotFound(UsbmuxError):
    """Apple's USB service isn't running (iTunes / Apple Devices not installed)."""


def _open_socket(timeout):
    if sys.platform != "win32":
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(USBMUXD_UNIX)
            return s
        except OSError:
            pass
    try:
        return socket.create_connection((USBMUXD_HOST, USBMUXD_PORT), timeout=timeout)
    except OSError as e:
        raise ServiceNotFound(str(e)) from e


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise UsbmuxError("connection closed by usbmuxd")
        buf += chunk
    return bytes(buf)


def _request(sock, payload, tag):
    base = {"ClientVersionString": "cambridge", "ProgName": "CamBridge"}
    base.update(payload)
    body = plistlib.dumps(base, fmt=plistlib.FMT_XML)
    header = struct.pack("<IIII", 16 + len(body), _PLIST_VERSION, _MSG_PLIST, tag)
    sock.sendall(header + body)
    length, _version, _msg, _tag = struct.unpack("<IIII", _recv_exact(sock, 16))
    if length < 16 or length > 16 * 1024 * 1024:
        raise UsbmuxError("bad reply from usbmuxd")
    return plistlib.loads(_recv_exact(sock, length - 16))


def list_devices(timeout=3.0):
    """Returns a list of dicts: {"id": int, "serial": str, "usb": bool}."""
    sock = _open_socket(timeout)
    try:
        reply = _request(sock, {"MessageType": "ListDevices"}, tag=1)
    finally:
        sock.close()
    devices = []
    for entry in reply.get("DeviceList", []):
        props = entry.get("Properties", {})
        dev_id = props.get("DeviceID", entry.get("DeviceID"))
        if dev_id is None:
            continue
        devices.append({
            "id": int(dev_id),
            "serial": str(props.get("SerialNumber", "")),
            "usb": props.get("ConnectionType", "USB") == "USB",
        })
    # USB first; network-paired devices last.
    devices.sort(key=lambda d: not d["usb"])
    return devices


_CONNECT_ERRORS = {
    1: "device refused the connection (is the CamBridge app open on the iPhone?)",
    2: "device is not connected",
    3: "connection refused (is the CamBridge app open on the iPhone?)",
    6: "bad request",
}


def connect(device_id, port, timeout=3.0):
    """Opens a tunnel to `port` inside the iPhone. Returns a connected socket."""
    sock = _open_socket(timeout)
    try:
        swapped = ((port & 0xFF) << 8) | ((port >> 8) & 0xFF)  # network byte order
        reply = _request(sock, {
            "MessageType": "Connect",
            "DeviceID": int(device_id),
            "PortNumber": swapped,
        }, tag=2)
        number = reply.get("Number", -1)
        if reply.get("MessageType") != "Result" or number != 0:
            raise UsbmuxError(_CONNECT_ERRORS.get(number, "connect failed (code %s)" % number))
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise
