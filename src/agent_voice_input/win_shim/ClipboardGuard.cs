using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

public static partial class VoiceWinShim {
    private const uint CF_DIB = 8u, CF_UNICODETEXT = 13u, CF_HDROP = 15u, CF_DIBV5 = 17u, GMEM_MOVEABLE = 0x0002u, GMEM_ZEROINIT = 0x0040u;
    public const int MaxClipSnapshotBytes = 10 * 1024 * 1024;
    private const int ClipRestoreDelayMs = 250, ClipRestoreRetryDelayMs = 60, MaxClipRestoreRetries = 3;
    private static readonly byte[] DwordZeroBytes = new byte[4];
    private static IntPtr _clipHwnd = IntPtr.Zero;
    private static System.Windows.Forms.Timer _clipRestoreTimer = null;
    private static bool _clipFormatsReady = false;
    private static uint _fmtExcludeMonitor = 0u, _fmtCanHistory = 0u, _fmtCanCloud = 0u, _fmtViewerIgnore = 0u, _fmtViewerIgnoreSpaced = 0u, _fmtAviTransient = 0u, _fmtAviRestored = 0u, _fmtPng = 0u, _fmtHtml = 0u, _fmtRtf = 0u;

    private enum SnapKind { ClearOnly, Data, Unrestorable }
    private enum ClipReadStatus { Missing, Ok, Oversize }

    private sealed class ClipEntry {
        public readonly uint Format;
        public readonly byte[] Data;
        public ClipEntry(uint fmt, byte[] data) { Format = fmt; Data = data; }
    }

    private sealed class ClipSnap {
        public static readonly ClipSnap ClearOnly = new ClipSnap(SnapKind.ClearOnly, null);
        public static readonly ClipSnap Unrestorable = new ClipSnap(SnapKind.Unrestorable, null);
        public readonly SnapKind Kind;
        public readonly List<ClipEntry> Entries;
        private ClipSnap(SnapKind kind, List<ClipEntry> entries) { Kind = kind; Entries = entries; }
        public static ClipSnap Data(List<ClipEntry> entries) { return new ClipSnap(SnapKind.Data, entries); }
    }

    private sealed class ClipGuardState {
        public ClipSnap SavedClip = null;
        public uint ExpectedSeq = 0u;
        public ulong NextToken = 0UL;
        public ulong ExpectedToken = 0UL;
        public int RetryCount = 0;
        public bool RestorePending = false;
    }

    private interface IClipStore {
        uint SequenceNumber { get; }
        uint FormatExcludeMonitor { get; }
        uint FormatCanHistory { get; }
        uint FormatCanCloud { get; }
        uint FormatViewerIgnore { get; }
        uint FormatViewerIgnoreSpaced { get; }
        uint FormatAviTransient { get; }
        uint FormatAviRestored { get; }
        uint FormatPng { get; }
        uint FormatHtml { get; }
        uint FormatRtf { get; }
        bool IsOwnerHung();
        bool Open();
        void Close();
        bool IsEmptyWhenOpen();
        bool HasFormatWhenOpen(uint fmt);
        ClipReadStatus ReadFormatBytesWhenOpen(uint fmt, int maxBytes, out byte[] data);
        bool EmptyWhenOpen();
        bool SetFormatBytesWhenOpen(uint fmt, byte[] data);
    }

    private static void EnsureClipFormats() {
        if (_clipFormatsReady) return;
        _fmtExcludeMonitor = RegisterClipboardFormat("ExcludeClipboardContentFromMonitorProcessing");
        _fmtCanHistory = RegisterClipboardFormat("CanIncludeInClipboardHistory");
        _fmtCanCloud = RegisterClipboardFormat("CanUploadToCloudClipboard");
        _fmtViewerIgnore = RegisterClipboardFormat("ClipboardViewerIgnore");
        _fmtViewerIgnoreSpaced = RegisterClipboardFormat("Clipboard Viewer Ignore");
        _fmtAviTransient = RegisterClipboardFormat("AgentVoiceInputTransient");
        _fmtAviRestored = RegisterClipboardFormat("AgentVoiceInputRestored");
        _fmtPng = RegisterClipboardFormat("PNG");
        _fmtHtml = RegisterClipboardFormat("HTML Format");
        _fmtRtf = RegisterClipboardFormat("Rich Text Format");
        _clipFormatsReady = true;
    }

