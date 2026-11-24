import AppKit
import ApplicationServices
import Foundation

func queryFrontmostWindowNumber(pid: Int) -> Int {
    guard pid > 0 else { return 0 }
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return 0
    }
    for entry in list {
        let ownerPid = (entry[kCGWindowOwnerPID as String] as? Int) ?? ((entry[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0)
        let layer = (entry[kCGWindowLayer as String] as? Int) ?? ((entry[kCGWindowLayer as String] as? NSNumber)?.intValue ?? -1)
        if ownerPid == pid && layer == 0 {
            return (entry[kCGWindowNumber as String] as? Int) ?? ((entry[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0)
        }
    }
    return 0
}

private let kMaxWebAreaFieldLen = 512
let kRejectedWebAreaUrl = "about:rejected"

func shouldProbeAxForEventKind(_ kind: String) -> Bool {
    return kind == "ptt_start" || kind == "toggle"
}

func shouldProbeWebAreaForEventKind(_ kind: String) -> Bool {
    return kind == "ptt_start" || kind == "toggle"
}

func shouldProbeWebAreaTitle(bundleId: String, windowTitle: String, allowDeepProbe: Bool = true) -> Bool {
    guard allowDeepProbe else { return false }
    guard bundleId.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == "com.anthropic.claudefordesktop" else {
        return false
    }
    _ = windowTitle
    return true
}

func sanitizeWebAreaTitle(_ raw: String) -> String {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty && trimmed.count <= kMaxWebAreaFieldLen else {
        return ""
    }
    if trimmed.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) {
        return ""
    }
    return trimmed
}

private func isClaudeTrustedOrigin(_ comps: URLComponents) -> Bool {
    guard comps.port == nil,
          comps.user == nil,
          comps.password == nil,
          comps.percentEncodedUser == nil,
          comps.percentEncodedPassword == nil else {
        return false
    }
    guard let scheme = comps.scheme?.lowercased(),
          let host = comps.host?.lowercased() else {
        return false
    }
    return (scheme == "app" && host == "localhost") || (scheme == "https" && host == "claude.ai")
}

func sanitizeWebAreaUrl(_ raw: String) -> String {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else {
        return ""
    }
    if trimmed.lowercased().hasPrefix("file://") {
        return ""
    }
    guard trimmed.count <= kMaxWebAreaFieldLen,
          !trimmed.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
        return kRejectedWebAreaUrl
    }
    guard var comps = URLComponents(string: trimmed),
          isClaudeTrustedOrigin(comps),
          let scheme = comps.scheme?.lowercased(),
          let host = comps.host?.lowercased() else {
        return kRejectedWebAreaUrl
    }
    comps.scheme = scheme
    comps.host = host
    comps.query = nil
    comps.fragment = nil
    guard let cleaned = comps.string,
          !cleaned.isEmpty,
          cleaned.count <= kMaxWebAreaFieldLen,
          !cleaned.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
        return kRejectedWebAreaUrl
    }
    return cleaned
}

func isClaudeShellWebArea(urlRaw: String, title: String) -> Bool {
    let lowUrl = urlRaw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if lowUrl.hasPrefix("file://") {
        return true
    }
    let cleanUrl = sanitizeWebAreaUrl(urlRaw)
    if !cleanUrl.isEmpty {
        return false
    }
    return sanitizeWebAreaTitle(title).isEmpty
}

private func isClaudeWebAreaTitleSuffix(_ title: String) -> Bool {
    let trimmed = sanitizeWebAreaTitle(title)
    guard !trimmed.isEmpty else { return false }
    let low = trimmed.lowercased()
    for suffix in ["claude code", "claude"] {
        for sep in [" - ", " \u{2013} ", " \u{2014} "] {
            let tail = sep + suffix
            if low.hasSuffix(tail) {
                let prefix = trimmed.dropLast(tail.count).trimmingCharacters(in: .whitespacesAndNewlines)
                if !prefix.isEmpty {
                    return true
                }
            }
        }
    }
    return false
}

private func isGenericClaudeWebAreaTitle(_ trimmed: String) -> Bool {
    let low = trimmed.lowercased()
    if low.isEmpty || low == "claude" || low == "claude code" {
        return true
    }
    let dashSet = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: "-\u{2013}\u{2014}"))
    let stripped = low.trimmingCharacters(in: dashSet)
    return stripped.isEmpty || stripped == "claude" || stripped == "claude code"
}

