import AppKit
import Foundation
import MachO

private let kPbReadyByte: UInt8 = 0x52 // 'R'
private let kPbStatusItems: UInt8 = 0
private let kPbStatusClearOnly: UInt8 = 1
let kPbStatusNone: UInt8 = 2
let kPbMaxItems = 4096
private let kPbMaxEntriesPerItem = 64

func resolveCurrentExecutablePath() -> String {
    var bufSize: UInt32 = 0
    _ = _NSGetExecutablePath(nil, &bufSize)
    if bufSize > 0 {
        var buf = [CChar](repeating: 0, count: Int(bufSize))
        if _NSGetExecutablePath(&buf, &bufSize) == 0 {
            let rawPath = String(cString: buf)
            if let resolved = realpath(rawPath, nil) {
                defer { free(resolved) }
                return String(cString: resolved)
            }
            return rawPath
        }
    }
    let arg0 = CommandLine.arguments.first ?? ""
    if !arg0.isEmpty, let resolved = realpath(arg0, nil) {
        defer { free(resolved) }
        return String(cString: resolved)
    }
    return arg0
}

private func appendBigEndianUInt32(_ data: inout Data, _ value: UInt32) {
    var be = value.bigEndian
    withUnsafeBytes(of: &be) { data.append(contentsOf: $0) }
}

private func readBigEndianUInt32(_ data: Data, at offset: Int) -> UInt32? {
    guard offset >= 0, offset + 4 <= data.count else { return nil }
    let b0 = UInt32(data[data.startIndex + offset]) << 24
    let b1 = UInt32(data[data.startIndex + offset + 1]) << 16
    let b2 = UInt32(data[data.startIndex + offset + 2]) << 8
    let b3 = UInt32(data[data.startIndex + offset + 3])
    return b0 | b1 | b2 | b3
}

private func readExactWithDeadline(fd: Int32, count: Int, deadlineMonoMs: UInt64) -> Data? {
    if count == 0 { return Data() }
    guard count > 0 else { return nil }
    var buf = Data(count: count)
    var offset = 0
    let ok = buf.withUnsafeMutableBytes { rawBuf -> Bool in
        guard let base = rawBuf.baseAddress else { return false }
        while offset < count {
            if deadlineMonoMs > 0 {
                let now = monotonicMs()
                if now >= deadlineMonoMs { return false }
                let remMs = min(deadlineMonoMs &- now, UInt64(Int32.max))
                var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
                let pr = Darwin.poll(&pfd, 1, Int32(remMs))
                if pr == 0 { return false }
                if pr < 0 {
                    if errno == EINTR { continue }
                    return false
                }
            }
            let n = Darwin.read(fd, base.advanced(by: offset), count - offset)
            if n > 0 {
                offset += n
            } else if n == 0 {
                return false
            } else if errno == EINTR {
                continue
            } else {
                return false
            }
        }
        return true
    }
    return ok ? buf : nil
}

private func readExact(fd: Int32, count: Int) -> Data? {
    readExactWithDeadline(fd: fd, count: count, deadlineMonoMs: 0)
}

func readLineWithDeadline(fd: Int32, deadlineMonoMs: UInt64, maxBytes: Int = 4096) -> String? {
    var bytes: [UInt8] = []
    var ch: UInt8 = 0
    while bytes.count < maxBytes {
        if deadlineMonoMs > 0 {
            let now = monotonicMs()
            if now >= deadlineMonoMs { return nil }
            let remMs = min(deadlineMonoMs &- now, UInt64(Int32.max))
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let pr = Darwin.poll(&pfd, 1, Int32(remMs))
            if pr == 0 { return nil }
            if pr < 0 {
                if errno == EINTR { continue }
                return nil
            }
        }
        let n = Darwin.read(fd, &ch, 1)
        if n == 1 {
            if ch == 0x0A { break }
            bytes.append(ch)
        } else if n < 0 && errno == EINTR {
            continue
        } else {
            break
        }
    }
    return String(bytes: bytes, encoding: .utf8)
}

func installParentExitWatchdog(parentPid: pid_t) -> DispatchSourceProcess? {
    if parentPid <= 1 || getppid() != parentPid {
        Darwin._exit(0)
    }
    let src = DispatchSource.makeProcessSource(
        identifier: parentPid,
        eventMask: .exit,
        queue: .global(qos: .utility)
    )
    src.setEventHandler {
        Darwin._exit(0)
    }
    src.resume()
    // Re-check after arming to close the race where parent exited before registration.
    if getppid() != parentPid {
        Darwin._exit(0)
    }
    return src
}

