import AppKit
import Foundation

struct HudRgba: Equatable {
    let r: CGFloat
    let g: CGFloat
    let b: CGFloat
    let a: CGFloat

    var nsColor: NSColor {
        NSColor(srgbRed: r, green: g, blue: b, alpha: a)
    }
}

private func hudRgba(_ r: Int, _ g: Int, _ b: Int, _ a: CGFloat = 1.0) -> HudRgba {
    HudRgba(r: CGFloat(r) / 255.0, g: CGFloat(g) / 255.0, b: CGFloat(b) / 255.0, a: a)
}

func rgba(_ r: Int, _ g: Int, _ b: Int, _ a: CGFloat = 1.0) -> NSColor {
    NSColor(srgbRed: CGFloat(r) / 255.0, green: CGFloat(g) / 255.0, blue: CGFloat(b) / 255.0, alpha: a)
}

func hostUiCulture() -> String {
    for raw in Locale.preferredLanguages {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.isEmpty { return s }
    }
    return Locale.current.identifier.trimmingCharacters(in: .whitespacesAndNewlines)
}

func isZhUiCulture(_ tag: String) -> Bool {
    let low = tag.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return low == "zh" || low.hasPrefix("zh-") || low.hasPrefix("zh_") || low.hasPrefix("zh.") || low.hasPrefix("zh@")
}

func normalizeHudPosition(_ position: String) -> String {
    let pos = position.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return pos == "top" ? "top" : "bottom"
}

func computeHudPillY(visibleMinY: CGFloat, visibleMaxY: CGFloat, pillH: CGFloat, position: String) -> CGFloat {
    if normalizeHudPosition(position) == "top" {
        return round(visibleMaxY - pillH - 14.0)
    }
    return round(visibleMinY + 8.0)
}

func computeHudToastY(visibleMinY: CGFloat, visibleMaxY: CGFloat, mainH: CGFloat, toastH: CGFloat, position: String) -> CGFloat {
    let effMainH = max(36.0, mainH)
    if normalizeHudPosition(position) == "top" {
        return round(visibleMaxY - 14.0 - effMainH - 8.0 - toastH)
    }
    return round(visibleMinY + 8.0 + effMainH + 8.0)
}

struct CgWindowCandidate {
    let ownerPid: Int
    let layer: Int
    let alpha: Double
    let bounds: CGRect
    let windowNumber: Int

    init(ownerPid: Int, layer: Int, alpha: Double, bounds: CGRect, windowNumber: Int = 0) {
        self.ownerPid = ownerPid
        self.layer = layer
        self.alpha = alpha
        self.bounds = bounds
        self.windowNumber = windowNumber
    }
}

func selectFrontmostWindowCgMatch(
    from candidates: [CgWindowCandidate],
    ownerPid: Int,
    minLayer: Int = Int(CGWindowLevelForKey(.normalWindow)),
    maxLayer: Int = Int(CGWindowLevelForKey(.floatingWindow))
) -> (windowId: Int, frame: CGRect)? {
    guard ownerPid > 0 else { return nil }
    for win in candidates {
        if win.ownerPid != ownerPid { continue }
        if win.layer < minLayer || win.layer > maxLayer { continue }
        if !win.alpha.isFinite || win.alpha <= 0.01 { continue }
        let r = win.bounds
        if !r.origin.x.isFinite || !r.origin.y.isFinite || !r.width.isFinite || !r.height.isFinite { continue }
        if r.width >= 20.0 && r.height >= 20.0 {
            return (win.windowNumber, r)
        }
    }
    return nil
}

func selectFrontmostWindowCgFrame(
    from candidates: [CgWindowCandidate],
    ownerPid: Int,
    minLayer: Int = Int(CGWindowLevelForKey(.normalWindow)),
    maxLayer: Int = Int(CGWindowLevelForKey(.floatingWindow))
) -> CGRect? {
    selectFrontmostWindowCgMatch(from: candidates, ownerPid: ownerPid, minLayer: minLayer, maxLayer: maxLayer)?.frame
}

func primaryScreenHeight(from screenFrames: [CGRect]) -> CGFloat? {
    if let originScreen = screenFrames.first(where: { abs($0.origin.x) < 0.5 && abs($0.origin.y) < 0.5 }) {
        return originScreen.maxY
    }
    return screenFrames.first?.maxY
}

