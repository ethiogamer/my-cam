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
    case uhd4K = "4K"
    var id: String { rawValue }
    var preset: AVCaptureSession.Preset {
        switch self {
        case .hd720: return .hd1280x720
        case .hd1080: return .hd1920x1080
        case .uhd4K: return .hd4K3840x2160
        }
    }
    var size: (width: Int32, height: Int32) {
        switch self {
        case .hd720: return (1280, 720)
        case .hd1080: return (1920, 1080)
        case .uhd4K: return (3840, 2160)
        }
    }
}

enum StreamCodec: String, CaseIterable, Identifiable {
    case h264, hevc, jpeg
    var id: String { rawValue }
    var label: String {
        switch self {
        case .h264: return "H.264"
        case .hevc: return "HEVC"
        case .jpeg: return "JPEG"
        }
    }
}

enum BitrateLevel: String, CaseIterable, Identifiable {
    case auto, low, high, max
    var id: String { rawValue }
    var label: String { rawValue.prefix(1).uppercased() + rawValue.dropFirst() }
    var factor: Double {
        switch self {
        case .low: return 0.5
        case .auto: return 1.0
        case .high: return 1.6
        case .max: return 2.5
        }
    }
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
    @Published var codec: StreamCodec = .h264 {
        didSet {
            guard codec != oldValue else { return }
            if codec != .jpeg { videoEncoder.resetFailures(); videoNote = "" }
            save(); updateEncoder(); videoEncoder.requestKeyframe(); sendInfo()
        }
    }
    @Published var bitrateLevel: BitrateLevel = .auto {
        didSet { guard bitrateLevel != oldValue else { return }; save(); updateEncoder(); sendInfo() }
    }
    @Published var lowLight = true {
        didSet { guard lowLight != oldValue else { return }; save(); applyLowLight(); sendInfo() }
    }
    /// The PC asked for the microphone.
    @Published private(set) var audioEnabled = false
    /// Shown on the PC when the video chip failed and the app fell back to JPEG.
    @Published private(set) var videoNote = ""
    @Published private(set) var micDenied = false
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
    @Published var torchLevel: Float = 0.5 {
        didSet { guard torchLevel != oldValue else { return }; applyTorch(); sendInfo() }
    }

