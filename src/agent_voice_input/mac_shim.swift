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
    return round(visibleMinY + 14.0)
}

final class HudPillController {
    private var panel: NSPanel?
    private var bgView: NSView?
    private var label: NSTextField?
    private var hideWorkItem: DispatchWorkItem?
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
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 220, height: 36), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        p.isOpaque = false; p.backgroundColor = .clear; p.hasShadow = true
        p.ignoresMouseEvents = true; p.hidesOnDeactivate = false; p.isReleasedWhenClosed = false

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 220, height: 36))
        container.wantsLayer = true
        container.layer?.backgroundColor = rgba(22, 24, 30, 0.92).cgColor
        container.layer?.cornerRadius = 18.0
        container.layer?.borderWidth = 1.25
        container.layer?.masksToBounds = true

        let tf = NSTextField(labelWithString: "")
        tf.font = NSFont.systemFont(ofSize: 13.5, weight: .semibold)
        tf.alignment = .center
        tf.lineBreakMode = .byTruncatingTail
        tf.maximumNumberOfLines = 1
        container.addSubview(tf)
        p.contentView = container
        panel = p; bgView = container; label = tf
    }

    func hide() {
        hideWorkItem?.cancel(); hideWorkItem = nil
        panel?.orderOut(nil)
    }

    func update(state: String, customText: String, durationMs: Int, position: String = "bottom") {
        hideWorkItem?.cancel(); hideWorkItem = nil
        let st = state.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if st.isEmpty || st == "idle" || st == "hide" { panel?.orderOut(nil); return }

        ensurePanel()
        guard let panel = panel, let bgView = bgView, let label = label else { return }

        let zh = isZhUiCulture(hostUiCulture())
        var (txt, fg, bd, autoHideMs): (String, NSColor, NSColor, Int)
        if st == "recording" {
            txt = zh
                ? "● 正在录音… (\(isPtt ? "松开" : "再按")\(hotkeyLabel) 完成)"
                : "● Listening… (\(isPtt ? "release" : "press") \(hotkeyLabelEn) to finish)"
            (fg, bd, autoHideMs) = (rgba(255, 102, 102), rgba(190, 62, 62, 0.95), 0)
        } else if st == "recognizing" || st == "processing" {
            (txt, fg, bd, autoHideMs) = (zh ? "◐ 正在语音识别…" : "◐ Transcribing…", rgba(34, 211, 238), rgba(20, 148, 175, 0.95), 0)
        } else if st == "refining" {
            (txt, fg, bd, autoHideMs) = (zh ? "◈ 正在整理精修…" : "◈ Refining…", rgba(251, 191, 36), rgba(185, 132, 28, 0.95), 0)
        } else if st == "done" {
            (txt, fg, bd, autoHideMs) = (zh ? "✓ 已填入" : "✓ Inserted", rgba(74, 222, 128), rgba(46, 150, 88, 0.95), 900)
        } else if st == "copied" {
            (txt, fg, bd, autoHideMs) = (zh ? "✓ 已复制(窗口已切换)" : "✓ Copied (window switched)", rgba(129, 140, 248), rgba(99, 102, 241, 0.95), 900)
        } else if st == "empty" {
            (txt, fg, bd, autoHideMs) = (zh ? "○ 未检测到语音" : "○ No speech detected", rgba(203, 213, 225), rgba(100, 116, 139, 0.95), 900)
        } else {
            (txt, fg, bd, autoHideMs) = (zh ? "✕ 识别失败" : "✕ Transcription failed", rgba(248, 113, 113), rgba(185, 65, 65, 0.95), 900)
        }
        if !customText.isEmpty { txt = customText }
        if durationMs > 0 { autoHideMs = durationMs }

        label.stringValue = txt; label.textColor = fg; label.sizeToFit()
        let textSize = label.frame.size
        let pillH: CGFloat = max(36.0, ceil(textSize.height) + 14.0)
        let pillW: CGFloat = min(640.0, max(164.0, ceil(textSize.width) + 36.0))

        let mouseLoc = NSEvent.mouseLocation
        let activeScreen = NSScreen.screens.first(where: { NSMouseInRect(mouseLoc, $0.frame, false) }) ?? NSScreen.main ?? NSScreen.screens.first
        let vf = activeScreen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)

        let pillY = computeHudPillY(visibleMinY: vf.minY, visibleMaxY: vf.maxY, pillH: pillH, position: position)
        panel.setFrame(NSRect(x: round(vf.midX - pillW / 2.0), y: pillY, width: pillW, height: pillH), display: false)
        bgView.frame = NSRect(x: 0, y: 0, width: pillW, height: pillH)
        bgView.layer?.cornerRadius = pillH / 2.0
        bgView.layer?.borderColor = bd.cgColor
        label.frame = NSRect(x: 16.0, y: round((pillH - textSize.height) / 2.0), width: pillW - 32.0, height: ceil(textSize.height))
        panel.orderFrontRegardless()

        if autoHideMs > 0 {
            let work = DispatchWorkItem { [weak self] in self?.panel?.orderOut(nil) }
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
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: "bottom") == 64.0, "HUD bottom pillY")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: "") == 64.0, "HUD default pillY")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: " TOP ") == 850.0, "HUD top pillY")

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
                        let pos = (obj["position"] as? String) ?? "bottom"
                        hud.update(state: (obj["state"] as? String) ?? "", customText: (obj["text"] as? String) ?? "", durationMs: dms, position: pos)
                        writeJsonLine(["type": "reply", "reply": "hud", "seq_id": seqId, "ok": true])
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
