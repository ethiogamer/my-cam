import AVFoundation
import Combine
import UIKit

enum CameraSide: String, CaseIterable, Identifiable {
    case back = "Back"
    case front = "Front"
    var id: String { rawValue }
    var position: AVCaptureDevice.Position { self == .back ? .back : .front }
}

enum Lens: String, CaseIterable, Identifiable {
    case ultraWide = "0.5x"
    case wide = "1x"
    case tele = "Zoom"
    var id: String { rawValue }
    var deviceType: AVCaptureDevice.DeviceType {
        switch self {
        case .ultraWide: return .builtInUltraWideCamera
        case .wide: return .builtInWideAngleCamera
        case .tele: return .builtInTelephotoCamera
        }
    }
}

enum VideoQuality: String, CaseIterable, Identifiable {
    case hd720 = "720p"
    case hd1080 = "1080p"
    var id: String { rawValue }
    var preset: AVCaptureSession.Preset { self == .hd720 ? .hd1280x720 : .hd1920x1080 }
}

enum OrientationMode: String, CaseIterable, Identifiable {
    case auto = "Auto"
    case landscape = "Landscape"
    case landscapeFlipped = "Landscape (flipped)"
    case portrait = "Portrait"
    var id: String { rawValue }
}

final class CameraModel: NSObject, ObservableObject {
    // MARK: Settings (saved between launches)
    @Published var side: CameraSide = .back {
        didSet { guard side != oldValue else { return }; save(); refreshLenses(); reconfigure() }
    }
    @Published var lens: Lens = .wide {
        didSet { guard lens != oldValue else { return }; save(); reconfigure() }
    }
    @Published var quality: VideoQuality = .hd1080 {
        didSet { guard quality != oldValue else { return }; save(); reconfigure() }
    }
    @Published var orientationMode: OrientationMode = .auto {
        didSet { guard orientationMode != oldValue else { return }; save(); applyOrientation() }
    }

    // MARK: Status
    @Published private(set) var availableLenses: [Lens] = [.wide]
    @Published private(set) var isConnected = false
    @Published private(set) var fps = 0
    @Published private(set) var permissionDenied = false
    @Published private(set) var outputDescription = ""
    @Published var screenOff = false {
        didSet { applyScreenOff() }
    }

    let session = AVCaptureSession()
    let server = StreamServer()

    private let sessionQueue = DispatchQueue(label: "akremcam.session")
    private let videoQueue = DispatchQueue(label: "akremcam.video", qos: .userInteractive)
    private let videoOutput = AVCaptureVideoDataOutput()
    private var videoInput: AVCaptureDeviceInput?
    private let encoder = JPEGEncoder()
    private var started = false
    private var fpsTimer: Timer?
    private var savedBrightness: CGFloat = 0.5
    private var currentVideoOrientation: AVCaptureVideoOrientation = .landscapeRight
    private var lastWidth = 0
    private var lastHeight = 0

    override init() {
        super.init()
        load()
        encoder.quality = 0.8
    }

    // MARK: - Start

    func start() {
        guard !started else { return }
        started = true

        UIApplication.shared.isIdleTimerDisabled = true
        UIDevice.current.beginGeneratingDeviceOrientationNotifications()

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(deviceOrientationChanged),
                       name: UIDevice.orientationDidChangeNotification, object: nil)
        nc.addObserver(self, selector: #selector(appWillEnterForeground),
                       name: UIApplication.willEnterForegroundNotification, object: nil)
        nc.addObserver(self, selector: #selector(appDidEnterBackground),
                       name: UIApplication.didEnterBackgroundNotification, object: nil)

        server.onConnectionChange = { [weak self] connected in
            DispatchQueue.main.async { self?.isConnected = connected }
        }
        server.onNewConnection = { [weak self] in
            self?.sendInfo()
        }
        server.start()

        fpsTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.fps = self.server.takeSentFrameCount()
        }

        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            setupSession()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { granted in
                DispatchQueue.main.async {
                    if granted { self.setupSession() } else { self.permissionDenied = true }
                }
            }
        default:
            permissionDenied = true
        }
    }

    private func setupSession() {
        refreshLenses()
        updateOrientationFromDevice()
        reconfigure()
    }

    @objc private func appWillEnterForeground() {
        UIApplication.shared.isIdleTimerDisabled = true
        server.start()
        sessionQueue.async {
            if !self.session.isRunning { self.session.startRunning() }
        }
    }

    @objc private func appDidEnterBackground() {
        if screenOff { screenOff = false }
    }

    // MARK: - Camera configuration

    private func findDevice(side: CameraSide, lens: Lens) -> AVCaptureDevice? {
        if let d = AVCaptureDevice.default(lens.deviceType, for: .video, position: side.position) {
            return d
        }
        return AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: side.position)
    }

    private func refreshLenses() {
        let found = Lens.allCases.filter {
            AVCaptureDevice.default($0.deviceType, for: .video, position: side.position) != nil
        }
        let lenses = found.isEmpty ? [.wide] : found
        availableLenses = lenses
        if !lenses.contains(lens) { lens = .wide }
    }

    private func reconfigure() {
        guard started, !permissionDenied else { return }
        let side = self.side, lens = self.lens, quality = self.quality
        sessionQueue.async {
            self.configureSession(side: side, lens: lens, quality: quality)
        }
    }

    private func configureSession(side: CameraSide, lens: Lens, quality: VideoQuality) {
        guard let device = findDevice(side: side, lens: lens) else { return }

        session.beginConfiguration()

        if let old = videoInput {
            session.removeInput(old)
            videoInput = nil
        }
        if let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) {
            session.addInput(input)
            videoInput = input
        }

        if session.canSetSessionPreset(quality.preset) {
            session.sessionPreset = quality.preset
        } else if session.canSetSessionPreset(.hd1280x720) {
            session.sessionPreset = .hd1280x720
        } else {
            session.sessionPreset = .high
        }

        if !session.outputs.contains(videoOutput) {
            videoOutput.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
            ]
            videoOutput.alwaysDiscardsLateVideoFrames = true
            videoOutput.setSampleBufferDelegate(self, queue: videoQueue)
            if session.canAddOutput(videoOutput) {
                session.addOutput(videoOutput)
            }
        }

        applyConnectionSettings()
        session.commitConfiguration()

        configureDevice(device)

        if !session.isRunning {
            session.startRunning()
        }
    }

    private func configureDevice(_ device: AVCaptureDevice) {
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }

            let supports30 = device.activeFormat.videoSupportedFrameRateRanges.contains {
                $0.minFrameRate <= 30 && 30 <= $0.maxFrameRate
            }
            if supports30 {
                device.activeVideoMinFrameDuration = CMTime(value: 1, timescale: 30)
                device.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: 30)
            }
            if device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusMode = .continuousAutoFocus
            }
            if device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposureMode = .continuousAutoExposure
            }
            if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
            if device.isLowLightBoostSupported {
                device.automaticallyEnablesLowLightBoostWhenAvailable = true
            }
        } catch {
            // Keep the camera's defaults.
        }
    }

    /// Must be called on sessionQueue.
    private func applyConnectionSettings() {
        guard let conn = videoOutput.connection(with: .video) else { return }
        if conn.isVideoOrientationSupported {
            conn.videoOrientation = currentVideoOrientation
        }
        if conn.isVideoMirroringSupported {
            // Video apps mirror your self-view themselves; send the true image.
            conn.automaticallyAdjustsVideoMirroring = false
            conn.isVideoMirrored = false
        }
        if conn.isVideoStabilizationSupported {
            conn.preferredVideoStabilizationMode = .off // lowest delay
        }
    }

    // MARK: - Orientation

    @objc private func deviceOrientationChanged() {
        guard orientationMode == .auto else { return }
        updateOrientationFromDevice()
    }

    private func updateOrientationFromDevice() {
        let target: AVCaptureVideoOrientation
        switch orientationMode {
        case .landscape:
            target = .landscapeRight
        case .landscapeFlipped:
            target = .landscapeLeft
        case .portrait:
            target = .portrait
        case .auto:
            switch UIDevice.current.orientation {
            case .landscapeLeft: target = .landscapeRight
            case .landscapeRight: target = .landscapeLeft
            case .portrait: target = .portrait
            case .portraitUpsideDown: target = .portraitUpsideDown
            default: return // face up / face down / unknown: keep the current one
            }
        }
        currentVideoOrientation = target
        sessionQueue.async {
            self.applyConnectionSettings()
        }
    }

    private func applyOrientation() {
        updateOrientationFromDevice()
    }

    // MARK: - Focus

    /// `point` is in camera coordinates (0...1).
    func focus(at point: CGPoint) {
        sessionQueue.async {
            guard let device = self.videoInput?.device else { return }
            do {
                try device.lockForConfiguration()
                defer { device.unlockForConfiguration() }
                if device.isFocusPointOfInterestSupported, device.isFocusModeSupported(.continuousAutoFocus) {
                    device.focusPointOfInterest = point
                    device.focusMode = .continuousAutoFocus
                }
                if device.isExposurePointOfInterestSupported, device.isExposureModeSupported(.continuousAutoExposure) {
                    device.exposurePointOfInterest = point
                    device.exposureMode = .continuousAutoExposure
                }
            } catch {}
        }
    }

    // MARK: - Screen off (saves battery and heat)

    private func applyScreenOff() {
        if screenOff {
            savedBrightness = UIScreen.main.brightness
            UIScreen.main.brightness = 0.0
        } else {
            UIScreen.main.brightness = savedBrightness
        }
    }

    // MARK: - Info to PC

    private func sendInfo() {
        DispatchQueue.main.async {
            var info: [String: Any] = [
                "app": "Akrem Cam",
                "camera": self.side.rawValue,
                "lens": self.lens.rawValue,
                "quality": self.quality.rawValue,
                "device": UIDevice.current.name,
            ]
            if self.lastWidth > 0 {
                info["width"] = self.lastWidth
                info["height"] = self.lastHeight
            }
            self.server.sendInfo(info)
        }
    }

    // MARK: - Saving settings

    private func save() {
        let d = UserDefaults.standard
        d.set(side.rawValue, forKey: "side")
        d.set(lens.rawValue, forKey: "lens")
        d.set(quality.rawValue, forKey: "quality")
        d.set(orientationMode.rawValue, forKey: "orientation")
    }

    private func load() {
        let d = UserDefaults.standard
        if let v = d.string(forKey: "side"), let s = CameraSide(rawValue: v) { side = s }
        if let v = d.string(forKey: "lens"), let l = Lens(rawValue: v) { lens = l }
        if let v = d.string(forKey: "quality"), let q = VideoQuality(rawValue: v) { quality = q }
        if let v = d.string(forKey: "orientation"), let o = OrientationMode(rawValue: v) { orientationMode = o }
    }
}

// MARK: - Frames

extension CameraModel: AVCaptureVideoDataOutputSampleBufferDelegate {
    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        // Nothing to do if the PC isn't connected or is still receiving the last frame.
        guard server.isReadyForFrame,
              let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let w = CVPixelBufferGetWidth(pixelBuffer)
        let h = CVPixelBufferGetHeight(pixelBuffer)
        if w != lastWidth || h != lastHeight {
            lastWidth = w
            lastHeight = h
            sendInfo()
        }

        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        encoder.encode(pixelBuffer, pts: pts) { [weak self] data in
            guard let data = data else { return }
            self?.server.sendFrame(data)
        }
    }
}
