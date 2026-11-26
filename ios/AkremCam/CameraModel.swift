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
    var size: (width: Int32, height: Int32) { self == .hd720 ? (1280, 720) : (1920, 1080) }
}

enum FrameRate: Int, CaseIterable, Identifiable {
    case fps30 = 30
    case fps60 = 60
    var id: Int { rawValue }
    var label: String { "\(rawValue) fps" }
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
    @Published var frameRate: FrameRate = .fps30 {
        didSet { guard frameRate != oldValue else { return }; save(); reconfigure() }
    }
    @Published var orientationMode: OrientationMode = .auto {
        didSet { guard orientationMode != oldValue else { return }; save(); updateOrientationFromDevice() }
    }
    @Published var mirror = false {
        didSet {
            guard mirror != oldValue else { return }
            save()
            mirrorForSession = mirror
            sessionQueue.async { self.applyConnectionSettings() }
            sendInfo()
        }
    }

    // MARK: Live adjustments
    @Published var zoom: CGFloat = 1.0 {
        didSet { guard zoom != oldValue else { return }; applyZoom(); sendInfo() }
    }
    @Published var exposureBias: Float = 0 {
        didSet { guard exposureBias != oldValue else { return }; applyExposure(); sendInfo() }
    }
    @Published var torchOn = false {
        didSet { guard torchOn != oldValue else { return }; applyTorch(); sendInfo() }
    }
    @Published var screenOff = false {
        didSet { guard screenOff != oldValue else { return }; applyScreenOff(); sendInfo() }
    }

    // MARK: Status
    @Published private(set) var availableLenses: [Lens] = [.wide]
    @Published private(set) var maxZoom: CGFloat = 1.0
    @Published private(set) var torchAvailable = false
    @Published private(set) var actualFrameRate = 30
    @Published private(set) var isConnected = false
    @Published private(set) var fps = 0
    @Published private(set) var isHot = false
    @Published private(set) var permissionDenied = false

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
    private var mirrorForSession = false
    private var pinchStartZoom: CGFloat = 1.0
    private var lastWidth = 0
    private var lastHeight = 0
    private var skipToggle = false

    static let zoomLimit: CGFloat = 10.0
    static let exposureLimit: Float = 2.0

