using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] internal class MMDeviceEnumeratorComObject { }
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceEnumerator {
    [PreserveSig] int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IntPtr ppDevices);
    [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
    [PreserveSig] int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string pwstrId, out IMMDevice ppDevice);
    [PreserveSig] int RegisterEndpointNotificationCallback(IntPtr pClient);
    [PreserveSig] int UnregisterEndpointNotificationCallback(IntPtr pClient);
}
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDevice {
    [PreserveSig] int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
    [PreserveSig] int OpenPropertyStore(int stgmAccess, out IntPtr ppProperties);
    [PreserveSig] int GetId(out IntPtr ppstrId);
    [PreserveSig] int GetState(out int pdwState);
}
[StructLayout(LayoutKind.Sequential)]
internal struct WAVEFORMATEX {
    public ushort wFormatTag, nChannels;
    public uint nSamplesPerSec, nAvgBytesPerSec;
    public ushort nBlockAlign, wBitsPerSample, cbSize;
}
[ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioClient {
    [PreserveSig] int Initialize(int ShareMode, uint StreamFlags, long hnsBufferDuration, long hnsPeriodicity, ref WAVEFORMATEX pFormat, IntPtr AudioSessionGuid);
    [PreserveSig] int GetBufferSize(out uint pNumBufferFrames);
    [PreserveSig] int GetStreamLatency(out long phnsLatency);
    [PreserveSig] int GetCurrentPadding(out uint pNumPaddingFrames);
    [PreserveSig] int IsFormatSupported(int ShareMode, IntPtr pFormat, out IntPtr ppClosestMatch);
    [PreserveSig] int GetMixFormat(out IntPtr ppDeviceFormat);
    [PreserveSig] int GetDevicePeriod(out long phnsDefaultDevicePeriod, out long phnsMinimumDevicePeriod);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr eventHandle);
    [PreserveSig] int GetService(ref Guid riid, [MarshalAs(UnmanagedType.IUnknown)] out object ppv);
}
[ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioCaptureClient {
    [PreserveSig] int GetBuffer(out IntPtr ppData, out uint pNumFramesToRead, out uint pdwFlags, out ulong pu64DevicePosition, out ulong pu64QPCPosition);
    [PreserveSig] int ReleaseBuffer(uint NumFramesRead);
    [PreserveSig] int GetNextPacketSize(out uint pNumFramesInNextPacket);
}

public static partial class VoiceWinShim {
    public static float ComputePcm16RmsLevel(byte[] pcm, int byteCount) {
        if (pcm == null || byteCount < 2) return 0f;
        int nSamples = byteCount / 2;
        double sumSq = 0.0;
        for (int i = 0; i < nSamples * 2; i += 2) {
            short s = (short)(pcm[i] | (pcm[i + 1] << 8));
            double d = (double)s;
            sumSq += d * d;
        }
        double rms = Math.Sqrt(sumSq / nSamples) / 32768.0;
        if (rms <= 1e-5) return 0f;
        double db = 20.0 * Math.Log10(rms);
        if (db <= -48.0) return 0f;
        if (db >= -14.0) return 1f;
        return (float)((db + 48.0) / 34.0);
    }

    public static void SetExternalAudioLevel(float level) {
        float clamped = (level < 0f) ? 0f : ((level > 1f) ? 1f : level);
        _extLevel = clamped;
        Interlocked.Exchange(ref _extLevelTick, (long)GetTickCount64());
    }

    public static float GetLiveAudioLevel() {
        RecSession s = _recSess;
        if (s != null && s.Run) return ((long)GetTickCount64() - Interlocked.Read(ref s.LevelTick) <= 260L) ? s.Level : 0f;
        if ((long)GetTickCount64() - Interlocked.Read(ref _extLevelTick) <= 260L) return _extLevel;
        return 0f;
    }

    private const int AUDCLNT_S_BUFFER_EMPTY = 0x08890001;
    private const long WasapiStartupFirstFrameTimeoutMs = 850L;
    private const long WasapiStallTimeoutMs = 900L;
    private const long WasapiReopenVerifyTimeoutMs = 500L;

    private sealed class RecSession {
        public volatile bool Run = true;
        public volatile bool InitOk = false;
        public volatile string Err = "";
        public volatile float Level = 0f;
        public long LevelTick = 0L;
        public long RecGen = 0L;
        public bool WorkerDone = false;
        public bool Abandoned = false;
        public Thread Worker;
        public ManualResetEvent InitEvt;
        public ManualResetEvent StopEvt;
        public MemoryStream Pcm;
        public void DisposeResources() {
            if (InitEvt != null) { try { InitEvt.Dispose(); } catch { } InitEvt = null; }
            if (StopEvt != null) { try { StopEvt.Dispose(); } catch { } StopEvt = null; }
            if (Pcm != null) { try { Pcm.Dispose(); } catch { } Pcm = null; }
        }
    }
    private sealed class StoppedRecSlot {
        public long RecGen = 0L;
        public byte[] WavBytes = new byte[0];
        public int PcmBytes = 0;
        public string Err = "";
    }
    private static readonly object _recSync = new object();
    private static volatile RecSession _recSess = null;
    private static StoppedRecSlot _stoppedRecSlot = null;
    private static volatile float _extLevel = 0f;
    private static long _extLevelTick = 0L;

    private static void WriteZeros(MemoryStream dst, byte[] zeros, int byteCount) {
        int rem = byteCount;
        while (rem > 0) { int chunk = Math.Min(rem, zeros.Length); dst.Write(zeros, 0, chunk); rem -= chunk; }
    }

    public static byte[] BuildPcm16MonoWav(byte[] pcm, int pcmLen, int sr) {
        byte[] buf = new byte[44 + pcmLen];
        Encoding.ASCII.GetBytes("RIFF").CopyTo(buf, 0); BitConverter.GetBytes(36 + pcmLen).CopyTo(buf, 4);
        Encoding.ASCII.GetBytes("WAVEfmt ").CopyTo(buf, 8); BitConverter.GetBytes(16).CopyTo(buf, 16);
        BitConverter.GetBytes((short)1).CopyTo(buf, 20); BitConverter.GetBytes((short)1).CopyTo(buf, 22);
        BitConverter.GetBytes(sr).CopyTo(buf, 24); BitConverter.GetBytes(sr * 2).CopyTo(buf, 28);
        BitConverter.GetBytes((short)2).CopyTo(buf, 32); BitConverter.GetBytes((short)16).CopyTo(buf, 34);
        Encoding.ASCII.GetBytes("data").CopyTo(buf, 36); BitConverter.GetBytes(pcmLen).CopyTo(buf, 40);
        Buffer.BlockCopy(pcm, 0, buf, 44, pcmLen);
        return buf;
    }

    private static void EnqueueAudioEvent(string kind, string reason, int gapMs, long recGen) {
        EvtQueue.Enqueue(new ShimEvent { Kind = kind, Reason = reason ?? "", GapMs = gapMs, RecGen = recGen, TsMs = GetTickCount64() });
        if (_mainTid != 0) PostThreadMessage(_mainTid, WM_APP_WAKE, IntPtr.Zero, IntPtr.Zero);
    }

    public static string ClassifyWasapiStartHr(int hr) {
        if (hr == unchecked((int)0x80070490)) return "no_mic:0x80070490";
        if (hr == unchecked((int)0x80070005)) return "mic_permission:0x80070005";
        return string.Format(CultureInfo.InvariantCulture, "mic_busy:0x{0:X8}", (uint)hr);
    }

    private static void ReleaseWasapiStream(ref IAudioCaptureClient cap, ref IAudioClient client, bool started) {
        if (started && client != null) { try { client.Stop(); } catch { } }
        if (cap != null) { try { Marshal.ReleaseComObject(cap); } catch { } cap = null; }
        if (client != null) { try { Marshal.ReleaseComObject(client); } catch { } client = null; }
    }

    private static string OpenWasapiStreamOnDevice(IMMDevice dev, bool loopback, out IAudioClient client, out IAudioCaptureClient cap, out int failHr) {
        client = null; cap = null; failHr = 0;
        Guid iidCli = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
        object oCli = null;
        int hr = dev.Activate(ref iidCli, 23, IntPtr.Zero, out oCli);
        if (hr < 0 || oCli == null) {
            failHr = (hr < 0) ? hr : unchecked((int)0x80004005);
            return ClassifyWasapiStartHr(failHr);
        }
        client = (IAudioClient)oCli;
        WAVEFORMATEX wf = new WAVEFORMATEX { wFormatTag = 1, nChannels = 1, nSamplesPerSec = 16000, wBitsPerSample = 16, nBlockAlign = 2, nAvgBytesPerSec = 32000, cbSize = 0 };
        uint sflags = 0x88000000u | (loopback ? 0x00020000u : 0u);
        hr = client.Initialize(0, sflags, 10000000L, 0L, ref wf, IntPtr.Zero);
        if (hr < 0) {
            failHr = hr;
            ReleaseWasapiStream(ref cap, ref client, false);
            return ClassifyWasapiStartHr(hr);
        }
        Guid iidCap = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
        object oCap = null;
        hr = client.GetService(ref iidCap, out oCap);
        if (hr < 0 || oCap == null) {
            failHr = (hr < 0) ? hr : unchecked((int)0x80004005);
            ReleaseWasapiStream(ref cap, ref client, false);
            return ClassifyWasapiStartHr(failHr);
        }
        cap = (IAudioCaptureClient)oCap;
        hr = client.Start();
        if (hr < 0) {
            failHr = hr;
            ReleaseWasapiStream(ref cap, ref client, false);
            return ClassifyWasapiStartHr(hr);
        }
        return "";
    }

    private static bool IsEndpointStillActive(IMMDeviceEnumerator enumObj, string deviceId) {
        if (enumObj == null || string.IsNullOrEmpty(deviceId)) return true;
        IMMDevice curDev = null;
        try {
            int hr = enumObj.GetDevice(deviceId, out curDev);
            if (hr < 0 || curDev == null) return false;
            int state = 0;
            hr = curDev.GetState(out state);
            return (hr >= 0) && ((state & 1) != 0);
        } catch {
            return false;
        } finally {
            if (curDev != null) { try { Marshal.ReleaseComObject(curDev); } catch { } }
        }
    }

    public static bool ProbeDefaultWasapiCapture(out string stateDesc) {
        stateDesc = "";
        IMMDeviceEnumerator en = null;
        IMMDevice dev = null;
        try {
            en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
            int hr = en.GetDefaultAudioEndpoint(1, 0, out dev);
            if (hr < 0 || dev == null) {
                stateDesc = string.Format(CultureInfo.InvariantCulture, "no_default:0x{0:X8}", (uint)hr);
                return false;
            }
            int state = 0;
            hr = dev.GetState(out state);
            if (hr < 0) {
                stateDesc = string.Format(CultureInfo.InvariantCulture, "get_state_failed:0x{0:X8}", (uint)hr);
                return false;
            }
            bool active = (state & 1) != 0;
            stateDesc = active ? "active" : string.Format(CultureInfo.InvariantCulture, "inactive:{0}", state);
            return active;
        } catch (Exception ex) {
            stateDesc = "exc:" + ex.GetType().Name;
            return false;
        } finally {
            if (dev != null) { try { Marshal.ReleaseComObject(dev); } catch { } }
            if (en != null) { try { Marshal.ReleaseComObject(en); } catch { } }
        }
    }

    private static int DrainWasapiPackets(RecSession sess, IAudioCaptureClient cap, MemoryStream dst, byte[] zeros, ref byte[] scratch, out bool gotFrames) {
        gotFrames = false;
        uint pkt = 0;
        int hr = cap.GetNextPacketSize(out pkt);
        if (hr < 0) { sess.Level = 0f; return hr; }
        while (pkt > 0) {
            IntPtr pData; uint frames, flags; ulong dpos, qpos;
            hr = cap.GetBuffer(out pData, out frames, out flags, out dpos, out qpos);
            if (hr < 0) { sess.Level = 0f; return hr; }
            if (hr == AUDCLNT_S_BUFFER_EMPTY || frames == 0) {
                cap.ReleaseBuffer(0);
                break;
            }
            int nBytes = (int)(frames * 2);
            if (nBytes > 0) {
                if ((flags & 2u) != 0 || pData == IntPtr.Zero) {
                    sess.Level = 0f;
                    Interlocked.Exchange(ref sess.LevelTick, (long)GetTickCount64());
                    WriteZeros(dst, zeros, nBytes);
                } else {
                    if (scratch == null || scratch.Length < nBytes) scratch = new byte[Math.Max(nBytes, 8192)];
                    Marshal.Copy(pData, scratch, 0, nBytes);
                    dst.Write(scratch, 0, nBytes);
                    sess.Level = ComputePcm16RmsLevel(scratch, nBytes);
                    Interlocked.Exchange(ref sess.LevelTick, (long)GetTickCount64());
                }
                gotFrames = true;
            }
            hr = cap.ReleaseBuffer(frames);
            if (hr < 0) { sess.Level = 0f; return hr; }
            hr = cap.GetNextPacketSize(out pkt);
            if (hr < 0) { sess.Level = 0f; return hr; }
        }
        return 0;
    }

    private static bool WaitForWasapiPacket(RecSession sess, IMMDeviceEnumerator en, string deviceId, IAudioCaptureClient cap, MemoryStream dst, byte[] zeros, ref byte[] scratch, long timeoutMs, out long packetTick, out int failHr, out bool endpointInactive) {
        long startTick = Stopwatch.GetTimestamp();
        long devCheckTick = startTick;
        packetTick = startTick;
        failHr = 0;
        endpointInactive = false;
        while (sess.Run && !StdinClosed) {
            bool gotFrames;
            int hr = DrainWasapiPackets(sess, cap, dst, zeros, ref scratch, out gotFrames);
            long nowTick = Stopwatch.GetTimestamp();
            if (hr < 0) {
                failHr = hr;
                endpointInactive = !string.IsNullOrEmpty(deviceId) && !IsEndpointStillActive(en, deviceId);
                return false;
            }
            if (gotFrames) {
                packetTick = nowTick;
                return true;
            }
            if (ElapsedStopwatchMs(devCheckTick, nowTick) >= 200L) {
                devCheckTick = nowTick;
                if (!string.IsNullOrEmpty(deviceId) && !IsEndpointStillActive(en, deviceId)) {
                    endpointInactive = true;
                    return false;
                }
            }
            if (ElapsedStopwatchMs(startTick, nowTick) >= timeoutMs) return false;
            if (sess.StopEvt.WaitOne(10)) break;
        }
        return false;
    }

    private static bool HasPendingStopOrQuitCmd() {
        foreach (QueuedCmd item in CmdQueue) {
            string raw = (item != null) ? item.Line : null;
            if (string.IsNullOrEmpty(raw)) continue;
            string s = raw.TrimStart();
            if (s.StartsWith("{\"cmd\":\"rec_stop\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\": \"rec_stop\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\":\"rec_cancel\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\": \"rec_cancel\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\":\"quit\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\": \"quit\"", StringComparison.Ordinal)) {
                return true;
            }
        }
        return false;
    }

    public static void ReleaseStoppedRecording(long recGen = 0L) {
        lock (_recSync) {
            if (_stoppedRecSlot != null && (recGen <= 0L || _stoppedRecSlot.RecGen == recGen)) {
                _stoppedRecSlot = null;
            }
        }
    }

    public static void CancelRecording(long recGen = 0L) {
        int dummyBytes; string dummyErr;
        StopRecording(recGen, out dummyBytes, out dummyErr, false);
        ReleaseStoppedRecording(recGen);
    }

    public static string StartRecording(bool loopback = false) {
        string err;
        return StartRecording(loopback, 0L, out err) ? "" : err;
    }

    public static bool StartRecording(bool loopback, out string error) {
        return StartRecording(loopback, 0L, out error);
    }

    public static bool StartRecording(bool loopback, long recGen, out string error) {
        int dummyBytes; string dummyErr;
        StopRecording(0L, out dummyBytes, out dummyErr, false);
        ReleaseStoppedRecording(0L);
        RecSession sess = new RecSession {
            RecGen = recGen,
            InitEvt = new ManualResetEvent(false),
            StopEvt = new ManualResetEvent(false),
            Pcm = new MemoryStream(64000)
        };
        Thread t = new Thread(() => {
            IMMDeviceEnumerator en = null; IMMDevice dev = null; IAudioClient client = null; IAudioCaptureClient cap = null;
            bool started = false;
            bool initNotified = false;
            try {
                en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
                int hr = en.GetDefaultAudioEndpoint(loopback ? 0 : 1, 0, out dev);
                if (hr < 0 || dev == null) {
                    sess.Err = ClassifyWasapiStartHr((hr < 0) ? hr : unchecked((int)0x80070490));
                    return;
                }
                if (!sess.Run || StdinClosed) { sess.Err = "mic_busy:wasapi_aborted"; return; }
                string initialDeviceId = "";
                IntPtr idPtr = IntPtr.Zero;
                int idHr = dev.GetId(out idPtr);
                if (idHr >= 0 && idPtr != IntPtr.Zero) {
                    try { initialDeviceId = Marshal.PtrToStringUni(idPtr) ?? ""; }
                    finally { Marshal.FreeCoTaskMem(idPtr); }
                }
                int openHr;
                string openErr = OpenWasapiStreamOnDevice(dev, loopback, out client, out cap, out openHr);
                if (!string.IsNullOrEmpty(openErr)) { sess.Err = openErr; return; }
                started = true;
                if (!sess.Run || StdinClosed) { sess.Err = "mic_busy:wasapi_aborted"; return; }
                byte[] zeros = new byte[8192]; byte[] scratch = null;
                long lastPacketTick = Stopwatch.GetTimestamp();
                long lastDeviceCheckTick = lastPacketTick;
                if (!loopback) {
                    int initHr; bool initInactive;
                    if (!WaitForWasapiPacket(sess, en, initialDeviceId, cap, sess.Pcm, zeros, ref scratch, WasapiStartupFirstFrameTimeoutMs, out lastPacketTick, out initHr, out initInactive)) {
                        sess.Err = initInactive ? "no_mic:wasapi_endpoint_inactive"
                                 : (initHr < 0) ? ClassifyWasapiStartHr(initHr)
                                 : (!sess.Run || StdinClosed || sess.StopEvt.WaitOne(0)) ? "mic_busy:wasapi_aborted"
                                 : "mic_busy:wasapi_startup_no_frames";
                        return;
                    }
                    lastDeviceCheckTick = lastPacketTick;
                }
                sess.InitOk = true;
                initNotified = true;
                try { sess.InitEvt.Set(); } catch { }
                int reopenCount = 0;
                while (sess.Run && !sess.StopEvt.WaitOne(10)) {
                    bool gotFrames;
                    int drainHr = DrainWasapiPackets(sess, cap, sess.Pcm, zeros, ref scratch, out gotFrames);
                    long nowTick = Stopwatch.GetTimestamp();
                    if (gotFrames) lastPacketTick = nowTick;
                    if (drainHr >= 0 && ElapsedStopwatchMs(lastDeviceCheckTick, nowTick) >= 200L) {
                        lastDeviceCheckTick = nowTick;
                        if (!string.IsNullOrEmpty(initialDeviceId) && !IsEndpointStillActive(en, initialDeviceId)) {
                            sess.Err = "wasapi_device_unplugged";
                            if (sess.Run) EnqueueAudioEvent("rec_lost", sess.Err, 0, sess.RecGen);
                            break;
                        }
                    }
                    bool stalled = (!loopback) && (drainHr >= 0) && (ElapsedStopwatchMs(lastPacketTick, nowTick) > WasapiStallTimeoutMs);
                    if (drainHr < 0 || stalled) {
                        int failStreamHr = drainHr;
                        if (!string.IsNullOrEmpty(initialDeviceId) && !IsEndpointStillActive(en, initialDeviceId)) {
                            sess.Err = "wasapi_device_unplugged";
                            if (sess.Run) EnqueueAudioEvent("rec_lost", sess.Err, 0, sess.RecGen);
                            break;
                        }
                        bool reopened = false;
                        if (!string.IsNullOrEmpty(initialDeviceId) && reopenCount < 3 && sess.Run && !StdinClosed) {
                            reopenCount++;
                            ReleaseWasapiStream(ref cap, ref client, started);
                            started = false;
                            if (dev != null) { try { Marshal.ReleaseComObject(dev); } catch { } dev = null; }
                            int[] retryDelaysMs = new int[] { 0, 150, 300 };
                            for (int attempt = 0; attempt < retryDelaysMs.Length && sess.Run && !StdinClosed; attempt++) {
                                if (retryDelaysMs[attempt] > 0) {
                                    if (sess.StopEvt.WaitOne(retryDelaysMs[attempt]) || !sess.Run || StdinClosed) break;
                                }
                                if (!IsEndpointStillActive(en, initialDeviceId)) {
                                    sess.Err = "wasapi_device_unplugged";
                                    break;
                                }
                                IMMDevice reDev = null;
                                int ghr = en.GetDevice(initialDeviceId, out reDev);
                                if (ghr >= 0 && reDev != null) {
                                    IAudioClient reCli; IAudioCaptureClient reCap; int reOpenHr;
                                    string reErr = OpenWasapiStreamOnDevice(reDev, loopback, out reCli, out reCap, out reOpenHr);
                                    if (string.IsNullOrEmpty(reErr) && reCli != null && reCap != null) {
                                        dev = reDev; client = reCli; cap = reCap; started = true;
                                        reopened = true;
                                        break;
                                    }
                                    try { Marshal.ReleaseComObject(reDev); } catch { }
                                    if (reOpenHr < 0 && failStreamHr >= 0) failStreamHr = reOpenHr;
                                } else {
                                    if (reDev != null) { try { Marshal.ReleaseComObject(reDev); } catch { } }
                                    if (ghr < 0 && failStreamHr >= 0) failStreamHr = ghr;
                                }
                            }
                        }
                        if (!sess.Run) break;
                        if (reopened) {
                            bool verifiedRecovery = loopback;
                            long resumeTick = Stopwatch.GetTimestamp();
                            using (MemoryStream reopenPcm = new MemoryStream(4096)) {
                                if (!loopback) {
                                    int reHr; bool reInactive;
                                    verifiedRecovery = WaitForWasapiPacket(sess, en, initialDeviceId, cap, reopenPcm, zeros, ref scratch, WasapiReopenVerifyTimeoutMs, out resumeTick, out reHr, out reInactive);
                                    if (!verifiedRecovery) {
                                        if (reHr < 0 && failStreamHr >= 0) failStreamHr = reHr;
                                        if (reInactive) sess.Err = "wasapi_device_unplugged";
                                    }
                                }
                                if (verifiedRecovery) {
                                    if (sess.Run || reopenPcm.Length > 0) {
                                        long rawGapMs = ElapsedStopwatchMs(lastPacketTick, resumeTick) - (reopenPcm.Length / 32L);
                                        int gapMs = (rawGapMs < 0L) ? 0 : ((rawGapMs > 30000L) ? 30000 : (int)rawGapMs);
                                        WriteZeros(sess.Pcm, zeros, gapMs * 32);
                                        if (reopenPcm.Length > 0) {
                                            reopenPcm.WriteTo(sess.Pcm);
                                        } else {
                                            sess.Level = 0f;
                                            Interlocked.Exchange(ref sess.LevelTick, (long)GetTickCount64());
                                        }
                                        if (sess.Run && !StdinClosed) {
                                            lastPacketTick = resumeTick;
                                            lastDeviceCheckTick = resumeTick;
                                            EnqueueAudioEvent("rec_recovered", "", gapMs, sess.RecGen);
                                            continue;
                                        }
                                    }
                                    break;
                                }
                            }
                        }
                        if (string.IsNullOrEmpty(sess.Err)) {
                            sess.Err = (failStreamHr < 0)
                                ? string.Format(CultureInfo.InvariantCulture, "wasapi_stream_lost:0x{0:X8}", (uint)failStreamHr)
                                : "wasapi_stream_stalled";
                        }
                        if (sess.Run) EnqueueAudioEvent("rec_lost", sess.Err, 0, sess.RecGen);
                        break;
                    }
                }
                if (started && cap != null && string.IsNullOrEmpty(sess.Err)) {
                    bool tailFrames;
                    DrainWasapiPackets(sess, cap, sess.Pcm, zeros, ref scratch, out tailFrames);
                }
            } catch (Exception ex) {
                if (!sess.InitOk) {
                    sess.Err = "mic_busy:exc:" + ex.GetType().Name + ":" + ex.Message;
                } else {
                    sess.Err = "wasapi_stream_lost:" + ex.GetType().Name + ":" + ex.Message;
                    if (sess.Run) EnqueueAudioEvent("rec_lost", sess.Err, 0, sess.RecGen);
                }
            } finally {
                sess.Level = 0f;
                ReleaseWasapiStream(ref cap, ref client, started);
                if (dev != null) { try { Marshal.ReleaseComObject(dev); } catch { } }
                if (en != null) { try { Marshal.ReleaseComObject(en); } catch { } }
                if (!initNotified) {
                    try { ManualResetEvent ie = sess.InitEvt; if (ie != null) ie.Set(); } catch { }
                }
                bool disposeSelf = false;
                lock (_recSync) {
                    sess.WorkerDone = true;
                    disposeSelf = sess.Abandoned;
                }
                if (disposeSelf) sess.DisposeResources();
            }
        });
        sess.Worker = t;
        t.IsBackground = true; t.SetApartmentState(ApartmentState.MTA); t.Start();
        long waitStartTick = Stopwatch.GetTimestamp();
        bool initSignaled = false;
        bool waitAborted = false;
        while (true) {
            if (sess.InitEvt.WaitOne(15)) { initSignaled = true; break; }
            MSG pm;
            PeekMessage(out pm, IntPtr.Zero, 0, 0, 0);
            if (StdinClosed || HasPendingStopOrQuitCmd()) { waitAborted = true; break; }
            if (ElapsedStopwatchMs(waitStartTick, Stopwatch.GetTimestamp()) >= 3000L) break;
        }
        if (initSignaled && sess.InitOk && !waitAborted && !StdinClosed && !HasPendingStopOrQuitCmd()) {
            lock (_recSync) {
                if (sess.InitEvt != null) { try { sess.InitEvt.Dispose(); } catch { } sess.InitEvt = null; }
                _recSess = sess;
            }
            error = "";
            return true;
        }
        sess.Run = false;
        try { sess.StopEvt.Set(); } catch { }
        bool joined = false;
        try { joined = t.Join(initSignaled ? 500 : 300); } catch { }
        bool disposeNow = joined;
        if (!disposeNow) {
            lock (_recSync) {
                if (sess.WorkerDone) disposeNow = true;
                else sess.Abandoned = true;
            }
        }
        if (disposeNow) sess.DisposeResources();
        if (!initSignaled && !waitAborted) {
            error = "mic_busy:wasapi_init_timeout";
        } else if (waitAborted && string.IsNullOrEmpty(sess.Err)) {
            error = "mic_busy:wasapi_aborted";
        } else {
            error = string.IsNullOrEmpty(sess.Err) ? "mic_busy:wasapi_init_failed" : sess.Err;
        }
        return false;
    }

    public static byte[] StopRecording(out string error) {
        int dummyBytes;
        return StopRecording(0L, out dummyBytes, out error, true);
    }

    public static byte[] StopRecording(out int pcmBytes, out string error) {
        return StopRecording(0L, out pcmBytes, out error, true);
    }

    private static void StoreStoppedRecSlot(long recGen, byte[] wavBytes, int pcmBytes, string err) {
        lock (_recSync) {
            if (recGen > 0L && _recSess == null) {
                _stoppedRecSlot = new StoppedRecSlot {
                    RecGen = recGen,
                    WavBytes = wavBytes ?? new byte[0],
                    PcmBytes = pcmBytes,
                    Err = err ?? ""
                };
            } else {
                _stoppedRecSlot = null;
            }
        }
    }

    public static byte[] StopRecording(long recGen, out int pcmBytes, out string error, bool retainSlot = true) {
        RecSession sess;
        StoppedRecSlot cached = null;
        lock (_recSync) {
            sess = _recSess;
            if (sess != null && recGen > 0L && sess.RecGen > 0L && sess.RecGen != recGen) {
                sess = null;
            } else {
                _recSess = null;
                if (sess == null) {
                    if (retainSlot && recGen > 0L && _stoppedRecSlot != null && _stoppedRecSlot.RecGen == recGen) {
                        cached = _stoppedRecSlot;
                    } else if (!retainSlot) {
                        if (recGen <= 0L || (_stoppedRecSlot != null && _stoppedRecSlot.RecGen == recGen)) {
                            _stoppedRecSlot = null;
                        }
                    }
                } else {
                    _stoppedRecSlot = null;
                }
            }
        }
        if (cached != null) {
            pcmBytes = cached.PcmBytes;
            error = cached.Err ?? "";
            return cached.WavBytes ?? new byte[0];
        }
        pcmBytes = 0; error = "";
        if (sess == null || sess.Worker == null) { error = "not_recording"; return new byte[0]; }
        long activeGen = (sess.RecGen > 0L) ? sess.RecGen : recGen;
        sess.Run = false;
        try { if (sess.StopEvt != null) sess.StopEvt.Set(); } catch { }
        bool joined = false;
        try { joined = sess.Worker.Join(2500); } catch { }
        bool workerDone = joined;
        if (!workerDone) {
            lock (_recSync) {
                if (sess.WorkerDone) workerDone = true;
                else sess.Abandoned = true;
            }
        }
        if (!workerDone) {
            error = string.IsNullOrEmpty(sess.Err) ? "wasapi_stop_timeout" : sess.Err;
            if (retainSlot && activeGen > 0L) StoreStoppedRecSlot(activeGen, new byte[0], 0, error);
            return new byte[0];
        }
        if (!retainSlot) {
            sess.DisposeResources();
            ReleaseStoppedRecording(recGen > 0L ? recGen : 0L);
            error = "";
            return new byte[0];
        }
        byte[] raw = new byte[0];
        if (sess.Pcm != null) { try { raw = sess.Pcm.ToArray(); } catch { } }
        sess.DisposeResources();
        int evenLen = raw.Length - (raw.Length % 2);
        pcmBytes = evenLen;
        if (evenLen < 2) {
            error = string.IsNullOrEmpty(sess.Err) ? "wasapi_zero_packets" : sess.Err;
            if (retainSlot && activeGen > 0L) StoreStoppedRecSlot(activeGen, new byte[0], 0, error);
            return new byte[0];
        }
        error = sess.Err ?? "";
        byte[] wav = BuildPcm16MonoWav(raw, evenLen, 16000);
        if (retainSlot && activeGen > 0L) StoreStoppedRecSlot(activeGen, wav, evenLen, error);
        return wav;
    }
}