func selectPreferredWebAreaTitle(_ candidates: [String]) -> String {
    var fallback = ""
    for raw in candidates {
        let trimmed = sanitizeWebAreaTitle(raw)
        if isGenericClaudeWebAreaTitle(trimmed) {
            continue
        }
        if isClaudeWebAreaTitleSuffix(trimmed) {
            return trimmed
        }
        if fallback.isEmpty {
            fallback = trimmed
        }
    }
    return fallback
}

private func isClaudeTrustedOriginUrl(_ cleanUrl: String) -> Bool {
    guard let comps = URLComponents(string: cleanUrl) else { return false }
    return isClaudeTrustedOrigin(comps)
}

private func isStrictUuid36(_ s: Substring) -> Bool {
    let bytes = Array(s.utf8)
    guard bytes.count == 36 else { return false }
    for (idx, b) in bytes.enumerated() {
        if idx == 8 || idx == 13 || idx == 18 || idx == 23 {
            if b != 0x2D { return false }
        } else {
            let isDigit = (b >= 0x30 && b <= 0x39)
            let isLowerHex = (b >= 0x61 && b <= 0x66)
            let isUpperHex = (b >= 0x41 && b <= 0x46)
            if !(isDigit || isLowerHex || isUpperHex) { return false }
        }
    }
    return true
}

func isClaudeSessionRouteUrl(_ cleanUrl: String) -> Bool {
    guard let comps = URLComponents(string: cleanUrl),
          isClaudeTrustedOrigin(comps) else {
        return false
    }
    var path = comps.percentEncodedPath
    if path.hasSuffix("/") {
        path.removeLast()
    }
    guard path.hasPrefix("/") else { return false }
    let segs = path.dropFirst().split(separator: "/", omittingEmptySubsequences: false)
    guard segs.count == 2 else { return false }
    let mode = segs[0].lowercased()
    guard mode == "cowork" || mode == "epitaxy" || mode == "code" else { return false }
    let idSeg = segs[1]
    guard idSeg.count == 42, idSeg.lowercased().hasPrefix("local_") else { return false }
    return isStrictUuid36(idSeg.dropFirst(6))
}

func selectPreferredWebAreaResult(
    _ candidates: [(title: String, pageUrl: String)]
) -> (title: String, pageUrl: String) {
    var bestAppSession: (title: String, pageUrl: String)? = nil
    var bestTrustedOther: (title: String, pageUrl: String)? = nil
    var bestWeb: (title: String, pageUrl: String)? = nil
    var titleOnlyList: [String] = []
    for raw in candidates {
        let cleanTitle = sanitizeWebAreaTitle(raw.title)
        let cleanUrl = sanitizeWebAreaUrl(raw.pageUrl)
        if cleanTitle.isEmpty && cleanUrl.isEmpty {
            continue
        }
        if !cleanUrl.isEmpty {
            if isClaudeSessionRouteUrl(cleanUrl) {
                if bestAppSession == nil || (isClaudeWebAreaTitleSuffix(cleanTitle) && !isClaudeWebAreaTitleSuffix(bestAppSession!.title)) {
                    bestAppSession = (cleanTitle, cleanUrl)
                }
            } else if isClaudeTrustedOriginUrl(cleanUrl) {
                if bestTrustedOther == nil || (!cleanTitle.isEmpty && bestTrustedOther!.title.isEmpty) {
                    bestTrustedOther = (cleanTitle, cleanUrl)
                }
            } else if bestWeb == nil {
                bestWeb = (cleanTitle, cleanUrl)
            }
        } else {
            titleOnlyList.append(cleanTitle)
        }
    }
    if let hit = bestAppSession { return hit }
    if let hit = bestTrustedOther { return hit }
    if let hit = bestWeb { return hit }
    return (selectPreferredWebAreaTitle(titleOnlyList), "")
}

private func readAxUrlString(_ el: AXUIElement) -> String {
    var urlRef: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, "AXURL" as CFString, &urlRef) == .success,
          let u = urlRef else {
        return ""
    }
    if let s = u as? String { return s }
    if let url = u as? URL { return url.absoluteString }
    if let nsUrl = u as? NSURL { return nsUrl.absoluteString ?? "" }
    return ""
}