    private static IntPtr EnsureClipHwnd() {
        if (_clipHwnd == IntPtr.Zero) {
            try { _clipHwnd = CreateWindowEx(0u, "STATIC", "", 0u, 0, 0, 0, 0, new IntPtr(-3), IntPtr.Zero, IntPtr.Zero, IntPtr.Zero); } catch { _clipHwnd = IntPtr.Zero; }
        }
        return _clipHwnd;
    }

    private sealed class Win32ClipStore : IClipStore {
        public uint SequenceNumber { get { return GetClipboardSequenceNumber(); } }
        public uint FormatExcludeMonitor { get { EnsureClipFormats(); return _fmtExcludeMonitor; } }
        public uint FormatCanHistory { get { EnsureClipFormats(); return _fmtCanHistory; } }
        public uint FormatCanCloud { get { EnsureClipFormats(); return _fmtCanCloud; } }
        public uint FormatViewerIgnore { get { EnsureClipFormats(); return _fmtViewerIgnore; } }
        public uint FormatViewerIgnoreSpaced { get { EnsureClipFormats(); return _fmtViewerIgnoreSpaced; } }
        public uint FormatAviTransient { get { EnsureClipFormats(); return _fmtAviTransient; } }
        public uint FormatAviRestored { get { EnsureClipFormats(); return _fmtAviRestored; } }
        public uint FormatPng { get { EnsureClipFormats(); return _fmtPng; } }
        public uint FormatHtml { get { EnsureClipFormats(); return _fmtHtml; } }
        public uint FormatRtf { get { EnsureClipFormats(); return _fmtRtf; } }

        public bool IsOwnerHung() {
            IntPtr owner = GetClipboardOwner();
            return (owner != IntPtr.Zero) && (owner != _clipHwnd) && IsHungAppWindow(owner);
        }

        public bool Open() {
            IntPtr hwnd = EnsureClipHwnd();
            if (hwnd == IntPtr.Zero) return false;
            return OpenClipboard(hwnd);
        }

        public void Close() { CloseClipboard(); }
        public bool IsEmptyWhenOpen() { return EnumClipboardFormats(0u) == 0u; }
        public bool HasFormatWhenOpen(uint fmt) { return (fmt != 0u) && IsClipboardFormatAvailable(fmt); }

        public ClipReadStatus ReadFormatBytesWhenOpen(uint fmt, int maxBytes, out byte[] data) {
            data = null;
            IntPtr hMem = GetClipboardData(fmt);
            if (hMem == IntPtr.Zero) return ClipReadStatus.Missing;
            ulong sz = (ulong)GlobalSize(hMem);
            if (sz == 0UL) return ClipReadStatus.Missing;
            if (sz > (ulong)maxBytes) return ClipReadStatus.Oversize;
            IntPtr ptr = GlobalLock(hMem);
            if (ptr == IntPtr.Zero) return ClipReadStatus.Missing;
            try {
                data = new byte[(int)sz];
                Marshal.Copy(ptr, data, 0, (int)sz);
                return ClipReadStatus.Ok;
            } finally {
                GlobalUnlock(hMem);
            }
        }

        public bool EmptyWhenOpen() { return EmptyClipboard(); }

