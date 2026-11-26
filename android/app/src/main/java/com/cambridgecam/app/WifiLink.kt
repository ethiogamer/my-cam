package com.cambridgecam.app

import android.content.Context
import android.net.Uri
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

/**
 * Finds CamBridge PCs on the Wi-Fi (mDNS / NSD), pairs by QR code, remembers PCs,
 * and reconnects to a remembered PC by itself. Everything here runs on the main thread.
 */
class WifiLink(
    private val context: Context,
    private val server: StreamServer,
    private val onChanged: () -> Unit,
) {
    data class FoundPC(val id: String, val name: String, val host: String, val port: Int)
    data class SavedPC(val id: String, var name: String, var key: String, var hosts: List<String>, var port: Int)

    var status = ""; private set
    var connectedPC: String? = null; private set
    val found = LinkedHashMap<String, FoundPC>()
    val saved = LinkedHashMap<String, SavedPC>()

    private val main = Handler(Looper.getMainLooper())
    private val prefs = context.getSharedPreferences("cambridge", Context.MODE_PRIVATE)
    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val resolveQueue = ArrayDeque<NsdServiceInfo>()
    private var resolving = false
    private var discovering = false
    private var target: Pair<String, String>? = null   // id, name
    private val blocked = HashSet<String>()
    private var autoConnect = true
    private val retry = Runnable { autoConnectIfPossible() }

    private val deviceId: String by lazy {
        prefs.getString("deviceId", null) ?: UUID.randomUUID().toString().also {
            prefs.edit().putString("deviceId", it).apply()
        }
    }

    private val deviceName: String
        get() = "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}"

    // ------------------------------------------------------------------ start / discovery

    fun start() {
        loadSaved()
        server.onWelcome = { status, info -> main.post { welcome(status, info) } }
        server.onOutgoingClosed = { accepted -> main.post { closed(accepted) } }
        startDiscovery()
    }

    fun stop() {
        if (discovering) {
            try { nsd.stopServiceDiscovery(discoveryListener) } catch (_: Exception) {}
            discovering = false
        }
    }

    private val discoveryListener = object : NsdManager.DiscoveryListener {
        override fun onDiscoveryStarted(serviceType: String) { discovering = true }
        override fun onDiscoveryStopped(serviceType: String) { discovering = false }
        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
            discovering = false
            main.postDelayed({ startDiscovery() }, 5000)
        }
        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {}
        override fun onServiceFound(info: NsdServiceInfo) {
            main.post {
                resolveQueue.addLast(info)
                resolveNext()
            }
        }
        override fun onServiceLost(info: NsdServiceInfo) {
            main.post {
                val gone = found.values.firstOrNull { it.name == info.serviceName || info.serviceName.startsWith(it.name) }
                if (gone != null) found.remove(gone.id)
                onChanged()
            }
        }
    }

    private fun startDiscovery() {
        if (discovering) return
        try {
            nsd.discoverServices("_cambridge._tcp", NsdManager.PROTOCOL_DNS_SD, discoveryListener)
        } catch (_: Exception) {
            main.postDelayed({ startDiscovery() }, 5000)
        }
    }

    @Suppress("DEPRECATION")
    private fun resolveNext() {
        if (resolving) return
        val info = resolveQueue.removeFirstOrNull() ?: return
        resolving = true
        try {
            nsd.resolveService(info, object : NsdManager.ResolveListener {
                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    main.post { resolving = false; resolveNext() }
                }
                override fun onServiceResolved(r: NsdServiceInfo) {
                    main.post {
                        resolving = false
                        val host = r.host?.hostAddress
                        if (host != null && !host.contains(":")) {
                            val attrs = r.attributes
                            val id = attrs["id"]?.let { String(it) } ?: r.serviceName
                            val name = attrs["name"]?.let { String(it) } ?: r.serviceName
                            found[id] = FoundPC(id, name, host, r.port)
                            onChanged()
                            autoConnectIfPossible()
                        }
                        resolveNext()
                    }
                }
            })
        } catch (_: Exception) {
            resolving = false
        }
    }

    // ------------------------------------------------------------------ connecting

    private fun hello(pcId: String): JSONObject = JSONObject()
        .put("device", deviceName)
        .put("deviceId", deviceId)
        .put("key", saved[pcId]?.key ?: "")
        .put("platform", "android")

    fun connect(pc: FoundPC) {
        autoConnect = true
        blocked.remove(pc.id)
        val hosts = mutableListOf(pc.host)
        saved[pc.id]?.hosts?.forEach { if (it !in hosts) hosts.add(it) }
        begin(pc.id, pc.name, hosts, pc.port)
    }

    /** From the QR code: cambridge://pair?h=ip1,ip2&p=7879&k=KEY&id=PCID&n=NAME */
    fun handleUrl(text: String): Boolean {
        val uri = try { Uri.parse(text) } catch (_: Exception) { return false }
        if (uri.scheme != "cambridge") return false
        val id = uri.getQueryParameter("id") ?: return false
        val key = uri.getQueryParameter("k") ?: return false
        val hosts = (uri.getQueryParameter("h") ?: "").split(",").filter { it.isNotBlank() }.toMutableList()
        val port = uri.getQueryParameter("p")?.toIntOrNull() ?: 7879
        val name = uri.getQueryParameter("n") ?: "PC"
        found[id]?.let { if (it.host !in hosts) hosts.add(0, it.host) }
        if (hosts.isEmpty()) return false
        saved[id] = SavedPC(id, name, key, hosts, port)
        storeSaved()
        autoConnect = true
        blocked.remove(id)
        begin(id, name, hosts, port)
        return true
    }

    fun disconnect() {
        autoConnect = false
        main.removeCallbacks(retry)
        target = null
        server.disconnectOut()
        connectedPC = null
        status = "Disconnected"
        onChanged()
    }

    fun forget(id: String) {
        saved.remove(id)
        storeSaved()
        onChanged()
    }

    private fun begin(id: String, name: String, hosts: List<String>, port: Int) {
        main.removeCallbacks(retry)
        target = id to name
        status = "Connecting to $name…"
        onChanged()
        server.connectOut(hosts, port, hello(id))
    }

    private fun autoConnectIfPossible() {
        if (!autoConnect || target != null || server.isConnected) return
        val pc = found.values.firstOrNull { saved.containsKey(it.id) && it.id !in blocked } ?: return
        connect(pc)
    }

    // ------------------------------------------------------------------ answers from the PC

    private fun welcome(status: String, info: JSONObject) {
        val pcName = info.optString("pc", target?.second ?: "PC")
        val pcId = info.optString("id", target?.first ?: "")
        when (status) {
            "waiting" -> this.status = "Click “Allow” on $pcName to connect"
            "ok" -> {
                this.status = "Connected to $pcName over Wi-Fi"
                connectedPC = pcName
                val key = info.optString("key", "")
                if (pcId.isNotEmpty() && key.isNotEmpty()) {
                    val entry = saved[pcId] ?: SavedPC(pcId, pcName, key, listOfNotNull(found[pcId]?.host), 7879)
                    entry.key = key
                    entry.name = pcName
                    found[pcId]?.let { f -> if (f.host !in entry.hosts) entry.hosts = listOf(f.host) + entry.hosts }
                    saved[pcId] = entry
                    storeSaved()
                }
            }
            "denied" -> {
                this.status = "$pcName said no"
                if (pcId.isNotEmpty()) blocked.add(pcId)
            }
            "busy" -> this.status = "$pcName is already using a phone"
        }
        onChanged()
    }

    private fun closed(accepted: Boolean) {
        val t = target
        target = null
        if (accepted) {
            connectedPC = null
            if (autoConnect) status = "Wi-Fi connection lost — reconnecting…"
            main.postDelayed(retry, 2000)
        } else {
            if (t != null && !status.endsWith("said no") && !status.contains("already using")) {
                status = "Couldn't reach ${t.second}. Is the phone on the same Wi-Fi?"
            }
            main.postDelayed(retry, 4000)
        }
        onChanged()
    }

    // ------------------------------------------------------------------ remembered PCs

    private fun loadSaved() {
        saved.clear()
        val text = prefs.getString("savedPCs", null) ?: return
        try {
            val arr = JSONArray(text)
            for (i in 0 until arr.length()) {
                val o = arr.getJSONObject(i)
                val hostsArr = o.optJSONArray("hosts") ?: JSONArray()
                val hosts = (0 until hostsArr.length()).map { hostsArr.getString(it) }
                saved[o.getString("id")] = SavedPC(o.getString("id"), o.optString("name"), o.optString("key"), hosts, o.optInt("port", 7879))
            }
        } catch (_: Exception) {
        }
    }

    private fun storeSaved() {
        val arr = JSONArray()
        for (pc in saved.values) {
            arr.put(JSONObject().put("id", pc.id).put("name", pc.name).put("key", pc.key)
                .put("hosts", JSONArray(pc.hosts)).put("port", pc.port))
        }
        prefs.edit().putString("savedPCs", arr.toString()).apply()
    }
}
