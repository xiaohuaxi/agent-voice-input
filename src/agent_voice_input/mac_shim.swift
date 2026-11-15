import AppKit
import ApplicationServices
import Foundation

// Device-dependent modifier masks from IOKit/hidsystem/IOLLEvent.h
private let NX_DEVICELCTLKEYMASK: UInt   = 0x00000001
private let NX_DEVICELSHIFTKEYMASK: UInt = 0x00000002
private let NX_DEVICERSHIFTKEYMASK: UInt = 0x00000004
private let NX_DEVICELCMDKEYMASK: UInt   = 0x00000008
private let NX_DEVICERCMDKEYMASK: UInt   = 0x00000010
private let NX_DEVICELALTKEYMASK: UInt   = 0x00000020
private let NX_DEVICERALTKEYMASK: UInt   = 0x00000040
private let NX_DEVICERCTLKEYMASK: UInt   = 0x00002000

private let FLAG_CAPSLOCK: UInt = NSEvent.ModifierFlags.capsLock.rawValue
private let FLAG_SHIFT: UInt    = NSEvent.ModifierFlags.shift.rawValue
private let FLAG_CONTROL: UInt  = NSEvent.ModifierFlags.control.rawValue
private let FLAG_OPTION: UInt   = NSEvent.ModifierFlags.option.rawValue
private let FLAG_COMMAND: UInt  = NSEvent.ModifierFlags.command.rawValue
private let FLAG_FUNCTION: UInt = NSEvent.ModifierFlags.function.rawValue

enum RawEventKind { case flagsChanged, keyDown, keyUp }

struct TargetKeySpec {
    let name: String
    let hudLabel: String
    let hudLabelEn: String
    let primaryKeyCode: UInt16
    let altKeyCodes: Set<UInt16>
    let isModifier: Bool
    let highFlag: UInt
    let deviceMask: UInt
    let oppositeDeviceMask: UInt

    func matchesKeyCode(_ kc: UInt16) -> Bool { kc == primaryKeyCode || altKeyCodes.contains(kc) }
}

struct EmittedEvent {
    let kind: String
    let vk: UInt16
    let flags: UInt
    let isDown: Bool
    let tainted: Bool
}

private let FUNCTION_KEY_CODES: [Int: UInt16] = [
    1: 0x7A, 2: 0x78, 3: 0x63, 4: 0x76, 5: 0x60, 6: 0x61, 7: 0x62, 8: 0x64, 9: 0x65, 10: 0x6D,
    11: 0x67, 12: 0x6F, 13: 0x69, 14: 0x6B, 15: 0x71, 16: 0x6A, 17: 0x40, 18: 0x4F, 19: 0x50, 20: 0x5A,
]

private let MODIFIER_TABLE: [([String], TargetKeySpec)] = [
    (["rcontrol", "rctrl", "rightcontrol", "rightctrl"], TargetKeySpec(name: "RControl", hudLabel: "右 Ctrl", hudLabelEn: "Right Ctrl", primaryKeyCode: 0x3E, altKeyCodes: [], isModifier: true, highFlag: FLAG_CONTROL, deviceMask: NX_DEVICERCTLKEYMASK, oppositeDeviceMask: NX_DEVICELCTLKEYMASK)),
    (["lcontrol", "lctrl", "leftcontrol", "leftctrl"], TargetKeySpec(name: "LControl", hudLabel: "左 Ctrl", hudLabelEn: "Left Ctrl", primaryKeyCode: 0x3B, altKeyCodes: [], isModifier: true, highFlag: FLAG_CONTROL, deviceMask: NX_DEVICELCTLKEYMASK, oppositeDeviceMask: NX_DEVICERCTLKEYMASK)),
    (["control", "ctrl"], TargetKeySpec(name: "LControl", hudLabel: " Ctrl", hudLabelEn: "Ctrl", primaryKeyCode: 0x3B, altKeyCodes: [], isModifier: true, highFlag: FLAG_CONTROL, deviceMask: NX_DEVICELCTLKEYMASK, oppositeDeviceMask: NX_DEVICERCTLKEYMASK)),
    (["ralt", "roption", "rightalt", "rightoption", "rmenu"], TargetKeySpec(name: "ROption", hudLabel: "右 Option", hudLabelEn: "Right Option", primaryKeyCode: 0x3D, altKeyCodes: [], isModifier: true, highFlag: FLAG_OPTION, deviceMask: NX_DEVICERALTKEYMASK, oppositeDeviceMask: NX_DEVICELALTKEYMASK)),
    (["lalt", "loption", "leftalt", "leftoption", "lmenu"], TargetKeySpec(name: "LOption", hudLabel: "左 Option", hudLabelEn: "Left Option", primaryKeyCode: 0x3A, altKeyCodes: [], isModifier: true, highFlag: FLAG_OPTION, deviceMask: NX_DEVICELALTKEYMASK, oppositeDeviceMask: NX_DEVICERALTKEYMASK)),
    (["alt", "option"], TargetKeySpec(name: "LOption", hudLabel: " Option", hudLabelEn: "Option", primaryKeyCode: 0x3A, altKeyCodes: [], isModifier: true, highFlag: FLAG_OPTION, deviceMask: NX_DEVICELALTKEYMASK, oppositeDeviceMask: NX_DEVICERALTKEYMASK)),
    (["rcommand", "rcmd", "rightcommand", "rightcmd"], TargetKeySpec(name: "RCommand", hudLabel: "右 Cmd", hudLabelEn: "Right Cmd", primaryKeyCode: 0x36, altKeyCodes: [], isModifier: true, highFlag: FLAG_COMMAND, deviceMask: NX_DEVICERCMDKEYMASK, oppositeDeviceMask: NX_DEVICELCMDKEYMASK)),
    (["lcommand", "lcmd", "leftcommand", "leftcmd"], TargetKeySpec(name: "LCommand", hudLabel: "左 Cmd", hudLabelEn: "Left Cmd", primaryKeyCode: 0x37, altKeyCodes: [], isModifier: true, highFlag: FLAG_COMMAND, deviceMask: NX_DEVICELCMDKEYMASK, oppositeDeviceMask: NX_DEVICERCMDKEYMASK)),
    (["command", "cmd"], TargetKeySpec(name: "LCommand", hudLabel: " Cmd", hudLabelEn: "Cmd", primaryKeyCode: 0x37, altKeyCodes: [], isModifier: true, highFlag: FLAG_COMMAND, deviceMask: NX_DEVICELCMDKEYMASK, oppositeDeviceMask: NX_DEVICERCMDKEYMASK)),
    (["rshift", "rightshift"], TargetKeySpec(name: "RShift", hudLabel: "右 Shift", hudLabelEn: "Right Shift", primaryKeyCode: 0x3C, altKeyCodes: [], isModifier: true, highFlag: FLAG_SHIFT, deviceMask: NX_DEVICERSHIFTKEYMASK, oppositeDeviceMask: NX_DEVICELSHIFTKEYMASK)),
    (["lshift", "leftshift"], TargetKeySpec(name: "LShift", hudLabel: "左 Shift", hudLabelEn: "Left Shift", primaryKeyCode: 0x38, altKeyCodes: [], isModifier: true, highFlag: FLAG_SHIFT, deviceMask: NX_DEVICELSHIFTKEYMASK, oppositeDeviceMask: NX_DEVICERSHIFTKEYMASK)),
    (["shift"], TargetKeySpec(name: "LShift", hudLabel: " Shift", hudLabelEn: "Shift", primaryKeyCode: 0x38, altKeyCodes: [], isModifier: true, highFlag: FLAG_SHIFT, deviceMask: NX_DEVICELSHIFTKEYMASK, oppositeDeviceMask: NX_DEVICERSHIFTKEYMASK)),
    (["fn", "function", "globe"], TargetKeySpec(name: "Fn", hudLabel: " Fn", hudLabelEn: "Fn", primaryKeyCode: 0x3F, altKeyCodes: [0xB3], isModifier: true, highFlag: FLAG_FUNCTION, deviceMask: 0, oppositeDeviceMask: 0)),
    (["capslock"], TargetKeySpec(name: "CapsLock", hudLabel: " CapsLock", hudLabelEn: "CapsLock", primaryKeyCode: 0x39, altKeyCodes: [], isModifier: true, highFlag: FLAG_CAPSLOCK, deviceMask: 0, oppositeDeviceMask: 0)),
]

