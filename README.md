# CamBridge

Use your **iPhone or Android phone** as a webcam on Windows — over the **USB cable** (no Wi-Fi, no hotspot).

## Connect over Wi-Fi (no cable)

1. Phone and PC on the **same Wi-Fi**. Open CamBridge on both.
2. On the PC click **Wi-Fi** to show a QR code. On the phone tap **Wi-Fi** → **Scan QR code**
   (on iPhone you can also scan it with the normal Camera app) — **or** just tap your PC in the list
   and click **Allow** on the PC.
3. Next time the phone reconnects by itself. The first time, Windows may ask to allow CamBridge
   on the network — click **Allow** (private networks).

The phone always connects *to* the PC, and only phones you paired or allowed get in.
A USB cable is still the most stable and has the lowest delay.

## Android setup (one time)

1. On the phone: Settings → About phone → tap **Build number** 7 times (turns on Developer options).
2. Settings → Developer options → turn on **USB debugging**.
3. Plug the phone into the PC and open **CamBridge.exe**. Tap **Allow** on the phone when it asks.
4. CamBridge installs its app on the phone and opens it by itself. (Or install `CamBridge.apk` yourself.)

No 7-day limit on Android — the app stays installed.

```
iPhone app  ──USB cable──▶  CamBridge.exe  ──▶  "CamBridge" webcam in Zoom / Telegram / OBS / Meet
```

## Downloads

Both files are on the **Releases** page → `latest`:

- `CamBridge.ipa` — the iPhone app
- `CamBridge.exe` — the Windows app

## One-time setup

1. **Install iTunes on the PC** (from apple.com — it includes Apple's USB driver).
2. **Install the iPhone app with Sideloadly**
   - Plug in the iPhone, open Sideloadly, drag in `CamBridge.ipa`, enter an Apple ID, click **Start**.
   - On the iPhone: Settings → Privacy & Security → **Developer Mode** → On (the phone restarts).
   - Settings → General → VPN & Device Management → tap your Apple ID → **Trust**.
3. **Run `CamBridge.exe`** on the PC. The first time, Windows asks for permission to install
   the webcam — click **Yes**. (If Windows SmartScreen appears: *More info → Run anyway*.)

## What you can control (from the PC window)

- **iPhone camera (left side):** Back/Front, lens (0.5x / 1x / Zoom), zoom, exposure (auto + brightness, or
  manual ISO + shutter speed), focus (auto, or manual distance), white balance (auto, or manual temperature
  + tint), flash light + strength, 720p/1080p, 30/60 fps, stabilization, HDR, dark iPhone screen.
- **Picture (right side):** filters (Vivid, Warm, Cool, Bright, Soft, Mono), brightness, contrast,
  saturation, warmth, tint, digital zoom (drag the preview to move, scroll to zoom), rotate, mirror, flip,
  webcam output size.
- **Top bar:** profiles, **Pause** (others see a "Camera paused" screen) and **Record** (saved to
  Videos\CamBridge). Shortcuts: Ctrl+P pause, Ctrl+R record.
- Closing the window keeps the camera running — the icon next to the clock opens it again or quits.

## Every day

1. Plug in the iPhone, open **CamBridge** on it.
2. Open **CamBridge.exe** on the PC.
3. In your video app, choose the camera **CamBridge**.

Tip: tap the 🌙 button on the iPhone to make the screen dark and save battery.

## Every 7 days (free Apple ID)

The iPhone app stops opening after 7 days. Plug in the phone, open Sideloadly, and click
**Start** again with the same `CamBridge.ipa`.

## How it works

- **iPhone (Swift):** camera → hardware JPEG encoder → TCP port 7878 on the phone.
- **USB:** Apple's usbmuxd service (comes with iTunes) tunnels the PC to that port over the cable.
- **Windows (Python):** receives frames → "CamBridge" DirectShow virtual camera
  (a renamed build of the open-source [Softcam](https://github.com/tshino/softcam) library, MIT).
- **Build:** GitHub Actions builds both apps on every push (macOS runner for the iPhone app).

## License

CamBridge is **free to use** (personal and business). It is not open source: copying, changing,
reselling or re-uploading it needs permission from the author. See [LICENSE](LICENSE).

CamBridge uses some open-source components, credited in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) with their licenses in the `licenses` folder.
