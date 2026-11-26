"""Sound for CamBridge.

Source (one of):
  * "none"
  * "phone"         — the phone's microphone, sent with the video (cable or Wi-Fi)
  * "pc:<name>"     — a microphone plugged into this PC (e.g. a professional USB mic)

The sound goes to:
  * recordings (MP4 with sound), and
  * optionally an output device — e.g. "CABLE Input (VB-Audio Virtual Cable)" so that
    Zoom / Telegram can use it as a microphone ("CABLE Output").
"""

import collections
import logging
import threading
import time

log = logging.getLogger("cambridge")

try:
    import audioop  # Python 3.12 and older
except ImportError:  # pragma: no cover
    audioop = None


def _sd():
    try:
        import sounddevice as sd
        return sd
    except Exception:
        return None


def _hostapi_index(sd):
    """Use one Windows sound system (avoids every device appearing 3-4 times)."""
    try:
        apis = sd.query_hostapis()
        for i, api in enumerate(apis):
            if "WASAPI" in api["name"]:
                return i
        return sd.default.hostapi if isinstance(sd.default.hostapi, int) else 0
    except Exception:
        return None


def list_devices(kind):
    """kind = 'input' or 'output'. Returns device names."""
    sd = _sd()
    if sd is None:
        return []
    api = _hostapi_index(sd)
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    names = []
    try:
        for dev in sd.query_devices():
            if dev[key] > 0 and (api is None or dev["hostapi"] == api) and dev["name"] not in names:
                names.append(dev["name"])
    except Exception:
        pass
    return names


def _find(sd, name, kind):
    api = _hostapi_index(sd)
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, dev in enumerate(sd.query_devices()):
        if dev["name"] == name and dev[key] > 0 and (api is None or dev["hostapi"] == api):
            return i, dev
    return None, None


def convert(pcm, rate, channels, to_rate, to_channels, state=None):
    """16-bit PCM rate/channel conversion. Returns (pcm, state)."""
    if audioop is None:
        return pcm, state
    if channels != to_channels:
        if channels == 2 and to_channels == 1:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        elif channels == 1 and to_channels == 2:
            pcm = audioop.tostereo(pcm, 2, 1, 1)
    if rate != to_rate:
        pcm, state = audioop.ratecv(pcm, 2, to_channels, rate, to_rate, state)
    return pcm, state


class Effects:
    """Volume, voice clarity (removes low rumble), noise gate and limiter for 16-bit PCM."""

    def __init__(self):
        self.gain_db = 0.0
        self.limiter = True
        self.gate = False
        self.clarity = False
        self._hp = {}          # high-pass filter state per channel
        self._gate_open = 0.0

    def process(self, pcm, rate, channels):
        if audioop is None or not pcm:
            return pcm
        if self.clarity:
            pcm = self._low_cut(pcm, rate, channels)
        if abs(self.gain_db) > 0.05:
            pcm = audioop.mul(pcm, 2, 10 ** (self.gain_db / 20.0))
        if self.gate:
            rms = audioop.rms(pcm, 2)
            # open fast, close slowly (no choppy words)
            self._gate_open = 1.0 if rms > 350 else max(0.0, self._gate_open - 0.08)
            if self._gate_open < 1.0:
                pcm = audioop.mul(pcm, 2, self._gate_open)
        if self.limiter:
            peak = audioop.max(pcm, 2)
            limit = 29000  # about -1 dBFS
            if peak > limit:
                pcm = audioop.mul(pcm, 2, limit / peak)
        return pcm

    def _low_cut(self, pcm, rate, channels):
        # 1st-order high-pass at ~90 Hz (removes desk thumps, fan and traffic rumble)
        import array
        rc = 1.0 / (2 * 3.14159265 * 90.0)
        dt = 1.0 / max(8000, rate)
        a = rc / (rc + dt)
        samples = array.array("h", pcm)
        for ch in range(channels):
            prev_x, prev_y = self._hp.get(ch, (0.0, 0.0))
            for i in range(ch, len(samples), channels):
                x = samples[i]
                y = a * (prev_y + x - prev_x)
                prev_x, prev_y = x, y
                samples[i] = -32768 if y < -32768 else 32767 if y > 32767 else int(y)
            self._hp[ch] = (prev_x, prev_y)
        return samples.tobytes()


class _Output(threading.Thread):
    """Plays sound into an output device (e.g. VB-CABLE) from its own thread."""

    def __init__(self, name):
        super().__init__(daemon=True)
        self.name = name
        self.queue = collections.deque(maxlen=50)
        self.cond = threading.Condition()
        self.running = True
        self.error = None
        self.start()

    def put(self, pcm, rate, channels):
        with self.cond:
            self.queue.append((pcm, rate, channels))
            self.cond.notify()

    def stop(self):
        with self.cond:
            self.running = False
            self.cond.notify()

    def run(self):
        sd = _sd()
        if sd is None:
            self.error = "sound library missing"
            return
        index, dev = _find(sd, self.name, "output")
        if index is None:
            self.error = "device not found: %s" % self.name
            return
        rate = int(dev.get("default_samplerate") or 48000)
        channels = min(2, int(dev["max_output_channels"]))
        state = None
        try:
            with sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16", device=index,
                                    latency="low") as stream:
                while self.running:
                    with self.cond:
                        if not self.queue:
                            self.cond.wait(0.5)
                        if not self.queue:
                            continue
                        pcm, src_rate, src_ch = self.queue.popleft()
                        if len(self.queue) > 10:  # running behind: catch up
                            self.queue.clear()
                    data, state = convert(pcm, src_rate, src_ch, rate, channels, state)
                    stream.write(data)
        except Exception as e:
            self.error = str(e)
            log.warning("audio output failed: %s", e)


