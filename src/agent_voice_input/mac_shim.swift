import AppKit
import ApplicationServices
import AVFoundation
import CoreAudio
import Foundation
import Security

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
private let AVI_INJECT_TAG: Int64 = 0x41564931

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
    var holdMs: Int = 0
}

struct HotkeyStepResult {
    var events: [EmittedEvent] = []
    var armTimers: Bool = false
    var cancelTimers: Bool = false
    var timerGen: UInt64 = 0
    var hintDelayMs: Int = 0
    var cancelDelayMs: Int = 0
    var showHoldPreview: Bool = false
    var clearHoldPreview: Bool = false
    var armPreviewSafetyClear: Bool = false
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

private let MODIFIER_KEY_CODES: Set<UInt16> = [
    0x36, // RCommand
    0x37, // LCommand
    0x38, // LShift
    0x39, // CapsLock
    0x3A, // LOption
    0x3B, // LControl
    0x3C, // RShift
    0x3D, // ROption
    0x3E, // RControl
    0x3F, // Fn
    0xB3, // Globe
]

private func hasAnyNonModifierKeyPhysicallyDown(excluding spec: TargetKeySpec) -> Bool {
    for kc: UInt16 in 0..<128 {
        if MODIFIER_KEY_CODES.contains(kc) || spec.matchesKeyCode(kc) { continue }
        if CGEventSource.keyState(.combinedSessionState, key: CGKeyCode(kc)) {
            return true
        }
    }
    return false
}

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
    let holdCancelMs: UInt64
    let holdHintMs: UInt64
    let debounceMs: UInt64
    let keyTest: Bool

    private(set) var targetDown = false
    private(set) var pttActive = false
    private(set) var chordTainted = false
    private(set) var holdFired = false
    private(set) var holdHintShown = false
    private(set) var holdGen: UInt64 = 0
    private var downTickMs: UInt64 = 0
    private var lastFireTickMs: UInt64 = 0
    private var hasFiredOnce = false
    private var downKeyCounter: UInt32 = 0
    private(set) var otherKeysDown: Set<UInt16> = []

    init(
        spec: TargetKeySpec,
        isPtt: Bool,
        tapMaxMs: Int = 500,
        holdCancelMs: Int = 800,
        holdHintMs: Int = 250,
        debounceMs: Int = 180,
        keyTest: Bool = false
    ) {
        self.spec = spec
        self.isPtt = isPtt
        let effTap = UInt64(max(50, tapMaxMs))
        self.tapMaxMs = effTap
        if holdCancelMs <= 0 {
            self.holdCancelMs = 0
            self.holdHintMs = 0
        } else {
            var effHold = UInt64(holdCancelMs)
            if effHold <= effTap { effHold = effTap + 300 }
            self.holdCancelMs = effHold
            if holdHintMs <= 0 || effHold <= 1 {
                self.holdHintMs = 0
            } else {
                self.holdHintMs = min(UInt64(holdHintMs), effHold / 2)
            }
        }
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

    private func isTargetPhysicallyDown(
        rawFlags: UInt,
        isKeyPhysicallyDown: (UInt16) -> Bool
    ) -> Bool {
        if spec.isModifier {
            if spec.highFlag == FLAG_CAPSLOCK { return targetDown }
            if spec.highFlag != 0 { return (rawFlags & spec.highFlag) != 0 }
            return isKeyPhysicallyDown(spec.primaryKeyCode)
        }
        return isKeyPhysicallyDown(spec.primaryKeyCode)
            || spec.altKeyCodes.contains(where: isKeyPhysicallyDown)
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
        isKeyPhysicallyDown: (UInt16) -> Bool = { _ in true },
        hasOtherNonModifierKeyDown: () -> Bool = { false }
    ) -> [EmittedEvent] {
        handleEventStep(
            kind: kind,
            keyCode: keyCode,
            rawFlags: rawFlags,
            isARepeat: isARepeat,
            nowMs: nowMs,
            keyDownCounter: keyDownCounter,
            isKeyPhysicallyDown: isKeyPhysicallyDown,
            hasOtherNonModifierKeyDown: hasOtherNonModifierKeyDown
        ).events
    }

    mutating func handleEventStep(
        kind: RawEventKind,
        keyCode: UInt16,
        rawFlags: UInt,
        isARepeat: Bool,
        nowMs: UInt64,
        keyDownCounter: UInt32,
        isKeyPhysicallyDown: (UInt16) -> Bool = { _ in true },
        hasOtherNonModifierKeyDown: () -> Bool = { false }
    ) -> HotkeyStepResult {
        var res = HotkeyStepResult()
        if kind == .keyDown && isARepeat { return res }
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

        if keyTest {
            let dispVk = (kind == .flagsChanged && keyCode == 0x3B && (rawFlags & NX_DEVICERCTLKEYMASK) != 0) ? UInt16(0x3E) : keyCode
            let allMods = FLAG_SHIFT | FLAG_CONTROL | FLAG_OPTION | FLAG_COMMAND | FLAG_FUNCTION | FLAG_CAPSLOCK
            let evDown = (kind == .keyDown) ? true : ((kind == .keyUp) ? false : (isTargetTransition ? nowDown : ((rawFlags & allMods) != 0)))
            res.events.append(EmittedEvent(kind: "key_event", vk: dispVk, flags: rawFlags, isDown: evDown, tainted: chordTainted))
        }

        if nowDown && !targetDown {
            targetDown = true
            downTickMs = nowMs
            downKeyCounter = keyDownCounter
            holdFired = false
            holdHintShown = false
            holdGen &+= 1
            chordTainted = !otherKeysDown.isEmpty || hasOtherModifiersActive(rawFlags) || hasOtherNonModifierKeyDown()
            if isPtt {
                if !chordTainted && debouncePassed(nowMs) {
                    lastFireTickMs = nowMs; hasFiredOnce = true; pttActive = true
                    res.events.append(EmittedEvent(kind: "ptt_start", vk: actionVk, flags: rawFlags, isDown: true, tainted: false))
                }
            } else if !chordTainted && holdCancelMs > 0 {
                res.armTimers = true
                res.timerGen = holdGen
                res.hintDelayMs = Int(holdHintMs)
                res.cancelDelayMs = Int(holdCancelMs)
            }
        } else if !nowDown && targetDown {
            holdGen &+= 1
            res.cancelTimers = true
            var wasHoldFired = holdFired
            let wasHintShown = holdHintShown
            if (spec.isModifier && keyDownCounter != downKeyCounter) || hasOtherModifiersActive(rawFlags) || !otherKeysDown.isEmpty || hasOtherNonModifierKeyDown() {
                chordTainted = true
            }
            if isPtt {
                if pttActive {
                    if !chordTainted {
                        lastFireTickMs = nowMs; hasFiredOnce = true
                        res.events.append(EmittedEvent(kind: "ptt_stop", vk: actionVk, flags: rawFlags, isDown: false, tainted: false))
                    } else {
                        res.events.append(EmittedEvent(kind: "ptt_cancel", vk: actionVk, flags: rawFlags, isDown: false, tainted: true))
                    }
                }
            } else if !wasHoldFired && !chordTainted && nowMs >= downTickMs && debouncePassed(nowMs) {
                let held = nowMs - downTickMs
                if held <= tapMaxMs {
                    lastFireTickMs = nowMs; hasFiredOnce = true
                    res.events.append(EmittedEvent(kind: "toggle", vk: actionVk, flags: rawFlags, isDown: false, tainted: false))
                } else if holdCancelMs > 0 && held >= holdCancelMs {
                    lastFireTickMs = nowMs; hasFiredOnce = true
                    wasHoldFired = true
                    res.events.append(EmittedEvent(kind: "hold_cancel", vk: actionVk, flags: rawFlags, isDown: false, tainted: false, holdMs: Int(held)))
                }
            }
            if wasHintShown {
                if wasHoldFired { res.armPreviewSafetyClear = true }
                else { res.clearHoldPreview = true }
            }
            targetDown = false; pttActive = false; chordTainted = false; holdFired = false; holdHintShown = false
        } else {
            if kind == .keyDown { otherKeysDown.insert(keyCode) }
            else if kind == .keyUp { otherKeysDown.remove(keyCode) }
            let modTaint = (kind == .flagsChanged) && (spec.isModifier ? !spec.matchesKeyCode(keyCode) : hasOtherModifiersActive(rawFlags))
            if targetDown && (kind == .keyDown || kind == .keyUp || modTaint || hasOtherModifiersActive(rawFlags)) {
                chordTainted = true
                holdGen &+= 1
                res.cancelTimers = true
                if holdHintShown {
                    holdHintShown = false
                    res.clearHoldPreview = true
                }
                if isPtt && pttActive {
                    pttActive = false
                    res.events.append(EmittedEvent(kind: "ptt_cancel", vk: keyCode, flags: rawFlags, isDown: kind == .keyDown, tainted: true))
                }
            }
        }
        return res
    }

    mutating func onHoldHintTimeout(
        gen: UInt64,
        nowMs: UInt64,
        keyDownCounter: UInt32,
        rawFlags: UInt,
        isKeyPhysicallyDown: (UInt16) -> Bool = { _ in true },
        hasOtherNonModifierKeyDown: () -> Bool = { false }
    ) -> HotkeyStepResult {
        var res = HotkeyStepResult()
        if isPtt || !targetDown || chordTainted || holdFired || gen != holdGen || holdCancelMs == 0 || holdHintMs == 0 {
            return res
        }
        if !otherKeysDown.isEmpty {
            otherKeysDown = otherKeysDown.filter(isKeyPhysicallyDown)
        }
        if !isTargetPhysicallyDown(rawFlags: rawFlags, isKeyPhysicallyDown: isKeyPhysicallyDown) {
            let hadHint = holdHintShown
            targetDown = false; pttActive = false; chordTainted = false; holdFired = false; holdHintShown = false
            holdGen &+= 1
            res.cancelTimers = true
            if hadHint { res.clearHoldPreview = true }
            return res
        }
        if (spec.isModifier && keyDownCounter != downKeyCounter) || hasOtherModifiersActive(rawFlags) || !otherKeysDown.isEmpty || hasOtherNonModifierKeyDown() {
            chordTainted = true
            holdGen &+= 1
            res.cancelTimers = true
            return res
        }
        let held = (nowMs >= downTickMs) ? (nowMs - downTickMs) : 0
        if held < holdHintMs || held >= holdCancelMs { return res }
        holdHintShown = true
        res.showHoldPreview = true
        return res
    }

    mutating func onHoldCancelTimeout(
        gen: UInt64,
        nowMs: UInt64,
        keyDownCounter: UInt32,
        rawFlags: UInt,
        isKeyPhysicallyDown: (UInt16) -> Bool = { _ in true },
        hasOtherNonModifierKeyDown: () -> Bool = { false }
    ) -> HotkeyStepResult {
        var res = HotkeyStepResult()
        if isPtt || !targetDown || chordTainted || holdFired || gen != holdGen || holdCancelMs == 0 {
            return res
        }
        if !otherKeysDown.isEmpty {
            otherKeysDown = otherKeysDown.filter(isKeyPhysicallyDown)
        }
        if !isTargetPhysicallyDown(rawFlags: rawFlags, isKeyPhysicallyDown: isKeyPhysicallyDown) {
            let hadHint = holdHintShown
            targetDown = false; pttActive = false; chordTainted = false; holdFired = false; holdHintShown = false
            holdGen &+= 1
            res.cancelTimers = true
            if hadHint { res.clearHoldPreview = true }
            return res
        }
        if (spec.isModifier && keyDownCounter != downKeyCounter) || hasOtherModifiersActive(rawFlags) || !otherKeysDown.isEmpty || hasOtherNonModifierKeyDown() {
            chordTainted = true
            holdGen &+= 1
            res.cancelTimers = true
            if holdHintShown {
                holdHintShown = false
                res.clearHoldPreview = true
            }
            return res
        }
        let held = (nowMs >= downTickMs) ? (nowMs - downTickMs) : 0
        if held < holdCancelMs { return res }
        if !debouncePassed(nowMs) {
            if holdHintShown {
                holdHintShown = false
                res.clearHoldPreview = true
            }
            return res
        }
        holdFired = true
        lastFireTickMs = nowMs
        hasFiredOnce = true
        holdGen &+= 1
        res.cancelTimers = true
        if holdHintShown {
            holdHintShown = false
            res.armPreviewSafetyClear = true
        }
        res.events.append(EmittedEvent(kind: "hold_cancel", vk: spec.primaryKeyCode, flags: rawFlags, isDown: true, tainted: false, holdMs: Int(held)))
        return res
    }

    mutating func resetForPowerResume() -> HotkeyStepResult {
        var res = HotkeyStepResult()
        let wasPttActive = (isPtt && pttActive)
        holdGen &+= 1
        targetDown = false
        pttActive = false
        chordTainted = false
        holdFired = false
        holdHintShown = false
        otherKeysDown.removeAll()
        res.cancelTimers = true
        res.clearHoldPreview = true
        if wasPttActive {
            res.events.append(EmittedEvent(kind: "ptt_stop", vk: spec.primaryKeyCode, flags: 0, isDown: false, tainted: false))
        }
        return res
    }
}

struct PowerResumeDebouncer {
    let debounceMs: UInt64
    private var hasEmitted = false
    private var lastEmitMs: UInt64 = 0

    init(debounceMs: UInt64 = 2000) {
        self.debounceMs = debounceMs
    }

    mutating func shouldEmit(nowMs: UInt64) -> Bool {
        if !hasEmitted || nowMs < lastEmitMs || (nowMs - lastEmitMs) >= debounceMs {
            hasEmitted = true
            lastEmitMs = nowMs
            return true
        }
        return false
    }
}

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

    let toastBg = HudRgba(r: 30.0 / 255.0, g: 31.0 / 255.0, b: 36.0 / 255.0, a: 0.98)
    let toastMainText = HudRgba(r: 244.0 / 255.0, g: 245.0 / 255.0, b: 248.0 / 255.0, a: 1.0)
    let toastHintText = HudRgba(r: 190.0 / 255.0, g: 195.0 / 255.0, b: 208.0 / 255.0, a: 1.0)
    let amberBorder = HudRgba(r: 251.0 / 255.0, g: 191.0 / 255.0, b: 36.0 / 255.0, a: 0.78)
    let amberIcon = HudRgba(r: 251.0 / 255.0, g: 191.0 / 255.0, b: 36.0 / 255.0, a: 1.0)
    let coralBorder = HudRgba(r: 248.0 / 255.0, g: 113.0 / 255.0, b: 113.0 / 255.0, a: 0.78)
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
            hintText: hudRgba(188, 178, 192, 1.0),
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
            autoHideMs: 2000,
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
    let padR: CGFloat = 16.0
    let waveW: CGFloat = WaveBarsView.totalWidth
    let gap1: CGFloat = 10.0
    let gap2: CGFloat = hasHint ? 10.0 : 0.0
    let badgePadX: CGFloat = 5.0
    let badgeSlotW: CGFloat = hasBadge ? max(22.0, ceil(badgeTextW) + badgePadX * 2.0) : 0.0
    let badgeH: CGFloat = hasBadge ? max(18.0, ceil(badgeTextH) + 4.0) : 0.0
    let badgeGap: CGFloat = hasBadge ? (hasHint ? 8.0 : 10.0) : 0.0
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

final class HudPillController {
    private static let recBg = rgba(64, 18, 26, 0.98)
    private static let recBorder = rgba(232, 88, 102, 0.68)
    private static let recWaveActive = rgba(255, 104, 112, 1.0)
    private static let recWaveQuiet = rgba(232, 92, 102, 1.0)
    private static let recMainText = rgba(248, 250, 252)
    private static let recHintTextCol = rgba(188, 178, 192, 1.0)