    override init() {
        super.init()
        load()
        mirrorForSession = mirror
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
        nc.addObserver(self, selector: #selector(thermalStateChanged),
                       name: ProcessInfo.thermalStateDidChangeNotification, object: nil)
        thermalStateChanged()

        server.onConnectionChange = { [weak self] connected in
            DispatchQueue.main.async { self?.isConnected = connected }
        }
        server.onNewConnection = { [weak self] in
            self?.sendInfo()
        }
        server.onCommand = { [weak self] command in
            DispatchQueue.main.async { self?.handle(command: command) }
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
        if torchOn { torchOn = false }
    }

    @objc private func thermalStateChanged() {
        let state = ProcessInfo.processInfo.thermalState
        let hot = state == .serious || state == .critical
        DispatchQueue.main.async {
            if self.isHot != hot {
                self.isHot = hot
                self.sendInfo()
            }
        }
    }

    // MARK: - Commands from the PC

    private func handle(command c: [String: Any]) {
        if let v = c["side"] as? String, let s = CameraSide(rawValue: v) { side = s }
        if let v = c["lens"] as? String, let l = Lens(rawValue: v), availableLenses.contains(l) { lens = l }
        if let v = c["quality"] as? String, let q = VideoQuality(rawValue: v) { quality = q }
        if let v = c["fps"] as? Int, let f = FrameRate(rawValue: v) { frameRate = f }
        if let v = c["orientation"] as? String, let o = OrientationMode(rawValue: v) { orientationMode = o }
        if let v = c["zoom"] as? Double { zoom = min(max(CGFloat(v), 1.0), maxZoom) }
        if let v = c["exposure"] as? Double {
            exposureBias = min(max(Float(v), -CameraModel.exposureLimit), CameraModel.exposureLimit)
        }
        if let v = c["torch"] as? Bool { torchOn = v && torchAvailable }
        if let v = c["mirror"] as? Bool { mirror = v }
        if let v = c["screenOff"] as? Bool { screenOff = v }
        if c["focusCenter"] != nil { focus(at: CGPoint(x: 0.5, y: 0.5)) }
        if c["getInfo"] != nil { sendInfo() }
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
        let side = self.side, lens = self.lens, quality = self.quality, rate = self.frameRate
        sessionQueue.async {
            self.configureSession(side: side, lens: lens, quality: quality, rate: rate)
        }
    }

    /// Finds a camera mode with exactly the size we want, preferring one that can do `fps`.
    private func bestFormat(for device: AVCaptureDevice, quality: VideoQuality, fps: Int) -> AVCaptureDevice.Format? {
        let size = quality.size
        let matching = device.formats.filter { format in
            let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            let subtype = CMFormatDescriptionGetMediaSubType(format.formatDescription)
            return dims.width == size.width && dims.height == size.height
                && subtype == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
        }
        func supports(_ f: AVCaptureDevice.Format, _ rate: Int) -> Bool {
            f.videoSupportedFrameRateRanges.contains { $0.minFrameRate <= Double(rate) && Double(rate) <= $0.maxFrameRate }
        }
        let fast = matching.filter { supports($0, fps) }
        return fast.first(where: { !$0.isVideoBinned }) ?? fast.first
            ?? matching.first(where: { supports($0, 30) }) ?? matching.first
    }

    private func configureSession(side: CameraSide, lens: Lens, quality: VideoQuality, rate: FrameRate) {
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

        // Pick the camera mode. Holding the device lock until after commit stops the
        // session from overriding our choice.
        var chosenRate = 30
        var locked = false
        if let format = bestFormat(for: device, quality: quality, fps: rate.rawValue),
           (try? device.lockForConfiguration()) != nil {
            locked = true
            device.activeFormat = format
            let canDo = format.videoSupportedFrameRateRanges.contains {
                $0.minFrameRate <= Double(rate.rawValue) && Double(rate.rawValue) <= $0.maxFrameRate
            }
            chosenRate = canDo ? rate.rawValue : 30
            let duration = CMTime(value: 1, timescale: CMTimeScale(chosenRate))
            device.activeVideoMinFrameDuration = duration
            device.activeVideoMaxFrameDuration = duration
            applyAutoModes(device)
        } else {
            if session.canSetSessionPreset(quality.preset) {
                session.sessionPreset = quality.preset
            } else if session.canSetSessionPreset(.hd1280x720) {
                session.sessionPreset = .hd1280x720
            } else {
                session.sessionPreset = .high
            }
        }

        applyConnectionSettings()
        session.commitConfiguration()
        if locked { device.unlockForConfiguration() }

        let maxZ = min(device.activeFormat.videoMaxZoomFactor, CameraModel.zoomLimit)
        let hasTorch = device.hasTorch

        if !session.isRunning {
            session.startRunning()
        }

        DispatchQueue.main.async {
            self.actualFrameRate = chosenRate
            self.maxZoom = max(1.0, maxZ)
            self.torchAvailable = hasTorch
            if !hasTorch { self.torchOn = false }
            if self.zoom > self.maxZoom { self.zoom = 1.0 }
            // A new camera starts at its defaults: re-apply the current adjustments.
            self.applyZoom()
            self.applyExposure()
            self.applyTorch()
            self.sendInfo()
        }
    }

    private func applyAutoModes(_ device: AVCaptureDevice) {
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
    }

    /// Must be called on sessionQueue.
    private func applyConnectionSettings() {
        guard let conn = videoOutput.connection(with: .video) else { return }
        if conn.isVideoOrientationSupported {
            conn.videoOrientation = currentVideoOrientation
        }
        if conn.isVideoMirroringSupported {
            // Video apps mirror your self-view themselves, so by default send the true image.
            conn.automaticallyAdjustsVideoMirroring = false
            conn.isVideoMirrored = mirrorForSession
        }
        if conn.isVideoStabilizationSupported {
            conn.preferredVideoStabilizationMode = .off // lowest delay
        }
    }

    // MARK: - Live adjustments

    private func withDevice(_ body: @escaping (AVCaptureDevice) -> Void) {
        sessionQueue.async {
            guard let device = self.videoInput?.device else { return }
            do {
                try device.lockForConfiguration()
                body(device)
                device.unlockForConfiguration()
            } catch {}
        }
    }

    private func applyZoom() {
        let wanted = zoom
        withDevice { device in
            let upper = min(device.maxAvailableVideoZoomFactor, CameraModel.zoomLimit)
            let lower = device.minAvailableVideoZoomFactor
            device.videoZoomFactor = min(max(wanted, lower), max(lower, upper))
        }
    }

    private func applyExposure() {
        let wanted = exposureBias
        withDevice { device in
            let value = min(max(wanted, device.minExposureTargetBias), device.maxExposureTargetBias)
            device.setExposureTargetBias(value, completionHandler: nil)
        }
    }

    private func applyTorch() {
        let on = torchOn
        withDevice { device in
            guard device.hasTorch, device.isTorchAvailable else { return }
            if on {
                try? device.setTorchModeOn(level: 0.5)
            } else if device.torchMode != .off {
                device.torchMode = .off
            }
        }
    }

    /// Pinch on the preview. `scale` is relative to the start of the pinch.
    func pinch(scale: CGFloat, began: Bool) {
        if began { pinchStartZoom = zoom }
        zoom = min(max(pinchStartZoom * scale, 1.0), maxZoom)
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

    // MARK: - Focus

    /// `point` is in camera coordinates (0...1).
    func focus(at point: CGPoint) {
        withDevice { device in
            if device.isFocusPointOfInterestSupported, device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusPointOfInterest = point
                device.focusMode = .continuousAutoFocus
            }
            if device.isExposurePointOfInterestSupported, device.isExposureModeSupported(.continuousAutoExposure) {
                device.exposurePointOfInterest = point
                device.exposureMode = .continuousAutoExposure
            }
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
        guard started else { return }
        if !Thread.isMainThread {
            DispatchQueue.main.async { self.sendInfo() }
            return
        }
        var info: [String: Any] = [
            "app": "Akrem Cam",
            "device": UIDevice.current.name,
            "camera": side.rawValue,
            "lens": lens.rawValue,
            "lenses": availableLenses.map { $0.rawValue },
            "quality": quality.rawValue,
            "fps": frameRate.rawValue,
            "actualFps": actualFrameRate,
            "orientation": orientationMode.rawValue,
            "zoom": Double(zoom),
            "maxZoom": Double(maxZoom),
            "exposure": Double(exposureBias),
            "exposureLimit": Double(CameraModel.exposureLimit),
            "torch": torchOn,
            "torchAvailable": torchAvailable,
            "mirror": mirror,
            "screenOff": screenOff,
            "hot": isHot,
        ]
        if lastWidth > 0 {
            info["width"] = lastWidth
            info["height"] = lastHeight
        }
        server.sendInfo(info)
    }

    // MARK: - Saving settings

    private func save() {
        let d = UserDefaults.standard
        d.set(side.rawValue, forKey: "side")
        d.set(lens.rawValue, forKey: "lens")
        d.set(quality.rawValue, forKey: "quality")
        d.set(frameRate.rawValue, forKey: "fps")
        d.set(orientationMode.rawValue, forKey: "orientation")
        d.set(mirror, forKey: "mirror")
    }

    private func load() {
        let d = UserDefaults.standard
        if let v = d.string(forKey: "side"), let s = CameraSide(rawValue: v) { side = s }
        if let v = d.string(forKey: "lens"), let l = Lens(rawValue: v) { lens = l }
        if let v = d.string(forKey: "quality"), let q = VideoQuality(rawValue: v) { quality = q }
        if let f = FrameRate(rawValue: d.integer(forKey: "fps")) { frameRate = f }
        if let v = d.string(forKey: "orientation"), let o = OrientationMode(rawValue: v) { orientationMode = o }
        mirror = d.bool(forKey: "mirror")
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

        // When the phone is very hot, send every other frame to cool it down.
        if ProcessInfo.processInfo.thermalState == .critical {
            skipToggle.toggle()
            if skipToggle { return }
        }

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