func cgRectToAppKit(_ cgRect: CGRect, primaryScreenMaxY: CGFloat) -> CGRect {
    CGRect(
        x: cgRect.origin.x,
        y: primaryScreenMaxY - cgRect.origin.y - cgRect.size.height,
        width: cgRect.size.width,
        height: cgRect.size.height
    )
}

func selectHudScreenIndex(
    screenFrames: [CGRect],
    focusedWindowCgFrame: CGRect?,
    mouseLocation: CGPoint,
    mainScreenIndex: Int? = nil
) -> Int? {
    guard !screenFrames.isEmpty else { return nil }
    if let cgWin = focusedWindowCgFrame,
       cgWin.origin.x.isFinite && cgWin.origin.y.isFinite && cgWin.width.isFinite && cgWin.height.isFinite,
       cgWin.width > 0.0 && cgWin.height > 0.0,
       let primaryMaxY = primaryScreenHeight(from: screenFrames) {
        let winFrame = cgRectToAppKit(cgWin, primaryScreenMaxY: primaryMaxY)
        let winCenter = CGPoint(x: winFrame.midX, y: winFrame.midY)
        var bestIdx: Int? = nil
        var bestArea: CGFloat = 0.0
        for (idx, frame) in screenFrames.enumerated() {
            let inter = frame.intersection(winFrame)
            if inter.isNull || inter.isEmpty { continue }
            let area = inter.width * inter.height
            if area > bestArea + 0.5 {
                bestArea = area
                bestIdx = idx
            } else if bestIdx != nil && abs(area - bestArea) <= 0.5 && frame.contains(winCenter) {
                bestIdx = idx
            }
        }
        if let idx = bestIdx {
            return idx
        }
    }
    if let mouseIdx = screenFrames.firstIndex(where: { $0.contains(mouseLocation) }) {
        return mouseIdx
    }
    if let mainIdx = mainScreenIndex, mainIdx >= 0 && mainIdx < screenFrames.count {
        return mainIdx
    }
    return 0
}

