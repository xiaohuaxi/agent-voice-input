import AVFoundation
import CoreAudio
import Foundation
import Security

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

func runAudioStatusMode() -> Int32 {
    signal(SIGPIPE, SIG_IGN)
    writeJsonLine(["ok": true, "mode": "audio-status", "audio_status": queryAudioInputStatus()])
    return 0
}
