import SwiftUI

@main
struct CamBridgeApp: App {
    @StateObject private var model = CameraModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .environmentObject(model.wifi)
                .onAppear { model.start() }
                .onOpenURL { url in
                    model.start()
                    model.wifi.handle(url: url)
                }
                .preferredColorScheme(.dark)
        }
    }
}