func stripOuterParens(_ raw: String) -> String {
    let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if t.count >= 2 && t.first == "(" && t.last == ")" {
        let inner = t.dropFirst().dropLast()
        return String(inner).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return t
}

private let LEADING_HUD_SYMBOLS: Set<String> = ["●", "◐", "◈", "✓", "○", "✕", "✖", "✗", "!", "⚠", "⚠️"]

func stripLeadingHudSymbol(_ raw: String) -> String {
    let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.isEmpty { return "" }
    let chars = Array(s)
    if let first = chars.first {
        let firstStr = String(first)
        if LEADING_HUD_SYMBOLS.contains(firstStr) {
            var idx = 1
            while idx < chars.count && chars[idx].isWhitespace { idx += 1 }
            return String(chars[idx...])
        }
    }
    let seg = splitHudSegments(s)
    if !seg.icon.isEmpty {
        if seg.hint.isEmpty { return seg.main }
        return seg.main + (seg.hasSpaceBeforeHint ? " " : "") + seg.hint
    }
    return s
}

struct HudSegments: Equatable {
    let icon: String
    let main: String
    let hint: String
    let hasSpaceBeforeHint: Bool
}

func splitHudSegments(_ raw: String) -> HudSegments {
    let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if s.isEmpty {
        return HudSegments(icon: "", main: "", hint: "", hasSpaceBeforeHint: false)
    }
    let chars = Array(s)
    var iconPart = ""
    var rest = s
    if let first = chars.first, LEADING_HUD_SYMBOLS.contains(String(first)) {
        iconPart = String(first)
        var afterIdx = 1
        while afterIdx < chars.count && chars[afterIdx] == " " { afterIdx += 1 }
        rest = (afterIdx < chars.count) ? String(chars[afterIdx...]) : ""
    } else if let spIdx = chars.firstIndex(of: " "), spIdx >= 1, spIdx <= 3, spIdx + 1 < chars.count {
        let prefix = chars[0..<spIdx]
        if !prefix.contains(where: { $0.isLetter || $0.isNumber }) {
            iconPart = String(prefix)
            var afterIdx = spIdx + 1
            while afterIdx < chars.count && chars[afterIdx] == " " { afterIdx += 1 }
            rest = String(chars[afterIdx...])
        }
    }
    if rest.hasSuffix(")"), let openIdx = rest.lastIndex(of: "("), openIdx > rest.startIndex {
        var trimmedEnd = openIdx
        while trimmedEnd > rest.startIndex && rest[rest.index(before: trimmedEnd)] == " " {
            trimmedEnd = rest.index(before: trimmedEnd)
        }
        if trimmedEnd > rest.startIndex {
            let trimmedMain = String(rest[..<trimmedEnd])
            let hadSpace = (trimmedEnd != openIdx)
            let hintPart = String(rest[openIdx...])
            return HudSegments(icon: iconPart, main: trimmedMain, hint: hintPart, hasSpaceBeforeHint: hadSpace)
        }
    }
    return HudSegments(icon: iconPart, main: rest, hint: "", hasSpaceBeforeHint: false)
}

struct HudVisualStyle: Equatable {
    let layer: String
    let defaultText: String
    let bg: HudRgba
    let border: HudRgba
    let borderWidth: CGFloat
    let icon: HudRgba
    let mainText: HudRgba
    let hintText: HudRgba
    let autoHideMs: Int
    let iconKind: String
}

func isCancellableMainHudState(_ state: String) -> Bool {
    let st = state.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return st == "recording" || st == "recognizing" || st == "processing" || st == "refining"
}

func formatHoldCancelHint(zh: Bool) -> String {
    zh ? "继续按住取消…" : "keep holding to cancel…"
}

func resolveHudLayer(state: String, explicitLayer: String = "") -> String {
    let st = state.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if st.isEmpty || st == "idle" || st == "hide" || st == "hide_toast" || st == "hide_all" {
        return "control"
    }
    let exp = explicitLayer.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if exp == "main" || exp == "toast" {
        return exp
    }
    switch st {
    case "recording", "recognizing", "processing", "refining", "done", "copied":
        return "main"
    default:
        return "toast"
    }
}

func resolveHudVisualStyle(state: String, explicitLayer: String = "", zh: Bool = false) -> HudVisualStyle {
    let st = state.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    let layer = resolveHudLayer(state: st, explicitLayer: explicitLayer)

    let toastBg = HudRgba(r: 28.0 / 255.0, g: 28.0 / 255.0, b: 30.0 / 255.0, a: 0.96)
    let toastMainText = HudRgba(r: 244.0 / 255.0, g: 245.0 / 255.0, b: 248.0 / 255.0, a: 1.0)
    let toastHintText = HudRgba(r: 190.0 / 255.0, g: 195.0 / 255.0, b: 208.0 / 255.0, a: 1.0)
    let amberBorder = HudRgba(r: 251.0 / 255.0, g: 191.0 / 255.0, b: 36.0 / 255.0, a: 0.52)
    let amberIcon = HudRgba(r: 251.0 / 255.0, g: 191.0 / 255.0, b: 36.0 / 255.0, a: 1.0)
    let coralBorder = HudRgba(r: 248.0 / 255.0, g: 113.0 / 255.0, b: 113.0 / 255.0, a: 0.55)
    let coralIcon = HudRgba(r: 248.0 / 255.0, g: 113.0 / 255.0, b: 113.0 / 255.0, a: 1.0)

    switch st {
    case "recording":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "● 录音中…" : "● Listening…",
            bg: hudRgba(64, 18, 26, 0.98),
            border: hudRgba(232, 88, 102, 0.68),
            borderWidth: 1.0,
            icon: hudRgba(255, 104, 112, 1.0),
            mainText: hudRgba(248, 250, 252, 1.0),
            hintText: hudRgba(200, 178, 182, 1.0),
            autoHideMs: 0,
            iconKind: ""
        )
    case "recognizing", "processing":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "◐ 语音识别…" : "◐ Transcribing…",
            bg: hudRgba(16, 40, 72, 0.98),
            border: hudRgba(56, 189, 248, 0.65),
            borderWidth: 1.0,
            icon: hudRgba(56, 189, 248, 1.0),
            mainText: hudRgba(244, 248, 252, 1.0),
            hintText: hudRgba(176, 198, 222, 1.0),
            autoHideMs: 0,
            iconKind: "recognizing"
        )
    case "refining":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "◈ 整理精修…" : "◈ Refining…",
            bg: hudRgba(46, 22, 76, 0.98),
            border: hudRgba(192, 132, 252, 0.65),
            borderWidth: 1.0,
            icon: hudRgba(208, 156, 255, 1.0),
            mainText: hudRgba(248, 245, 255, 1.0),
            hintText: hudRgba(198, 182, 226, 1.0),
            autoHideMs: 0,
            iconKind: "refining"
        )
    case "done":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✓ 已填入" : "✓ Inserted",
            bg: hudRgba(14, 54, 36, 0.98),
            border: hudRgba(74, 222, 128, 0.62),
            borderWidth: 1.0,
            icon: hudRgba(74, 222, 128, 1.0),
            mainText: hudRgba(242, 253, 246, 1.0),
            hintText: hudRgba(174, 218, 192, 1.0),
            autoHideMs: 900,
            iconKind: "done"
        )
    case "copied":
        let isToast = (layer == "toast")
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✓ 已复制(窗口已切换)" : "✓ Copied (window switched)",
            bg: isToast ? toastBg : hudRgba(28, 32, 82, 0.98),
            border: hudRgba(129, 140, 248, 0.68),
            borderWidth: 1.0,
            icon: hudRgba(144, 156, 255, 1.0),
            mainText: isToast ? toastMainText : hudRgba(240, 244, 255, 1.0),
            hintText: isToast ? toastHintText : hudRgba(186, 196, 238, 1.0),
            autoHideMs: 5000,
            iconKind: "done"
        )
    case "empty":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "○ 未检测到语音" : "○ No speech detected",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2200,
            iconKind: "empty_ring"
        )
    case "cancelled":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "○ 已取消" : "○ Cancelled",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 1600,
            iconKind: "empty_ring"
        )
    case "mic_unavailable":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 麦克风暂忙，请再试一次" : "! Microphone busy, try again",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2600,
            iconKind: "warning"
        )
    case "queue_full":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 待处理队列已满，请稍候" : "! Dictation queue full, please wait",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2600,
            iconKind: "warning"
        )
    case "mic_no_stream":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 麦克风无音频流，请检查耳机/设备" : "! No mic audio stream, check headset",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2600,
            iconKind: "warning"
        )
    case "audio_recovered":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 麦克风短暂中断，已自动恢复" : "! Mic briefly interrupted, resumed",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2400,
            iconKind: "warning"
        )
    case "partial_asr":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 部分语音未识别，可重试" : "! Partial speech missed, retry available",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "warning"
        )
    case "partial_asr_unsaved":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 部分语音未识别" : "! Partial speech missed",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "warning"
        )
    case "partial_asr_copied":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 已复制，部分未识别可重试" : "! Copied; partial missed, can retry",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "warning"
        )
    case "partial_asr_copied_unsaved":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 已复制，部分语音未识别" : "! Copied; partial speech missed",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "warning"
        )
    case "refiner_fallback":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "⚠ 已贴原文(精修失败)" : "⚠ Raw pasted (refiner failed)",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 4000,
            iconKind: "warning"
        )
    case "asr_empty_retry":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "! 识别为空，已存录音可重试" : "! Empty transcript, audio saved for retry",
            bg: toastBg,
            border: amberBorder,
            borderWidth: 1.0,
            icon: amberIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "warning"
        )
    case "asr_empty":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 识别为空" : "✕ Empty transcript",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2600,
            iconKind: "cross"
        )
    case "paste_failed_retry":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 填入失败，已存录音可重试" : "✕ Insert failed, audio saved for retry",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "cross"
        )
    case "error_retry":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 识别失败，已存录音可重试" : "✕ Transcription failed, audio saved for retry",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "cross"
        )
    case "no_mic":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 无可用麦克风" : "✕ No microphone found",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2200,
            iconKind: "cross"
        )
    case "mic_permission":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 麦克风权限未开启" : "✕ Microphone access denied",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 3200,
            iconKind: "cross"
        )
    case "mic_disconnected":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 麦克风已断开" : "✕ Microphone disconnected",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2200,
            iconKind: "cross"
        )
    case "paste_failed":
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 填入失败" : "✕ Failed to insert text",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2600,
            iconKind: "cross"
        )
    default:
        return HudVisualStyle(
            layer: layer,
            defaultText: zh ? "✕ 识别失败" : "✕ Transcription failed",
            bg: toastBg,
            border: coralBorder,
            borderWidth: 1.0,
            icon: coralIcon,
            mainText: toastMainText,
            hintText: toastHintText,
            autoHideMs: 2600,
            iconKind: "cross"
        )
    }
}

