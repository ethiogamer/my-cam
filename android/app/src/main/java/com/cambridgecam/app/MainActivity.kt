package com.cambridgecam.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat

class MainActivity : ComponentActivity() {
    private lateinit var previewView: PreviewView
    private lateinit var statusDot: View
    private lateinit var statusTitle: TextView
    private lateinit var statusSub: TextView
    private lateinit var torchButton: TextView
    private lateinit var qualityButton: TextView
    private lateinit var darkOverlay: FrameLayout
    private lateinit var darkText: TextView
    private lateinit var permissionView: LinearLayout
    private lateinit var server: StreamServer
    private lateinit var camera: CameraController
    private lateinit var wifi: WifiLink
    private lateinit var scanBanner: TextView
    private val handler = Handler(Looper.getMainLooper())
    private var fps = 0
    private var started = false

    private val askCamera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startCamera() else permissionView.visibility = View.VISIBLE
    }

    private val ticker = object : Runnable {
        override fun run() {
            fps = server.takeSentFrameCount()
            checkHeat()
            if (server.isConnected) camera.sendInfo()
            updateStatus()
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(buildUi())

        server = StreamServer(
            onConnectionChange = { runOnUiThread { updateStatus() } },
            onNewConnection = { runOnUiThread { camera.sendInfo() } },
            onCommand = { cmd -> runOnUiThread { camera.handle(cmd) } },
        )
        camera = CameraController(this, previewView, server,
            onScreenOff = { off -> showDark(off) },
            onChanged = { updateStatus() },
            onQr = { text -> wifi.handleUrl(text); updateStatus() })
        server.start()
        wifi = WifiLink(this, server) { runOnUiThread { updateStatus() } }
        wifi.start()
        handleLink(intent)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            askCamera.launch(Manifest.permission.CAMERA)
        }
        handler.post(ticker)
    }

    private fun startCamera() {
        if (started) return
        started = true
        permissionView.visibility = View.GONE
        camera.start()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleLink(intent)
    }

    /** Opened from the QR code with the phone's normal camera / Google Lens. */
    private fun handleLink(intent: Intent?) {
        val data = intent?.data ?: return
        if (data.scheme == "cambridge") wifi.handleUrl(data.toString())
    }

    private fun showWifiDialog() {
        val labels = ArrayList<String>()
        val actions = ArrayList<() -> Unit>()
        for (pc in wifi.found.values) {
            val tag = when {
                wifi.connectedPC == pc.name -> "   ✓ connected"
                wifi.saved.containsKey(pc.id) -> "   (paired)"
                else -> ""
            }
            labels.add("💻  ${pc.name}$tag")
            actions.add { wifi.connect(pc) }
        }
        if (wifi.found.isEmpty()) {
            labels.add("Looking for PCs… (open CamBridge on the PC)")
            actions.add { handler.postDelayed({ showWifiDialog() }, 300) }
        }
        labels.add("📷  Scan QR code on the PC")
        actions.add { camera.scanning = true; updateStatus() }
        if (wifi.connectedPC != null) {
            labels.add("Disconnect Wi-Fi")
            actions.add { wifi.disconnect() }
        }
        for (pc in wifi.saved.values.toList()) {
            labels.add("Forget ${pc.name}")
            actions.add { wifi.forget(pc.id) }
        }
        labels.add("ⓘ  About CamBridge & licenses")
        actions.add { showLicenses() }
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle(if (wifi.status.isEmpty()) "Connect over Wi-Fi" else wifi.status)
            .setItems(labels.toTypedArray()) { _, which -> actions[which]() }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun showLicenses() {
        val text = try {
            assets.open("licenses.txt").bufferedReader().use { it.readText() }
        } catch (_: Exception) {
            "CamBridge — free to use. © 2026 ethiogamer."
        }
        val view = android.widget.ScrollView(this).apply {
            addView(TextView(this@MainActivity).apply {
                this.text = text
                textSize = 11f
                typeface = Typeface.MONOSPACE
                setTextColor(Color.rgb(230, 230, 230))
                setPadding(dp(18), dp(12), dp(18), dp(12))
            })
        }
        AlertDialog.Builder(this, android.R.style.Theme_Material_Dialog_Alert)
            .setTitle("CamBridge — license & credits")
            .setView(view)
            .setPositiveButton("Close", null)
            .show()
    }

    override fun onDestroy() {
        handler.removeCallbacks(ticker)
        if (::wifi.isInitialized) wifi.stop()
        camera.stop()
        server.stop()
        super.onDestroy()
    }

    override fun onPause() {
        if (::camera.isInitialized && camera.screenOff) camera.setScreenOff(false)
        super.onPause()
    }

    private fun checkHeat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            camera.hot = pm.currentThermalStatus >= PowerManager.THERMAL_STATUS_SEVERE
        }
    }

    // ------------------------------------------------------------------ UI

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun pill(color: Int, radius: Int = 22): GradientDrawable =
        GradientDrawable().apply { setColor(color); cornerRadius = dp(radius).toFloat() }

    private fun label(text: String, size: Float, bold: Boolean = false, color: Int = Color.WHITE): TextView =
        TextView(this).apply {
            this.text = text
            textSize = size
            setTextColor(color)
            if (bold) typeface = Typeface.DEFAULT_BOLD
        }

    private fun button(text: String, onClick: () -> Unit): TextView =
        label(text, 14f, bold = true).apply {
            gravity = Gravity.CENTER
            background = pill(Color.argb(150, 0, 0, 0))
            setPadding(dp(11), dp(10), dp(11), dp(10))
            setOnClickListener { onClick() }
        }

    @SuppressLint("ClickableViewAccessibility")
    private fun buildUi(): View {
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }

        previewView = PreviewView(this).apply {
            scaleType = PreviewView.ScaleType.FIT_CENTER
            setOnTouchListener { v, e ->
                if (e.action == MotionEvent.ACTION_UP) {
                    if (::camera.isInitialized) camera.focusAt(e.x, e.y)
                    v.performClick()
                }
                true
            }
        }
        root.addView(previewView, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // Status card (top)
        val status = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            background = pill(Color.argb(150, 0, 0, 0), 16)
            setPadding(dp(14), dp(12), dp(14), dp(12))
        }
        statusDot = View(this).apply { background = pill(Color.rgb(245, 158, 11), 6) }
        status.addView(statusDot, LinearLayout.LayoutParams(dp(12), dp(12)).apply { rightMargin = dp(12) })
        val texts = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        statusTitle = label("Waiting for PC", 16f, bold = true)
        statusSub = label("Plug in the cable and open CamBridge on the PC", 12f, color = Color.argb(200, 255, 255, 255))
        texts.addView(statusTitle)
        texts.addView(statusSub)
        status.addView(texts, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        val statusParams = FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP)
        root.addView(status, statusParams)

        // Bottom bar
        val bottom = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
        }
        val bar = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER }
        val gap = { LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply { setMargins(dp(3), 0, dp(3), 0) } }
        bar.addView(button("⟲ Flip") { camera.switchCamera() }, gap())
        torchButton = button("Light") { camera.toggleTorch() }
        bar.addView(torchButton, gap())
        qualityButton = button("1080p") { camera.toggleQuality() }
        bar.addView(qualityButton, gap())
        bar.addView(button("Wi-Fi") { showWifiDialog() }, gap())
        bar.addView(button("Dark") { camera.setScreenOff(true) }, gap())
        bottom.addView(bar)
        bottom.addView(label("Control zoom, exposure, focus and more from CamBridge on your PC.", 11f,
            color = Color.argb(160, 255, 255, 255)).apply { gravity = Gravity.CENTER; setPadding(dp(20), dp(10), dp(20), 0) })
        val bottomParams = FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM)
        root.addView(bottom, bottomParams)

        // QR scanning banner
        scanBanner = label("Point at the QR code on your PC\n\nTap here to cancel", 16f, bold = true).apply {
            gravity = Gravity.CENTER
            background = pill(Color.argb(170, 0, 0, 0), 18)
            setPadding(dp(24), dp(18), dp(24), dp(18))
            visibility = View.GONE
            setOnClickListener { camera.scanning = false; updateStatus() }
        }
        root.addView(scanBanner, FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER))

        // Permission message
        permissionView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.BLACK)
            visibility = View.GONE
            addView(label("Camera access is off", 20f, bold = true).apply { gravity = Gravity.CENTER })
            addView(label("CamBridge needs the camera to work as your PC webcam.", 14f,
                color = Color.argb(190, 255, 255, 255)).apply { gravity = Gravity.CENTER; setPadding(dp(30), dp(10), dp(30), dp(20)) })
            addView(button("Open settings") {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            })
        }
        root.addView(permissionView, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // Dark screen (saves battery) — tap to wake
        darkOverlay = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            visibility = View.GONE
            setOnClickListener { camera.setScreenOff(false) }
        }
        darkText = label("Streaming to PC\nTap anywhere to wake", 13f, color = Color.argb(70, 255, 255, 255)).apply { gravity = Gravity.CENTER }
        darkOverlay.addView(darkText, FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER))
        root.addView(darkOverlay, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // Keep the cards clear of the status bar and navigation bar.
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            statusParams.setMargins(dp(14), bars.top + dp(10), dp(14), 0)
            bottomParams.setMargins(dp(14), 0, dp(14), bars.bottom + dp(16))
            status.layoutParams = statusParams
            bottom.layoutParams = bottomParams
            insets
        }
        return root
    }

    private fun showDark(off: Boolean) {
        runOnUiThread {
            darkOverlay.visibility = if (off) View.VISIBLE else View.GONE
            val lp = window.attributes
            lp.screenBrightness = if (off) 0.01f else WindowManager.LayoutParams.BRIGHTNESS_OVERRIDE_NONE
            window.attributes = lp
        }
    }

    private fun updateStatus() {
        if (!::camera.isInitialized) return
        val connected = server.isConnected
        statusDot.background = pill(if (connected) Color.rgb(34, 197, 94) else Color.rgb(245, 158, 11), 6)
        statusTitle.text = if (connected) "Connected to PC" else "Waiting for PC"
        val how = if (server.isWifi) "Wi-Fi" else "USB"
        if (connected && ::wifi.isInitialized && wifi.connectedPC != null && server.isWifi) {
            statusTitle.text = "Connected to ${wifi.connectedPC}"
        }
        statusSub.text = when {
            connected && camera.hot -> "$fps fps · $how · phone is hot — remove the case"
            connected -> "$fps fps · ${camera.quality} · $how"
            ::wifi.isInitialized && wifi.status.isNotEmpty() -> wifi.status
            else -> "Plug in the cable, or tap Wi-Fi below"
        }
        scanBanner.visibility = if (camera.scanning) View.VISIBLE else View.GONE
        darkText.text = if (connected) "Streaming to PC\nTap anywhere to wake" else "Waiting for PC\nTap anywhere to wake"
        torchButton.visibility = if (camera.hasTorch) View.VISIBLE else View.GONE
        torchButton.background = pill(if (camera.torchOn) Color.rgb(245, 197, 24) else Color.argb(150, 0, 0, 0))
        torchButton.setTextColor(if (camera.torchOn) Color.BLACK else Color.WHITE)
        qualityButton.text = camera.quality
    }
}
