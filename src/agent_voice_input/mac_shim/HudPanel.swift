import AppKit
import ApplicationServices
import Foundation

private func addStatusIconQuadCurve(_ path: NSBezierPath, from p0: NSPoint, control ctrl: NSPoint, to p1: NSPoint) {
    let c1 = NSPoint(x: p0.x + (ctrl.x - p0.x) * (2.0 / 3.0), y: p0.y + (ctrl.y - p0.y) * (2.0 / 3.0))
    let c2 = NSPoint(x: p1.x + (ctrl.x - p1.x) * (2.0 / 3.0), y: p1.y + (ctrl.y - p1.y) * (2.0 / 3.0))
    path.curve(to: p1, controlPoint1: c1, controlPoint2: c2)
}

func drawStatusIcon(
    kind iconKind: String,
    color iconColor: NSColor,
    bounds: NSRect,
    box configuredSize: CGFloat,
    isFlipped: Bool
) {
    let box = min(configuredSize, min(bounds.width, bounds.height))
    if box <= 2.0 { return }
    let ox = bounds.minX + (bounds.width - box) * 0.5
    let oy = bounds.minY + (bounds.height - box) * 0.5
    let dySign: CGFloat = isFlipped ? 1.0 : -1.0
    func py(_ frac: CGFloat) -> CGFloat {
        oy + (isFlipped ? (box * frac) : (box * (1.0 - frac)))
    }

    switch iconKind {
    case "recognizing":
        let ins: CGFloat = 1.1
        let d = max(2.0, box - ins * 2.0)
        let ovalRect = NSRect(x: ox + ins, y: oy + ins, width: d, height: d)
        let ring = NSBezierPath(ovalIn: ovalRect)
        ring.lineWidth = 1.8
        iconColor.setStroke()
        ring.stroke()
        NSGraphicsContext.saveGraphicsState()
        NSRect(x: ox, y: oy, width: box * 0.5, height: box).clip()
        iconColor.setFill()
        NSBezierPath(ovalIn: ovalRect).fill()
        NSGraphicsContext.restoreGraphicsState()
    case "refining":
        let cx = ox + box * 0.5
        let cy = oy + box * 0.5
        let top = NSPoint(x: cx, y: py(0.04))
        let right = NSPoint(x: ox + box * 0.96, y: cy)
        let bottom = NSPoint(x: cx, y: py(0.96))
        let left = NSPoint(x: ox + box * 0.04, y: cy)
        let pull = box * 0.085
        let sp = NSBezierPath()
        sp.move(to: top)
        addStatusIconQuadCurve(sp, from: top, control: NSPoint(x: cx + pull, y: cy - dySign * pull), to: right)
        addStatusIconQuadCurve(sp, from: right, control: NSPoint(x: cx + pull, y: cy + dySign * pull), to: bottom)
        addStatusIconQuadCurve(sp, from: bottom, control: NSPoint(x: cx - pull, y: cy + dySign * pull), to: left)
        addStatusIconQuadCurve(sp, from: left, control: NSPoint(x: cx - pull, y: cy - dySign * pull), to: top)
        sp.close()
        iconColor.setFill()
        sp.fill()
    case "done":
        let p = NSBezierPath()
        p.lineWidth = 2.0
        p.lineCapStyle = .round
        p.lineJoinStyle = .round
        p.move(to: NSPoint(x: ox + box * 0.14, y: py(0.54)))
        p.line(to: NSPoint(x: ox + box * 0.40, y: py(0.80)))
        p.line(to: NSPoint(x: ox + box * 0.88, y: py(0.24)))
        iconColor.setStroke()
        p.stroke()
    case "empty", "empty_ring":
        let ins: CGFloat = 1.1
        let d = max(2.0, box - ins * 2.0)
        let ring = NSBezierPath(ovalIn: NSRect(x: ox + ins, y: oy + ins, width: d, height: d))
        ring.lineWidth = 1.8
        iconColor.setStroke()
        ring.stroke()
    case "warning":
        let cx = ox + box * 0.5
        let stem = NSBezierPath()
        stem.lineWidth = 2.0
        stem.lineCapStyle = .round
        stem.move(to: NSPoint(x: cx, y: py(0.15)))
        stem.line(to: NSPoint(x: cx, y: py(0.56)))
        iconColor.setStroke()
        stem.stroke()
        let dotD = max(2.2, box * 0.17)
        let dotRect = NSRect(x: cx - dotD * 0.5, y: py(0.82) - dotD * 0.5, width: dotD, height: dotD)
        iconColor.setFill()
        NSBezierPath(ovalIn: dotRect).fill()
    default:
        let ins = max(1.8, box * 0.16)
        let p = NSBezierPath()
        p.lineWidth = 2.0
        p.lineCapStyle = .round
        p.move(to: NSPoint(x: ox + ins, y: oy + ins))
        p.line(to: NSPoint(x: ox + box - ins, y: oy + box - ins))
        p.move(to: NSPoint(x: ox + box - ins, y: oy + ins))
        p.line(to: NSPoint(x: ox + ins, y: oy + box - ins))
        iconColor.setStroke()
        p.stroke()
    }
}

final class StatusIconView: NSView {
    static let mainBoxSize: CGFloat = 13.0
    static let toastBoxSize: CGFloat = 12.0
    static let boxSize: CGFloat = mainBoxSize

    override var isFlipped: Bool { true }

    var configuredSize: CGFloat = mainBoxSize {
        didSet { needsDisplay = true }
    }
    var iconKind: String = "recognizing" {
        didSet { needsDisplay = true }
    }
    var iconColor: NSColor = .white {
        didSet { needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        drawStatusIcon(kind: iconKind, color: iconColor, bounds: bounds, box: configuredSize, isFlipped: true)
    }
}

final class WaveBarsView: NSView {
    static let barWidth: CGFloat = 2.5
    static let barGap: CGFloat = 2.5
    static let maxBarHeight: CGFloat = 15.0
    static let totalWidth: CGFloat = 42.5

