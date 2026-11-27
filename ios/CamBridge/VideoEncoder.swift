import CoreMedia
import Foundation
import VideoToolbox

/// Hardware H.264 / HEVC encoder (the iPhone's video chip), tuned for live video:
/// realtime, no frame reordering (lowest delay), keyframe on request.
/// Output: 1 flag byte (bit 0 = keyframe) + Annex-B NAL units (parameter sets before keyframes).
final class VideoEncoder {
    enum Codec: String { case h264, hevc }

    private(set) var codec: Codec = .h264
    private var session: VTCompressionSession?
    private var width: Int32 = 0
    private var height: Int32 = 0
    private var fps = 30
    private var bitrate = 12_000_000
    private let lock = NSLock()
    private var forceKey = true
    /// Called when HEVC isn't available and the encoder switched to H.264 by itself.
    var onFallback: ((Codec) -> Void)?
    private var useLowLatency = false
    private var failures = 0
    /// Which set of settings is being tried (0 = full settings). All stages keep the same
    /// size, frame rate and bitrate; later stages leave out optional fine-tuning that some
    /// iPhones reject.
    private(set) var stage = 0
    static let stageCount = 3
    private(set) var lastError = ""
    private(set) var rejected: [String] = []   // settings the video chip refused
    private(set) var encodeCalls = 0
    private(set) var encodedFrames = 0

    deinit { invalidate() }

    func configure(codec: Codec, fps: Int, bitrate: Int) {
        lock.lock()
        defer { lock.unlock() }
        if codec != self.codec || fps != self.fps {
            self.codec = codec
            self.fps = fps
            invalidateLocked()
        }
        if bitrate != self.bitrate {
            self.bitrate = bitrate
            if let s = session { applyBitrate(s) }
        }
    }

    func requestKeyframe() {
        lock.lock()
        forceKey = true
        lock.unlock()
    }

    func invalidate() {
        lock.lock()
        invalidateLocked()
        lock.unlock()
    }

    private func invalidateLocked() {
        if let s = session {
            VTCompressionSessionCompleteFrames(s, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(s)
        }
        session = nil
        forceKey = true
    }

    // MARK: - Session

    private func applyBitrate(_ s: VTCompressionSession) {
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AverageBitRate, value: NSNumber(value: bitrate))
        if stage < 2 {
            let bytesPerSecond = Double(bitrate) * 1.5 / 8.0
            VTSessionSetProperty(s, key: kVTCompressionPropertyKey_DataRateLimits,
                                 value: [NSNumber(value: bytesPerSecond), NSNumber(value: 1.0)] as CFArray)
        }
    }

    private func makeSession(width w: Int32, height h: Int32, codec c: Codec) -> VTCompressionSession? {
        let type = c == .hevc ? kCMVideoCodecType_HEVC : kCMVideoCodecType_H264
        var spec: CFDictionary?
        if c == .h264 && useLowLatency {
            spec = [kVTVideoEncoderSpecification_EnableLowLatencyRateControl as String: NSNumber(value: true)] as CFDictionary
        }
        var s: VTCompressionSession?
        var status = VTCompressionSessionCreate(allocator: kCFAllocatorDefault, width: w, height: h,
                                                codecType: type, encoderSpecification: spec,
                                                imageBufferAttributes: nil, compressedDataAllocator: nil,
                                                outputCallback: nil, refcon: nil, compressionSessionOut: &s)
        if status != noErr, spec != nil {
            status = VTCompressionSessionCreate(allocator: kCFAllocatorDefault, width: w, height: h,
                                                codecType: type, encoderSpecification: nil,
                                                imageBufferAttributes: nil, compressedDataAllocator: nil,
                                                outputCallback: nil, refcon: nil, compressionSessionOut: &s)
        }
        guard status == noErr, let session = s else {
            lastError = "create \(status)"
            return nil
        }
        rejected = []
        func set(_ name: String, _ key: CFString, _ value: CFTypeRef) {
            let st = VTSessionSetProperty(session, key: key, value: value)
            if st != noErr { rejected.append("\(name) \(st)") }
        }
        set("realtime", kVTCompressionPropertyKey_RealTime, kCFBooleanTrue)
        set("reorder", kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse)
        if stage == 0 {
            set("profile", kVTCompressionPropertyKey_ProfileLevel,
                c == .hevc ? kVTProfileLevel_HEVC_Main_AutoLevel : kVTProfileLevel_H264_High_AutoLevel)
        }
        set("fps", kVTCompressionPropertyKey_ExpectedFrameRate, NSNumber(value: fps))
        set("keyint", kVTCompressionPropertyKey_MaxKeyFrameInterval, NSNumber(value: fps * 2))
        set("keydur", kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, NSNumber(value: 2.0))
        set("bitrate", kVTCompressionPropertyKey_AverageBitRate, NSNumber(value: bitrate))
        if stage < 2 {
            let bytesPerSecond = Double(bitrate) * 1.5 / 8.0
            set("limits", kVTCompressionPropertyKey_DataRateLimits,
                [NSNumber(value: bytesPerSecond), NSNumber(value: 1.0)] as CFArray)
        }
        let prep = VTCompressionSessionPrepareToEncodeFrames(session)
        if prep != noErr { rejected.append("prepare \(prep)") }
        return session
    }

    // MARK: - Encoding

