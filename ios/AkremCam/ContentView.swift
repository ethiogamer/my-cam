import AVFoundation
import SwiftUI

struct ContentView: View {
    @EnvironmentObject var model: CameraModel
    @State private var showPanel = false
    @State private var panelTab = 0

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            if model.permissionDenied {
                PermissionView()
            } else {
                CameraPreview(session: model.session,
                              onFocus: { point in model.focus(at: point) },
                              onPinch: { scale, began in model.pinch(scale: scale, began: began) })
                .ignoresSafeArea()

                VStack(spacing: 10) {
                    StatusBar()
                    Spacer()
                    if showPanel {
                        ControlPanel(tab: $panelTab)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                    Toolbar(showPanel: $showPanel)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .animation(.easeOut(duration: 0.2), value: showPanel)
            }

            if model.screenOff {
                ScreenOffView()
            }
        }
        .statusBarHidden(true)
    }
}

// MARK: - Status

private struct StatusBar: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        VStack(spacing: 8) {
            HStack(spacing: 10) {
                Circle()
                    .fill(model.isConnected ? Color.green : Color.orange)
                    .frame(width: 12, height: 12)
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.isConnected ? "Connected to PC" : "Waiting for PC")
                        .font(.headline)
                    Text(model.isConnected
                         ? "\(model.fps) fps • \(model.quality.rawValue)"
                         : "Plug in the cable and open Akrem Cam on the PC")
                        .font(.caption)
                        .foregroundColor(.white.opacity(0.75))
                }
                Spacer()
                if model.torchAvailable {
                    RoundButton(system: model.torchOn ? "bolt.fill" : "bolt.slash",
                                active: model.torchOn, label: "Light") {
                        model.torchOn.toggle()
                    }
                }
                RoundButton(system: "moon.fill", active: false, label: "Turn screen dark") {
                    model.screenOff = true
                }
            }
            if model.isHot {
                Label("Phone is hot — remove the case or switch to 30 fps", systemImage: "thermometer.sun.fill")
                    .font(.caption.bold())
                    .foregroundColor(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16).fill(Color.black.opacity(0.55)))
        .foregroundColor(.white)
    }
}

private struct RoundButton: View {
    let system: String
    let active: Bool
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: system)
                .font(.system(size: 18, weight: .semibold))
                .frame(width: 44, height: 44)
                .background(Circle().fill(active ? Color.yellow : Color.black.opacity(0.5)))
                .foregroundColor(active ? .black : .white)
        }
        .accessibilityLabel(label)
    }
}

// MARK: - Toolbar (always visible)

private struct Toolbar: View {
    @EnvironmentObject var model: CameraModel
    @Binding var showPanel: Bool

    var body: some View {
        HStack(spacing: 10) {
            RoundButton(system: "arrow.triangle.2.circlepath.camera", active: false, label: "Switch camera") {
                model.side = model.side == .back ? .front : .back
            }
            Spacer(minLength: 0)
            if model.availableLenses.count > 1 {
                HStack(spacing: 6) {
                    ForEach(model.availableLenses) { lens in
                        Button {
                            model.lens = lens
                        } label: {
                            Text(lens.rawValue)
                                .font(.system(size: 13, weight: .bold))
                                .frame(minWidth: 44, minHeight: 34)
                                .background(Capsule().fill(model.lens == lens ? Color.yellow : Color.black.opacity(0.55)))
                                .foregroundColor(model.lens == lens ? .black : .white)
                        }
                    }
                }
            }
            Spacer(minLength: 0)
            RoundButton(system: showPanel ? "chevron.down" : "slider.horizontal.3", active: showPanel,
                        label: "Camera settings") {
                showPanel.toggle()
            }
        }
    }
}

// MARK: - Settings panel

private struct ControlPanel: View {
    @EnvironmentObject var model: CameraModel
    @Binding var tab: Int