    var barNorms: [CGFloat] = Array(repeating: 0.0, count: 9) {
        didSet { needsDisplay = true }
    }
    var activeColor: NSColor = rgba(255, 104, 112, 1.0) {
        didSet { needsDisplay = true }
    }
    var quietColor: NSColor = rgba(244, 104, 114, 0.52) {
        didSet { needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let maxH = max(Self.barWidth, min(bounds.height - 8.0, Self.maxBarHeight))
        let radius = Self.barWidth * 0.5
        for i in 0..<9 {
            let norm = (i < barNorms.count) ? min(1.0, max(0.0, barNorms[i])) : 0.0
            let bh = Self.barWidth + norm * (maxH - Self.barWidth)
            let bx = CGFloat(i) * (Self.barWidth + Self.barGap)
            let by = (bounds.height - bh) * 0.5
            let rect = NSRect(x: bx, y: by, width: Self.barWidth, height: bh)
            let path = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
            (norm > 0.01 ? activeColor : quietColor).setFill()
            path.fill()
        }
    }
}

final class HudPillController {
    private static let recBg = rgba(64, 18, 26, 0.98)
    private static let recBorder = rgba(232, 88, 102, 0.68)
    static let recWaveActive = rgba(255, 104, 112, 1.0)
    static let recWaveQuiet = rgba(244, 104, 114, 0.52)
    private static let recMainText = rgba(248, 250, 252)
    private static let recHintTextCol = rgba(200, 178, 182, 1.0)

    private static let warnBg = rgba(72, 38, 12, 0.98)
    private static let warnBorder = rgba(245, 158, 11, 0.72)
    static let warnWaveActive = rgba(251, 191, 36, 1.0)
    static let warnWaveQuiet = rgba(251, 191, 36, 0.52)
    private static let warnMainText = rgba(254, 252, 232)
    private static let warnHintTextCol = rgba(251, 191, 36, 1.0)

    static let recBadgeBg = rgba(255, 255, 255, 0.14)
    static let recBadgeBorder = rgba(255, 214, 220, 0.28)
    static let recBadgeTextCol = rgba(255, 241, 242, 1.0)

    static let warnBadgeBg = rgba(254, 243, 199, 0.15)
    static let warnBadgeBorder = rgba(251, 191, 36, 0.36)
    static let warnBadgeTextCol = rgba(254, 243, 199, 1.0)

    static let hudFont = NSFont.systemFont(ofSize: 13.5, weight: .regular)
    static let monoDurFont = NSFont.monospacedDigitSystemFont(ofSize: 13.5, weight: .regular)
    static let toastFont = NSFont.systemFont(ofSize: 12.5, weight: .regular)
    static let badgeFont = NSFont.monospacedDigitSystemFont(ofSize: 11.5, weight: .semibold)
    private static let bellWeight: [CGFloat] = [1.00, 0.92, 0.78, 0.58, 0.40]

    private var panel: NSPanel?
    private var bgView: NSView?
    private var iconView: StatusIconView?
    private var label: NSTextField?
    private var waveView: WaveBarsView?
    private var durLabel: NSTextField?
    private var hintLabel: NSTextField?
    private var bgBadgeView: NSView?
    private var bgBadgeLabel: NSTextField?
    private var hideWorkItem: DispatchWorkItem?
    private var animTimer: DispatchSourceTimer?

    private var toastPanel: NSPanel?
    private var toastBgView: NSView?
    private var toastIconView: StatusIconView?
    private var toastLabel: NSTextField?
    private var toastHideWorkItem: DispatchWorkItem?

    private var currentMainPillH: CGFloat = 36.0
    private var isMainVisible = false
    private var isToastVisible = false
    private var anchoredScale: CGFloat = 2.0
    private var anchoredVisibleFrame: NSRect = .zero
    private var anchorSessionGen: UInt64 = 0
    private var anchoredWindowPid: Int = 0
    private var anchoredWindowId: Int = 0
    private var lastToastPosition: String = "bottom"
    private var currentToastSize: NSSize = NSSize(width: 120.0, height: 32.0)
    private var isWaveRecording = false
    private var recStartMs: UInt64 = 0
    private var liveLevel: CGFloat = 0.0
    private var liveLevelMs: UInt64 = 0
    private var smoothLevel: CGFloat = 0.0
    private var waveHist: [CGFloat] = Array(repeating: 0.0, count: 5)
    private var frameIdx: Int = 0
    private var recHintText: String = ""
    private var recZhHint: Bool = false
    private var recPosition: String = "bottom"
    private var recBgCount: Int = 0
    private var maxDurationSec: Int = 0
    private var warnRemainSec: Int = 0
    private var lastDurText: String = ""
    private var lastSlotTemplate: String = ""
    private var lastHintSlotTemplate: String = ""
    private var lastWarnState: Bool = false
    private var lastBgCount: Int = 0
    private var lastHoldPreviewState: Bool = false
    private var currentMainState: String = ""
    private var currentMainText: String = ""
    private var currentMainStyle: HudVisualStyle? = nil
    private var currentMainPosition: String = "bottom"
    private var currentMainZh: Bool = false
    private var holdCancelPreview: Bool = false
    private var holdPreviewSafetyWorkItem: DispatchWorkItem?

    private let hotkeyLabel: String
    private let hotkeyLabelEn: String
    private let isPtt: Bool
    private let headless: Bool
    private var toastSlotAllocated: Bool = false
    private var toastSlotCount: Int = 0
    private var toastUpdateCount: Int = 0
    private var toastLastText: String = ""
    private var toastLastAutoHideMs: Int = 0

    var canShowHoldCancelPreview: Bool { isMainVisible && isCancellableMainHudState(currentMainState) }
    var debugMainVisible: Bool { isMainVisible }
    var debugToastVisible: Bool { isToastVisible }
    var debugMainBgCount: Int { recBgCount }
    var debugHoldPreviewActive: Bool { holdCancelPreview }
    var debugMainEffectiveHint: String {
        if holdCancelPreview {
            let effZh = isWaveRecording ? recZhHint : (currentMainText.isEmpty ? currentMainZh : containsCjkScalar(currentMainText))
            let hint = formatHoldCancelHint(zh: effZh)
            return isWaveRecording ? hint : "(\(hint))"
        }
        return isWaveRecording ? stripOuterParens(recHintText) : splitHudSegments(currentMainText).hint
    }
    var debugToastSlotCount: Int { toastSlotCount }
    var debugToastUpdateCount: Int { toastUpdateCount }
    var debugToastLastText: String { toastLastText }
    var debugToastLastAutoHideMs: Int { toastLastAutoHideMs }

