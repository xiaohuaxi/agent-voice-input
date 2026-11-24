using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static partial class VoiceWinShim {
    public static void RunStoppedRecSlotSelfCheck() {
        try {
            ReleaseStoppedRecording(0L);
            byte[] pcm1 = new byte[320];
            for (int i = 0; i < pcm1.Length; i += 2) { pcm1[i] = 0x34; pcm1[i + 1] = 0x12; }
            byte[] wav1 = BuildPcm16MonoWav(pcm1, pcm1.Length, 16000);
            StoreStoppedRecSlot(101L, wav1, pcm1.Length, "");

            // 1. Idempotent retry returns identical WAV bytes for matching rec_gen
            int pb1 = 0, pb2 = 0; string err1 = "", err2 = "";
            byte[] r1 = StopRecording(101L, out pb1, out err1, true);
            byte[] r2 = StopRecording(101L, out pb2, out err2, true);
            if (r1 == null || r1.Length != wav1.Length || pb1 != pcm1.Length || !string.IsNullOrEmpty(err1))
                throw new InvalidOperationException("StoppedRecSlot retry 1 mismatch");
            if (r2 == null || r2.Length != wav1.Length || pb2 != pcm1.Length || !string.IsNullOrEmpty(err2))
                throw new InvalidOperationException("StoppedRecSlot retry 2 mismatch");

            // 2. Mismatched rec_gen never leaks previous slot
            int pbWrong = 0; string errWrong = "";
            byte[] rWrong = StopRecording(999L, out pbWrong, out errWrong, true);
            if (rWrong.Length != 0 || errWrong != "not_recording")
                throw new InvalidOperationException("StoppedRecSlot mismatched rec_gen leaked audio");

            // 3. Mismatched ReleaseStoppedRecording does not evict slot 101
            ReleaseStoppedRecording(999L);
            int pbStill = 0; string errStill = "";
            byte[] rStill = StopRecording(101L, out pbStill, out errStill, true);
            if (rStill.Length != wav1.Length)
                throw new InvalidOperationException("StoppedRecSlot wrong-gen release evicted active slot");

            // 4. Matching ReleaseStoppedRecording frees slot 101
            ReleaseStoppedRecording(101L);
            int pbGone = 0; string errGone = "";
            byte[] rGone = StopRecording(101L, out pbGone, out errGone, true);
            if (rGone.Length != 0 || errGone != "not_recording")
                throw new InvalidOperationException("StoppedRecSlot release failed to evict slot");

            // 5. Overwriting slot with newer generation drops old generation cleanly
            byte[] pcm2 = new byte[160];
            byte[] wav2 = BuildPcm16MonoWav(pcm2, pcm2.Length, 16000);
            StoreStoppedRecSlot(102L, wav1, pcm1.Length, "");
            StoreStoppedRecSlot(103L, wav2, pcm2.Length, "");
            int pbOld = 0, pbNew = 0; string errOld = "", errNew = "";
            byte[] rOld = StopRecording(102L, out pbOld, out errOld, true);
            byte[] rNew = StopRecording(103L, out pbNew, out errNew, true);
            if (rOld.Length != 0 || errOld != "not_recording")
                throw new InvalidOperationException("StoppedRecSlot overwrite kept old gen");
            if (rNew.Length != wav2.Length || pbNew != pcm2.Length)
                throw new InvalidOperationException("StoppedRecSlot overwrite corrupted new gen");

            // 6. CancelRecording / discard clears single slot immediately
            CancelRecording(103L);
            int pbCan = 0; string errCan = "";
            byte[] rCan = StopRecording(103L, out pbCan, out errCan, true);
            if (rCan.Length != 0 || errCan != "not_recording")
                throw new InvalidOperationException("StoppedRecSlot cancel failed to clear slot");

            // 7. Mismatched rec_gen StopRecording / CancelRecording never touches an active newer RecSession
            RecSession guardSess = new RecSession { RecGen = 200L, Run = true };
            try {
                lock (_recSync) { _recSess = guardSess; }
                CancelRecording(199L);
                int pbMis = 0; string errMis = "";
                byte[] rMis = StopRecording(199L, out pbMis, out errMis, true);
                if (rMis.Length != 0 || errMis != "not_recording")
                    throw new InvalidOperationException("Mismatched StopRecording returned unexpected audio");
                lock (_recSync) {
                    if (!object.ReferenceEquals(_recSess, guardSess) || !guardSess.Run)
                        throw new InvalidOperationException("Mismatched rec_gen touched active RecSession");
                }
            } finally {
                lock (_recSync) {
                    if (object.ReferenceEquals(_recSess, guardSess)) _recSess = null;
                }
            }
        } finally {
            ReleaseStoppedRecording(0L);
        }
    }

    private sealed class MemClipStore : IClipStore {
        public uint Seq = 10u;
        public bool Hung = false;
        public int FailNextOpens = 0;
        public int OversizeFmt = 0;
        public bool ThrowOnRead = false;
        public Action OnRead = null;
        public Action OnEmpty = null;
        public readonly Dictionary<uint, byte[]> Formats = new Dictionary<uint, byte[]>();
        public uint SequenceNumber { get { return Seq; } }
        public uint FormatExcludeMonitor { get { return 0xC101u; } }
        public uint FormatCanHistory { get { return 0xC102u; } }
        public uint FormatCanCloud { get { return 0xC103u; } }
        public uint FormatViewerIgnore { get { return 0xC104u; } }
        public uint FormatViewerIgnoreSpaced { get { return 0xC108u; } }
        public uint FormatAviTransient { get { return 0xC109u; } }
        public uint FormatAviRestored { get { return 0xC10Au; } }
        public uint FormatPng { get { return 0xC105u; } }
        public uint FormatHtml { get { return 0xC106u; } }
        public uint FormatRtf { get { return 0xC107u; } }
        public bool IsOwnerHung() { return Hung; }
        public bool Open() { if (FailNextOpens > 0) { FailNextOpens--; return false; } return true; }
        public void Close() { }
        public bool IsEmptyWhenOpen() { return Formats.Count == 0; }
        public bool HasFormatWhenOpen(uint fmt) { return Formats.ContainsKey(fmt); }
        public ClipReadStatus ReadFormatBytesWhenOpen(uint fmt, int maxBytes, out byte[] data) {
            data = null;
            if (OnRead != null) OnRead();
            if (ThrowOnRead) throw new InvalidOperationException("simulated_read_fault");
            if ((uint)OversizeFmt == fmt) return ClipReadStatus.Oversize;
            byte[] src;
            if (!Formats.TryGetValue(fmt, out src) || src == null) return ClipReadStatus.Missing;
            if (src.Length > maxBytes) return ClipReadStatus.Oversize;
            data = new byte[src.Length];
            Buffer.BlockCopy(src, 0, data, 0, src.Length);
            return ClipReadStatus.Ok;
        }
        public bool EmptyWhenOpen() { if (OnEmpty != null) OnEmpty(); Formats.Clear(); Seq++; return true; }
        public bool SetFormatBytesWhenOpen(uint fmt, byte[] data) {
            byte[] cp = new byte[data.Length];
            Buffer.BlockCopy(data, 0, cp, 0, data.Length);
            Formats[fmt] = cp;
            return true;
        }
        public void SimulateUserCopy(uint fmt, byte[] data) {
            Formats.Clear();
            SetFormatBytesWhenOpen(fmt, data);
            Seq++;
        }
    }

    public static void RunClipStateSelfCheck() {
        if (MaxClipSnapshotBytes != 10 * 1024 * 1024) throw new InvalidOperationException("MaxClipSnapshotBytes expected 10MiB");
        MemClipStore mem = new MemClipStore();
        ClipGuardState st = new ClipGuardState();
        int armed = 0, retried = 0, stopped = 0;
        Action armFn = () => { armed++; };
        Action retryFn = () => { retried++; };
        Action stopFn = () => { stopped++; };
        byte[] png0 = new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A };
        mem.Formats[mem.FormatPng] = png0;
        mem.Formats[CF_UNICODETEXT] = EncodeUnicodeClipBytes("orig_caption");

        // 1. Rapid-fire #1 + #2 (and transient lock retry during flush) preserves original PNG + text
        if (!WriteClipTextCore(st, mem, "voice_one", true, armFn, stopFn)) throw new InvalidOperationException("step1 write failed");
        if (!st.RestorePending || !mem.Formats.ContainsKey(mem.FormatExcludeMonitor) || !mem.Formats.ContainsKey(mem.FormatCanHistory) || !mem.Formats.ContainsKey(mem.FormatAviTransient)) throw new InvalidOperationException("step1 markers missing");
        if (BitConverter.ToInt32(mem.Formats[mem.FormatCanHistory], 0) != 0) throw new InvalidOperationException("step1 DWORD 0 invalid");
        mem.FailNextOpens = 1;
        if (WriteClipTextCore(st, mem, "voice_two", true, armFn, stopFn)) throw new InvalidOperationException("step2 lock simulation failed");
        if (!WriteClipTextCore(st, mem, "voice_two", true, armFn, stopFn)) throw new InvalidOperationException("step2 retry failed");
        if (DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "voice_two") throw new InvalidOperationException("step2 text mismatch");
        mem.FailNextOpens = 2;
        FlushClipRestoreCore(st, mem, retryFn, stopFn);
        if (!st.RestorePending || retried != 1) throw new InvalidOperationException("step2 flush lock retry 1 failed");
        FlushClipRestoreCore(st, mem, retryFn, stopFn);
        if (!st.RestorePending || retried != 2) throw new InvalidOperationException("step2 flush lock retry 2 failed");
        FlushClipRestoreCore(st, mem, retryFn, stopFn);
        if (st.RestorePending || st.SavedClip != null || !mem.Formats.ContainsKey(mem.FormatPng) || mem.Formats[mem.FormatPng].Length != png0.Length) throw new InvalidOperationException("step2 PNG not restored");
        if (DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "orig_caption") throw new InvalidOperationException("step2 text not restored");
        if (!mem.Formats.ContainsKey(mem.FormatAviRestored)) throw new InvalidOperationException("step2 AviRestored marker missing");

        // 1b. Subsequent dictation after a previous restore still re-snapshots restored ordinary content (not mistaken for password!)
        if (!WriteClipTextCore(st, mem, "voice_after_restore", true, armFn, stopFn)) throw new InvalidOperationException("step2b write failed");
        FlushClipRestoreCore(st, mem, null, stopFn);
        if (st.SavedClip != null || !mem.Formats.ContainsKey(mem.FormatPng) || DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "orig_caption") throw new InvalidOperationException("step2b ordinary restored clip clobbered");

        // 1c. CF_DIBV5 + synthesized CF_DIB coexistence captures only CF_DIBV5 (never double-counts bitmap quota), and falls back to CF_DIB alone
        byte[] dibV5Sample = new byte[] { 0x7C, 0x00, 0x00, 0x00, 0xAA, 0xBB };
        byte[] dibLegacySample = new byte[] { 0x28, 0x00, 0x00, 0x00, 0xCC, 0xDD };
        mem.Formats.Clear();
        mem.Formats[CF_DIBV5] = dibV5Sample;
        mem.Formats[CF_DIB] = dibLegacySample;
        mem.Seq++;
        if (!WriteClipTextCore(st, mem, "voice_dibv5", true, armFn, stopFn)) throw new InvalidOperationException("step2c dibv5 write failed");
        if (st.SavedClip == null || st.SavedClip.Entries == null || st.SavedClip.Entries.Count != 1 || st.SavedClip.Entries[0].Format != CF_DIBV5)
            throw new InvalidOperationException("step2c expected exclusive CF_DIBV5 capture without CF_DIB duplication");
        FlushClipRestoreCore(st, mem, null, stopFn);
        if (st.SavedClip != null || !mem.Formats.ContainsKey(CF_DIBV5) || mem.Formats.ContainsKey(CF_DIB))
            throw new InvalidOperationException("step2c CF_DIBV5 restore state mismatch");
        mem.Formats.Clear();
        mem.Formats[CF_DIB] = dibLegacySample;
        mem.Seq++;
        if (!WriteClipTextCore(st, mem, "voice_dib_only", true, armFn, stopFn)) throw new InvalidOperationException("step2d dib fallback write failed");
        FlushClipRestoreCore(st, mem, null, stopFn);
        if (st.SavedClip != null || !mem.Formats.ContainsKey(CF_DIB))
            throw new InvalidOperationException("step2d CF_DIB fallback restore failed");
        mem.Formats.Clear();
        mem.Formats[mem.FormatPng] = png0;
        mem.Formats[CF_UNICODETEXT] = EncodeUnicodeClipBytes("orig_caption");
        mem.Seq++;

        // 2. User manual Ctrl+C during 250ms window is never overwritten by timer OR subsequent #2
        WriteClipTextCore(st, mem, "voice_three", true, armFn, stopFn);
        mem.SimulateUserCopy(CF_UNICODETEXT, EncodeUnicodeClipBytes("user_manual_copy"));
        FlushClipRestoreCore(st, mem, null, stopFn);
        if (st.SavedClip != null || DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "user_manual_copy" || mem.Formats.ContainsKey(mem.FormatPng)) throw new InvalidOperationException("step3 clobbered user copy");

        WriteClipTextCore(st, mem, "voice_four_a", true, armFn, stopFn);
        mem.SimulateUserCopy(CF_UNICODETEXT, EncodeUnicodeClipBytes("user_copy_between_bursts"));
        WriteClipTextCore(st, mem, "voice_four_b", true, armFn, stopFn);
        FlushClipRestoreCore(st, mem, null, stopFn);
        if (st.SavedClip != null || DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "user_copy_between_bursts") throw new InvalidOperationException("step3b burst did not re-baseline user copy");

        // 3. Focus-drift persistent fallback cancels pending timer and omits exclusion markers
        mem.SimulateUserCopy(mem.FormatPng, png0);
        WriteClipTextCore(st, mem, "voice_five", true, armFn, stopFn);
        WriteClipTextCore(st, mem, "fallback_keep_me", false, armFn, stopFn);
        if (st.RestorePending || st.SavedClip != null || mem.Formats.ContainsKey(mem.FormatExcludeMonitor) || mem.Formats.ContainsKey(mem.FormatCanHistory) || mem.Formats.ContainsKey(mem.FormatAviTransient)) throw new InvalidOperationException("step4 fallback kept transient markers");
        FlushClipRestoreCore(st, mem, null, stopFn);
        if (DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "fallback_keep_me") throw new InvalidOperationException("step4 fallback wiped");

        // 4. Password manager markers (KeePass 'Clipboard Viewer Ignore', 'ClipboardViewerIgnore', external 'ExcludeClipboardContentFromMonitorProcessing') -> ClearOnly
        foreach (uint pwFmt in new uint[] { mem.FormatViewerIgnore, mem.FormatViewerIgnoreSpaced, mem.FormatExcludeMonitor }) {
            mem.Formats.Clear();
            mem.Formats[CF_UNICODETEXT] = EncodeUnicodeClipBytes("s3cr3t_pw");
            mem.Formats[pwFmt] = DwordZeroBytes;
            mem.Seq++;
            WriteClipTextCore(st, mem, "voice_six", true, armFn, stopFn);
            FlushClipRestoreCore(st, mem, null, stopFn);
            if (st.SavedClip != null || mem.Formats.Count != 0) throw new InvalidOperationException("step5 password secret not cleared for fmt " + pwFmt);
        }

        // 5. Oversize (>10MiB) or hung owner -> Unrestorable, and #2 burst does not snapshot #1's transient text
        mem.Formats.Clear();
        mem.Formats[mem.FormatPng] = png0;
        mem.OversizeFmt = (int)mem.FormatPng;
        mem.Seq++;
        WriteClipTextCore(st, mem, "voice_seven", true, armFn, stopFn);
        mem.OversizeFmt = 0;
        if (st.RestorePending || st.SavedClip != null) throw new InvalidOperationException("step6 oversize armed restore");
        WriteClipTextCore(st, mem, "voice_seven_b", true, armFn, stopFn);
        if (st.RestorePending || st.SavedClip != null) throw new InvalidOperationException("step6b burst after oversize armed restore");
        FlushClipRestoreCore(st, mem, null, stopFn);
        if (DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "voice_seven_b") throw new InvalidOperationException("step6 oversize wiped clipboard");

        mem.Formats.Clear();
        mem.Formats[CF_UNICODETEXT] = EncodeUnicodeClipBytes("from_hung_app");
        mem.Hung = true;
        mem.Seq++;
        WriteClipTextCore(st, mem, "voice_eight", true, armFn, stopFn);
        mem.Hung = false;
        if (st.RestorePending || st.SavedClip != null) throw new InvalidOperationException("step7 hung owner armed restore");

        // 6. Snapshot read exception -> voice-first: skips restore, still writes voice text; timer stopped before EmptyWhenOpen
        mem.Formats.Clear();
        mem.Formats[CF_UNICODETEXT] = EncodeUnicodeClipBytes("faulty_source");
        mem.Seq++;
        int stoppedBeforeEmpty = stopped;
        bool stopCalledBeforeEmpty = false;
        mem.OnEmpty = () => { stopCalledBeforeEmpty = (stopped > stoppedBeforeEmpty); };
        mem.ThrowOnRead = true;
        bool wroteOnReadErr = WriteClipTextCore(st, mem, "voice_nine_priority", true, armFn, stopFn);
        mem.ThrowOnRead = false;
        mem.OnEmpty = null;
        if (!wroteOnReadErr) throw new InvalidOperationException("step8 read exception refused voice paste");
        if (!stopCalledBeforeEmpty) throw new InvalidOperationException("step8 timer not stopped before EmptyWhenOpen");
        if (st.RestorePending || st.SavedClip != null) throw new InvalidOperationException("step8 read exception armed restore");
        if (DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "voice_nine_priority") throw new InvalidOperationException("step8 voice text not written");

        // 7. Monotonic deadline_ms guard prevents late clipboard overwrite before open and after slow delayed-render read
        long nowSw = Stopwatch.GetTimestamp();
        if (IsDeadlineExceeded(nowSw, 0)) throw new InvalidOperationException("step9 zero deadline should never expire");
        if (IsDeadlineExceeded(nowSw, 60000)) throw new InvalidOperationException("step9 future deadline should not expire");
        if (!IsDeadlineExceeded(nowSw - Stopwatch.Frequency, 500)) throw new InvalidOperationException("step9 past deadline should expire");
        mem.Formats.Clear();
        mem.Formats[CF_UNICODETEXT] = EncodeUnicodeClipBytes("user_intact_clip");
        mem.Seq++;
        bool emptyCalledOnDeadline = false;
        mem.OnEmpty = () => { emptyCalledOnDeadline = true; };
        if (WriteClipTextCore(st, mem, "late_transient", true, armFn, stopFn, () => true))
            throw new InvalidOperationException("step9 pre-expired transient write should abort");
        if (WriteClipTextCore(st, mem, "late_persistent", false, armFn, stopFn, () => true))
            throw new InvalidOperationException("step9 pre-expired persistent write should abort");
        bool slowExpired = false;
        mem.OnRead = () => { slowExpired = true; };
        if (WriteClipTextCore(st, mem, "late_after_slow_render", true, armFn, stopFn, () => slowExpired))
            throw new InvalidOperationException("step9 slow-render expired write should abort");
        mem.OnRead = null;
        mem.OnEmpty = null;
        if (emptyCalledOnDeadline) throw new InvalidOperationException("step9 expired write touched EmptyWhenOpen");
        if (st.RestorePending || st.SavedClip != null) throw new InvalidOperationException("step9 expired write armed restore");
        if (DecodeUnicodeClipBytes(mem.Formats[CF_UNICODETEXT]) != "user_intact_clip")
            throw new InvalidOperationException("step9 expired write clobbered user clipboard");
    }

    private static bool OpenWin32ClipWithRetry(Win32ClipStore store) {
        for (int i = 0; i < 5; i++) {
            if (store.Open()) return true;
            Thread.Sleep(15);
        }
        return false;
    }

    public static void RunRealWin32ClipStoreSelfCheck() {
        Win32ClipStore store = _win32ClipStore;
        ClipGuardState st = new ClipGuardState();
        int armed = 0, stopped = 0;
        Action armFn = () => { armed++; };
        Action stopFn = () => { stopped++; };
        string runTag = "avi_sc_" + Guid.NewGuid().ToString("N");
        string baseText = "\u6D4B\u8BD5_" + runTag + "_base";
        string tempText = "\u8BED\u97F3\u4E34\u65F6_" + runTag + "_temp";
        byte[] basePng = new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D };
        uint pngFmt = store.FormatPng;
        if (pngFmt == 0u) throw new InvalidOperationException("win32 clip check: PNG format registration failed");
        bool wroteSelfCheck = false;
        uint lastOwnedSeq = 0u;
        byte[] baselinePng = null;
        try {
            if (!OpenWin32ClipWithRetry(store)) throw new InvalidOperationException("win32 clip check: initial OpenClipboard failed");
            try {
                if (!store.IsEmptyWhenOpen())
                    throw new InvalidOperationException("win32 clip check: refusing to run -RealClipboard on a non-empty clipboard");
                if (!store.EmptyWhenOpen()) throw new InvalidOperationException("win32 clip check: initial EmptyClipboard failed");
                wroteSelfCheck = true;
                if (!store.SetFormatBytesWhenOpen(CF_UNICODETEXT, EncodeUnicodeClipBytes(baseText)))
                    throw new InvalidOperationException("win32 clip check: SetClipboardData CF_UNICODETEXT failed");
                if (!store.SetFormatBytesWhenOpen(pngFmt, basePng))
                    throw new InvalidOperationException("win32 clip check: SetClipboardData PNG failed");
                if (store.ReadFormatBytesWhenOpen(pngFmt, 4096, out baselinePng) != ClipReadStatus.Ok || baselinePng == null || baselinePng.Length < basePng.Length)
                    throw new InvalidOperationException("win32 clip check: failed to read back baseline PNG snapshot");
                for (int i = 0; i < basePng.Length; i++) {
                    if (baselinePng[i] != basePng[i])
                        throw new InvalidOperationException("win32 clip check: baseline PNG prefix mismatch at " + i);
                }
            } finally {
                store.Close();
            }
            // Win32 CloseClipboard() bumps GetClipboardSequenceNumber() when committing writes;
            // sample lastOwnedSeq strictly after Close(), matching WriteClipTextCore.
            lastOwnedSeq = store.SequenceNumber;

            if (!WriteClipTextCore(st, store, tempText, true, armFn, stopFn))
                throw new InvalidOperationException("win32 clip check: transient WriteClipTextCore failed");
            lastOwnedSeq = store.SequenceNumber;
            if (!st.RestorePending || armed != 1)
                throw new InvalidOperationException("win32 clip check: transient write did not arm restore");

            if (!OpenWin32ClipWithRetry(store)) throw new InvalidOperationException("win32 clip check: OpenClipboard after transient write failed");
            try {
                byte[] curTxtRaw;
                if (store.ReadFormatBytesWhenOpen(CF_UNICODETEXT, 4096, out curTxtRaw) != ClipReadStatus.Ok)
                    throw new InvalidOperationException("win32 clip check: failed to read transient CF_UNICODETEXT");
                if (DecodeUnicodeClipBytes(curTxtRaw) != tempText)
                    throw new InvalidOperationException("win32 clip check: transient CF_UNICODETEXT mismatch");
                if (!store.HasFormatWhenOpen(store.FormatAviTransient) || !store.HasFormatWhenOpen(store.FormatExcludeMonitor) || !store.HasFormatWhenOpen(store.FormatCanHistory))
                    throw new InvalidOperationException("win32 clip check: transient exclusion markers missing");
            } finally {
                store.Close();
            }
            lastOwnedSeq = store.SequenceNumber;

            FlushClipRestoreCore(st, store, null, stopFn);
            lastOwnedSeq = store.SequenceNumber;
            if (st.RestorePending || st.SavedClip != null)
                throw new InvalidOperationException("win32 clip check: FlushClipRestoreCore did not clear pending state");

            if (!OpenWin32ClipWithRetry(store)) throw new InvalidOperationException("win32 clip check: OpenClipboard after restore failed");
            try {
                byte[] restTxtRaw;
                if (store.ReadFormatBytesWhenOpen(CF_UNICODETEXT, 4096, out restTxtRaw) != ClipReadStatus.Ok)
                    throw new InvalidOperationException("win32 clip check: failed to read restored CF_UNICODETEXT");
                if (DecodeUnicodeClipBytes(restTxtRaw) != baseText)
                    throw new InvalidOperationException("win32 clip check: restored CF_UNICODETEXT mismatch");
                byte[] restPng;
                if (store.ReadFormatBytesWhenOpen(pngFmt, 4096, out restPng) != ClipReadStatus.Ok || restPng == null || restPng.Length != baselinePng.Length)
                    throw new InvalidOperationException("win32 clip check: restored PNG missing or length mismatch vs baseline");
                for (int i = 0; i < baselinePng.Length; i++) {
                    if (restPng[i] != baselinePng[i])
                        throw new InvalidOperationException("win32 clip check: restored PNG byte mismatch at " + i);
                }
                for (int i = 0; i < basePng.Length; i++) {
                    if (restPng[i] != basePng[i])
                        throw new InvalidOperationException("win32 clip check: restored PNG prefix mismatch at " + i);
                }
                if (!store.HasFormatWhenOpen(store.FormatAviRestored))
                    throw new InvalidOperationException("win32 clip check: FormatAviRestored marker missing after restore");
            } finally {
                store.Close();
            }
            lastOwnedSeq = store.SequenceNumber;
        } finally {
            CancelClipRestoreCore(st, stopFn);
            if (wroteSelfCheck && OpenWin32ClipWithRetry(store)) {
                try {
                    if (store.SequenceNumber == lastOwnedSeq) {
                        byte[] curRaw;
                        if (store.ReadFormatBytesWhenOpen(CF_UNICODETEXT, 4096, out curRaw) == ClipReadStatus.Ok) {
                            string curTxt = DecodeUnicodeClipBytes(curRaw);
                            if (curTxt.IndexOf(runTag, StringComparison.Ordinal) >= 0 && (curTxt == baseText || curTxt == tempText)) {
                                store.EmptyWhenOpen();
                            }
                        }
                    }
                } finally {
                    store.Close();
                }
            }
            if (_clipHwnd != IntPtr.Zero) {
                try { DestroyWindow(_clipHwnd); } catch { }
                _clipHwnd = IntPtr.Zero;
            }
        }
    }

    private static bool RegionHasDistinctForeground(byte[] px, int stride, int bmpW, int bmpH, Rectangle rc, byte bgB, byte bgG, byte bgR) {
        int x0 = Math.Max(0, rc.Left);
        int x1 = Math.Min(bmpW, rc.Right);
        int y0 = Math.Max(2, rc.Top);
        int y1 = Math.Min(bmpH - 2, rc.Bottom);
        for (int y = y0; y < y1; y++) {
            int row = y * stride;
            for (int x = x0; x < x1; x++) {
                int idx = row + x * 4;
                byte a = px[idx + 3];
                if (a < 180) continue;
                int db = Math.Abs(px[idx] - bgB);
                int dg = Math.Abs(px[idx + 1] - bgG);
                int dr = Math.Abs(px[idx + 2] - bgR);
                if (db + dg + dr >= 36) return true;
            }
        }
        return false;
    }

    private static void VerifyHudLayoutCase(string label, string state, string customText, int dpi, int workW, int workH, bool zh, bool isToast, long elapsedSec, int maxDurSec, int warnRemSec, int bgCount, bool holdCancelPreview = false) {
        float[] bars = new float[] { 0.15f, 0.35f, 0.65f, 0.90f, 1.00f, 0.85f, 0.60f, 0.30f, 0.12f };
        HudLayoutMetrics m;
        using (Bitmap bmp = RenderHudSnapshotWithMetrics(state, customText, dpi, workW, workH, bars, out m, elapsedSec, maxDurSec, warnRemSec, zh, isToast, bgCount, holdCancelPreview)) {
            if (bmp == null) throw new InvalidOperationException(label + ": null bitmap");
            if (bmp.Width != m.Width || bmp.Height != m.Height)
                throw new InvalidOperationException(label + ": bitmap size mismatch");
            if (m.Width <= 0 || m.Height <= 0 || m.Width > m.MaxWidth || m.Width < m.PadL + m.PadR)
                throw new InvalidOperationException(label + ": pill bounds invalid w=" + m.Width + " maxW=" + m.MaxWidth + " h=" + m.Height);
            if (m.Truncated)
                throw new InvalidOperationException(label + ": unexpected truncation w=" + m.Width + " maxW=" + m.MaxWidth);
            if (holdCancelPreview) {
                string expHold = FormatHoldCancelHint(zh);
                if (m.IsWaveRecording && m.HintText != expHold)
                    throw new InvalidOperationException(label + ": wave hold hint mismatch got=" + m.HintText);
                if (!m.IsWaveRecording && (m.HintText != ("(" + expHold + ")") || m.HintSegMeasuredW <= 0))
                    throw new InvalidOperationException(label + ": status hold hint mismatch got=" + m.HintText);
            }

            if (m.IsWaveRecording) {
                if (m.DurRect.Left < m.PadL)
                    throw new InvalidOperationException(label + ": DurRect violates left padding");
                if (m.DurRect.Width < m.DurMeasuredW || m.DurSlotW < m.DurMeasuredW)
                    throw new InvalidOperationException(label + ": DurRect overflows slot");
                if (m.DurRect.Right >= m.WaveRect.Left)
                    throw new InvalidOperationException(label + ": DurRect overlaps WaveRect (" + m.DurRect.Right + " >= " + m.WaveRect.Left + ")");
                if (m.WaveRect.Width <= 0)
                    throw new InvalidOperationException(label + ": WaveRect empty");
                if (!string.IsNullOrEmpty(m.HintText)) {
                    if (m.WaveRect.Right >= m.HintRect.Left)
                        throw new InvalidOperationException(label + ": WaveRect overlaps HintRect (" + m.WaveRect.Right + " >= " + m.HintRect.Left + ")");
                    if (m.HintRect.Width < m.HintMeasuredW || m.HintSlotW < m.HintMeasuredW)
                        throw new InvalidOperationException(label + ": HintRect overflows slot");
                }
                if (bgCount > 0) {
                    if (m.PadR >= m.PadL)
                        throw new InvalidOperationException(label + ": expected optical tighter PadR when badge present (padL=" + m.PadL + " padR=" + m.PadR + ")");
                    string expBadge = "+" + bgCount.ToString(CultureInfo.InvariantCulture);
                    if (m.BadgeText != expBadge)
                        throw new InvalidOperationException(label + ": BadgeText mismatch got=" + m.BadgeText);
                    if (m.BadgeRect.Width < m.BadgeMeasuredW || m.BadgeSlotW < m.BadgeMeasuredW)
                        throw new InvalidOperationException(label + ": BadgeRect overflows slot");
                    int prevRight = (!string.IsNullOrEmpty(m.HintText)) ? m.HintRect.Right : m.WaveRect.Right;
                    if (prevRight >= m.BadgeRect.Left)
                        throw new InvalidOperationException(label + ": BadgeRect overlaps previous segment (" + prevRight + " >= " + m.BadgeRect.Left + ")");
                    if (m.BadgeRect.Right > m.Width - m.PadR)
                        throw new InvalidOperationException(label + ": BadgeRect violates right padding (" + m.BadgeRect.Right + " > " + (m.Width - m.PadR) + ")");
                } else {
                    if (m.PadR != m.PadL)
                        throw new InvalidOperationException(label + ": expected symmetric PadR when no badge (padL=" + m.PadL + " padR=" + m.PadR + ")");
                    if (m.BadgeRect.Width != 0)
                        throw new InvalidOperationException(label + ": unexpected BadgeRect when bgCount=0");
                    int lastRight = (!string.IsNullOrEmpty(m.HintText)) ? m.HintRect.Right : m.WaveRect.Right;
                    if (lastRight > m.Width - m.PadR)
                        throw new InvalidOperationException(label + ": HintRect violates right padding");
                }
            } else {
                if (m.IconRect.Width > 0) {
                    if (m.IconRect.Left < m.PadL)
                        throw new InvalidOperationException(label + ": IconRect violates left padding");
                    if (m.MainTextRect.Width > 0 && m.IconRect.Right >= m.MainTextRect.Left)
                        throw new InvalidOperationException(label + ": IconRect overlaps MainTextRect");
                }
                if (m.MainTextRect.Width < m.MainMeasuredW + m.HintSegMeasuredW)
                    throw new InvalidOperationException(label + ": MainTextRect smaller than measured text");
                if (m.MainTextRect.Right > m.Width - m.PadR)
                    throw new InvalidOperationException(label + ": MainTextRect violates right padding");
            }

            Rectangle full = new Rectangle(0, 0, bmp.Width, bmp.Height);
            BitmapData bd = bmp.LockBits(full, ImageLockMode.ReadOnly, PixelFormat.Format32bppPArgb);
            try {
                int len = bd.Stride * bmp.Height;
                byte[] px = new byte[len];
                Marshal.Copy(bd.Scan0, px, 0, len);
                if (px[3] != 0)
                    throw new InvalidOperationException(label + ": top-left corner pixel not transparent (alpha=" + px[3] + ")");
                int midY = bmp.Height / 2;
                int bgSampleX = Math.Max(3, Math.Min(bmp.Width - 4, m.PadL / 2));
                int bgIdx = midY * bd.Stride + bgSampleX * 4;
                if (px[bgIdx + 3] < 180)
                    throw new InvalidOperationException(label + ": capsule interior unexpectedly transparent");
                byte bgB = px[bgIdx], bgG = px[bgIdx + 1], bgR = px[bgIdx + 2];

                if (m.IsWaveRecording) {
                    if (!RegionHasDistinctForeground(px, bd.Stride, bmp.Width, bmp.Height, m.DurRect, bgB, bgG, bgR))
                        throw new InvalidOperationException(label + ": DurRect has no rendered foreground pixels");
                    if (!RegionHasDistinctForeground(px, bd.Stride, bmp.Width, bmp.Height, m.WaveRect, bgB, bgG, bgR))
                        throw new InvalidOperationException(label + ": WaveRect has no rendered waveform pixels");
                    if (!string.IsNullOrEmpty(m.HintText) && !RegionHasDistinctForeground(px, bd.Stride, bmp.Width, bmp.Height, m.HintRect, bgB, bgG, bgR))
                        throw new InvalidOperationException(label + ": HintRect has no rendered hint pixels");
                    if (bgCount > 0 && !RegionHasDistinctForeground(px, bd.Stride, bmp.Width, bmp.Height, m.BadgeRect, bgB, bgG, bgR))
                        throw new InvalidOperationException(label + ": BadgeRect has no rendered badge pixels");
                } else {
                    if (m.IconRect.Width > 0 && !RegionHasDistinctForeground(px, bd.Stride, bmp.Width, bmp.Height, m.IconRect, bgB, bgG, bgR))
                        throw new InvalidOperationException(label + ": IconRect has no rendered icon pixels");
                    if (m.MainTextRect.Width > 0 && !RegionHasDistinctForeground(px, bd.Stride, bmp.Width, bmp.Height, m.MainTextRect, bgB, bgG, bgR))
                        throw new InvalidOperationException(label + ": MainTextRect has no rendered text pixels");
                }
            } finally {
                bmp.UnlockBits(bd);
            }
        }
    }

    public static int RunHudLayoutSelfCheck() {
        int cases = 0;
        int[][] dpiSpecs = new int[][] {
            new int[] { 96, 1920, 1080 },
            new int[] { 144, 2560, 1440 },
            new int[] { 192, 3840, 2160 }
        };
        string[] enRecTexts = new string[] {
            "\u25CF Listening\u2026 (press Right Ctrl to end)",
            "\u25CF Listening\u2026 (release Right Ctrl to end)",
            "\u25CF Listening\u2026 (press Right Option to end)",
            "\u25CF Listening\u2026 (release ScrollLock to end)"
        };
        string[] zhRecTexts = new string[] {
            "\u25CF \u5F55\u97F3\u4E2D\u2026 (\u6309\u53F3 Ctrl \u7ED3\u675F)",
            "\u25CF \u5F55\u97F3\u4E2D\u2026 (\u677E\u5F00\u53F3 Ctrl \u7ED3\u675F)",
            "\u25CF \u5F55\u97F3\u4E2D\u2026 (\u6309\u53F3 Option \u7ED3\u675F)",
            "\u25CF \u5F55\u97F3\u4E2D\u2026 (\u677E\u5F00 ScrollLock \u7ED3\u675F)"
        };
        string[] mainStates = new string[] { "recognizing", "processing", "refining", "done", "copied" };
        string[] toastStates = new string[] {
            "cancelled", "empty", "no_mic", "mic_unavailable", "mic_no_stream",
            "mic_permission", "mic_disconnected", "audio_recovered",
            "partial_asr", "partial_asr_unsaved", "partial_asr_copied", "partial_asr_copied_unsaved",
            "asr_empty_retry", "asr_empty", "paste_failed_retry", "paste_failed", "error"
        };

        foreach (int[] ds in dpiSpecs) {
            int dpi = ds[0], ww = ds[1], wh = ds[2];
            foreach (bool zh in new bool[] { false, true }) {
                string langTag = zh ? "zh" : "en";
                string[] recTexts = zh ? zhRecTexts : enRecTexts;
                for (int rIdx = 0; rIdx < recTexts.Length; rIdx++) {
                    long dur = (rIdx == 0) ? 0L : ((rIdx == 1) ? 59L : ((rIdx == 2) ? 65L : 605L));
                    foreach (int bg in new int[] { 0, 1, 2 }) {
                        VerifyHudLayoutCase("rec_" + langTag + "_dpi" + dpi + "_k" + rIdx + "_bg" + bg, "recording", recTexts[rIdx], dpi, ww, wh, zh, false, dur, 0, 0, bg);
                        cases++;
                    }
                }
                // Countdown warning (12s & 5s remaining, with and without bg_count badge)
                VerifyHudLayoutCase("rec_warn12_" + langTag + "_dpi" + dpi, "recording", recTexts[0], dpi, ww, wh, zh, false, 48L, 60, 15, 0);
                cases++;
                VerifyHudLayoutCase("rec_warn5_bg2_" + langTag + "_dpi" + dpi, "recording", recTexts[2], dpi, ww, wh, zh, false, 55L, 60, 15, 2);
                cases++;
                // Hold-to-cancel preview in recording (normal, bg_count=2, countdown) and cancellable main states
                VerifyHudLayoutCase("rec_hold_" + langTag + "_dpi" + dpi, "recording", recTexts[0], dpi, ww, wh, zh, false, 12L, 0, 0, 0, true);
                cases++;
                VerifyHudLayoutCase("rec_hold_bg2_" + langTag + "_dpi" + dpi, "recording", recTexts[2], dpi, ww, wh, zh, false, 65L, 600, 15, 2, true);
                cases++;
                VerifyHudLayoutCase("rec_hold_warn_" + langTag + "_dpi" + dpi, "recording", recTexts[0], dpi, ww, wh, zh, false, 55L, 60, 15, 1, true);
                cases++;
                VerifyHudLayoutCase("main_recognizing_hold_" + langTag + "_dpi" + dpi, "recognizing", "", dpi, ww, wh, zh, false, 0L, 0, 0, 0, true);
                cases++;
                VerifyHudLayoutCase("main_refining_hold_" + langTag + "_dpi" + dpi, "refining", "", dpi, ww, wh, zh, false, 0L, 0, 0, 0, true);
                cases++;

                // All non-wave main states + cancelling custom main copy
                foreach (string st in mainStates) {
                    VerifyHudLayoutCase("main_" + st + "_" + langTag + "_dpi" + dpi, st, "", dpi, ww, wh, zh, false, 0L, 0, 0, 0);
                    cases++;
                }
                string cancellingTxt = zh ? "\u25CB \u6B63\u5728\u53D6\u6D88\u2026" : "\u25CB Cancelling\u2026";
                VerifyHudLayoutCase("main_cancelling_" + langTag + "_dpi" + dpi, "processing", cancellingTxt, dpi, ww, wh, zh, false, 0L, 0, 0, 0);
                cases++;

                // All toast states + copied routed as toast
                foreach (string tst in toastStates) {
                    VerifyHudLayoutCase("toast_" + tst + "_" + langTag + "_dpi" + dpi, tst, "", dpi, ww, wh, zh, true, 0L, 0, 0, 0);
                    cases++;
                }
                VerifyHudLayoutCase("toast_copied_" + langTag + "_dpi" + dpi, "copied", "", dpi, ww, wh, zh, true, 0L, 0, 0, 0);
                cases++;
            }
        }
        return cases;
    }

    public static void RunToastSingleSlotSelfCheck() {
        Rectangle mon1 = new Rectangle(0, 0, 1920, 1080);
        Rectangle mon2 = new Rectangle(1920, 0, 2560, 1440);
        _headlessHudMode = true;
        _headlessDefaultWa = mon1;
        _headlessDefaultDpi = 96;
        try {
            // 1) Start recording on monitor 1 via production UpdateHud
            UpdateHud("recording", "", 0, "bottom", 60, 15, "", null, null, 2);
            if (_hud == null || !_hud.IsShown || !_hud.IsWaveRecording || _hud.CurrentBgCount != 2)
                throw new InvalidOperationException("single-slot check: UpdateHud recording bgCount=2 failed");
            if (_hud.AnchoredWorkArea != mon1 || _hud.AnchoredDpi != 96)
                throw new InvalidOperationException("single-slot check: initial monitor1 anchor mismatch");

            // 2) Simulate foreground moving to monitor 2 WITH NO active toast:
            //    in-place recording bg_count 2 -> 1 refresh MUST keep main pill anchored on monitor 1
            _headlessDefaultWa = mon2;
            _headlessDefaultDpi = 144;
            UpdateHud("recording", "", 0, "bottom", 60, 15, "", null, false, 1);
            if (!_hud.IsShown || !_hud.IsWaveRecording || _hud.CurrentBgCount != 1 || (_toastHud != null && _toastHud.IsShown))
                throw new InvalidOperationException("single-slot check: bgCount 2->1 refresh without toast failed");
            if (_hud.AnchoredWorkArea != mon1 || _hud.AnchoredDpi != 96)
                throw new InvalidOperationException("single-slot check: recording bgCount refresh jumped monitor without toast");

            // 3) Sequential toasts while foreground is on monitor 2 must reuse single _toastHud slot in-place and anchor to mon1
            string[] seqToasts = new string[] { "mic_no_stream", "copied", "cancelled", "audio_recovered" };
            int[] expDurs = new int[] { 2600, 5000, 1600, 2400 };
            HudPillForm firstToastRef = null;
            for (int i = 0; i < seqToasts.Length; i++) {
                UpdateHud(seqToasts[i], "", 0, "bottom", 0, 0, "toast", false, null, 0);
                if (_toastHud == null || !_toastHud.IsShown)
                    throw new InvalidOperationException("single-slot check: toast not shown at " + seqToasts[i]);
                if (firstToastRef == null) firstToastRef = _toastHud;
                if (!object.ReferenceEquals(_toastHud, firstToastRef))
                    throw new InvalidOperationException("single-slot check: toast slot instance changed");
                if (_toastHud.LastAutoHideMs != expDurs[i] || _toastHud.ShowUpdateCount != i + 1 || string.IsNullOrEmpty(_toastHud.CurrentText))
                    throw new InvalidOperationException("single-slot check: toast in-place replace mismatch at " + seqToasts[i]);
                if (_toastHud.AnchoredWorkArea != mon1 || _toastHud.AnchoredDpi != 96 || !_hud.IsShown)
                    throw new InvalidOperationException("single-slot check: toast anchor/main preservation failed at " + seqToasts[i]);
            }

            // 4) Refresh recording bg_count 1 -> 0 with clearToast=false while toast is active:
            //    must NOT jump to monitor 2 and must NOT clear active toast
            UpdateHud("recording", "", 0, "bottom", 60, 15, "", null, false, 0);
            if (!_hud.IsShown || !_hud.IsWaveRecording || _hud.CurrentBgCount != 0 || !_toastHud.IsShown)
                throw new InvalidOperationException("single-slot check: bgCount 1->0 clear failed");
            if (_hud.AnchoredWorkArea != mon1 || _hud.AnchoredDpi != 96)
                throw new InvalidOperationException("single-slot check: recording bgCount clear jumped monitor");

            // 5) clearMain=true hides main slot while toast slot remains intact in-place; hide_toast clears toast
            UpdateHud("cancelled", "", 0, "bottom", 0, 0, "toast", true, null, 0);
            if (_hud.IsShown || _hud.CurrentBgCount != 0 || !_toastHud.IsShown || !object.ReferenceEquals(_toastHud, firstToastRef))
                throw new InvalidOperationException("single-slot check: clearMain isolation failed");
            UpdateHud("hide_toast", "", 0);
            if (_toastHud.IsShown)
                throw new InvalidOperationException("single-slot check: hide_toast failed");

            // 5) Fresh recording start (not already recording) DOES re-anchor to monitor 2
            UpdateHud("recording", "", 0, "bottom", 60, 15, "", null, true, 0);
            if (!_hud.IsShown || _hud.AnchoredWorkArea != mon2 || _hud.AnchoredDpi != 144)
                throw new InvalidOperationException("single-slot check: fresh recording failed to re-anchor to monitor2");

            // 6) Hold-cancel preview works ONLY on cancellable main states, preserves anchor/toast/bgCount,
            //    survives single-pass bg_count refresh without flashing, and restores cleanly
            UpdateHud("recording", "\u25CF \u5F55\u97F3\u4E2D\u2026 (\u6309\u53F3 Ctrl \u7ED3\u675F)", 0, "bottom", 60, 15, "", null, false, 2);
            UpdateHud("audio_recovered", "", 0, "bottom", 0, 0, "toast", false, null, 0);
            _hud.SetHoldCancelPreview(true, true);
            if (!_hud.HoldCancelPreviewActive || _hud.LastRenderedHint != FormatHoldCancelHint(true) || _hud.CurrentBgCount != 2 || _hud.AnchoredWorkArea != mon2 || !_toastHud.IsShown)
                throw new InvalidOperationException("single-slot check: recording hold preview failed");
            WinHotkeyEngine prevEng = _hotkeyEngine;
            try {
                WinHotkeyEngine simEng = new WinHotkeyEngine(0xA3u, false, 500, 800, 250, 180, false);
                HotkeyStepResult simD = simEng.ProcessKeyEvent(0xA3u, 0x1D, true, true, false, 1000UL, vk => vk == 0xA3u);
                simEng.OnHoldHintTimeout(simD.TimerGen, 1250UL, vk => vk == 0xA3u);
                _hotkeyEngine = simEng;
                int beforeUpd = _hud.ShowUpdateCount;
                UpdateHud("recording", "\u25CF \u5F55\u97F3\u4E2D\u2026 (\u6309\u53F3 Ctrl \u7ED3\u675F)", 0, "bottom", 60, 15, "", null, false, 1);
                if (_hud.ShowUpdateCount != beforeUpd + 1 || !_hud.HoldCancelPreviewActive || _hud.LastRenderedHint != FormatHoldCancelHint(true) || _hud.CurrentBgCount != 1 || _hud.AnchoredWorkArea != mon2 || !_toastHud.IsShown)
                    throw new InvalidOperationException("single-slot check: single-pass bgCount refresh during hold preview failed");
            } finally {
                _hotkeyEngine = prevEng;
            }
            _hud.SetHoldCancelPreview(false, true);
            if (_hud.HoldCancelPreviewActive || _hud.LastRenderedHint == FormatHoldCancelHint(true) || _hud.CurrentBgCount != 1 || !_toastHud.IsShown)
                throw new InvalidOperationException("single-slot check: recording hold preview restore failed");

            UpdateHud("refining", "\u25C8 Refining\u2026", 0, "bottom", 0, 0, "main", null, false, 0);
            _hud.SetHoldCancelPreview(true, true);
            if (!_hud.HoldCancelPreviewActive || _hud.LastRenderedHint != ("(" + FormatHoldCancelHint(false) + ")"))
                throw new InvalidOperationException("single-slot check: refining hold preview en failed");
            _hud.SetHoldCancelPreview(false, true);
            if (_hud.HoldCancelPreviewActive || _hud.LastRenderedHint != "")
                throw new InvalidOperationException("single-slot check: refining hold preview restore failed");

            // Non-cancellable main states (done / copied / hidden) MUST ignore hold preview
            UpdateHud("done", "", 900, "bottom", 0, 0, "main", null, false, 0);
            _hud.SetHoldCancelPreview(true, true);
            if (_hud.HoldCancelPreviewActive)
                throw new InvalidOperationException("single-slot check: done must ignore hold preview");
            UpdateHud("copied", "", 5000, "bottom", 0, 0, "main", null, false, 0);
            _hud.SetHoldCancelPreview(true, true);
            if (_hud.HoldCancelPreviewActive)
                throw new InvalidOperationException("single-slot check: copied must ignore hold preview");

            UpdateHud("hide_all", "", 0);
            _hud.SetHoldCancelPreview(true, true);
            if (_hud.IsShown || _toastHud.IsShown || _hud.HoldCancelPreviewActive)
                throw new InvalidOperationException("single-slot check: hide_all failed");
        } finally {
            _headlessHudMode = false;
            _headlessDefaultWa = Rectangle.Empty;
            _headlessDefaultDpi = 96;
            if (_hud != null) { try { _hud.Dispose(); } catch { } _hud = null; }
            if (_toastHud != null) { try { _toastHud.Dispose(); } catch { } _toastHud = null; }
        }
    }

    public static int RunHotkeyFsmSelfCheck() {
        int checks = 0;
        const uint VK_RCTRL = 0xA3u;
        const uint VK_CTRL = 0x11u;
        const uint VK_C = 0x43u;
        const uint VK_A = 0x41u;
        Func<uint, bool> noneDown = vk => false;
        Func<uint, bool> onlyTargetDown = vk => vk == VK_RCTRL;

        // 1) Short tap (<=500ms) -> toggle, boundary 500ms -> toggle, companion VK_CONTROL ignored
        WinHotkeyEngine eng = new WinHotkeyEngine(VK_RCTRL, false, 500, 800, 250, 180, false);
        HotkeyStepResult s1d = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 1000UL, onlyTargetDown);
        if (!s1d.ArmTimers || s1d.HintDelayMs != 250 || s1d.CancelDelayMs != 800 || (s1d.Events != null && s1d.Events.Count > 0))
            throw new InvalidOperationException("fsm check 1a: tap down arm mismatch");
        HotkeyStepResult s1c = eng.ProcessKeyEvent(VK_CTRL, 0x1D, false, true, false, 1010UL, onlyTargetDown);
        if (eng.ChordTainted || s1c.CancelTimers)
            throw new InvalidOperationException("fsm check 1b: companion VK_CONTROL must not taint");
        HotkeyStepResult s1u = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 1120UL, noneDown);
        if (!s1u.CancelTimers || s1u.ClearHoldPreview || s1u.Events == null || s1u.Events.Count != 1 || s1u.Events[0].Kind != "toggle")
            throw new InvalidOperationException("fsm check 1c: short tap 120ms toggle mismatch");
        checks++;

        // Boundary 500ms exact -> toggle
        eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 2000UL, onlyTargetDown);
        HotkeyStepResult s1b = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 2500UL, noneDown);
        if (s1b.Events == null || s1b.Events.Count != 1 || s1b.Events[0].Kind != "toggle")
            throw new InvalidOperationException("fsm check 1d: boundary 500ms toggle mismatch");
        checks++;

        // 2) Hesitation deadzone (501ms..799ms): 250ms preview activates, release clears preview and emits ZERO events
        HotkeyStepResult s2d = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 3000UL, onlyTargetDown);
        HotkeyStepResult s2h = eng.OnHoldHintTimeout(s2d.TimerGen, 3250UL, onlyTargetDown);
        if (!s2h.ShowHoldPreview || !eng.HoldHintShown)
            throw new InvalidOperationException("fsm check 2a: 250ms hold preview failed");
        HotkeyStepResult s2u = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 3600UL, noneDown);
        if (!s2u.CancelTimers || !s2u.ClearHoldPreview || (s2u.Events != null && s2u.Events.Count > 0))
            throw new InvalidOperationException("fsm check 2b: 600ms deadzone must be silent and clear preview");
        // Boundaries 501ms and 799ms
        eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 4000UL, onlyTargetDown);
        HotkeyStepResult s2b1 = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 4501UL, noneDown);
        if (s2b1.Events != null && s2b1.Events.Count > 0)
            throw new InvalidOperationException("fsm check 2c: 501ms deadzone must be silent");
        eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 5000UL, onlyTargetDown);
        HotkeyStepResult s2b2 = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 5799UL, noneDown);
        if (s2b2.Events != null && s2b2.Events.Count > 0)
            throw new InvalidOperationException("fsm check 2d: 799ms deadzone must be silent");
        checks++;

        // 3) Real-time 800ms hold_cancel BEFORE keyup + keyup swallowed even after >8000ms + stale gen ignored
        HotkeyStepResult s3d = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 6000UL, onlyTargetDown);
        ulong g3 = s3d.TimerGen;
        HotkeyStepResult s3h = eng.OnHoldHintTimeout(g3, 6250UL, onlyTargetDown);
        if (!s3h.ShowHoldPreview) throw new InvalidOperationException("fsm check 3a: hint at 250ms");
        HotkeyStepResult s3c = eng.OnHoldCancelTimeout(g3, 6800UL, onlyTargetDown);
        if (!eng.HoldFired || !s3c.ArmPreviewSafetyClear || s3c.Events == null || s3c.Events.Count != 1 || s3c.Events[0].Kind != "hold_cancel" || s3c.Events[0].HoldMs != 800)
            throw new InvalidOperationException("fsm check 3b: real-time 800ms hold_cancel mismatch");
        // Stale timer callback must be ignored
        HotkeyStepResult s3stale = eng.OnHoldCancelTimeout(g3, 6900UL, onlyTargetDown);
        if (s3stale.Events != null && s3stale.Events.Count > 0)
            throw new InvalidOperationException("fsm check 3c: stale timer fired");
        // Keyup after 12000ms (>8000ms old cap) MUST be swallowed cleanly
        HotkeyStepResult s3u = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 18000UL, noneDown);
        if ((s3u.Events != null && s3u.Events.Count > 0) || eng.TargetDown || eng.HoldFired)
            throw new InvalidOperationException("fsm check 3d: post-hold_cancel keyup after 12s not swallowed");
        checks++;

        // 4) Timer jitter fallback: keyup at 805ms before timer fires still emits hold_cancel once
        HotkeyStepResult s4d = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 20000UL, onlyTargetDown);
        eng.OnHoldHintTimeout(s4d.TimerGen, 20250UL, onlyTargetDown);
        HotkeyStepResult s4u = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 20805UL, noneDown);
        if (s4u.Events == null || s4u.Events.Count != 1 || s4u.Events[0].Kind != "hold_cancel" || s4u.Events[0].HoldMs != 805 || !s4u.ArmPreviewSafetyClear)
            throw new InvalidOperationException("fsm check 4: jitter fallback hold_cancel mismatch");
        checks++;

        // 5) Chord taint (RControl+C) before 250ms AND during 250-800ms preview restores preview immediately
        HotkeyStepResult s5d = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 22000UL, onlyTargetDown);
        HotkeyStepResult s5c = eng.ProcessKeyEvent(VK_C, 0x2E, false, true, false, 22100UL, vk => vk == VK_RCTRL || vk == VK_C);
        if (!eng.ChordTainted || !s5c.CancelTimers)
            throw new InvalidOperationException("fsm check 5a: early chord taint failed");
        if (eng.OnHoldHintTimeout(s5d.TimerGen, 22250UL, vk => vk == VK_RCTRL || vk == VK_C).ShowHoldPreview)
            throw new InvalidOperationException("fsm check 5b: tainted hint must not show");
        eng.ProcessKeyEvent(VK_C, 0x2E, false, false, true, 22150UL, onlyTargetDown);
        HotkeyStepResult s5u = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 22900UL, noneDown);
        if (s5u.Events != null && s5u.Events.Count > 0)
            throw new InvalidOperationException("fsm check 5c: tainted release must be silent");

        HotkeyStepResult s5d2 = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 24000UL, onlyTargetDown);
        HotkeyStepResult s5h2 = eng.OnHoldHintTimeout(s5d2.TimerGen, 24250UL, onlyTargetDown);
        if (!s5h2.ShowHoldPreview) throw new InvalidOperationException("fsm check 5d: preview did not activate");
        HotkeyStepResult s5c2 = eng.ProcessKeyEvent(VK_C, 0x2E, false, true, false, 24400UL, vk => vk == VK_RCTRL || vk == VK_C);
        if (!s5c2.ClearHoldPreview || !s5c2.CancelTimers || eng.HoldHintShown)
            throw new InvalidOperationException("fsm check 5e: mid-preview chord must immediately clear preview");
        eng.ProcessKeyEvent(VK_C, 0x2E, false, false, true, 24450UL, onlyTargetDown);
        HotkeyStepResult s5u2 = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 24900UL, noneDown);
        if (s5u2.Events != null && s5u2.Events.Count > 0)
            throw new InvalidOperationException("fsm check 5f: mid-preview tainted release must be silent");
        checks++;

        // 6) Stale _otherDown self-healing (swallowed WM_KEYUP from UAC/lock screen) + stuck _targetDown recovery
        eng.ProcessKeyEvent(VK_A, 0x1E, false, true, false, 26000UL, vk => vk == VK_A);
        if (eng.OtherDownCount != 1) throw new InvalidOperationException("fsm check 6a: otherDown not recorded");
        // No WM_KEYUP for VK_A; physical state reports VK_A is UP -> pruned on RControl down!
        HotkeyStepResult s6d = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 26500UL, onlyTargetDown);
        if (eng.OtherDownCount != 0 || eng.ChordTainted || !s6d.ArmTimers)
            throw new InvalidOperationException("fsm check 6b: stale _otherDown self-heal failed");
        HotkeyStepResult s6u = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 26620UL, noneDown);
        if (s6u.Events == null || s6u.Events.Count != 1 || s6u.Events[0].Kind != "toggle")
            throw new InvalidOperationException("fsm check 6c: toggle after stale self-heal failed");
        // Stuck _targetDown when WM_KEYUP of target itself was lost -> timer sees physical UP and resets
        HotkeyStepResult s6td = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 27000UL, onlyTargetDown);
        HotkeyStepResult s6th = eng.OnHoldHintTimeout(s6td.TimerGen, 27250UL, noneDown);
        if (s6th.ShowHoldPreview || eng.TargetDown)
            throw new InvalidOperationException("fsm check 6d: stuck _targetDown recovery failed");
        checks++;

        // 7) holdCancelMs = 0 disables hold_cancel and timers while keeping tap_toggle intact; tight threshold keeps HoldHintMs < HoldCancelMs
        WinHotkeyEngine noHold = new WinHotkeyEngine(VK_RCTRL, false, 500, 0, 250, 180, false);
        if (noHold.HoldCancelMs != 0UL || noHold.HoldHintMs != 0UL)
            throw new InvalidOperationException("fsm check 7a: holdCancelMs=0 should disable timers");
        HotkeyStepResult s7d = noHold.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 1000UL, onlyTargetDown);
        if (s7d.ArmTimers) throw new InvalidOperationException("fsm check 7b: disabled hold must not arm timers");
        HotkeyStepResult s7u = noHold.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 2500UL, noneDown);
        if (s7u.Events != null && s7u.Events.Count > 0)
            throw new InvalidOperationException("fsm check 7c: disabled hold must ignore >tapMaxMs release");
        WinHotkeyEngine tightEng = new WinHotkeyEngine(VK_RCTRL, false, 50, 80, 250, 0, false);
        if (tightEng.TapMaxMs != 50UL || tightEng.HoldCancelMs != 80UL || tightEng.HoldHintMs != 40UL || tightEng.HoldHintMs >= tightEng.HoldCancelMs)
            throw new InvalidOperationException("fsm check 7d: tight HoldHintMs must be strictly < HoldCancelMs");
        checks++;

        // 8) PTT mode zero regression: immediate ptt_start, no hold timers, >800ms release -> ptt_stop, chord -> ptt_cancel
        WinHotkeyEngine ptt = new WinHotkeyEngine(VK_RCTRL, true, 500, 800, 250, 180, false);
        HotkeyStepResult p1 = ptt.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 1000UL, onlyTargetDown);
        if (p1.ArmTimers || p1.Events == null || p1.Events.Count != 1 || p1.Events[0].Kind != "ptt_start")
            throw new InvalidOperationException("fsm check 8a: ptt_start mismatch");
        HotkeyStepResult p2 = ptt.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 2500UL, noneDown);
        if (p2.Events == null || p2.Events.Count != 1 || p2.Events[0].Kind != "ptt_stop")
            throw new InvalidOperationException("fsm check 8b: ptt_stop mismatch");
        ptt.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 3000UL, onlyTargetDown);
        HotkeyStepResult p3 = ptt.ProcessKeyEvent(VK_C, 0x2E, false, true, false, 3100UL, vk => vk == VK_RCTRL || vk == VK_C);
        if (p3.Events == null || p3.Events.Count != 1 || p3.Events[0].Kind != "ptt_cancel")
            throw new InvalidOperationException("fsm check 8c: ptt_cancel mismatch");
        ptt.ProcessKeyEvent(VK_C, 0x2E, false, false, true, 3150UL, onlyTargetDown);
        HotkeyStepResult p4 = ptt.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 3200UL, noneDown);
        if (p4.Events != null && p4.Events.Count > 0)
            throw new InvalidOperationException("fsm check 8d: post-ptt_cancel release must be silent");
        checks++;

        return checks;
    }

    public static void RunPowerResumeSelfCheck() {
        const uint VK_RCTRL = 0xA3u;
        const uint VK_A = 0x41u;
        Func<uint, bool> onlyTargetDown = vk => vk == VK_RCTRL;
        Func<uint, bool> noneDown = vk => false;

        // 1) Stuck tap_toggle + hold preview + stale otherDown reset cleanly on power resume
        WinHotkeyEngine eng = new WinHotkeyEngine(VK_RCTRL, false, 500, 800, 250, 180, false);
        HotkeyStepResult d1 = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 1000UL, onlyTargetDown);
        HotkeyStepResult h1 = eng.OnHoldHintTimeout(d1.TimerGen, 1260UL, onlyTargetDown);
        if (!eng.TargetDown || !eng.HoldHintShown || !h1.ShowHoldPreview)
            throw new InvalidOperationException("power_resume check 1a: setup hold preview failed");
        eng.ProcessKeyEvent(VK_A, 0x1E, false, true, false, 1300UL, vk => vk == VK_RCTRL || vk == VK_A);
        if (eng.OtherDownCount != 1)
            throw new InvalidOperationException("power_resume check 1b: setup otherDown failed");

        HotkeyStepResult r1 = eng.ResetForPowerResume();
        if (eng.TargetDown || eng.PttActive || eng.ChordTainted || eng.HoldFired || eng.HoldHintShown || eng.OtherDownCount != 0)
            throw new InvalidOperationException("power_resume check 1c: engine state not fully reset");
        if (!r1.CancelTimers || !r1.ClearHoldPreview)
            throw new InvalidOperationException("power_resume check 1d: reset must cancel timers and clear preview");
        // Stale timer from pre-sleep gen must be ignored
        HotkeyStepResult staleCancel = eng.OnHoldCancelTimeout(d1.TimerGen, 2000UL, onlyTargetDown);
        if (staleCancel.Events != null && staleCancel.Events.Count > 0)
            throw new InvalidOperationException("power_resume check 1e: stale pre-sleep timer fired after reset");
        // Subsequent fresh tap works immediately
        eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 3000UL, onlyTargetDown);
        HotkeyStepResult u1 = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 3120UL, noneDown);
        if (u1.Events == null || u1.Events.Count != 1 || u1.Events[0].Kind != "toggle")
            throw new InvalidOperationException("power_resume check 1f: post-resume toggle failed");

        // 2) Active PTT across sleep emits exactly one ptt_stop on power resume; subsequent key-up is silent
        WinHotkeyEngine ptt = new WinHotkeyEngine(VK_RCTRL, true, 500, 800, 250, 180, false);
        ptt.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 1000UL, onlyTargetDown);
        if (!ptt.PttActive || !ptt.TargetDown)
            throw new InvalidOperationException("power_resume check 2a: ptt setup failed");
        HotkeyStepResult rp = ptt.ResetForPowerResume();
        if (ptt.PttActive || ptt.TargetDown || !rp.CancelTimers || !rp.ClearHoldPreview)
            throw new InvalidOperationException("power_resume check 2b: ptt state not cleared");
        if (rp.Events == null || rp.Events.Count != 1 || rp.Events[0].Kind != "ptt_stop")
            throw new InvalidOperationException("power_resume check 2c: active PTT must emit exactly one ptt_stop on resume");
        HotkeyStepResult rpUp = ptt.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 1400UL, noneDown);
        if (rpUp.Events != null && rpUp.Events.Count > 0)
            throw new InvalidOperationException("power_resume check 2d: post-resume key-up must not re-emit ptt_stop");
        // Idle PTT emits zero events on resume
        HotkeyStepResult rpIdle = ptt.ResetForPowerResume();
        if (rpIdle.Events != null && rpIdle.Events.Count > 0)
            throw new InvalidOperationException("power_resume check 2e: idle PTT must not emit ptt_stop on resume");

        // 3) WM_POWERBROADCAST wParam filtering and debounce window (RESUMEAUTOMATIC + RESUMESUSPEND)
        if (!PowerResumeDebouncer.IsResumeWParam(new IntPtr(PowerResumeDebouncer.PBT_APMRESUMEAUTOMATIC)))
            throw new InvalidOperationException("power_resume check 3a: PBT_APMRESUMEAUTOMATIC not recognized");
        if (!PowerResumeDebouncer.IsResumeWParam(new IntPtr(PowerResumeDebouncer.PBT_APMRESUMESUSPEND)))
            throw new InvalidOperationException("power_resume check 3b: PBT_APMRESUMESUSPEND not recognized");
        if (PowerResumeDebouncer.IsResumeWParam(new IntPtr(0x0004)))
            throw new InvalidOperationException("power_resume check 3c: PBT_APMSUSPEND falsely recognized as resume");

        PowerResumeDebouncer db = new PowerResumeDebouncer(2000UL);
        if (!db.ShouldEmit(10000UL))
            throw new InvalidOperationException("power_resume check 3d: first resume must emit");
        // Press between duplicate wakes must NOT be wiped by debounced second wake
        eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, true, false, 10020UL, onlyTargetDown);
        if (db.ShouldEmit(10040UL))
            throw new InvalidOperationException("power_resume check 3e: duplicate resume within 40ms must be debounced");
        if (!eng.TargetDown)
            throw new InvalidOperationException("power_resume check 3e2: debounced wake must preserve active press");
        HotkeyStepResult afterDbUp = eng.ProcessKeyEvent(VK_RCTRL, 0x1D, true, false, true, 10120UL, noneDown);
        if (afterDbUp.Events == null || afterDbUp.Events.Count != 1 || afterDbUp.Events[0].Kind != "toggle")
            throw new InvalidOperationException("power_resume check 3e3: press across debounced wake must still toggle");
        if (db.ShouldEmit(11999UL))
            throw new InvalidOperationException("power_resume check 3f: resume at 1999ms must be debounced");
        if (!db.ShouldEmit(12000UL))
            throw new InvalidOperationException("power_resume check 3g: resume at 2000ms must emit");
    }

    public static int RunHeadlessSelfCheck(out int hotkeyFsmChecks) {
        if (ResolveKeyName("RControl") != 0xA3u) throw new InvalidOperationException("ResolveKeyName RControl failed");
        if (ResolveKeyName("RAlt") != 0xA5u) throw new InvalidOperationException("ResolveKeyName RAlt failed");
        if (ResolveKeyName("F13") != 0x7Cu) throw new InvalidOperationException("ResolveKeyName F13 failed");
        if (ResolveKeyName("0x7B") != 0x7Bu) throw new InvalidOperationException("ResolveKeyName 0x7B failed");

        INPUT[] cv = BuildPasteChordInputs("ctrl_v");
        if (cv == null || cv.Length != 4 || cv[0].u.ki.wVk != 0xA2 || cv[1].u.ki.wVk != 0x56 || cv[2].u.ki.wVk != 0x56 || cv[3].u.ki.wVk != 0xA2)
            throw new InvalidOperationException("BuildPasteChordInputs ctrl_v vk mismatch");
        if ((cv[0].u.ki.dwFlags & KEYEVENTF_KEYUP) != 0 || (cv[1].u.ki.dwFlags & KEYEVENTF_KEYUP) != 0 || (cv[2].u.ki.dwFlags & KEYEVENTF_KEYUP) == 0 || (cv[3].u.ki.dwFlags & KEYEVENTF_KEYUP) == 0)
            throw new InvalidOperationException("BuildPasteChordInputs ctrl_v flags mismatch");

        INPUT[] csv = BuildPasteChordInputs("ctrl_shift_v");
        if (csv == null || csv.Length != 6 || csv[0].u.ki.wVk != 0xA2 || csv[1].u.ki.wVk != 0xA0 || csv[2].u.ki.wVk != 0x56 || csv[3].u.ki.wVk != 0x56 || csv[4].u.ki.wVk != 0xA0 || csv[5].u.ki.wVk != 0xA2)
            throw new InvalidOperationException("BuildPasteChordInputs ctrl_shift_v mismatch");

        INPUT[] si = BuildPasteChordInputs("shift_insert");
        if (si == null || si.Length != 4 || si[0].u.ki.wVk != 0xA0 || si[1].u.ki.wVk != 0x2D || si[2].u.ki.wVk != 0x2D || si[3].u.ki.wVk != 0xA0)
            throw new InvalidOperationException("BuildPasteChordInputs shift_insert vk mismatch");
        if ((si[1].u.ki.dwFlags & KEYEVENTF_EXTENDEDKEY) == 0 || (si[2].u.ki.dwFlags & KEYEVENTF_EXTENDEDKEY) == 0 || (si[2].u.ki.dwFlags & KEYEVENTF_KEYUP) == 0)
            throw new InvalidOperationException("BuildPasteChordInputs shift_insert extended flags mismatch");

        string segIcon, segMain, segHint; bool segSp;
        SplitHudSegments("\u25CF \u5F55\u97F3\u4E2D\u2026 (\u6309\u53F3 Ctrl \u7ED3\u675F)", out segIcon, out segMain, out segHint, out segSp);
        if (segIcon != "\u25CF" || segMain != "\u5F55\u97F3\u4E2D\u2026" || segHint != "(\u6309\u53F3 Ctrl \u7ED3\u675F)" || !segSp)
            throw new InvalidOperationException("SplitHudSegments mismatch");
        if (FormatRecordingDuration(5) != "5s" || FormatRecordingDuration(65) != "1m 05s")
            throw new InvalidOperationException("FormatRecordingDuration mismatch");
        if (CountdownHintSlotTemplate(9, false) != "(auto-finish in 8s)" || CountdownHintSlotTemplate(12, false) != "(auto-finish in 88s)")
            throw new InvalidOperationException("CountdownHintSlotTemplate mismatch");

        byte[] zeros = new byte[640];
        if (ComputePcm16RmsLevel(zeros, 640) != 0f)
            throw new InvalidOperationException("ComputePcm16RmsLevel zero mismatch");
        byte[] loud = new byte[640];
        for (int i = 0; i < loud.Length; i += 2) {
            short sv = (short)(((i / 2) % 2 == 0) ? 6000 : -6000);
            loud[i] = (byte)(sv & 0xFF);
            loud[i + 1] = (byte)((sv >> 8) & 0xFF);
        }
        if (ComputePcm16RmsLevel(loud, 640) <= 0.5f)
            throw new InvalidOperationException("ComputePcm16RmsLevel loud mismatch");
        byte[] wav = BuildPcm16MonoWav(loud, 640, 16000);
        if (wav == null || wav.Length != 44 + 640 || Encoding.ASCII.GetString(wav, 0, 4) != "RIFF" || Encoding.ASCII.GetString(wav, 8, 4) != "WAVE")
            throw new InvalidOperationException("BuildPcm16MonoWav header mismatch");

        RunClipStateSelfCheck();
        RunStoppedRecSlotSelfCheck();
        RunToastSingleSlotSelfCheck();
        RunPowerResumeSelfCheck();
        hotkeyFsmChecks = RunHotkeyFsmSelfCheck();
        return RunHudLayoutSelfCheck();
    }
    public static int RunHeadlessSelfCheck() {
        int unused;
        return RunHeadlessSelfCheck(out unused);
    }
}