func resolveClaudeWebAreaDeadlineMs(
    nowMs: UInt64,
    budgetMs: UInt64,
    outerDeadlineMonoMs: UInt64?
) -> UInt64 {
    let ownDeadlineMs = nowMs &+ budgetMs
    guard let outer = outerDeadlineMonoMs else { return ownDeadlineMs }
    return min(outer, ownDeadlineMs)
}

private func queryClaudeWebAreaMetadata(
    axWin: AXUIElement,
    maxDepth: Int = 12,
    maxNodes: Int = 96,
    budgetMs: UInt64 = 80,
    deadlineMonoMs: UInt64? = nil
) -> (title: String, pageUrl: String) {
    let containerRoles: Set<String> = [
        "AXWindow",
        "AXGroup",
        "AXSplitGroup",
        "AXTabGroup",
        "AXScrollArea",
        "AXSheet",
        "AXLayoutArea",
        "AXUnknown",
    ]
    let deadlineMs = resolveClaudeWebAreaDeadlineMs(
        nowMs: monotonicMs(),
        budgetMs: budgetMs,
        outerDeadlineMonoMs: deadlineMonoMs
    )
    func applyRemainingTimeout(_ el: AXUIElement) -> Bool {
        let now = monotonicMs()
        if now >= deadlineMs { return false }
        let remMs = deadlineMs &- now
        if remMs <= 5 { return false }
        let timeoutSec = Float(min(remMs, budgetMs)) / 1000.0
        _ = AXUIElementSetMessagingTimeout(el, timeoutSec)
        return true
    }

    var queue: [(AXUIElement, Int)] = [(axWin, 0)]
    var head = 0
    var visited = 0
    var candidates: [(title: String, pageUrl: String)] = []
    while head < queue.count && visited < maxNodes {
        let (cur, depth) = queue[head]
        head += 1
        visited += 1
        guard applyRemainingTimeout(cur) else { break }
        var roleRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(cur, kAXRoleAttribute as CFString, &roleRef) == .success,
              let role = roleRef as? String else {
            continue
        }
        if role == "AXWebArea" {
            guard applyRemainingTimeout(cur) else { break }
            let rawUrl = readAxUrlString(cur)
            let cleanUrl = sanitizeWebAreaUrl(rawUrl)
            guard applyRemainingTimeout(cur) else {
                if !cleanUrl.isEmpty {
                    candidates.append(("", cleanUrl))
                }
                break
            }
            var titleRef: CFTypeRef?
            let rawTitle: String = {
                if AXUIElementCopyAttributeValue(cur, kAXTitleAttribute as CFString, &titleRef) == .success,
                   let s = titleRef as? String {
                    return s
                }
                return ""
            }()
            let cleanTitle = sanitizeWebAreaTitle(rawTitle)
            if !cleanUrl.isEmpty || !cleanTitle.isEmpty {
                candidates.append((cleanTitle, cleanUrl))
                if isClaudeSessionRouteUrl(cleanUrl) && isClaudeWebAreaTitleSuffix(cleanTitle) {
                    return (cleanTitle, cleanUrl)
                }
            }
            // Allow descending through the outer file:// Electron shell AXWebArea (depth 8 -> nested content AXWebArea at depth 9),
            // but never descend into a business content AXWebArea's internal DOM.
            if !(depth < maxDepth && isClaudeShellWebArea(urlRaw: rawUrl, title: rawTitle)) {
                continue
            }
        } else if !(depth < maxDepth && containerRoles.contains(role)) {
            continue
        }
        guard applyRemainingTimeout(cur) else { break }
        var childrenRef: CFTypeRef?
        guard AXUIElementCopyAttributeValue(cur, kAXChildrenAttribute as CFString, &childrenRef) == .success,
              let rawChildren = childrenRef as? [AnyObject] else {
            continue
        }
        var enqueued = 0
        for item in rawChildren {
            if enqueued >= 24 { break }
            guard CFGetTypeID(item) == AXUIElementGetTypeID() else { continue }
            let child = unsafeBitCast(item, to: AXUIElement.self)
            if CFEqual(child, cur) || CFEqual(child, axWin) { continue }
            queue.append((child, depth + 1))
            enqueued += 1
        }
    }
    return selectPreferredWebAreaResult(candidates)
}