    // MARK: Pro controls
    @Published var focusManual = false {
        didSet {
            guard focusManual != oldValue else { return }
            if focusManual, liveLens >= 0 { lensPosition = liveLens }
            applyFocus(); sendInfo()
        }
    }
    @Published var lensPosition: Float = 0.5 {
        didSet { guard lensPosition != oldValue else { return }; applyFocus(); sendInfo() }
    }
    @Published var exposureManual = false {
        didSet {
            guard exposureManual != oldValue else { return }
            if exposureManual, liveISO > 0 { iso = liveISO; shutter = liveShutter }
            applyExposure(); sendInfo()
        }
    }
    @Published var iso: Float = 100 {
        didSet { guard iso != oldValue else { return }; applyExposure(); sendInfo() }
    }
    /// Shutter speed in seconds (e.g. 1/120).
    @Published var shutter: Double = 1.0 / 60.0 {
        didSet { guard shutter != oldValue else { return }; applyExposure(); sendInfo() }
    }
    @Published var whiteBalanceManual = false {
        didSet {
            guard whiteBalanceManual != oldValue else { return }
            if whiteBalanceManual, liveTemperature > 0 { temperature = liveTemperature; tint = liveTint }
            applyWhiteBalance(); sendInfo()
        }
    }
    @Published var temperature: Float = 5000 {
        didSet { guard temperature != oldValue else { return }; applyWhiteBalance(); sendInfo() }
    }
    @Published var tint: Float = 0 {
        didSet { guard tint != oldValue else { return }; applyWhiteBalance(); sendInfo() }
    }
    @Published var stabilization = false {
        didSet {
            guard stabilization != oldValue else { return }
            save()
            stabilizationForSession = stabilization
            sessionQueue.async { self.applyConnectionSettings() }
            sendInfo()
        }
    }
    @Published var hdr = false {
        didSet { guard hdr != oldValue else { return }; save(); applyHDR(); sendInfo() }
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
    @Published private(set) var isoRange: ClosedRange<Float> = 25...1600
    @Published private(set) var shutterRange: ClosedRange<Double> = (1.0 / 8000.0)...(1.0 / 30.0)
    @Published private(set) var manualFocusSupported = false
    @Published private(set) var manualExposureSupported = false
    @Published private(set) var manualWhiteBalanceSupported = false
    @Published private(set) var stabilizationSupported = false
    @Published private(set) var hdrSupported = false
    // What the camera is doing right now (in auto modes too).
    @Published private(set) var liveISO: Float = 0
    @Published private(set) var liveShutter: Double = 0
    @Published private(set) var liveLens: Float = -1
    @Published private(set) var liveTemperature: Float = 0
    @Published private(set) var liveTint: Float = 0

    let session = AVCaptureSession()
    let server = StreamServer()
    let wifi = WifiManager()

    /// Looking for the pairing QR code on the PC screen.
    @Published var scanning = false {
        didSet { guard scanning != oldValue else { return }; applyScanning() }
    }
    private let metadataOutput = AVCaptureMetadataOutput()

    private let sessionQueue = DispatchQueue(label: "cambridge.session")
    private let videoQueue = DispatchQueue(label: "cambridge.video", qos: .userInteractive)
    private let videoOutput = AVCaptureVideoDataOutput()
    private var videoInput: AVCaptureDeviceInput?
    private let encoder = JPEGEncoder()
    private let videoEncoder = VideoEncoder()
    private let audioOutput = AVCaptureAudioDataOutput()
    private var audioInput: AVCaptureDeviceInput?
    private let audioQueue = DispatchQueue(label: "cambridge.audio")
    private var adaptive = true
    private var adaptiveScale = 1.0
    private var lastCongestion = Date.distantPast
    private var streamCodecForFrames: StreamCodec = .h264
    private var started = false
    private var fpsTimer: Timer?
    private var savedBrightness: CGFloat = 0.5
    private var currentVideoOrientation: AVCaptureVideoOrientation = .landscapeRight
    private var mirrorForSession = false
    private var lowLightForSession = true
    private var stabilizationForSession = false
    private var liveTimer: Timer?
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
        stabilizationForSession = stabilization
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
        server.onNeedKeyframe = { [weak self] in self?.videoEncoder.requestKeyframe() }
        server.onCongestion = { [weak self] in
            DispatchQueue.main.async { self?.congested() }
        }
        videoEncoder.onFallback = { [weak self] _ in self?.codec = .h264 }
        videoEncoder.onFailure = { [weak self] message in
            guard let self = self else { return }
            self.videoNote = message + " — switched to JPEG"
            self.codec = .jpeg
        }
        server.start()
        wifi.start(server: server)

        fpsTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.fps = self.server.takeSentFrameCount()
            self.readLiveValues()
            self.recoverBitrate()
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
        if let v = c["torchLevel"] as? Double { torchLevel = min(max(Float(v), 0.05), 1.0) }
        if let v = c["focusManual"] as? Bool { focusManual = v && manualFocusSupported }
        if let v = c["lensPosition"] as? Double { lensPosition = min(max(Float(v), 0), 1) }
        if let v = c["exposureManual"] as? Bool { exposureManual = v && manualExposureSupported }
        if let v = c["iso"] as? Double { iso = min(max(Float(v), isoRange.lowerBound), isoRange.upperBound) }
        if let v = c["shutter"] as? Double { shutter = min(max(v, shutterRange.lowerBound), shutterRange.upperBound) }
        if let v = c["whiteBalanceManual"] as? Bool { whiteBalanceManual = v && manualWhiteBalanceSupported }
        if let v = c["temperature"] as? Double { temperature = min(max(Float(v), 2000), 10000) }
        if let v = c["tint"] as? Double { tint = min(max(Float(v), -150), 150) }
        if let v = c["stabilization"] as? Bool { stabilization = v && stabilizationSupported }
        if let v = c["hdr"] as? Bool { hdr = v && hdrSupported }
        if c["resetPro"] != nil {
            focusManual = false; exposureManual = false; whiteBalanceManual = false; exposureBias = 0
        }
        if let v = c["mirror"] as? Bool { mirror = v }
        if let v = c["screenOff"] as? Bool { screenOff = v }
        if c["focusCenter"] != nil { focus(at: CGPoint(x: 0.5, y: 0.5)) }
        if c["getInfo"] != nil { sendInfo() }
        if let v = c["codec"] as? String, let cd = StreamCodec(rawValue: v) { codec = cd }
        if let v = c["bitrate"] as? String, let b = BitrateLevel(rawValue: v) { bitrateLevel = b }
        if let v = c["lowLight"] as? Bool { lowLight = v }
        if let v = c["adaptive"] as? Bool { adaptive = v; if !v { adaptiveScale = 1.0; updateEncoder() } }
        if let v = c["audio"] as? Bool { setAudio(v) }
        if c["keyframe"] != nil { videoEncoder.requestKeyframe() }
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

        if !session.outputs.contains(metadataOutput), session.canAddOutput(metadataOutput) {
            session.addOutput(metadataOutput)
            metadataOutput.setMetadataObjectsDelegate(self, queue: .main)
        }
        applyConnectionSettings()
        session.commitConfiguration()
        if locked { device.unlockForConfiguration() }
        DispatchQueue.main.async { self.applyScanning() }

        let maxZ = min(device.activeFormat.videoMaxZoomFactor, CameraModel.zoomLimit)
        let hasTorch = device.hasTorch
        let format = device.activeFormat
        let isoLow = format.minISO, isoHigh = format.maxISO
        let shutterLow = max(CMTimeGetSeconds(format.minExposureDuration), 1.0 / 10000.0)
        let shutterHigh = min(CMTimeGetSeconds(format.maxExposureDuration), 1.0 / Double(chosenRate))
        let canFocus = device.isLockingFocusWithCustomLensPositionSupported && device.isFocusModeSupported(.locked)
        let canExpose = device.isExposureModeSupported(.custom)
        let canWB = device.isLockingWhiteBalanceWithCustomDeviceGainsSupported
        let canHDR = format.isVideoHDRSupported
        let canStabilize = videoOutput.connection(with: .video)?.isVideoStabilizationSupported ?? false

        if !session.isRunning {
            session.startRunning()
        }

        DispatchQueue.main.async {
            self.actualFrameRate = chosenRate
            self.maxZoom = max(1.0, maxZ)
            if isoLow < isoHigh { self.isoRange = isoLow...isoHigh }
            if shutterLow < shutterHigh { self.shutterRange = shutterLow...shutterHigh }
            self.manualFocusSupported = canFocus
            self.manualExposureSupported = canExpose
            self.manualWhiteBalanceSupported = canWB
            self.hdrSupported = canHDR
            self.stabilizationSupported = canStabilize
            if !canFocus { self.focusManual = false }
            if !canExpose { self.exposureManual = false }
            if !canWB { self.whiteBalanceManual = false }
            if !canHDR { self.hdr = false }
            self.torchAvailable = hasTorch
            if !hasTorch { self.torchOn = false }
            if self.zoom > self.maxZoom { self.zoom = 1.0 }
            // A new camera starts at its defaults: re-apply the current adjustments.
            self.applyZoom()
            self.applyExposure()
            self.applyFocus()
            self.applyWhiteBalance()
            self.applyHDR()
            self.applyTorch()
            self.applyLowLight()
            self.updateEncoder()
            self.videoEncoder.requestKeyframe()
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
            device.automaticallyEnablesLowLightBoostWhenAvailable = lowLightForSession
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
            // Off = lowest delay. Standard smooths shaky video but adds a little delay.
            conn.preferredVideoStabilizationMode = stabilizationForSession ? .standard : .off
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
        let manual = exposureManual, wantedISO = iso, wantedShutter = shutter, bias = exposureBias
        withDevice { device in
            if manual, device.isExposureModeSupported(.custom) {
                let format = device.activeFormat
                let isoValue = min(max(wantedISO, format.minISO), format.maxISO)
                let longest = min(CMTimeGetSeconds(format.maxExposureDuration),
                                  CMTimeGetSeconds(device.activeVideoMaxFrameDuration))
                let shortest = CMTimeGetSeconds(format.minExposureDuration)
                let seconds = min(max(wantedShutter, shortest), max(shortest, longest))
                device.setExposureModeCustom(duration: CMTime(seconds: seconds, preferredTimescale: 1_000_000),
                                             iso: isoValue, completionHandler: nil)
            } else {
                if device.isExposureModeSupported(.continuousAutoExposure) {
                    device.exposureMode = .continuousAutoExposure
                }
                let value = min(max(bias, device.minExposureTargetBias), device.maxExposureTargetBias)
                device.setExposureTargetBias(value, completionHandler: nil)
            }
        }
    }

    private func applyFocus() {
        let manual = focusManual, position = lensPosition
        withDevice { device in
            if manual, device.isLockingFocusWithCustomLensPositionSupported, device.isFocusModeSupported(.locked) {
                device.setFocusModeLocked(lensPosition: min(max(position, 0), 1), completionHandler: nil)
            } else if device.isFocusModeSupported(.continuousAutoFocus) {
                device.focusMode = .continuousAutoFocus
            }
        }
    }

    private func applyWhiteBalance() {
        let manual = whiteBalanceManual, temp = temperature, tintValue = tint
        withDevice { device in
            if manual, device.isLockingWhiteBalanceWithCustomDeviceGainsSupported {
                let values = AVCaptureDevice.WhiteBalanceTemperatureAndTintValues(temperature: temp, tint: tintValue)
                var gains = device.deviceWhiteBalanceGains(for: values)
                let top = device.maxWhiteBalanceGain
                gains.redGain = min(max(gains.redGain, 1), top)
                gains.greenGain = min(max(gains.greenGain, 1), top)
                gains.blueGain = min(max(gains.blueGain, 1), top)
                device.setWhiteBalanceModeLocked(with: gains, completionHandler: nil)
            } else if device.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                device.whiteBalanceMode = .continuousAutoWhiteBalance
            }
        }
    }

