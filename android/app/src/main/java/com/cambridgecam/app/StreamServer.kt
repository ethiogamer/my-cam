package com.cambridgecam.app

import org.json.JSONObject
import java.io.DataInputStream
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * Listens on the phone's own port 7878. The PC reaches it through the USB cable
 * with "adb forward" — no Wi-Fi needed.
 *
 * Every message (both directions):
 *   4 bytes "CBG1", 4 bytes type (big-endian), 4 bytes length (big-endian), payload
 *   type 1 = JPEG frame, 2 = JSON info (phone -> PC), 3 = JSON command (PC -> phone)
 */
class StreamServer(
    private val port: Int = 7878,
    private val onConnectionChange: (Boolean) -> Unit,
    private val onNewConnection: () -> Unit,
    private val onCommand: (JSONObject) -> Unit,
) {
    /** Wi-Fi: the PC answered our hello ("waiting", "ok", "denied", "busy"). */
    var onWelcome: ((String, JSONObject) -> Unit)? = null
    /** Wi-Fi: our connection to the PC closed. true = it had been accepted. */
    var onOutgoingClosed: ((Boolean) -> Unit)? = null
    @Volatile private var outgoing: Socket? = null
    @Volatile private var attempt = 0
    @Volatile private var running = false
    @Volatile private var client: Socket? = null
    @Volatile private var serverSocket: ServerSocket? = null
    private val sending = AtomicBoolean(false)
    private val sentFrames = AtomicInteger(0)
    private val writeLock = Any()
    private val frames = ArrayBlockingQueue<ByteArray>(1)
    private val infoExecutor = Executors.newSingleThreadExecutor()

    val isConnected: Boolean get() = client != null

    /** True when the PC is connected and the previous frame is already sent. */
    val isReadyForFrame: Boolean get() = client != null && !sending.get() && frames.isEmpty()

    fun takeSentFrameCount(): Int = sentFrames.getAndSet(0)

    fun start() {
        if (running) return
        running = true
        Thread({ acceptLoop() }, "cambridge-accept").start()
        Thread({ sendLoop() }, "cambridge-send").start()
    }

    fun stop() {
        running = false
        try { serverSocket?.close() } catch (_: IOException) {}
        dropClient(null)
    }

    private fun acceptLoop() {
        while (running) {
            try {
                val ss = ServerSocket()
                ss.reuseAddress = true
                ss.bind(InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 1)
                serverSocket = ss
                while (running) {
                    val socket = ss.accept()
                    socket.tcpNoDelay = true
                    dropClient(null)
                    client = socket
                    onConnectionChange(true)
                    Thread({ readLoop(socket) }, "cambridge-read").start()
                    onNewConnection()
                }
            } catch (e: IOException) {
                if (running) Thread.sleep(1000)
            } finally {
                try { serverSocket?.close() } catch (_: IOException) {}
                serverSocket = null
            }
        }
    }

    private fun readLoop(socket: Socket) {
        try {
            val input = DataInputStream(socket.getInputStream())
            val header = ByteArray(12)
            while (running && client === socket) {
                input.readFully(header)
                if (header[0] != 'C'.code.toByte() || header[1] != 'B'.code.toByte() ||
                    header[2] != 'G'.code.toByte() || header[3] != '1'.code.toByte()) break
                val buf = ByteBuffer.wrap(header)
                val type = buf.getInt(4)
                val length = buf.getInt(8)
                if (length < 0 || length > 1_000_000) break
                val payload = ByteArray(length)
                input.readFully(payload)
                if (type == 3) {
                    try { onCommand(JSONObject(String(payload, Charsets.UTF_8))) } catch (_: Exception) {}
                }
            }
        } catch (_: IOException) {
        } finally {
            dropClient(socket)
        }
    }

    /**
     * Wi-Fi: connect to the PC (trying each address), say hello, wait for "ok",
     * then stream exactly like over USB.
     */
    fun connectOut(hosts: List<String>, port: Int, hello: JSONObject) {
        val myAttempt = ++attempt
        Thread({
            var found: Socket? = null
            for (host in hosts) {
                if (myAttempt != attempt) return@Thread
                try {
                    val s = Socket()
                    s.connect(InetSocketAddress(host, port), 4000)
                    s.tcpNoDelay = true
                    found = s
                    break
                } catch (_: IOException) {
                }
            }
            val socket: Socket = found ?: run {
                onOutgoingClosed?.invoke(false)
                return@Thread
            }
            outgoing?.let { try { it.close() } catch (_: IOException) {} }
            outgoing = socket
            var accepted = false
            try {
                write(socket, 5, hello.toString().toByteArray(Charsets.UTF_8))
                val input = DataInputStream(socket.getInputStream())
                val header = ByteArray(12)
                socket.soTimeout = 70_000
                while (running && myAttempt == attempt) {
                    input.readFully(header)
                    val buf = ByteBuffer.wrap(header)
                    val type = buf.getInt(4)
                    val length = buf.getInt(8)
                    if (length < 0 || length > 100_000) break
                    val payload = ByteArray(length)
                    input.readFully(payload)
                    if (type != 6) continue
                    val welcome = JSONObject(String(payload, Charsets.UTF_8))
                    val status = welcome.optString("status")
                    onWelcome?.invoke(status, welcome)
                    if (status == "ok") {
                        accepted = true
                        socket.soTimeout = 0
                        dropClient(null)
                        client = socket
                        onConnectionChange(true)
                        onNewConnection()
                        readLoop(socket)  // returns when the connection ends
                        break
                    }
                    if (status != "waiting") break
                }
            } catch (_: Exception) {
            } finally {
                try { socket.close() } catch (_: IOException) {}
                if (outgoing === socket) outgoing = null
                onOutgoingClosed?.invoke(accepted)
            }
        }, "cambridge-wifi").start()
    }

    /** Wi-Fi: hang up on purpose. */
    fun disconnectOut() {
        attempt++
        val s = outgoing ?: return
        outgoing = null
        try { s.close() } catch (_: IOException) {}
        dropClient(s)
    }

    val isWifi: Boolean get() = outgoing != null && client === outgoing

    private fun dropClient(only: Socket?) {
        val c = synchronized(this) {
            val current = client
            if (current == null || (only != null && current !== only)) {
                null
            } else {
                client = null
                current
            }
        } ?: return
        try { c.close() } catch (_: IOException) {}
        frames.clear()
        sending.set(false)
        onConnectionChange(false)
    }

    private fun write(socket: Socket, type: Int, payload: ByteArray) {
        val header = ByteBuffer.allocate(12)
        header.put("CBG1".toByteArray(Charsets.US_ASCII))
        header.putInt(type)
        header.putInt(payload.size)
        synchronized(writeLock) {
            val out = socket.getOutputStream()
            out.write(header.array())
            out.write(payload)
            out.flush()
        }
    }

    private fun sendLoop() {
        while (running) {
            val frame = frames.poll(500, TimeUnit.MILLISECONDS) ?: continue
            val socket = client ?: continue
            sending.set(true)
            try {
                write(socket, 1, frame)
                sentFrames.incrementAndGet()
            } catch (_: IOException) {
                dropClient(socket)
            } finally {
                sending.set(false)
            }
        }
    }

    /** Queues a frame; an older frame still waiting is thrown away (keeps delay low). */
    fun sendFrame(jpeg: ByteArray) {
        if (client == null) return
        frames.clear()
        frames.offer(jpeg)
    }

    fun sendInfo(info: JSONObject) {
        val socket = client ?: return
        val bytes = info.toString().toByteArray(Charsets.UTF_8)
        infoExecutor.execute {
            try { write(socket, 2, bytes) } catch (_: IOException) { dropClient(socket) }
        }
    }
}