private func writeAll(fd: Int32, data: Data) -> Bool {
    if data.isEmpty { return true }
    var offset = 0
    let total = data.count
    return data.withUnsafeBytes { rawBuf -> Bool in
        guard let base = rawBuf.baseAddress else { return false }
        while offset < total {
            let n = Darwin.write(fd, base.advanced(by: offset), total - offset)
            if n > 0 {
                offset += n
            } else if n < 0 && errno == EINTR {
                continue
            } else {
                return false
            }
        }
        return true
    }
}

func setCloseOnExec(_ fd: Int32) {
    let flags = fcntl(fd, F_GETFD)
    if flags >= 0 {
        _ = fcntl(fd, F_SETFD, flags | FD_CLOEXEC)
    }
}

func encodePasteboardResponse(
    outcome: PasteboardGuard.ReadOnlyCaptureOutcome
) -> (header: Data, payload: Data) {
    let statusByte: UInt8
    var payload = Data()
    switch outcome {
    case .items(let itemList):
        let withinLimits = itemList.count <= kPbMaxItems
            && !itemList.contains(where: { $0.count > kPbMaxEntriesPerItem })
        if !withinLimits {
            statusByte = kPbStatusNone
        } else {
            statusByte = kPbStatusItems
            var payloadBytes = 4
            for entries in itemList {
                payloadBytes += 4
                for (pbType, rawData) in entries {
                    payloadBytes += 4 + pbType.rawValue.utf8.count + 4 + rawData.count
                }
            }
            payload.reserveCapacity(payloadBytes)
            appendBigEndianUInt32(&payload, UInt32(itemList.count))
            for entries in itemList {
                appendBigEndianUInt32(&payload, UInt32(entries.count))
                for (pbType, rawData) in entries {
                    let typeUtf8 = pbType.rawValue.utf8
                    appendBigEndianUInt32(&payload, UInt32(typeUtf8.count))
                    payload.append(contentsOf: typeUtf8)
                    appendBigEndianUInt32(&payload, UInt32(rawData.count))
                    payload.append(rawData)
                }
            }
        }
    case .clearOnly:
        statusByte = kPbStatusClearOnly
    case .readFailed, .ownTransient, .unpreserved:
        statusByte = kPbStatusNone
    }
    var header = Data()
    header.reserveCapacity(5)
    header.append(statusByte)
    appendBigEndianUInt32(&header, UInt32(payload.count))
    return (header, payload)
}

func decodePasteboardResponse(
    statusByte: UInt8,
    payload: Data
) -> PasteboardGuard.ClipSnap?? {
    switch statusByte {
    case kPbStatusClearOnly:
        return .some(.clearOnly)
    case kPbStatusNone:
        return .some(nil)
    case kPbStatusItems:
        var offset = 0
        guard let itemCount = readBigEndianUInt32(payload, at: offset), itemCount <= UInt32(kPbMaxItems) else {
            return nil
        }
        offset += 4
        var items: [[(NSPasteboard.PasteboardType, Data)]] = []
        items.reserveCapacity(Int(itemCount))
        for _ in 0..<itemCount {
            guard let entryCount = readBigEndianUInt32(payload, at: offset), entryCount <= UInt32(kPbMaxEntriesPerItem) else {
                return nil
            }
            offset += 4
            var entries: [(NSPasteboard.PasteboardType, Data)] = []
            entries.reserveCapacity(Int(entryCount))
            for _ in 0..<entryCount {
                guard let typeLen = readBigEndianUInt32(payload, at: offset) else { return nil }
                offset += 4
                let tLen = Int(typeLen)
                guard tLen > 0, tLen <= 512, offset + tLen <= payload.count else { return nil }
                let typeSlice = payload.subdata(in: offset..<(offset + tLen))
                offset += tLen
                guard let typeStr = String(data: typeSlice, encoding: .utf8), !typeStr.isEmpty else { return nil }
                guard let dataLen = readBigEndianUInt32(payload, at: offset) else { return nil }
                offset += 4
                let dLen = Int(dataLen)
                guard dLen >= 0, offset + dLen <= payload.count else { return nil }
                let rawData = payload.subdata(in: offset..<(offset + dLen))
                offset += dLen
                entries.append((NSPasteboard.PasteboardType(typeStr), rawData))
            }
            items.append(entries)
        }
        guard offset == payload.count else { return nil }
        return .some(.items(items))
    default:
        return nil
    }
}

