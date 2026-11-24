import AppKit
import ApplicationServices
import Foundation

// Device-dependent modifier masks from IOKit/hidsystem/IOLLEvent.h
let NX_DEVICELCTLKEYMASK: UInt   = 0x00000001
private let NX_DEVICELSHIFTKEYMASK: UInt = 0x00000002
private let NX_DEVICERSHIFTKEYMASK: UInt = 0x00000004
private let NX_DEVICELCMDKEYMASK: UInt   = 0x00000008
private let NX_DEVICERCMDKEYMASK: UInt   = 0x00000010
private let NX_DEVICELALTKEYMASK: UInt   = 0x00000020
private let NX_DEVICERALTKEYMASK: UInt   = 0x00000040
let NX_DEVICERCTLKEYMASK: UInt   = 0x00002000

private let FLAG_CAPSLOCK: UInt = NSEvent.ModifierFlags.capsLock.rawValue
let FLAG_SHIFT: UInt    = NSEvent.ModifierFlags.shift.rawValue
let FLAG_CONTROL: UInt  = NSEvent.ModifierFlags.control.rawValue
private let FLAG_OPTION: UInt   = NSEvent.ModifierFlags.option.rawValue
private let FLAG_COMMAND: UInt  = NSEvent.ModifierFlags.command.rawValue
let FLAG_FUNCTION: UInt = NSEvent.ModifierFlags.function.rawValue
let AVI_INJECT_TAG: Int64 = 0x41564931

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

let KEY_CODE_NAMES: [UInt16: String] = {
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

func hasAnyNonModifierKeyPhysicallyDown(excluding spec: TargetKeySpec) -> Bool {
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

    private mutating func resetInternal(activePttEventKind: String, pttTainted: Bool) -> HotkeyStepResult {
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
            res.events.append(EmittedEvent(kind: activePttEventKind, vk: spec.primaryKeyCode, flags: 0, isDown: false, tainted: pttTainted))
        }
        return res
    }

    mutating func resetForPowerResume() -> HotkeyStepResult {
        resetInternal(activePttEventKind: "ptt_stop", pttTainted: false)
    }

    mutating func resetForStaleEvent() -> HotkeyStepResult {
        resetInternal(activePttEventKind: "ptt_cancel", pttTainted: true)
    }
}

func isStaleKeyEventTimestamp(
    _ eventTimestamp: TimeInterval,
    systemUptime: TimeInterval,
    maxAgeSec: TimeInterval = 2.0
) -> Bool {
    guard eventTimestamp > 0.0, eventTimestamp <= systemUptime else {
        return false
    }
    return (systemUptime - eventTimestamp) > maxAgeSec
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
