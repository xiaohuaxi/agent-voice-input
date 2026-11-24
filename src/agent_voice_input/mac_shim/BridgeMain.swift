import AppKit
import ApplicationServices
import Foundation

private let stdoutLock = NSLock()

func writeJsonLine(_ dict: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(dict),
          let data = try? JSONSerialization.data(withJSONObject: dict, options: []),
          let str = String(data: data, encoding: .utf8) else { return }
    stdoutLock.lock()
    fputs(str + "\n", stdout); fflush(stdout)
    stdoutLock.unlock()
}

func monotonicMs() -> UInt64 {
    ProcessInfo.processInfo.systemUptime.isFinite ? UInt64(ProcessInfo.processInfo.systemUptime * 1000.0) : 0
}

func postEventsIfWithinDeadline<T>(
    events: [T],
    deadlineMonoMs: UInt64,
    nowMs: () -> UInt64 = monotonicMs,
    postEvent: (T) -> Void
) -> Bool {
    if deadlineMonoMs > 0 && nowMs() >= deadlineMonoMs {
        return false
    }
    for ev in events {
        postEvent(ev)
    }
    return true
}

private func parseOptionalBool(_ raw: Any?) -> Bool? {
    if let b = raw as? Bool { return b }
    if let n = raw as? NSNumber { return n.boolValue }
    return nil
}

@main
struct MacShimMain {
    static func main() {
        signal(SIGPIPE, SIG_IGN)
        var (mode, hotkey, triggerMode, tapMaxMs, holdCancelMs, holdHintMs, debounceMs, seconds) = ("bridge", "RControl", "tap_toggle", 500, 800, 250, 180, 10)
        var pasteboardNameArg: String? = nil
        var providerDelayMs = 2000
        var providerText = "stalled_promise_data"
        let args = CommandLine.arguments
        var i = 1
        while i < args.count {
            let arg = args[i].lowercased()
            if arg == "--self-check" || arg == "-self-check" { mode = "self-check"; i += 1; continue }
            if arg == "--audio-status" || arg == "-audio-status" { mode = "audio-status"; i += 1; continue }
            if arg == "--pasteboard-worker" || arg == "-pasteboard-worker" { mode = "pasteboard-worker"; i += 1; continue }
            if arg == "--self-check-promise-provider" { mode = "self-check-promise-provider"; i += 1; continue }
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
            case "--pasteboard-name", "-pasteboardname": if let v = nextVal { pasteboardNameArg = v; i += 2 } else { i += 1 }
            case "--delay-ms": if let v = nextVal, let n = Int(v) { providerDelayMs = max(0, n); i += 2 } else { i += 1 }
            case "--text": if let v = nextVal { providerText = v; i += 2 } else { i += 1 }
            default: i += 1
            }
        }
        if mode == "audio-status" { Foundation.exit(runAudioStatusMode()) }
        if mode == "pasteboard-worker" { Foundation.exit(runPasteboardWorker(pasteboardName: pasteboardNameArg)) }
        if mode == "self-check-promise-provider" {
            Foundation.exit(runSelfCheckPromiseProvider(pasteboardName: pasteboardNameArg ?? "", delayMs: providerDelayMs, text: providerText))
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
        let audioQueryQueue = DispatchQueue(label: "agent_voice_input.audio_query", qos: .userInitiated)
        let windowQueryQueue = DispatchQueue(label: "agent_voice_input.window_query", qos: .userInitiated)
        let eventEmitQueue = DispatchQueue(label: "agent_voice_input.event_emit", qos: .userInitiated)
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
            DispatchQueue.global().asyncAfter(deadline: .now() + 2.0) { Foundation.exit(code) }
            cancelHoldWorkItems()
            if let wo = wakeObserver { NSWorkspace.shared.notificationCenter.removeObserver(wo); wakeObserver = nil }
            if let gm = globalMonitor { NSEvent.removeMonitor(gm); globalMonitor = nil }
            pasteboardGuard.flushPendingRestore()
            pasteboardGuard.shutdown()
            audioMonitor.stop()
            hud.close()
            Foundation.exit(code)
        }

