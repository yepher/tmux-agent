// This file held the in-app SOCKS5 / HTTP proxy for the short-lived
// attempt to use `WKWebsiteDataStore.proxyConfigurations` as an app-scoped
// VPN. That approach doesn't work on iOS for non-browser apps (the property
// is silently ignored without Apple's browser-engine entitlement), so the
// project reverted to the custom `tunnel://` URL scheme in
// `TunnelSchemeHandler.swift`. This file is intentionally left empty so the
// Xcode project file reference keeps compiling; you can remove it from the
// project at your convenience.