    var body: some View {
        VStack(spacing: 10) {
            Picker("", selection: $tab) {
                Text("Basic").tag(0)
                Text("Pro").tag(1)
            }
            .pickerStyle(.segmented)

            ScrollView(showsIndicators: false) {
                VStack(spacing: 12) {
                    if tab == 0 { BasicControls() } else { ProControls() }
                }
                .padding(.vertical, 2)
            }
            .frame(maxHeight: 290)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 18).fill(Color.black.opacity(0.7)))
        .foregroundColor(.white)
    }
}

private struct BasicControls: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        if model.maxZoom > 1.01 {
            SliderRow(icon: "plus.magnifyingglass",
                      value: Binding(get: { Double(model.zoom) }, set: { model.zoom = CGFloat($0) }),
                      range: 1...Double(model.maxZoom),
                      text: String(format: "%.1fx", Double(model.zoom)))
        }
        SliderRow(icon: "sun.max.fill",
                  value: Binding(get: { Double(model.exposureBias) }, set: { model.exposureBias = Float($0) }),
                  range: -Double(CameraModel.exposureLimit)...Double(CameraModel.exposureLimit),
                  text: String(format: "%+.1f", Double(model.exposureBias)))
            .disabled(model.exposureManual)
        if model.torchAvailable && model.torchOn {
            SliderRow(icon: "flashlight.on.fill",
                      value: Binding(get: { Double(model.torchLevel) }, set: { model.torchLevel = Float($0) }),
                      range: 0.05...1.0,
                      text: String(format: "%d%%", Int(model.torchLevel * 100)))
        }
        HStack(spacing: 8) {
            Picker("Quality", selection: $model.quality) {
                ForEach(VideoQuality.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            Picker("Frame rate", selection: $model.frameRate) {
                ForEach(FrameRate.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
        }
        HStack {
            Toggle("Mirror", isOn: $model.mirror).fixedSize()
            Spacer()
            Picker("Rotation", selection: $model.orientationMode) {
                ForEach(OrientationMode.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.menu)
        }
        .font(.subheadline)
    }
}

private struct ProControls: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        // Focus
        ModeHeader(title: "Focus", manual: $model.focusManual, enabled: model.manualFocusSupported,
                   live: model.liveLens >= 0 ? String(format: "%.2f", Double(model.liveLens)) : "")
        if model.focusManual {
            SliderRow(icon: "scope",
                      value: Binding(get: { Double(model.lensPosition) }, set: { model.lensPosition = Float($0) }),
                      range: 0...1, text: String(format: "%.2f", Double(model.lensPosition)))
        }

        // Exposure
        ModeHeader(title: "Exposure", manual: $model.exposureManual, enabled: model.manualExposureSupported,
                   live: model.liveISO > 0 ? "ISO \(Int(model.liveISO)) · \(shutterText(model.liveShutter))" : "")
        if model.exposureManual {
            SliderRow(icon: "camera.aperture",
                      value: Binding(get: { Double(model.iso) }, set: { model.iso = Float($0) }),
                      range: Double(model.isoRange.lowerBound)...Double(model.isoRange.upperBound),
                      text: "\(Int(model.iso))")
            SliderRow(icon: "timer",
                      value: Binding(get: { shutterToSlider(model.shutter) },
                                     set: { model.shutter = sliderToShutter($0) }),
                      range: 0...1, text: shutterText(model.shutter))
        }

        // White balance
        ModeHeader(title: "White balance", manual: $model.whiteBalanceManual,
                   enabled: model.manualWhiteBalanceSupported,
                   live: model.liveTemperature > 0 ? "\(Int(model.liveTemperature))K" : "")
        if model.whiteBalanceManual {
            SliderRow(icon: "thermometer",
                      value: Binding(get: { Double(model.temperature) }, set: { model.temperature = Float($0) }),
                      range: 2500...9000, text: "\(Int(model.temperature))K")
            SliderRow(icon: "drop.fill",
                      value: Binding(get: { Double(model.tint) }, set: { model.tint = Float($0) }),
                      range: -100...100, text: "\(Int(model.tint))")
        }

        if model.stabilizationSupported {
            Toggle("Stabilization (adds a little delay)", isOn: $model.stabilization)
                .font(.subheadline)
        }
        if model.hdrSupported {
            Toggle("HDR video", isOn: $model.hdr)
                .font(.subheadline)
        }
    }

    // Shutter slider is logarithmic so short and long speeds are both easy to pick.
    private func shutterToSlider(_ seconds: Double) -> Double {
        let lo = log(model.shutterRange.lowerBound), hi = log(model.shutterRange.upperBound)
        guard hi > lo else { return 0 }
        return (log(max(seconds, 1e-6)) - lo) / (hi - lo)
    }

    private func sliderToShutter(_ value: Double) -> Double {
        let lo = log(model.shutterRange.lowerBound), hi = log(model.shutterRange.upperBound)
        return exp(lo + value * (hi - lo))
    }
}

func shutterText(_ seconds: Double) -> String {
    guard seconds > 0 else { return "" }
    if seconds >= 0.5 { return String(format: "%.1fs", seconds) }
    return "1/\(Int((1.0 / seconds).rounded()))"
}

private struct ModeHeader: View {
    let title: String
    @Binding var manual: Bool
    let enabled: Bool
    let live: String

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.subheadline.bold())
                if !live.isEmpty {
                    Text(live).font(.caption2.monospacedDigit()).foregroundColor(.white.opacity(0.6))
                }
            }
            Spacer()
            Picker(title, selection: $manual) {
                Text("Auto").tag(false)
                Text("Manual").tag(true)
            }
            .pickerStyle(.segmented)
            .frame(width: 150)
            .disabled(!enabled)
        }
    }
}

