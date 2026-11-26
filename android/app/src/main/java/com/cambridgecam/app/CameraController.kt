package com.cambridgecam.app

import android.annotation.SuppressLint
import android.content.Context
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.os.Build
import android.util.Range
import android.util.Size
import android.view.OrientationEventListener
import android.view.Surface
import androidx.activity.ComponentActivity
import androidx.camera.camera2.interop.Camera2CameraControl
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.camera2.interop.Camera2Interop
import androidx.camera.camera2.interop.CaptureRequestOptions
import androidx.camera.camera2.interop.ExperimentalCamera2Interop
import androidx.camera.core.Camera
import androidx.camera.core.CameraInfo
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Runs the camera, streams frames to the PC, and applies every setting the PC sends
 * (same command names as the iPhone app, so the Windows app treats both the same way).
 */
@androidx.annotation.OptIn(ExperimentalCamera2Interop::class)
class CameraController(
    private val activity: ComponentActivity,
    private val previewView: PreviewView,
    private val server: StreamServer,
    private val onScreenOff: (Boolean) -> Unit,
    private val onChanged: () -> Unit,
    private val onQr: (String) -> Unit,
    private val onMicWanted: (Boolean) -> Unit = {},
) {
    var micOn = false
    /** Looking for the pairing QR code on the PC screen. */
    @Volatile var scanning = false
    private var scanCounter = 0
    private val qrReader = MultiFormatReader().apply {
        setHints(mapOf(
            DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
            DecodeHintType.TRY_HARDER to true,
        ))
    }
    private val prefs = activity.getSharedPreferences("cambridge", Context.MODE_PRIVATE)

    // ---- settings
    var front = prefs.getBoolean("front", false); private set
    var quality = prefs.getString("quality", "1080p") ?: "1080p"; private set
    var fps = prefs.getInt("fps", 30); private set
    var orientationMode = prefs.getString("orientation", "Auto") ?: "Auto"; private set
    var stabilization = prefs.getBoolean("stabilization", false); private set
    var zoom = 1f; private set
    var exposureEv = 0f; private set
    var torchOn = false; private set
    var focusManual = false; private set
    var lensPosition = 0.5f; private set
    var exposureManual = false; private set
    var iso = 100; private set
    var shutterSec = 1.0 / 60.0; private set
    var wbManual = false; private set
    var temperature = 5500f; private set
    var screenOff = false; private set
    var hot = false

    // ---- what this camera can do
    private var minZoom = 1f
    private var maxZoom = 1f
    private var torchAvailable = false
    private var isoRange = Range(100, 1600)
    private var shutterRange = Range(1.0 / 8000.0, 1.0 / 30.0)
    private var minFocusDiopters = 0f
    private var manualSensor = false
    private var stabilizationSupported = false
    private var awbModes = intArrayOf()
    private var evStep = 0f
    private var evRange = Range(0, 0)
    private var actualFps = 30

    // ---- live values from the camera
    @Volatile private var liveIso = 0
    @Volatile private var liveShutter = 0.0
    @Volatile private var liveFocus = -1f
    @Volatile var frameWidth = 0; private set
    @Volatile var frameHeight = 0; private set
    @Volatile private var frameRotation = 0
    private var skipToggle = false

    private var provider: ProcessCameraProvider? = null
    private var camera: Camera? = null
    private var analysis: ImageAnalysis? = null
    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private val encoder = YuvToJpeg()
    private var targetRotation = Surface.ROTATION_90

    private val orientationListener = object : OrientationEventListener(activity) {
        override fun onOrientationChanged(orientation: Int) {
            if (orientation == ORIENTATION_UNKNOWN || orientationMode != "Auto") return
            val rotation = when (orientation) {
                in 45..134 -> Surface.ROTATION_270
                in 135..224 -> Surface.ROTATION_180
                in 225..314 -> Surface.ROTATION_90
                else -> Surface.ROTATION_0
            }
            setTargetRotation(rotation)
        }
    }

    private val captureCallback = object : CameraCaptureSession.CaptureCallback() {
        override fun onCaptureCompleted(session: CameraCaptureSession, request: CaptureRequest, result: TotalCaptureResult) {
            result.get(CaptureResult.SENSOR_SENSITIVITY)?.let { liveIso = it }
            result.get(CaptureResult.SENSOR_EXPOSURE_TIME)?.let { liveShutter = it / 1_000_000_000.0 }
            result.get(CaptureResult.LENS_FOCUS_DISTANCE)?.let { d ->
                liveFocus = if (minFocusDiopters > 0f) (1f - d / minFocusDiopters).coerceIn(0f, 1f) else -1f
            }
        }
    }

    // ------------------------------------------------------------------ start

    fun start() {
        applyFixedOrientation()
        orientationListener.enable()
        val future = ProcessCameraProvider.getInstance(activity)
        future.addListener({
            provider = future.get()
            bind()
        }, ContextCompat.getMainExecutor(activity))
    }

    fun stop() {
        orientationListener.disable()
    }

    private fun cameraInfoFor(isFront: Boolean): CameraInfo? {
        val p = provider ?: return null
        val facing = if (isFront) CameraSelector.LENS_FACING_FRONT else CameraSelector.LENS_FACING_BACK
        return try {
            CameraSelector.Builder().requireLensFacing(facing).build().filter(p.availableCameraInfos).firstOrNull()
        } catch (_: Exception) {
            null
        }
    }

    private fun chooseFpsRange(info: CameraInfo?): Range<Int>? {
        info ?: return null
        val ranges = Camera2CameraInfo.from(info)
            .getCameraCharacteristic(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES) ?: return null
        ranges.firstOrNull { it.lower == fps && it.upper == fps }?.let { return it }
        ranges.firstOrNull { it.upper == fps }?.let { return it }
        return ranges.firstOrNull { it.lower == 30 && it.upper == 30 } ?: ranges.firstOrNull { it.upper == 30 }
    }

    @SuppressLint("RestrictedApi")
    fun bind() {
        val p = provider ?: return
        if (cameraInfoFor(front) == null) front = !front
        val info = cameraInfoFor(front) ?: return
        p.unbindAll()

        val size = if (quality == "720p") Size(1280, 720) else Size(1920, 1080)
        val resolution = ResolutionSelector.Builder()
            .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
            .setResolutionStrategy(ResolutionStrategy(size, ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER))
            .build()
        val fpsRange = chooseFpsRange(info)
        actualFps = fpsRange?.upper ?: 30

        val previewBuilder = Preview.Builder().setResolutionSelector(resolution)
        val analysisBuilder = ImageAnalysis.Builder()
            .setResolutionSelector(resolution)
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_YUV_420_888)
        val previewExt = Camera2Interop.Extender(previewBuilder).setSessionCaptureCallback(captureCallback)
        val analysisExt = Camera2Interop.Extender(analysisBuilder)
        if (fpsRange != null) {
            previewExt.setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange)
            analysisExt.setCaptureRequestOption(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange)
        }

        val preview = previewBuilder.build()
        preview.setSurfaceProvider(previewView.surfaceProvider)
        val imageAnalysis = analysisBuilder.build()
        imageAnalysis.targetRotation = targetRotation
        imageAnalysis.setAnalyzer(analysisExecutor) { image -> analyze(image) }
        analysis = imageAnalysis

        val facing = if (front) CameraSelector.LENS_FACING_FRONT else CameraSelector.LENS_FACING_BACK
        val selector = CameraSelector.Builder().requireLensFacing(facing).build()
        camera = try {
            p.bindToLifecycle(activity, selector, preview, imageAnalysis)
        } catch (e: Exception) {
            null
        }
        readCapabilities()
        zoom = 1f
        applyAll()
        onChanged()
        sendInfo()
    }

    private fun readCapabilities() {
        val cam = camera ?: return
        val info = cam.cameraInfo
        val c2 = Camera2CameraInfo.from(info)
        info.zoomState.value?.let { minZoom = it.minZoomRatio; maxZoom = it.maxZoomRatio }
        torchAvailable = info.hasFlashUnit()
        c2.getCameraCharacteristic(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)?.let { isoRange = it }
        c2.getCameraCharacteristic(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)?.let {
            val lo = max(it.lower / 1e9, 1.0 / 10000.0)
            val hi = min(it.upper / 1e9, 1.0 / actualFps)
            if (lo < hi) shutterRange = Range(lo, hi)
        }
        minFocusDiopters = c2.getCameraCharacteristic(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE) ?: 0f
        val caps = c2.getCameraCharacteristic(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES) ?: intArrayOf()
        manualSensor = caps.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR)
        val stab = c2.getCameraCharacteristic(CameraCharacteristics.CONTROL_AVAILABLE_VIDEO_STABILIZATION_MODES) ?: intArrayOf()
        stabilizationSupported = stab.contains(CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_ON)
        awbModes = c2.getCameraCharacteristic(CameraCharacteristics.CONTROL_AWB_AVAILABLE_MODES) ?: intArrayOf()
        val exposure = info.exposureState
        evStep = if (exposure.isExposureCompensationSupported) exposure.exposureCompensationStep.toFloat() else 0f
        evRange = exposure.exposureCompensationRange
        if (!manualSensor) { exposureManual = false; focusManual = false }
        if (minFocusDiopters <= 0f) focusManual = false
        if (!stabilizationSupported) stabilization = false
        if (!torchAvailable) torchOn = false
    }

    // ------------------------------------------------------------------ frames

    private fun analyze(image: ImageProxy) {
        try {
            val rotation = image.imageInfo.rotationDegrees
            if (rotation != frameRotation || image.width != frameWidth || image.height != frameHeight) {
                frameRotation = rotation
                frameWidth = image.width
                frameHeight = image.height
                sendInfo()
            }
            if (scanning) {
                scanCounter++
                if (scanCounter % 3 == 0) {
                    decodeQr(image)?.let { text ->
                        if (text.startsWith("cambridge://")) {
                            scanning = false
                            activity.runOnUiThread { onQr(text) }
                        }
                    }
                }
            }
            if (!server.isReadyForFrame) return
            if (hot) {
                skipToggle = !skipToggle
                if (skipToggle) return
            }
            server.sendFrame(encoder.convert(image, 80))
        } catch (_: Exception) {
        } finally {
            image.close()
        }
    }

    private fun decodeQr(image: ImageProxy): String? {
        val plane = image.planes[0]
        val buffer = plane.buffer
        buffer.rewind()
        val data = ByteArray(buffer.remaining())
        buffer.get(data)
        val source = PlanarYUVLuminanceSource(data, plane.rowStride, image.height, 0, 0,
            image.width, image.height, false)
        return try {
            qrReader.decodeWithState(BinaryBitmap(HybridBinarizer(source))).text
        } catch (_: Exception) {
            null
        } finally {
            qrReader.reset()
        }
    }

    // ------------------------------------------------------------------ applying settings

    private fun applyAll() {
        applyZoom()
        applyEv()
        applyTorch()
        applyCamera2()
    }

    private fun applyZoom() {
        camera?.cameraControl?.setZoomRatio(zoom.coerceIn(minZoom, max(minZoom, maxZoom)))
    }

    private fun applyEv() {
        val cam = camera ?: return
        if (evStep <= 0f) return
        val index = (exposureEv / evStep).roundToInt().coerceIn(evRange.lower, evRange.upper)
        cam.cameraControl.setExposureCompensationIndex(index)
    }

    private fun applyTorch() {
        val cam = camera ?: return
        if (torchAvailable) cam.cameraControl.enableTorch(torchOn)
    }

    /** Manual exposure / focus / white balance / stabilization, all in one request. */
    private fun applyCamera2() {
        val cam = camera ?: return
        val b = CaptureRequestOptions.Builder()
        if (exposureManual && manualSensor) {
            val isoValue = iso.coerceIn(isoRange.lower, isoRange.upper)
            val seconds = shutterSec.coerceIn(shutterRange.lower, shutterRange.upper)
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_OFF)
            b.setCaptureRequestOption(CaptureRequest.SENSOR_SENSITIVITY, isoValue)
            b.setCaptureRequestOption(CaptureRequest.SENSOR_EXPOSURE_TIME, (seconds * 1e9).toLong())
            b.setCaptureRequestOption(CaptureRequest.SENSOR_FRAME_DURATION, (1e9 / actualFps).toLong())
        }
        if (focusManual && manualSensor && minFocusDiopters > 0f) {
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_OFF)
            b.setCaptureRequestOption(CaptureRequest.LENS_FOCUS_DISTANCE, minFocusDiopters * (1f - lensPosition))
        }
        if (wbManual) {
            b.setCaptureRequestOption(CaptureRequest.CONTROL_AWB_MODE, awbModeFor(temperature))
        }
        if (stabilizationSupported) {
            b.setCaptureRequestOption(
                CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE,
                if (stabilization) CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_ON
                else CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF,
            )
        }
        try {
            Camera2CameraControl.from(cam.cameraControl).setCaptureRequestOptions(b.build())
        } catch (_: Exception) {
        }
    }

    /** Android phones offer white-balance presets; pick the one closest to the temperature. */
    private fun awbModeFor(kelvin: Float): Int {
        val presets = listOf(
            2850f to CameraMetadata.CONTROL_AWB_MODE_INCANDESCENT,
            3200f to CameraMetadata.CONTROL_AWB_MODE_WARM_FLUORESCENT,
            4200f to CameraMetadata.CONTROL_AWB_MODE_FLUORESCENT,
            5500f to CameraMetadata.CONTROL_AWB_MODE_DAYLIGHT,
            6500f to CameraMetadata.CONTROL_AWB_MODE_CLOUDY_DAYLIGHT,
            7500f to CameraMetadata.CONTROL_AWB_MODE_SHADE,
        ).filter { awbModes.isEmpty() || awbModes.contains(it.second) }
        return presets.minByOrNull { abs(it.first - kelvin) }?.second ?: CameraMetadata.CONTROL_AWB_MODE_AUTO
    }

    private fun applyFixedOrientation() {
        when (orientationMode) {
            "Landscape" -> setTargetRotation(Surface.ROTATION_90)
            "Landscape (flipped)" -> setTargetRotation(Surface.ROTATION_270)
            "Portrait" -> setTargetRotation(Surface.ROTATION_0)
        }
    }

    private fun setTargetRotation(rotation: Int) {
        if (rotation == targetRotation) return
        targetRotation = rotation
        analysis?.targetRotation = rotation
    }

    fun focusAt(x: Float, y: Float) {
        val cam = camera ?: return
        if (focusManual) return
        val point = previewView.meteringPointFactory.createPoint(x, y)
        try {
            cam.cameraControl.startFocusAndMetering(FocusMeteringAction.Builder(point).build())
        } catch (_: Exception) {
        }
    }

    // ------------------------------------------------------------------ simple actions (phone buttons)

    fun switchCamera() { front = !front; save(); bind() }
    fun toggleTorch() { torchOn = !torchOn && torchAvailable; applyTorch(); changed() }
    fun toggleQuality() { quality = if (quality == "720p") "1080p" else "720p"; save(); bind() }
    fun setScreenOff(off: Boolean) { screenOff = off; onScreenOff(off); changed() }
    val hasTorch: Boolean get() = torchAvailable

    private fun changed() {
        onChanged()
        sendInfo()
    }

    private fun save() {
        prefs.edit()
            .putBoolean("front", front)
            .putString("quality", quality)
            .putInt("fps", fps)
            .putString("orientation", orientationMode)
            .putBoolean("stabilization", stabilization)
            .apply()
    }

    // ------------------------------------------------------------------ commands from the PC (main thread)

    fun handle(c: JSONObject) {
        var rebind = false
        if (c.has("side")) {
            val wantFront = c.optString("side") == "Front"
            if (wantFront != front) { front = wantFront; rebind = true }
        }
        if (c.has("quality")) {
            val q = c.optString("quality")
            if ((q == "720p" || q == "1080p") && q != quality) { quality = q; rebind = true }
        }
        if (c.has("fps")) {
            val f = c.optInt("fps", fps)
            if ((f == 30 || f == 60) && f != fps) { fps = f; rebind = true }
        }
        if (c.has("orientation")) {
            orientationMode = c.optString("orientation", "Auto")
            applyFixedOrientation()
        }
        if (c.has("lens")) {
            zoom = when (c.optString("lens")) {
                "0.5x" -> minZoom
                "Zoom" -> min(2f, maxZoom)
                else -> 1f
            }
            applyZoom()
        }
        if (c.has("zoom")) { zoom = c.optDouble("zoom", 1.0).toFloat().coerceIn(minZoom, max(minZoom, maxZoom)); applyZoom() }
        if (c.has("exposure")) { exposureEv = c.optDouble("exposure", 0.0).toFloat().coerceIn(-2f, 2f); applyEv() }
        if (c.has("torch")) { torchOn = c.optBoolean("torch") && torchAvailable; applyTorch() }
        if (c.has("focusManual")) {
            focusManual = c.optBoolean("focusManual") && manualSensor && minFocusDiopters > 0f
            if (focusManual && liveFocus >= 0f) lensPosition = liveFocus
        }
        if (c.has("lensPosition")) lensPosition = c.optDouble("lensPosition", 0.5).toFloat().coerceIn(0f, 1f)
        if (c.has("exposureManual")) {
            exposureManual = c.optBoolean("exposureManual") && manualSensor
            if (exposureManual && liveIso > 0) { iso = liveIso; shutterSec = liveShutter }
        }
        if (c.has("iso")) iso = c.optDouble("iso", 100.0).roundToInt()
        if (c.has("shutter")) shutterSec = c.optDouble("shutter", 1.0 / 60.0)
        if (c.has("whiteBalanceManual")) wbManual = c.optBoolean("whiteBalanceManual")
        if (c.has("temperature")) temperature = c.optDouble("temperature", 5500.0).toFloat()
        if (c.has("stabilization")) stabilization = c.optBoolean("stabilization") && stabilizationSupported
        if (c.has("screenOff")) setScreenOff(c.optBoolean("screenOff"))
        if (c.has("focusCenter")) focusAt(previewView.width / 2f, previewView.height / 2f)
        if (c.has("audio")) onMicWanted(c.optBoolean("audio"))
        if (c.has("resetPro")) {
            exposureManual = false; focusManual = false; wbManual = false; exposureEv = 0f
            applyEv()
        }
        save()
        if (rebind) bind() else { applyCamera2(); changed() }
    }

    // ------------------------------------------------------------------ info for the PC

    private fun displayLens(): String = when {
        zoom < 0.99f -> "0.5x"
        zoom >= 1.99f -> "Zoom"
        else -> "1x"
    }

    fun sendInfo() {
        if (!server.isConnected) return
        val lenses = JSONArray()
        if (minZoom < 0.99f) lenses.put("0.5x")
        lenses.put("1x")
        if (maxZoom >= 2f) lenses.put("Zoom")
        val info = JSONObject()
        try {
            info.put("app", "CamBridge")
            info.put("platform", "android")
            info.put("device", "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}")
            info.put("camera", if (front) "Front" else "Back")
            info.put("lens", displayLens())
            info.put("lenses", lenses)
            info.put("quality", quality)
            info.put("fps", fps)
            info.put("actualFps", actualFps)
            info.put("orientation", orientationMode)
            info.put("zoom", zoom.toDouble())
            info.put("minZoom", minZoom.toDouble())
            info.put("maxZoom", maxZoom.toDouble())
            info.put("exposure", exposureEv.toDouble())
            info.put("torch", torchOn)
            info.put("torchAvailable", torchAvailable)
            info.put("torchLevelSupported", false)
            info.put("screenOff", screenOff)
            info.put("hot", hot)
            info.put("focusManual", focusManual)
            info.put("lensPosition", lensPosition.toDouble())
            info.put("manualFocusSupported", manualSensor && minFocusDiopters > 0f)
            info.put("exposureManual", exposureManual)
            info.put("iso", iso)
            info.put("shutter", shutterSec)
            info.put("isoMin", isoRange.lower)
            info.put("isoMax", isoRange.upper)
            info.put("shutterMin", shutterRange.lower)
            info.put("shutterMax", shutterRange.upper)
            info.put("manualExposureSupported", manualSensor)
            info.put("whiteBalanceManual", wbManual)
            info.put("temperature", temperature.toDouble())
            info.put("tint", 0)
            info.put("tintSupported", false)
            info.put("manualWhiteBalanceSupported", awbModes.size > 1)
            info.put("stabilization", stabilization)
            info.put("stabilizationSupported", stabilizationSupported)
            info.put("hdr", false)
            info.put("hdrSupported", false)
            info.put("liveISO", liveIso)
            if (liveShutter.isFinite()) info.put("liveShutter", liveShutter)
            info.put("liveLens", liveFocus.toDouble())
            info.put("frameRotation", frameRotation)
            info.put("codec", "jpeg")
            info.put("codecs", JSONArray().put("jpeg"))
            info.put("audio", micOn)
            if (frameWidth > 0) {
                info.put("width", frameWidth)
                info.put("height", frameHeight)
            }
        } catch (_: Exception) {
        }
        server.sendInfo(info)
    }
}