func queryFrontWindowMetadata(
    pid: Int,
    bundleId: String,
    pinnedWindowId: Int? = nil,
    allowAxProbe: Bool = true,
    allowDeepWebAreaProbe: Bool = true,
    axBudgetMs: UInt64 = 200,
    liveWindowNumberProvider: (Int) -> Int = queryFrontmostWindowNumber
) -> (hwnd: Int, title: String, document: String, pageUrl: String, axTrusted: Bool) {
    let trusted = AXIsProcessTrusted()
    guard pid > 0 else { return (pinnedWindowId ?? 0, "", "", "", trusted) }
    let effectiveWinNum: Int
    if let pinned = pinnedWindowId {
        if pinned <= 0 {
            return (0, "", "", "", trusted)
        }
        if !allowAxProbe {
            return (pinned, "", "", "", trusted)
        }
        let liveWinNum = liveWindowNumberProvider(pid)
        if liveWinNum != pinned {
            return (pinned, "", "", "", trusted)
        }
        effectiveWinNum = pinned
    } else {
        effectiveWinNum = liveWindowNumberProvider(pid)
    }
    var axTitle = ""
    var axDoc = ""
    var axPageUrl = ""
    if allowAxProbe && bundleId.lowercased() != "com.mitchellh.ghostty" && trusted {
        let deadlineMs = monotonicMs() &+ max(30, axBudgetMs)
        let isClaudeDeepProbe = shouldProbeWebAreaTitle(bundleId: bundleId, windowTitle: "", allowDeepProbe: allowDeepWebAreaProbe)
        func applyOuterTimeout(_ el: AXUIElement, maxCallSec: Float = 0.15) -> Bool {
            let now = monotonicMs()
            if now >= deadlineMs { return false }
            let remMs = deadlineMs &- now
            if remMs <= 5 { return false }
            let capSec = min(Float(remMs) / 1000.0, maxCallSec)
            _ = AXUIElementSetMessagingTimeout(el, capSec)
            return true
        }

        let axApp = AXUIElementCreateApplication(pid_t(pid))
        var winRef: CFTypeRef?
        var st: AXError = .failure
        let winCallCap: Float = isClaudeDeepProbe ? 0.07 : 0.15
        if applyOuterTimeout(axApp, maxCallSec: winCallCap) {
            st = AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &winRef)
        }
        if (st != .success || winRef == nil) && st != .cannotComplete && applyOuterTimeout(axApp, maxCallSec: winCallCap) {
            st = AXUIElementCopyAttributeValue(axApp, kAXMainWindowAttribute as CFString, &winRef)
        }
        if st == .success, let w = winRef, CFGetTypeID(w) == AXUIElementGetTypeID() {
            let axWin = unsafeBitCast(w, to: AXUIElement.self)
            var titleStatus: AXError = .failure
            let titleCallCap: Float = isClaudeDeepProbe ? 0.05 : 0.15
            if applyOuterTimeout(axWin, maxCallSec: titleCallCap) {
                var titleRef: CFTypeRef?
                titleStatus = AXUIElementCopyAttributeValue(axWin, kAXTitleAttribute as CFString, &titleRef)
                if titleStatus == .success, let s = titleRef as? String {
                    axTitle = s.trimmingCharacters(in: .whitespacesAndNewlines)
                }
            }
            // Claude Desktop routes via AXWebArea URL/title; skip AXDocument to reserve the shared 200ms budget for WebArea routing.
            if !isClaudeDeepProbe && titleStatus != .cannotComplete && applyOuterTimeout(axWin) {
                var docRef: CFTypeRef?
                if AXUIElementCopyAttributeValue(axWin, kAXDocumentAttribute as CFString, &docRef) == .success, let d = docRef {
                    let s = (d as? String) ?? (d as? URL)?.absoluteString ?? (d as? NSURL)?.absoluteString ?? ""
                    let rawDoc = s.trimmingCharacters(in: .whitespacesAndNewlines)
                    if rawDoc.lowercased().hasPrefix("file://") {
                        axDoc = rawDoc
                    }
                }
            }
            // Share the exact same absolute deadlineMs (no extra +80ms over budget) and still probe WebArea even if
            // outer AXTitle returned .cannotComplete so Cowork/Code page_url routing can still succeed within budget.
            if isClaudeDeepProbe && monotonicMs() < deadlineMs {
                let webMeta = queryClaudeWebAreaMetadata(
                    axWin: axWin,
                    maxDepth: 12,
                    maxNodes: 96,
                    budgetMs: 80,
                    deadlineMonoMs: deadlineMs
                )
                if !webMeta.title.isEmpty {
                    axTitle = webMeta.title
                }
                if !webMeta.pageUrl.isEmpty {
                    axPageUrl = webMeta.pageUrl
                }
            }
            if let pinned = pinnedWindowId, pinned > 0, liveWindowNumberProvider(pid) != pinned {
                return (pinned, "", "", "", trusted)
            }
        }
    }
    return (effectiveWinNum, axTitle, axDoc, axPageUrl, trusted)
}