private let KEY_CODE_NAMES: [UInt16: String] = {
    var m: [UInt16: String] = [
        0x3E: "RControl", 0x3B: "LControl", 0x3D: "ROption", 0x3A: "LOption",
        0x36: "RCommand", 0x37: "LCommand", 0x3C: "RShift", 0x38: "LShift",
        0x39: "CapsLock", 0x3F: "Fn", 0xB3: "Globe", 0x31: "Space", 0x24: "Return", 0x30: "Tab", 0x35: "Escape",
    ]
    for (fn, kc) in FUNCTION_KEY_CODES { m[kc] = "F\(fn)" }
    return m
}()

func resolveTargetKey(_ raw: String) throws -> TargetKeySpec {
    let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    let k = (trimmed.isEmpty ? "RControl" : trimmed).lowercased()
    for (aliases, spec) in MODIFIER_TABLE where aliases.contains(k) { return spec }
    if k == "space" {
        return TargetKeySpec(name: "Space", hudLabel: " Space", hudLabelEn: "Space", primaryKeyCode: 0x31, altKeyCodes: [], isModifier: false, highFlag: 0, deviceMask: 0, oppositeDeviceMask: 0)
    }
    if k.hasPrefix("f"), let fn = Int(k.dropFirst()), let kc = FUNCTION_KEY_CODES[fn] {
        return TargetKeySpec(name: "F\(fn)", hudLabel: " F\(fn)", hudLabelEn: "F\(fn)", primaryKeyCode: kc, altKeyCodes: [], isModifier: false, highFlag: 0, deviceMask: 0, oppositeDeviceMask: 0)
    }
    if k.hasPrefix("0x"), let parsed = UInt16(k.dropFirst(2), radix: 16) {
        for (_, spec) in MODIFIER_TABLE where spec.primaryKeyCode == parsed { return spec }
        let hex = String(format: "0x%02X", parsed)
        return TargetKeySpec(name: hex, hudLabel: " \(hex)", hudLabelEn: hex, primaryKeyCode: parsed, altKeyCodes: [], isModifier: false, highFlag: 0, deviceMask: 0, oppositeDeviceMask: 0)
    }
    throw NSError(domain: "MacShim", code: 2, userInfo: [NSLocalizedDescriptionKey: "Unsupported macOS hotkey name: \(raw)"])
}

struct HotkeyEngine {
    let spec: TargetKeySpec
    let isPtt: Bool
    let tapMaxMs: UInt64
    let debounceMs: UInt64
    let keyTest: Bool

    private(set) var targetDown = false
    private(set) var pttActive = false
    private(set) var chordTainted = false
    private var downTickMs: UInt64 = 0
    private var lastFireTickMs: UInt64 = 0
    private var hasFiredOnce = false
    private var downKeyCounter: UInt32 = 0
    private(set) var otherKeysDown: Set<UInt16> = []

    init(spec: TargetKeySpec, isPtt: Bool, tapMaxMs: Int, debounceMs: Int, keyTest: Bool) {
        self.spec = spec
        self.isPtt = isPtt
        self.tapMaxMs = UInt64(max(50, tapMaxMs))
        self.debounceMs = UInt64(max(0, debounceMs))
        self.keyTest = keyTest
    }

    private func debouncePassed(_ nowMs: UInt64) -> Bool {
        !hasFiredOnce || (nowMs >= lastFireTickMs && (nowMs - lastFireTickMs) >= debounceMs)
    }

    private func hasOtherModifiersActive(_ rawFlags: UInt) -> Bool {
        let allHigh = FLAG_SHIFT | FLAG_CONTROL | FLAG_OPTION | FLAG_COMMAND
        if !spec.isModifier { return (rawFlags & allHigh) != 0 }
        if (rawFlags & (allHigh & ~spec.highFlag)) != 0 { return true }
        return spec.oppositeDeviceMask != 0 && (rawFlags & spec.oppositeDeviceMask) != 0
    }

    private func evaluateModifierDown(kind: RawEventKind, keyCode: UInt16, rawFlags: UInt) -> Bool {
        if spec.deviceMask != 0 {
            if (rawFlags & spec.deviceMask) != 0 { return true }
            if (rawFlags & spec.highFlag) == 0 { return false }
            if spec.oppositeDeviceMask != 0 && (rawFlags & spec.oppositeDeviceMask) != 0 { return false }
            return (kind == .flagsChanged && spec.matchesKeyCode(keyCode)) ? !targetDown : targetDown
        }
        if spec.highFlag == FLAG_FUNCTION {
            return (kind == .flagsChanged && spec.matchesKeyCode(keyCode)) ? ((rawFlags & FLAG_FUNCTION) != 0) : targetDown
        }
        if spec.highFlag == FLAG_CAPSLOCK {
            return (kind == .flagsChanged && spec.matchesKeyCode(keyCode)) ? !targetDown : targetDown
        }
        return targetDown
    }