func spawnChildProcess(
    executablePath: String,
    arguments: [String],
    stdinReadFd: Int32?,
    stdoutWriteFd: Int32?
) -> pid_t? {
    var fileActions: posix_spawn_file_actions_t? = nil
    guard posix_spawn_file_actions_init(&fileActions) == 0 else { return nil }
    defer { posix_spawn_file_actions_destroy(&fileActions) }
    if let inFd = stdinReadFd {
        guard posix_spawn_file_actions_adddup2(&fileActions, inFd, STDIN_FILENO) == 0 else { return nil }
    }
    if let outFd = stdoutWriteFd {
        guard posix_spawn_file_actions_adddup2(&fileActions, outFd, STDOUT_FILENO) == 0 else { return nil }
    }

    let fullArgs = [executablePath] + arguments
    var argvPtrs: [UnsafeMutablePointer<CChar>?] = fullArgs.map { strdup($0) }
    argvPtrs.append(nil)
    defer {
        for ptr in argvPtrs { if let p = ptr { free(p) } }
    }

    let envStrings = ProcessInfo.processInfo.environment.map { "\($0.key)=\($0.value)" }
    var envpPtrs: [UnsafeMutablePointer<CChar>?] = envStrings.map { strdup($0) }
    envpPtrs.append(nil)
    defer {
        for ptr in envpPtrs { if let p = ptr { free(p) } }
    }

    var childPid: pid_t = 0
    let rc = executablePath.withCString { exeC in
        posix_spawn(&childPid, exeC, &fileActions, nil, &argvPtrs, &envpPtrs)
    }
    guard rc == 0, childPid > 0 else { return nil }
    return childPid
}

private final class PasteboardWorkerHandle {
    let pid: pid_t
    let inFd: Int32
    let outFd: Int32
    private let lock = NSLock()
    private var killed = false
    private var fdsClosed = false
    private var reaped = false

    init(pid: pid_t, inFd: Int32, outFd: Int32) {
        self.pid = pid
        self.inFd = inFd
        self.outFd = outFd
    }

    func sigkill() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        // Send SIGKILL inside lock while !reaped so waitpid cannot allow OS PID reuse beforehand.
        if reaped || killed { return false }
        killed = true
        let targetPid = pid
        if targetPid > 0 {
            _ = Darwin.kill(targetPid, SIGKILL)
        }
        return true
    }

    func closeAndReapOnIoQueue(forceKill: Bool) -> Bool {
        let didKill = forceKill ? sigkill() : false
        lock.lock()
        let needClose = !fdsClosed
        if needClose { fdsClosed = true }
        let needReap = !reaped
        if needReap { reaped = true }
        let targetPid = pid
        let fdIn = inFd
        let fdOut = outFd
        lock.unlock()

        if needClose {
            if fdIn >= 0 { _ = Darwin.close(fdIn) }
            if fdOut >= 0 { _ = Darwin.close(fdOut) }
        }
        if needReap && targetPid > 0 {
            var status: Int32 = 0
            while true {
                let w = Darwin.waitpid(targetPid, &status, 0)
                if w < 0 && errno == EINTR { continue }
                break
            }
        }
        return didKill
    }
}

// Single-slot lazy persistent pasteboard reader subprocess:
// - Lazily spawned on first snapshot request; reused across normal requests (~4-8ms roundtrip).
// - Hard read timeout (default 200ms): immediately SIGKILL + waitpid reaps hung RDP/lazy Promise
//   readers and respawns on demand on the next request.
private final class PasteboardWorkerClient {
    private let pasteboardNameArg: String?
    private let executablePath: String
    private let ioQueue: DispatchQueue
    private let stateLock = NSLock()
    private var worker: PasteboardWorkerHandle?
    private var isShutdown = false
    private var _spawnCount = 0
    private var _killCount = 0
    private var _requestCount = 0

    init(pasteboardName: NSPasteboard.Name?) {
        if let name = pasteboardName, name != NSPasteboard.general.name {
            self.pasteboardNameArg = name.rawValue
        } else {
            self.pasteboardNameArg = nil
        }
        self.executablePath = resolveCurrentExecutablePath()
        self.ioQueue = DispatchQueue(label: "avi.pasteboard.worker_io", qos: .userInitiated)
    }

