import SwiftUI
import UIKit

/// Shared orientation mask consulted by AppDelegate. A view that wants to
/// force a specific orientation sets this and the system calls back into us
/// whenever it asks the app what orientations it currently supports.
@Observable
final class OrientationLock {
    static let shared = OrientationLock()
    var mask: UIInterfaceOrientationMask = .all

    private init() {}

    /// Update the preferred orientation mask and push the change through to
    /// the active window scene so the device rotates now, not on the next
    /// user-initiated event.
    @MainActor
    func set(_ newMask: UIInterfaceOrientationMask) {
        mask = newMask
        guard let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first(where: { $0.activationState == .foregroundActive })
                ?? (UIApplication.shared.connectedScenes.first as? UIWindowScene)
        else { return }
        scene.requestGeometryUpdate(.iOS(interfaceOrientations: newMask))
        scene.windows.forEach {
            $0.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
        }
    }
}

/// Subclass whose `supportedInterfaceOrientations` is read live from the
/// `OrientationLock.shared.mask`. Used as the app delegate's orientation hook.
final class TmuxAppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        supportedInterfaceOrientationsFor window: UIWindow?
    ) -> UIInterfaceOrientationMask {
        OrientationLock.shared.mask
    }
}

/// Apply to any view that should force a specific interface-orientation mask
/// while on screen, and release it back to `.all` on exit.
struct LockedOrientation: ViewModifier {
    let mask: UIInterfaceOrientationMask

    func body(content: Content) -> some View {
        content
            .onAppear { OrientationLock.shared.set(mask) }
            .onDisappear { OrientationLock.shared.set(.all) }
    }
}

extension View {
    /// Force a particular orientation while this view is on screen.
    /// E.g. `.lockedOrientation([.landscapeLeft, .landscapeRight])`.
    func lockedOrientation(_ mask: UIInterfaceOrientationMask) -> some View {
        modifier(LockedOrientation(mask: mask))
    }
}