    mutating func handleEvent(
        kind: RawEventKind,
        keyCode: UInt16,
        rawFlags: UInt,
        isARepeat: Bool,
        nowMs: UInt64,
        keyDownCounter: UInt32,
        isKeyPhysicallyDown: (UInt16) -> Bool = { _ in true }
    ) -> [EmittedEvent] {
        if kind == .keyDown && isARepeat { return [] }
        // Prune stale keys whose .keyUp was swallowed by Cmd+Tab, lock screen, or Secure Input
        if !otherKeysDown.isEmpty {
            otherKeysDown = otherKeysDown.filter(isKeyPhysicallyDown)
        }

        let nowDown: Bool
        let actionVk: UInt16
        if spec.isModifier {
            nowDown = evaluateModifierDown(kind: kind, keyCode: keyCode, rawFlags: rawFlags)
            actionVk = spec.primaryKeyCode
        } else if spec.matchesKeyCode(keyCode) && (kind == .keyDown || kind == .keyUp) {
            nowDown = (kind == .keyDown)
            actionVk = keyCode
        } else {
            nowDown = targetDown
            actionVk = keyCode
        }
        let isTargetTransition = (nowDown != targetDown)

        var out: [EmittedEvent] = []
        if keyTest {
            let dispVk = (kind == .flagsChanged && keyCode == 0x3B && (rawFlags & NX_DEVICERCTLKEYMASK) != 0) ? UInt16(0x3E) : keyCode
            let allMods = FLAG_SHIFT | FLAG_CONTROL | FLAG_OPTION | FLAG_COMMAND | FLAG_FUNCTION | FLAG_CAPSLOCK
            let evDown = (kind == .keyDown) ? true : ((kind == .keyUp) ? false : (isTargetTransition ? nowDown : ((rawFlags & allMods) != 0)))
            out.append(EmittedEvent(kind: "key_event", vk: dispVk, flags: rawFlags, isDown: evDown, tainted: chordTainted))
        }

        if nowDown && !targetDown {
            targetDown = true
            downTickMs = nowMs
            downKeyCounter = keyDownCounter
            chordTainted = !otherKeysDown.isEmpty || hasOtherModifiersActive(rawFlags)
            if isPtt && !chordTainted && debouncePassed(nowMs) {
                lastFireTickMs = nowMs; hasFiredOnce = true; pttActive = true
                out.append(EmittedEvent(kind: "ptt_start", vk: actionVk, flags: rawFlags, isDown: true, tainted: false))
            }
        } else if !nowDown && targetDown {
            if (spec.isModifier && keyDownCounter != downKeyCounter) || hasOtherModifiersActive(rawFlags) || !otherKeysDown.isEmpty {
                chordTainted = true
            }
            if isPtt {
                if pttActive {
                    if !chordTainted {
                        lastFireTickMs = nowMs; hasFiredOnce = true
                        out.append(EmittedEvent(kind: "ptt_stop", vk: actionVk, flags: rawFlags, isDown: false, tainted: false))
                    } else {
                        out.append(EmittedEvent(kind: "ptt_cancel", vk: actionVk, flags: rawFlags, isDown: false, tainted: true))
                    }
                }
            } else if !chordTainted && nowMs >= downTickMs && (nowMs - downTickMs) <= tapMaxMs && debouncePassed(nowMs) {
                lastFireTickMs = nowMs; hasFiredOnce = true
                out.append(EmittedEvent(kind: "toggle", vk: actionVk, flags: rawFlags, isDown: false, tainted: false))
            }
            targetDown = false; pttActive = false; chordTainted = false
        } else {
            if kind == .keyDown { otherKeysDown.insert(keyCode) }
            else if kind == .keyUp { otherKeysDown.remove(keyCode) }
            let modTaint = (kind == .flagsChanged) && (spec.isModifier ? !spec.matchesKeyCode(keyCode) : hasOtherModifiersActive(rawFlags))
            if targetDown && (kind == .keyDown || kind == .keyUp || modTaint || hasOtherModifiersActive(rawFlags)) {
                chordTainted = true
                if isPtt && pttActive {
                    pttActive = false
                    out.append(EmittedEvent(kind: "ptt_cancel", vk: keyCode, flags: rawFlags, isDown: kind == .keyDown, tainted: true))
                }
            }
        }
        return out
    }
}

private func rgba(_ r: Int, _ g: Int, _ b: Int, _ a: CGFloat = 1.0) -> NSColor {
    NSColor(srgbRed: CGFloat(r) / 255.0, green: CGFloat(g) / 255.0, blue: CGFloat(b) / 255.0, alpha: a)
}

private func hostUiCulture() -> String {
    for raw in Locale.preferredLanguages {
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.isEmpty { return s }
    }
    return Locale.current.identifier.trimmingCharacters(in: .whitespacesAndNewlines)
}

private func isZhUiCulture(_ tag: String) -> Bool {
    let low = tag.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return low == "zh" || low.hasPrefix("zh-") || low.hasPrefix("zh_") || low.hasPrefix("zh.") || low.hasPrefix("zh@")
}

func computeHudPillY(visibleMinY: CGFloat, visibleMaxY: CGFloat, pillH: CGFloat, position: String) -> CGFloat {
    let pos = position.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if pos == "top" {
        return round(visibleMaxY - pillH - 14.0)
    }
    return round(visibleMinY + 8.0)
}