    var workerSpawnCount: Int { stateLock.lock(); defer { stateLock.unlock() }; return _spawnCount }
    var workerKillCount: Int { stateLock.lock(); defer { stateLock.unlock() }; return _killCount }
    var requestCount: Int { stateLock.lock(); defer { stateLock.unlock() }; return _requestCount }
    var currentWorkerPid: pid_t { stateLock.lock(); defer { stateLock.unlock() }; return worker?.pid ?? 0 }

    private func killAndReapCurrentOnIoQueue(_ handle: PasteboardWorkerHandle) {
        stateLock.lock()
        if worker === handle { worker = nil }
        stateLock.unlock()
        if handle.closeAndReapOnIoQueue(forceKill: true) {
            stateLock.lock()
            _killCount += 1
            stateLock.unlock()
        }
    }

    private func ensureWorkerOnIoQueue(readyDeadlineMonoMs: UInt64) -> PasteboardWorkerHandle? {
        stateLock.lock()
        if isShutdown {
            stateLock.unlock()
            return nil
        }
        if let existing = worker {
            stateLock.unlock()
            return existing
        }
        stateLock.unlock()
        if executablePath.isEmpty { return nil }

        var inPipe: [Int32] = [-1, -1]
        var outPipe: [Int32] = [-1, -1]
        guard Darwin.pipe(&inPipe) == 0 else { return nil }
        guard Darwin.pipe(&outPipe) == 0 else {
            _ = Darwin.close(inPipe[0]); _ = Darwin.close(inPipe[1])
            return nil
        }
        setCloseOnExec(inPipe[0]); setCloseOnExec(inPipe[1])
        setCloseOnExec(outPipe[0]); setCloseOnExec(outPipe[1])

        var args = ["--pasteboard-worker"]
        if let pbName = pasteboardNameArg {
            args.append("--pasteboard-name")
            args.append(pbName)
        }
        let childPid = spawnChildProcess(
            executablePath: executablePath,
            arguments: args,
            stdinReadFd: inPipe[0],
            stdoutWriteFd: outPipe[1]
        )
        _ = Darwin.close(inPipe[0])
        _ = Darwin.close(outPipe[1])
        guard let pid = childPid else {
            _ = Darwin.close(inPipe[1])
            _ = Darwin.close(outPipe[0])
            return nil
        }

        let handle = PasteboardWorkerHandle(pid: pid, inFd: inPipe[1], outFd: outPipe[0])
        stateLock.lock()
        if isShutdown {
            stateLock.unlock()
            _ = handle.closeAndReapOnIoQueue(forceKill: true)
            return nil
        }
        worker = handle
        _spawnCount += 1
        stateLock.unlock()

        guard let ready = readExactWithDeadline(fd: handle.outFd, count: 1, deadlineMonoMs: readyDeadlineMonoMs),
              ready.first == kPbReadyByte else {
            killAndReapCurrentOnIoQueue(handle)
            return nil
        }
        return handle
    }

    func requestSnapshotAsync(
        maxSnapshotBytes: Int,
        readTimeoutMs: UInt64,
        overallDeadlineMonoMs: UInt64,
        completion: @escaping (PasteboardGuard.ClipSnap?) -> Void
    ) {
        ioQueue.async { [weak self] in
            guard let self = self else {
                completion(nil)
                return
            }
            let nowStart = monotonicMs()
            if overallDeadlineMonoMs > 0 && nowStart >= overallDeadlineMonoMs {
                completion(nil)
                return
            }
            let readyDeadline = (overallDeadlineMonoMs > 0)
                ? min(overallDeadlineMonoMs, nowStart &+ 1000)
                : (nowStart &+ 1500)
            guard let handle = self.ensureWorkerOnIoQueue(readyDeadlineMonoMs: readyDeadline) else {
                completion(nil)
                return
            }

            let nowReq = monotonicMs()
            let readDeadline: UInt64
            if overallDeadlineMonoMs > 0 {
                if nowReq >= overallDeadlineMonoMs {
                    completion(nil)
                    return
                }
                readDeadline = min(overallDeadlineMonoMs, nowReq &+ max(1, readTimeoutMs))
            } else {
                readDeadline = nowReq &+ max(1, readTimeoutMs)
            }

            self.stateLock.lock()
            if self.isShutdown {
                self.stateLock.unlock()
                completion(nil)
                return
            }
            self._requestCount += 1
            self.stateLock.unlock()

            let clampedMax = max(1, min(maxSnapshotBytes, PasteboardGuard.defaultMaxSnapshotBytes))
            var reqBytes = Data()
            reqBytes.reserveCapacity(4)
            appendBigEndianUInt32(&reqBytes, UInt32(clampedMax))

            let maxAllowedPayload = clampedMax + 1024 * 1024
            if writeAll(fd: handle.inFd, data: reqBytes),
               let hdr = readExactWithDeadline(fd: handle.outFd, count: 5, deadlineMonoMs: readDeadline),
               let payloadLen = readBigEndianUInt32(hdr, at: 1),
               Int(payloadLen) <= maxAllowedPayload,
               let payload = readExactWithDeadline(fd: handle.outFd, count: Int(payloadLen), deadlineMonoMs: readDeadline),
               let decoded = decodePasteboardResponse(statusByte: hdr[hdr.startIndex], payload: payload) {
                completion(decoded)
                return
            }

            // Timeout (e.g. 60s RDP Promise), EOF, or protocol error: SIGKILL + waitpid reap immediately.
            self.killAndReapCurrentOnIoQueue(handle)
            completion(nil)
        }
    }