        public bool SetFormatBytesWhenOpen(uint fmt, byte[] data) {
            if (fmt == 0u || data == null) return false;
            int len = data.Length;
            IntPtr hMem = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, new UIntPtr((uint)Math.Max(1, len)));
            if (hMem == IntPtr.Zero) return false;
            bool transferred = false;
            try {
                IntPtr ptr = GlobalLock(hMem);
                if (ptr == IntPtr.Zero) return false;
                try {
                    if (len > 0) Marshal.Copy(data, 0, ptr, len);
                } finally {
                    GlobalUnlock(hMem);
                }
                if (SetClipboardData(fmt, hMem) != IntPtr.Zero) {
                    transferred = true;
                    return true;
                }
                return false;
            } finally {
                if (!transferred) GlobalFree(hMem);
            }
        }
    }

    private static readonly ClipGuardState _clipState = new ClipGuardState();
    private static readonly Win32ClipStore _win32ClipStore = new Win32ClipStore();

    public static byte[] EncodeUnicodeClipBytes(string s) {
        byte[] body = Encoding.Unicode.GetBytes(s ?? "");
        byte[] raw = new byte[body.Length + 2];
        if (body.Length > 0) Buffer.BlockCopy(body, 0, raw, 0, body.Length);
        return raw;
    }

    public static string DecodeUnicodeClipBytes(byte[] raw) {
        if (raw == null || raw.Length == 0) return "";
        int evenLen = raw.Length - (raw.Length % 2);
        int end = 0;
        while (end + 1 < evenLen && !(raw[end] == 0 && raw[end + 1] == 0)) end += 2;
        return (end > 0) ? Encoding.Unicode.GetString(raw, 0, end) : "";
    }

    private static bool HasMatchingTransientTokenWhenOpen(IClipStore store, ulong token) {
        if (token == 0UL || store.FormatAviTransient == 0u || !store.HasFormatWhenOpen(store.FormatAviTransient)) return false;
        byte[] raw;
        ClipReadStatus st = store.ReadFormatBytesWhenOpen(store.FormatAviTransient, 64, out raw);
        if (st != ClipReadStatus.Ok || raw == null || raw.Length < 8) return false;
        return BitConverter.ToUInt64(raw, 0) == token;
    }

    private static ClipSnap CaptureOpenClip(IClipStore store, Func<bool> isDeadlineExceededFn = null) {
        if (store.IsEmptyWhenOpen()) return ClipSnap.ClearOnly;
        if (store.FormatViewerIgnore != 0u && store.HasFormatWhenOpen(store.FormatViewerIgnore)) return ClipSnap.ClearOnly;
        if (store.FormatViewerIgnoreSpaced != 0u && store.HasFormatWhenOpen(store.FormatViewerIgnoreSpaced)) return ClipSnap.ClearOnly;
        if (store.FormatAviTransient != 0u && store.HasFormatWhenOpen(store.FormatAviTransient)) return ClipSnap.Unrestorable;
        bool isOwnRestored = (store.FormatAviRestored != 0u) && store.HasFormatWhenOpen(store.FormatAviRestored);
        if (!isOwnRestored && store.FormatExcludeMonitor != 0u && store.HasFormatWhenOpen(store.FormatExcludeMonitor)) return ClipSnap.ClearOnly;
        uint imgDibFmt = store.HasFormatWhenOpen(CF_DIBV5) ? CF_DIBV5 : CF_DIB;
        uint[] whitelist = new uint[] { CF_UNICODETEXT, store.FormatHtml, store.FormatRtf, store.FormatPng, imgDibFmt, CF_HDROP };
        List<ClipEntry> entries = new List<ClipEntry>(whitelist.Length);
        long totalBytes = 0L;
        for (int i = 0; i < whitelist.Length; i++) {
            if (isDeadlineExceededFn != null && isDeadlineExceededFn()) return ClipSnap.Unrestorable;
            uint fmt = whitelist[i];
            if (fmt == 0u || !store.HasFormatWhenOpen(fmt)) continue;
            byte[] data;
            ClipReadStatus st = store.ReadFormatBytesWhenOpen(fmt, MaxClipSnapshotBytes, out data);
            if (st == ClipReadStatus.Oversize) return ClipSnap.Unrestorable;
            if (st == ClipReadStatus.Ok && data != null && data.Length > 0) {
                if (totalBytes + (long)data.Length > (long)MaxClipSnapshotBytes) return ClipSnap.Unrestorable;
                totalBytes += (long)data.Length;
                entries.Add(new ClipEntry(fmt, data));
            }
        }
        return (entries.Count > 0) ? ClipSnap.Data(entries) : ClipSnap.Unrestorable;
    }

    private static void WriteExcludeMarkersWhenOpen(IClipStore store) {
        if (store.FormatExcludeMonitor != 0u) store.SetFormatBytesWhenOpen(store.FormatExcludeMonitor, DwordZeroBytes);
        if (store.FormatCanHistory != 0u) store.SetFormatBytesWhenOpen(store.FormatCanHistory, DwordZeroBytes);
        if (store.FormatCanCloud != 0u) store.SetFormatBytesWhenOpen(store.FormatCanCloud, DwordZeroBytes);
    }

    private static void CancelClipRestoreCore(ClipGuardState st, Action stopTimerFn) {
        if (stopTimerFn != null) stopTimerFn();
        st.RestorePending = false;
        st.ExpectedToken = 0UL;
        st.RetryCount = 0;
        st.SavedClip = null;
    }

    private static bool WriteClipTextCore(ClipGuardState st, IClipStore store, string text, bool transientForPaste, Action armTimerFn, Action stopTimerFn, Func<bool> isDeadlineExceededFn = null) {
        if (isDeadlineExceededFn != null && isDeadlineExceededFn()) return false;
        if (!transientForPaste) {
            CancelClipRestoreCore(st, stopTimerFn);
            if (!store.Open()) return false;
            try {
                if (isDeadlineExceededFn != null && isDeadlineExceededFn()) return false;
                if (!store.EmptyWhenOpen()) return false;
                if (isDeadlineExceededFn != null && isDeadlineExceededFn()) return false;
                if (!string.IsNullOrEmpty(text)) {
                    if (!store.SetFormatBytesWhenOpen(CF_UNICODETEXT, EncodeUnicodeClipBytes(text))) return false;
                }
                return true;
            } finally {
                store.Close();
            }
        }
        bool preHung = store.IsOwnerHung();
        if (!store.Open()) return false;
        ClipSnap nextSnap = null;
        ulong myToken = 0UL;
        bool wroteOk = false;
        try {
            if (stopTimerFn != null) stopTimerFn();
            try {
                bool reuseExisting = st.RestorePending && (store.SequenceNumber == st.ExpectedSeq) && HasMatchingTransientTokenWhenOpen(store, st.ExpectedToken);
                bool ownerHung = (!reuseExisting) && (preHung || store.IsOwnerHung());
                nextSnap = reuseExisting ? st.SavedClip : (ownerHung ? ClipSnap.Unrestorable : CaptureOpenClip(store, isDeadlineExceededFn));
            } catch {
                nextSnap = ClipSnap.Unrestorable;
            }
            if (isDeadlineExceededFn != null && isDeadlineExceededFn()) return false;
            myToken = ++st.NextToken;
            if (myToken == 0UL) myToken = ++st.NextToken;
            if (!store.EmptyWhenOpen()) return false;
            if (isDeadlineExceededFn != null && isDeadlineExceededFn()) return false;
            if (store.SetFormatBytesWhenOpen(CF_UNICODETEXT, EncodeUnicodeClipBytes(text ?? ""))) {
                WriteExcludeMarkersWhenOpen(store);
                if (store.FormatAviTransient != 0u) store.SetFormatBytesWhenOpen(store.FormatAviTransient, BitConverter.GetBytes(myToken));
                wroteOk = true;
            }
        } finally {
            store.Close();
        }
        if (!wroteOk) return false;
        if (nextSnap != null && nextSnap.Kind != SnapKind.Unrestorable) {
            st.SavedClip = nextSnap;
            st.ExpectedToken = myToken;
            st.ExpectedSeq = store.SequenceNumber;
            st.RetryCount = 0;
            st.RestorePending = true;
            if (armTimerFn != null) armTimerFn();
        } else {
            st.SavedClip = null;
            st.ExpectedToken = 0UL;
            st.RetryCount = 0;
            st.RestorePending = false;
        }
        return true;
    }

    private static void FlushClipRestoreCore(ClipGuardState st, IClipStore store, Action retryTimerFn, Action stopTimerFn) {
        if (!st.RestorePending) return;
        ClipSnap snap = st.SavedClip;
        uint expSeq = st.ExpectedSeq;
        ulong expTok = st.ExpectedToken;
        if (snap == null || snap.Kind == SnapKind.Unrestorable || store.SequenceNumber != expSeq) {
            CancelClipRestoreCore(st, stopTimerFn);
            return;
        }
        bool opened = false;
        int syncTries = (retryTimerFn != null) ? 1 : 3;
        for (int a = 0; a < syncTries; a++) {
            if (store.Open()) { opened = true; break; }
        }
        if (!opened) {
            if (retryTimerFn != null && st.RetryCount < MaxClipRestoreRetries) {
                st.RetryCount++;
                retryTimerFn();
                return;
            }
            CancelClipRestoreCore(st, stopTimerFn);
            return;
        }
        try {
            CancelClipRestoreCore(st, stopTimerFn);
            if (store.SequenceNumber != expSeq || !HasMatchingTransientTokenWhenOpen(store, expTok)) return;
            if (!store.EmptyWhenOpen()) return;
            if (snap.Kind == SnapKind.Data && snap.Entries != null) {
                for (int i = 0; i < snap.Entries.Count; i++) {
                    ClipEntry e = snap.Entries[i];
                    store.SetFormatBytesWhenOpen(e.Format, e.Data);
                }
                WriteExcludeMarkersWhenOpen(store);
                if (store.FormatAviRestored != 0u) store.SetFormatBytesWhenOpen(store.FormatAviRestored, DwordZeroBytes);
            }
        } finally {
            store.Close();
        }
    }

    private static void StopMainClipTimer() {
        if (_clipRestoreTimer != null) { try { _clipRestoreTimer.Stop(); } catch { } }
    }

    private static void StartMainClipTimerMs(int delayMs) {
        if (_clipRestoreTimer == null) {
            _clipRestoreTimer = new System.Windows.Forms.Timer();
            _clipRestoreTimer.Tick += (s, e) => {
                StopMainClipTimer();
                FlushClipRestoreCore(_clipState, _win32ClipStore, ArmRetryClipTimer, StopMainClipTimer);
            };
        }
        _clipRestoreTimer.Stop();
        _clipRestoreTimer.Interval = Math.Max(10, delayMs);
        _clipRestoreTimer.Start();
    }

    private static void ArmMainClipTimer() {
        StartMainClipTimerMs(ClipRestoreDelayMs);
    }

    private static void ArmRetryClipTimer() {
        StartMainClipTimerMs(ClipRestoreRetryDelayMs);
    }

    public static void CancelPendingClipRestore() {
        CancelClipRestoreCore(_clipState, StopMainClipTimer);
    }

    public static bool WriteClipTextOnce(string text, bool transientForPaste) {
        return WriteClipTextCore(_clipState, _win32ClipStore, text, transientForPaste, ArmMainClipTimer, StopMainClipTimer, null);
    }

    public static bool WriteClipTextOnce(string text, bool transientForPaste, long startTick, int deadlineMs) {
        Func<bool> expiredFn = (deadlineMs > 0) ? (Func<bool>)(() => IsDeadlineExceeded(startTick, deadlineMs)) : null;
        return WriteClipTextCore(_clipState, _win32ClipStore, text, transientForPaste, ArmMainClipTimer, StopMainClipTimer, expiredFn);
    }

    public static void FlushPendingClipRestore() {
        FlushClipRestoreCore(_clipState, _win32ClipStore, null, StopMainClipTimer);
    }
}