func stripOuterParens(_ raw: String) -> String {
    let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
    if t.count >= 2 && t.first == "(" && t.last == ")" {
        let inner = t.dropFirst().dropLast()
        return String(inner).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    return t
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
    if let spIdx = chars.firstIndex(of: " "), spIdx >= 1, spIdx <= 3, spIdx + 1 < chars.count {
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

private func snapToPixel(_ value: CGFloat, scale: CGFloat) -> CGFloat {
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

private func containsCjkScalar(_ s: String) -> Bool {
    s.unicodeScalars.contains(where: { $0.value >= 0x2E80 && $0.value <= 0x9FFF })
}

final class StatusIconView: NSView {
    static let boxSize: CGFloat = 11.0

    override var isFlipped: Bool { true }

    var iconKind: String = "recognizing" {
        didSet { needsDisplay = true }
    }
    var iconColor: NSColor = .white {
        didSet { needsDisplay = true }
    }

    private func addQuadCurve(_ path: NSBezierPath, from p0: NSPoint, control ctrl: NSPoint, to p1: NSPoint) {
        let c1 = NSPoint(x: p0.x + (ctrl.x - p0.x) * (2.0 / 3.0), y: p0.y + (ctrl.y - p0.y) * (2.0 / 3.0))
        let c2 = NSPoint(x: p1.x + (ctrl.x - p1.x) * (2.0 / 3.0), y: p1.y + (ctrl.y - p1.y) * (2.0 / 3.0))
        path.curve(to: p1, controlPoint1: c1, controlPoint2: c2)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let box = min(bounds.width, bounds.height)
        if box <= 2.0 { return }
        let ox = (bounds.width - box) * 0.5
        let oy = (bounds.height - box) * 0.5

        switch iconKind {
        case "recognizing":
            let ins: CGFloat = 1.0
            let d = max(2.0, box - ins * 2.0)
            let ovalRect = NSRect(x: ox + ins, y: oy + ins, width: d, height: d)
            let ring = NSBezierPath(ovalIn: ovalRect)
            ring.lineWidth = 1.6
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
            let top = NSPoint(x: cx, y: oy + box * 0.04)
            let right = NSPoint(x: ox + box * 0.96, y: cy)
            let bottom = NSPoint(x: cx, y: oy + box * 0.96)
            let left = NSPoint(x: ox + box * 0.04, y: cy)
            let pull = box * 0.085
            let sp = NSBezierPath()
            sp.move(to: top)
            addQuadCurve(sp, from: top, control: NSPoint(x: cx + pull, y: cy - pull), to: right)
            addQuadCurve(sp, from: right, control: NSPoint(x: cx + pull, y: cy + pull), to: bottom)
            addQuadCurve(sp, from: bottom, control: NSPoint(x: cx - pull, y: cy + pull), to: left)
            addQuadCurve(sp, from: left, control: NSPoint(x: cx - pull, y: cy - pull), to: top)
            sp.close()
            iconColor.setFill()
            sp.fill()
        case "done":
            let p = NSBezierPath()
            p.lineWidth = 1.8
            p.lineCapStyle = .round
            p.lineJoinStyle = .round
            p.move(to: NSPoint(x: ox + box * 0.14, y: oy + box * 0.54))
            p.line(to: NSPoint(x: ox + box * 0.40, y: oy + box * 0.80))
            p.line(to: NSPoint(x: ox + box * 0.88, y: oy + box * 0.24))
            iconColor.setStroke()
            p.stroke()
        case "empty":
            let ins: CGFloat = 1.0
            let d = max(2.0, box - ins * 2.0)
            let ring = NSBezierPath(ovalIn: NSRect(x: ox + ins, y: oy + ins, width: d, height: d))
            ring.lineWidth = 1.6
            iconColor.setStroke()
            ring.stroke()
        default:
            let ins: CGFloat = 1.8
            let p = NSBezierPath()
            p.lineWidth = 1.8
            p.lineCapStyle = .round
            p.move(to: NSPoint(x: ox + ins, y: oy + ins))
            p.line(to: NSPoint(x: ox + box - ins, y: oy + box - ins))
            p.move(to: NSPoint(x: ox + box - ins, y: oy + ins))
            p.line(to: NSPoint(x: ox + ins, y: oy + box - ins))
            iconColor.setStroke()
            p.stroke()
        }
    }
}

final class WaveBarsView: NSView {
    static let barWidth: CGFloat = 2.5
    static let barGap: CGFloat = 2.5
    static let maxBarHeight: CGFloat = 15.0
    static let totalWidth: CGFloat = 9.0 * 2.5 + 8.0 * 2.5

    var barNorms: [CGFloat] = Array(repeating: 0.0, count: 9) {
        didSet { needsDisplay = true }
    }
    var activeColor: NSColor = rgba(255, 104, 112, 1.0) {
        didSet { needsDisplay = true }
    }
    var quietColor: NSColor = rgba(232, 92, 102, 1.0) {
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
    private static let recBg = rgba(64, 18, 26, 0.95)
    private static let recBorder = rgba(232, 88, 102, 0.68)
    private static let recWaveActive = rgba(255, 104, 112, 1.0)
    private static let recWaveQuiet = rgba(232, 92, 102, 1.0)
    private static let recMainText = rgba(248, 250, 252)
    private static let recHintTextCol = rgba(188, 178, 192, 0.90)

    private static let warnBg = rgba(72, 38, 12, 0.95)
    private static let warnBorder = rgba(245, 158, 11, 0.72)
    private static let warnWaveActive = rgba(251, 191, 36, 1.0)
    private static let warnWaveQuiet = rgba(251, 191, 36, 0.86)
    private static let warnMainText = rgba(254, 252, 232)
    private static let warnHintTextCol = rgba(251, 191, 36, 1.0)

    private static let hudFont = NSFont.systemFont(ofSize: 12.5, weight: .medium)
    private static let monoDurFont = NSFont.monospacedDigitSystemFont(ofSize: 12.5, weight: .medium)
    private static let bellWeight: [CGFloat] = [1.00, 0.92, 0.78, 0.58, 0.40]

    private var panel: NSPanel?
    private var bgView: NSView?
    private var iconView: StatusIconView?
    private var label: NSTextField?
    private var waveView: WaveBarsView?
    private var durLabel: NSTextField?
    private var hintLabel: NSTextField?
    private var hideWorkItem: DispatchWorkItem?
    private var animTimer: DispatchSourceTimer?

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
    private var maxDurationSec: Int = 0
    private var warnRemainSec: Int = 0
    private var lastDurText: String = ""
    private var lastSlotTemplate: String = ""
    private var lastHintSlotTemplate: String = ""
    private var lastWarnState: Bool = false

    private let hotkeyLabel: String
    private let hotkeyLabelEn: String
    private let isPtt: Bool

    init(hotkeyLabel: String, hotkeyLabelEn: String, isPtt: Bool) {
        self.hotkeyLabel = hotkeyLabel
        self.hotkeyLabelEn = hotkeyLabelEn
        self.isPtt = isPtt
    }

    private func ensurePanel() {
        if panel != nil { return }
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 200, height: 34), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = true
        p.ignoresMouseEvents = true; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 200, height: 34))
        container.wantsLayer = true
        container.layer?.backgroundColor = Self.recBg.cgColor
        container.layer?.cornerRadius = 17.0
        container.layer?.borderWidth = 1.0
        container.layer?.masksToBounds = true

        let iv = StatusIconView(frame: NSRect(x: 0, y: 0, width: StatusIconView.boxSize, height: StatusIconView.boxSize))
        iv.isHidden = true
        container.addSubview(iv)

        let tf = NSTextField(labelWithString: "")
        tf.font = Self.hudFont
        tf.alignment = .left
        tf.lineBreakMode = .byTruncatingTail
        tf.maximumNumberOfLines = 1
        container.addSubview(tf)

        let wv = WaveBarsView(frame: NSRect(x: 0, y: 0, width: WaveBarsView.totalWidth, height: 34))
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

        p.contentView = container
        panel = p; bgView = container; iconView = iv; label = tf; waveView = wv; durLabel = dl; hintLabel = hl
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

    private func layoutWavePill(forceFrame: Bool) {
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
        let rawEffHint = isWarn ? formatCountdownHint(remainSec, zh: recZhHint) : recHintText
        let effHint = stripOuterParens(rawEffHint)
        let hintSlotTpl = isWarn ? stripOuterParens(countdownHintSlotTemplate(remainSec, zh: recZhHint)) : effHint
        let durStr = formatRecordingDuration(elapsedSec)
        let slotTpl = durationSlotTemplate(elapsedSec)

        if !forceFrame && durStr == lastDurText && slotTpl == lastSlotTemplate && isWarn == lastWarnState && hintSlotTpl == lastHintSlotTemplate {
            return
        }
        let needRelayout = forceFrame || (slotTpl != lastSlotTemplate) || (isWarn != lastWarnState) || (hintSlotTpl != lastHintSlotTemplate)
        lastDurText = durStr
        lastSlotTemplate = slotTpl
        lastHintSlotTemplate = hintSlotTpl
        lastWarnState = isWarn

        if !needRelayout {
            durLabel.stringValue = durStr
            if isWarn {
                hintLabel.stringValue = effHint
            }
            return
        }

        let effBg = isWarn ? Self.warnBg : Self.recBg
        let effBorder = isWarn ? Self.warnBorder : Self.recBorder
        waveView.activeColor = isWarn ? Self.warnWaveActive : Self.recWaveActive
        waveView.quietColor = isWarn ? Self.warnWaveQuiet : Self.recWaveQuiet
        durLabel.textColor = isWarn ? Self.warnMainText : Self.recMainText
        hintLabel.textColor = isWarn ? Self.warnHintTextCol : Self.recHintTextCol

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

        var hintW: CGFloat = 0.0
        var hintH: CGFloat = 0.0
        if !effHint.isEmpty {
            var tplHintW: CGFloat = 0.0
            if isWarn {
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

        let padL: CGFloat = 15.0, padR: CGFloat = 15.0
        let waveW: CGFloat = WaveBarsView.totalWidth
        let gap1: CGFloat = 9.0
        let gap2: CGFloat = effHint.isEmpty ? 0.0 : 9.0
        let totalContentW = slotW + gap1 + waveW + gap2 + hintW
        let textH = max(max(tplH, actH), hintH)

        let mouseLoc = NSEvent.mouseLocation
        let activeScreen = NSScreen.screens.first(where: { NSMouseInRect(mouseLoc, $0.frame, false) }) ?? NSScreen.main ?? NSScreen.screens.first
        let scale = activeScreen?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2.0
        let vf = activeScreen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)

        let maxW = max(64.0, min(640.0, vf.width - 24.0))
        let pillH: CGFloat = max(34.0, textH + 14.0)
        let pillW: CGFloat = min(maxW, max(64.0, snapToPixel(totalContentW + padL + padR, scale: scale)))
        let pillX = snapToPixel(vf.midX - pillW / 2.0, scale: scale)
        let pillY = computeHudPillY(visibleMinY: vf.minY, visibleMaxY: vf.maxY, pillH: pillH, position: recPosition)

        panel.setFrame(NSRect(x: pillX, y: pillY, width: pillW, height: pillH), display: true)
        bgView.frame = NSRect(x: 0, y: 0, width: pillW, height: pillH)
        bgView.layer?.contentsScale = scale
        bgView.layer?.cornerRadius = pillH / 2.0
        bgView.layer?.backgroundColor = effBg.cgColor
        bgView.layer?.borderColor = effBorder.cgColor

        let availW = max(1.0, pillW - padL - padR)
        var curX = padL + ((totalContentW <= availW) ? snapToPixel((availW - totalContentW) * 0.5, scale: scale) : 0.0)

        let durH = max(tplH, actH)
        let durY = snapToPixel((pillH - durH) / 2.0, scale: scale)
        durLabel.frame = NSRect(x: curX, y: durY, width: slotW, height: durH)
        curX += slotW + gap1

        waveView.frame = NSRect(x: snapToPixel(curX, scale: scale), y: 0, width: waveW, height: pillH)
        waveView.layer?.contentsScale = scale
        curX += waveW + gap2

        if !effHint.isEmpty {
            let curXSnap = snapToPixel(curX, scale: scale)
            let remW = max(0.0, pillW - padR - curXSnap)
            let hintY = snapToPixel((pillH - hintH) / 2.0, scale: scale)
            hintLabel.frame = NSRect(x: curXSnap, y: hintY, width: min(hintW, remW), height: hintH)
        }
    }

    func hide() {
        hideWorkItem?.cancel(); hideWorkItem = nil
        stopWaveTimer()
        maxDurationSec = 0; warnRemainSec = 0
        panel?.orderOut(nil)
    }

    private static func resolveIconKind(stateKind: String, iconSeg: String) -> String {
        if !stateKind.isEmpty { return stateKind }
        switch iconSeg {
        case "◐": return "recognizing"
        case "◈": return "refining"
        case "✓": return "done"
        case "○": return "empty"
        default: return "error"
        }
    }

    func update(state: String, customText: String, durationMs: Int, position: String = "bottom", maxDurationSec: Int = 0, warnRemainSec: Int = 0) {
        hideWorkItem?.cancel(); hideWorkItem = nil
        let st = state.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if st.isEmpty || st == "idle" || st == "hide" {
            stopWaveTimer()
            self.maxDurationSec = 0; self.warnRemainSec = 0
            panel?.orderOut(nil)
            return
        }

        ensurePanel()
        guard let panel = panel, let bgView = bgView, let iconView = iconView, let label = label else { return }

        let zh = isZhUiCulture(hostUiCulture())
        var (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind): (String, NSColor, NSColor, NSColor, NSColor, NSColor, Int, String)
        var waveMode = false
        if st == "recording" {
            txt = zh
                ? "● 录音中… (\(isPtt ? "松开" : "按")\(hotkeyLabel) 完成)"
                : "● Listening… (\(isPtt ? "release" : "press") \(hotkeyLabelEn) to finish)"
            (bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                Self.recBg, Self.recWaveActive, Self.recMainText, Self.recHintTextCol, Self.recBorder, 0, ""
            )
            let trimmedCustom = customText.trimmingCharacters(in: .whitespacesAndNewlines)
            waveMode = trimmedCustom.isEmpty || trimmedCustom.hasPrefix("● ")
        } else if st == "recognizing" || st == "processing" {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "◐ 语音识别…" : "◐ Transcribing…",
                rgba(16, 40, 72, 0.95), rgba(56, 189, 248), rgba(244, 248, 252), rgba(176, 198, 222), rgba(56, 189, 248, 0.65), 0, "recognizing"
            )
        } else if st == "refining" {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "◈ 整理精修…" : "◈ Refining…",
                rgba(46, 22, 76, 0.95), rgba(208, 156, 255), rgba(248, 245, 255), rgba(198, 182, 226), rgba(192, 132, 252, 0.65), 0, "refining"
            )
        } else if st == "done" {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "✓ 已填入" : "✓ Inserted",
                rgba(14, 54, 36, 0.95), rgba(74, 222, 128), rgba(242, 253, 246), rgba(174, 218, 192), rgba(74, 222, 128, 0.62), 900, "done"
            )
        } else if st == "copied" {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "✓ 已复制(窗口已切换)" : "✓ Copied (window switched)",
                rgba(28, 32, 82, 0.95), rgba(144, 156, 255), rgba(240, 244, 255), rgba(186, 196, 238), rgba(129, 140, 248, 0.68), 2000, "done"
            )
        } else if st == "empty" {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "○ 未检测到语音" : "○ No speech detected",
                rgba(66, 44, 12, 0.95), rgba(251, 191, 36), rgba(254, 243, 199), rgba(226, 202, 148), rgba(251, 191, 36, 0.68), 2200, "empty"
            )
        } else if st == "no_mic" {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "✕ 无可用麦克风" : "✕ No microphone found",
                rgba(84, 18, 24, 0.95), rgba(252, 129, 129), rgba(254, 226, 226), rgba(238, 178, 182), rgba(248, 113, 113, 0.78), 2200, "error"
            )
        } else if st == "mic_disconnected" {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "✕ 麦克风已断开" : "✕ Microphone disconnected",
                rgba(84, 18, 24, 0.95), rgba(252, 129, 129), rgba(254, 226, 226), rgba(238, 178, 182), rgba(248, 113, 113, 0.78), 2200, "error"
            )
        } else {
            (txt, bgCol, iconColor, mainColor, hintColor, bd, autoHideMs, iconKind) = (
                zh ? "✕ 识别失败" : "✕ Transcription failed",
                rgba(84, 18, 24, 0.95), rgba(252, 129, 129), rgba(254, 226, 226), rgba(238, 178, 182), rgba(248, 113, 113, 0.78), 2600, "error"
            )
        }
        if !customText.isEmpty { txt = customText }
        if durationMs > 0 { autoHideMs = durationMs }

        let seg = splitHudSegments(txt)
        if waveMode {
            recHintText = seg.hint
            recZhHint = txt.isEmpty ? zh : containsCjkScalar(txt)
            recPosition = position
            self.maxDurationSec = max(0, maxDurationSec)
            self.warnRemainSec = (self.maxDurationSec > 0) ? (warnRemainSec > 0 ? warnRemainSec : recordingCountdownWarnSec(self.maxDurationSec)) : 0
            if !isWaveRecording {
                isWaveRecording = true
                recStartMs = monotonicMs()
                smoothLevel = 0.0
                waveHist = Array(repeating: 0.0, count: 5)
                frameIdx = 0
                lastDurText = ""
                lastSlotTemplate = ""
                lastHintSlotTemplate = ""
                lastWarnState = false
                let initLvl = currentLiveLevel()
                waveView?.barNorms = (initLvl > 0.0) ? stepWaveEnvelope(initLvl) : Array(repeating: 0.0, count: 9)
            }
            layoutWavePill(forceFrame: true)
            panel.orderFrontRegardless()
            startWaveTimerIfNeeded()
        } else {
            stopWaveTimer()
            self.maxDurationSec = 0; self.warnRemainSec = 0
            waveView?.isHidden = true
            durLabel?.isHidden = true
            hintLabel?.isHidden = true

            let hasIcon = !seg.icon.isEmpty
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

            let textSize = hasBody ? label.frame.size : NSSize(width: 0, height: 15)
            let iconBox: CGFloat = hasIcon ? StatusIconView.boxSize : 0.0
            let iconGap: CGFloat = (hasIcon && hasBody) ? 6.0 : 0.0
            let padL: CGFloat = 15.0, padR: CGFloat = 15.0
            let contentW = iconBox + iconGap + ceil(textSize.width)
            let pillH: CGFloat = max(34.0, ceil(textSize.height) + 14.0)
            let pillW: CGFloat = min(640.0, max(64.0, contentW + padL + padR))

            let mouseLoc = NSEvent.mouseLocation
            let activeScreen = NSScreen.screens.first(where: { NSMouseInRect(mouseLoc, $0.frame, false) }) ?? NSScreen.main ?? NSScreen.screens.first
            let scale = activeScreen?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2.0
            let vf = activeScreen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)

            let pillX = snapToPixel(vf.midX - pillW / 2.0, scale: scale)
            let pillY = computeHudPillY(visibleMinY: vf.minY, visibleMaxY: vf.maxY, pillH: pillH, position: position)
            panel.setFrame(NSRect(x: pillX, y: pillY, width: pillW, height: pillH), display: false)
            bgView.frame = NSRect(x: 0, y: 0, width: pillW, height: pillH)
            bgView.layer?.contentsScale = scale
            bgView.layer?.cornerRadius = pillH / 2.0
            bgView.layer?.backgroundColor = bgCol.cgColor
            bgView.layer?.borderColor = bd.cgColor

            let availW = max(1.0, pillW - padL - padR)
            var curX = padL + ((contentW <= availW) ? snapToPixel((availW - contentW) * 0.5, scale: scale) : 0.0)
            if hasIcon {
                iconView.iconKind = Self.resolveIconKind(stateKind: iconKind, iconSeg: seg.icon)
                iconView.iconColor = iconColor
                let iconY = snapToPixel((pillH - iconBox) / 2.0, scale: scale)
                iconView.frame = NSRect(x: curX, y: iconY, width: iconBox, height: iconBox)
                iconView.layer?.contentsScale = scale
                iconView.isHidden = false
                curX += iconBox + iconGap
            } else {
                iconView.isHidden = true
            }
            if hasBody {
                let labelH = ceil(textSize.height)
                let labelY = snapToPixel((pillH - labelH) / 2.0, scale: scale)
                let remW = max(0.0, pillW - padR - curX)
                label.frame = NSRect(x: curX, y: labelY, width: min(ceil(textSize.width), remW), height: labelH)
            }
            panel.orderFrontRegardless()
        }

        if autoHideMs > 0 {
            let work = DispatchWorkItem { [weak self] in self?.hide() }
            hideWorkItem = work
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(autoHideMs), execute: work)
        }
    }

    func close() {
        hide(); panel?.close(); panel = nil
    }
}