func snapToPixel(_ value: CGFloat, scale: CGFloat) -> CGFloat {
    let s = scale > 0.0 ? scale : 2.0
    return (value * s).rounded() / s
}

func formatRecordingDuration(_ elapsedSec: Int) -> String {
    let sec = max(0, elapsedSec)
    if sec < 60 { return "\(sec)s" }
    let m = sec / 60
    let s = sec % 60
    return "\(m)m \(s < 10 ? "0" : "")\(s)s"
}

func durationSlotTemplate(_ elapsedSec: Int) -> String {
    if elapsedSec < 10 { return "8s" }
    if elapsedSec < 60 { return "88s" }
    if elapsedSec < 600 { return "8m 88s" }
    return "88m 88s"
}

func recordingCountdownWarnSec(_ maxDurationSec: Int) -> Int {
    if maxDurationSec <= 0 { return 0 }
    if maxDurationSec >= 45 { return 15 }
    let third = maxDurationSec / 3
    return max(1, min(10, third > 0 ? third : 1))
}

func formatCountdownHint(_ remainSec: Int, zh: Bool) -> String {
    let r = max(0, remainSec)
    return zh ? "(\(r)s 后自动完成)" : "(auto-finish in \(r)s)"
}

func countdownHintSlotTemplate(_ remainSec: Int, zh: Bool) -> String {
    let tpl = (remainSec >= 10) ? "88" : "8"
    return zh ? "(\(tpl)s 后自动完成)" : "(auto-finish in \(tpl)s)"
}