    private func applyHDR() {
        let on = hdr
        withDevice { device in
            guard device.activeFormat.isVideoHDRSupported else { return }
            device.automaticallyAdjustsVideoHDREnabled = false
            device.isVideoHDREnabled = on
        }
    }

    private func applyTorch() {
        let on = torchOn, level = torchLevel
        withDevice { device in
            guard device.hasTorch, device.isTorchAvailable else { return }
            if on {
                try? device.setTorchModeOn(level: min(max(level, 0.05), 1.0))
            } else if device.torchMode != .off {
                device.torchMode = .off
            }
        }
    }

    private func applyLowLight() {
        lowLightForSession = lowLight
        let on = lowLight
        withDevice { device in
            if device.isLowLightBoostSupported {
                device.automaticallyEnablesLowLightBoostWhenAvailable = on
            }
        }
    }

    // MARK: - Video encoding settings

    /// Bits per second for the current size / frame rate / level.
    private func targetBitrate() -> Int {
        let h = lastHeight > 0 ? min(lastWidth, lastHeight) : Int(quality.size.height)
        let base: Double = h <= 720 ? 6_000_000 : (h <= 1080 ? 12_000_000 : 35_000_000)
        var rate = base * (actualFrameRate > 30 ? 1.5 : 1.0) * bitrateLevel.factor
        if codec == .hevc { rate *= 0.7 }
        if adaptive { rate *= adaptiveScale }
        return Int(rate)
    }

