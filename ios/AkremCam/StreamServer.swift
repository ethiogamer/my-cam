import Foundation
import Network

/// Listens on the phone's loopback port. The PC reaches it through the USB cable
/// using Apple's USB channel (usbmuxd) — no Wi-Fi, no hotspot.
///
/// Wire format (phone -> PC), every message:
///   4 bytes  magic "AKC1"
///   4 bytes  type   (UInt32, big-endian)  1 = JPEG frame, 2 = JSON info
///   4 bytes  length (UInt32, big-endian)
///   N bytes  payload
final class StreamServer {
    static let port: UInt16 = 7878

    enum MessageType: UInt32 {
        case jpeg = 1
        case info = 2
    }

    /// Called (on any thread) when the PC connects or disconnects.
    var onConnectionChange: ((Bool) -> Void)?
    /// Called (on any thread) right after a PC connects, so we can send info.
    var onNewConnection: (() -> Void)?

    private let queue = DispatchQueue(label: "akremcam.server")
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

    private func accept(_ conn: NWConnection) {
        // Only one PC at a time: the newest connection wins.
        dropConnection()
        connection = conn
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

    /// Reads (and ignores) anything the PC sends, so we notice when it disconnects.
    private func receiveLoop(_ conn: NWConnection) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 4096) { [weak self] _, _, isComplete, error in
            guard let self = self else { return }
            if isComplete || error != nil {
                self.queue.async {
                    if self.connection === conn { self.dropConnection() }
                }
            } else {
                self.receiveLoop(conn)
            }
        }
    }

    private func dropConnection() {
        connection?.stateUpdateHandler = nil
        connection?.cancel()
        connection = nil
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
            message.append(contentsOf: Array("AKC1".utf8))
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