private let stdoutLock = NSLock()

private func writeJsonLine(_ dict: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(dict),
          let data = try? JSONSerialization.data(withJSONObject: dict, options: []),
          let str = String(data: data, encoding: .utf8) else { return }
    stdoutLock.lock()
    fputs(str + "\n", stdout); fflush(stdout)
    stdoutLock.unlock()
}

private func monotonicMs() -> UInt64 {
    ProcessInfo.processInfo.systemUptime.isFinite ? UInt64(ProcessInfo.processInfo.systemUptime * 1000.0) : 0
}

private func queryFrontmostApp() -> (name: String, bundleId: String, pid: Int) {
    guard let app = NSWorkspace.shared.frontmostApplication else { return ("", "", 0) }
    let name = app.localizedName ?? app.bundleURL?.deletingPathExtension().lastPathComponent ?? ""
    return (name, app.bundleIdentifier ?? "", Int(app.processIdentifier))
}

private func runSelfCheck() -> Int32 {
    func check(_ cond: Bool, _ msg: String) throws {
        if !cond { throw NSError(domain: "SelfCheck", code: 1, userInfo: [NSLocalizedDescriptionKey: msg]) }
    }
    do {
        let rctrl = try resolveTargetKey("RControl")
        try check(rctrl.primaryKeyCode == 0x3E && rctrl.deviceMask == NX_DEVICERCTLKEYMASK && rctrl.isModifier, "RControl spec")
        try check(try resolveTargetKey("LControl").primaryKeyCode == 0x3B, "LControl")
        try check(try resolveTargetKey("RAlt").primaryKeyCode == 0x3D, "RAlt")
        try check(try resolveTargetKey("RCommand").primaryKeyCode == 0x36, "RCommand")
        try check(try resolveTargetKey("Fn").primaryKeyCode == 0x3F, "Fn")
        try check(try resolveTargetKey("F8").primaryKeyCode == 0x64 && (try resolveTargetKey("F20")).primaryKeyCode == 0x5A, "F-keys")

        // 1-3: RControl via standard (0x3E+mask), mask-only (0x3B+0x2000), and keyCode-only (0x3E+FLAG_CONTROL)
        var eng = HotkeyEngine(spec: rctrl, isPtt: false, tapMaxMs: 1200, debounceMs: 180, keyTest: false)
        for (idx, (kc, flags)) in [(UInt16(0x3E), FLAG_CONTROL | NX_DEVICERCTLKEYMASK), (UInt16(0x3B), FLAG_CONTROL | NX_DEVICERCTLKEYMASK), (UInt16(0x3E), FLAG_CONTROL)].enumerated() {
            let t = UInt64(1000 + idx * 500)
            try check(eng.handleEvent(kind: .flagsChanged, keyCode: kc, rawFlags: flags, isARepeat: false, nowMs: t, keyDownCounter: 10).isEmpty, "down \(idx)")
            let up = eng.handleEvent(kind: .flagsChanged, keyCode: kc, rawFlags: 0, isARepeat: false, nowMs: t + 100, keyDownCounter: 10)
            try check(up.count == 1 && up[0].kind == "toggle", "up \(idx)")
        }

        // 4: LControl must NOT trigger RControl
        _ = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3B, rawFlags: FLAG_CONTROL | NX_DEVICELCTLKEYMASK, isARepeat: false, nowMs: 2600, keyDownCounter: 10)
        try check(eng.handleEvent(kind: .flagsChanged, keyCode: 0x3B, rawFlags: 0, isARepeat: false, nowMs: 2700, keyDownCounter: 10).isEmpty, "LControl isolation")

        // 5: FlagsChanged-only + counter increment suppresses tap_toggle AND emits ptt_cancel on PTT release
        _ = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 3000, keyDownCounter: 10)
        try check(eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 3120, keyDownCounter: 11).isEmpty, "counter chord tap suppression")

        var pttEng = HotkeyEngine(spec: rctrl, isPtt: true, tapMaxMs: 1200, debounceMs: 180, keyTest: false)
        let ps1 = pttEng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 4000, keyDownCounter: 20)
        let pc1 = pttEng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 4150, keyDownCounter: 21)
        try check(ps1.count == 1 && ps1[0].kind == "ptt_start" && pc1.count == 1 && pc1[0].kind == "ptt_cancel", "counter-only ptt_cancel")

        // 6: Swallowed .keyUp recovery via isKeyPhysicallyDown physical state check
        _ = eng.handleEvent(kind: .keyDown, keyCode: 0x08, rawFlags: 0, isARepeat: false, nowMs: 5000, keyDownCounter: 30)
        try check(eng.otherKeysDown.contains(0x08), "otherKeysDown recorded")
        // Physical state reports key 0x08 is no longer held -> pruned, so subsequent RControl tap succeeds!
        _ = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 5500, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(eng.otherKeysDown.isEmpty, "stale key pruned")
        let recov = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 5600, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(recov.count == 1 && recov[0].kind == "toggle", "recovered toggle after swallowed keyUp")

        try check(isZhUiCulture("zh-Hans-CN") && isZhUiCulture("zh_TW.UTF-8") && !isZhUiCulture("en-US"), "ui_culture zh detection")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: "bottom") == 58.0, "HUD bottom pillY")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: "") == 58.0, "HUD default pillY")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: " TOP ") == 850.0, "HUD top pillY")
        try check(splitHudSegments("● 录音中… (按右 Ctrl 完成)") == HudSegments(icon: "●", main: "录音中…", hint: "(按右 Ctrl 完成)", hasSpaceBeforeHint: true), "HUD split recording zh")
        try check(stripOuterParens("(按右 Ctrl 完成)") == "按右 Ctrl 完成" && stripOuterParens("12s 后自动完成") == "12s 后自动完成" && stripOuterParens("") == "", "HUD strip outer parens")
        try check(splitHudSegments("✓ 已复制(窗口已切换)") == HudSegments(icon: "✓", main: "已复制", hint: "(窗口已切换)", hasSpaceBeforeHint: false), "HUD split copied zh")
        try check(splitHudSegments("Custom HUD") == HudSegments(icon: "", main: "Custom HUD", hint: "", hasSpaceBeforeHint: false), "HUD split custom")
        try check(formatRecordingDuration(-5) == "0s" && formatRecordingDuration(0) == "0s" && formatRecordingDuration(9) == "9s" && formatRecordingDuration(59) == "59s", "HUD duration seconds")
        try check(formatRecordingDuration(60) == "1m 00s" && formatRecordingDuration(65) == "1m 05s" && formatRecordingDuration(605) == "10m 05s", "HUD duration minutes")
        try check(durationSlotTemplate(0) == "8s" && durationSlotTemplate(59) == "88s" && durationSlotTemplate(60) == "8m 88s" && durationSlotTemplate(600) == "88m 88s", "HUD duration slot template")
        try check(recordingCountdownWarnSec(0) == 0 && recordingCountdownWarnSec(600) == 15 && recordingCountdownWarnSec(45) == 15 && recordingCountdownWarnSec(30) == 10 && recordingCountdownWarnSec(9) == 3 && recordingCountdownWarnSec(2) == 1, "HUD countdown warn sec")
        try check(formatCountdownHint(12, zh: true) == "(12s 后自动完成)" && formatCountdownHint(5, zh: false) == "(auto-finish in 5s)", "HUD countdown hint")
        try check(countdownHintSlotTemplate(12, zh: true) == "(88s 后自动完成)" && countdownHintSlotTemplate(9, zh: false) == "(auto-finish in 8s)", "HUD countdown slot template")

        #if arch(arm64)
        let archName = "arm64"
        #elseif arch(x86_64)
        let archName = "x86_64"
        #else
        let archName = "unknown"
        #endif
        writeJsonLine(["ok": true, "mode": "self-check", "arch": archName, "rcontrol_vk": String(format: "0x%02X", rctrl.primaryKeyCode), "rcontrol_mask": String(format: "0x%04X", rctrl.deviceMask), "ui_culture": hostUiCulture()])
        return 0
    } catch {
        fputs("self-check error: \(error.localizedDescription)\n", stderr)
        return 1
    }
}

