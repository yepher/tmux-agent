import SwiftUI
import WebKit

struct BrowserView: View {
    @Bindable var connection: RoomConnection
    @State private var addressBar: String = "tunnel://localhost/"
    @State private var loadURL: URL? = URL(string: "tunnel://localhost/")
    @State private var reloadToken: Int = 0

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                HStack {
                    TextField("tunnel://localhost/", text: $addressBar)
                        .textFieldStyle(.roundedBorder)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .onSubmit(load)
                    Button("Go", action: load).buttonStyle(.borderedProminent)
                    Button { reloadToken &+= 1 } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.bordered)
                    .disabled(loadURL == nil)
                }
                .padding(.horizontal).padding(.top, 8)

                if connection.state == .connected, let proxy = connection.proxy {
                    TunnelWebView(
                        url: loadURL,
                        proxy: proxy,
                        reloadToken: reloadToken
                    )
                    .ignoresSafeArea(edges: .bottom)
                } else {
                    ContentUnavailableView(
                        "Not connected",
                        systemImage: "network.slash",
                        description: Text(
                            "Connect to the agent on the Agent tab first, then load a tunnel:// URL to see the dev server running on the agent machine."
                        )
                    )
                }
            }
            .navigationTitle("Browser")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func load() {
        guard let url = URL(string: addressBar),
              url.scheme?.lowercased() == TunnelSchemeHandler.scheme
        else { return }
        loadURL = url
        // Bump the token so updateUIView forces a reload even on the same URL.
        reloadToken &+= 1
    }
}

private struct TunnelWebView: UIViewRepresentable {
    let url: URL?
    let proxy: ProxyClient
    let reloadToken: Int

    final class Coordinator {
        var handler: TunnelSchemeHandler?
        var lastLoaded: URL?
        var lastReloadToken: Int = 0
    }
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let handler = TunnelSchemeHandler(proxy: proxy)
        context.coordinator.handler = handler
        let config = WKWebViewConfiguration()
        // Disable WebKit's disk/memory cache so a reload always hits the
        // scheme handler and re-fetches through the tunnel.
        config.websiteDataStore = .nonPersistent()
        // Register BEFORE the WKWebView sees any load for this scheme.
        config.setURLSchemeHandler(handler, forURLScheme: TunnelSchemeHandler.scheme)
        let view = WKWebView(frame: .zero, configuration: config)
        view.allowsBackForwardNavigationGestures = true
        if let url {
            view.load(freshRequest(url))
            context.coordinator.lastLoaded = url
        }
        context.coordinator.lastReloadToken = reloadToken
        return view
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        let urlChanged = url != context.coordinator.lastLoaded
        let reloadBumped = reloadToken != context.coordinator.lastReloadToken
        guard urlChanged || reloadBumped else { return }
        if let url {
            uiView.load(freshRequest(url))
            context.coordinator.lastLoaded = url
        }
        context.coordinator.lastReloadToken = reloadToken
    }

    private func freshRequest(_ url: URL) -> URLRequest {
        URLRequest(
            url: url,
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: 30
        )
    }
}
