"""Phones connect to the PC over Wi-Fi.

The PHONE connects to the PC (the PC never has to find the phone), so the camera is never
exposed on the network. Three ways a phone gets in:
  * it scanned the QR code shown on the PC (the code holds a secret pairing key), or
  * it was approved before (remembered phones), or
  * it found the PC automatically (Bonjour/mDNS) and the user clicks "Allow" on the PC.
"""

import json
import logging
import queue
import secrets
import socket
import struct
import threading
import time
import urllib.parse
import uuid

log = logging.getLogger("cambridge")

WIFI_PORT = 7879
SERVICE_TYPE = "_cambridge._tcp.local."
MAGIC = b"CBG1"
MSG_HELLO = 5      # phone -> PC: {"device", "deviceId", "key", "platform"}
MSG_WELCOME = 6    # PC -> phone: {"status": "waiting" | "ok" | "denied" | "busy", "pc": name}


def pc_name():
    name = socket.gethostname() or "PC"
    return name.split(".")[0]


def local_ipv4s():
    """This PC's network addresses, the most likely Wi-Fi/LAN one first."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # picks the default route; sends nothing
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if isinstance(ip.ip, str) and ip.ip not in found:
                    found.append(ip.ip)
    except Exception:
        pass
    return [ip for ip in found if not ip.startswith(("127.", "169.254.", "0."))]


def ensure_identity(settings):
    """Gives this PC a permanent id and a pairing key (saved in settings)."""
    changed = False
    if not settings["pc_id"]:
        settings["pc_id"] = uuid.uuid4().hex[:12]
        changed = True
    if not settings["pair_key"]:
        settings["pair_key"] = secrets.token_urlsafe(12)
        changed = True
    if changed:
        settings.save()


def pairing_url(settings):
    query = urllib.parse.urlencode({
        "h": ",".join(local_ipv4s()),
        "p": WIFI_PORT,
        "k": settings["pair_key"],
        "id": settings["pc_id"],
        "n": pc_name(),
    })
    return "cambridge://pair?" + query


def qr_image(text, size=300):
    import qrcode
    qr = qrcode.QRCode(border=2, box_size=10, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((size, size))


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return bytes(buf)


def _send_json(sock, msg_type, obj):
    payload = json.dumps(obj).encode("utf-8")
    sock.sendall(MAGIC + struct.pack(">II", msg_type, len(payload)) + payload)


class ApprovalRequest:
    def __init__(self, device, device_id, platform):
        self.device = device
        self.device_id = device_id
        self.platform = platform
        self.allowed = None
        self.done = threading.Event()

    def answer(self, allowed):
        self.allowed = bool(allowed)
        self.done.set()


class WifiServer(threading.Thread):
    def __init__(self, settings, link):
        super().__init__(daemon=True)
        self.settings = settings
        self.link = link
        self.requests = queue.Queue()     # ApprovalRequest objects for the window
        self.running = True
        self.error = None
        self.port = WIFI_PORT
        self._zeroconf = None
        self._service = None
        self._ips = []
        self._sock = None

    # ---- discovery (Bonjour / mDNS) -----------------------------------------

    def _advertise(self):
        ips = local_ipv4s()
        if ips == self._ips and self._service is not None:
            return
        self._ips = ips
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
            if self._zeroconf is None:
                self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            if self._service is not None:
                self._zeroconf.unregister_service(self._service)
                self._service = None
            if not ips:
                return
            name = pc_name()
            info = ServiceInfo(
                SERVICE_TYPE,
                "%s (%s).%s" % (name, self.settings["pc_id"][:4], SERVICE_TYPE),
                addresses=[socket.inet_aton(ip) for ip in ips],
                port=self.port,
                properties={"id": self.settings["pc_id"], "name": name, "v": "1"},
                server="cambridge-%s.local." % self.settings["pc_id"],
            )
            self._zeroconf.register_service(info, allow_name_change=True)
            self._service = info
            log.info("advertising on Wi-Fi: %s port %d", ips, self.port)
        except Exception as e:  # discovery is a convenience; QR still works without it
            log.warning("Wi-Fi discovery unavailable: %s", e)

    # ---- server --------------------------------------------------------------

    def stop(self):
        self.running = False
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        try:
            if self._zeroconf:
                self._zeroconf.unregister_all_services()
                self._zeroconf.close()
        except Exception:
            pass

    def run(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(4)
            srv.settimeout(1.0)
            self._sock = srv
        except OSError as e:
            self.error = "Wi-Fi port %d is busy (%s)" % (self.port, e)
            log.error(self.error)
            return
        self._advertise()
        last_check = time.monotonic()
        while self.running:
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                if time.monotonic() - last_check > 20:
                    last_check = time.monotonic()
                    self._advertise()  # network may have changed
                continue
            except OSError:
                break
            threading.Thread(target=self._handshake, args=(conn, addr), daemon=True).start()

    def _handshake(self, conn, addr):
        try:
            conn.settimeout(10)
            header = _recv_exact(conn, 12)
            if header[:4] != MAGIC:
                raise ConnectionError("not a CamBridge phone")
            msg_type, length = struct.unpack(">II", header[4:])
            if msg_type != MSG_HELLO or length > 10000:
                raise ConnectionError("bad hello")
            hello = json.loads(_recv_exact(conn, length).decode("utf-8"))
            device = str(hello.get("device") or "Phone")[:60]
            device_id = str(hello.get("deviceId") or "")[:80]
            key = str(hello.get("key") or "")
            platform = str(hello.get("platform") or "")
            trusted = self.settings["trusted"]

            if self.link.is_busy():
                _send_json(conn, MSG_WELCOME, {"status": "busy", "pc": pc_name()})
                raise ConnectionError("already streaming from another phone")

            if key and secrets.compare_digest(key, self.settings["pair_key"]):
                allowed = True
            elif device_id and device_id in trusted:
                allowed = True
            else:
                _send_json(conn, MSG_WELCOME, {"status": "waiting", "pc": pc_name(), "id": self.settings["pc_id"]})
                request = ApprovalRequest(device, device_id, platform)
                self.requests.put(request)
                request.done.wait(60)
                allowed = bool(request.allowed)

            if not allowed:
                _send_json(conn, MSG_WELCOME, {"status": "denied", "pc": pc_name()})
                raise ConnectionError("denied")
            if device_id and trusted.get(device_id) != device:
                trusted[device_id] = device
                self.settings.save()
            _send_json(conn, MSG_WELCOME, {"status": "ok", "pc": pc_name(), "id": self.settings["pc_id"],
                                           "key": self.settings["pair_key"]})
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            log.info("Wi-Fi phone accepted: %s (%s)", device, addr[0])
            self.link.offer_wifi(conn, device)
        except Exception as e:
            log.info("Wi-Fi handshake from %s ended: %s", addr[0], e)
            try:
                conn.close()
            except OSError:
                pass