@main
struct MacShimMain {
    static func main() {
        var (mode, hotkey, triggerMode, tapMaxMs, debounceMs, seconds) = ("bridge", "RControl", "tap_toggle", 1200, 180, 10)
        let args = CommandLine.arguments
        var i = 1
        while i < args.count {
            let arg = args[i].lowercased()
            if arg == "--self-check" || arg == "-self-check" { mode = "self-check"; i += 1; continue }
            let nextVal: String? = (i + 1 < args.count) ? args[i + 1] : nil
            switch arg {
            case "--mode", "-mode": if let v = nextVal { mode = v.lowercased(); i += 2 } else { i += 1 }
            case "--hotkey", "-hotkey": if let v = nextVal { hotkey = v; i += 2 } else { i += 1 }
            case "--trigger-mode", "-triggermode": if let v = nextVal { triggerMode = v.lowercased(); i += 2 } else { i += 1 }
            case "--tap-max-ms", "-tapmaxms": if let v = nextVal, let n = Int(v) { tapMaxMs = n; i += 2 } else { i += 1 }
            case "--debounce-ms", "-debouncems": if let v = nextVal, let n = Int(v) { debounceMs = n; i += 2 } else { i += 1 }
            case "--seconds", "-seconds": if let v = nextVal, let n = Int(v) { seconds = max(1, n); i += 2 } else { i += 1 }
            default: i += 1
            }
        }
        if mode == "self-check" { Foundation.exit(runSelfCheck()) }

        let spec: TargetKeySpec
        do { spec = try resolveTargetKey(hotkey) } catch {
            writeJsonLine(["ok": false, "error": error.localizedDescription])
            Foundation.exit(2)
        }

        let isPtt = (triggerMode == "ptt" || triggerMode == "push_to_talk")
        let isKeyTest = (mode == "key-test")
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)

