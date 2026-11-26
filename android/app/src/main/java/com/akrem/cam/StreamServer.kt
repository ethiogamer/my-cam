package com.akrem.cam

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
 *   4 bytes "AKC1", 4 bytes type (big-endian), 4 bytes length (big-endian), payload
 *   type 1 = JPEG frame, 2 = JSON info (phone -> PC), 3 = JSON command (PC -> phone)
 */
class StreamServer(
    private val port: Int = 7878,
    private val onConnectionChange: (Boolean) -> Unit,
    private val onNewConnection: () -> Unit,
    private val onCommand: (JSONObject) -> Unit,
) {
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
        Thread({ acceptLoop() }, "akremcam-accept").start()
        Thread({ sendLoop() }, "akremcam-send").start()
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
                    Thread({ readLoop(socket) }, "akremcam-read").start()
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
                if (header[0] != 'A'.code.toByte() || header[1] != 'K'.code.toByte() ||
                    header[2] != 'C'.code.toByte() || header[3] != '1'.code.toByte()) break
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
        header.put("AKC1".toByteArray(Charsets.US_ASCII))
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
