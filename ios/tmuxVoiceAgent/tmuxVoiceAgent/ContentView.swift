import SwiftUI

struct ContentView: View {
    @State private var settings = SettingsStore()
    @State private var connection = RoomConnection()

    var body: some View {
        TabView {
            RoomView(connection: connection, settings: settings)
                .tabItem { Label("Agent", systemImage: "display") }
            BrowserView(connection: connection)
                .tabItem { Label("Browser", systemImage: "safari") }
            SettingsView(settings: settings)
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}

#Preview {
    ContentView()
}
