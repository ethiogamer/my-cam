import Foundation
import Network

/// Listens on the phone's loopback port. The PC reaches it through the USB cable
/// using Apple's USB channel (usbmuxd) — no Wi-Fi, no hotspot.
///
/// Wire format (phone -> PC), every message:
///   4 bytes  magic "CBG1"
///   4 bytes  type   (UInt32, big-endian)  1 = JPEG frame, 2 = JSON info, 3 = JSON command (PC -> phone)
///   4 bytes  length (UInt32, big-endian)
///   N bytes  payload
final class StreamServer {
    static let port: UInt16 = 7878

    enum MessageType: UInt32 {
        case jpeg = 1
        case info = 2
        case command = 3
        case hello = 5
        case welcome = 6
        case h264 = 7
        case hevc = 8
        case audio = 9
    }

    /// Video: the link fell behind and frames were dropped; the encoder must send a keyframe.
    var onNeedKeyframe: (() -> Void)?
    /// Video: the link is congested (for "keep it smooth" bitrate lowering).
    var onCongestion: (() -> Void)?
    private var videoInFlight = 0
    private var videoDropping = false
    static let maxInFlightBytes = 6_000_000

    /// Called (on any thread) when the PC connects or disconnects.
    var onConnectionChange: ((Bool) -> Void)?
    /// Called (on any thread) right after a PC connects, so we can send info.
    var onNewConnection: (() -> Void)?
    /// Called (on any thread) when the PC sends a command, e.g. ["zoom": 2.0].
    var onCommand: (([String: Any]) -> Void)?

    /// Wi-Fi: the PC answered our hello ("waiting", "ok", "denied", "busy").
    var onWelcome: ((String, [String: Any]) -> Void)?
    /// Wi-Fi: our outgoing connection closed. Bool = it had been accepted.
    var onOutgoingClosed: ((Bool) -> Void)?

    private var inbox = Data()
    private var outgoing = false
    private var outgoingAccepted = false

    private let queue = DispatchQueue(label: "cambridge.server")
    private var listener: NWListener?
    private var connection: NWConnection?

    private let lock = NSLock()
    private var _connected = false
    private var _sending = false
    private var _sentFrames = 0

    var isConnected: Bool { lock.lock(); defer { lock.unlock() }; return _connected }
    /// True when a PC is connected and the previous frame has finished sending.
    var isReadyForFrame: Bool { lock.lock(); defer { lock.unlock() }; return _connected && !_sending }

    /// Returns frames sent since the last call.
    func takeSentFrameCount() -> Int {
        lock.lock(); defer { lock.unlock() }
        let n = _sentFrames
        _sentFrames = 0
        return n
    }

    // MARK: - Listener

    func start() {
        queue.async { self.startListener() }
    }

    func stop() {
        queue.async {
            self.listener?.cancel()
            self.listener = nil
            self.dropConnection()
        }
    }

    private func startListener() {
        guard listener == nil else { return }
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = NWEndpoint.hostPort(
            host: .ipv4(.loopback),
            port: NWEndpoint.Port(rawValue: StreamServer.port)!
        )
        do {
            let l = try NWListener(using: params)
            l.newConnectionHandler = { [weak self] conn in
                self?.accept(conn)
            }
            l.stateUpdateHandler = { [weak self] state in
                guard let self = self else { return }
                switch state {
                case .failed, .cancelled:
                    self.queue.async {
                        if self.listener === l { self.listener = nil }
                    }
                    if case .failed = state {
                        self.queue.asyncAfter(deadline: .now() + 1) { self.startListener() }
                    }
                default:
                    break
                }
            }
            listener = l
            l.start(queue: queue)
        } catch {
            queue.asyncAfter(deadline: .now() + 1) { self.startListener() }
        }
    }

