import AppKit
import ApplicationServices
import AVFoundation
import CoreAudio
import Foundation
import Security

func runSelfCheckPromiseProvider(pasteboardName: String, delayMs: Int, text: String) -> Int32 {
    signal(SIGPIPE, SIG_IGN)
    let parentPid = getppid()
    let parentExitSrc = installParentExitWatchdog(parentPid: parentPid)
    return withExtendedLifetime(parentExitSrc) { () -> Int32 in
        final class DelayedProvider: NSObject, NSPasteboardItemDataProvider {
            let delayMs: Int
            let text: String
            init(delayMs: Int, text: String) {
                self.delayMs = max(0, delayMs)
                self.text = text
            }
            func pasteboard(_ pasteboard: NSPasteboard?, item: NSPasteboardItem, provideDataForType type: NSPasteboard.PasteboardType) {
                if delayMs > 0 {
                    usleep(useconds_t(min(delayMs, 5000) * 1000))
                }
                if type == .string {
                    item.setString(text, forType: .string)
                }
            }
        }
        // Exit immediately if parent closes stdin or dies so self-check never leaves orphan helpers.
        let watchdog = Thread {
            var b: UInt8 = 0
            while true {
                let n = Darwin.read(STDIN_FILENO, &b, 1)
                if n == 0 || (n < 0 && errno != EINTR) || getppid() != parentPid {
                    Darwin._exit(0)
                }
            }
        }
        watchdog.start()

        let pb = NSPasteboard(name: NSPasteboard.Name(pasteboardName))
        let provider = DelayedProvider(delayMs: delayMs, text: text)
        pb.clearContents()
        let item = NSPasteboardItem()
        guard item.setDataProvider(provider, forTypes: [.string]), pb.writeObjects([item]) else {
            return 2
        }
        let cc = pb.changeCount
        let line = "READY \(cc)\n"
        fputs(line, stdout)
        fflush(stdout)
        let endAt = Date().addingTimeInterval(6.0)
        while Date() < endAt {
            if getppid() != parentPid { break }
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        return 0
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
        holdCancelPreview: Bool = false,
        barNorms: [CGFloat] = [0.2, 0.45, 0.75, 0.95, 1.0, 0.85, 0.6, 0.35, 0.15],
        verifyPerBar: Bool = false
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
        let hintCol = (isWarn || holdCancelPreview) ? rgba(251, 191, 36, 1.0) : rgba(200, 178, 182, 1.0)
        let bgCol = isWarn ? rgba(72, 38, 12, 0.98) : rgba(64, 18, 26, 0.98)
        let borderCol = isWarn ? rgba(245, 158, 11, 0.72) : rgba(232, 88, 102, 0.68)
        let waveActiveCol = isWarn ? HudPillController.warnWaveActive : HudPillController.recWaveActive
        let waveQuietCol = isWarn ? HudPillController.warnWaveQuiet : HudPillController.recWaveQuiet
        let badgeBgCol = isWarn ? HudPillController.warnBadgeBg : HudPillController.recBadgeBg
        let badgeBorderCol = isWarn ? HudPillController.warnBadgeBorder : HudPillController.recBadgeBorder
        let badgeTextCol = isWarn ? HudPillController.warnBadgeTextCol : HudPillController.recBadgeTextCol

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
        let badgeAttr = NSAttributedString(string: badgeStr, attributes: [.font: HudPillController.badgeFont, .foregroundColor: badgeTextCol, .paragraphStyle: centerPara])
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
            try assertLayout(geom.padR == 10.0, "\(label): expected optical padR=10.0 with badge, got \(geom.padR)")
            let prevRight = effHint.isEmpty ? geom.waveFrame.maxX : geom.hintFrame.maxX
            try assertLayout(prevRight < geom.badgeFrame.minX, "\(label): badgeFrame overlaps previous segment")
            try assertLayout(geom.badgeFrame.width >= badgeSz.width, "\(label): badgeFrame overflow")
            try assertLayout(geom.badgeFrame.maxX <= geom.pillW - geom.padR, "\(label): badgeFrame right padding")
        } else {
            try assertLayout(geom.padR == 16.0, "\(label): expected padR=16.0 without badge, got \(geom.padR)")
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
        let waveView = WaveBarsView(frame: NSRect(origin: .zero, size: geom.waveFrame.size))
        waveView.activeColor = waveActiveCol
        waveView.quietColor = waveQuietCol
        waveView.barNorms = barNorms
        ctx.cgContext.saveGState()
        ctx.cgContext.translateBy(x: geom.waveFrame.minX, y: geom.waveFrame.minY)
        waveView.draw(waveView.bounds)
        ctx.cgContext.restoreGState()
        if !effHint.isEmpty {
            hintAttr.draw(in: NSRect(x: geom.hintFrame.minX + 2.0, y: geom.hintFrame.minY, width: max(1.0, geom.hintFrame.width - 2.0), height: geom.hintFrame.height))
        }
        if hasBadge {
            let bPath = NSBezierPath(roundedRect: geom.badgeFrame, xRadius: geom.badgeFrame.height / 2.0, yRadius: geom.badgeFrame.height / 2.0)
            badgeBgCol.setFill()
            bPath.fill()
            badgeBorderCol.setStroke()
            bPath.lineWidth = 1.0
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
        if verifyPerBar {
            for idx in 0..<9 {
                let norm = (idx < barNorms.count) ? min(1.0, max(0.0, barNorms[idx])) : 0.0
                let bx = geom.waveFrame.minX + CGFloat(idx) * (WaveBarsView.barWidth + WaveBarsView.barGap)
                let barRect = NSRect(x: bx, y: geom.waveFrame.minY, width: WaveBarsView.barWidth, height: geom.waveFrame.height)
                let kind = norm > 0.01 ? "active" : "quiet"
                try assertLayout(
                    regionHasDistinctForeground(data: data, bpr: bpr, spp: spp, pxW: pxW, pxH: pxH, rect: barRect, scale: scale, bgR: bgR, bgG: bgG, bgB: bgB),
                    "\(label): wave bar[\(idx)] (\(kind), norm=\(norm)) invisible"
                )
            }
        }
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
            let mixedWaveNorms: [CGFloat] = [0.0, 0.005, 0.01, 0.25, 0.75, 1.0, 0.0, 0.01, 0.0]
            try renderAndVerifyWave(
                label: "mac_rec_wave_mixed_\(langTag)_s\(Int(spec.scale))",
                text: recTexts[0],
                elapsedSec: 5,
                maxDurSec: 0,
                warnRemSec: 0,
                bgCount: 0,
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale,
                barNorms: mixedWaveNorms,
                verifyPerBar: true
            )
            cases += 1
            try renderAndVerifyWave(
                label: "mac_warn_wave_mixed_\(langTag)_s\(Int(spec.scale))",
                text: recTexts[0],
                elapsedSec: 55,
                maxDurSec: 60,
                warnRemSec: 15,
                bgCount: 0,
                zh: zh,
                visibleW: spec.width,
                scale: spec.scale,
                barNorms: mixedWaveNorms,
                verifyPerBar: true
            )
            cases += 1
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
    for (idx, (tst, expMs)) in [("mic_no_stream", 2600), ("copied", 5000), ("cancelled", 1600), ("audio_recovered", 2400)].enumerated() {
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
    ctrl.update(state: "copied", customText: "", durationMs: 5000, layer: "main", clearToast: false)
    ctrl.setHoldCancelPreview(true)
    try assertLayout(!ctrl.debugHoldPreviewActive, "slot copied ignores hold preview")

    ctrl.update(state: "hide_all", customText: "", durationMs: 0)
    ctrl.setHoldCancelPreview(true)
    try assertLayout(!ctrl.debugMainVisible && !ctrl.debugToastVisible && !ctrl.debugHoldPreviewActive, "slot hide_all clears both")

    return cases
}

func runSelfCheck() -> Int32 {
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
        // Stale key event during active PTT cancels (ptt_cancel) rather than submitting (ptt_stop)
        var stalePtt = HotkeyEngine(spec: rctrl, isPtt: true, tapMaxMs: 500, holdCancelMs: 800, holdHintMs: 250, debounceMs: 180, keyTest: false)
        _ = stalePtt.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: FLAG_CONTROL | NX_DEVICERCTLKEYMASK, isARepeat: false, nowMs: 1000, keyDownCounter: 1)
        try check(stalePtt.pttActive && stalePtt.targetDown, "stale event ptt setup")
        let stalePttReset = stalePtt.resetForStaleEvent()
        try check(!stalePtt.pttActive && !stalePtt.targetDown && stalePttReset.cancelTimers && stalePttReset.clearHoldPreview, "stale event clears ptt state")
        try check(stalePttReset.events.count == 1 && stalePttReset.events[0].kind == "ptt_cancel" && stalePttReset.events[0].tainted, "stale event emits only ptt_cancel (never ptt_stop) for active PTT")
        let stalePttUp = stalePtt.handleEventStep(kind: .flagsChanged, keyCode: 0x3E, rawFlags: 0, isARepeat: false, nowMs: 1400, keyDownCounter: 1)
        try check(stalePttUp.events.isEmpty, "post-stale-reset PTT key-up emits no ptt_stop")
        let stalePttIdleReset = stalePtt.resetForStaleEvent()
        try check(stalePttIdleReset.events.isEmpty, "idle PTT stale reset emits no event")
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

        // Multi-screen HUD selection self-check (CG top-left -> AppKit bottom-left coordinate conversion + fallbacks)
        do {
            let horizScreens: [CGRect] = [
                CGRect(x: 0.0, y: 0.0, width: 1440.0, height: 900.0),       // primary
                CGRect(x: 1440.0, y: -540.0, width: 2560.0, height: 1440.0), // taller right display top-aligned with primary
            ]
            let convRight = cgRectToAppKit(CGRect(x: 1800.0, y: 200.0, width: 1200.0, height: 800.0), primaryScreenMaxY: 900.0)
            try check(convRight == CGRect(x: 1800.0, y: -100.0, width: 1200.0, height: 800.0), "cgRectToAppKit right display")
            // Focused window on secondary screen while mouse stays on primary -> picks secondary (index 1)
            try check(
                selectHudScreenIndex(
                    screenFrames: horizScreens,
                    focusedWindowCgFrame: CGRect(x: 1800.0, y: 200.0, width: 1200.0, height: 800.0),
                    mouseLocation: CGPoint(x: 500.0, y: 400.0),
                    mainScreenIndex: 0
                ) == 1,
                "HUD screen prefers focused window on secondary over mouse on primary"
            )
            // Vertical stack: primary + display above (CG y < 0) + display below (CG y >= primaryH)
            let vertScreens: [CGRect] = [
                CGRect(x: 0.0, y: 0.0, width: 1920.0, height: 1080.0),     // primary
                CGRect(x: 0.0, y: 1080.0, width: 1920.0, height: 1080.0),  // above primary (CG y in -1080..0)
                CGRect(x: 0.0, y: -1080.0, width: 1920.0, height: 1080.0), // below primary (CG y in 1080..2160)
            ]
            try check(
                selectHudScreenIndex(
                    screenFrames: vertScreens,
                    focusedWindowCgFrame: CGRect(x: 200.0, y: -900.0, width: 1000.0, height: 700.0),
                    mouseLocation: CGPoint(x: 500.0, y: 500.0),
                    mainScreenIndex: 0
                ) == 1,
                "HUD screen maps top display CG negative Y to AppKit upper display"
            )
            try check(
                selectHudScreenIndex(
                    screenFrames: vertScreens,
                    focusedWindowCgFrame: CGRect(x: 200.0, y: 1200.0, width: 1000.0, height: 700.0),
                    mouseLocation: CGPoint(x: 500.0, y: 500.0),
                    mainScreenIndex: 0
                ) == 2,
                "HUD screen maps bottom display CG positive Y to AppKit lower display"
            )
            // No focused window frame -> falls back to mouse screen
            try check(
                selectHudScreenIndex(
                    screenFrames: horizScreens,
                    focusedWindowCgFrame: nil,
                    mouseLocation: CGPoint(x: 2000.0, y: 100.0),
                    mainScreenIndex: 0
                ) == 1,
                "HUD screen nil focused frame falls back to mouse screen"
            )
            // Off-screen focused window + out-of-bounds mouse -> falls back to mainScreenIndex then 0
            try check(
                selectHudScreenIndex(
                    screenFrames: horizScreens,
                    focusedWindowCgFrame: CGRect(x: 9000.0, y: 9000.0, width: 400.0, height: 300.0),
                    mouseLocation: CGPoint(x: -500.0, y: -500.0),
                    mainScreenIndex: 1
                ) == 1,
                "HUD screen off-screen window + mouse falls back to mainScreenIndex"
            )
            try check(
                selectHudScreenIndex(
                    screenFrames: horizScreens,
                    focusedWindowCgFrame: nil,
                    mouseLocation: CGPoint(x: -500.0, y: -500.0),
                    mainScreenIndex: nil
                ) == 0,
                "HUD screen falls back to first screen when all signals absent"
            )
            // Straddling two screens: selects screen with larger intersection area
            let sideBySide: [CGRect] = [
                CGRect(x: 0.0, y: 0.0, width: 1440.0, height: 900.0),
                CGRect(x: 1440.0, y: 0.0, width: 1440.0, height: 900.0),
            ]
            try check(
                selectHudScreenIndex(
                    screenFrames: sideBySide,
                    focusedWindowCgFrame: CGRect(x: 1140.0, y: 100.0, width: 1000.0, height: 600.0), // 300px on 0, 700px on 1
                    mouseLocation: CGPoint(x: 100.0, y: 100.0),
                    mainScreenIndex: 0
                ) == 1,
                "HUD screen straddling window picks screen with larger overlap (secondary)"
            )
            try check(
                selectHudScreenIndex(
                    screenFrames: sideBySide,
                    focusedWindowCgFrame: CGRect(x: 740.0, y: 100.0, width: 1000.0, height: 600.0), // 700px on 0, 300px on 1
                    mouseLocation: CGPoint(x: 2000.0, y: 100.0),
                    mainScreenIndex: 1
                ) == 0,
                "HUD screen straddling window picks screen with larger overlap (primary)"
            )
            try check(
                selectHudScreenIndex(screenFrames: [], focusedWindowCgFrame: nil, mouseLocation: .zero) == nil,
                "HUD screen empty screens returns nil"
            )
            // Floating always-on-top Ghostty window on secondary display ahead of normal primary window
            let floatLayer = Int(CGWindowLevelForKey(.floatingWindow))
            let normalLayer = Int(CGWindowLevelForKey(.normalWindow))
            let floatWinRect = CGRect(x: 1800.0, y: 200.0, width: 1200.0, height: 800.0)
            let normalWinRect = CGRect(x: 100.0, y: 100.0, width: 1000.0, height: 700.0)
            let winCandidates: [CgWindowCandidate] = [
                CgWindowCandidate(ownerPid: 999, layer: normalLayer, alpha: 1.0, bounds: normalWinRect),
                CgWindowCandidate(ownerPid: 42, layer: Int(CGWindowLevelForKey(.statusWindow)), alpha: 1.0, bounds: normalWinRect),
                CgWindowCandidate(ownerPid: 42, layer: floatLayer, alpha: 1.0, bounds: CGRect(x: 0.0, y: 0.0, width: 4.0, height: 4.0)),
                CgWindowCandidate(ownerPid: 42, layer: floatLayer, alpha: 0.0, bounds: normalWinRect),
                CgWindowCandidate(ownerPid: 42, layer: floatLayer, alpha: 1.0, bounds: floatWinRect),
                CgWindowCandidate(ownerPid: 42, layer: normalLayer, alpha: 1.0, bounds: normalWinRect),
            ]
            let pickedFloatRect = selectFrontmostWindowCgFrame(from: winCandidates, ownerPid: 42)
            try check(pickedFloatRect == floatWinRect, "floating always-on-top window on secondary preferred over trailing normal window")
            try check(
                selectHudScreenIndex(
                    screenFrames: horizScreens,
                    focusedWindowCgFrame: pickedFloatRect,
                    mouseLocation: CGPoint(x: 500.0, y: 400.0),
                    mainScreenIndex: 0
                ) == 1,
                "HUD screen anchors to floating secondary Ghostty window"
            )
            // Same-PID multi-window cache invalidation: windowId mismatch immediately misses old cached frame
            let testCache = FrontmostWindowFrameCache()
            let nowCacheMs = monotonicMs()
            testCache.storeFrame(pid: 4242, windowId: 101, frame: floatWinRect, nowMs: nowCacheMs)
            try check(testCache.freshFrame(forPid: 4242, windowId: 101, nowMs: nowCacheMs &+ 100) == floatWinRect, "HUD frame cache hits exact (pid, windowId)")
            try check(testCache.freshFrame(forPid: 4242, windowId: 202, nowMs: nowCacheMs &+ 100) == nil, "HUD frame cache invalidates immediately on same-PID different windowId")
            try check(testCache.freshFrame(forPid: 4242, windowId: 0, nowMs: nowCacheMs &+ 100) == nil, "HUD frame cache ignores zero windowId")
        }

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
        try check(stCopiedMain.layer == "main" && stCopiedMain.iconKind == "done" && stCopiedMain.autoHideMs == 5000 && stCopiedMain.bg != stBusy.bg, "style copied main")
        try check(
            stCopiedToast.layer == "toast"
                && stCopiedToast.iconKind == "done"
                && stCopiedToast.autoHideMs == 5000
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
            // Stale NSEvent timestamp guard self-check
            try check(!isStaleKeyEventTimestamp(99.5, systemUptime: 100.0), "fresh event not stale")
            try check(!isStaleKeyEventTimestamp(98.0, systemUptime: 100.0), "boundary 2.0s event not stale")
            try check(isStaleKeyEventTimestamp(97.9, systemUptime: 100.0), "old >2.0s event marked stale")
            try check(!isStaleKeyEventTimestamp(0.0, systemUptime: 100.0), "synthetic zero timestamp not stale")
            try check(!isStaleKeyEventTimestamp(105.0, systemUptime: 100.0), "future timestamp not stale")

            func awaitMainAsync<T>(timeoutSec: TimeInterval = 5.0, _ start: (@escaping (T) -> Void) -> Void) throws -> T {
                var result: T?
                start { val in result = val }
                let limit = Date().addingTimeInterval(timeoutSec)
                while result == nil && Date() < limit {
                    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
                }
                guard let unwrapped = result else {
                    throw NSError(domain: "SelfCheck", code: 3, userInfo: [NSLocalizedDescriptionKey: "awaitMainAsync timed out"])
                }
                return unwrapped
            }

            func captureSync(_ guardObj: PasteboardGuard, deadlineMonoMs: UInt64 = 0) throws -> (Int, PasteboardGuard.ClipSnap?) {
                return try awaitMainAsync { done in
                    guardObj.captureSnapshotAsync(deadlineMonoMs: deadlineMonoMs) { preCc, snap in
                        done((preCc, snap))
                    }
                }
            }

            func linearPasteSync(_ guardObj: PasteboardGuard, _ text: String, deadlineMonoMs: UInt64 = 0, armTimer: Bool = false) throws -> Bool {
                let (preCc, snap) = try captureSync(guardObj, deadlineMonoMs: deadlineMonoMs)
                let effSnap: PasteboardGuard.ClipSnap? = (guardObj.currentChangeCount == preCc) ? snap : nil
                guard guardObj.writeTransient(text, snap: effSnap) else { return false }
                if armTimer {
                    guardObj.armPendingRestoreTimer()
                }
                return true
            }

            try check(PasteboardGuard.defaultMaxSnapshotBytes == 10 * 1024 * 1024, "pb defaultMaxSnapshotBytes 10MiB")
            try check(PasteboardGuard.defaultMaxCaptureMs == 200, "pb defaultMaxCaptureMs 200ms")
            try check(PasteboardGuard.defaultRestoreDelayMs == 250, "pb defaultRestoreDelayMs 250ms")

            // 0) pasteboardItems == nil vs empty array semantics + minimal wire framing round-trip
            if case .readFailed = PasteboardGuard.classifyRawPasteboardItems(nil) {
                // expected: read failure must NEVER map to .clearOnly
            } else {
                try check(false, "pb classifyRawPasteboardItems(nil) must return .readFailed, not .clearOnly")
            }
            if case .clearOnly = PasteboardGuard.classifyRawPasteboardItems([]) {
                // expected: truly empty pasteboard maps to .clearOnly
            } else {
                try check(false, "pb classifyRawPasteboardItems([]) must return .clearOnly")
            }
            let (rtReadFailedHdr, rtReadFailedPayload) = encodePasteboardResponse(outcome: .readFailed)
            try check(rtReadFailedHdr.count == 5 && rtReadFailedPayload.isEmpty && rtReadFailedHdr[0] == kPbStatusNone, "pb wire encode .readFailed")
            if case .some(nil) = decodePasteboardResponse(statusByte: rtReadFailedHdr[0], payload: rtReadFailedPayload) {
                // expected
            } else {
                try check(false, "pb wire decode .readFailed -> nil snap")
            }
            let overCapItems: [[(NSPasteboard.PasteboardType, Data)]] = Array(repeating: [(.string, Data("x".utf8))], count: kPbMaxItems + 1)
            let (overCapHdr, overCapPayload) = encodePasteboardResponse(outcome: .items(overCapItems))
            try check(overCapHdr.count == 5 && overCapPayload.isEmpty && overCapHdr[0] == kPbStatusNone, "pb encoder caps >4096 items symmetrically")
            if case .some(nil) = decodePasteboardResponse(statusByte: overCapHdr[0], payload: overCapPayload) {
                // expected
            } else {
                try check(false, "pb decoder accepts downgraded >4096 item frame")
            }

            let testPb = NSPasteboard.withUniqueName()
            let pbg = PasteboardGuard(pasteboard: testPb, restoreDelayMs: 50, maxSnapshotBytes: 4096, maxCaptureMs: 500)
            defer {
                pbg.shutdown()
                testPb.releaseGlobally()
            }
            try check(pbg.workerSpawnCount == 0, "pb worker is lazily spawned on demand")
            let samplePng = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x01, 0x02])
            let sampleHtml = Data("<b>clip</b>".utf8)

            // 1) Composite PNG + HTML + plain text via single-slot lazy worker + rapid-fire #1 + #2 preserving Image0
            testPb.clearContents()
            let seedItem = NSPasteboardItem()
            _ = seedItem.setData(samplePng, forType: .png)
            _ = seedItem.setData(sampleHtml, forType: .html)
            _ = seedItem.setString("orig_caption", forType: .string)
            try check(testPb.writeObjects([seedItem]), "pb seed composite")
            try check(try linearPasteSync(pbg, "voice_1"), "pb transient #1")
            try check(pbg.restorePending && testPb.string(forType: .string) == "voice_1", "pb #1 armed")
            try check(pbg.workerSpawnCount == 1 && pbg.workerKillCount == 0 && pbg.workerRequestCount == 1, "pb #1 lazily spawned single worker")
            let p1Types = testPb.pasteboardItems?.first?.types ?? []
            try check(p1Types.contains(PasteboardGuard.transientType) && p1Types.contains(PasteboardGuard.autoGeneratedType), "pb #1 transient markers")
            try check(try linearPasteSync(pbg, "voice_2"), "pb transient #2 rapid-fire")
            try check(pbg.restorePending && testPb.string(forType: .string) == "voice_2", "pb #2 armed")
            try check(pbg.workerRequestCount == 1, "pb #2 reused savedSnap synchronously without extra worker IPC")
            try check(pbg.flushPendingRestore(), "pb flush returned true")
            try check(!pbg.restorePending, "pb flushed idle")
            try check(testPb.data(forType: .png) == samplePng && testPb.data(forType: .html) == sampleHtml && testPb.string(forType: .string) == "orig_caption", "pb restored composite Image0")

            // 2) Timer restore succeeds when untouched, and ABORTS if clipboard changes before timer expires
            try check(try linearPasteSync(pbg, "voice_timer_ok", armTimer: true), "pb timer ok write")
            let restoreDeadline = Date().addingTimeInterval(0.5)
            while pbg.restorePending && Date() < restoreDeadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
            try check(!pbg.restorePending && testPb.data(forType: .png) == samplePng && testPb.string(forType: .string) == "orig_caption", "pb timer restored original clipboard")

            try check(try linearPasteSync(pbg, "voice_timer_abort", armTimer: true), "pb timer abort setup")
            testPb.clearContents()
            try check(testPb.setString("user_cmd_c_before_timer", forType: .string), "pb simulate user copy before timer")
            let waitAbortUntil = Date().addingTimeInterval(0.12)
            while Date() < waitAbortUntil {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
            try check(!pbg.restorePending && testPb.string(forType: .string) == "user_cmd_c_before_timer" && testPb.data(forType: .png) == nil, "pb timer aborted restore after changeCount mismatch")

            // 3) Focus-drift / force_clipboard_only persistent write cancels armed restore timer and omits TransientType
            testPb.clearContents()
            let seedImg2 = NSPasteboardItem()
            _ = seedImg2.setData(samplePng, forType: .png)
            _ = testPb.writeObjects([seedImg2])
            try check(try linearPasteSync(pbg, "voice_before_drift", armTimer: true), "pb transient before drift")
            try check(pbg.restorePending, "pb timer armed before drift")
            let expiredFb = pbg.writePersistentFallbackIfWithinDeadline(
                "expired_stale_fallback",
                deadlineMonoMs: 1000,
                nowMs: { 1000 }
            )
            try check(!expiredFb.written && expiredFb.reason == "paste_deadline_exceeded", "pb force_clipboard_only expired deadline refuses write")
            try check(pbg.restorePending && testPb.string(forType: .string) == "voice_before_drift", "pb expired force_clipboard_only left clipboard and timer intact")
            let validFb = pbg.writePersistentFallbackIfWithinDeadline(
                "fallback_keep",
                deadlineMonoMs: 1001,
                nowMs: { 1000 }
            )
            try check(validFb.written && validFb.reason == "clipboard_only", "pb persistent fallback within deadline")
            let fbTypes = testPb.pasteboardItems?.first?.types ?? []
            try check(!pbg.restorePending && !fbTypes.contains(PasteboardGuard.transientType), "pb fallback cancelled timer & omitted transient marker")
            let waitCancelledUntil = Date().addingTimeInterval(0.12)
            while Date() < waitCancelledUntil {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
            try check(testPb.string(forType: .string) == "fallback_keep" && testPb.data(forType: .png) == nil, "pb cancelled timer never overwrote fallback")

            // 3b) Micro-window external Cmd+C right after writeObjects bumps changeCount past clearContents() ->
            //     writeTransient refuses to adopt foreign changeCount, skips arming restore, and returns false so Cmd+V is aborted.
            testPb.clearContents()
            let seedImgMicro = NSPasteboardItem()
            _ = seedImgMicro.setData(samplePng, forType: .png)
            try check(testPb.writeObjects([seedImgMicro]), "pb seed micro-window image")
            let (microPreCc, microSnap) = try captureSync(pbg)
            try check(microSnap != nil && pbg.currentChangeCount == microPreCc, "pb captured micro-window image")
            let microWrote = pbg.writeTransient("voice_micro_window", snap: microSnap) {
                let extPb = NSPasteboard(name: testPb.name)
                extPb.clearContents()
                _ = extPb.setString("user_micro_window_copy", forType: .string)
            }
            try check(!microWrote, "pb micro-window writeTransient returned false")
            pbg.armPendingRestoreTimer()
            try check(!pbg.restorePending, "pb micro-window mismatch skipped restore arm")
            let waitMicroUntil = Date().addingTimeInterval(0.12)
            while Date() < waitMicroUntil {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
            try check(testPb.string(forType: .string) == "user_micro_window_copy" && testPb.data(forType: .png) == nil, "pb micro-window concurrent user copy never overwritten")

            // 4) ConcealedType & external TransientType -> clearOnly; oversize (>maxSnapshotBytes) -> nil snap & still pastes
            testPb.clearContents()
            let pwItem = NSPasteboardItem()
            _ = pwItem.setString("top_secret_pw", forType: .string)
            _ = pwItem.setData(Data(), forType: PasteboardGuard.concealedType)
            _ = testPb.writeObjects([pwItem])
            try check(try linearPasteSync(pbg, "voice_over_concealed"), "pb transient over concealed")
            pbg.flushPendingRestore()
            try check((testPb.pasteboardItems ?? []).isEmpty && testPb.string(forType: .string) == nil, "pb concealed cleared")

            testPb.clearContents()
            let extTrItem = NSPasteboardItem()
            _ = extTrItem.setString("external_ephemeral_snippet", forType: .string)
            _ = extTrItem.setData(Data(), forType: PasteboardGuard.transientType)
            _ = testPb.writeObjects([extTrItem])
            try check(try linearPasteSync(pbg, "voice_over_ext_transient"), "pb transient over external transient")
            pbg.flushPendingRestore()
            try check((testPb.pasteboardItems ?? []).isEmpty && testPb.string(forType: .string) == nil, "pb external transient cleared")

            testPb.clearContents()
            let bigItem = NSPasteboardItem()
            _ = bigItem.setData(Data(repeating: 0xAA, count: 8192), forType: .png)
            _ = testPb.writeObjects([bigItem])
            try check(try linearPasteSync(pbg, "voice_oversize"), "pb transient over oversize")
            try check(!pbg.restorePending && testPb.string(forType: .string) == "voice_oversize", "pb oversize skips restore arm and still pastes")
            try check(try linearPasteSync(pbg, "voice_oversize_2"), "pb transient #2 after oversize")
            try check(!pbg.restorePending && testPb.string(forType: .string) == "voice_oversize_2", "pb oversize burst never snapshots voice_oversize")

            // 5) PNG + TIFF deduplication & TIFF-only fallback
            testPb.clearContents()
            let dualImgItem = NSPasteboardItem()
            let oversizeTiff = Data(repeating: 0xBB, count: 8192)
            try check(dualImgItem.setData(samplePng, forType: .png) && dualImgItem.setData(oversizeTiff, forType: .tiff) && testPb.writeObjects([dualImgItem]), "pb seed PNG+TIFF")
            try check(try linearPasteSync(pbg, "voice_dual_img"), "pb transient over PNG+TIFF")
            try check(pbg.restorePending, "pb PNG+TIFF armed without double-counting oversize TIFF")
            pbg.flushPendingRestore()
            try check(!pbg.restorePending && testPb.data(forType: .png) == samplePng && testPb.data(forType: .tiff) != oversizeTiff, "pb PNG restored without TIFF duplication")

            let sampleTiff = Data([0x49, 0x49, 0x2A, 0x00, 0x08, 0x00, 0x00, 0x00])
            testPb.clearContents()
            let tiffOnlyItem = NSPasteboardItem()
            try check(tiffOnlyItem.setData(sampleTiff, forType: .tiff) && testPb.writeObjects([tiffOnlyItem]), "pb seed TIFF-only")
            try check(try linearPasteSync(pbg, "voice_tiff_only"), "pb transient over TIFF-only")
            pbg.flushPendingRestore()
            try check(!pbg.restorePending && testPb.data(forType: .tiff) == sampleTiff, "pb TIFF-only fallback restored")
            try check(pbg.workerSpawnCount == 1 && pbg.workerKillCount == 0, "pb reused single lazy worker across all normal tests")

            // 6) postEventsIfWithinDeadline + resolveClaudeWebAreaDeadlineMs pure helpers
            var postedExpiredCount = 0
            let expiredOk = postEventsIfWithinDeadline(
                events: [0x37, 0x09, 0x09, 0x37],
                deadlineMonoMs: 1000,
                nowMs: { 1000 },
                postEvent: { _ in postedExpiredCount += 1 }
            )
            try check(!expiredOk && postedExpiredCount == 0, "pre-keystroke expired deadline posts zero keyboard events")
            var postedValidCount = 0
            let validOk = postEventsIfWithinDeadline(
                events: [0x37, 0x09, 0x09, 0x37],
                deadlineMonoMs: 1001,
                nowMs: { 1000 },
                postEvent: { _ in postedValidCount += 1 }
            )
            try check(validOk && postedValidCount == 4, "pre-keystroke valid deadline posts all 4 keyboard events")
            try check(
                resolveClaudeWebAreaDeadlineMs(nowMs: 1000, budgetMs: 80, outerDeadlineMonoMs: 1180) == 1080,
                "claude web area deadline caps at own 80ms budget when outer deadline is looser"
            )
            try check(
                resolveClaudeWebAreaDeadlineMs(nowMs: 1000, budgetMs: 80, outerDeadlineMonoMs: 1035) == 1035,
                "claude web area deadline honors tighter outer 200ms deadline"
            )

            // 7) Cross-process stalled Promise (simulating 60s RDP hang):
            //    200ms hard timeout -> SIGKILL + waitpid reap (0 zombie) -> voice paste proceeds with nil snap ->
            //    next request lazily respawns worker and restores healthy clipboard.
            func spawnPromiseProvider(pbName: String, delayMs: Int, text: String) throws -> (pid: pid_t, inFd: Int32, cc: Int) {
                var inPipe: [Int32] = [-1, -1]
                var outPipe: [Int32] = [-1, -1]
                guard Darwin.pipe(&inPipe) == 0 else {
                    throw NSError(domain: "SelfCheck", code: 20, userInfo: [NSLocalizedDescriptionKey: "provider pipe failed"])
                }
                guard Darwin.pipe(&outPipe) == 0 else {
                    _ = Darwin.close(inPipe[0]); _ = Darwin.close(inPipe[1])
                    throw NSError(domain: "SelfCheck", code: 20, userInfo: [NSLocalizedDescriptionKey: "provider out pipe failed"])
                }
                setCloseOnExec(inPipe[0]); setCloseOnExec(inPipe[1])
                setCloseOnExec(outPipe[0]); setCloseOnExec(outPipe[1])
                let pidOpt = spawnChildProcess(
                    executablePath: resolveCurrentExecutablePath(),
                    arguments: [
                        "--self-check-promise-provider",
                        "--pasteboard-name", pbName,
                        "--delay-ms", "\(delayMs)",
                        "--text", text,
                    ],
                    stdinReadFd: inPipe[0],
                    stdoutWriteFd: outPipe[1]
                )
                _ = Darwin.close(inPipe[0])
                _ = Darwin.close(outPipe[1])
                guard let childPid = pidOpt else {
                    _ = Darwin.close(inPipe[1]); _ = Darwin.close(outPipe[0])
                    throw NSError(domain: "SelfCheck", code: 21, userInfo: [NSLocalizedDescriptionKey: "spawn provider failed"])
                }
                let readyLine = readLineWithDeadline(fd: outPipe[0], deadlineMonoMs: monotonicMs() &+ 3000) ?? ""
                _ = Darwin.close(outPipe[0])
                let parts = readyLine.trimmingCharacters(in: .whitespacesAndNewlines).split(separator: " ")
                guard parts.count == 2, parts[0] == "READY", let cc = Int(parts[1]) else {
                    _ = Darwin.kill(childPid, SIGKILL)
                    _ = Darwin.close(inPipe[1])
                    var st: Int32 = 0
                    _ = Darwin.waitpid(childPid, &st, 0)
                    throw NSError(domain: "SelfCheck", code: 22, userInfo: [NSLocalizedDescriptionKey: "provider did not emit READY (got \(readyLine))"])
                }
                return (childPid, inPipe[1], cc)
            }

            func stopPromiseProvider(_ prov: (pid: pid_t, inFd: Int32, cc: Int)) {
                _ = Darwin.kill(prov.pid, SIGKILL)
                _ = Darwin.close(prov.inFd)
                var st: Int32 = 0
                while true {
                    let w = Darwin.waitpid(prov.pid, &st, 0)
                    if w < 0 && errno == EINTR { continue }
                    break
                }
            }

            func waitPidReaped(_ pid: pid_t, timeoutSec: TimeInterval = 1.5) -> Bool {
                guard pid > 0 else { return true }
                let limit = Date().addingTimeInterval(timeoutSec)
                while Date() < limit {
                    if Darwin.kill(pid, 0) != 0 && errno == ESRCH {
                        return true
                    }
                    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
                }
                return Darwin.kill(pid, 0) != 0 && errno == ESRCH
            }

            let promisePbNameStr = "avi.selfcheck.\(ProcessInfo.processInfo.processIdentifier).\(monotonicMs())"
            let promisePb = NSPasteboard(name: NSPasteboard.Name(promisePbNameStr))
            let promiseGuard = PasteboardGuard(pasteboard: promisePb, restoreDelayMs: 40, maxSnapshotBytes: 4096, maxCaptureMs: 200)
            defer {
                promiseGuard.shutdown()
                promisePb.releaseGlobally()
            }
            // First healthy read lazily spawns workerPid1
            promisePb.clearContents()
            _ = promisePb.setString("warmup_ok", forType: .string)
            let (_, warmSnap) = try captureSync(promiseGuard, deadlineMonoMs: monotonicMs() &+ 2000)
            try check(warmSnap != nil && promiseGuard.workerSpawnCount == 1, "pb lazy worker warmup succeeded")
            let workerPid1 = promiseGuard.currentWorkerPid
            try check(workerPid1 > 0, "pb workerPid1 active")

            // 7a) Stalled 2s Promise hits 200ms hard timeout -> SIGKILL + waitpid reap -> voice paste still proceeds cleanly
            let provStall = try spawnPromiseProvider(pbName: promisePbNameStr, delayMs: 2000, text: "stalled_rdp")
            defer { stopPromiseProvider(provStall) }
            try check(promisePb.changeCount == provStall.cc, "pb stalled promise changeCount matches")
            let t0 = monotonicMs()
            let (stallPreCc, stallSnap) = try captureSync(promiseGuard, deadlineMonoMs: t0 &+ 1250)
            let elapsedStall = monotonicMs() &- t0
            try check(stallSnap == nil, "pb stalled promise returned nil snapshot")
            try check(promiseGuard.workerKillCount == 1, "pb stalled promise killed worker once (got \(promiseGuard.workerKillCount))")
            try check(waitPidReaped(workerPid1), "pb stalled workerPid1 SIGKILLed and reaped (zero zombie)")
            try check(elapsedStall < 1000, "pb stalled promise hard timeout well below 2s stall (got \(elapsedStall)ms)")
            try check(promiseGuard.currentChangeCount == stallPreCc, "pb stalled promise left clipboard intact until write")
            try check(promiseGuard.writeTransient("voice_after_stalled_promise", snap: stallSnap), "pb paste succeeded after stalled promise")
            try check(!promiseGuard.restorePending && promisePb.string(forType: .string) == "voice_after_stalled_promise", "pb voice text landed without restore arm")

            // 7b) Next healthy copy lazily respawns workerPid2 and restores intact
            promisePb.clearContents()
            try check(promisePb.setString("local_healthy_after_stall", forType: .string), "pb seed healthy copy after stall")
            try check(try linearPasteSync(promiseGuard, "voice_after_respawn", deadlineMonoMs: monotonicMs() &+ 2000), "pb healthy paste after stall")
            let workerPid2 = promiseGuard.currentWorkerPid
            try check(promiseGuard.workerSpawnCount == 2 && workerPid2 > 0 && workerPid2 != workerPid1, "pb lazily respawned workerPid2")
            try check(promiseGuard.restorePending && promisePb.string(forType: .string) == "voice_after_respawn", "pb respawned worker armed restore")
            try check(promiseGuard.flushPendingRestore() && promisePb.string(forType: .string) == "local_healthy_after_stall", "pb restored healthy copy after stall")

            // 8) Mid-read changeCount change (user Cmd+C during worker read):
            //    NO recapture, NO paste refusal; drops stale snapshot so stale data never overwrites user's new copy.
            let provMid = try spawnPromiseProvider(pbName: promisePbNameStr, delayMs: 80, text: "old_slow_clip")
            defer { stopPromiseProvider(provMid) }
            var midCaptured: (Int, PasteboardGuard.ClipSnap?)?
            promiseGuard.captureSnapshotAsync(deadlineMonoMs: monotonicMs() &+ 1500) { preCc, snap in
                midCaptured = (preCc, snap)
            }
            let copyAt = Date().addingTimeInterval(0.020)
            while Date() < copyAt {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
            promisePb.clearContents()
            try check(promisePb.setString("user_fresh_mid_copy", forType: .string), "pb simulate mid-read user Cmd+C")
            let midLimit = Date().addingTimeInterval(2.0)
            while midCaptured == nil && Date() < midLimit {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
            guard let (midPreCc, midSnap) = midCaptured else {
                try check(false, "pb mid-read capture timed out")
                return 1
            }
            try check(promiseGuard.currentChangeCount != midPreCc, "pb detected mid-read changeCount mismatch")
            let midEffectiveSnap: PasteboardGuard.ClipSnap? = (promiseGuard.currentChangeCount == midPreCc) ? midSnap : nil
            try check(midEffectiveSnap == nil, "pb dropped stale snapshot on mid-read changeCount mismatch")
            try check(promiseGuard.writeTransient("voice_over_mid_copy", snap: midEffectiveSnap), "pb voice paste still succeeded on mid-read change")
            promiseGuard.armPendingRestoreTimer()
            try check(!promiseGuard.restorePending && promisePb.string(forType: .string) == "voice_over_mid_copy", "pb skipped restore so stale old_slow_clip is never restored")

            // 9) Pinned trigger windowId mismatch & pinned==0 (no trigger-time window) both discard drifted window B metadata
            let driftedMeta = queryFrontWindowMetadata(
                pid: 4242,
                bundleId: "com.example.editor",
                pinnedWindowId: 101,
                liveWindowNumberProvider: { _ in 202 }
            )
            try check(driftedMeta.hwnd == 101 && driftedMeta.title.isEmpty && driftedMeta.document.isEmpty, "pinned windowId drift discards window B title/document")
            let zeroPinnedMeta = queryFrontWindowMetadata(
                pid: 4242,
                bundleId: "com.example.editor",
                pinnedWindowId: 0,
                liveWindowNumberProvider: { _ in 303 }
            )
            try check(zeroPinnedMeta.hwnd == 0 && zeroPinnedMeta.title.isEmpty && zeroPinnedMeta.document.isEmpty, "pinned windowId == 0 never backfills later window B")

            // 10) Hotkey AX gating + Claude Desktop AXWebArea title & URL probe gating, sanitization, shell descent & candidate selection
            try check(shouldProbeAxForEventKind("ptt_start") && shouldProbeAxForEventKind("toggle"), "start/toggle events allow AX probe")
            try check(!shouldProbeAxForEventKind("ptt_stop") && !shouldProbeAxForEventKind("ptt_cancel") && !shouldProbeAxForEventKind("hold_cancel"), "stop/cancel events skip AX probe")
            try check(shouldProbeWebAreaForEventKind("ptt_start") && shouldProbeWebAreaForEventKind("toggle"), "start/toggle events allow deep web area probe")
            try check(!shouldProbeWebAreaForEventKind("ptt_stop") && !shouldProbeWebAreaForEventKind("ptt_cancel") && !shouldProbeWebAreaForEventKind("hold_cancel"), "stop/cancel events skip deep web area probe")
            try check(shouldProbeWebAreaTitle(bundleId: "com.anthropic.claudefordesktop", windowTitle: "", allowDeepProbe: true), "claude desktop allows deep web area probe")
            try check(!shouldProbeWebAreaTitle(bundleId: "com.anthropic.claudefordesktop", windowTitle: "Claude", allowDeepProbe: false), "claude desktop respects allowDeepProbe=false")
            try check(!shouldProbeWebAreaTitle(bundleId: "com.openai.codex", windowTitle: "Claude", allowDeepProbe: true), "non-claude bundleId skips web area probe")
            let sampleUuid = "11111111-2222-4333-8444-555555555555"
            try check(isClaudeShellWebArea(urlRaw: "file:///Applications/Claude.app/Contents/Resources/main_window/index.html", title: ""), "file:// main_window shell AXWebArea allows descent")
            try check(!isClaudeShellWebArea(urlRaw: "app://localhost/cowork/local_\(sampleUuid)", title: "Weekly Plan - Claude"), "app:// content AXWebArea blocks internal DOM descent")
            try check(sanitizeWebAreaUrl("app://LOCALHOST/cowork/local_\(sampleUuid)?token=secret#frag") == "app://localhost/cowork/local_\(sampleUuid)", "sanitizeWebAreaUrl strips query/fragment and lowercases trusted scheme/host")
            try check(sanitizeWebAreaUrl("app://user:pass@LOCALHOST/cowork/local_\(sampleUuid)?token=secret#frag") == kRejectedWebAreaUrl, "sanitizeWebAreaUrl rejects userinfo instead of laundering to localhost")
            try check(sanitizeWebAreaUrl("app://@localhost/epitaxy/local_\(sampleUuid)") == kRejectedWebAreaUrl, "sanitizeWebAreaUrl rejects bare @ userinfo")
            try check(sanitizeWebAreaUrl("app://localhost:8080/epitaxy/local_\(sampleUuid)") == kRejectedWebAreaUrl, "sanitizeWebAreaUrl rejects port as non-session sentinel")
            try check(sanitizeWebAreaUrl("https://evil.example/code/local_\(sampleUuid)") == kRejectedWebAreaUrl, "sanitizeWebAreaUrl rejects untrusted host as non-session sentinel")
            try check(sanitizeWebAreaUrl("file:///etc/passwd").isEmpty, "sanitizeWebAreaUrl skips file:// shell URLs")
            try check(sanitizeWebAreaUrl("app://localhost/new\nInjected") == kRejectedWebAreaUrl, "sanitizeWebAreaUrl rejects control chars as non-session sentinel")
            try check(isClaudeSessionRouteUrl("app://localhost/cowork/local_\(sampleUuid)"), "isClaudeSessionRouteUrl accepts app://localhost cowork UUID")
            try check(isClaudeSessionRouteUrl("app://localhost/epitaxy/local_\(sampleUuid)"), "isClaudeSessionRouteUrl accepts app://localhost epitaxy UUID")
            try check(isClaudeSessionRouteUrl("https://claude.ai/code/local_\(sampleUuid)"), "isClaudeSessionRouteUrl accepts https://claude.ai code UUID")
            try check(!isClaudeSessionRouteUrl("app://evil.local/cowork/local_\(sampleUuid)"), "isClaudeSessionRouteUrl rejects untrusted app host")
            try check(!isClaudeSessionRouteUrl("app://localhost:8080/cowork/local_\(sampleUuid)"), "isClaudeSessionRouteUrl rejects port on localhost")
            try check(!isClaudeSessionRouteUrl("https://evil.example/cowork/local_\(sampleUuid)"), "isClaudeSessionRouteUrl rejects untrusted https host")
            try check(!isClaudeSessionRouteUrl("https://claude.ai:8443/cowork/local_\(sampleUuid)"), "isClaudeSessionRouteUrl rejects port on claude.ai")
            try check(!isClaudeSessionRouteUrl("app://localhost/cowork/local_short"), "isClaudeSessionRouteUrl rejects non-UUID local id")
            try check(!isClaudeSessionRouteUrl("app://localhost/cowork/local_\(sampleUuid)/extra"), "isClaudeSessionRouteUrl rejects trailing path segments")
            try check(!isClaudeSessionRouteUrl("app://localhost/prefix/cowork/local_\(sampleUuid)"), "isClaudeSessionRouteUrl rejects prefixed path segments")
            try check(!isClaudeSessionRouteUrl(kRejectedWebAreaUrl), "isClaudeSessionRouteUrl rejects about:rejected sentinel")
            try check(sanitizeWebAreaTitle("Line1\nLine2 - Claude").isEmpty, "sanitizeWebAreaTitle rejects control chars")
            try check(sanitizeWebAreaTitle(String(repeating: "a", count: 600)).isEmpty, "sanitizeWebAreaTitle rejects oversized title > 512 chars")
            try check(selectPreferredWebAreaTitle(["", "Claude", "  claude  "]).isEmpty, "selectPreferredWebAreaTitle ignores empty and generic Claude")
            try check(selectPreferredWebAreaTitle(["Sidebar", "Refactor Auth - Claude Code", "Other - Claude"]) == "Refactor Auth - Claude Code", "selectPreferredWebAreaTitle prefers Claude Code suffix over plain title")
            try check(selectPreferredWebAreaTitle(["Sidebar", "Weekly Plan \u{2014} Claude"]) == "Weekly Plan \u{2014} Claude", "selectPreferredWebAreaTitle prefers em-dash Claude suffix")
            try check(selectPreferredWebAreaTitle([" - Claude", "Fallback Web Title"]) == "Fallback Web Title", "selectPreferredWebAreaTitle ignores empty-prefix suffix and falls back")
            let selNested = selectPreferredWebAreaResult([
                ("", ""),
                ("Shell", ""),
                ("Spoof - Claude", "https://evil.example/cowork/local_\(sampleUuid)"),
                ("Weekly Plan - Claude", "app://localhost/cowork/local_\(sampleUuid)"),
            ])
            try check(selNested.title == "Weekly Plan - Claude" && selNested.pageUrl == "app://localhost/cowork/local_\(sampleUuid)", "selectPreferredWebAreaResult picks trusted app:// session WebArea over untrusted host")
            let selTrustedOverUntrusted = selectPreferredWebAreaResult([
                ("Spoof - Claude", "app://evil.host/cowork/local_\(sampleUuid)"),
                ("Claude", "app://localhost/new"),
            ])
            try check(selTrustedOverUntrusted.pageUrl == "app://localhost/new", "selectPreferredWebAreaResult prefers trusted app://localhost/new over untrusted app:// host")
            let selUserinfoOnly = selectPreferredWebAreaResult([
                ("Refactor Auth - Claude Code", "app://user:pass@localhost/epitaxy/local_\(sampleUuid)"),
            ])
            try check(selUserinfoOnly.pageUrl == kRejectedWebAreaUrl, "selectPreferredWebAreaResult emits non-empty about:rejected for userinfo-only WebArea so Python stays fail-closed")
            let selBlank = selectPreferredWebAreaResult([
                ("Claude", "app://localhost/epitaxy"),
            ])
            try check(selBlank.title == "Claude" && selBlank.pageUrl == "app://localhost/epitaxy", "selectPreferredWebAreaResult preserves blank app:// route for Python fail-closed check")
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