        let hud = HudPillController(hotkeyLabel: spec.hudLabel, hotkeyLabelEn: spec.hudLabelEn, isPtt: isPtt)
        var engine = HotkeyEngine(spec: spec, isPtt: isPtt, tapMaxMs: tapMaxMs, debounceMs: debounceMs, keyTest: isKeyTest)
        var globalMonitor: Any?

        let cleanupAndExit: (Int32) -> Never = { code in
            if let gm = globalMonitor { NSEvent.removeMonitor(gm); globalMonitor = nil }
            hud.close()
            Foundation.exit(code)
        }

        signal(SIGINT, SIG_IGN); signal(SIGTERM, SIG_IGN)
        let sigIntSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        sigIntSrc.setEventHandler { cleanupAndExit(0) }; sigIntSrc.resume()
        let sigTermSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        sigTermSrc.setEventHandler { cleanupAndExit(0) }; sigTermSrc.resume()

        let hasListenAccess = CGPreflightListenEventAccess() || AXIsProcessTrusted()
        let listenMask: NSEvent.EventTypeMask = (!spec.isModifier || isKeyTest || hasListenAccess)
            ? [.flagsChanged, .keyDown, .keyUp]
            : [.flagsChanged]

        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: listenMask) { ev in
            let rawKind: RawEventKind
            switch ev.type {
            case .flagsChanged: rawKind = .flagsChanged
            case .keyDown: rawKind = .keyDown
            case .keyUp: rawKind = .keyUp
            default: return
            }
            if isKeyTest && rawKind == .keyDown && ev.keyCode == 0x35 { cleanupAndExit(0) }

            let now = monotonicMs()
            let kdCount = CGEventSource.counterForEventType(.combinedSessionState, eventType: .keyDown)
            let emitted = engine.handleEvent(
                kind: rawKind,
                keyCode: ev.keyCode,
                rawFlags: ev.modifierFlags.rawValue,
                isARepeat: (rawKind == .keyDown) ? ev.isARepeat : false,
                nowMs: now,
                keyDownCounter: kdCount,
                isKeyPhysicallyDown: { CGEventSource.keyState(.combinedSessionState, key: CGKeyCode($0)) }
            )
            for item in emitted {
                if isKeyTest {
                    let kname = KEY_CODE_NAMES[item.vk] ?? String(format: "VK_0x%02X", item.vk)
                    writeJsonLine(["event": item.kind, "vk": String(format: "0x%02X", item.vk), "key_name": kname, "flags": String(format: "0x%X", item.flags), "is_down": item.isDown, "tainted": item.tainted])
                } else {
                    let fg = queryFrontmostApp()
                    writeJsonLine(["event": item.kind, "hwnd": 0, "process": fg.name, "bundle_id": fg.bundleId, "pid": fg.pid, "title": "", "ts_ms": now])
                }
            }
        }

        if isKeyTest {
            writeJsonLine(["event": "key_test_ready", "hotkey": hotkey, "target_vk": String(format: "0x%02X", spec.primaryKeyCode), "mode": triggerMode, "seconds": seconds, "listen_access": hasListenAccess])
            DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(seconds)) { cleanupAndExit(0) }
            app.run(); cleanupAndExit(0)
        }

        let stdinThread = Thread {
            while let rawLine = readLine(strippingNewline: true) {
                let trimmed = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed.isEmpty { continue }
                DispatchQueue.main.async {
                    guard let data = trimmed.data(using: .utf8),
                          let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
                    let op = (obj["cmd"] as? String) ?? "unknown"
                    let seqId = (obj["seq_id"] as? Int) ?? ((obj["seq_id"] as? NSNumber)?.intValue ?? 0)
                    switch op {
                    case "fg_window":
                        let fg = queryFrontmostApp()
                        writeJsonLine(["type": "reply", "reply": "fg_window", "seq_id": seqId, "ok": true, "hwnd": 0, "process": fg.name, "bundle_id": fg.bundleId, "pid": fg.pid, "title": ""])
                    case "hud":
                        let dms = (obj["duration_ms"] as? Int) ?? ((obj["duration_ms"] as? NSNumber)?.intValue ?? 0)
                        let maxDur = (obj["max_duration_sec"] as? Int) ?? ((obj["max_duration_sec"] as? NSNumber)?.intValue ?? 0)
                        let warnRem = (obj["warn_remain_sec"] as? Int) ?? ((obj["warn_remain_sec"] as? NSNumber)?.intValue ?? 0)
                        let pos = (obj["position"] as? String) ?? "bottom"
                        hud.update(state: (obj["state"] as? String) ?? "", customText: (obj["text"] as? String) ?? "", durationMs: dms, position: pos, maxDurationSec: maxDur, warnRemainSec: warnRem)
                        writeJsonLine(["type": "reply", "reply": "hud", "seq_id": seqId, "ok": true])
                    case "hud_level":
                        let lvl = (obj["level"] as? Double) ?? ((obj["level"] as? NSNumber)?.doubleValue ?? 0.0)
                        hud.updateAudioLevel(CGFloat(lvl))
                        if seqId > 0 {
                            writeJsonLine(["type": "reply", "reply": "hud_level", "seq_id": seqId, "ok": true])
                        }
                    case "quit":
                        cleanupAndExit(0)
                    default:
                        writeJsonLine(["type": "reply", "reply": op, "seq_id": seqId, "ok": false, "error": "unknown_cmd:\(op)"])
                    }
                }
            }
            DispatchQueue.main.async { cleanupAndExit(0) }
            DispatchQueue.global().asyncAfter(deadline: .now() + 2.0) { Foundation.exit(0) }
        }
        stdinThread.name = "mac-shim-stdin"
        stdinThread.start()

        writeJsonLine(["event": "ready", "target_vk": String(format: "0x%02X", spec.primaryKeyCode), "mode": triggerMode, "listen_access": hasListenAccess, "ui_culture": hostUiCulture()])
        app.run(); cleanupAndExit(0)
    }
}