    /// Wi-Fi: connect out to the PC and say hello. Frames start once the PC answers "ok".
    func connectOut(to endpoint: NWEndpoint, hello: [String: Any]) {
        queue.async {
            self.dropConnection()
            let tcp = NWProtocolTCP.Options()
            tcp.noDelay = true
            tcp.connectionTimeout = 6
            let conn = NWConnection(to: endpoint, using: NWParameters(tls: nil, tcp: tcp))
            self.connection = conn
            self.inbox = Data()
            self.outgoing = true
            self.outgoingAccepted = false
            conn.stateUpdateHandler = { [weak self, weak conn] state in
                guard let self = self, let conn = conn else { return }
                switch state {
                case .ready:
                    if let data = try? JSONSerialization.data(withJSONObject: hello) {
                        self.sendRaw(conn, type: .hello, payload: data)
                    }
                    self.receiveLoop(conn)
                case .waiting:
                    conn.cancel()  // can't reach this address
                case .failed, .cancelled:
                    self.queue.async {
                        if self.connection === conn { self.dropConnection() }
                    }
                default:
                    break
                }
            }
            conn.start(queue: self.queue)
        }
    }

    /// Wi-Fi: hang up on purpose.
    func disconnectOut() {
        queue.async {
            if self.outgoing { self.dropConnection() }
        }
    }

    private func sendRaw(_ conn: NWConnection, type: MessageType, payload: Data) {
        var message = Data(capacity: 12 + payload.count)
        message.append(contentsOf: Array("CBG1".utf8))
        message.append(StreamServer.bigEndianBytes(type.rawValue))
        message.append(StreamServer.bigEndianBytes(UInt32(payload.count)))
        message.append(payload)
        conn.send(content: message, completion: .contentProcessed { _ in })
    }

    private func accept(_ conn: NWConnection) {
        // Only one PC at a time: the newest connection wins (a USB cable beats Wi-Fi).
        dropConnection()
        connection = conn
        inbox = Data()
        conn.stateUpdateHandler = { [weak self, weak conn] state in
            guard let self = self, let conn = conn else { return }
            switch state {
            case .ready:
                self.setConnected(true)
                self.onNewConnection?()
                self.receiveLoop(conn)
            case .failed, .cancelled:
                self.queue.async {
                    if self.connection === conn { self.dropConnection() }
                }
            default:
                break
            }
        }
        conn.start(queue: queue)
    }