    private static let warnBg = rgba(72, 38, 12, 0.98)
    private static let warnBorder = rgba(245, 158, 11, 0.72)
    private static let warnWaveActive = rgba(251, 191, 36, 1.0)
    private static let warnWaveQuiet = rgba(251, 191, 36, 0.86)
    private static let warnMainText = rgba(254, 252, 232)
    private static let warnHintTextCol = rgba(251, 191, 36, 1.0)

    fileprivate static let badgeBg = rgba(56, 189, 248, 0.22)
    fileprivate static let badgeBorder = rgba(56, 189, 248, 0.58)
    fileprivate static let badgeTextCol = rgba(186, 230, 253, 1.0)

    fileprivate static let hudFont = NSFont.systemFont(ofSize: 13.5, weight: .regular)
    fileprivate static let monoDurFont = NSFont.monospacedDigitSystemFont(ofSize: 13.5, weight: .regular)
    fileprivate static let toastFont = NSFont.systemFont(ofSize: 12.5, weight: .regular)
    fileprivate static let badgeFont = NSFont.monospacedDigitSystemFont(ofSize: 11.0, weight: .semibold)
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
    fileprivate var debugMainVisible: Bool { isMainVisible }
    fileprivate var debugToastVisible: Bool { isToastVisible }
    fileprivate var debugMainBgCount: Int { recBgCount }
    fileprivate var debugHoldPreviewActive: Bool { holdCancelPreview }
    fileprivate var debugMainEffectiveHint: String {
        if holdCancelPreview {
            let effZh = isWaveRecording ? recZhHint : (currentMainText.isEmpty ? currentMainZh : containsCjkScalar(currentMainText))
            let hint = formatHoldCancelHint(zh: effZh)
            return isWaveRecording ? hint : "(\(hint))"
        }
        return isWaveRecording ? stripOuterParens(recHintText) : splitHudSegments(currentMainText).hint
    }
    fileprivate var debugToastSlotCount: Int { toastSlotCount }
    fileprivate var debugToastUpdateCount: Int { toastUpdateCount }
    fileprivate var debugToastLastText: String { toastLastText }
    fileprivate var debugToastLastAutoHideMs: Int { toastLastAutoHideMs }

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

        let bv = NSView(frame: NSRect(x: 0, y: 0, width: 22, height: 18))
        bv.wantsLayer = true
        bv.layer?.backgroundColor = Self.badgeBg.cgColor
        bv.layer?.borderColor = Self.badgeBorder.cgColor
        bv.layer?.borderWidth = 0.85
        bv.layer?.cornerRadius = 9.0
        bv.layer?.masksToBounds = true
        bv.isHidden = true

        let bl = NSTextField(labelWithString: "")
        bl.font = Self.badgeFont
        bl.textColor = Self.badgeTextCol
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

    private func activeScreenMetrics() -> (scale: CGFloat, visibleFrame: NSRect) {
        let mouseLoc = NSEvent.mouseLocation
        let activeScreen = NSScreen.screens.first(where: { NSMouseInRect(mouseLoc, $0.frame, false) }) ?? NSScreen.main ?? NSScreen.screens.first
        let scale = activeScreen?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2.0
        let vf = activeScreen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        return (scale, vf)
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

        let (scale, vf) = resolveAnchoredScreenMetrics(refresh: false)
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
    }

    func hide() {
        hideMain()
    }

    fileprivate static func resolveIconKind(stateKind: String, iconSeg: String) -> String {
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
            if !isWaveRecording {
                _ = resolveAnchoredScreenMetrics(refresh: !isToastVisible)
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
            layoutWavePill(forceFrame: true)
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

private func queryFrontWindowMetadata(pid: Int, bundleId: String) -> (hwnd: Int, title: String, document: String, axTrusted: Bool) {
    let trusted = AXIsProcessTrusted()
    guard pid > 0 else { return (0, "", "", trusted) }
    var winNum = 0
    if let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
        for entry in list {
            let ownerPid = (entry[kCGWindowOwnerPID as String] as? Int) ?? ((entry[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0)
            let layer = (entry[kCGWindowLayer as String] as? Int) ?? ((entry[kCGWindowLayer as String] as? NSNumber)?.intValue ?? -1)
            if ownerPid == pid && layer == 0 {
                winNum = (entry[kCGWindowNumber as String] as? Int) ?? ((entry[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0)
                break
            }
        }
    }
    var axTitle = ""
    var axDoc = ""
    if bundleId.lowercased() != "com.mitchellh.ghostty" && trusted {
        let axApp = AXUIElementCreateApplication(pid_t(pid))
        _ = AXUIElementSetMessagingTimeout(axApp, 0.25)
        var winRef: CFTypeRef?
        var st = AXUIElementCopyAttributeValue(axApp, kAXFocusedWindowAttribute as CFString, &winRef)
        if (st != .success || winRef == nil) && st != .cannotComplete {
            st = AXUIElementCopyAttributeValue(axApp, kAXMainWindowAttribute as CFString, &winRef)
        }
        if st == .success, let w = winRef, CFGetTypeID(w) == AXUIElementGetTypeID() {
            let axWin = unsafeBitCast(w, to: AXUIElement.self)
            _ = AXUIElementSetMessagingTimeout(axWin, 0.25)
            var titleRef: CFTypeRef?
            if AXUIElementCopyAttributeValue(axWin, kAXTitleAttribute as CFString, &titleRef) == .success,
               let s = titleRef as? String {
                axTitle = s.trimmingCharacters(in: .whitespacesAndNewlines)
            }
            var docRef: CFTypeRef?
            if AXUIElementCopyAttributeValue(axWin, kAXDocumentAttribute as CFString, &docRef) == .success, let d = docRef {
                let s = (d as? String) ?? (d as? URL)?.absoluteString ?? (d as? NSURL)?.absoluteString ?? ""
                let rawDoc = s.trimmingCharacters(in: .whitespacesAndNewlines)
                if rawDoc.lowercased().hasPrefix("file://") {
                    axDoc = rawDoc
                }
            }
        }
    }
    return (winNum, axTitle, axDoc, trusted)
}

private func queryFrontmostApp() -> (name: String, bundleId: String, pid: Int, hwnd: Int, title: String, document: String, axTrusted: Bool) {
    guard let app = NSWorkspace.shared.frontmostApplication else { return ("", "", 0, 0, "", "", AXIsProcessTrusted()) }
    let name = app.localizedName ?? app.bundleURL?.deletingPathExtension().lastPathComponent ?? ""
    let bundleId = app.bundleIdentifier ?? ""
    let pid = Int(app.processIdentifier)
    let meta = queryFrontWindowMetadata(pid: pid, bundleId: bundleId)
    return (name, bundleId, pid, meta.hwnd, meta.title, meta.document, meta.axTrusted)
}

private func parseOptionalBool(_ raw: Any?) -> Bool? {
    if let b = raw as? Bool { return b }
    if let n = raw as? NSNumber { return n.boolValue }
    return nil
}

private func audioDeviceInputChannelCount(_ devId: AudioDeviceID) -> Int {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain
    )
    if !AudioObjectHasProperty(devId, &addr) { return 0 }
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(devId, &addr, 0, nil, &size) == noErr,
          size >= UInt32(MemoryLayout<AudioBufferList>.size) else {
        return 0
    }
    let rawPtr = UnsafeMutableRawPointer.allocate(
        byteCount: Int(size),
        alignment: MemoryLayout<AudioBufferList>.alignment
    )
    defer { rawPtr.deallocate() }
    guard AudioObjectGetPropertyData(devId, &addr, 0, nil, &size, rawPtr) == noErr else {
        return 0
    }
    let ablPtr = rawPtr.assumingMemoryBound(to: AudioBufferList.self)
    let buffers = UnsafeMutableAudioBufferListPointer(ablPtr)
    var totalChannels = 0
    for buf in buffers {
        totalChannels += Int(buf.mNumberChannels)
    }
    return totalChannels
}

private func isAudioDeviceAlive(_ devId: AudioDeviceID) -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceIsAlive,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    if !AudioObjectHasProperty(devId, &addr) {
        return true
    }
    var alive: UInt32 = 1
    var size = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(devId, &addr, 0, nil, &size, &alive)
    if status != noErr {
        return true
    }
    return alive != 0
}

private func readAudioStringProperty(_ devId: AudioObjectID, selector: AudioObjectPropertySelector) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    if !AudioObjectHasProperty(devId, &addr) {
        return ""
    }
    var unmanagedStr: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &unmanagedStr) { ptr in
        AudioObjectGetPropertyData(devId, &addr, 0, nil, &size, ptr)
    }
    guard status == noErr, let cfStr = unmanagedStr?.takeRetainedValue() else {
        return ""
    }
    return (cfStr as String).trimmingCharacters(in: .whitespacesAndNewlines)
}

private func queryDefaultInputDeviceID() -> AudioDeviceID {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var devId = AudioDeviceID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devId)
    return (status == noErr) ? devId : AudioDeviceID(kAudioObjectUnknown)
}

func queryMicrophonePermissionInfo() -> [String: Any] {
    let micAuth: String
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized:
        micAuth = "authorized"
    case .denied:
        micAuth = "denied"
    case .restricted:
        micAuth = "restricted"
    case .notDetermined:
        micAuth = "not_determined"
    @unknown default:
        micAuth = "unknown"
    }
    var info: [String: Any] = ["mic_auth": micAuth]
    var sid = SecuritySessionId(0)
    var attrs = SessionAttributeBits(rawValue: 0)
    if SessionGetInfo(callerSecuritySession, &sid, &attrs) == errSessionSuccess {
        info["has_graphic_session"] = attrs.contains(.sessionHasGraphicAccess)
        info["is_remote_session"] = attrs.contains(.sessionIsRemote)
    }
    return info
}

func queryAudioInputStatus() -> [String: Any] {
    let permInfo = queryMicrophonePermissionInfo()
    func failedHalStatus() -> [String: Any] {
        var out: [String: Any] = [
            "hal_ok": false,
            "has_input": false,
            "input_count": 0,
            "default_uid": "",
            "default_name": "",
            "default_name_unique": false,
            "alive_uids": [String](),
            "uid_names": [String: String](),
        ]
        for (k, v) in permInfo { out[k] = v }
        return out
    }
    let sysObj = AudioObjectID(kAudioObjectSystemObject)
    var devicesAddr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(sysObj, &devicesAddr, 0, nil, &dataSize) == noErr else {
        return failedHalStatus()
    }
    let count = Int(dataSize) / MemoryLayout<AudioDeviceID>.size
    var deviceIds = [AudioDeviceID](repeating: 0, count: count)
    var actualCount = 0
    if count > 0 {
        var actualSize = dataSize
        guard AudioObjectGetPropertyData(sysObj, &devicesAddr, 0, nil, &actualSize, &deviceIds) == noErr else {
            return failedHalStatus()
        }
        actualCount = min(count, Int(actualSize) / MemoryLayout<AudioDeviceID>.size)
    }

    var aliveUids: [String] = []
    var uidNames: [String: String] = [:]
    var aliveById: [AudioDeviceID: (uid: String, name: String)] = [:]
    for idx in 0..<actualCount {
        let devId = deviceIds[idx]
        if devId == AudioDeviceID(kAudioObjectUnknown) { continue }
        if audioDeviceInputChannelCount(devId) <= 0 { continue }
        if !isAudioDeviceAlive(devId) { continue }
        let rawUid = readAudioStringProperty(devId, selector: kAudioDevicePropertyDeviceUID)
        let uid = rawUid.isEmpty ? "dev-\(devId)" : rawUid
        let name = readAudioStringProperty(devId, selector: kAudioObjectPropertyName)
        if !aliveUids.contains(uid) {
            aliveUids.append(uid)
        }
        uidNames[uid] = name
        aliveById[devId] = (uid: uid, name: name)
    }

    let defId = queryDefaultInputDeviceID()
    var defaultUid = ""
    var defaultName = ""
    if let entry = aliveById[defId] {
        defaultUid = entry.uid
        defaultName = entry.name
    }
    var defaultNameUnique = !defaultUid.isEmpty && !defaultName.isEmpty && !defaultName.contains(":")
    if defaultNameUnique {
        for (otherUid, otherName) in uidNames where otherUid != defaultUid {
            if otherName.isEmpty || otherName == defaultName || otherName.hasPrefix(defaultName) || defaultName.hasPrefix(otherName) {
                defaultNameUnique = false
                break
            }
        }
    }
    var result: [String: Any] = [
        "hal_ok": true,
        "has_input": !aliveUids.isEmpty,
        "input_count": aliveUids.count,
        "default_uid": defaultUid,
        "default_name": defaultName,
        "default_name_unique": defaultNameUnique,
        "alive_uids": aliveUids,
        "uid_names": uidNames,
    ]
    for (k, v) in permInfo { result[k] = v }
    return result
}

private func audioStatusSnapshotKey(_ st: [String: Any]) -> String {
    let hasIn = (st["has_input"] as? Bool) ?? false
    let cnt = (st["input_count"] as? Int) ?? 0
    let defUid = (st["default_uid"] as? String) ?? ""
    let defName = (st["default_name"] as? String) ?? ""
    let defUniq = (st["default_name_unique"] as? Bool) ?? false
    let uids = (st["alive_uids"] as? [String]) ?? []
    let uidNames = (st["uid_names"] as? [String: String]) ?? [:]
    let pairs = uids.map { "\($0)=\(uidNames[$0] ?? "")" }.joined(separator: ",")
    return "\(hasIn)|\(cnt)|\(defUid)|\(defName)|\(defUniq)|\(pairs)"
}

final class CoreAudioInputMonitor {
    private let queue = DispatchQueue(label: "agent_voice_input.coreaudio")
    private var sysDevicesBlock: AudioObjectPropertyListenerBlock?
    private var sysDefaultBlock: AudioObjectPropertyListenerBlock?
    private var devAliveBlock: AudioObjectPropertyListenerBlock?
    private var watchedDefaultDevId: AudioDeviceID = AudioDeviceID(kAudioObjectUnknown)
    private var lastSnapshotKey: String = ""
    private var started = false

    private func rebindDefaultDeviceAliveListenerOnQueue() {
        let newDevId = queryDefaultInputDeviceID()
        if newDevId == watchedDefaultDevId { return }
        var aliveAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsAlive,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        if watchedDefaultDevId != AudioDeviceID(kAudioObjectUnknown), let oldBlock = devAliveBlock {
            _ = AudioObjectRemovePropertyListenerBlock(watchedDefaultDevId, &aliveAddr, queue, oldBlock)
            watchedDefaultDevId = AudioDeviceID(kAudioObjectUnknown)
        }
        if newDevId != AudioDeviceID(kAudioObjectUnknown), let block = devAliveBlock, AudioObjectHasProperty(newDevId, &aliveAddr) {
            if AudioObjectAddPropertyListenerBlock(newDevId, &aliveAddr, queue, block) == noErr {
                watchedDefaultDevId = newDevId
            }
        }
    }

