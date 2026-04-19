import Foundation
import WebKit

/// Intercepts every `tunnel://` request made by the WKWebView, sends it over
/// LiveKit to the agent via `ProxyClient`, and fulfills the URLSchemeTask with
/// the response.
///
/// WKWebView does not let us intercept `http(s)` directly, and WKWebView on
/// iOS silently ignores `WKWebsiteDataStore.proxyConfigurations` for non-
/// browser apps (the API requires Apple's browser-engine entitlement),
/// so a custom scheme is the only way to tunnel webview traffic on iOS.
/// The webview loads `tunnel://localhost/...` and relative URLs stay inside
/// the scheme; the agent maps them onto `PROXY_TARGET` (default
/// `http://localhost:3000`).
final class TunnelSchemeHandler: NSObject, WKURLSchemeHandler {
    static let scheme = "tunnel"

    private let proxy: ProxyClient
    private var activeTasks: [ObjectIdentifier: Task<Void, Never>] = [:]

    init(proxy: ProxyClient) {
        self.proxy = proxy
        super.init()
    }

    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        let request = urlSchemeTask.request
        let method = request.httpMethod ?? "GET"
        let url = request.url
        var path = url?.path ?? "/"
        if path.isEmpty { path = "/" }
        if let query = url?.query, !query.isEmpty {
            path += "?\(query)"
        }
        let headers = (request.allHTTPHeaderFields ?? [:])
        let body = request.httpBody ?? readHTTPBodyStream(request)

        let taskId = ObjectIdentifier(urlSchemeTask)
        let task = Task { @MainActor in
            do {
                let response = try await proxy.send(
                    method: method, path: path, headers: headers, body: body
                )
                fulfill(task: urlSchemeTask, url: url, response: response)
            } catch is CancellationError {
                // stopURLSchemeTask already called; nothing to fulfill.
            } catch {
                urlSchemeTask.didFailWithError(error)
            }
            activeTasks.removeValue(forKey: taskId)
        }
        activeTasks[taskId] = task
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {
        let taskId = ObjectIdentifier(urlSchemeTask)
        activeTasks.removeValue(forKey: taskId)?.cancel()
    }

    private func fulfill(
        task: WKURLSchemeTask,
        url: URL?,
        response: ProxyClient.Response
    ) {
        let target = url ?? URL(string: "tunnel://localhost/")!
        var httpHeaders = response.headers
        httpHeaders["Content-Length"] = "\(response.body.count)"
        // HTTPURLResponse's initializer returns optional but only returns nil
        // for invalid URLs — `target` is always a valid `tunnel://` URL.
        let httpResp = HTTPURLResponse(
            url: target,
            statusCode: response.status,
            httpVersion: "HTTP/1.1",
            headerFields: httpHeaders
        )!
        task.didReceive(httpResp)
        if !response.body.isEmpty {
            task.didReceive(response.body)
        }
        task.didFinish()
    }

    private func readHTTPBodyStream(_ request: URLRequest) -> Data? {
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var buf = Data()
        let chunk = 8 * 1024
        var temp = [UInt8](repeating: 0, count: chunk)
        while stream.hasBytesAvailable {
            let n = stream.read(&temp, maxLength: chunk)
            if n <= 0 { break }
            buf.append(temp, count: n)
        }
        return buf.isEmpty ? nil : buf
    }
}