    func shutdown() {
        stateLock.lock()
        if isShutdown {
            stateLock.unlock()
            return
        }
        isShutdown = true
        let active = worker
        worker = nil
        stateLock.unlock()

        _ = active?.sigkill()
        // Active worker received SIGKILL above so any blocked poll/read on ioQueue unblocks immediately.
        ioQueue.sync {
            _ = active?.closeAndReapOnIoQueue(forceKill: true)
        }
    }
}

final class PasteboardGuard {
    static let defaultMaxSnapshotBytes = 10 * 1024 * 1024
    static let defaultMaxCaptureMs: UInt64 = 200
    static let defaultRestoreDelayMs = 250
    static let transientType = NSPasteboard.PasteboardType("org.nspasteboard.TransientType")
    static let autoGeneratedType = NSPasteboard.PasteboardType("org.nspasteboard.AutoGeneratedType")
    static let concealedType = NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType")
    static let ownTransientMarker = NSPasteboard.PasteboardType("com.agentvoiceinput.transient")
    static let ownRestoredMarker = NSPasteboard.PasteboardType("com.agentvoiceinput.restored")
    private static let baseWhitelistTypes: [NSPasteboard.PasteboardType] = [.string, .rtf, .html, .png, .fileURL]

    enum ClipSnap {
        case clearOnly
        case items([[(NSPasteboard.PasteboardType, Data)]])
    }

    enum ReadOnlyCaptureOutcome {
        case items([[(NSPasteboard.PasteboardType, Data)]])
        case clearOnly
        case readFailed
        case ownTransient
        case unpreserved
    }

    // All mutable state below and all pasteboard writes MUST remain strictly on DispatchQueue.main.
    private let pasteboardProvider: () -> NSPasteboard
    private let workerClient: PasteboardWorkerClient
    private let restoreDelayMs: Int
    private let maxSnapshotBytes: Int
    private let maxCaptureMs: UInt64
    private var savedSnap: ClipSnap?
    private var writtenChangeCount: Int = -1
    private(set) var restorePending: Bool = false
    private var restoreGen: UInt64 = 0
    private var restoreWork: DispatchWorkItem?

    init(
        pasteboard: NSPasteboard? = nil,
        restoreDelayMs: Int = PasteboardGuard.defaultRestoreDelayMs,
        maxSnapshotBytes: Int = PasteboardGuard.defaultMaxSnapshotBytes,
        maxCaptureMs: UInt64 = PasteboardGuard.defaultMaxCaptureMs
    ) {
        if let pb = pasteboard {
            self.pasteboardProvider = { pb }
            self.workerClient = PasteboardWorkerClient(pasteboardName: pb.name)
        } else {
            self.pasteboardProvider = { NSPasteboard.general }
            self.workerClient = PasteboardWorkerClient(pasteboardName: nil)
        }
        self.restoreDelayMs = max(10, restoreDelayMs)
        self.maxSnapshotBytes = max(1, maxSnapshotBytes)
        self.maxCaptureMs = max(1, maxCaptureMs)
    }

    deinit {
        restoreWork?.cancel()
        workerClient.shutdown()
    }

