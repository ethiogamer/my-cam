# Akrem Cam

Use your iPhone as a webcam on Windows — over the **USB cable** (no Wi-Fi, no hotspot).

```
iPhone app  ──USB cable──▶  AkremCam.exe  ──▶  "Akrem Cam" webcam in Zoom / Telegram / OBS / Meet
```

## Downloads

Both files are on the **Releases** page → `latest`:

- `AkremCam.ipa` — the iPhone app
- `AkremCam.exe` — the Windows app

## One-time setup

1. **Install iTunes on the PC** (from apple.com — it includes Apple's USB driver).
2. **Install the iPhone app with Sideloadly**
   - Plug in the iPhone, open Sideloadly, drag in `AkremCam.ipa`, enter an Apple ID, click **Start**.
   - On the iPhone: Settings → Privacy & Security → **Developer Mode** → On (the phone restarts).
   - Settings → General → VPN & Device Management → tap your Apple ID → **Trust**.
3. **Run `AkremCam.exe`** on the PC. The first time, Windows asks for permission to install
   the webcam — click **Yes**. (If Windows SmartScreen appears: *More info → Run anyway*.)

## What you can control (from the PC window)

- **iPhone camera (left side):** Back/Front, lens (0.5x / 1x / Zoom), zoom, exposure (auto + brightness, or
  manual ISO + shutter speed), focus (auto, or manual distance), white balance (auto, or manual temperature
  + tint), flash light + strength, 720p/1080p, 30/60 fps, stabilization, HDR, dark iPhone screen.
- **Picture (right side):** filters (Vivid, Warm, Cool, Bright, Soft, Mono), brightness, contrast,
  saturation, warmth, tint, digital zoom (drag the preview to move, scroll to zoom), rotate, mirror, flip,
  webcam output size.
- **Top bar:** profiles, **Pause** (others see a "Camera paused" screen) and **Record** (saved to
  Videos\Akrem Cam). Shortcuts: Ctrl+P pause, Ctrl+R record.
- Closing the window keeps the camera running — the icon next to the clock opens it again or quits.

## Every day

1. Plug in the iPhone, open **Akrem Cam** on it.
2. Open **AkremCam.exe** on the PC.
3. In your video app, choose the camera **Akrem Cam**.

Tip: tap the 🌙 button on the iPhone to make the screen dark and save battery.

## Every 7 days (free Apple ID)

The iPhone app stops opening after 7 days. Plug in the phone, open Sideloadly, and click
**Start** again with the same `AkremCam.ipa`.

## How it works

- **iPhone (Swift):** camera → hardware JPEG encoder → TCP port 7878 on the phone.
- **USB:** Apple's usbmuxd service (comes with iTunes) tunnels the PC to that port over the cable.
- **Windows (Python):** receives frames → "Akrem Cam" DirectShow virtual camera
  (a renamed build of the open-source [Softcam](https://github.com/tshino/softcam) library, MIT).
- **Build:** GitHub Actions builds both apps on every push (macOS runner for the iPhone app).
