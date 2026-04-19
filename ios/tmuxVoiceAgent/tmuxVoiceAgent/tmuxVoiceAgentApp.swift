//
//  tmuxVoiceAgentApp.swift
//  tmuxVoiceAgent
//
//  Created by Chris Wilson on 4/18/26.
//

import SwiftUI

@main
struct tmuxVoiceAgentApp: App {
    // UIKit AppDelegate lives alongside the SwiftUI lifecycle so we can
    // override supportedInterfaceOrientationsFor per-scene. Currently the
    // app stays in portrait everywhere (the inner video view rotates
    // itself in RoomView), so OrientationLock.shared.mask stays .all.
    // Kept here so `.lockedOrientation(...)` can be re-enabled without
    // re-plumbing the delegate.
    @UIApplicationDelegateAdaptor(TmuxAppDelegate.self) var appDelegate

    // LiveKit's AudioManager configures AVAudioSession itself when a track is
    // published. We explicitly do NOT touch AVAudioSession here — fighting
    // with the SDK over category/mode caused the session to go silent.

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