    func shutdown() {
        cancelPendingRestore()
        workerClient.shutdown()
    }

    var workerSpawnCount: Int { workerClient.workerSpawnCount }
    var workerKillCount: Int { workerClient.workerKillCount }
    var workerRequestCount: Int { workerClient.requestCount }
    var currentWorkerPid: pid_t { workerClient.currentWorkerPid }
    var currentChangeCount: Int { pasteboardProvider().changeCount }

    func cancelPendingRestore() {
        restoreGen &+= 1
        restoreWork?.cancel()
        restoreWork = nil
        restorePending = false
        savedSnap = nil
    }

    static func classifyRawPasteboardItems(_ rawItems: [NSPasteboardItem]?) -> ReadOnlyCaptureOutcome? {
        guard let items = rawItems else {
            // Apple docs: pasteboardItems == nil means read failure, NEVER empty board.
            return .readFailed
        }
        if items.isEmpty {
            return .clearOnly
        }
        for item in items {
            let itemTypes = item.types
            if itemTypes.contains(Self.concealedType) {
                return .clearOnly
            }
            if itemTypes.contains(Self.ownTransientMarker) {
                return .ownTransient
            }
            if itemTypes.contains(Self.transientType) && !itemTypes.contains(Self.ownRestoredMarker) {
                return .clearOnly
            }
        }
        return nil
    }

    // Pure read-only snapshot invoked strictly on the isolated --pasteboard-worker main thread.
    static func captureCurrentReadOnly(
        pasteboardName: NSPasteboard.Name,
        maxSnapshotBytes: Int
    ) -> ReadOnlyCaptureOutcome {
        let pb = NSPasteboard(name: pasteboardName)
        let rawItems = pb.pasteboardItems
        if let early = classifyRawPasteboardItems(rawItems) {
            return early
        }
        guard let items = rawItems else {
            return .readFailed
        }
        var captured: [[(NSPasteboard.PasteboardType, Data)]] = []
        var totalBytes = 0
        for item in items {
            let itemTypes = item.types
            var candidateTypes = Self.baseWhitelistTypes
            if !itemTypes.contains(.png) && itemTypes.contains(.tiff) {
                candidateTypes.append(.tiff)
            }
            var entries: [(NSPasteboard.PasteboardType, Data)] = []
            for t in candidateTypes where itemTypes.contains(t) {
                let maybeData: Data? = autoreleasepool {
                    item.data(forType: t)
                }
                guard let d = maybeData else { continue }
                if d.count > maxSnapshotBytes || (totalBytes + d.count) > maxSnapshotBytes {
                    return .unpreserved
                }
                totalBytes += d.count
                entries.append((t, d))
            }
            if !entries.isEmpty {
                captured.append(entries)
            }
        }
        return captured.isEmpty ? .unpreserved : .items(captured)
    }

    func writePersistentFallback(_ text: String) -> Bool {
        cancelPendingRestore()
        writtenChangeCount = -1
        let pb = pasteboardProvider()
        pb.clearContents()
        if text.isEmpty {
            return true
        }
        return pb.setString(text, forType: .string)
    }

    func writePersistentFallbackIfWithinDeadline(
        _ text: String,
        deadlineMonoMs: UInt64,
        nowMs: () -> UInt64 = monotonicMs
    ) -> (written: Bool, reason: String) {
        if deadlineMonoMs > 0 && nowMs() >= deadlineMonoMs {
            return (false, "paste_deadline_exceeded")
        }
        let ok = writePersistentFallback(text)
        return (ok, ok ? "clipboard_only" : "clipboard_failed")
    }

    func captureSnapshotAsync(
        deadlineMonoMs: UInt64,
        completion: @escaping (_ preChangeCount: Int, _ snap: ClipSnap?) -> Void
    ) {
        let pb = pasteboardProvider()
        let preChangeCount = pb.changeCount
        // Rapid-fire burst: clipboard still holds our own transient voice text; reuse savedSnap without IPC.
        if writtenChangeCount >= 0 && preChangeCount == writtenChangeCount {
            completion(preChangeCount, restorePending ? savedSnap : nil)
            return
        }
        if deadlineMonoMs > 0 && monotonicMs() >= deadlineMonoMs {
            completion(preChangeCount, nil)
            return
        }
        workerClient.requestSnapshotAsync(
            maxSnapshotBytes: maxSnapshotBytes,
            readTimeoutMs: maxCaptureMs,
            overallDeadlineMonoMs: deadlineMonoMs
        ) { snap in
            DispatchQueue.main.async {
                completion(preChangeCount, snap)
            }
        }
    }