    init(hotkeyLabel: String, hotkeyLabelEn: String, isPtt: Bool, headless: Bool = false) {
        self.hotkeyLabel = hotkeyLabel
        self.hotkeyLabelEn = hotkeyLabelEn
        self.isPtt = isPtt
        self.headless = headless
    }

    private func alignSubviewRect(_ rect: NSRect, in container: NSView, scale: CGFloat) -> NSRect {
        if container.window != nil {
            return container.backingAlignedRect(rect, options: .alignAllEdgesNearest)
        }
        return NSRect(
            x: snapToPixel(rect.origin.x, scale: scale),
            y: snapToPixel(rect.origin.y, scale: scale),
            width: snapToPixel(rect.size.width, scale: scale),
            height: snapToPixel(rect.size.height, scale: scale)
        )
    }

    private func ensurePanel() {
        if panel != nil { return }
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 240, height: 36), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.appearance = NSAppearance(named: .darkAqua)
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = true
        p.ignoresMouseEvents = true; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 240, height: 36))
        container.wantsLayer = true
        container.layer?.backgroundColor = Self.recBg.cgColor
        container.layer?.cornerRadius = 18.0
        container.layer?.borderWidth = 1.0
        container.layer?.masksToBounds = true

        let iv = StatusIconView(frame: NSRect(x: 0, y: 0, width: StatusIconView.mainBoxSize, height: StatusIconView.mainBoxSize))
        iv.configuredSize = StatusIconView.mainBoxSize
        iv.isHidden = true
        container.addSubview(iv)

        let tf = NSTextField(labelWithString: "")
        tf.font = Self.hudFont
        tf.alignment = .left
        tf.lineBreakMode = .byTruncatingTail
        tf.maximumNumberOfLines = 1
        container.addSubview(tf)

        let wv = WaveBarsView(frame: NSRect(x: 0, y: 0, width: WaveBarsView.totalWidth, height: 36))
        wv.isHidden = true
        container.addSubview(wv)

        let dl = NSTextField(labelWithString: "0s")
        dl.font = Self.monoDurFont
        dl.textColor = Self.recMainText
        dl.alignment = .center
        dl.lineBreakMode = .byClipping
        dl.maximumNumberOfLines = 1
        dl.isHidden = true
        container.addSubview(dl)

        let hl = NSTextField(labelWithString: "")
        hl.font = Self.hudFont
        hl.textColor = Self.recHintTextCol
        hl.alignment = .left
        hl.lineBreakMode = .byTruncatingTail
        hl.maximumNumberOfLines = 1
        hl.isHidden = true
        container.addSubview(hl)

        let bv = NSView(frame: NSRect(x: 0, y: 0, width: 22, height: 20))
        bv.wantsLayer = true
        bv.layer?.backgroundColor = Self.recBadgeBg.cgColor
        bv.layer?.borderColor = Self.recBadgeBorder.cgColor
        bv.layer?.borderWidth = 1.0
        bv.layer?.cornerRadius = 10.0
        bv.layer?.masksToBounds = true
        bv.isHidden = true

        let bl = NSTextField(labelWithString: "")
        bl.font = Self.badgeFont
        bl.textColor = Self.recBadgeTextCol
        bl.alignment = .center
        bl.lineBreakMode = .byClipping
        bl.maximumNumberOfLines = 1
        bv.addSubview(bl)
        container.addSubview(bv)