func snapshotFrontmostAppBasic() -> (name: String, bundleId: String, pid: Int) {
    guard let app = NSWorkspace.shared.frontmostApplication else { return ("", "", 0) }
    let name = app.localizedName ?? app.bundleURL?.deletingPathExtension().lastPathComponent ?? ""
    let bundleId = app.bundleIdentifier ?? ""
    let pid = Int(app.processIdentifier)
    return (name, bundleId, pid)
}

final class FrontmostWindowFrameCache {
    static let shared = FrontmostWindowFrameCache()
    private let lock = NSLock()
    private let bgQueue = DispatchQueue(label: "agent_voice_input.hud_frame_ax", qos: .utility)
    private var cachedPid: Int = 0
    private var cachedWindowId: Int = 0
    private var cachedFrame: CGRect? = nil
    private var cachedAtMs: UInt64 = 0
    private var refreshInFlightPid: Int = 0
    private var refreshInFlightWindowId: Int = 0
    private var pendingCallbacks: [(CGRect?) -> Void] = []

    func freshFrame(forPid pid: Int, windowId: Int, maxAgeMs: UInt64 = 2500, nowMs: UInt64 = monotonicMs()) -> CGRect? {
        guard pid > 0 && windowId > 0 else { return nil }
        lock.lock()
        defer { lock.unlock() }
        if cachedPid == pid && cachedWindowId == windowId,
           let f = cachedFrame,
           nowMs >= cachedAtMs,
           (nowMs &- cachedAtMs) <= maxAgeMs {
            return f
        }
        return nil
    }

    func storeFrame(pid: Int, windowId: Int, frame: CGRect?, nowMs: UInt64 = monotonicMs()) {
        guard pid > 0 && windowId > 0 else { return }
        lock.lock()
        if let f = frame {
            cachedPid = pid
            cachedWindowId = windowId
            cachedFrame = f
            cachedAtMs = nowMs
        } else if cachedPid == pid && cachedWindowId == windowId {
            cachedFrame = nil
            cachedAtMs = 0
        }
        lock.unlock()
    }

    func refreshAsync(forPid pid: Int, windowId: Int, onResolved: ((CGRect?) -> Void)? = nil) {
        guard pid > 0 && windowId > 0 && pid != Int(ProcessInfo.processInfo.processIdentifier) else { return }
        guard AXIsProcessTrusted() else { return }
        lock.lock()
        if refreshInFlightPid == pid && refreshInFlightWindowId == windowId {
            if let cb = onResolved {
                pendingCallbacks.append(cb)
            }
            lock.unlock()
            return
        }
        refreshInFlightPid = pid
        refreshInFlightWindowId = windowId
        pendingCallbacks.removeAll()
        if let cb = onResolved {
            pendingCallbacks.append(cb)
        }
        lock.unlock()

        bgQueue.async { [weak self] in
            let frame = queryWindowAxFrameOffMain(pid: pid)
            let stillSameWin = (queryCgFrontmostWindowMatch(forPid: pid)?.windowId == windowId)
            let validatedFrame = stillSameWin ? frame : nil
            guard let self = self else { return }
            let ts = monotonicMs()
            self.lock.lock()
            if stillSameWin {
                if let f = validatedFrame {
                    self.cachedPid = pid
                    self.cachedWindowId = windowId
                    self.cachedFrame = f
                    self.cachedAtMs = ts
                } else if self.cachedPid == pid && self.cachedWindowId == windowId {
                    self.cachedFrame = nil
                    self.cachedAtMs = 0
                }
            }
            var callbacks: [(CGRect?) -> Void] = []
            if self.refreshInFlightPid == pid && self.refreshInFlightWindowId == windowId {
                self.refreshInFlightPid = 0
                self.refreshInFlightWindowId = 0
                callbacks = self.pendingCallbacks
                self.pendingCallbacks.removeAll()
            }
            self.lock.unlock()
            if !callbacks.isEmpty {
                DispatchQueue.main.async {
                    for cb in callbacks {
                        cb(validatedFrame)
                    }
                }
            }
        }
    }
}

