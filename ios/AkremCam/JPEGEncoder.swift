import Foundation
import CoreImage
import CoreMedia
import ImageIO
import VideoToolbox

/// Turns camera frames into JPEG images.
/// Uses the iPhone's hardware encoder; falls back to Core Image if that is not available.
final class JPEGEncoder {
    /// 0.0 – 1.0
    var quality: Float = 0.8 {
        didSet { if oldValue != quality { invalidate() } }
    }

    private var session: VTCompressionSession?
    private var width: Int32 = 0
    private var height: Int32 = 0
    private var useHardware = true
    private var hardwareFailures = 0
    private lazy var ciContext = CIContext(options: [.cacheIntermediates: false])
    private let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

    deinit { invalidate() }

    func invalidate() {
        if let s = session {
            VTCompressionSessionInvalidate(s)
        }
        session = nil
    }

    /// Calls `completion` exactly once with the JPEG bytes, or nil if the frame was dropped.
    func encode(_ pixelBuffer: CVPixelBuffer, pts: CMTime, completion: @escaping (Data?) -> Void) {
        if useHardware, encodeHardware(pixelBuffer, pts: pts, completion: completion) {
            return
        }
        completion(encodeSoftware(pixelBuffer))
    }

    // MARK: - Hardware (VideoToolbox)

    private func makeSession(width w: Int32, height h: Int32) -> Bool {
        invalidate()
        var newSession: VTCompressionSession?
        let status = VTCompressionSessionCreate(
            allocator: kCFAllocatorDefault,
            width: w,
            height: h,
            codecType: kCMVideoCodecType_JPEG,
            encoderSpecification: nil,
            imageBufferAttributes: nil,
            compressedDataAllocator: nil,
            outputCallback: nil,
            refcon: nil,
            compressionSessionOut: &newSession
        )
        guard status == noErr, let s = newSession else { return false }
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_Quality, value: NSNumber(value: quality))
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTCompressionSessionPrepareToEncodeFrames(s)
        session = s
        width = w
        height = h
        return true
    }

    /// Returns false if the hardware path could not even start (caller then uses software).
    private func encodeHardware(_ pixelBuffer: CVPixelBuffer, pts: CMTime, completion: @escaping (Data?) -> Void) -> Bool {
        let w = Int32(CVPixelBufferGetWidth(pixelBuffer))
        let h = Int32(CVPixelBufferGetHeight(pixelBuffer))
        if session == nil || w != width || h != height {
            if !makeSession(width: w, height: h) {
                useHardware = false
                return false
            }
        }
        guard let s = session else { return false }

        let status = VTCompressionSessionEncodeFrame(
            s,
            imageBuffer: pixelBuffer,
            presentationTimeStamp: pts,
            duration: .invalid,
            frameProperties: nil,
            infoFlagsOut: nil
        ) { [weak self] status, _, sampleBuffer in
            guard status == noErr,
                  let sampleBuffer = sampleBuffer,
                  let data = JPEGEncoder.bytes(of: sampleBuffer),
                  data.count > 4, data[0] == 0xFF, data[1] == 0xD8 else {
                self?.noteHardwareFailure()
                completion(nil)
                return
            }
            self?.hardwareFailures = 0
            completion(data)
        }
        if status != noErr {
            noteHardwareFailure()
            return false
        }
        return true
    }

    private func noteHardwareFailure() {
        hardwareFailures += 1
        if hardwareFailures >= 10 {
            useHardware = false
        }
    }

    private static func bytes(of sampleBuffer: CMSampleBuffer) -> Data? {
        guard let block = CMSampleBufferGetDataBuffer(sampleBuffer) else { return nil }
        let length = CMBlockBufferGetDataLength(block)
        guard length > 0 else { return nil }
        var data = Data(count: length)
        let status = data.withUnsafeMutableBytes { raw -> OSStatus in
            guard let base = raw.baseAddress else { return -1 }
            return CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: base)
        }
        return status == kCMBlockBufferNoErr ? data : nil
    }

    // MARK: - Software (Core Image)

    private func encodeSoftware(_ pixelBuffer: CVPixelBuffer) -> Data? {
        let image = CIImage(cvPixelBuffer: pixelBuffer)
        let key = CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String)
        return ciContext.jpegRepresentation(of: image, colorSpace: sRGB, options: [key: quality])
    }
}