    func writeTransient(
        _ text: String,
        snap: ClipSnap?,
        afterWriteHook: (() -> Void)? = nil
    ) -> Bool {
        let item = NSPasteboardItem()
        guard item.setString(text, forType: .string),
              item.setData(Data(), forType: Self.transientType),
              item.setData(Data(), forType: Self.autoGeneratedType),
              item.setData(Data(), forType: Self.ownTransientMarker) else {
            return false
        }
        cancelPendingRestore()
        let pb = pasteboardProvider()
        let clearedChangeCount = pb.clearContents()
        guard pb.writeObjects([item]) else {
            writtenChangeCount = -1
            return false
        }
        afterWriteHook?()
        if pb.changeCount != clearedChangeCount {
            // Concurrent external Cmd+C in the micro-window: never adopt its changeCount, arm restore,
            // or let paste_cmd_v post Cmd+V over the user's newer clipboard item.
            writtenChangeCount = -1
            savedSnap = nil
            restorePending = false
            return false
        }
        writtenChangeCount = clearedChangeCount
        savedSnap = snap
        restorePending = (snap != nil)
        return true
    }

    func armPendingRestoreTimer() {
        guard restorePending, savedSnap != nil else { return }
        restoreWork?.cancel()
        let myGen = restoreGen
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.restorePending, self.restoreGen == myGen else { return }
            self.flushPendingRestore()
        }
        restoreWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(restoreDelayMs), execute: work)
    }

    private static func buildRestoreItems(from itemList: [[(NSPasteboard.PasteboardType, Data)]]) -> [NSPasteboardItem] {
        var rebuilt: [NSPasteboardItem] = []
        rebuilt.reserveCapacity(itemList.count)
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
        return rebuilt
    }

    @discardableResult
    func flushPendingRestore() -> Bool {
        guard restorePending else { return true }
        let snap = savedSnap
        let expectedCc = writtenChangeCount
        cancelPendingRestore()
        writtenChangeCount = -1
        let pb = pasteboardProvider()
        guard let snap = snap, expectedCc >= 0, pb.changeCount == expectedCc else {
            return false
        }
        switch snap {
        case .clearOnly:
            _ = pb.clearContents()
            return true
        case .items(let itemList):
            let rebuilt = Self.buildRestoreItems(from: itemList)
            guard !rebuilt.isEmpty, pb.changeCount == expectedCc else {
                return false
            }
            _ = pb.clearContents()
            return pb.writeObjects(rebuilt)
        }
    }
}

func runPasteboardWorker(pasteboardName: String?) -> Int32 {
    signal(SIGPIPE, SIG_IGN)
    let parentPid = getppid()
    let parentExitSrc = installParentExitWatchdog(parentPid: parentPid)
    return withExtendedLifetime(parentExitSrc) { () -> Int32 in
        let targetName: NSPasteboard.Name = {
            if let raw = pasteboardName, !raw.isEmpty {
                return NSPasteboard.Name(raw)
            }
            return NSPasteboard.general.name
        }()
        // Warm AppKit pasteboard connection on worker main thread before signalling READY.
        _ = NSPasteboard(name: targetName)
        guard writeAll(fd: STDOUT_FILENO, data: Data([kPbReadyByte])) else {
            return 0
        }
        while getppid() == parentPid {
            let keepRunning = autoreleasepool { () -> Bool in
                guard let reqData = readExact(fd: STDIN_FILENO, count: 4),
                      getppid() == parentPid,
                      let rawMax = readBigEndianUInt32(reqData, at: 0) else {
                    return false
                }
                let maxBytes = max(1, min(Int(rawMax), PasteboardGuard.defaultMaxSnapshotBytes))
                let outcome = PasteboardGuard.captureCurrentReadOnly(
                    pasteboardName: targetName,
                    maxSnapshotBytes: maxBytes
                )
                let (respHeader, respPayload) = encodePasteboardResponse(outcome: outcome)
                guard writeAll(fd: STDOUT_FILENO, data: respHeader) else {
                    return false
                }
                if !respPayload.isEmpty {
                    guard writeAll(fd: STDOUT_FILENO, data: respPayload) else {
                        return false
                    }
                }
                return true
            }
            if !keepRunning { break }
        }
        return 0
    }
}