        let emitStepEvents: ([EmittedEvent], UInt64) -> Void = { emitted, now in
            if isKeyTest {
                for item in emitted {
                    let kname = KEY_CODE_NAMES[item.vk] ?? String(format: "VK_0x%02X", item.vk)
                    var ktPayload: [String: Any] = ["event": item.kind, "vk": String(format: "0x%02X", item.vk), "key_name": kname, "flags": String(format: "0x%X", item.flags), "is_down": item.isDown, "tainted": item.tainted]
                    if item.kind == "hold_cancel" { ktPayload["hold_ms"] = item.holdMs }
                    writeJsonLine(ktPayload)
                }
                return
            }
            for item in emitted {
                let appSnap = snapshotFrontmostAppBasic()
                let pinnedHwnd = queryFrontmostWindowNumber(pid: appSnap.pid)
                let allowAxProbe = shouldProbeAxForEventKind(item.kind)
                let allowDeepProbe = shouldProbeWebAreaForEventKind(item.kind)
                eventEmitQueue.async {
                    let meta = queryFrontWindowMetadata(
                        pid: appSnap.pid,
                        bundleId: appSnap.bundleId,
                        pinnedWindowId: pinnedHwnd,
                        allowAxProbe: allowAxProbe,
                        allowDeepWebAreaProbe: allowDeepProbe
                    )
                    var payload: [String: Any] = ["event": item.kind, "hwnd": meta.hwnd, "process": appSnap.name, "bundle_id": appSnap.bundleId, "pid": appSnap.pid, "title": meta.title, "document": meta.document, "page_url": meta.pageUrl, "ax_trusted": meta.axTrusted, "ts_ms": now]
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
            if isStaleKeyEventTimestamp(ev.timestamp, systemUptime: ProcessInfo.processInfo.systemUptime) {
                let staleReset = engine.resetForStaleEvent()
                applyStep(staleReset, now: now)
                return
            }
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
            eventEmitQueue.async {
                writeJsonLine(["event": "power_resume", "ts_ms": now])
            }
        }
        var postAccessPrompted = false
        var pasteInFlight = false
        var pendingPasteJobs: [() -> Void] = []
        var runNextPasteJob: (() -> Void)!
        runNextPasteJob = {
            guard !pasteInFlight, !pendingPasteJobs.isEmpty else { return }
            pasteInFlight = true
            let job = pendingPasteJobs.removeFirst()
            job()
        }
        let finishCurrentPasteJob: () -> Void = {
            pasteInFlight = false
            runNextPasteJob()
        }

        let isHotkeyOrModifierHeld: () -> Bool = {
            let modFlags = CGEventSource.flagsState(.combinedSessionState)
            let blockingMods: CGEventFlags = [.maskShift, .maskControl, .maskAlternate, .maskCommand]
            let fnHeld = (spec.highFlag == FLAG_FUNCTION) && modFlags.contains(.maskSecondaryFn)
            let nonModHeld = !spec.isModifier && (
                CGEventSource.keyState(.combinedSessionState, key: CGKeyCode(spec.primaryKeyCode))
                || spec.altKeyCodes.contains(where: { CGEventSource.keyState(.combinedSessionState, key: CGKeyCode($0)) })
            )
            return engine.targetDown || !modFlags.intersection(blockingMods).isEmpty || fnHeld || nonModHeld
        }

        let stdinThread = Thread {
            while true {
                let keepReading = autoreleasepool { () -> Bool in
                    guard let rawLine = readLine(strippingNewline: true) else {
                        return false
                    }
                    let trimmed = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
                    if trimmed.isEmpty { return true }
                    let enqueuedMs = monotonicMs()
                    guard let data = trimmed.data(using: .utf8),
                          let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return true }
                    let op = (obj["cmd"] as? String) ?? "unknown"
                    let seqId = (obj["seq_id"] as? Int) ?? ((obj["seq_id"] as? NSNumber)?.intValue ?? 0)
                    switch op {
                    case "ping":
                        audioQueryQueue.async {
                            writeJsonLine(["type": "reply", "reply": "ping", "seq_id": seqId, "ok": true, "ax_trusted": AXIsProcessTrusted(), "listen_event_access": CGPreflightListenEventAccess(), "audio_status": queryAudioInputStatus()])
                        }
                        return true
                    case "audio_status":
                        audioQueryQueue.async {
                            writeJsonLine(["type": "reply", "reply": "audio_status", "seq_id": seqId, "ok": true, "audio_status": queryAudioInputStatus()])
                        }
                        return true
                    default:
                        break
                    }
                    DispatchQueue.main.async {
                        switch op {
                        case "fg_window":
                            let appSnap = snapshotFrontmostAppBasic()
                            let pinnedHwnd = queryFrontmostWindowNumber(pid: appSnap.pid)
                            windowQueryQueue.async {
                                let meta = queryFrontWindowMetadata(
                                    pid: appSnap.pid,
                                    bundleId: appSnap.bundleId,
                                    pinnedWindowId: pinnedHwnd,
                                    allowAxProbe: true,
                                    allowDeepWebAreaProbe: true
                                )
                                writeJsonLine(["type": "reply", "reply": "fg_window", "seq_id": seqId, "ok": true, "hwnd": meta.hwnd, "process": appSnap.name, "bundle_id": appSnap.bundleId, "pid": appSnap.pid, "title": meta.title, "document": meta.document, "page_url": meta.pageUrl, "ax_trusted": meta.axTrusted])
                            }
                        case "key_state":
                            let held = isHotkeyOrModifierHeld()
                            let immediate = parseOptionalBool(obj["immediate"]) ?? false
                            let replyPayload: [String: Any] = ["type": "reply", "reply": "key_state", "seq_id": seqId, "ok": true, "hotkey_held": held]
                            if immediate {
                                writeJsonLine(replyPayload)
                            } else {
                                eventEmitQueue.async {
                                    writeJsonLine(replyPayload)
                                }
                            }
                        case "paste_cmd_v":
                            let pasteTextOpt = obj["text"] as? String
                            let fbText: String = {
                                if let rawFb = obj["fallback_text"] as? String, !rawFb.isEmpty {
                                    return rawFb
                                }
                                return pasteTextOpt ?? ""
                            }()
                            let forceClipboardOnly = parseOptionalBool(obj["force_clipboard_only"]) ?? false
                            let reqDeadlineMs = (obj["deadline_ms"] as? Int) ?? ((obj["deadline_ms"] as? NSNumber)?.intValue ?? 0)
                            let effectiveBudgetMs: UInt64 = (reqDeadlineMs > 0) ? UInt64(min(reqDeadlineMs, 10_000)) : 1250
                            let pasteDeadlineMs: UInt64 = enqueuedMs &+ effectiveBudgetMs
                            let normProc: (String) -> String = { raw in
                                let low = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                                return low.hasSuffix(".app") ? String(low.dropLast(4)).trimmingCharacters(in: .whitespacesAndNewlines) : low
                            }
                            let expBundle = ((obj["expect_bundle_id"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                            let expProc = normProc((obj["expect_process"] as? String) ?? "")
                            let expHwnd = (obj["expect_hwnd"] as? Int) ?? ((obj["expect_hwnd"] as? NSNumber)?.intValue ?? 0)
                            let hasFocusExpectation = (!expBundle.isEmpty || !expProc.isEmpty || expHwnd > 0)

                            let pasteJob: () -> Void = {
                                var jobDone = false
                                let completeJob: () -> Void = {
                                    guard !jobDone else { return }
                                    jobDone = true
                                    finishCurrentPasteJob()
                                }
                                let replyPaste: (Bool, Bool?, String, String, Bool) -> Void = { posted, clip, reason, fgName, ok in
                                    var payload: [String: Any] = [
                                        "type": "reply", "reply": "paste_cmd_v", "seq_id": seqId, "ok": ok,
                                        "posted": posted, "reason": reason,
                                    ]
                                    if let c = clip { payload["clipboard"] = c }
                                    if !fgName.isEmpty { payload["fg_process"] = fgName }
                                    writeJsonLine(payload)
                                    completeJob()
                                }
                                let checkFocus: () -> (matched: Bool, fgName: String) = {
                                    if !hasFocusExpectation { return (true, "") }
                                    guard let curApp = NSWorkspace.shared.frontmostApplication else {
                                        return (false, "")
                                    }
                                    let curName = curApp.localizedName ?? curApp.bundleURL?.deletingPathExtension().lastPathComponent ?? ""
                                    let curProc = normProc(curName)
                                    let curBundle = (curApp.bundleIdentifier ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                                    let curPid = Int(curApp.processIdentifier)
                                    let curHwnd = (expHwnd > 0 && curPid > 0) ? queryFrontmostWindowNumber(pid: curPid) : 0
                                    let appMismatch = (!expBundle.isEmpty && !curBundle.isEmpty && curBundle != expBundle)
                                        || ((expBundle.isEmpty || curBundle.isEmpty) && !expProc.isEmpty && !curProc.isEmpty && curProc != expProc)
                                        || (curBundle.isEmpty && curProc.isEmpty)
                                    let hwndMismatch = (expHwnd > 0 && curHwnd != expHwnd)
                                    return (!(appMismatch || hwndMismatch), curName)
                                }
                                let buildCmdVEvents: () -> [CGEvent]? = {
                                    guard let src = CGEventSource(stateID: .privateState) else { return nil }
                                    let steps: [(CGKeyCode, Bool, CGEventFlags)] = [
                                        (0x37, true, .maskCommand),
                                        (0x09, true, .maskCommand),
                                        (0x09, false, .maskCommand),
                                        (0x37, false, []),
                                    ]
                                    var events: [CGEvent] = []
                                    events.reserveCapacity(steps.count)
                                    for (kc, isDown, fl) in steps {
                                        guard let ev = CGEvent(keyboardEventSource: src, virtualKey: kc, keyDown: isDown) else {
                                            return nil
                                        }
                                        ev.flags = fl
                                        ev.setIntegerValueField(.eventSourceUserData, value: AVI_INJECT_TAG)
                                        events.append(ev)
                                    }
                                    return events
                                }

                                // Fast-path: force_clipboard_only persists text on main thread without worker snapshot, focus, or AX checks,
                                // while still honoring pasteDeadlineMs so an expired request never clobbers a newer user copy.
                                if forceClipboardOnly, pasteTextOpt != nil {
                                    let (clipOk, clipReason) = pasteboardGuard.writePersistentFallbackIfWithinDeadline(
                                        fbText,
                                        deadlineMonoMs: pasteDeadlineMs
                                    )
                                    replyPaste(false, clipOk, clipReason, "", true)
                                    return
                                }
                                if monotonicMs() >= pasteDeadlineMs {
                                    replyPaste(false, pasteTextOpt != nil ? false : nil, "shim_paste_unconfirmed", "", true)
                                    return
                                }
                                guard CGPreflightPostEventAccess() else {
                                    if pasteTextOpt != nil {
                                        let clipOk = pasteboardGuard.writePersistentFallback(fbText)
                                        replyPaste(false, clipOk, clipOk ? "accessibility_denied" : "clipboard_failed", "", true)
                                    } else {
                                        replyPaste(false, nil, "accessibility_denied", "", true)
                                    }
                                    if !postAccessPrompted {
                                        postAccessPrompted = true
                                        DispatchQueue.main.async { _ = CGRequestPostEventAccess() }
                                    }
                                    return
                                }
                                // Legacy Cmd+V only mode (no managed clipboard text)
                                guard let pasteTxt = pasteTextOpt else {
                                    if isHotkeyOrModifierHeld() {
                                        replyPaste(false, nil, "key_held", "", false)
                                        return
                                    }
                                    let focus = checkFocus()
                                    if !focus.matched {
                                        replyPaste(false, nil, "focus_changed", focus.fgName, true)
                                        return
                                    }
                                    guard let events = buildCmdVEvents() else {
                                        replyPaste(false, nil, "cgevent_failed", focus.fgName, true)
                                        return
                                    }
                                    let postedOk = postEventsIfWithinDeadline(events: events, deadlineMonoMs: pasteDeadlineMs) { ev in
                                        ev.post(tap: .cghidEventTap)
                                    }
                                    replyPaste(postedOk, nil, postedOk ? "ok" : "shim_paste_unconfirmed", focus.fgName, true)
                                    return
                                }

                                // Managed linear paste pipeline:
                                // (a) One-shot snapshot via single-slot worker (nil on timeout/error/>10MiB; never retries or refuses)
                                pasteboardGuard.captureSnapshotAsync(deadlineMonoMs: pasteDeadlineMs) { preChangeCount, snap in
                                    // (b) Single pre-write gate on main thread: deadline -> focus -> modifier/hotkey held
                                    if monotonicMs() >= pasteDeadlineMs {
                                        replyPaste(false, false, "shim_paste_unconfirmed", "", true)
                                        return
                                    }
                                    let focus = checkFocus()
                                    if !focus.matched {
                                        // (c) Focus drifted: cancel pending restore & persist fallback
                                        let clipOk = pasteboardGuard.writePersistentFallback(fbText)
                                        replyPaste(false, clipOk, clipOk ? "focus_changed" : "clipboard_failed", focus.fgName, true)
                                        return
                                    }
                                    if isHotkeyOrModifierHeld() {
                                        // (c) Modifier/hotkey held: cancel pending restore & persist fallback
                                        let clipOk = pasteboardGuard.writePersistentFallback(fbText)
                                        replyPaste(false, clipOk, clipOk ? "key_held" : "clipboard_failed", focus.fgName, true)
                                        return
                                    }
                                    // (d) Same window & keys released: if clipboard changed during read, drop snapshot (no recapture, no refusal)
                                    let effectiveSnap: PasteboardGuard.ClipSnap? = (pasteboardGuard.currentChangeCount == preChangeCount) ? snap : nil
                                    guard let events = buildCmdVEvents() else {
                                        let clipOk = pasteboardGuard.writePersistentFallback(fbText)
                                        replyPaste(false, clipOk, clipOk ? "cgevent_failed" : "clipboard_failed", focus.fgName, true)
                                        return
                                    }
                                    // Atomic main-thread sequence (<1ms, zero async, zero focus re-check):
                                    // cancel old timer -> write transient text -> record writtenChangeCount -> post Cmd+V -> arm 250ms timer
                                    guard pasteboardGuard.writeTransient(pasteTxt, snap: effectiveSnap) else {
                                        replyPaste(false, false, "clipboard_failed", focus.fgName, true)
                                        return
                                    }
                                    for ev in events {
                                        ev.post(tap: .cghidEventTap)
                                    }
                                    pasteboardGuard.armPendingRestoreTimer()
                                    replyPaste(true, true, "ok", focus.fgName, true)
                                }
                            }
                            pendingPasteJobs.append(pasteJob)
                            runNextPasteJob()
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
                    return true
                }
                if !keepReading { break }
            }
            DispatchQueue.main.async { cleanupAndExit(0) }
            DispatchQueue.global().asyncAfter(deadline: .now() + 2.0) { Foundation.exit(0) }
        }
        stdinThread.name = "mac-shim-stdin"
        stdinThread.start()

        writeJsonLine(["event": "ready", "target_vk": String(format: "0x%02X", spec.primaryKeyCode), "mode": triggerMode, "listen_access": hasListenAccess, "ui_culture": hostUiCulture(), "caps": ["paste_clip", "key_state_immediate"], "audio_status": initialAudioStatus])
        app.run(); cleanupAndExit(0)
    }
}
