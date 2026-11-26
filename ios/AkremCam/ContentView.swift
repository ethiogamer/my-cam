import AVFoundation
import SwiftUI

struct ContentView: View {
    @EnvironmentObject var model: CameraModel

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

                VStack(spacing: 0) {
                    StatusBar()
                    Spacer()
                    Controls()
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
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

// MARK: - Controls

private struct Controls: View {
    @EnvironmentObject var model: CameraModel

    var body: some View {
        VStack(spacing: 10) {
            if model.availableLenses.count > 1 {
                HStack(spacing: 10) {
                    ForEach(model.availableLenses) { lens in
                        Button {
                            model.lens = lens
                        } label: {
                            Text(lens.rawValue)
                                .font(.system(size: 14, weight: .bold))
                                .frame(minWidth: 52, minHeight: 36)
                                .background(
                                    Capsule().fill(model.lens == lens ? Color.yellow : Color.black.opacity(0.55))
                                )
                                .foregroundColor(model.lens == lens ? .black : .white)
                        }
                    }
                }
            }

            VStack(spacing: 10) {
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

                Picker("Camera", selection: $model.side) {
                    ForEach(CameraSide.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)

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
                    Toggle("Mirror", isOn: $model.mirror)
                        .fixedSize()
                    Spacer()
                    Picker("Rotation", selection: $model.orientationMode) {
                        ForEach(OrientationMode.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.menu)
                }
                .font(.subheadline)
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 16).fill(Color.black.opacity(0.55)))

            Text("Tip: use the Back camera for the best picture. You can control everything from the PC too.")
                .font(.caption2)
                .foregroundColor(.white.opacity(0.6))
                .multilineTextAlignment(.center)
        }
        .foregroundColor(.white)
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
                .frame(width: 42, alignment: .trailing)
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
