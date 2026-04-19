import LiveKit
import SwiftUI

/// SwiftUI wrapper for LiveKit's UIView-based VideoView. Stable across minor
/// SDK versions — we just bind the `track` property. `layoutMode` controls
/// whether the video letterboxes (`.fit`) or crop-fills (`.fill`) its bounds.
struct LiveKitVideoView: UIViewRepresentable {
    let track: VideoTrack?
    var layoutMode: VideoView.LayoutMode = .fit

    func makeUIView(context: Context) -> VideoView {
        let view = VideoView()
        view.layoutMode = layoutMode
        view.mirrorMode = .off
        view.track = track
        return view
    }

    func updateUIView(_ uiView: VideoView, context: Context) {
        if uiView.layoutMode != layoutMode {
            uiView.layoutMode = layoutMode
        }
        if uiView.track !== track {
            uiView.track = track
        }
    }
}