    private func emitIfChangedOnQueue() {
        if !started { return }
        rebindDefaultDeviceAliveListenerOnQueue()
        let st = queryAudioInputStatus()
        let key = audioStatusSnapshotKey(st)
        if key != lastSnapshotKey {
            lastSnapshotKey = key
            var ev = st
            ev["event"] = "audio_devices_changed"
            writeJsonLine(ev)
        }
    }

    func start() -> [String: Any] {
        return queue.sync {
            let initial = queryAudioInputStatus()
            if started { return initial }
            started = true
            lastSnapshotKey = audioStatusSnapshotKey(initial)

            let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
                self?.emitIfChangedOnQueue()
            }
            sysDevicesBlock = listener
            sysDefaultBlock = listener
            devAliveBlock = listener

            let sysObj = AudioObjectID(kAudioObjectSystemObject)
            var devsAddr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDevices,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            _ = AudioObjectAddPropertyListenerBlock(sysObj, &devsAddr, queue, listener)

            var defAddr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDefaultInputDevice,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            _ = AudioObjectAddPropertyListenerBlock(sysObj, &defAddr, queue, listener)

            rebindDefaultDeviceAliveListenerOnQueue()
            return initial
        }
    }

    func stop() {
        queue.sync {
            if !started { return }
            started = false
            let sysObj = AudioObjectID(kAudioObjectSystemObject)
            if let b1 = sysDevicesBlock {
                var devsAddr = AudioObjectPropertyAddress(
                    mSelector: kAudioHardwarePropertyDevices,
                    mScope: kAudioObjectPropertyScopeGlobal,
                    mElement: kAudioObjectPropertyElementMain
                )
                _ = AudioObjectRemovePropertyListenerBlock(sysObj, &devsAddr, queue, b1)
            }
            if let b2 = sysDefaultBlock {
                var defAddr = AudioObjectPropertyAddress(
                    mSelector: kAudioHardwarePropertyDefaultInputDevice,
                    mScope: kAudioObjectPropertyScopeGlobal,
                    mElement: kAudioObjectPropertyElementMain
                )
                _ = AudioObjectRemovePropertyListenerBlock(sysObj, &defAddr, queue, b2)
            }
            if watchedDefaultDevId != AudioDeviceID(kAudioObjectUnknown), let b3 = devAliveBlock {
                var aliveAddr = AudioObjectPropertyAddress(
                    mSelector: kAudioDevicePropertyDeviceIsAlive,
                    mScope: kAudioObjectPropertyScopeGlobal,
                    mElement: kAudioObjectPropertyElementMain
                )
                _ = AudioObjectRemovePropertyListenerBlock(watchedDefaultDevId, &aliveAddr, queue, b3)
                watchedDefaultDevId = AudioDeviceID(kAudioObjectUnknown)
            }
            sysDevicesBlock = nil
            sysDefaultBlock = nil
            devAliveBlock = nil
        }
    }
}

final class PasteboardGuard {
    enum WriteOutcome {
        case ok
        case expired
        case failed
    }

    static let defaultMaxSnapshotBytes = 100 * 1024 * 1024
    static let defaultRestoreDelayMs = 250
    static let transientType = NSPasteboard.PasteboardType("org.nspasteboard.TransientType")
    static let autoGeneratedType = NSPasteboard.PasteboardType("org.nspasteboard.AutoGeneratedType")
    static let concealedType = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")
    static let ownTransientMarker = NSPasteboard.PasteboardType("com.agentvoiceinput.transient")
    static let ownRestoredMarker = NSPasteboard.PasteboardType("com.agentvoiceinput.restored")
    private static let baseWhitelistTypes: [NSPasteboard.PasteboardType] = [.string, .rtf, .html, .png, .fileURL]

    private enum ClipSnap {
        case clearOnly
        case items([[(NSPasteboard.PasteboardType, Data)]])
    }

    private let pasteboardProvider: () -> NSPasteboard
    private let restoreDelayMs: Int
    private let maxSnapshotBytes: Int
    private var savedSnap: ClipSnap?
    private var expectedChangeCount: Int = -1
    private var expectedToken: Data?
    private(set) var restorePending: Bool = false
    private var restoreGen: UInt64 = 0
    private var restoreWork: DispatchWorkItem?

    init(
        pasteboard: NSPasteboard? = nil,
        restoreDelayMs: Int = PasteboardGuard.defaultRestoreDelayMs,
        maxSnapshotBytes: Int = PasteboardGuard.defaultMaxSnapshotBytes
    ) {
        if let pb = pasteboard {
            self.pasteboardProvider = { pb }
        } else {
            self.pasteboardProvider = { NSPasteboard.general }
        }
        self.restoreDelayMs = max(10, restoreDelayMs)
        self.maxSnapshotBytes = max(1, maxSnapshotBytes)
    }

    func cancelPendingRestore() {
        restoreGen &+= 1
        restoreWork?.cancel()
        restoreWork = nil
        restorePending = false
        expectedToken = nil
        savedSnap = nil
    }

    private func captureCurrent(pb: NSPasteboard) -> ClipSnap? {
        guard let rawItems = pb.pasteboardItems, !rawItems.isEmpty else {
            return .clearOnly
        }
        for item in rawItems {
            let itemTypes = item.types
            if itemTypes.contains(Self.concealedType) {
                return .clearOnly
            }
            if itemTypes.contains(Self.ownTransientMarker) {
                return nil
            }
            if itemTypes.contains(Self.transientType) && !itemTypes.contains(Self.ownRestoredMarker) {
                return .clearOnly
            }
        }
        var captured: [[(NSPasteboard.PasteboardType, Data)]] = []
        var totalBytes = 0
        for item in rawItems {
            let itemTypes = item.types
            var candidateTypes = Self.baseWhitelistTypes
            if !itemTypes.contains(.png) && itemTypes.contains(.tiff) {
                candidateTypes.append(.tiff)
            }
            var entries: [(NSPasteboard.PasteboardType, Data)] = []
            for t in candidateTypes where itemTypes.contains(t) {
                guard let d = item.data(forType: t) else { continue }
                if d.count > maxSnapshotBytes || (totalBytes + d.count) > maxSnapshotBytes {
                    return nil
                }
                totalBytes += d.count
                entries.append((t, d))
            }
            if !entries.isEmpty {
                captured.append(entries)
            }
        }
        if captured.isEmpty {
            return nil
        }
        return .items(captured)
    }

    func writePersistentFallback(_ text: String) -> Bool {
        cancelPendingRestore()
        let pb = pasteboardProvider()
        pb.clearContents()
        if text.isEmpty {
            return true
        }
        return pb.setString(text, forType: .string)
    }

    func writeTransientChecked(_ text: String, deadlineMonoMs: UInt64) -> WriteOutcome {
        if deadlineMonoMs > 0 && monotonicMs() >= deadlineMonoMs {
            return .expired
        }
        let pb = pasteboardProvider()
        let isStillOurs: Bool = {
            guard restorePending, pb.changeCount == expectedChangeCount, let expTok = expectedToken else {
                return false
            }
            return pb.data(forType: Self.ownTransientMarker) == expTok
        }()
        let nextSnap: ClipSnap? = isStillOurs ? savedSnap : captureCurrent(pb: pb)
        if deadlineMonoMs > 0 && monotonicMs() >= deadlineMonoMs {
            return .expired
        }
        restoreGen &+= 1
        restoreWork?.cancel()
        restoreWork = nil
        restorePending = false
        expectedToken = nil
        let myGen = restoreGen
        let tokenData = Data("avi:\(myGen)".utf8)

        pb.clearContents()
        let item = NSPasteboardItem()
        guard item.setString(text, forType: .string) else {
            savedSnap = nil
            return .failed
        }
        _ = item.setData(Data(), forType: Self.transientType)
        _ = item.setData(Data(), forType: Self.autoGeneratedType)
        _ = item.setData(tokenData, forType: Self.ownTransientMarker)
        guard pb.writeObjects([item]) else {
            savedSnap = nil
            return .failed
        }
        savedSnap = nextSnap
        if nextSnap != nil {
            expectedChangeCount = pb.changeCount
            expectedToken = tokenData
            restorePending = true
            let work = DispatchWorkItem { [weak self] in
                guard let self = self, self.restorePending, self.restoreGen == myGen else { return }
                self.flushPendingRestore()
            }
            restoreWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(restoreDelayMs), execute: work)
        }
        return .ok
    }

    func writeTransientAndArm(_ text: String) -> Bool {
        return writeTransientChecked(text, deadlineMonoMs: 0) == .ok
    }

    func flushPendingRestore() {
        guard restorePending else { return }
        let snap = savedSnap
        let exp = expectedChangeCount
        let expTok = expectedToken
        cancelPendingRestore()
        let pb = pasteboardProvider()
        guard let snap = snap, let expTok = expTok, pb.changeCount == exp, pb.data(forType: Self.ownTransientMarker) == expTok else {
            return
        }
        pb.clearContents()
        switch snap {
        case .clearOnly:
            break
        case .items(let itemList):
            var rebuilt: [NSPasteboardItem] = []
            for entries in itemList {
                let item = NSPasteboardItem()
                var wroteAny = false
                for (t, d) in entries {
                    if item.setData(d, forType: t) {
                        wroteAny = true
                    }
                }
                if wroteAny {
                    _ = item.setData(Data(), forType: Self.transientType)
                    _ = item.setData(Data(), forType: Self.autoGeneratedType)
                    _ = item.setData(Data(), forType: Self.ownRestoredMarker)
                    rebuilt.append(item)
                }
            }
            if !rebuilt.isEmpty {
                _ = pb.writeObjects(rebuilt)
            }
        }
    }
}

private func regionHasDistinctForeground(
    data: UnsafeMutablePointer<UInt8>,
    bpr: Int,
    spp: Int,
    pxW: Int,
    pxH: Int,
    rect: NSRect,
    scale: CGFloat,
    bgR: Int,
    bgG: Int,
    bgB: Int
) -> Bool {
    let leftPx = max(0, min(pxW, Int(floor(rect.minX * scale))))
    let rightPx = max(0, min(pxW, Int(ceil(rect.maxX * scale))))
    let topPx = max(2, min(pxH - 2, pxH - Int(ceil(rect.maxY * scale))))
    let botPx = max(2, min(pxH - 2, pxH - Int(floor(rect.minY * scale))))
    if leftPx >= rightPx || topPx >= botPx { return false }
    for y in topPx..<botPx {
        let row = y * bpr
        for x in leftPx..<rightPx {
            let idx = row + x * spp
            let a = Int(data[idx + 3])
            if a < 180 { continue }
            let dr = abs(Int(data[idx]) - bgR)
            let dg = abs(Int(data[idx + 1]) - bgG)
            let db = abs(Int(data[idx + 2]) - bgB)
            if dr + dg + db >= 36 { return true }
        }
    }
    return false
}

