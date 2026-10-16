import LiveKit
import SwiftUI

struct RoomView: View {
    @Bindable var connection: RoomConnection
    let settings: SettingsStore

    @State private var showHangupConfirm = false
    @State private var showNewSessionAlert = false
    @State private var newSessionName = ""

    /// iPhone reports a compact vertical size class in landscape.
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    private var isLandscape: Bool { verticalSizeClass == .compact }

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()

                if let track = connection.screenShareTrack {
                    // Portrait: show the landscape terminal sideways so it
                    // fills the screen (turn your head, or the phone).
                    //   1. First frame (h × w) gives the video a landscape-
                    //      shaped canvas as if the phone were rotated.
                    //   2. rotationEffect rotates the whole thing -90° so
                    //      landscape content appears sideways on the screen.
                    //   3. Second frame (w × h) reports the final visual
                    //      size back to SwiftUI layout so it fills the
                    //      portrait screen.
                    // Landscape: iOS has already rotated the UI, so the
                    // extra -90° would turn the text upside down. Show the
                    // video upright, edge to edge (nav/tab bars hidden,
                    // safe areas ignored), aspect-fit so nothing is cropped.
                    GeometryReader { geo in
                        if isLandscape {
                            LiveKitVideoView(track: track, layoutMode: .fit)
                                .frame(width: geo.size.width,
                                       height: geo.size.height)
                        } else {
                            LiveKitVideoView(track: track, layoutMode: .fill)
                                .frame(width: geo.size.height,
                                       height: geo.size.width)
                                .rotationEffect(.degrees(-90))
                                .frame(width: geo.size.width,
                                       height: geo.size.height)
                                .clipped()
                        }
                    }
                    .ignoresSafeArea(edges: isLandscape ? .all : [])
                } else {
                    placeholder
                }

                if let micError = connection.micError {
                    VStack {
                        Spacer()
                        Text(micError)
                            .font(.caption)
                            .foregroundStyle(.white)
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .background(.red.opacity(0.85), in: Capsule())
                            .padding(.horizontal)
                            .padding(.bottom, 100)
                    }
                }

                if connection.state == .connected {
                    VStack {
                        Spacer()
                        HStack {
                            Spacer()
                            micButton
                                .padding(.trailing, 20)
                                .padding(.bottom, 24)
                        }
                    }
                }
            }
            .navigationTitle("Agent")
            .navigationBarTitleDisplayMode(.inline)
            // Landscape is for reading the terminal: give the video the
            // whole screen. Rotate back to portrait for sessions / hang up.
            .toolbar(isLandscape ? .hidden : .visible, for: .navigationBar)
            .toolbar(isLandscape ? .hidden : .visible, for: .tabBar)
            .statusBarHidden(isLandscape)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if connection.state == .connected {
                        sessionsMenu
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    topBarButton
                }
            }
            .confirmationDialog(
                "Hang up?",
                isPresented: $showHangupConfirm,
                titleVisibility: .visible
            ) {
                Button("Hang up", role: .destructive) {
                    Task { await connection.disconnect() }
                }
                Button("Cancel", role: .cancel) {}
            }
            .alert(
                "New tmux session",
                isPresented: $showNewSessionAlert
            ) {
                TextField("name", text: $newSessionName)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                Button("Create") {
                    let name = newSessionName.trimmingCharacters(in: .whitespaces)
                    guard !name.isEmpty else { return }
                    Task { await connection.switchSession(name: name) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("tmux attaches if the session exists, or creates it.")
            }
        }
    }

    private var sessionsMenu: some View {
        Menu {
            ForEach(connection.sessions) { s in
                Button {
                    guard !s.current else { return }
                    Task { await connection.switchSession(name: s.name) }
                } label: {
                    if s.current {
                        Label(s.name, systemImage: "checkmark")
                    } else {
                        Text(s.name)
                    }
                }
            }
            if !connection.sessions.isEmpty {
                Divider()
            }
            Button {
                newSessionName = ""
                showNewSessionAlert = true
            } label: {
                Label("New session…", systemImage: "plus")
            }
            Button {
                Task { await connection.refreshSessions() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
        } label: {
            Image(systemName: "square.stack.3d.up")
                .foregroundStyle(.white)
        }
    }

    @ViewBuilder
    private var topBarButton: some View {
        switch connection.state {
        case .connected:
            Button(role: .destructive) {
                showHangupConfirm = true
            } label: {
                Image(systemName: "phone.down.fill")
                    .foregroundStyle(.red)
            }
        case .connecting:
            ProgressView()
        case .disconnected, .failed:
            Button {
                Task { await connection.connect(settings: settings) }
            } label: {
                Image(systemName: "phone.fill")
                    .foregroundStyle(settings.isComplete ? .green : .gray)
            }
            .disabled(!settings.isComplete)
        }
    }

    private var micButton: some View {
        Button {
            Task { await connection.setMic(!connection.micEnabled) }
        } label: {
            Image(
                systemName: connection.micEnabled
                    ? "mic.fill" : "mic.slash.fill"
            )
            .font(.title2)
            .foregroundStyle(connection.micEnabled ? .white : .white.opacity(0.9))
            .frame(width: 56, height: 56)
            .background(
                connection.micEnabled ? Color.blue : Color.gray.opacity(0.7),
                in: Circle()
            )
            .shadow(radius: 4)
        }
    }

    @ViewBuilder
    private var placeholder: some View {
        switch connection.state {
        case .disconnected:
            VStack(spacing: 12) {
                Image(systemName: "rectangle.on.rectangle.slash")
                    .font(.system(size: 48))
                Text("Not connected").font(.headline)
                Text("Tap Connect to join \(settings.room).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .foregroundStyle(.white)
        case .connecting:
            ProgressView("Connecting…").foregroundStyle(.white)
        case .connected:
            Text("Waiting for console…")
                .foregroundStyle(.white)
        case .failed(let msg):
            VStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                Text("Connection failed").font(.headline)
                Text(msg).font(.caption).multilineTextAlignment(.center)
            }
            .foregroundStyle(.white)
            .padding()
        }
    }
}
