# Third-party notices

CamBridge is written by its author. It uses the open-source components below, each under its
own license. Full license texts are in the `licenses` folder (and inside each app, under
**Licenses**).

## Windows app (CamBridge.exe)

| Component | Used for | License | Copyright |
|---|---|---|---|
| [Softcam](https://github.com/tshino/softcam) (renamed build) | the virtual webcam driver | MIT — `licenses/Softcam-MIT.txt` | © 2020 tshino |
| [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools) (adb) | USB connection to Android phones | Apache 2.0 — `licenses/Apache-2.0.txt` | © The Android Open Source Project |
| [Python](https://www.python.org) | runtime | PSF License — `licenses/Python-PSF.txt` | © Python Software Foundation |
| [Pillow](https://python-pillow.org) | picture processing | MIT-CMU — `licenses/Pillow.txt` | © Jeffrey A. Clark and contributors; © Secret Labs AB; © Fredrik Lundh |
| [pystray](https://github.com/moses-palmer/pystray) | taskbar (tray) icon | LGPL 3.0 — `licenses/pystray-LGPL-3.0.txt` (+ `GPL-3.0.txt`) | © Moses Palmér |
| [python-zeroconf](https://github.com/python-zeroconf/python-zeroconf) | finding phones on Wi-Fi | LGPL 2.1 or later — `licenses/zeroconf-LGPL-2.1.txt` | © Paul Scott-Murphy, William McBrine, and contributors |
| [ifaddr](https://github.com/ifaddr/ifaddr) | listing network addresses | MIT — `licenses/ifaddr-MIT.txt` | © 2014 Stefan C. Mueller |
| [qrcode](https://github.com/lincolnloop/python-qrcode) | the pairing QR code | BSD — `licenses/qrcode-BSD.txt` | © 2011 Lincoln Loop |
| [PyInstaller](https://pyinstaller.org) bootloader | packaging into one .exe | GPL 2.0 with bootloader exception — `licenses/PyInstaller.txt` | © PyInstaller Development Team |
| Tcl/Tk (part of Python) | the window | Tcl/Tk license (BSD-style) | © The Regents of the University of California, Sun Microsystems and others |

**LGPL components (pystray, python-zeroconf):** their source code is available at the links
above. CamBridge's own source code is published at https://github.com/ethiogamer/my-cam, so you
can rebuild the program with modified versions of these libraries, as the LGPL allows.

## Android app (CamBridge.apk)

| Component | License | Copyright |
|---|---|---|
| [AndroidX: CameraX, Activity, Core](https://developer.android.com/jetpack/androidx) | Apache 2.0 | © The Android Open Source Project |
| [Kotlin standard library](https://kotlinlang.org) | Apache 2.0 | © JetBrains s.r.o. and Kotlin contributors |
| [ZXing core](https://github.com/zxing/zxing) (QR code reading) | Apache 2.0 | © ZXing authors |

## iPhone app (CamBridge.ipa)

Uses only Apple's built-in system frameworks; no third-party code is included.

## Trademarks

iPhone is a trademark of Apple Inc. Android is a trademark of Google LLC. Windows is a trademark
of Microsoft Corporation. Other names belong to their owners. CamBridge is an independent product
and is not affiliated with, endorsed by, or sponsored by any of them.