private func runHudLayoutSelfCheck() throws -> Int {
    func assertLayout(_ cond: Bool, _ msg: String) throws {
        if !cond {
            throw NSError(domain: "SelfCheck", code: 10, userInfo: [NSLocalizedDescriptionKey: "HUD layout check failed: \(msg)"])
        }
    }

    func measureCell(_ attr: NSAttributedString) -> NSSize {
        let raw = attr.size()
        return NSSize(width: ceil(raw.width) + 4.0, height: ceil(raw.height))
    }

    func renderAndVerifyWave(
        label: String,
        text: String,
        elapsedSec: Int,
        maxDurSec: Int,
        warnRemSec: Int,
        bgCount: Int,
        zh: Bool,
        visibleW: CGFloat,
        scale: CGFloat,
        holdCancelPreview: Bool = false
    ) throws {
        let seg = splitHudSegments(text)
        var isWarn = false
        var remainSec = 0
        if maxDurSec > 0 && warnRemSec > 0 {
            remainSec = max(0, maxDurSec - elapsedSec)
            isWarn = (remainSec <= warnRemSec)
        }
        let effHint: String
        let hintTpl: String
        if holdCancelPreview {
            effHint = formatHoldCancelHint(zh: zh)
            hintTpl = effHint
        } else {
            let rawHint = isWarn ? formatCountdownHint(remainSec, zh: zh) : seg.hint
            effHint = stripOuterParens(rawHint)
            hintTpl = isWarn ? stripOuterParens(countdownHintSlotTemplate(remainSec, zh: zh)) : effHint
        }
        let durStr = formatRecordingDuration(elapsedSec)
        let durTpl = durationSlotTemplate(elapsedSec)

        let centerPara = NSMutableParagraphStyle()
        centerPara.alignment = .center
        let leftPara = NSMutableParagraphStyle()
        leftPara.alignment = .left

        let mainCol = isWarn ? rgba(254, 252, 232) : rgba(248, 250, 252)
        let hintCol = (isWarn || holdCancelPreview) ? rgba(251, 191, 36, 1.0) : rgba(188, 178, 192, 1.0)
        let bgCol = isWarn ? rgba(72, 38, 12, 0.98) : rgba(64, 18, 26, 0.98)
        let borderCol = isWarn ? rgba(245, 158, 11, 0.72) : rgba(232, 88, 102, 0.68)
        let waveCol = isWarn ? rgba(251, 191, 36, 1.0) : rgba(255, 104, 112, 1.0)

        let durAttr = NSAttributedString(string: durStr, attributes: [.font: HudPillController.monoDurFont, .foregroundColor: mainCol, .paragraphStyle: centerPara])
        let durTplAttr = NSAttributedString(string: durTpl, attributes: [.font: HudPillController.monoDurFont, .foregroundColor: mainCol, .paragraphStyle: centerPara])
        let durActSz = measureCell(durAttr)
        let durTplSz = measureCell(durTplAttr)
        let durSlotW = max(durActSz.width, durTplSz.width)
        let durH = max(durActSz.height, durTplSz.height)

        let hintAttr = NSAttributedString(string: effHint, attributes: [.font: HudPillController.hudFont, .foregroundColor: hintCol, .paragraphStyle: leftPara])
        let hintTplAttr = NSAttributedString(string: hintTpl, attributes: [.font: HudPillController.hudFont, .foregroundColor: hintCol, .paragraphStyle: leftPara])
        let hintActSz = effHint.isEmpty ? NSSize.zero : measureCell(hintAttr)
        let hintTplSz = effHint.isEmpty ? NSSize.zero : measureCell(hintTplAttr)
        let hintSlotW = max(hintActSz.width, hintTplSz.width)
        let hintH = max(hintActSz.height, hintTplSz.height)

        let hasBadge = bgCount > 0
        let badgeStr = hasBadge ? "+\(bgCount)" : ""
        let badgeAttr = NSAttributedString(string: badgeStr, attributes: [.font: HudPillController.badgeFont, .foregroundColor: HudPillController.badgeTextCol, .paragraphStyle: centerPara])
        let badgeSz = hasBadge ? measureCell(badgeAttr) : NSSize.zero

        let geom = computeWaveHudLayout(
            durSlotW: durSlotW,
            durH: durH,
            hintW: hintSlotW,
            hintH: hintH,
            hasHint: !effHint.isEmpty,
            badgeTextW: badgeSz.width,
            badgeTextH: badgeSz.height,
            hasBadge: hasBadge,
            visibleWidth: visibleW,
            scale: scale
        )

        try assertLayout(geom.pillW > 0.0 && geom.pillW <= geom.maxW && geom.pillH > 0.0 && geom.pillW >= geom.padL + geom.padR, "\(label): bounds invalid")
        try assertLayout(!geom.truncated, "\(label): unexpected truncation pillW=\(geom.pillW) maxW=\(geom.maxW)")
        try assertLayout(geom.durFrame.minX >= geom.padL - 0.5, "\(label): durFrame left padding")
        try assertLayout(geom.durFrame.width >= durActSz.width && geom.durSlotW >= durActSz.width, "\(label): durFrame overflow")
        try assertLayout(geom.durFrame.maxX < geom.waveFrame.minX, "\(label): durFrame overlaps waveFrame")
        try assertLayout(geom.waveFrame.width > 0.0, "\(label): waveFrame empty")
        if !effHint.isEmpty {
            try assertLayout(geom.waveFrame.maxX < geom.hintFrame.minX, "\(label): waveFrame overlaps hintFrame")
            try assertLayout(geom.hintFrame.width >= hintActSz.width && geom.hintSlotW >= hintActSz.width, "\(label): hintFrame overflow")
        }
        if hasBadge {
            let prevRight = effHint.isEmpty ? geom.waveFrame.maxX : geom.hintFrame.maxX
            try assertLayout(prevRight < geom.badgeFrame.minX, "\(label): badgeFrame overlaps previous segment")
            try assertLayout(geom.badgeFrame.width >= badgeSz.width, "\(label): badgeFrame overflow")
            try assertLayout(geom.badgeFrame.maxX <= geom.pillW - geom.padR, "\(label): badgeFrame right padding")
        } else {
            try assertLayout(geom.badgeFrame == .zero, "\(label): unexpected badgeFrame when bgCount=0")
            let lastRight = effHint.isEmpty ? geom.waveFrame.maxX : geom.hintFrame.maxX
            try assertLayout(lastRight <= geom.pillW - geom.padR, "\(label): hintFrame right padding")
        }

        let pxW = Int(round(geom.pillW * scale))
        let pxH = Int(round(geom.pillH * scale))
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: pxW,
            pixelsHigh: pxH,
            bitsPerSample: 8,
            samplesPerPixel: 4,
            hasAlpha: true,
            isPlanar: false,
            colorSpaceName: .deviceRGB,
            bytesPerRow: 0,
            bitsPerPixel: 0
        ), let ctx = NSGraphicsContext(bitmapImageRep: rep) else {
            throw NSError(domain: "SelfCheck", code: 11, userInfo: [NSLocalizedDescriptionKey: "\(label): failed to allocate offscreen bitmap"])
        }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = ctx
        defer { NSGraphicsContext.restoreGraphicsState() }
        ctx.cgContext.scaleBy(x: scale, y: scale)
        ctx.cgContext.clear(CGRect(x: 0.0, y: 0.0, width: geom.pillW, height: geom.pillH))

        let capsule = NSBezierPath(roundedRect: NSRect(x: 0.5, y: 0.5, width: geom.pillW - 1.0, height: geom.pillH - 1.0), xRadius: (geom.pillH - 1.0) / 2.0, yRadius: (geom.pillH - 1.0) / 2.0)
        bgCol.setFill()
        capsule.fill()
        borderCol.setStroke()
        capsule.lineWidth = 1.0
        capsule.stroke()

        durAttr.draw(in: geom.durFrame)
        let norms: [CGFloat] = [0.2, 0.45, 0.75, 0.95, 1.0, 0.85, 0.6, 0.35, 0.15]
        let maxBarH = max(WaveBarsView.barWidth, min(geom.pillH - 8.0, WaveBarsView.maxBarHeight))
        waveCol.setFill()
        for idx in 0..<9 {
            let bh = WaveBarsView.barWidth + norms[idx] * (maxBarH - WaveBarsView.barWidth)
            let bx = geom.waveFrame.minX + CGFloat(idx) * (WaveBarsView.barWidth + WaveBarsView.barGap)
            let by = (geom.pillH - bh) * 0.5
            NSBezierPath(roundedRect: NSRect(x: bx, y: by, width: WaveBarsView.barWidth, height: bh), xRadius: WaveBarsView.barWidth * 0.5, yRadius: WaveBarsView.barWidth * 0.5).fill()
        }
        if !effHint.isEmpty {
            hintAttr.draw(in: NSRect(x: geom.hintFrame.minX + 2.0, y: geom.hintFrame.minY, width: max(1.0, geom.hintFrame.width - 2.0), height: geom.hintFrame.height))
        }
        if hasBadge {
            let bPath = NSBezierPath(roundedRect: geom.badgeFrame, xRadius: geom.badgeFrame.height / 2.0, yRadius: geom.badgeFrame.height / 2.0)
            HudPillController.badgeBg.setFill()
            bPath.fill()
            HudPillController.badgeBorder.setStroke()
            bPath.lineWidth = 0.85
            bPath.stroke()
            let drawBadgeRect = NSRect(x: geom.badgeFrame.minX, y: geom.badgeFrame.minY + geom.badgeLabelFrame.minY, width: geom.badgeFrame.width, height: geom.badgeLabelFrame.height)
            badgeAttr.draw(in: drawBadgeRect)
        }
        ctx.flushGraphics()

        guard let data = rep.bitmapData else {
            throw NSError(domain: "SelfCheck", code: 12, userInfo: [NSLocalizedDescriptionKey: "\(label): missing bitmapData"])
        }
        let bpr = rep.bytesPerRow
        let spp = rep.samplesPerPixel
        try assertLayout(data[3] == 0, "\(label): top-left corner alpha not 0")
        let bgX = max(3, min(pxW - 4, Int(round(geom.padL * 0.5 * scale))))
        let bgIdx = (pxH / 2) * bpr + bgX * spp
        try assertLayout(Int(data[bgIdx + 3]) >= 180, "\(label): interior pixel unexpectedly transparent")
        let bgR = Int(data[bgIdx]), bgG = Int(data[bgIdx + 1]), bgB = Int(data[bgIdx + 2])
        try assertLayout(regionHasDistinctForeground(data: data, bpr: bpr, spp: spp, pxW: pxW, pxH: pxH, rect: geom.durFrame, scale: scale, bgR: bgR, bgG: bgG, bgB: bgB), "\(label): durFrame blank")
        try assertLayout(regionHasDistinctForeground(data: data, bpr: bpr, spp: spp, pxW: pxW, pxH: pxH, rect: geom.waveFrame, scale: scale, bgR: bgR, bgG: bgG, bgB: bgB), "\(label): waveFrame blank")
        if !effHint.isEmpty {
            try assertLayout(regionHasDistinctForeground(data: data, bpr: bpr, spp: spp, pxW: pxW, pxH: pxH, rect: geom.hintFrame, scale: scale, bgR: bgR, bgG: bgG, bgB: bgB), "\(label): hintFrame blank")
        }
        if hasBadge {
            try assertLayout(regionHasDistinctForeground(data: data, bpr: bpr, spp: spp, pxW: pxW, pxH: pxH, rect: geom.badgeFrame, scale: scale, bgR: bgR, bgG: bgG, bgB: bgB), "\(label): badgeFrame blank")
        }
    }

    func renderAndVerifyStatus(
        label: String,
        state: String,
        customText: String,
        explicitLayer: String,
        zh: Bool,
        visibleW: CGFloat,
        scale: CGFloat,
        holdCancelPreview: Bool = false
    ) throws {
        let style = resolveHudVisualStyle(state: state, explicitLayer: explicitLayer, zh: zh)
        let isToast = (style.layer == "toast")
        let rawTxt = customText.isEmpty ? style.defaultText : customText
        let baseSeg = splitHudSegments(rawTxt)
        let effHold = holdCancelPreview && !isToast && isCancellableMainHudState(state)
        let seg = effHold
            ? HudSegments(icon: baseSeg.icon, main: baseSeg.main, hint: "(\(formatHoldCancelHint(zh: zh)))", hasSpaceBeforeHint: true)
            : baseSeg
        let hintCol = effHold ? rgba(251, 191, 36, 1.0) : style.hintText.nsColor
        let effIconKind = HudPillController.resolveIconKind(stateKind: style.iconKind, iconSeg: seg.icon)
        let hasIcon = isToast ? !effIconKind.isEmpty : (!seg.icon.isEmpty || !style.iconKind.isEmpty)
        let bodyTxt = isToast ? stripLeadingHudSymbol(rawTxt) : (seg.main + (seg.hint.isEmpty ? "" : ((seg.hasSpaceBeforeHint ? " " : "") + seg.hint)))
        let hasBody = !bodyTxt.isEmpty
        let font = isToast ? HudPillController.toastFont : HudPillController.hudFont
        let para = NSMutableParagraphStyle()
        para.alignment = hasIcon ? .left : .center
        let attr = NSMutableAttributedString()
        if isToast && seg.hint.isEmpty {
            if !bodyTxt.isEmpty {
                attr.append(NSAttributedString(string: bodyTxt, attributes: [.font: font, .foregroundColor: style.mainText.nsColor, .paragraphStyle: para]))
            }
        } else {
            if !seg.main.isEmpty {
                attr.append(NSAttributedString(string: seg.main, attributes: [.font: font, .foregroundColor: style.mainText.nsColor, .paragraphStyle: para]))
            }
            if !seg.hint.isEmpty {
                let prefixSpace = (seg.hasSpaceBeforeHint && attr.length > 0) ? " " : ""
                attr.append(NSAttributedString(string: prefixSpace + seg.hint, attributes: [.font: font, .foregroundColor: hintCol, .paragraphStyle: para]))
            }
        }
        let measured = hasBody ? measureCell(attr) : NSSize(width: 0.0, height: isToast ? 17.0 : 18.0)
        let geom = computeStatusHudLayout(
            isToast: isToast,
            hasIcon: hasIcon,
            hasBody: hasBody,
            textW: measured.width,
            textH: measured.height,
            visibleWidth: visibleW,
            scale: scale
        )
        try assertLayout(geom.pillW > 0.0 && geom.pillW <= geom.maxW && geom.pillH > 0.0 && geom.pillW >= geom.padL + geom.padR, "\(label): bounds invalid")
        try assertLayout(!geom.truncated, "\(label): unexpected truncation pillW=\(geom.pillW) maxW=\(geom.maxW)")
        if hasIcon {
            try assertLayout(geom.iconFrame.minX >= geom.padL - 0.5, "\(label): iconFrame left padding")
            if hasBody {
                try assertLayout(geom.iconFrame.maxX < geom.labelFrame.minX, "\(label): iconFrame overlaps labelFrame")
            }
        }
        if hasBody {
            try assertLayout(geom.labelFrame.width >= measured.width - 0.5, "\(label): labelFrame smaller than measured text")
            try assertLayout(geom.labelFrame.maxX <= geom.pillW - geom.padR + 0.5, "\(label): labelFrame right padding")
        }

        let pxW = Int(round(geom.pillW * scale))
        let pxH = Int(round(geom.pillH * scale))
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: pxW,
            pixelsHigh: pxH,
            bitsPerSample: 8,
            samplesPerPixel: 4,
            hasAlpha: true,
            isPlanar: false,
            colorSpaceName: .deviceRGB,
            bytesPerRow: 0,
            bitsPerPixel: 0
        ), let ctx = NSGraphicsContext(bitmapImageRep: rep) else {
            throw NSError(domain: "SelfCheck", code: 13, userInfo: [NSLocalizedDescriptionKey: "\(label): failed to allocate status offscreen bitmap"])
        }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = ctx
        defer { NSGraphicsContext.restoreGraphicsState() }
        ctx.cgContext.scaleBy(x: scale, y: scale)
        ctx.cgContext.clear(CGRect(x: 0.0, y: 0.0, width: geom.pillW, height: geom.pillH))

        let capsule = NSBezierPath(roundedRect: NSRect(x: 0.5, y: 0.5, width: geom.pillW - 1.0, height: geom.pillH - 1.0), xRadius: (geom.pillH - 1.0) / 2.0, yRadius: (geom.pillH - 1.0) / 2.0)
        style.bg.nsColor.setFill()
        capsule.fill()
        style.border.nsColor.setStroke()
        capsule.lineWidth = style.borderWidth
        capsule.stroke()

        if hasIcon {
            drawStatusIcon(
                kind: effIconKind,
                color: style.icon.nsColor,
                bounds: geom.iconFrame,
                box: isToast ? StatusIconView.toastBoxSize : StatusIconView.mainBoxSize,
                isFlipped: false
            )
        }
        if hasBody {
            attr.draw(in: NSRect(x: geom.labelFrame.minX + 2.0, y: geom.labelFrame.minY, width: max(1.0, geom.labelFrame.width - 2.0), height: geom.labelFrame.height))
        }
        ctx.flushGraphics()

        guard let data = rep.bitmapData else {
            throw NSError(domain: "SelfCheck", code: 14, userInfo: [NSLocalizedDescriptionKey: "\(label): missing status bitmapData"])
        }
        let bpr = rep.bytesPerRow
        let spp = rep.samplesPerPixel
        try assertLayout(data[3] == 0, "\(label): status top-left corner alpha not 0")
        let bgX = max(3, min(pxW - 4, Int(round(geom.padL * 0.5 * scale))))
        let bgIdx = (pxH / 2) * bpr + bgX * spp
        try assertLayout(Int(data[bgIdx + 3]) >= 180, "\(label): status interior pixel transparent")
        let bgR = Int(data[bgIdx]), bgG = Int(data[bgIdx + 1]), bgB = Int(data[bgIdx + 2])
        if hasIcon {
            try assertLayout(regionHasDistinctForeground(data: data, bpr: bpr, spp: spp, pxW: pxW, pxH: pxH, rect: geom.iconFrame, scale: scale, bgR: bgR, bgG: bgG, bgB: bgB), "\(label): iconFrame blank")
        }
        if hasBody {
            try assertLayout(regionHasDistinctForeground(data: data, bpr: bpr, spp: spp, pxW: pxW, pxH: pxH, rect: geom.labelFrame, scale: scale, bgR: bgR, bgG: bgG, bgB: bgB), "\(label): labelFrame blank")
        }
    }

    var cases = 0
    let scaleSpecs: [(scale: CGFloat, width: CGFloat)] = [(1.0, 1280.0), (2.0, 1440.0)]
    let enRecTexts = [
        "● Listening… (press Right Ctrl to end)",
        "● Listening… (release Right Ctrl to end)",
        "● Listening… (press Right Option to end)",
        "● Listening… (release ScrollLock to end)",
    ]
    let zhRecTexts = [
        "● 录音中… (按右 Ctrl 结束)",
        "● 录音中… (松开右 Ctrl 结束)",
        "● 录音中… (按右 Option 结束)",
        "● 录音中… (松开 ScrollLock 结束)",
    ]
    let mainStates = ["recognizing", "processing", "refining", "done", "copied"]
    let toastStates = [
        "cancelled", "empty", "no_mic", "mic_unavailable", "mic_no_stream",
        "mic_permission", "mic_disconnected", "audio_recovered", "paste_failed", "error",
    ]

    for spec in scaleSpecs {
        for zh in [false, true] {
            let langTag = zh ? "zh" : "en"
            let recTexts = zh ? zhRecTexts : enRecTexts
            for (rIdx, recTxt) in recTexts.enumerated() {
                let dur = (rIdx == 0) ? 0 : ((rIdx == 1) ? 59 : ((rIdx == 2) ? 65 : 605))
                for bg in [0, 1, 2] {
                    try renderAndVerifyWave(
                        label: "mac_rec_\(langTag)_s\(Int(spec.scale))_k\(rIdx)_bg\(bg)",
                        text: recTxt,
                        elapsedSec: dur,
                        maxDurSec: 0,
                        warnRemSec: 0,
                        bgCount: bg,
                        zh: zh,
                        visibleW: spec.width,
                        scale: spec.scale
                    )
                    cases += 1
                }
            }
            try renderAndVerifyWave(
                label: "mac_warn12_\(langTag)_s\(Int(spec.scale))",
                text: recTexts[0],
                elapsedSec: 48,
                maxDurSec: 60,
                warnRemSec: 15,
                bgCount: 0,
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale
            )
            cases += 1
            try renderAndVerifyWave(
                label: "mac_warn5_bg2_\(langTag)_s\(Int(spec.scale))",
                text: recTexts[2],
                elapsedSec: 55,
                maxDurSec: 60,
                warnRemSec: 15,
                bgCount: 2,
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale
            )
            cases += 1
            try renderAndVerifyWave(
                label: "mac_rec_hold_\(langTag)_s\(Int(spec.scale))",
                text: recTexts[0],
                elapsedSec: 12,
                maxDurSec: 0,
                warnRemSec: 0,
                bgCount: 0,
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale,
                holdCancelPreview: true
            )
            cases += 1
            try renderAndVerifyWave(
                label: "mac_rec_hold_bg2_\(langTag)_s\(Int(spec.scale))",
                text: recTexts[2],
                elapsedSec: 65,
                maxDurSec: 600,
                warnRemSec: 15,
                bgCount: 2,
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale,
                holdCancelPreview: true
            )
            cases += 1
            try renderAndVerifyWave(
                label: "mac_rec_hold_warn_\(langTag)_s\(Int(spec.scale))",
                text: recTexts[0],
                elapsedSec: 55,
                maxDurSec: 60,
                warnRemSec: 15,
                bgCount: 1,
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale,
                holdCancelPreview: true
            )
            cases += 1
            try renderAndVerifyStatus(
                label: "mac_main_recognizing_hold_\(langTag)_s\(Int(spec.scale))",
                state: "recognizing",
                customText: "",
                explicitLayer: "main",
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale,
                holdCancelPreview: true
            )
            cases += 1
            try renderAndVerifyStatus(
                label: "mac_main_refining_hold_\(langTag)_s\(Int(spec.scale))",
                state: "refining",
                customText: "",
                explicitLayer: "main",
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale,
                holdCancelPreview: true
            )
            cases += 1

            for st in mainStates {
                try renderAndVerifyStatus(
                    label: "mac_main_\(st)_\(langTag)_s\(Int(spec.scale))",
                    state: st,
                    customText: "",
                    explicitLayer: "main",
                    zh: zh,
                    visibleW: spec.width,
                    scale: spec.scale
                )
                cases += 1
            }
            try renderAndVerifyStatus(
                label: "mac_main_cancelling_\(langTag)_s\(Int(spec.scale))",
                state: "processing",
                customText: zh ? "○ 正在取消…" : "○ Cancelling…",
                explicitLayer: "main",
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale
            )
            cases += 1

            for tst in toastStates {
                try renderAndVerifyStatus(
                    label: "mac_toast_\(tst)_\(langTag)_s\(Int(spec.scale))",
                    state: tst,
                    customText: "",
                    explicitLayer: "toast",
                    zh: zh,
                    visibleW: spec.width,
                    scale: spec.scale
                )
                cases += 1
            }
            try renderAndVerifyStatus(
                label: "mac_toast_copied_\(langTag)_s\(Int(spec.scale))",
                state: "copied",
                customText: "",
                explicitLayer: "toast",
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale
            )
            cases += 1
        }
    }

    // Verify single-slot toast replacement & clearMain/clearToast invariants through production HudPillController.update(...)
    let ctrl = HudPillController(hotkeyLabel: "右 Ctrl", hotkeyLabelEn: "Right Ctrl", isPtt: false, headless: true)
    defer { ctrl.close() }
    ctrl.update(state: "recording", customText: "", durationMs: 0, bgCount: 2)
    try assertLayout(ctrl.debugMainVisible && ctrl.debugMainBgCount == 2 && !ctrl.debugToastVisible && ctrl.debugToastSlotCount == 0, "slot init recording bg=2")
    for (idx, (tst, expMs)) in [("mic_no_stream", 2600), ("copied", 2000), ("cancelled", 1600), ("audio_recovered", 2400)].enumerated() {
        ctrl.update(state: tst, customText: "", durationMs: 0, layer: "toast", clearMain: false)
        try assertLayout(
            ctrl.debugToastSlotCount == 1
                && ctrl.debugToastVisible
                && ctrl.debugToastUpdateCount == idx + 1
                && ctrl.debugToastLastAutoHideMs == expMs
                && !ctrl.debugToastLastText.isEmpty
                && ctrl.debugMainVisible,
            "slot toast in-place replace \(tst)"
        )
    }
    ctrl.update(state: "recording", customText: "", durationMs: 0, clearToast: false, bgCount: 1)
    try assertLayout(ctrl.debugMainVisible && ctrl.debugMainBgCount == 1 && ctrl.debugToastVisible && ctrl.debugToastSlotCount == 1, "slot recording bg 2->1 keeps toast")
    ctrl.update(state: "recording", customText: "", durationMs: 0, clearToast: false, bgCount: 0)
    try assertLayout(ctrl.debugMainVisible && ctrl.debugMainBgCount == 0 && ctrl.debugToastVisible && ctrl.debugToastSlotCount == 1, "slot recording bg 1->0 keeps toast")
    ctrl.update(state: "cancelled", customText: "", durationMs: 0, layer: "toast", clearMain: true)
    try assertLayout(!ctrl.debugMainVisible && ctrl.debugMainBgCount == 0 && ctrl.debugToastVisible && ctrl.debugToastSlotCount == 1, "slot clearMain hides main keeps toast")
    ctrl.update(state: "hide_toast", customText: "", durationMs: 0)
    try assertLayout(!ctrl.debugMainVisible && !ctrl.debugToastVisible && ctrl.debugToastSlotCount == 1, "slot hide_toast clears toast")
    ctrl.update(state: "mic_no_stream", customText: "", durationMs: 0, layer: "toast", clearMain: false)
    try assertLayout(ctrl.debugToastVisible && ctrl.debugToastSlotCount == 1, "slot re-show toast reuses single slot")
    ctrl.update(state: "recording", customText: "", durationMs: 0, clearToast: nil, bgCount: 0)
    try assertLayout(ctrl.debugMainVisible && !ctrl.debugToastVisible, "slot fresh recording default clearToast clears toast")

    // Verify hold-to-cancel preview activates ONLY on cancellable main states and restores cleanly
    ctrl.update(state: "recording", customText: "● 录音中… (按右 Ctrl 结束)", durationMs: 0, clearToast: false, bgCount: 2)
    ctrl.update(state: "audio_recovered", customText: "", durationMs: 0, layer: "toast", clearMain: false)
    ctrl.setHoldCancelPreview(true)
    try assertLayout(ctrl.debugHoldPreviewActive && ctrl.debugMainEffectiveHint == formatHoldCancelHint(zh: true) && ctrl.debugMainBgCount == 2 && ctrl.debugToastVisible, "slot recording hold preview zh")
    ctrl.setHoldCancelPreview(false)
    try assertLayout(!ctrl.debugHoldPreviewActive && ctrl.debugMainEffectiveHint != formatHoldCancelHint(zh: true) && ctrl.debugMainBgCount == 2 && ctrl.debugToastVisible, "slot recording hold preview restore")

    ctrl.update(state: "refining", customText: "◈ Refining…", durationMs: 0, layer: "main", clearToast: false)
    ctrl.setHoldCancelPreview(true)
    try assertLayout(ctrl.debugHoldPreviewActive && ctrl.debugMainEffectiveHint == "(\(formatHoldCancelHint(zh: false)))", "slot refining hold preview en")
    ctrl.setHoldCancelPreview(false)
    try assertLayout(!ctrl.debugHoldPreviewActive && ctrl.debugMainEffectiveHint.isEmpty, "slot refining hold preview restore")

    ctrl.update(state: "done", customText: "", durationMs: 900, layer: "main", clearToast: false)
    ctrl.setHoldCancelPreview(true)
    try assertLayout(!ctrl.debugHoldPreviewActive, "slot done ignores hold preview")
    ctrl.update(state: "copied", customText: "", durationMs: 2000, layer: "main", clearToast: false)
    ctrl.setHoldCancelPreview(true)
    try assertLayout(!ctrl.debugHoldPreviewActive, "slot copied ignores hold preview")

    ctrl.update(state: "hide_all", customText: "", durationMs: 0)
    ctrl.setHoldCancelPreview(true)
    try assertLayout(!ctrl.debugMainVisible && !ctrl.debugToastVisible && !ctrl.debugHoldPreviewActive, "slot hide_all clears both")

    return cases
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
        var hotkeyFsmChecks = 0
        var eng = HotkeyEngine(spec: rctrl, isPtt: false, tapMaxMs: 500, holdCancelMs: 800, holdHintMs: 250, debounceMs: 180, keyTest: false)
        for (idx, (kc, flags)) in [(UInt16(0x3E), FLAG_CONTROL | NX_DEVICERCTLKEYMASK), (UInt16(0x3B), FLAG_CONTROL | NX_DEVICERCTLKEYMASK), (UInt16(0x3E), FLAG_CONTROL)].enumerated() {
            let t = UInt64(1000 + idx * 500)
            let downStep = eng.handleEventStep(kind: .flagsChanged, keyCode: kc, rawFlags: flags, isARepeat: false, nowMs: t, keyDownCounter: 10)
            try check(downStep.events.isEmpty && downStep.armTimers && downStep.hintDelayMs == 250 && downStep.cancelDelayMs == 800, "down \(idx)")
            let upStep = eng.handleEventStep(kind: .flagsChanged, keyCode: kc, rawFlags: 0, isARepeat: false, nowMs: t + 100, keyDownCounter: 10)
            try check(upStep.cancelTimers && !upStep.clearHoldPreview && upStep.events.count == 1 && upStep.events[0].kind == "toggle", "up \(idx)")
        }
        // Boundary 500ms exact -> toggle
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 3000, keyDownCounter: 10)
        let tap500 = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 3500, keyDownCounter: 10)
        try check(tap500.events.count == 1 && tap500.events[0].kind == "toggle", "tap 500ms boundary")
        hotkeyFsmChecks += 1

        // 4: LControl must NOT trigger RControl
        _ = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3B, rawFlags: FLAG_CONTROL | NX_DEVICELCTLKEYMASK, isARepeat: false, nowMs: 4000, keyDownCounter: 10)
        try check(eng.handleEvent(kind: .flagsChanged, keyCode: 0x3B, rawFlags: 0, isARepeat: false, nowMs: 4100, keyDownCounter: 10).isEmpty, "LControl isolation")
        hotkeyFsmChecks += 1

        // 5: FlagsChanged-only + counter increment suppresses tap_toggle AND emits ptt_cancel on PTT release
        _ = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 4500, keyDownCounter: 10)
        try check(eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 4620, keyDownCounter: 11).isEmpty, "counter chord tap suppression")

        var pttEng = HotkeyEngine(spec: rctrl, isPtt: true, tapMaxMs: 500, holdCancelMs: 800, holdHintMs: 250, debounceMs: 180, keyTest: false)
        let ps1 = pttEng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 5000, keyDownCounter: 20)
        let pc1 = pttEng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 5150, keyDownCounter: 21)
        try check(!ps1.armTimers && ps1.events.count == 1 && ps1.events[0].kind == "ptt_start" && pc1.events.count == 1 && pc1.events[0].kind == "ptt_cancel", "counter-only ptt_cancel")
        let ps2 = pttEng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 6000, keyDownCounter: 21)
        let pe2 = pttEng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 7500, keyDownCounter: 21)
        try check(!ps2.armTimers && ps2.events.count == 1 && ps2.events[0].kind == "ptt_start" && pe2.events.count == 1 && pe2.events[0].kind == "ptt_stop", "ptt >800ms release emits ptt_stop")
        hotkeyFsmChecks += 1

        // 6: Swallowed .keyUp recovery via isKeyPhysicallyDown physical state check
        _ = eng.handleEvent(kind: .keyDown, keyCode: 0x08, rawFlags: 0, isARepeat: false, nowMs: 8000, keyDownCounter: 30)
        try check(eng.otherKeysDown.contains(0x08), "otherKeysDown recorded")
        // Physical state reports key 0x08 is no longer held -> pruned, so subsequent RControl tap succeeds!
        _ = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 8500, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(eng.otherKeysDown.isEmpty, "stale key pruned")
        let recov = eng.handleEvent(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 8600, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(recov.count == 1 && recov[0].kind == "toggle", "recovered toggle after swallowed keyUp")
        hotkeyFsmChecks += 1

        // 7: Hesitation deadzone (501ms..799ms): 250ms preview shows, release restores preview and emits ZERO events
        let dzDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 9000, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        let dzHint = eng.onHoldHintTimeout(gen: dzDown.timerGen, nowMs: 9250, keyDownCounter: 30, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(dzHint.showHoldPreview && eng.holdHintShown, "250ms hold preview activates")
        let dzUp = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 9600, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(dzUp.cancelTimers && dzUp.clearHoldPreview && dzUp.events.isEmpty, "600ms deadzone silent & clears preview")
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 10000, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 10501, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false }).events.isEmpty, "501ms deadzone silent")
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 11000, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 11799, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false }).events.isEmpty, "799ms deadzone silent")
        hotkeyFsmChecks += 1

        // 8: Real-time 800ms hold_cancel BEFORE keyup + keyup swallowed even after >8000ms + stale gen ignored
        let hcDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 12000, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        let hcHint = eng.onHoldHintTimeout(gen: hcDown.timerGen, nowMs: 12250, keyDownCounter: 30, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(hcHint.showHoldPreview, "hold hint before real-time cancel")
        let hcFire = eng.onHoldCancelTimeout(gen: hcDown.timerGen, nowMs: 12800, keyDownCounter: 30, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(eng.holdFired && hcFire.armPreviewSafetyClear && hcFire.events.count == 1 && hcFire.events[0].kind == "hold_cancel" && hcFire.events[0].holdMs == 800, "real-time 800ms hold_cancel")
        let hcStale = eng.onHoldCancelTimeout(gen: hcDown.timerGen, nowMs: 12900, keyDownCounter: 30, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(hcStale.events.isEmpty, "stale hold timer ignored")
        let hcUpLate = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 24000, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(hcUpLate.events.isEmpty && !eng.targetDown && !eng.holdFired, "post-hold_cancel keyup after 12s swallowed")
        hotkeyFsmChecks += 1

        // 9: Timer jitter fallback: release at 805ms before timer tick still emits hold_cancel once
        let jitDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 25000, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        _ = eng.onHoldHintTimeout(gen: jitDown.timerGen, nowMs: 25250, keyDownCounter: 30, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        let jitUp = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 25805, keyDownCounter: 30, isKeyPhysicallyDown: { _ in false })
        try check(jitUp.events.count == 1 && jitUp.events[0].kind == "hold_cancel" && jitUp.events[0].holdMs == 805 && jitUp.armPreviewSafetyClear, "jitter fallback hold_cancel")
        hotkeyFsmChecks += 1

        // 10: No Input Monitoring timer callback secondary check (keyDownCounter increment + other modifier + lost release)
        let nmDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 27000, keyDownCounter: 40, isKeyPhysicallyDown: { _ in false })
        let nmHintBlocked = eng.onHoldHintTimeout(gen: nmDown.timerGen, nowMs: 27250, keyDownCounter: 41, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(!nmHintBlocked.showHoldPreview && eng.chordTainted, "timer hint recheck catches counter increment")
        let nmCancelBlocked = eng.onHoldCancelTimeout(gen: nmDown.timerGen, nowMs: 27800, keyDownCounter: 41, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(nmCancelBlocked.events.isEmpty, "timer cancel blocked after counter taint")
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 27900, keyDownCounter: 41, isKeyPhysicallyDown: { _ in false })

        // Mid-preview counter taint at 800ms clears preview & blocks cancel
        let mpDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 29000, keyDownCounter: 50, isKeyPhysicallyDown: { _ in false })
        let mpHint = eng.onHoldHintTimeout(gen: mpDown.timerGen, nowMs: 29250, keyDownCounter: 50, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(mpHint.showHoldPreview, "mid-preview hint shown")
        let mpCancel = eng.onHoldCancelTimeout(gen: mpDown.timerGen, nowMs: 29800, keyDownCounter: 51, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false })
        try check(mpCancel.events.isEmpty && mpCancel.clearHoldPreview && eng.chordTainted, "800ms counter recheck clears preview & blocks cancel")
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 29900, keyDownCounter: 51, isKeyPhysicallyDown: { _ in false })

        // Other modifier (Shift) or target no longer physically down in timer callback
        let modDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 31000, keyDownCounter: 60, isKeyPhysicallyDown: { _ in false })
        let modCancel = eng.onHoldCancelTimeout(gen: modDown.timerGen, nowMs: 31800, keyDownCounter: 60, rawFlags: FLAG_CONTROL | FLAG_SHIFT, isKeyPhysicallyDown: { _ in false })
        try check(modCancel.events.isEmpty && eng.chordTainted, "timer cancel catches other modifier flags")
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 31900, keyDownCounter: 60, isKeyPhysicallyDown: { _ in false })

        let lostDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 33000, keyDownCounter: 70, isKeyPhysicallyDown: { _ in false })
        let lostCheck = eng.onHoldHintTimeout(gen: lostDown.timerGen, nowMs: 33250, keyDownCounter: 70, rawFlags: 0, isKeyPhysicallyDown: { _ in false })
        try check(!lostCheck.showHoldPreview && !eng.targetDown, "timer recovers stuck targetDown when rawFlags=0")
        hotkeyFsmChecks += 1

        // 11: holdCancelMs = 0 disables hold timers cleanly; tight threshold keeps holdHintMs strictly < holdCancelMs
        var noHoldEng = HotkeyEngine(spec: rctrl, isPtt: false, tapMaxMs: 500, holdCancelMs: 0, holdHintMs: 250, debounceMs: 180, keyTest: false)
        try check(noHoldEng.holdCancelMs == 0 && noHoldEng.holdHintMs == 0, "holdCancelMs=0 disables timers")
        let nhDown = noHoldEng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 1000, keyDownCounter: 1)
        try check(!nhDown.armTimers, "holdCancelMs=0 does not arm timers")
        try check(noHoldEng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 2500, keyDownCounter: 1).events.isEmpty, "holdCancelMs=0 ignores long release")
        let tightEng = HotkeyEngine(spec: rctrl, isPtt: false, tapMaxMs: 50, holdCancelMs: 80, holdHintMs: 250, debounceMs: 0, keyTest: false)
        try check(tightEng.tapMaxMs == 50 && tightEng.holdCancelMs == 80 && tightEng.holdHintMs == 40 && tightEng.holdHintMs < tightEng.holdCancelMs, "tight holdHintMs strictly < holdCancelMs")
        hotkeyFsmChecks += 1

        // 12: No Input Monitoring pre-held regular key before pressing modifier hotkey -> immediately taints & blocks both hold_cancel and toggle
        let preKeyDown = eng.handleEventStep(
            kind: .flagsChanged,
            keyCode: 0x3E,
            rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK,
            isARepeat: false,
            nowMs: 35000,
            keyDownCounter: 80,
            isKeyPhysicallyDown: { _ in false },
            hasOtherNonModifierKeyDown: { true }
        )
        try check(eng.chordTainted && !preKeyDown.armTimers && preKeyDown.events.isEmpty, "pre-held normal key taints modifier down")
        try check(
            !eng.onHoldHintTimeout(
                gen: preKeyDown.timerGen,
                nowMs: 35250,
                keyDownCounter: 80,
                rawFlags: FLAG_CONTROL,
                isKeyPhysicallyDown: { _ in false },
                hasOtherNonModifierKeyDown: { true }
            ).showHoldPreview,
            "pre-held normal key blocks hint"
        )
        try check(
            eng.onHoldCancelTimeout(
                gen: preKeyDown.timerGen,
                nowMs: 35800,
                keyDownCounter: 80,
                rawFlags: FLAG_CONTROL,
                isKeyPhysicallyDown: { _ in false },
                hasOtherNonModifierKeyDown: { true }
            ).events.isEmpty,
            "pre-held normal key blocks hold_cancel"
        )
        try check(
            eng.handleEventStep(
                kind: .flagsChanged,
                keyCode: 0x3E,
                rawFlags: 0,
                isARepeat: false,
                nowMs: 35900,
                keyDownCounter: 80,
                isKeyPhysicallyDown: { _ in false },
                hasOtherNonModifierKeyDown: { false }
            ).events.isEmpty,
            "pre-held normal key release swallowed"
        )
        // Normal key detected physically down at 800ms timer callback clears preview and blocks hold_cancel
        let midScanDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 37000, keyDownCounter: 80, isKeyPhysicallyDown: { _ in false }, hasOtherNonModifierKeyDown: { false })
        let midScanHint = eng.onHoldHintTimeout(gen: midScanDown.timerGen, nowMs: 37250, keyDownCounter: 80, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false }, hasOtherNonModifierKeyDown: { false })
        try check(midScanHint.showHoldPreview, "mid-scan preview shown")
        let midScanCancel = eng.onHoldCancelTimeout(gen: midScanDown.timerGen, nowMs: 37800, keyDownCounter: 80, rawFlags: FLAG_CONTROL, isKeyPhysicallyDown: { _ in false }, hasOtherNonModifierKeyDown: { true })
        try check(midScanCancel.events.isEmpty && midScanCancel.clearHoldPreview && eng.chordTainted, "mid-scan physical normal key clears preview & blocks hold_cancel")
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 37900, keyDownCounter: 80, isKeyPhysicallyDown: { _ in false }, hasOtherNonModifierKeyDown: { false })
        hotkeyFsmChecks += 1

        // 13: Power resume reset clears stuck hold/preview/otherKeysDown/PTT and debounces duplicate wake events
        let wakeDown = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 40000, keyDownCounter: 90)
        let wakeHint = eng.onHoldHintTimeout(gen: wakeDown.timerGen, nowMs: 40260, keyDownCounter: 90, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK)
        try check(eng.targetDown && eng.holdHintShown && wakeHint.showHoldPreview, "power_resume setup hold preview")
        _ = eng.handleEventStep(kind: .keyDown, keyCode: 0x00, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 40300, keyDownCounter: 91)
        try check(!eng.otherKeysDown.isEmpty, "power_resume setup otherKeysDown")
        let wakeReset = eng.resetForPowerResume()
        try check(
            !eng.targetDown && !eng.pttActive && !eng.chordTainted && !eng.holdFired && !eng.holdHintShown && eng.otherKeysDown.isEmpty,
            "power_resume clears all stuck engine state"
        )
        try check(wakeReset.cancelTimers && wakeReset.clearHoldPreview, "power_resume cancels timers and clears preview")
        try check(
            eng.onHoldCancelTimeout(gen: wakeDown.timerGen, nowMs: 41000, keyDownCounter: 91, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK).events.isEmpty,
            "power_resume invalidates stale pre-sleep timer gen"
        )
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 42000, keyDownCounter: 92)
        let wakeTapUp = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 42120, keyDownCounter: 92)
        try check(wakeTapUp.events.count == 1 && wakeTapUp.events[0].kind == "toggle", "post-resume fresh tap emits toggle")
        var wakePtt = HotkeyEngine(spec: rctrl, isPtt: true, tapMaxMs: 500, holdCancelMs: 800, holdHintMs: 250, debounceMs: 180, keyTest: false)
        _ = wakePtt.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 1000, keyDownCounter: 1)
        try check(wakePtt.pttActive && wakePtt.targetDown, "power_resume ptt setup")
        let wakePttReset = wakePtt.resetForPowerResume()
        try check(!wakePtt.pttActive && !wakePtt.targetDown && wakePttReset.cancelTimers && wakePttReset.clearHoldPreview, "power_resume clears ptt state")
        try check(wakePttReset.events.count == 1 && wakePttReset.events[0].kind == "ptt_stop", "power_resume emits exactly one ptt_stop for active PTT")
        let wakePttUp = wakePtt.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 1400, keyDownCounter: 1)
        try check(wakePttUp.events.isEmpty, "post-resume PTT key-up must not re-emit ptt_stop")
        let wakePttIdleReset = wakePtt.resetForPowerResume()
        try check(wakePttIdleReset.events.isEmpty, "idle PTT resume emits no ptt_stop")
        // Debounce gate runs BEFORE reset so a duplicate wake never wipes a fresh press between wake 1 and wake 2
        var wakeDebouncer = PowerResumeDebouncer(debounceMs: 2000)
        try check(wakeDebouncer.shouldEmit(nowMs: 50000), "power_resume debouncer allows wake 1")
        _ = eng.resetForPowerResume()
        _ = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 50050, keyDownCounter: 93)
        try check(eng.targetDown, "fresh key-down after wake 1 is active")
        if wakeDebouncer.shouldEmit(nowMs: 50100) {
            _ = eng.resetForPowerResume()
            try check(false, "power_resume debouncer must suppress wake 2 within 100ms")
        }
        try check(eng.targetDown, "debounced wake 2 must not wipe active key-down")
        let wake2Up = eng.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 50180, keyDownCounter: 93)
        try check(wake2Up.events.count == 1 && wake2Up.events[0].kind == "toggle", "key pressed across debounced wake 2 still emits toggle")
        try check(!wakeDebouncer.shouldEmit(nowMs: 51999), "power_resume debouncer suppresses 1999ms duplicate")
        try check(wakeDebouncer.shouldEmit(nowMs: 52000), "power_resume debouncer allows after window")
        hotkeyFsmChecks += 1

        try check(isZhUiCulture("zh-Hans-CN") && isZhUiCulture("zh_TW.UTF-8") && !isZhUiCulture("en-US"), "ui_culture zh detection")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: "bottom") == 58.0, "HUD bottom pillY")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: "") == 58.0, "HUD default pillY")
        try check(computeHudPillY(visibleMinY: 50.0, visibleMaxY: 900.0, pillH: 36.0, position: " TOP ") == 850.0, "HUD top pillY")
        try check(computeHudToastY(visibleMinY: 50.0, visibleMaxY: 900.0, mainH: 36.0, toastH: 32.0, position: "bottom") == 102.0, "HUD bottom toastY")
        try check(computeHudToastY(visibleMinY: 50.0, visibleMaxY: 900.0, mainH: 0.0, toastH: 32.0, position: "") == 102.0, "HUD bottom toastY min mainH")
        try check(computeHudToastY(visibleMinY: 50.0, visibleMaxY: 900.0, mainH: 36.0, toastH: 32.0, position: " TOP ") == 810.0, "HUD top toastY")

        try check(stripLeadingHudSymbol("! 麦克风暂忙") == "麦克风暂忙", "stripLeadingHudSymbol bang")
        try check(stripLeadingHudSymbol("⚠ 麦克风暂忙") == "麦克风暂忙", "stripLeadingHudSymbol warn")
        try check(stripLeadingHudSymbol("⚠️ 麦克风暂忙") == "麦克风暂忙", "stripLeadingHudSymbol warn vs16")
        try check(stripLeadingHudSymbol("✕ 无可用麦克风") == "无可用麦克风", "stripLeadingHudSymbol cross")

        let stBusy = resolveHudVisualStyle(state: "mic_unavailable", zh: true)
        let stNoStream = resolveHudVisualStyle(state: "mic_no_stream", zh: true)
        let stRecov = resolveHudVisualStyle(state: "audio_recovered", zh: true)
        let stEmpty = resolveHudVisualStyle(state: "empty", zh: true)
        let stCancelled = resolveHudVisualStyle(state: "cancelled", zh: true)
        let stNoMic = resolveHudVisualStyle(state: "no_mic", zh: true)
        let stPerm = resolveHudVisualStyle(state: "mic_permission", zh: true)
        let stPaste = resolveHudVisualStyle(state: "paste_failed", zh: true)
        let stRecog = resolveHudVisualStyle(state: "recognizing", zh: true)
        let stCopiedMain = resolveHudVisualStyle(state: "copied", zh: true)
        let stCopiedToast = resolveHudVisualStyle(state: "copied", explicitLayer: "toast", zh: true)
        try check(stBusy.layer == "toast" && stBusy.iconKind == "warning" && stBusy.borderWidth == 1.0, "style mic_unavailable")
        try check(
            stNoStream.layer == "toast"
                && stNoStream.iconKind == "warning"
                && stNoStream.autoHideMs == 2600
                && stNoStream.bg == stBusy.bg
                && stNoStream.border == stBusy.border
                && stNoStream.defaultText == "! 麦克风无音频流，请检查耳机/设备",
            "style mic_no_stream"
        )
        try check(stRecov.layer == "toast" && stRecov.iconKind == "warning" && stRecov.bg == stBusy.bg && stRecov.border == stBusy.border, "style audio_recovered")
        try check(stEmpty.layer == "toast" && stEmpty.iconKind == "empty_ring" && stEmpty.border == stBusy.border, "style empty")
        try check(stCancelled.layer == "toast" && stCancelled.iconKind == "empty_ring" && stCancelled.autoHideMs == 1600 && stCancelled.defaultText == "○ 已取消", "style cancelled")
        try check(stNoMic.layer == "toast" && stNoMic.iconKind == "cross" && stNoMic.bg == stBusy.bg && stNoMic.border != stBusy.border, "style no_mic")
        try check(stPerm.layer == "toast" && stPerm.iconKind == "cross" && stPerm.autoHideMs == 3200, "style mic_permission")
        try check(stPaste.layer == "toast" && stPaste.iconKind == "cross" && stPaste.border == stNoMic.border, "style paste_failed")
        try check(stRecog.layer == "main" && stRecog.iconKind == "recognizing", "style recognizing")
        try check(stCopiedMain.layer == "main" && stCopiedMain.iconKind == "done" && stCopiedMain.bg != stBusy.bg, "style copied main")
        try check(
            stCopiedToast.layer == "toast"
                && stCopiedToast.iconKind == "done"
                && stCopiedToast.bg == stBusy.bg
                && stCopiedToast.mainText == stBusy.mainText
                && stCopiedToast.hintText == stBusy.hintText
                && stCopiedToast.border == stCopiedMain.border
                && stCopiedToast.icon == stCopiedMain.icon,
            "style copied toast"
        )

        try check(splitHudSegments("● 录音中… (按右 Ctrl 结束)") == HudSegments(icon: "●", main: "录音中…", hint: "(按右 Ctrl 结束)", hasSpaceBeforeHint: true), "HUD split recording zh")
        try check(stripOuterParens("(按右 Ctrl 结束)") == "按右 Ctrl 结束" && stripOuterParens("12s 后自动完成") == "12s 后自动完成" && stripOuterParens("") == "", "HUD strip outer parens")
        try check(splitHudSegments("✓ 已复制(窗口已切换)") == HudSegments(icon: "✓", main: "已复制", hint: "(窗口已切换)", hasSpaceBeforeHint: false), "HUD split copied zh")
        try check(splitHudSegments("Custom HUD") == HudSegments(icon: "", main: "Custom HUD", hint: "", hasSpaceBeforeHint: false), "HUD split custom")
        try check(formatRecordingDuration(-5) == "0s" && formatRecordingDuration(0) == "0s" && formatRecordingDuration(9) == "9s" && formatRecordingDuration(59) == "59s", "HUD duration seconds")
        try check(formatRecordingDuration(60) == "1m 00s" && formatRecordingDuration(65) == "1m 05s" && formatRecordingDuration(605) == "10m 05s", "HUD duration minutes")
        try check(durationSlotTemplate(0) == "8s" && durationSlotTemplate(59) == "88s" && durationSlotTemplate(60) == "8m 88s" && durationSlotTemplate(600) == "88m 88s", "HUD duration slot template")
        try check(recordingCountdownWarnSec(0) == 0 && recordingCountdownWarnSec(600) == 15 && recordingCountdownWarnSec(45) == 15 && recordingCountdownWarnSec(30) == 10 && recordingCountdownWarnSec(9) == 3 && recordingCountdownWarnSec(2) == 1, "HUD countdown warn sec")
        try check(formatCountdownHint(12, zh: true) == "(12s 后自动完成)" && formatCountdownHint(5, zh: false) == "(auto-finish in 5s)", "HUD countdown hint")
        try check(countdownHintSlotTemplate(12, zh: true) == "(88s 后自动完成)" && countdownHintSlotTemplate(9, zh: false) == "(auto-finish in 8s)", "HUD countdown slot template")

        let audioSt = queryAudioInputStatus()
        let validMicAuths: Set<String> = ["authorized", "denied", "restricted", "not_determined", "unknown"]
        guard let halOk = audioSt["hal_ok"] as? Bool,
              let micAuth = audioSt["mic_auth"] as? String,
              !micAuth.isEmpty,
              validMicAuths.contains(micAuth),
              let hasInput = audioSt["has_input"] as? Bool,
              let inputCount = audioSt["input_count"] as? Int,
              let defaultUid = audioSt["default_uid"] as? String,
              let _ = audioSt["default_name"] as? String,
              let aliveUids = audioSt["alive_uids"] as? [String] else {
            throw NSError(domain: "SelfCheck", code: 2, userInfo: [NSLocalizedDescriptionKey: "invalid queryAudioInputStatus structure"])
        }
        _ = halOk
        try check(aliveUids.count == inputCount && (inputCount > 0) == hasInput, "audio_status count invariant")
        try check(defaultUid.isEmpty || aliveUids.contains(defaultUid), "audio_status default_uid invariant")

        // Isolated NSPasteboard single-slot clipboard guard self-check (never touches NSPasteboard.general)
        do {
            try check(PasteboardGuard.defaultMaxSnapshotBytes == 100 * 1024 * 1024, "pb defaultMaxSnapshotBytes 100MiB")
            let testPb = NSPasteboard.withUniqueName()
            defer { testPb.releaseGlobally() }
            let pbg = PasteboardGuard(pasteboard: testPb, restoreDelayMs: 40, maxSnapshotBytes: 4096)
            let samplePng = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x01, 0x02])
            let sampleHtml = Data("<b>clip</b>".utf8)

            // 1) Composite PNG + HTML + plain text snapshot + transient markers + 50ms rapid-fire #1 + #2 preserving Image0
            testPb.clearContents()
            let seedItem = NSPasteboardItem()
            _ = seedItem.setData(samplePng, forType: .png)
            _ = seedItem.setData(sampleHtml, forType: .html)
            _ = seedItem.setString("orig_caption", forType: .string)
            try check(testPb.writeObjects([seedItem]), "pb seed composite")
            try check(pbg.writeTransientAndArm("voice_1"), "pb transient #1")
            try check(pbg.restorePending && testPb.string(forType: .string) == "voice_1", "pb #1 armed")
            let p1Types = testPb.pasteboardItems?.first?.types ?? []
            try check(p1Types.contains(PasteboardGuard.transientType) && p1Types.contains(PasteboardGuard.autoGeneratedType), "pb #1 transient markers")
            try check(pbg.writeTransientAndArm("voice_2"), "pb transient #2 rapid-fire")
            try check(pbg.restorePending && testPb.string(forType: .string) == "voice_2", "pb #2 armed")
            pbg.flushPendingRestore()
            try check(!pbg.restorePending, "pb flushed idle")
            try check(testPb.data(forType: .png) == samplePng && testPb.data(forType: .html) == sampleHtml && testPb.string(forType: .string) == "orig_caption", "pb restored composite Image0")

            // 2) Manual Cmd+C during pending window must NEVER be overwritten by flush OR re-baselined away by #2
            try check(pbg.writeTransientAndArm("voice_3"), "pb transient #3")
            testPb.clearContents()
            _ = testPb.setString("user_cmd_c_new", forType: .string)
            pbg.flushPendingRestore()
            try check(testPb.string(forType: .string) == "user_cmd_c_new" && testPb.data(forType: .png) == nil, "pb preserved user Cmd+C on flush")

            try check(pbg.writeTransientAndArm("voice_4a"), "pb transient #4a")
            testPb.clearContents()
            _ = testPb.setString("user_between_bursts", forType: .string)
            try check(pbg.writeTransientAndArm("voice_4b"), "pb transient #4b")
            pbg.flushPendingRestore()
            try check(testPb.string(forType: .string) == "user_between_bursts", "pb re-baselined user copy between bursts")

            // 3) Focus-drift persistent fallback cancels pending timer and omits TransientType
            testPb.clearContents()
            let seedImg2 = NSPasteboardItem()
            _ = seedImg2.setData(samplePng, forType: .png)
            _ = testPb.writeObjects([seedImg2])
            try check(pbg.writeTransientAndArm("voice_5"), "pb transient #5")
            try check(pbg.writePersistentFallback("fallback_keep"), "pb persistent fallback")
            let fbTypes = testPb.pasteboardItems?.first?.types ?? []
            try check(!pbg.restorePending && !fbTypes.contains(PasteboardGuard.transientType), "pb fallback no transient marker")
            pbg.flushPendingRestore()
            try check(testPb.string(forType: .string) == "fallback_keep", "pb fallback retained")

            // 4) ConcealedType & external TransientType -> ClearOnly (never retains secret/transient) & oversize burst skip
            testPb.clearContents()
            let pwItem = NSPasteboardItem()
            _ = pwItem.setString("top_secret_pw", forType: .string)
            _ = pwItem.setData(Data(), forType: PasteboardGuard.concealedType)
            _ = testPb.writeObjects([pwItem])
            try check(pbg.writeTransientAndArm("voice_6"), "pb transient over concealed")
            pbg.flushPendingRestore()
            try check((testPb.pasteboardItems ?? []).isEmpty && testPb.string(forType: .string) == nil, "pb concealed cleared")

            testPb.clearContents()
            let extTrItem = NSPasteboardItem()
            _ = extTrItem.setString("external_ephemeral_snippet", forType: .string)
            _ = extTrItem.setData(Data(), forType: PasteboardGuard.transientType)
            _ = testPb.writeObjects([extTrItem])
            try check(pbg.writeTransientAndArm("voice_6b"), "pb transient over external transient")
            pbg.flushPendingRestore()
            try check((testPb.pasteboardItems ?? []).isEmpty && testPb.string(forType: .string) == nil, "pb external transient cleared")

            testPb.clearContents()
            let bigItem = NSPasteboardItem()
            _ = bigItem.setData(Data(repeating: 0xAA, count: 8192), forType: .png)
            _ = testPb.writeObjects([bigItem])
            try check(pbg.writeTransientAndArm("voice_oversize"), "pb transient over oversize")
            try check(!pbg.restorePending && testPb.string(forType: .string) == "voice_oversize", "pb oversize skips restore arm")
            try check(pbg.writeTransientAndArm("voice_oversize_2"), "pb transient #2 after oversize")
            try check(!pbg.restorePending && testPb.string(forType: .string) == "voice_oversize_2", "pb oversize burst never snapshots voice_oversize")
            pbg.flushPendingRestore()
            try check(testPb.string(forType: .string) == "voice_oversize_2", "pb oversize #2 retained")

            // 5) PNG + oversize/small TIFF coexistence deduplicates TIFF (only snapshots PNG, avoiding double bitmap quota), while TIFF-only falls back cleanly
            testPb.clearContents()
            let dualImgItem = NSPasteboardItem()
            let oversizeTiff = Data(repeating: 0xBB, count: 8192)
            let okDualPng = dualImgItem.setData(samplePng, forType: .png)
            let okDualTiff = dualImgItem.setData(oversizeTiff, forType: .tiff)
            try check(okDualPng && okDualTiff && testPb.writeObjects([dualImgItem]), "pb seed PNG+TIFF")
            let dualSeedTypes = testPb.pasteboardItems?.first?.types ?? []
            try check(dualSeedTypes.contains(.png) && dualSeedTypes.contains(.tiff), "pb seed contains both PNG and TIFF")
            try check(pbg.writeTransientAndArm("voice_dual_img"), "pb transient over PNG+TIFF")
            try check(pbg.restorePending, "pb PNG+TIFF armed without double-counting oversize TIFF")
            pbg.flushPendingRestore()
            // AppKit NSPasteboard may implicitly expose a translated TIFF flavor when PNG is present;
            // verify exact PNG restoration and that our seeded TIFF payloads were never snapshotted back.
            let restoredDualTiff = testPb.data(forType: .tiff)
            try check(!pbg.restorePending && testPb.data(forType: .png) == samplePng && restoredDualTiff != oversizeTiff, "pb PNG restored without TIFF duplication")

            let tinySentinelTiff = Data([0x49, 0x49, 0x2A, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])
            testPb.clearContents()
            let dualSmallItem = NSPasteboardItem()
            try check(dualSmallItem.setData(samplePng, forType: .png) && dualSmallItem.setData(tinySentinelTiff, forType: .tiff) && testPb.writeObjects([dualSmallItem]), "pb seed PNG+small TIFF")
            try check(pbg.writeTransientAndArm("voice_dual_small"), "pb transient over PNG+small TIFF")
            pbg.flushPendingRestore()
            try check(!pbg.restorePending && testPb.data(forType: .png) == samplePng && testPb.data(forType: .tiff) != tinySentinelTiff, "pb PNG+small TIFF skipped TIFF capture")

            let sampleTiff = Data([0x49, 0x49, 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00])
            testPb.clearContents()
            let tiffOnlyItem = NSPasteboardItem()
            try check(tiffOnlyItem.setData(sampleTiff, forType: .tiff) && testPb.writeObjects([tiffOnlyItem]), "pb seed TIFF-only")
            try check(pbg.writeTransientAndArm("voice_tiff_only"), "pb transient over TIFF-only")
            pbg.flushPendingRestore()
            try check(!pbg.restorePending && testPb.data(forType: .tiff) == sampleTiff, "pb TIFF-only fallback restored")
        }

        let hudLayoutCases = try runHudLayoutSelfCheck()

        #if arch(arm64)
        let archName = "arm64"
        #elseif arch(x86_64)
        let archName = "x86_64"
        #else
        let archName = "unknown"
        #endif
        writeJsonLine(["ok": true, "mode": "self-check", "arch": archName, "rcontrol_vk": String(format: "0x%02X", rctrl.primaryKeyCode), "rcontrol_mask": String(format: "0x%04X", rctrl.deviceMask), "ui_culture": hostUiCulture(), "ax_trusted": AXIsProcessTrusted(), "listen_event_access": CGPreflightListenEventAccess(), "hud_layout_cases": hudLayoutCases, "hotkey_fsm_checks": hotkeyFsmChecks, "audio_status": audioSt])
        return 0
    } catch {
        fputs("self-check error: \(error.localizedDescription)\n", stderr)
        return 1
    }
}