class AudioHub:
    def __init__(self, settings, link):
        self.settings = settings
        self.link = link
        self.source = settings["audio_source"]
        self.send_to = settings["audio_send_to"]
        self.recorder = None              # RecordingWorker (set while recording)
        self.level = 0.0                  # 0..1 for the level meter
        self.rate = 48000
        self.channels = 1
        self.last_audio = 0.0
        self.error = None
        self.effects = Effects()
        fx = settings["audio_fx"]
        self.effects.gain_db = float(fx.get("gain_db", 0.0))
        self.effects.limiter = bool(fx.get("limiter", True))
        self.effects.gate = bool(fx.get("gate", False))
        self.effects.clarity = bool(fx.get("clarity", False))
        self.muted = False
        self._in_stream = None
        self._out = None
        self._rec_state = None
        self._lock = threading.Lock()
        self.apply()

    # ---- settings -------------------------------------------------------------

    def set_source(self, source):
        self.source = source
        self.settings["audio_source"] = source
        self.settings.save()
        self.apply()

    def set_send_to(self, name):
        self.send_to = name
        self.settings["audio_send_to"] = name
        self.settings.save()
        self.apply()

    def apply(self):
        self.error = None
        self._stop_input()
        if self._out is not None:
            self._out.stop()
            self._out = None
        if self.send_to:
            self._out = _Output(self.send_to)
        self.link.audio_wanted = self.source == "phone"
        self.link.send_command(audio=self.source == "phone")
        if self.source.startswith("pc:"):
            self._start_input(self.source[3:])

    def set_effect(self, name, value):
        setattr(self.effects, name, value)
        self.settings["audio_fx"][name] = value
        self.settings.save()

    def phone_wants_audio(self):
        return self.source == "phone"

    # ---- PC microphone ----------------------------------------------------------

    def _start_input(self, name):
        sd = _sd()
        if sd is None:
            self.error = "sound library missing"
            return
        try:
            index, dev = _find(sd, name, "input")
            if index is None:
                self.error = "microphone not found: %s" % name
                return
            channels = min(2, int(dev["max_input_channels"]))
            rate = 48000
            try:
                sd.check_input_settings(device=index, channels=channels, dtype="int16", samplerate=rate)
            except Exception:
                rate = int(dev.get("default_samplerate") or 48000)
            self.rate, self.channels = rate, channels

            def callback(indata, frames, time_info, status):
                self.deliver(bytes(indata), rate, channels)

            self._in_stream = sd.RawInputStream(samplerate=rate, channels=channels, dtype="int16",
                                                device=index, blocksize=rate // 100 * 2, callback=callback,
                                                latency="low")
            self._in_stream.start()
        except Exception as e:
            self.error = "microphone problem: %s" % e
            log.warning(self.error)

    def _stop_input(self):
        if self._in_stream is not None:
            try:
                self._in_stream.stop()
                self._in_stream.close()
            except Exception:
                pass
            self._in_stream = None

    # ---- sound arriving (phone or PC mic) ------------------------------------------

    def phone_audio(self, payload):
        """Message from the phone: 4 bytes sample rate (big-endian), 1 byte channels, 16-bit PCM."""
        if self.source != "phone" or len(payload) < 7:
            return
        rate = int.from_bytes(payload[0:4], "big")
        channels = payload[4] or 1
        self.deliver(payload[5:], rate, channels)

    def deliver(self, pcm, rate, channels):
        now = time.monotonic()
        self.rate, self.channels, self.last_audio = rate, channels, now
        pcm = self.effects.process(pcm, rate, channels)
        if self.muted and audioop is not None:
            pcm = audioop.mul(pcm, 2, 0.0)
        if audioop is not None and pcm:
            try:
                peak = audioop.max(pcm, 2) / 32768.0
                self.level = max(peak, self.level * 0.85)
            except Exception:
                pass
        rec = self.recorder
        if rec is not None and rec.audio_rate:
            with self._lock:
                data, self._rec_state = convert(pcm, rate, channels, rec.audio_rate, rec.audio_channels,
                                                self._rec_state)
            frames = len(data) // (2 * rec.audio_channels)
            rec.put_audio(data, frames, now)
        if self._out is not None:
            self._out.put(pcm, rate, channels)

    def active(self):
        return self.source != "none" and time.monotonic() - self.last_audio < 1.5

    def recording_format(self):
        """(rate, channels) to open a recording with, or (0, 0) for no sound."""
        if self.source == "none":
            return 0, 0
        rate = self.rate if self.rate in (44100, 48000) else 48000
        return rate, min(2, self.channels or 1)

    def close(self):
        self._stop_input()
        if self._out is not None:
            self._out.stop()