func containsCjkScalar(_ s: String) -> Bool {
    s.unicodeScalars.contains(where: { $0.value >= 0x2E80 && $0.value <= 0x9FFF })
}

struct HudWaveLayoutGeometry {
    let pillW: CGFloat
    let pillH: CGFloat
    let maxW: CGFloat
    let padL: CGFloat
    let padR: CGFloat
    let truncated: Bool
    let durFrame: NSRect
    let waveFrame: NSRect
    let hintFrame: NSRect
    let badgeFrame: NSRect
    let badgeLabelFrame: NSRect
    let durSlotW: CGFloat
    let hintSlotW: CGFloat
    let badgeSlotW: CGFloat
}

struct HudStatusLayoutGeometry {
    let pillW: CGFloat
    let pillH: CGFloat
    let maxW: CGFloat
    let padL: CGFloat
    let padR: CGFloat
    let truncated: Bool
    let iconFrame: NSRect
    let labelFrame: NSRect
    let textMeasuredW: CGFloat
}

func computeWaveHudLayout(
    durSlotW: CGFloat,
    durH: CGFloat,
    hintW: CGFloat,
    hintH: CGFloat,
    hasHint: Bool,
    badgeTextW: CGFloat,
    badgeTextH: CGFloat,
    hasBadge: Bool,
    visibleWidth: CGFloat,
    scale: CGFloat
) -> HudWaveLayoutGeometry {
    let padL: CGFloat = 16.0
    let padR: CGFloat = hasBadge ? 10.0 : 16.0
    let waveW: CGFloat = WaveBarsView.totalWidth
    let gap1: CGFloat = 10.0
    let gap2: CGFloat = hasHint ? 10.0 : 0.0
    let badgePadX: CGFloat = 6.0
    let badgeSlotW: CGFloat = hasBadge ? max(22.0, ceil(badgeTextW) + badgePadX * 2.0) : 0.0
    let badgeH: CGFloat = hasBadge ? max(20.0, ceil(badgeTextH) + 4.0) : 0.0
    let badgeGap: CGFloat = hasBadge ? 10.0 : 0.0
    let effHintW: CGFloat = hasHint ? hintW : 0.0
    let totalContentW = durSlotW + gap1 + waveW + gap2 + effHintW + badgeGap + badgeSlotW
    let textH = max(max(durH, hasHint ? hintH : 0.0), badgeH)

    let maxW = max(64.0, min(680.0, visibleWidth - 24.0))
    let pillH: CGFloat = max(36.0, ceil(textH) + 12.0)
    let rawW: CGFloat = max(64.0, snapToPixel(ceil(totalContentW) + padL + padR, scale: scale))
    let truncated = rawW > maxW
    let pillW: CGFloat = min(maxW, rawW)
    let availW = max(1.0, pillW - padL - padR)
    var curX = padL + ((totalContentW <= availW) ? snapToPixel((availW - totalContentW) * 0.5, scale: scale) : 0.0)

    let durY = snapToPixel((pillH - durH) / 2.0, scale: scale)
    let durFrame = NSRect(x: snapToPixel(curX, scale: scale), y: durY, width: durSlotW, height: durH)
    curX += durSlotW + gap1

    let waveFrame = NSRect(x: snapToPixel(curX, scale: scale), y: 0.0, width: waveW, height: pillH)
    curX += waveW + gap2

    let reservedRight = hasBadge ? (badgeGap + badgeSlotW) : 0.0
    let hintFrame: NSRect
    if hasHint {
        let hx = snapToPixel(curX, scale: scale)
        let remW = max(0.0, pillW - padR - reservedRight - hx)
        let drawHintW = min(effHintW, remW)
        let hintY = snapToPixel((pillH - hintH) / 2.0, scale: scale)
        hintFrame = NSRect(x: hx, y: hintY, width: drawHintW, height: hintH)
        curX = hx + drawHintW
    } else {
        hintFrame = .zero
    }

    let badgeFrame: NSRect
    let badgeLabelFrame: NSRect
    if hasBadge {
        let bx = snapToPixel(curX + badgeGap, scale: scale)
        let effBH = min(pillH - 6.0, badgeH)
        let by = snapToPixel((pillH - effBH) / 2.0, scale: scale)
        badgeFrame = NSRect(x: bx, y: by, width: badgeSlotW, height: effBH)
        let lblY = snapToPixel((effBH - badgeTextH) / 2.0, scale: scale)
        badgeLabelFrame = NSRect(x: 0.0, y: lblY, width: badgeSlotW, height: badgeTextH)
    } else {
        badgeFrame = .zero
        badgeLabelFrame = .zero
    }

    return HudWaveLayoutGeometry(
        pillW: pillW,
        pillH: pillH,
        maxW: maxW,
        padL: padL,
        padR: padR,
        truncated: truncated,
        durFrame: durFrame,
        waveFrame: waveFrame,
        hintFrame: hintFrame,
        badgeFrame: badgeFrame,
        badgeLabelFrame: badgeLabelFrame,
        durSlotW: durSlotW,
        hintSlotW: effHintW,
        badgeSlotW: badgeSlotW
    )
}