@main
struct MacShimMain {
    static func main() {
        var (mode, hotkey, triggerMode, tapMaxMs, holdCancelMs, holdHintMs, debounceMs, seconds) = ("bridge", "RControl", "tap_toggle", 500, 800, 250, 180, 10)
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
            case "--hold-cancel-ms", "-holdcancelms": if let v = nextVal, let n = Int(v) { holdCancelMs = n; i += 2 } else { i += 1 }
            case "--hold-hint-ms", "-holdhintms": if let v = nextVal, let n = Int(v) { holdHintMs = n; i += 2 } else { i += 1 }
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
        let audioMonitor = CoreAudioInputMonitor()
        let pasteboardGuard = PasteboardGuard()
        var engine = HotkeyEngine(
            spec: spec,
            isPtt: isPtt,
            tapMaxMs: tapMaxMs,
            holdCancelMs: holdCancelMs,
            holdHintMs: holdHintMs,
            debounceMs: debounceMs,
            keyTest: isKeyTest
        )
        var globalMonitor: Any?
        var wakeObserver: NSObjectProtocol?
        var powerResumeDebouncer = PowerResumeDebouncer()
        var holdHintWorkItem: DispatchWorkItem?
        var holdCancelWorkItem: DispatchWorkItem?

        let cancelHoldWorkItems: () -> Void = {
            holdHintWorkItem?.cancel(); holdHintWorkItem = nil
            holdCancelWorkItem?.cancel(); holdCancelWorkItem = nil
        }

        let cleanupAndExit: (Int32) -> Never = { code in
            cancelHoldWorkItems()
            if let wo = wakeObserver { NSWorkspace.shared.notificationCenter.removeObserver(wo); wakeObserver = nil }
            if let gm = globalMonitor { NSEvent.removeMonitor(gm); globalMonitor = nil }
            pasteboardGuard.flushPendingRestore()
            audioMonitor.stop()
            hud.close()
            Foundation.exit(code)
        }

        let emitStepEvents: ([EmittedEvent], UInt64) -> Void = { emitted, now in
            for item in emitted {
                if isKeyTest {
                    let kname = KEY_CODE_NAMES[item.vk] ?? String(format: "VK_0x%02X", item.vk)
                    var ktPayload: [String: Any] = ["event": item.kind, "vk": String(format: "0x%02X", item.vk), "key_name": kname, "flags": String(format: "0x%X", item.flags), "is_down": item.isDown, "tainted": item.tainted]
                    if item.kind == "hold_cancel" { ktPayload["hold_ms"] = item.holdMs }
                    writeJsonLine(ktPayload)
                } else {
                    let fg = queryFrontmostApp()
                    var payload: [String: Any] = ["event": item.kind, "hwnd": fg.hwnd, "process": fg.name, "bundle_id": fg.bundleId, "pid": fg.pid, "title": fg.title, "document": fg.document, "ax_trusted": fg.axTrusted, "ts_ms": now]
                    if item.kind == "hold_cancel" { payload["hold_ms"] = item.holdMs }
                    writeJsonLine(payload)
                }
            }
        }

        let checkOtherNonModDown: () -> Bool = {
            spec.isModifier ? hasAnyNonModifierKeyPhysicallyDown(excluding: spec) : false
        }

        func applyStep(_ step: HotkeyStepResult, now: UInt64) {
            if step.cancelTimers {
                cancelHoldWorkItems()
            }
            if step.armTimers {
                cancelHoldWorkItems()
                let gen = step.timerGen
                if step.hintDelayMs > 0 {
                    let hw = DispatchWorkItem {
                        if engine.holdGen == gen { holdHintWorkItem = nil }
                        let tNow = monotonicMs()
                        let kdCount = CGEventSource.counterForEventType(.combinedSessionState, eventType: .keyDown)
                        let liveFlags = UInt(truncatingIfNeeded: CGEventSource.flagsState(.combinedSessionState).rawValue)
                        let hStep = engine.onHoldHintTimeout(
                            gen: gen,
                            nowMs: tNow,
                            keyDownCounter: kdCount,
                            rawFlags: liveFlags,
                            isKeyPhysicallyDown: { CGEventSource.keyState(.combinedSessionState, key: CGKeyCode($0)) },
                            hasOtherNonModifierKeyDown: checkOtherNonModDown
                        )
                        applyStep(hStep, now: tNow)
                    }
                    holdHintWorkItem = hw
                    DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(step.hintDelayMs), execute: hw)
                }
                if step.cancelDelayMs > 0 {
                    let cw = DispatchWorkItem {
                        if engine.holdGen == gen { holdCancelWorkItem = nil }
                        let tNow = monotonicMs()
                        let kdCount = CGEventSource.counterForEventType(.combinedSessionState, eventType: .keyDown)
                        let liveFlags = UInt(truncatingIfNeeded: CGEventSource.flagsState(.combinedSessionState).rawValue)
                        let cStep = engine.onHoldCancelTimeout(
                            gen: gen,
                            nowMs: tNow,
                            keyDownCounter: kdCount,
                            rawFlags: liveFlags,
                            isKeyPhysicallyDown: { CGEventSource.keyState(.combinedSessionState, key: CGKeyCode($0)) },
                            hasOtherNonModifierKeyDown: checkOtherNonModDown
                        )
                        applyStep(cStep, now: tNow)
                    }
                    holdCancelWorkItem = cw
                    DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(step.cancelDelayMs), execute: cw)
                }
            }
            if !isKeyTest {
                if step.clearHoldPreview {
                    hud.setHoldCancelPreview(false)
                } else if step.showHoldPreview {
                    hud.setHoldCancelPreview(true)
                } else if step.armPreviewSafetyClear {
                    hud.scheduleHoldPreviewSafetyClear(delayMs: 400)
                }
            }
            if !step.events.isEmpty {
                emitStepEvents(step.events, now)
            }
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
            if ev.cgEvent?.getIntegerValueField(.eventSourceUserData) == AVI_INJECT_TAG { return }
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
            let step = engine.handleEventStep(
                kind: rawKind,
                keyCode: ev.keyCode,
                rawFlags: ev.modifierFlags.rawValue,
                isARepeat: (rawKind == .keyDown) ? ev.isARepeat : false,
                nowMs: now,
                keyDownCounter: kdCount,
                isKeyPhysicallyDown: { CGEventSource.keyState(.combinedSessionState, key: CGKeyCode($0)) },
                hasOtherNonModifierKeyDown: checkOtherNonModDown
            )
            applyStep(step, now: now)
        }

        if isKeyTest {
            writeJsonLine(["event": "key_test_ready", "hotkey": hotkey, "target_vk": String(format: "0x%02X", spec.primaryKeyCode), "mode": triggerMode, "seconds": seconds, "tap_max_ms": tapMaxMs, "hold_cancel_ms": holdCancelMs, "hold_hint_ms": holdHintMs, "listen_access": hasListenAccess])
            DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(seconds)) { cleanupAndExit(0) }
            app.run(); cleanupAndExit(0)
        }

        let initialAudioStatus = audioMonitor.start()
        wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification,
            object: nil,
            queue: .main
        ) { _ in
            let now = monotonicMs()
            guard powerResumeDebouncer.shouldEmit(nowMs: now) else { return }
            let resetStep = engine.resetForPowerResume()
            applyStep(resetStep, now: now)
            writeJsonLine(["event": "power_resume", "ts_ms": now])
        }
        var postAccessPrompted = false

        let stdinThread = Thread {
            while let rawLine = readLine(strippingNewline: true) {
                let trimmed = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed.isEmpty { continue }
                let enqueuedMs = monotonicMs()
                DispatchQueue.main.async {
                    guard let data = trimmed.data(using: .utf8),
                          let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
                    let op = (obj["cmd"] as? String) ?? "unknown"
                    let seqId = (obj["seq_id"] as? Int) ?? ((obj["seq_id"] as? NSNumber)?.intValue ?? 0)
                    switch op {
                    case "fg_window":
                        let fg = queryFrontmostApp()
                        writeJsonLine(["type": "reply", "reply": "fg_window", "seq_id": seqId, "ok": true, "hwnd": fg.hwnd, "process": fg.name, "bundle_id": fg.bundleId, "pid": fg.pid, "title": fg.title, "document": fg.document, "ax_trusted": fg.axTrusted])
                    case "audio_status":
                        writeJsonLine(["type": "reply", "reply": true, "seq_id": seqId, "ok": true, "audio_status": queryAudioInputStatus()])
                    case "key_state":
                        let modFlags = CGEventSource.flagsState(.combinedSessionState)
                        let blockingMods: CGEventFlags = [.maskShift, .maskControl, .maskAlternate, .maskCommand]
                        let fnHeld = (spec.highFlag == FLAG_FUNCTION) && modFlags.contains(.maskSecondaryFn)
                        let nonModHeld = !spec.isModifier && (
                            CGEventSource.keyState(.combinedSessionState, key: CGKeyCode(spec.primaryKeyCode))
                            || spec.altKeyCodes.contains(where: { CGEventSource.keyState(.combinedSessionState, key: CGKeyCode($0)) })
                        )
                        let held = engine.targetDown || !modFlags.intersection(blockingMods).isEmpty || fnHeld || nonModHeld
                        writeJsonLine(["type": "reply", "reply": "key_state", "seq_id": seqId, "ok": true, "hotkey_held": held])
                    case "paste_cmd_v":
                        let pasteTextOpt = obj["text"] as? String
                        let fbText: String = {
                            if let rawFb = obj["fallback_text"] as? String, !rawFb.isEmpty {
                                return rawFb
                            }
                            return pasteTextOpt ?? ""
                        }()
                        let pasteDeadlineMs: UInt64 = enqueuedMs &+ 1250
                        if monotonicMs() >= pasteDeadlineMs {
                            if pasteTextOpt != nil {
                                writeJsonLine([
                                    "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true,
                                    "posted": false, "clipboard": false, "reason": "shim_paste_unconfirmed",
                                ])
                            } else {
                                writeJsonLine(["type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true, "posted": false, "reason": "shim_paste_unconfirmed"])
                            }
                            return
                        }
                        guard CGPreflightPostEventAccess() else {
                            if pasteTextOpt != nil {
                                let clipOk = pasteboardGuard.writePersistentFallback(fbText)
                                writeJsonLine([
                                    "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true,
                                    "posted": false, "clipboard": clipOk,
                                    "reason": clipOk ? "accessibility_denied" : "clipboard_failed",
                                ])
                            } else {
                                writeJsonLine(["type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true, "posted": false, "reason": "accessibility_denied"])
                            }
                            if !postAccessPrompted {
                                postAccessPrompted = true
                                DispatchQueue.main.async { _ = CGRequestPostEventAccess() }
                            }
                            return
                        }
                        let normProc: (String) -> String = { raw in
                            let low = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                            return low.hasSuffix(".app") ? String(low.dropLast(4)).trimmingCharacters(in: .whitespacesAndNewlines) : low
                        }
                        let expBundle = ((obj["expect_bundle_id"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                        let expProc = normProc((obj["expect_process"] as? String) ?? "")
                        let expHwnd = (obj["expect_hwnd"] as? Int) ?? ((obj["expect_hwnd"] as? NSNumber)?.intValue ?? 0)
                        let hasFocusExpectation = (!expBundle.isEmpty || !expProc.isEmpty || expHwnd > 0)
                        let checkFocus: () -> (matched: Bool, fgName: String) = {
                            if !hasFocusExpectation { return (true, "") }
                            guard let curApp = NSWorkspace.shared.frontmostApplication else {
                                return (false, "")
                            }
                            let curName = curApp.localizedName ?? curApp.bundleURL?.deletingPathExtension().lastPathComponent ?? ""
                            let curProc = normProc(curName)
                            let curBundle = (curApp.bundleIdentifier ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                            let curPid = Int(curApp.processIdentifier)
                            var curHwnd = 0
                            if expHwnd > 0 && curPid > 0,
                               let wlist = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
                                for entry in wlist {
                                    let ownerPid = (entry[kCGWindowOwnerPID as String] as? Int) ?? ((entry[kCGWindowOwnerPID as String] as? NSNumber)?.intValue ?? 0)
                                    let wLayer = (entry[kCGWindowLayer as String] as? Int) ?? ((entry[kCGWindowLayer as String] as? NSNumber)?.intValue ?? -1)
                                    if ownerPid == curPid && wLayer == 0 {
                                        curHwnd = (entry[kCGWindowNumber as String] as? Int) ?? ((entry[kCGWindowNumber as String] as? NSNumber)?.intValue ?? 0)
                                        break
                                    }
                                }
                            }
                            let appMismatch = (!expBundle.isEmpty && !curBundle.isEmpty && curBundle != expBundle)
                                || ((expBundle.isEmpty || curBundle.isEmpty) && !expProc.isEmpty && !curProc.isEmpty && curProc != expProc)
                                || (curBundle.isEmpty && curProc.isEmpty)
                            let hwndMismatch = (expHwnd > 0 && curHwnd != expHwnd)
                            return (!(appMismatch || hwndMismatch), curName)
                        }
                        let replyFocusChanged: (String) -> Void = { fgName in
                            if pasteTextOpt != nil {
                                let clipOk = pasteboardGuard.writePersistentFallback(fbText)
                                writeJsonLine([
                                    "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true,
                                    "posted": false, "clipboard": clipOk,
                                    "reason": clipOk ? "focus_changed" : "clipboard_failed", "fg_process": fgName,
                                ])
                            } else {
                                writeJsonLine(["type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true, "posted": false, "reason": "focus_changed", "fg_process": fgName])
                            }
                        }
                        let focus1 = checkFocus()
                        if !focus1.matched {
                            replyFocusChanged(focus1.fgName)
                            return
                        }
                        var verifiedFgName = focus1.fgName
                        if let pasteTxt = pasteTextOpt {
                            let writeRes = pasteboardGuard.writeTransientChecked(pasteTxt, deadlineMonoMs: pasteDeadlineMs)
                            if writeRes == .expired {
                                var expPayload: [String: Any] = [
                                    "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true,
                                    "posted": false, "clipboard": false, "reason": "shim_paste_unconfirmed",
                                ]
                                if !verifiedFgName.isEmpty { expPayload["fg_process"] = verifiedFgName }
                                writeJsonLine(expPayload)
                                return
                            }
                            if writeRes != .ok {
                                var failPayload: [String: Any] = [
                                    "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true,
                                    "posted": false, "clipboard": false, "reason": "clipboard_failed",
                                ]
                                if !verifiedFgName.isEmpty { failPayload["fg_process"] = verifiedFgName }
                                writeJsonLine(failPayload)
                                return
                            }
                            if hasFocusExpectation {
                                let focus2 = checkFocus()
                                if !focus2.matched {
                                    replyFocusChanged(focus2.fgName)
                                    return
                                }
                                verifiedFgName = focus2.fgName
                            }
                        }
                        let src = CGEventSource(stateID: .privateState)
                        let steps: [(CGKeyCode, Bool, CGEventFlags)] = [
                            (0x37, true, .maskCommand),
                            (0x09, true, .maskCommand),
                            (0x09, false, .maskCommand),
                            (0x37, false, []),
                        ]
                        var events: [CGEvent] = []
                        if src != nil {
                            for (kc, isDown, fl) in steps {
                                guard let ev = CGEvent(keyboardEventSource: src, virtualKey: kc, keyDown: isDown) else {
                                    events.removeAll()
                                    break
                                }
                                ev.flags = fl
                                ev.setIntegerValueField(.eventSourceUserData, value: AVI_INJECT_TAG)
                                events.append(ev)
                            }
                        }
                        let postedOk = (events.count == steps.count)
                        if postedOk {
                            for ev in events {
                                ev.post(tap: .cghidEventTap)
                            }
                            if pasteTextOpt != nil {
                                var okPayload: [String: Any] = [
                                    "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true,
                                    "posted": true, "clipboard": true, "reason": "ok",
                                ]
                                if !verifiedFgName.isEmpty { okPayload["fg_process"] = verifiedFgName }
                                writeJsonLine(okPayload)
                            } else {
                                writeJsonLine(["type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true, "posted": true, "reason": "ok"])
                            }
                        } else if pasteTextOpt != nil {
                            let clipOk = pasteboardGuard.writePersistentFallback(fbText)
                            var cgFailPayload: [String: Any] = [
                                "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true,
                                "posted": false, "clipboard": clipOk,
                                "reason": clipOk ? "cgevent_failed" : "clipboard_failed",
                            ]
                            if !verifiedFgName.isEmpty { cgFailPayload["fg_process"] = verifiedFgName }
                            writeJsonLine(cgFailPayload)
                        } else {
                            writeJsonLine(["type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": true, "posted": false, "reason": "cgevent_failed"])
                        }
                    case "hud":
                        let dms = (obj["duration_ms"] as? Int) ?? ((obj["duration_ms"] as? NSNumber)?.intValue ?? 0)
                        let maxDur = (obj["max_duration_sec"] as? Int) ?? ((obj["max_duration_sec"] as? NSNumber)?.intValue ?? 0)
                        let warnRem = (obj["warn_remain_sec"] as? Int) ?? ((obj["warn_remain_sec"] as? NSNumber)?.intValue ?? 0)
                        let bgCount = (obj["bg_count"] as? Int) ?? ((obj["bg_count"] as? NSNumber)?.intValue ?? 0)
                        let pos = (obj["position"] as? String) ?? "bottom"
                        let layer = (obj["layer"] as? String) ?? ""
                        let clearMain = parseOptionalBool(obj["clear_main"])
                        let clearToast = parseOptionalBool(obj["clear_toast"])
                        hud.update(
                            state: (obj["state"] as? String) ?? "",
                            customText: (obj["text"] as? String) ?? "",
                            durationMs: dms,
                            position: pos,
                            maxDurationSec: maxDur,
                            warnRemainSec: warnRem,
                            layer: layer,
                            clearMain: clearMain,
                            clearToast: clearToast,
                            bgCount: bgCount,
                            preserveHoldPreview: engine.holdHintShown && !engine.holdFired
                        )
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

        writeJsonLine(["event": "ready", "target_vk": String(format: "0x%02X", spec.primaryKeyCode), "mode": triggerMode, "listen_access": hasListenAccess, "ui_culture": hostUiCulture(), "caps": ["paste_clip"], "audio_status": initialAudioStatus])
        app.run(); cleanupAndExit(0)
    }
}