        p.contentView = container
        panel = p; bgView = container; iconView = iv; label = tf; waveView = wv; durLabel = dl; hintLabel = hl; bgBadgeView = bv; bgBadgeLabel = bl
    }

    private func ensureToastPanel() {
        if toastPanel != nil { return }
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 220, height: 32), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.appearance = NSAppearance(named: .darkAqua)
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = true
        p.ignoresMouseEvents = true; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 220, height: 32))
        container.wantsLayer = true
        container.layer?.cornerRadius = 16.0
        container.layer?.borderWidth = 1.0
        container.layer?.masksToBounds = true

        let iv = StatusIconView(frame: NSRect(x: 0, y: 0, width: StatusIconView.toastBoxSize, height: StatusIconView.toastBoxSize))
        iv.configuredSize = StatusIconView.toastBoxSize
        iv.isHidden = true
        container.addSubview(iv)

        let tf = NSTextField(labelWithString: "")
        tf.font = Self.toastFont
        tf.alignment = .left
        tf.lineBreakMode = .byTruncatingTail
        tf.maximumNumberOfLines = 1
        container.addSubview(tf)

        p.contentView = container
        toastPanel = p; toastBgView = container; toastIconView = iv; toastLabel = tf
    }

    private func pickScreenMetrics(screens: [NSScreen], focusedFrame: CGRect?) -> (scale: CGFloat, visibleFrame: NSRect) {
        let activeScreen: NSScreen?
        if screens.count <= 1 {
            activeScreen = screens.first ?? NSScreen.main
        } else {
            let screenFrames = screens.map { $0.frame }
            let mainIdx = NSScreen.main.flatMap { mainScr in screens.firstIndex(where: { $0 === mainScr }) }
            let pickedIdx = selectHudScreenIndex(
                screenFrames: screenFrames,
                focusedWindowCgFrame: focusedFrame,
                mouseLocation: NSEvent.mouseLocation,
                mainScreenIndex: mainIdx
            )
            activeScreen = pickedIdx.flatMap { idx in (idx >= 0 && idx < screens.count) ? screens[idx] : nil }
                ?? NSScreen.main
                ?? screens.first
        }
        let scale = activeScreen?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2.0
        let vf = activeScreen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        return (scale, vf)
    }

    private func applyBackgroundAxAnchorCorrection(sessionGen: UInt64, pid: Int, windowId: Int, axFrame: CGRect?) {
        guard !headless, (isMainVisible || isToastVisible) else { return }
        guard anchorSessionGen == sessionGen, anchoredWindowPid == pid, anchoredWindowId == windowId else { return }
        guard let axFrame = axFrame else { return }
        // Only apply correction if the live frontmost window is STILL the exact same (pid, windowId).
        guard let live = queryFrontmostWindowCgMatch(), live.pid == pid, live.windowId == windowId else { return }
        let screens = NSScreen.screens
        guard screens.count > 1 else { return }
        let (newScale, newVf) = pickScreenMetrics(screens: screens, focusedFrame: axFrame)
        guard newVf != anchoredVisibleFrame || abs(newScale - anchoredScale) > 0.01 else { return }
        anchoredScale = newScale
        anchoredVisibleFrame = newVf
        if isMainVisible {
            if isWaveRecording {
                layoutWavePill(forceFrame: true, screenMetrics: (newScale, newVf))
            } else if let style = currentMainStyle {
                renderNonWaveMainPill(style: style, rawText: currentMainText, position: currentMainPosition, zh: currentMainZh)
            }
        }
        if isToastVisible {
            syncToastToAnchor()
        }
    }

    private func activeScreenMetrics() -> (scale: CGFloat, visibleFrame: NSRect) {
        let screens = NSScreen.screens
        if screens.count <= 1 {
            anchoredWindowPid = 0
            anchoredWindowId = 0
            return pickScreenMetrics(screens: screens, focusedFrame: nil)
        }
        anchorSessionGen &+= 1
        let gen = anchorSessionGen
        // Never perform synchronous AXUIElement IPC on the main thread when positioning HUD.
        // Use windowId-keyed background AX cache or fast CGWindowListCopyWindowInfo,
        // and trigger a non-blocking background AX refresh that corrects this HUD in place if still on the same window.
        let match = queryFrontmostWindowCgMatch()
        let focusedFrame = match?.frame
        if let m = match, m.pid > 0, m.windowId > 0 {
            anchoredWindowPid = m.pid
            anchoredWindowId = m.windowId
            FrontmostWindowFrameCache.shared.refreshAsync(forPid: m.pid, windowId: m.windowId) { [weak self] axFrame in
                self?.applyBackgroundAxAnchorCorrection(sessionGen: gen, pid: m.pid, windowId: m.windowId, axFrame: axFrame)
            }
        } else {
            anchoredWindowPid = 0
            anchoredWindowId = 0
        }
        return pickScreenMetrics(screens: screens, focusedFrame: focusedFrame)
    }

    private func resolveAnchoredScreenMetrics(refresh: Bool) -> (scale: CGFloat, visibleFrame: NSRect) {
        if refresh || anchoredVisibleFrame.width <= 0.0 || (!isMainVisible && !isToastVisible) {
            let (s, vf) = activeScreenMetrics()
            anchoredScale = s
            anchoredVisibleFrame = vf
        }
        return (anchoredScale, anchoredVisibleFrame)
    }

    private func syncToastToAnchor(position: String? = nil) {
        guard isToastVisible, let toastPanel = toastPanel, anchoredVisibleFrame.width > 0.0 else { return }
        if let pos = position, !pos.isEmpty { lastToastPosition = pos }
        let vf = anchoredVisibleFrame
        let scale = anchoredScale
        let toastW = currentToastSize.width
        let toastH = currentToastSize.height
        let toastX = snapToPixel(vf.midX - toastW / 2.0, scale: scale)
        let mainSlotH: CGFloat = isMainVisible ? max(36.0, currentMainPillH) : 36.0
        let toastY = computeHudToastY(visibleMinY: vf.minY, visibleMaxY: vf.maxY, mainH: mainSlotH, toastH: toastH, position: lastToastPosition)
        toastPanel.setFrame(NSRect(x: toastX, y: toastY, width: toastW, height: toastH), display: false)
    }

    private func stopWaveTimer() {
        if let t = animTimer {
            t.setEventHandler {}
            t.cancel()
            animTimer = nil
        }
        isWaveRecording = false
    }

    private func startWaveTimerIfNeeded() {
        if animTimer != nil { return }
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + .milliseconds(60), repeating: .milliseconds(60), leeway: .milliseconds(8))
        t.setEventHandler { [weak self] in
            self?.tickWaveFrame()
        }
        animTimer = t
        t.resume()
    }

    func updateAudioLevel(_ level: CGFloat) {
        liveLevel = min(1.0, max(0.0, level))
        liveLevelMs = monotonicMs()
    }

    private func currentLiveLevel() -> CGFloat {
        let now = monotonicMs()
        if liveLevelMs == 0 || (now >= liveLevelMs && (now - liveLevelMs) > 260) {
            return 0.0
        }
        return liveLevel
    }

    private func stepWaveEnvelope(_ rawLvl: CGFloat) -> [CGFloat] {
        let r = min(1.0, max(0.0, rawLvl))
        smoothLevel = (r > smoothLevel) ? (smoothLevel * 0.35 + r * 0.65) : (smoothLevel * 0.70 + r * 0.30)
        for k in stride(from: waveHist.count - 1, through: 1, by: -1) {
            waveHist[k] = waveHist[k - 1]
        }
        waveHist[0] = smoothLevel
        frameIdx += 1
        var norms = Array(repeating: CGFloat(0.0), count: 9)
        for i in 0..<9 {
            let dist = abs(i - 4)
            var v = waveHist[dist] * Self.bellWeight[dist]
            if v > 0.03 {
                let osc = 0.86 + 0.14 * sin(Double(frameIdx) * 0.75 + Double(i) * 1.15)
                v *= CGFloat(osc)
            } else {
                v = 0.0
            }
            norms[i] = min(1.0, max(0.0, v))
        }
        return norms
    }

    private func tickWaveFrame() {
        if !isWaveRecording {
            stopWaveTimer()
            return
        }
        waveView?.barNorms = stepWaveEnvelope(currentLiveLevel())
        layoutWavePill(forceFrame: false)
    }

    private func layoutWavePill(forceFrame: Bool, screenMetrics: (scale: CGFloat, visibleFrame: NSRect)? = nil) {
        guard let panel = panel, let bgView = bgView, let iconView = iconView, let label = label,
              let waveView = waveView, let durLabel = durLabel, let hintLabel = hintLabel else { return }
        let now = monotonicMs()
        let elapsedSec = Int((now >= recStartMs ? (now - recStartMs) : 0) / 1000)
        var isWarn = false
        var remainSec = 0
        if maxDurationSec > 0 && warnRemainSec > 0 {
            remainSec = max(0, maxDurationSec - elapsedSec)
            isWarn = (remainSec <= warnRemainSec)
        }
        let effHint: String
        let hintSlotTpl: String
        if holdCancelPreview {
            effHint = formatHoldCancelHint(zh: recZhHint)
            hintSlotTpl = effHint
        } else {
            let rawEffHint = isWarn ? formatCountdownHint(remainSec, zh: recZhHint) : recHintText
            effHint = stripOuterParens(rawEffHint)
            hintSlotTpl = isWarn ? stripOuterParens(countdownHintSlotTemplate(remainSec, zh: recZhHint)) : effHint
        }
        let durStr = formatRecordingDuration(elapsedSec)
        let slotTpl = durationSlotTemplate(elapsedSec)

        if !forceFrame && durStr == lastDurText && slotTpl == lastSlotTemplate && isWarn == lastWarnState && hintSlotTpl == lastHintSlotTemplate && recBgCount == lastBgCount && holdCancelPreview == lastHoldPreviewState {
            return
        }
        let needRelayout = forceFrame || (slotTpl != lastSlotTemplate) || (isWarn != lastWarnState) || (hintSlotTpl != lastHintSlotTemplate) || (recBgCount != lastBgCount) || (holdCancelPreview != lastHoldPreviewState)
        lastDurText = durStr
        lastSlotTemplate = slotTpl
        lastHintSlotTemplate = hintSlotTpl
        lastWarnState = isWarn
        lastBgCount = recBgCount
        lastHoldPreviewState = holdCancelPreview

        if !needRelayout {
            durLabel.stringValue = durStr
            if isWarn && !holdCancelPreview {
                hintLabel.stringValue = effHint
            }
            return
        }

        let effBg = isWarn ? Self.warnBg : Self.recBg
        let effBorder = isWarn ? Self.warnBorder : Self.recBorder
        waveView.activeColor = isWarn ? Self.warnWaveActive : Self.recWaveActive
        waveView.quietColor = isWarn ? Self.warnWaveQuiet : Self.recWaveQuiet
        durLabel.textColor = isWarn ? Self.warnMainText : Self.recMainText
        hintLabel.textColor = (isWarn || holdCancelPreview) ? Self.warnHintTextCol : Self.recHintTextCol

        iconView.isHidden = true
        label.isHidden = true
        waveView.isHidden = false
        durLabel.isHidden = false

        durLabel.stringValue = slotTpl
        durLabel.sizeToFit()
        let tplW = ceil(durLabel.frame.size.width)
        let tplH = ceil(durLabel.frame.size.height)
        durLabel.stringValue = durStr
        durLabel.sizeToFit()
        let actW = ceil(durLabel.frame.size.width)
        let actH = ceil(durLabel.frame.size.height)
        let slotW = max(actW, tplW)
        let durH = max(tplH, actH)

        var hintW: CGFloat = 0.0
        var hintH: CGFloat = 0.0
        if !effHint.isEmpty {
            var tplHintW: CGFloat = 0.0
            if isWarn && !holdCancelPreview {
                hintLabel.stringValue = hintSlotTpl
                hintLabel.sizeToFit()
                tplHintW = ceil(hintLabel.frame.size.width)
            }
            hintLabel.stringValue = effHint
            hintLabel.sizeToFit()
            hintW = max(ceil(hintLabel.frame.size.width), tplHintW)
            hintH = ceil(hintLabel.frame.size.height)
            hintLabel.isHidden = false
        } else {
            hintLabel.stringValue = ""
            hintLabel.isHidden = true
        }

        var badgeTextW: CGFloat = 0.0
        var badgeTextH: CGFloat = 0.0
        let hasBadge = (recBgCount > 0)
        if hasBadge, let bl = bgBadgeLabel {
            bl.stringValue = "+\(recBgCount)"
            bl.sizeToFit()
            badgeTextW = ceil(bl.frame.size.width)
            badgeTextH = ceil(bl.frame.size.height)
            bgBadgeView?.isHidden = false
        } else {
            bgBadgeLabel?.stringValue = ""
            bgBadgeView?.isHidden = true
        }

        let (scale, vf) = screenMetrics ?? resolveAnchoredScreenMetrics(refresh: false)
        let geom = computeWaveHudLayout(
            durSlotW: slotW,
            durH: durH,
            hintW: hintW,
            hintH: hintH,
            hasHint: !effHint.isEmpty,
            badgeTextW: badgeTextW,
            badgeTextH: badgeTextH,
            hasBadge: hasBadge,
            visibleWidth: vf.width,
            scale: scale
        )
        let pillW = geom.pillW
        let pillH = geom.pillH
        let pillX = snapToPixel(vf.midX - pillW / 2.0, scale: scale)
        let pillY = computeHudPillY(visibleMinY: vf.minY, visibleMaxY: vf.maxY, pillH: pillH, position: recPosition)
        currentMainPillH = pillH

        panel.setFrame(NSRect(x: pillX, y: pillY, width: pillW, height: pillH), display: true)
        syncToastToAnchor(position: recPosition)
        bgView.frame = NSRect(x: 0, y: 0, width: pillW, height: pillH)
        bgView.layer?.contentsScale = scale
        bgView.layer?.cornerRadius = pillH / 2.0
        bgView.layer?.backgroundColor = effBg.cgColor
        bgView.layer?.borderColor = effBorder.cgColor
        bgView.layer?.borderWidth = 1.0

        durLabel.frame = alignSubviewRect(geom.durFrame, in: bgView, scale: scale)
        waveView.frame = alignSubviewRect(geom.waveFrame, in: bgView, scale: scale)
        waveView.layer?.contentsScale = scale

        if !effHint.isEmpty {
            hintLabel.frame = alignSubviewRect(geom.hintFrame, in: bgView, scale: scale)
        }
        if hasBadge, let bv = bgBadgeView, let bl = bgBadgeLabel {
            let alignedBadge = alignSubviewRect(geom.badgeFrame, in: bgView, scale: scale)
            bv.frame = alignedBadge
            bv.layer?.contentsScale = scale
            bv.layer?.cornerRadius = alignedBadge.height / 2.0
            bv.layer?.backgroundColor = (isWarn ? Self.warnBadgeBg : Self.recBadgeBg).cgColor
            bv.layer?.borderColor = (isWarn ? Self.warnBadgeBorder : Self.recBadgeBorder).cgColor
            bv.layer?.borderWidth = 1.0
            bl.textColor = isWarn ? Self.warnBadgeTextCol : Self.recBadgeTextCol
            bl.frame = alignSubviewRect(geom.badgeLabelFrame, in: bv, scale: scale)
        }
    }

    func hideMain() {
        hideWorkItem?.cancel(); hideWorkItem = nil
        holdPreviewSafetyWorkItem?.cancel(); holdPreviewSafetyWorkItem = nil
        stopWaveTimer()
        holdCancelPreview = false; lastHoldPreviewState = false
        currentMainState = ""; currentMainText = ""; currentMainStyle = nil
        maxDurationSec = 0; warnRemainSec = 0
        recBgCount = 0; lastBgCount = 0
        bgBadgeView?.isHidden = true
        isMainVisible = false
        currentMainPillH = 36.0
        panel?.orderOut(nil)
        if !isToastVisible {
            anchorSessionGen &+= 1
            anchoredWindowPid = 0
            anchoredWindowId = 0
        }
    }

    func setHoldCancelPreview(_ active: Bool) {
        holdPreviewSafetyWorkItem?.cancel(); holdPreviewSafetyWorkItem = nil
        let target = active && canShowHoldCancelPreview
        if holdCancelPreview == target { return }
        holdCancelPreview = target
        if headless || !isMainVisible { return }
        if isWaveRecording {
            layoutWavePill(forceFrame: true)
        } else if let style = currentMainStyle {
            renderNonWaveMainPill(style: style, rawText: currentMainText, position: currentMainPosition, zh: currentMainZh)
        }
    }

    func scheduleHoldPreviewSafetyClear(delayMs: Int = 400) {
        holdPreviewSafetyWorkItem?.cancel(); holdPreviewSafetyWorkItem = nil
        guard holdCancelPreview, !headless else { return }
        let work = DispatchWorkItem { [weak self] in
            self?.holdPreviewSafetyWorkItem = nil
            self?.setHoldCancelPreview(false)
        }
        holdPreviewSafetyWorkItem = work
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(max(50, delayMs)), execute: work)
    }

    private func renderNonWaveMainPill(
        style: HudVisualStyle,
        rawText: String,
        position: String,
        zh: Bool
    ) {
        guard let panel = panel, let bgView = bgView, let iconView = iconView, let label = label else { return }
        let baseSeg = splitHudSegments(rawText)
        let effZh = rawText.isEmpty ? zh : containsCjkScalar(rawText)
        let seg: HudSegments
        let hintColor: NSColor
        if holdCancelPreview {
            seg = HudSegments(
                icon: baseSeg.icon,
                main: baseSeg.main,
                hint: "(\(formatHoldCancelHint(zh: effZh)))",
                hasSpaceBeforeHint: true
            )
            hintColor = Self.warnHintTextCol
        } else {
            seg = baseSeg
            hintColor = style.hintText.nsColor
        }
        let mainColor = style.mainText.nsColor
        let iconColor = style.icon.nsColor
        let bgCol = style.bg.nsColor
        let bd = style.border.nsColor

        let hasIcon = !seg.icon.isEmpty || !style.iconKind.isEmpty
        let hasBody = !seg.main.isEmpty || !seg.hint.isEmpty
        let para = NSMutableParagraphStyle()
        para.alignment = hasIcon ? .left : .center
        para.lineBreakMode = .byTruncatingTail
        let attr = NSMutableAttributedString()
        if !seg.main.isEmpty {
            attr.append(NSAttributedString(string: seg.main, attributes: [
                .font: Self.hudFont, .foregroundColor: mainColor, .paragraphStyle: para,
            ]))
        }
        if !seg.hint.isEmpty {
            if !seg.hasSpaceBeforeHint && attr.length > 0 {
                attr.addAttribute(.kern, value: 1.5, range: NSRange(location: attr.length - 1, length: 1))
            }
            let prefixSpace = (seg.hasSpaceBeforeHint && attr.length > 0) ? " " : ""
            attr.append(NSAttributedString(string: prefixSpace + seg.hint, attributes: [
                .font: Self.hudFont, .foregroundColor: hintColor, .paragraphStyle: para,
            ]))
        }
        label.attributedStringValue = attr
        label.sizeToFit()
        label.isHidden = !hasBody

        let textSize = hasBody ? label.frame.size : NSSize(width: 0, height: 18)
        let (scale, vf) = resolveAnchoredScreenMetrics(refresh: false)
        let geom = computeStatusHudLayout(
            isToast: false,
            hasIcon: hasIcon,
            hasBody: hasBody,
            textW: textSize.width,
            textH: textSize.height,
            visibleWidth: vf.width,
            scale: scale
        )
        let pillW = geom.pillW
        let pillH = geom.pillH
        let pillX = snapToPixel(vf.midX - pillW / 2.0, scale: scale)
        let pillY = computeHudPillY(visibleMinY: vf.minY, visibleMaxY: vf.maxY, pillH: pillH, position: position)
        currentMainPillH = pillH

        panel.setFrame(NSRect(x: pillX, y: pillY, width: pillW, height: pillH), display: false)
        syncToastToAnchor(position: position)
        bgView.frame = NSRect(x: 0, y: 0, width: pillW, height: pillH)
        bgView.layer?.contentsScale = scale
        bgView.layer?.cornerRadius = pillH / 2.0
        bgView.layer?.backgroundColor = bgCol.cgColor
        bgView.layer?.borderColor = bd.cgColor
        bgView.layer?.borderWidth = style.borderWidth

        if hasIcon {
            iconView.configuredSize = StatusIconView.mainBoxSize
            iconView.iconKind = Self.resolveIconKind(stateKind: style.iconKind, iconSeg: seg.icon)
            iconView.iconColor = iconColor
            iconView.frame = alignSubviewRect(geom.iconFrame, in: bgView, scale: scale)
            iconView.layer?.contentsScale = scale
            iconView.isHidden = false
        } else {
            iconView.isHidden = true
        }
        if hasBody {
            label.frame = alignSubviewRect(geom.labelFrame, in: bgView, scale: scale)
        }
    }

    func hideToast() {
        toastHideWorkItem?.cancel(); toastHideWorkItem = nil
        isToastVisible = false
        toastPanel?.orderOut(nil)
        if !isMainVisible {
            anchorSessionGen &+= 1
            anchoredWindowPid = 0
            anchoredWindowId = 0
        }
    }

    func hide() {
        hideMain()
    }

    static func resolveIconKind(stateKind: String, iconSeg: String) -> String {
        if !stateKind.isEmpty { return stateKind }
        switch iconSeg {
        case "◐": return "recognizing"
        case "◈": return "refining"
        case "✓": return "done"
        case "○": return "empty_ring"
        case "!", "⚠", "⚠️": return "warning"
        default: return "cross"
        }
    }

    private func updateMain(
        state st: String,
        style: HudVisualStyle,
        customText: String,
        durationMs: Int,
        position: String,
        maxDurationSec: Int,
        warnRemainSec: Int,
        zh: Bool,
        bgCount: Int = 0,
        preserveHoldPreview: Bool = false
    ) {
        hideWorkItem?.cancel(); hideWorkItem = nil
        holdPreviewSafetyWorkItem?.cancel(); holdPreviewSafetyWorkItem = nil

        var txt = style.defaultText
        var waveMode = false
        if st == "recording" {
            txt = zh
                ? "● 录音中… (\(isPtt ? "松开" : "按")\(hotkeyLabel) 结束)"
                : "● Listening… (\(isPtt ? "release" : "press") \(hotkeyLabelEn) to end)"
            let trimmedCustom = customText.trimmingCharacters(in: .whitespacesAndNewlines)
            waveMode = trimmedCustom.isEmpty || trimmedCustom.hasPrefix("● ")
        }
        if !customText.isEmpty { txt = customText }
        let autoHideMs = (durationMs > 0) ? durationMs : style.autoHideMs
        let seg = splitHudSegments(txt)

        currentMainState = st
        currentMainText = txt
        currentMainStyle = style
        currentMainPosition = position
        currentMainZh = zh
        holdCancelPreview = preserveHoldPreview && isCancellableMainHudState(st)

        if headless {
            if waveMode {
                recHintText = seg.hint
                recZhHint = txt.isEmpty ? zh : containsCjkScalar(txt)
                recPosition = position
                recBgCount = max(0, bgCount)
                self.maxDurationSec = max(0, maxDurationSec)
                self.warnRemainSec = (self.maxDurationSec > 0) ? (warnRemainSec > 0 ? warnRemainSec : recordingCountdownWarnSec(self.maxDurationSec)) : 0
                if !isWaveRecording {
                    isWaveRecording = true
                    recStartMs = monotonicMs()
                }
            } else {
                stopWaveTimer()
                self.maxDurationSec = 0; self.warnRemainSec = 0
                recBgCount = 0; lastBgCount = 0
            }
            isMainVisible = true
            return
        }

        ensurePanel()
        guard let panel = panel else { return }

        if waveMode {
            recHintText = seg.hint
            recZhHint = txt.isEmpty ? zh : containsCjkScalar(txt)
            recPosition = position
            recBgCount = max(0, bgCount)
            self.maxDurationSec = max(0, maxDurationSec)
            self.warnRemainSec = (self.maxDurationSec > 0) ? (warnRemainSec > 0 ? warnRemainSec : recordingCountdownWarnSec(self.maxDurationSec)) : 0
            var preResolvedMetrics: (scale: CGFloat, visibleFrame: NSRect)? = nil
            if !isWaveRecording {
                preResolvedMetrics = resolveAnchoredScreenMetrics(refresh: !isToastVisible)
                isWaveRecording = true
                recStartMs = monotonicMs()
                smoothLevel = 0.0
                waveHist = Array(repeating: 0.0, count: 5)
                frameIdx = 0
                lastDurText = ""
                lastSlotTemplate = ""
                lastHintSlotTemplate = ""
                lastWarnState = false
                lastBgCount = -1
                lastHoldPreviewState = false
                let initLvl = currentLiveLevel()
                waveView?.barNorms = (initLvl > 0.0) ? stepWaveEnvelope(initLvl) : Array(repeating: 0.0, count: 9)
            }
            layoutWavePill(forceFrame: true, screenMetrics: preResolvedMetrics)
            isMainVisible = true
            panel.orderFrontRegardless()
            startWaveTimerIfNeeded()
        } else {
            stopWaveTimer()
            self.maxDurationSec = 0; self.warnRemainSec = 0
            recBgCount = 0; lastBgCount = 0
            waveView?.isHidden = true
            durLabel?.isHidden = true
            hintLabel?.isHidden = true
            bgBadgeView?.isHidden = true

            renderNonWaveMainPill(style: style, rawText: txt, position: position, zh: zh)
            isMainVisible = true
            panel.orderFrontRegardless()
        }

        if autoHideMs > 0 {
            let work = DispatchWorkItem { [weak self] in self?.hideMain() }
            hideWorkItem = work
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(autoHideMs), execute: work)
        }
    }

    private func ensureToastSlot() {
        if !toastSlotAllocated {
            toastSlotAllocated = true
            toastSlotCount += 1
        }
        if !headless {
            ensureToastPanel()
        }
    }

    private func updateToast(
        style: HudVisualStyle,
        customText: String,
        durationMs: Int,
        position: String
    ) {
        toastHideWorkItem?.cancel(); toastHideWorkItem = nil
        let rawTxt = customText.isEmpty ? style.defaultText : customText
        let autoHideMs = (durationMs > 0) ? durationMs : style.autoHideMs
        ensureToastSlot()
        toastUpdateCount += 1
        toastLastText = rawTxt
        toastLastAutoHideMs = autoHideMs
        if headless {
            isToastVisible = true
            return
        }
        guard let toastPanel = toastPanel, let toastBgView = toastBgView,
              let toastIconView = toastIconView, let toastLabel = toastLabel else { return }

        let bodyTxt = stripLeadingHudSymbol(rawTxt)
        let seg = splitHudSegments(rawTxt)
        let effIconKind = Self.resolveIconKind(stateKind: style.iconKind, iconSeg: seg.icon)
        let hasIcon = !effIconKind.isEmpty
        let hasBody = !bodyTxt.isEmpty

        let para = NSMutableParagraphStyle()
        para.alignment = hasIcon ? .left : .center
        para.lineBreakMode = .byTruncatingTail
        if !seg.hint.isEmpty {
            let attr = NSMutableAttributedString()
            if !seg.main.isEmpty {
                attr.append(NSAttributedString(string: seg.main, attributes: [
                    .font: Self.toastFont,
                    .foregroundColor: style.mainText.nsColor,
                    .paragraphStyle: para,
                ]))
            }
            if !seg.hasSpaceBeforeHint && attr.length > 0 {
                attr.addAttribute(.kern, value: 1.5, range: NSRange(location: attr.length - 1, length: 1))
            }
            let prefixSpace = (seg.hasSpaceBeforeHint && attr.length > 0) ? " " : ""
            attr.append(NSAttributedString(string: prefixSpace + seg.hint, attributes: [
                .font: Self.toastFont,
                .foregroundColor: style.hintText.nsColor,
                .paragraphStyle: para,
            ]))
            toastLabel.attributedStringValue = attr
        } else {
            toastLabel.attributedStringValue = NSAttributedString(string: bodyTxt, attributes: [
                .font: Self.toastFont,
                .foregroundColor: style.mainText.nsColor,
                .paragraphStyle: para,
            ])
        }
        toastLabel.sizeToFit()
        toastLabel.isHidden = !hasBody

        let textSize = hasBody ? toastLabel.frame.size : NSSize(width: 0, height: 17)
        let (scale, vf) = resolveAnchoredScreenMetrics(refresh: !(isMainVisible || isToastVisible))
        let geom = computeStatusHudLayout(
            isToast: true,
            hasIcon: hasIcon,
            hasBody: hasBody,
            textW: textSize.width,
            textH: textSize.height,
            visibleWidth: vf.width,
            scale: scale
        )
        let toastW = geom.pillW
        let toastH = geom.pillH
        let toastX = snapToPixel(vf.midX - toastW / 2.0, scale: scale)
        let mainSlotH: CGFloat = isMainVisible ? max(36.0, currentMainPillH) : 36.0
        let toastY = computeHudToastY(visibleMinY: vf.minY, visibleMaxY: vf.maxY, mainH: mainSlotH, toastH: toastH, position: position)
        lastToastPosition = position
        currentToastSize = NSSize(width: toastW, height: toastH)

        toastPanel.setFrame(NSRect(x: toastX, y: toastY, width: toastW, height: toastH), display: false)
        toastBgView.frame = NSRect(x: 0, y: 0, width: toastW, height: toastH)
        toastBgView.layer?.contentsScale = scale
        toastBgView.layer?.cornerRadius = toastH / 2.0
        toastBgView.layer?.backgroundColor = style.bg.nsColor.cgColor
        toastBgView.layer?.borderColor = style.border.nsColor.cgColor
        toastBgView.layer?.borderWidth = style.borderWidth

        if hasIcon {
            toastIconView.configuredSize = StatusIconView.toastBoxSize
            toastIconView.iconKind = effIconKind
            toastIconView.iconColor = style.icon.nsColor
            toastIconView.frame = alignSubviewRect(geom.iconFrame, in: toastBgView, scale: scale)
            toastIconView.layer?.contentsScale = scale
            toastIconView.isHidden = false
        } else {
            toastIconView.isHidden = true
        }
        if hasBody {
            toastLabel.frame = alignSubviewRect(geom.labelFrame, in: toastBgView, scale: scale)
        }
        isToastVisible = true
        toastPanel.orderFrontRegardless()

        if autoHideMs > 0 {
            let work = DispatchWorkItem { [weak self] in self?.hideToast() }
            toastHideWorkItem = work
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(autoHideMs), execute: work)
        }
    }

    func update(
        state: String,
        customText: String,
        durationMs: Int,
        position: String = "bottom",
        maxDurationSec: Int = 0,
        warnRemainSec: Int = 0,
        layer: String = "",
        clearMain: Bool? = nil,
        clearToast: Bool? = nil,
        bgCount: Int = 0,
        preserveHoldPreview: Bool = false
    ) {
        let st = state.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if st.isEmpty || st == "idle" || st == "hide" {
            hideMain()
            if clearToast == true { hideToast() }
            return
        }
        if st == "hide_toast" {
            hideToast()
            return
        }
        if st == "hide_all" {
            hideMain()
            hideToast()
            return
        }

        let zh = isZhUiCulture(hostUiCulture())
        let style = resolveHudVisualStyle(state: st, explicitLayer: layer, zh: zh)
        if style.layer == "main" {
            if clearToast == true || (st == "recording" && clearToast == nil) {
                hideToast()
            }
            updateMain(
                state: st,
                style: style,
                customText: customText,
                durationMs: durationMs,
                position: position,
                maxDurationSec: maxDurationSec,
                warnRemainSec: warnRemainSec,
                zh: zh,
                bgCount: bgCount,
                preserveHoldPreview: preserveHoldPreview
            )
        } else {
            updateToast(
                style: style,
                customText: customText,
                durationMs: durationMs,
                position: position
            )
            if clearMain == true {
                hideMain()
            }
        }
    }

    func close() {
        hideMain()
        hideToast()
        panel?.close(); panel = nil
        toastPanel?.close(); toastPanel = nil
        toastSlotAllocated = false
    }
}