private struct SliderRow: View {
    let icon: String
    let value: Binding<Double>
    let range: ClosedRange<Double>
    let text: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .frame(width: 22)
            Slider(value: value, in: range)
                .tint(.yellow)
            Text(text)
                .font(.caption.monospacedDigit())
                .frame(width: 50, alignment: .trailing)
        }
    }
}

// MARK: - Screen off

private struct ScreenOffView: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            VStack(spacing: 8) {
                Text(model.isConnected ? "Streaming to PC" : "Waiting for PC")
                Text("Tap anywhere to wake")
                    .font(.caption)
            }
            .foregroundColor(.white.opacity(0.25))
        }
        .contentShape(Rectangle())
        .onTapGesture { model.screenOff = false }
    }
}

// MARK: - Permission

private struct PermissionView: View {
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "camera.fill")
                .font(.system(size: 44))
            Text("Camera access is off")
                .font(.title2.bold())
            Text("Akrem Cam needs the camera to work as your PC webcam.")
                .multilineTextAlignment(.center)
                .foregroundColor(.white.opacity(0.75))
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) {
                    UIApplication.shared.open(url)
                }
            }
            .buttonStyle(.borderedProminent)
        }
        .foregroundColor(.white)
        .padding(30)
    }
}

// MARK: - Live preview

final class PreviewView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    let onFocus: (CGPoint) -> Void
    let onPinch: (CGFloat, Bool) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onFocus: onFocus, onPinch: onPinch) }

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.backgroundColor = .black
        view.previewLayer.session = session
        view.previewLayer.videoGravity = .resizeAspect
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped(_:)))
        view.addGestureRecognizer(tap)
        let pinch = UIPinchGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.pinched(_:)))
        view.addGestureRecognizer(pinch)
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}

    final class Coordinator: NSObject {
        let onFocus: (CGPoint) -> Void
        let onPinch: (CGFloat, Bool) -> Void
        init(onFocus: @escaping (CGPoint) -> Void, onPinch: @escaping (CGFloat, Bool) -> Void) {
            self.onFocus = onFocus
            self.onPinch = onPinch
        }

        @objc func pinched(_ gesture: UIPinchGestureRecognizer) {
            switch gesture.state {
            case .began: onPinch(gesture.scale, true)
            case .changed: onPinch(gesture.scale, false)
            default: break
            }
        }

        @objc func tapped(_ gesture: UITapGestureRecognizer) {
            guard let view = gesture.view as? PreviewView else { return }
            let point = gesture.location(in: view)
            let devicePoint = view.previewLayer.captureDevicePointConverted(fromLayerPoint: point)
            onFocus(devicePoint)
        }
    }
}