private func queryWindowAxFrameOffMain(pid: Int) -> CGRect? {
    guard pid > 0 && AXIsProcessTrusted() else { return nil }
    let deadlineMs = monotonicMs() &+ 150
    func applyTimeout(_ el: AXUIElement) -> Bool {
        let now = monotonicMs()
        if now >= deadlineMs { return false }
        let remMs = deadlineMs &- now
        if remMs <= 5 { return false }
        _ = AXUIElementSetMessagingTimeout(el, min(Float(remMs) / 1000.0, 0.10))
        return true
    }
    let axApp = AXUIElementCreateApplication(pid_t(pid))
    var winRef: CFTypeRef?
    var st: AXError = .failure
    if applyTimeout(axApp) {
        st = AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &winRef)
    }
    if (st != .success || winRef == nil) && st != .cannotComplete && applyTimeout(axApp) {
        st = AXUIElementCopyAttributeValue(axApp, kAXMainWindowAttribute as CFString, &winRef)
    }
    guard st == .success, let w = winRef, CFGetTypeID(w) == AXUIElementGetTypeID() else {
        return nil
    }
    let axWin = unsafeBitCast(w, to: AXUIElement.self)
    var posRef: CFTypeRef?
    var sizeRef: CFTypeRef?
    var pos = CGPoint.zero
    var size = CGSize.zero
    guard applyTimeout(axWin),
          AXUIElementCopyAttributeValue(axWin, kAXPositionAttribute as CFString, &posRef) == .success,
          let pVal = posRef, CFGetTypeID(pVal) == AXValueGetTypeID(),
          AXValueGetValue(unsafeBitCast(pVal, to: AXValue.self), .cgPoint, &pos),
          applyTimeout(axWin),
          AXUIElementCopyAttributeValue(axWin, kAXSizeAttribute as CFString, &sizeRef) == .success,
          let sVal = sizeRef, CFGetTypeID(sVal) == AXValueGetTypeID(),
          AXValueGetValue(unsafeBitCast(sVal, to: AXValue.self), .cgSize, &size),
          size.width >= 20.0 && size.height >= 20.0 else {
        return nil
    }
    return CGRect(origin: pos, size: size)
}

private func queryCgFrontmostWindowMatch(forPid pid: Int) -> (windowId: Int, frame: CGRect)? {
    guard pid > 0 else { return nil }
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return nil
    }
    var candidates: [CgWindowCandidate] = []
    for entry in list {
        let ownerPid = (entry[kCGWindowOwnerPID as String] as? Int) ?? ((entry[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0)
        if ownerPid != pid { continue }
        let layer = (entry[kCGWindowLayer as String] as? Int) ?? ((entry[kCGWindowLayer as String] as? NSNumber)?.intValue ?? -1)
        let alpha = (entry[kCGWindowAlpha as String] as? Double) ?? ((entry[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1.0)
        let winNum = (entry[kCGWindowNumber as String] as? Int) ?? ((entry[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0)
        if let boundsDict = entry[kCGWindowBounds as String] as? NSDictionary,
           let cgRect = CGRect(dictionaryRepresentation: boundsDict as CFDictionary) {
            candidates.append(CgWindowCandidate(ownerPid: ownerPid, layer: layer, alpha: alpha, bounds: cgRect, windowNumber: winNum))
        }
    }
    return selectFrontmostWindowCgMatch(from: candidates, ownerPid: pid)
}

func queryFrontmostWindowCgMatch() -> (pid: Int, windowId: Int, frame: CGRect?)? {
    guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
    let pid = Int(app.processIdentifier)
    guard pid > 0 && pid != Int(ProcessInfo.processInfo.processIdentifier) else { return nil }

    // WindowServer in-memory lookup identifies the active windowId and fallback CG bounds without blocking on hung apps.
    let cgMatch = queryCgFrontmostWindowMatch(forPid: pid)
    let windowId = cgMatch?.windowId ?? 0
    // Fast path: reuse recent background AX geometry cache ONLY if it matches BOTH current PID and current windowId.
    if windowId > 0, let cached = FrontmostWindowFrameCache.shared.freshFrame(forPid: pid, windowId: windowId) {
        return (pid, windowId, cached)
    }
    return (pid, windowId, cgMatch?.frame)
}

private func queryFrontmostWindowCgFrame() -> CGRect? {
    queryFrontmostWindowCgMatch()?.frame
}
