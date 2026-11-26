import Foundation
import Network
import UIKit

struct FoundPC: Identifiable, Hashable {
    let id: String
    let name: String
    let endpoint: NWEndpoint
}

struct SavedPC: Codable {
    var id: String
    var name: String
    var key: String
    var hosts: [String]
    var port: UInt16
}

/// Finds CamBridge PCs on the Wi-Fi (Bonjour), pairs by QR code, remembers PCs,
/// and reconnects to a remembered PC by itself.
final class WifiManager: ObservableObject {
    static let serviceType = "_cambridge._tcp"

    @Published private(set) var found: [FoundPC] = []
    @Published private(set) var status = ""
    @Published private(set) var connectedPC: String?
    @Published private(set) var saved: [String: SavedPC] = [:]

    private weak var server: StreamServer?
    private var browser: NWBrowser?
    private var target: (id: String, name: String)?
    private var pendingHosts: [NWEndpoint] = []
    private var blocked = Set<String>()       // PCs that said no (this session)
    private var autoConnect = true
    private var retryWork: DispatchWorkItem?

    private var deviceId: String {
        if let id = UIDevice.current.identifierForVendor?.uuidString { return id }
        let d = UserDefaults.standard
        if let id = d.string(forKey: "deviceId") { return id }
        let id = UUID().uuidString
        d.set(id, forKey: "deviceId")
        return id
    }

    // MARK: - Start

    func start(server: StreamServer) {
        self.server = server
        loadSaved()
        server.onWelcome = { [weak self] status, info in
            DispatchQueue.main.async { self?.welcome(status, info) }
        }
        server.onOutgoingClosed = { [weak self] accepted in
            DispatchQueue.main.async { self?.closed(accepted: accepted) }
        }
        browse()
    }

    private func browse() {
        browser?.cancel()
        let b = NWBrowser(for: .bonjourWithTXTRecord(type: WifiManager.serviceType, domain: nil),
                          using: NWParameters())
        b.browseResultsChangedHandler = { [weak self] results, _ in
            var list: [FoundPC] = []
            for r in results {
                guard case let .service(name, _, _, _) = r.endpoint else { continue }
                var id = name
                var display = name
                if case let .bonjour(txt) = r.metadata {
                    if let v = txt["id"] { id = v }
                    if let v = txt["name"] { display = v }
                }
                list.append(FoundPC(id: id, name: display, endpoint: r.endpoint))
            }
            DispatchQueue.main.async {
                self?.found = list.sorted { $0.name < $1.name }
                self?.autoConnectIfPossible()
            }
        }
        b.stateUpdateHandler = { [weak self] state in
            if case .failed = state {
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self?.browse() }
            }
        }
        browser = b
        b.start(queue: .main)
    }

    // MARK: - Connecting

    private func hello(for pcId: String) -> [String: Any] {
        [
            "device": UIDevice.current.name,
            "deviceId": deviceId,
            "key": saved[pcId]?.key ?? "",
            "platform": "ios",
        ]
    }

    /// Tapped in the list of PCs.
    func connect(_ pc: FoundPC) {
        autoConnect = true
        blocked.remove(pc.id)
        start(target: (pc.id, pc.name), endpoints: [pc.endpoint])
    }

    /// From the QR code: cambridge://pair?h=ip1,ip2&p=7879&k=KEY&id=PCID&n=NAME
    @discardableResult
    func handle(url: URL) -> Bool {
        guard url.scheme == "cambridge",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return false }
        var q: [String: String] = [:]
        for item in items { q[item.name] = item.value ?? "" }
        guard let id = q["id"], let key = q["k"], let hostList = q["h"], !hostList.isEmpty else { return false }
        let port = UInt16(q["p"] ?? "") ?? 7879
        let name = q["n"] ?? "PC"
        let hosts = hostList.split(separator: ",").map(String.init)
        saved[id] = SavedPC(id: id, name: name, key: key, hosts: hosts, port: port)
        storeSaved()
        autoConnect = true
        blocked.remove(id)
        var endpoints = hosts.map { NWEndpoint.hostPort(host: NWEndpoint.Host($0), port: NWEndpoint.Port(rawValue: port)!) }
        if let pc = found.first(where: { $0.id == id }) { endpoints.insert(pc.endpoint, at: 0) }
        start(target: (id, name), endpoints: endpoints)
        return true
    }

    func disconnect() {
        autoConnect = false
        retryWork?.cancel()
        target = nil
        pendingHosts = []
        server?.disconnectOut()
        status = "Disconnected"
    }

    func forget(_ id: String) {
        saved[id] = nil
        storeSaved()
    }

    private func start(target t: (id: String, name: String), endpoints: [NWEndpoint]) {
        guard let server = server, let first = endpoints.first else { return }
        retryWork?.cancel()
        target = t
        pendingHosts = Array(endpoints.dropFirst())
        status = "Connecting to \(t.name)…"
        server.connectOut(to: first, hello: hello(for: t.id))
    }

    private func autoConnectIfPossible() {
        guard autoConnect, target == nil, let server = server, !server.isConnected else { return }
        if let pc = found.first(where: { saved[$0.id] != nil && !blocked.contains($0.id) }) {
            start(target: (pc.id, pc.name), endpoints: [pc.endpoint])
        }
    }

    private func scheduleRetry(after seconds: Double) {
        retryWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.autoConnectIfPossible() }
        retryWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    // MARK: - Answers from the PC

    private func welcome(_ status: String, _ info: [String: Any]) {
        let pcName = info["pc"] as? String ?? target?.name ?? "PC"
        let pcId = info["id"] as? String ?? target?.id ?? ""
        switch status {
        case "waiting":
            self.status = "Click “Allow” on \(pcName) to connect"
        case "ok":
            self.status = "Connected to \(pcName) over Wi-Fi"
            connectedPC = pcName
            pendingHosts = []
            if !pcId.isEmpty, let key = info["key"] as? String {
                var entry = saved[pcId] ?? SavedPC(id: pcId, name: pcName, key: key, hosts: [], port: 7879)
                entry.key = key
                entry.name = pcName
                saved[pcId] = entry
                storeSaved()
            }
        case "denied":
            self.status = "\(pcName) said no"
            if !pcId.isEmpty { blocked.insert(pcId) }
        case "busy":
            self.status = "\(pcName) is already using a phone"
        default:
            break
        }
    }

    private func closed(accepted: Bool) {
        if accepted {
            connectedPC = nil
            target = nil
            status = autoConnect ? "Wi-Fi connection lost — reconnecting…" : status
            scheduleRetry(after: 2)
            return
        }
        // Not accepted yet: try the next address, if any.
        if let next = pendingHosts.first, let t = target, let server = server {
            pendingHosts.removeFirst()
            server.connectOut(to: next, hello: hello(for: t.id))
            return
        }
        if let t = target, !status.hasSuffix("said no"), !status.contains("already using") {
            status = "Couldn't reach \(t.name). Is the phone on the same Wi-Fi?"
        }
        target = nil
        scheduleRetry(after: 4)
    }

    // MARK: - Remembered PCs

    private func loadSaved() {
        guard let data = UserDefaults.standard.data(forKey: "savedPCs"),
              let list = try? JSONDecoder().decode([String: SavedPC].self, from: data) else { return }
        saved = list
    }

    private func storeSaved() {
        if let data = try? JSONEncoder().encode(saved) {
            UserDefaults.standard.set(data, forKey: "savedPCs")
        }
    }
}