    private func updateEncoder() {
        streamCodecForFrames = codec
        guard codec != .jpeg else { return }
        videoEncoder.configure(codec: codec == .hevc ? .hevc : .h264, fps: actualFrameRate, bitrate: targetBitrate())
    }

    /// "Keep it smooth": the link is busy, so send less detail for a while.
    private func congested() {
        guard adaptive else { return }
        lastCongestion = Date()
        adaptiveScale = max(0.3, adaptiveScale * 0.75)
        updateEncoder()
    }

    private func recoverBitrate() {
        guard adaptive, adaptiveScale < 1.0, Date().timeIntervalSince(lastCongestion) > 3 else { return }
        adaptiveScale = min(1.0, adaptiveScale * 1.1)
        updateEncoder()
    }

    // MARK: - Microphone (only when the PC asks for it)

    private func setAudio(_ on: Bool) {
        guard on != audioEnabled else { return }
        if on {
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized:
                break
            case .notDetermined:
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    DispatchQueue.main.async {
                        if granted { self.setAudio(true) } else { self.micDenied = true; self.sendInfo() }
                    }
                }
                return
            default:
                micDenied = true
                sendInfo()
                return
            }
        }
        audioEnabled = on
        sessionQueue.async {
            self.session.beginConfiguration()
            if on {
                if self.audioInput == nil, let mic = AVCaptureDevice.default(for: .audio),
                   let input = try? AVCaptureDeviceInput(device: mic), self.session.canAddInput(input) {
                    self.session.addInput(input)
                    self.audioInput = input
                }
                if !self.session.outputs.contains(self.audioOutput), self.session.canAddOutput(self.audioOutput) {
                    self.audioOutput.setSampleBufferDelegate(self, queue: self.audioQueue)
                    self.session.addOutput(self.audioOutput)
                }
            } else {
                if let input = self.audioInput {
                    self.session.removeInput(input)
                    self.audioInput = nil
                }
                if self.session.outputs.contains(self.audioOutput) {
                    self.session.removeOutput(self.audioOutput)
                }
            }
            self.session.commitConfiguration()
        }
        sendInfo()
    }

    /// Microphone samples -> 16-bit PCM message for the PC.
    fileprivate func handleAudio(_ sampleBuffer: CMSampleBuffer) {
        guard let fd = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fd),
              let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }
        let asbd = asbdPtr.pointee
        let length = CMBlockBufferGetDataLength(block)
        guard length > 0 else { return }
        var raw = [UInt8](repeating: 0, count: length)
        guard CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: &raw) == kCMBlockBufferNoErr else { return }
        let isFloat = (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let nonInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
        var channels = Int(max(1, asbd.mChannelsPerFrame))
        var pcm = Data()
        if isFloat && asbd.mBitsPerChannel == 32 {
            let count = length / 4
            let usable = nonInterleaved ? count / channels : count
            if nonInterleaved { channels = 1 }
            pcm.reserveCapacity(usable * 2)
            raw.withUnsafeBytes { buf in
                let f = buf.bindMemory(to: Float32.self)
                for i in 0..<usable {
                    let v = Int16(max(-1, min(1, f[i])) * 32767)
                    withUnsafeBytes(of: v.littleEndian) { pcm.append(contentsOf: $0) }
                }
            }
        } else if asbd.mBitsPerChannel == 16 {
            if nonInterleaved && channels > 1 {
                pcm = Data(raw[0..<(length / channels)])
                channels = 1
            } else {
                pcm = Data(raw)
            }
        } else {
            return
        }
        var payload = Data()
        let rate = UInt32(asbd.mSampleRate)
        payload.append(contentsOf: [UInt8(rate >> 24 & 0xFF), UInt8(rate >> 16 & 0xFF), UInt8(rate >> 8 & 0xFF), UInt8(rate & 0xFF)])
        payload.append(UInt8(min(2, channels)))
        payload.append(pcm)
        server.sendAudio(payload)
    }

    /// Reads what the camera is doing right now (ISO, shutter, focus, colour) for display.
    private func readLiveValues() {
        guard isConnected || !screenOff else { return }
        sessionQueue.async {
            guard let device = self.videoInput?.device else { return }
            let isoNow = device.iso
            let shutterNow = CMTimeGetSeconds(device.exposureDuration)
            let lensNow = device.lensPosition
            var tempNow: Float = 0, tintNow: Float = 0
            let gains = device.deviceWhiteBalanceGains
            if gains.redGain >= 1, gains.greenGain >= 1, gains.blueGain >= 1 {
                let tt = device.temperatureAndTintValues(for: gains)
                tempNow = tt.temperature
                tintNow = tt.tint
            }
            DispatchQueue.main.async {
                self.liveISO = isoNow
                self.liveShutter = shutterNow.isFinite ? shutterNow : 0
                self.liveLens = lensNow
                self.liveTemperature = tempNow
                self.liveTint = tintNow
                if self.isConnected { self.sendInfo() }
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

    // MARK: - QR code scanning (Wi-Fi pairing)

    private func applyScanning() {
        let wanted = scanning
        sessionQueue.async {
            let types: [AVMetadataObject.ObjectType] =
                wanted && self.metadataOutput.availableMetadataObjectTypes.contains(.qr) ? [.qr] : []
            self.metadataOutput.metadataObjectTypes = types
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
            "app": "CamBridge",
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
            "torchLevel": Double(torchLevel),
            "focusManual": focusManual,
            "lensPosition": Double(lensPosition),
            "manualFocusSupported": manualFocusSupported,
            "exposureManual": exposureManual,
            "iso": Double(iso),
            "shutter": shutter,
            "isoMin": Double(isoRange.lowerBound),
            "isoMax": Double(isoRange.upperBound),
            "shutterMin": shutterRange.lowerBound,
            "shutterMax": shutterRange.upperBound,
            "manualExposureSupported": manualExposureSupported,
            "whiteBalanceManual": whiteBalanceManual,
            "temperature": Double(temperature),
            "tint": Double(tint),
            "manualWhiteBalanceSupported": manualWhiteBalanceSupported,
            "stabilization": stabilization,
            "stabilizationSupported": stabilizationSupported,
            "hdr": hdr,
            "hdrSupported": hdrSupported,
            "liveISO": Double(liveISO),
            "liveShutter": liveShutter,
            "liveLens": Double(liveLens),
            "liveTemperature": Double(liveTemperature),
            "liveTint": Double(liveTint),
            "codec": codec.rawValue,
            "codecs": StreamCodec.allCases.map { $0.rawValue },
            "bitrate": bitrateLevel.rawValue,
            "bitrateMbps": Double(targetBitrate()) / 1_000_000,
            "lowLight": lowLight,
            "audio": audioEnabled,
            "micDenied": micDenied,
            "videoNote": videoNote,
            "encodedFrames": videoEncoder.encodedFrames,
            "encoderError": videoEncoder.lastError,
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
        d.set(stabilization, forKey: "stabilization")
        d.set(codec.rawValue, forKey: "codec")
        d.set(bitrateLevel.rawValue, forKey: "bitrate")
        d.set(lowLight, forKey: "lowLight")
        d.set(hdr, forKey: "hdr")
    }

    private func load() {
        let d = UserDefaults.standard
        if let v = d.string(forKey: "side"), let s = CameraSide(rawValue: v) { side = s }
        if let v = d.string(forKey: "lens"), let l = Lens(rawValue: v) { lens = l }
        if let v = d.string(forKey: "quality"), let q = VideoQuality(rawValue: v) { quality = q }
        if let f = FrameRate(rawValue: d.integer(forKey: "fps")) { frameRate = f }
        if let v = d.string(forKey: "orientation"), let o = OrientationMode(rawValue: v) { orientationMode = o }
        mirror = d.bool(forKey: "mirror")
        stabilization = d.bool(forKey: "stabilization")
        if let v = d.string(forKey: "codec"), let c = StreamCodec(rawValue: v) { codec = c }
        if let v = d.string(forKey: "bitrate"), let b = BitrateLevel(rawValue: v) { bitrateLevel = b }
        if d.object(forKey: "lowLight") != nil { lowLight = d.bool(forKey: "lowLight") }
        hdr = d.bool(forKey: "hdr")
    }
}

// MARK: - QR codes

extension CameraModel: AVCaptureMetadataOutputObjectsDelegate {
    func metadataOutput(_ output: AVCaptureMetadataOutput,
                        didOutput metadataObjects: [AVMetadataObject],
                        from connection: AVCaptureConnection) {
        guard scanning else { return }
        for object in metadataObjects {
            guard let code = object as? AVMetadataMachineReadableCodeObject,
                  let text = code.stringValue, let url = URL(string: text),
                  url.scheme == "cambridge" else { continue }
            scanning = false
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            wifi.handle(url: url)
            return
        }
    }
}

// MARK: - Frames

extension CameraModel: AVCaptureVideoDataOutputSampleBufferDelegate, AVCaptureAudioDataOutputSampleBufferDelegate {
    func captureOutput(_ output: AVCaptureOutput,
                       didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        if output === audioOutput {
            if server.isConnected { handleAudio(sampleBuffer) }
            return
        }
        // Video (H.264 / HEVC): every frame is encoded; the link skips ahead if it falls behind.
        if streamCodecForFrames != .jpeg {
            guard server.isConnected, let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
            let w = CVPixelBufferGetWidth(pixelBuffer)
            let h = CVPixelBufferGetHeight(pixelBuffer)
            if w != lastWidth || h != lastHeight {
                lastWidth = w
                lastHeight = h
                DispatchQueue.main.async { self.updateEncoder() }
                sendInfo()
            }
            if ProcessInfo.processInfo.thermalState == .critical {
                skipToggle.toggle()
                if skipToggle { return }
            }
            let hevc = streamCodecForFrames == .hevc
            videoEncoder.encode(pixelBuffer, pts: CMSampleBufferGetPresentationTimeStamp(sampleBuffer)) { [weak self] data, key in
                self?.server.sendVideo(data, hevc: hevc, keyframe: key)
            }
            return
        }
        // JPEG: nothing to do if the PC isn't connected or is still receiving the last frame.
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