    func encode(_ pixelBuffer: CVPixelBuffer, pts: CMTime, output: @escaping (Data, Bool) -> Void) {
        let w = Int32(CVPixelBufferGetWidth(pixelBuffer))
        let h = Int32(CVPixelBufferGetHeight(pixelBuffer))
        lock.lock()
        if session == nil || w != width || h != height {
            invalidateLocked()
            var s = makeSession(width: w, height: h, codec: codec)
            if s == nil, codec == .hevc {
                codec = .h264
                s = makeSession(width: w, height: h, codec: .h264)
                let cb = onFallback
                DispatchQueue.main.async { cb?(.h264) }
            }
            session = s
            width = w
            height = h
            forceKey = true
        }
        guard let s = session else {
            lock.unlock()
            noteFailure()
            return
        }
        var props: CFDictionary?
        if forceKey {
            props = [kVTEncodeFrameOptionKey_ForceKeyFrame as String: NSNumber(value: true)] as CFDictionary
            forceKey = false
        }
        let hevc = codec == .hevc
        encodeCalls += 1
        lock.unlock()

        let st = VTCompressionSessionEncodeFrame(s, imageBuffer: pixelBuffer, presentationTimeStamp: pts,
                                                 duration: .invalid, frameProperties: props, infoFlagsOut: nil) {
            [weak self] status, _, sampleBuffer in
            guard let self = self else { return }
            if status != noErr {
                self.lastError = "encode \(status)"
                self.noteFailure()
                return
            }
            guard let sb = sampleBuffer else { return }  // frame skipped by the encoder
            guard let result = VideoEncoder.annexB(sb, hevc: hevc) else {
                self.requestKeyframe()
                return
            }
            self.lock.lock()
            self.failures = 0
            self.encodedFrames += 1
            self.lock.unlock()
            output(result.0, result.1)
        }
        if st != noErr {
            lastError = "encode \(st)"
            noteFailure()
        }
    }

    /// Failures in a row: start a fresh session with the next set of settings.
    /// Never lowers quality and never switches to JPEG — the error is reported to the PC.
    private func noteFailure() {
        lock.lock()
        failures += 1
        if failures % 10 == 0 {
            stage = min(stage + 1, VideoEncoder.stageCount - 1)
            invalidateLocked()
        }
        forceKey = true
        lock.unlock()
    }

    /// Lets the full settings be tried again (when the user changes the format).
    func resetFailures() {
        lock.lock()
        failures = 0
        stage = 0
        lastError = ""
        invalidateLocked()
        lock.unlock()
    }

    /// One line for the PC's screen, e.g. "in 2, out 0, stage 2/3, error encode -12902".
    func diagnostics() -> String {
        lock.lock()
        defer { lock.unlock() }
        var parts = ["stage \(stage + 1)/\(VideoEncoder.stageCount)"]
        if !lastError.isEmpty { parts.append("error " + lastError) }
        if !rejected.isEmpty { parts.append("refused: " + rejected.joined(separator: ", ")) }
        return parts.joined(separator: " · ")
    }

    private static let startCode: [UInt8] = [0, 0, 0, 1]

    static func annexB(_ sb: CMSampleBuffer, hevc: Bool) -> (Data, Bool)? {
        var key = true
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[CFString: Any]],
           let first = attachments.first, let notSync = first[kCMSampleAttachmentKey_NotSync] as? Bool {
            key = !notSync
        }
        var out = Data()
        out.append(key ? 1 : 0)

        if key, let fd = CMSampleBufferGetFormatDescription(sb) {
            var count = 0
            var nalLen: Int32 = 0
            if hevc {
                _ = CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(fd, parameterSetIndex: 0, parameterSetPointerOut: nil,
                                                                   parameterSetSizeOut: nil, parameterSetCountOut: &count,
                                                                   nalUnitHeaderLengthOut: &nalLen)
            } else {
                _ = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fd, parameterSetIndex: 0, parameterSetPointerOut: nil,
                                                                   parameterSetSizeOut: nil, parameterSetCountOut: &count,
                                                                   nalUnitHeaderLengthOut: &nalLen)
            }
            for i in 0..<count {
                var ptr: UnsafePointer<UInt8>?
                var size = 0
                let st: OSStatus
                if hevc {
                    st = CMVideoFormatDescriptionGetHEVCParameterSetAtIndex(fd, parameterSetIndex: i, parameterSetPointerOut: &ptr,
                                                                           parameterSetSizeOut: &size, parameterSetCountOut: nil,
                                                                           nalUnitHeaderLengthOut: nil)
                } else {
                    st = CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fd, parameterSetIndex: i, parameterSetPointerOut: &ptr,
                                                                           parameterSetSizeOut: &size, parameterSetCountOut: nil,
                                                                           nalUnitHeaderLengthOut: nil)
                }
                if st == noErr, let p = ptr, size > 0 {
                    out.append(contentsOf: startCode)
                    out.append(p, count: size)
                }
            }
        }

        guard let block = CMSampleBufferGetDataBuffer(sb) else { return nil }
        let length = CMBlockBufferGetDataLength(block)
        guard length > 4 else { return nil }
        var avcc = [UInt8](repeating: 0, count: length)
        guard CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: &avcc) == kCMBlockBufferNoErr else {
            return nil
        }
        // Length-prefixed NAL units (4-byte big-endian) -> start codes
        var offset = 0
        while offset + 4 <= length {
            let nalSize = Int(avcc[offset]) << 24 | Int(avcc[offset + 1]) << 16 | Int(avcc[offset + 2]) << 8 | Int(avcc[offset + 3])
            offset += 4
            guard nalSize > 0, offset + nalSize <= length else { break }
            out.append(contentsOf: startCode)
            out.append(contentsOf: avcc[offset..<(offset + nalSize)])
            offset += nalSize
        }
        return (out, key)
    }
}
