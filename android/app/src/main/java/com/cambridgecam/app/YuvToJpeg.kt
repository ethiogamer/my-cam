package com.cambridgecam.app

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import androidx.camera.core.ImageProxy
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import kotlin.math.min

/** Camera frame (YUV_420_888) -> JPEG bytes. Used from one thread only. */
class YuvToJpeg {
    private var nv21 = ByteArray(0)
    private var row = ByteArray(0)
    private val out = ByteArrayOutputStream(512 * 1024)

    fun convert(image: ImageProxy, quality: Int): ByteArray {
        val w = image.width and 1.inv()
        val h = image.height and 1.inv()
        val ySize = w * h
        val size = ySize + ySize / 2
        if (nv21.size != size) nv21 = ByteArray(size)

        val y = image.planes[0]
        val u = image.planes[1]
        val v = image.planes[2]
        copyPlane(y.buffer, y.rowStride, y.pixelStride, w, h, ySize, 0, 1)
        // NV21 = all Y, then V and U interleaved (V first).
        copyPlane(v.buffer, v.rowStride, v.pixelStride, w / 2, h / 2, size, ySize, 2)
        copyPlane(u.buffer, u.rowStride, u.pixelStride, w / 2, h / 2, size, ySize + 1, 2)

        out.reset()
        YuvImage(nv21, ImageFormat.NV21, w, h, null).compressToJpeg(Rect(0, 0, w, h), quality, out)
        return out.toByteArray()
    }

    private fun copyPlane(
        src: ByteBuffer, rowStride: Int, pixelStride: Int, w: Int, h: Int,
        limit: Int, offset: Int, step: Int,
    ) {
        src.rewind()
        if (pixelStride == 1 && step == 1) {
            if (rowStride == w && src.remaining() >= w * h) {
                src.get(nv21, offset, w * h)
            } else {
                for (r in 0 until h) {
                    src.position(r * rowStride)
                    src.get(nv21, offset + r * w, min(w, src.remaining()))
                }
            }
            return
        }
        if (row.size < rowStride) row = ByteArray(rowStride)
        var o = offset
        for (r in 0 until h) {
            val start = r * rowStride
            if (start >= src.limit()) break
            src.position(start)
            val len = min(rowStride, src.remaining())
            src.get(row, 0, len)
            var i = 0
            for (c in 0 until w) {
                if (i >= len || o >= limit) break
                nv21[o] = row[i]
                o += step
                i += pixelStride
            }
        }
    }
}
