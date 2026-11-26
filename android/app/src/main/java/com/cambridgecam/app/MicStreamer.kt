package com.cambridgecam.app

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder

/** Sends the phone's microphone to the PC (48 kHz mono, 16-bit) while the PC asks for it. */
class MicStreamer(private val server: StreamServer) {
    @Volatile private var running = false
    private var thread: Thread? = null

    val isRunning: Boolean get() = running

    @SuppressLint("MissingPermission")
    fun start() {
        if (running) return
        running = true
        thread = Thread({
            val rate = 48000
            val minBuf = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val record = try {
                AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, rate, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, rate / 5))
            } catch (_: Exception) {
                running = false
                return@Thread
            }
            if (record.state != AudioRecord.STATE_INITIALIZED) {
                record.release()
                running = false
                return@Thread
            }
            val chunk = ByteArray(rate / 50 * 2)  // 20 ms
            record.startRecording()
            try {
                while (running) {
                    val n = record.read(chunk, 0, chunk.size)
                    if (n <= 0) continue
                    if (!server.isConnected) continue
                    val payload = ByteArray(5 + n)
                    payload[0] = (rate shr 24).toByte()
                    payload[1] = (rate shr 16).toByte()
                    payload[2] = (rate shr 8).toByte()
                    payload[3] = rate.toByte()
                    payload[4] = 1
                    System.arraycopy(chunk, 0, payload, 5, n)
                    server.sendAudio(payload)
                }
            } finally {
                try { record.stop() } catch (_: Exception) {}
                record.release()
            }
        }, "cambridge-mic").also { it.start() }
    }

    fun stop() {
        running = false
    }
}
