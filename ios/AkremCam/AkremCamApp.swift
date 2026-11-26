import SwiftUI

@main
struct AkremCamApp: App {
    @StateObject private var model = CameraModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .onAppear { model.start() }
                .preferredColorScheme(.dark)
        }
    }
}