    /// Reads commands from the PC, and notices when it disconnects.
    private func receiveLoop(_ conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            guard let self = self else { return }
            if let data = data, !data.isEmpty, self.connection === conn {
                self.inbox.append(data)
                if !self.parseInbox() {
                    self.dropConnection()
                    return
                }
            }
            if isComplete || error != nil {
                if self.connection === conn { self.dropConnection() }
            } else {
                self.receiveLoop(conn)
            }
        }
    }

    /// Returns false if the PC sent garbage.
    private func parseInbox() -> Bool {
        while inbox.count >= 12 {
            let header = [UInt8](inbox.prefix(12))
            guard header[0] == 0x43, header[1] == 0x42, header[2] == 0x47, header[3] == 0x31 else { return false }
            let type = UInt32(header[4]) << 24 | UInt32(header[5]) << 16 | UInt32(header[6]) << 8 | UInt32(header[7])
            let length = Int(UInt32(header[8]) << 24 | UInt32(header[9]) << 16 | UInt32(header[10]) << 8 | UInt32(header[11]))
            guard length <= 1_000_000 else { return false }
            guard inbox.count >= 12 + length else { break }
            let payload = Data(inbox.dropFirst(12).prefix(length))
            inbox = Data(inbox.dropFirst(12 + length))
            guard let object = try? JSONSerialization.jsonObject(with: payload),
                  let dict = object as? [String: Any] else { continue }
            if type == MessageType.command.rawValue {
                onCommand?(dict)
            } else if type == MessageType.welcome.rawValue, outgoing {
                let status = dict["status"] as? String ?? ""
                if status == "ok" {
                    outgoingAccepted = true
                    setConnected(true)
                    onNewConnection?()
                }
                onWelcome?(status, dict)
                if status == "denied" || status == "busy" { return false }
            }
        }
        return true
    }

    private func dropConnection() {
        let wasOutgoing = outgoing, wasAccepted = outgoingAccepted
        outgoing = false
        outgoingAccepted = false
        connection?.stateUpdateHandler = nil
        connection?.cancel()
        connection = nil
        videoInFlight = 0
        videoDropping = false
        if wasOutgoing { onOutgoingClosed?(wasAccepted) }
        lock.lock()
        _sending = false
        lock.unlock()
        setConnected(false)
    }

    private func setConnected(_ value: Bool) {
        lock.lock()
        let changed = _connected != value
        _connected = value
        lock.unlock()
        if changed { onConnectionChange?(value) }
    }

    // MARK: - Sending

    func sendFrame(_ jpeg: Data) {
        send(type: .jpeg, payload: jpeg, isFrame: true)
    }

    /// Video frames must arrive in order; when the link can't keep up we skip ahead to
    /// the next keyframe instead of building up delay.
    func sendVideo(_ payload: Data, hevc: Bool, keyframe: Bool) {
        queue.async {
            guard let conn = self.connection, self.isConnected else { return }
            if self.videoDropping && !keyframe { return }
            if self.videoInFlight > StreamServer.maxInFlightBytes {
                self.videoDropping = true
                self.onNeedKeyframe?()
                self.onCongestion?()
                return
            }
            if keyframe { self.videoDropping = false }
            let size = payload.count + 12
            self.videoInFlight += size
            var message = Data(capacity: size)
            message.append(contentsOf: Array("CBG1".utf8))
            message.append(StreamServer.bigEndianBytes((hevc ? MessageType.hevc : MessageType.h264).rawValue))
            message.append(StreamServer.bigEndianBytes(UInt32(payload.count)))
            message.append(payload)
            conn.send(content: message, completion: .contentProcessed { [weak self] error in
                guard let self = self else { return }
                self.queue.async { self.videoInFlight = max(0, self.videoInFlight - size) }
                if error == nil {
                    self.lock.lock()
                    self._sentFrames += 1
                    self.lock.unlock()
                }
            })
        }
    }

    /// payload: 4-byte sample rate (big-endian), 1-byte channels, 16-bit PCM.
    func sendAudio(_ payload: Data) {
        send(type: .audio, payload: payload, isFrame: false)
    }

    func sendInfo(_ dict: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: dict) else { return }
        send(type: .info, payload: data, isFrame: false)
    }

    private func send(type: MessageType, payload: Data, isFrame: Bool) {
        queue.async {
            guard let conn = self.connection else { return }
            if isFrame {
                self.lock.lock()
                if self._sending || !self._connected {
                    // Previous frame still on the wire: drop this one to keep delay low.
                    self.lock.unlock()
                    return
                }
                self._sending = true
                self.lock.unlock()
            }

            var message = Data(capacity: 12 + payload.count)
            message.append(contentsOf: Array("CBG1".utf8))
            message.append(StreamServer.bigEndianBytes(type.rawValue))
            message.append(StreamServer.bigEndianBytes(UInt32(payload.count)))
            message.append(payload)

            conn.send(content: message, completion: .contentProcessed { [weak self] error in
                guard let self = self else { return }
                if isFrame {
                    self.lock.lock()
                    self._sending = false
                    if error == nil { self._sentFrames += 1 }
                    self.lock.unlock()
                }
                if error != nil {
                    self.queue.async {
                        if self.connection === conn { self.dropConnection() }
                    }
                }
            })
        }
    }

    private static func bigEndianBytes(_ value: UInt32) -> Data {
        var be = value.bigEndian
        return Data(bytes: &be, count: 4)
    }
}