func computeStatusHudLayout(
    isToast: Bool,
    hasIcon: Bool,
    hasBody: Bool,
    textW: CGFloat,
    textH: CGFloat,
    visibleWidth: CGFloat,
    scale: CGFloat
) -> HudStatusLayoutGeometry {
    let ceilTextW = hasBody ? ceil(textW) : 0.0
    let iconBox: CGFloat = hasIcon ? (isToast ? StatusIconView.toastBoxSize : StatusIconView.mainBoxSize) : 0.0
    let iconGap: CGFloat = (hasIcon && hasBody) ? (isToast ? 6.5 : 7.0) : 0.0
    let padL: CGFloat = isToast ? 14.0 : 16.0
    let padR: CGFloat = isToast ? 14.0 : 16.0
    let contentW = iconBox + iconGap + ceilTextW
    let pillH: CGFloat = isToast ? max(32.0, ceil(textH) + 10.0) : max(36.0, ceil(textH) + 12.0)
    let maxW = max(64.0, min(680.0, visibleWidth - 24.0))
    let rawW: CGFloat = max(64.0, snapToPixel(ceil(contentW) + padL + padR, scale: scale))
    let truncated = rawW > maxW
    let pillW: CGFloat = min(maxW, rawW)

    let availW = max(1.0, pillW - padL - padR)
    var curX = padL + ((contentW <= availW) ? snapToPixel((availW - contentW) * 0.5, scale: scale) : 0.0)
    let iconFrame: NSRect
    if hasIcon {
        let iconY = snapToPixel((pillH - iconBox) / 2.0, scale: scale)
        iconFrame = NSRect(x: snapToPixel(curX, scale: scale), y: iconY, width: iconBox, height: iconBox)
        curX += iconBox + iconGap
    } else {
        iconFrame = .zero
    }
    let labelFrame: NSRect
    if hasBody {
        let lx = snapToPixel(curX, scale: scale)
        let labelH = ceil(textH)
        let labelY = snapToPixel((pillH - labelH) / 2.0, scale: scale)
        let remW = max(0.0, pillW - padR - lx)
        labelFrame = NSRect(x: lx, y: labelY, width: min(ceilTextW, remW), height: labelH)
    } else {
        labelFrame = .zero
    }
    return HudStatusLayoutGeometry(
        pillW: pillW,
        pillH: pillH,
        maxW: maxW,
        padL: padL,
        padR: padR,
        truncated: truncated,
        iconFrame: iconFrame,
        labelFrame: labelFrame,
        textMeasuredW: ceilTextW
    )
}
