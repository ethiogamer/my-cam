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
