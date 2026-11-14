param(
    [string]$Mode = "bridge", [string]$Hotkey = "RControl", [string]$TriggerMode = "tap_toggle",
    [int]$TapMaxMs = 1200, [int]$DebounceMs = 180, [int]$Seconds = 10, [int]$Beep = 0
)
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition @"
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Media;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public struct ShimEvent { public string Kind, Process, Title; public uint Vk, Scan; public bool Extended, IsDown, Tainted; public long Hwnd; public ulong TsMs; }

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] internal class MMDeviceEnumeratorComObject { }
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IntPtr ppDevices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
}
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IMMDevice {
    int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
}
[StructLayout(LayoutKind.Sequential)]
internal struct WAVEFORMATEX {
    public ushort wFormatTag, nChannels;
    public uint nSamplesPerSec, nAvgBytesPerSec;
    public ushort nBlockAlign, wBitsPerSample, cbSize;
}
[ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioClient {
    int Initialize(int ShareMode, uint StreamFlags, long hnsBufferDuration, long hnsPeriodicity, ref WAVEFORMATEX pFormat, IntPtr AudioSessionGuid);
    int GetBufferSize(out uint pNumBufferFrames);
    int GetStreamLatency(out long phnsLatency);
    int GetCurrentPadding(out uint pNumPaddingFrames);
    int IsFormatSupported(int ShareMode, IntPtr pFormat, out IntPtr ppClosestMatch);
    int GetMixFormat(out IntPtr ppDeviceFormat);
    int GetDevicePeriod(out long phnsDefaultDevicePeriod, out long phnsMinimumDevicePeriod);
    int Start();
    int Stop();
    int Reset();
    int SetEventHandle(IntPtr eventHandle);
    int GetService(ref Guid riid, [MarshalAs(UnmanagedType.IUnknown)] out object ppv);
}
[ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioCaptureClient {
    int GetBuffer(out IntPtr ppData, out uint pNumFramesToRead, out uint pdwFlags, out ulong pu64DevicePosition, out ulong pu64QPCPosition);
    int ReleaseBuffer(uint NumFramesRead);
    int GetNextPacketSize(out uint pNumFramesInNextPacket);
}

internal sealed class HudPillForm : Form {
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateRoundRectRgn(int x1, int y1, int x2, int y2, int cx, int cy);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr hObject);
    private const TextFormatFlags MeasureFlags = TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix;
    private const TextFormatFlags DrawFlags = TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix | TextFormatFlags.EndEllipsis;
    private Font _font;
    private bool _ownsFont, _cjkFont;
    private readonly System.Windows.Forms.Timer _timer = new System.Windows.Forms.Timer();
    private string _text = "";
    private Color _fg = Color.White, _border = Color.Gray;

    private static bool HasCjk(string s) {
        if (string.IsNullOrEmpty(s)) return false;
        for (int i = 0; i < s.Length; i++) { if (s[i] >= 0x2E80 && s[i] <= 0x9FFF) return true; }
        return false;
    }
    private static Font CreateHudFont(bool preferCjk, out bool owns) {
        string[] fams = preferCjk ? new string[] { "Microsoft YaHei UI", "Segoe UI" } : new string[] { "Segoe UI", "Microsoft YaHei UI" };
        foreach (string n in fams) {
            try { using (FontFamily ff = new FontFamily(n)) { owns = true; return new Font(ff, 10.5f, ff.IsStyleAvailable(FontStyle.Bold) ? FontStyle.Bold : FontStyle.Regular); } } catch { }
        }
        try { owns = true; return new Font(FontFamily.GenericSansSerif, 10.5f, FontStyle.Bold); } catch { owns = false; return SystemFonts.DefaultFont; }
    }

    public HudPillForm() {
        _cjkFont = VoiceWinShim.GetHostUiCulture().StartsWith("zh", StringComparison.OrdinalIgnoreCase);
        _font = CreateHudFont(_cjkFont, out _ownsFont);
        FormBorderStyle = FormBorderStyle.None; StartPosition = FormStartPosition.Manual;
        ShowInTaskbar = false; DoubleBuffered = true; BackColor = Color.FromArgb(24, 26, 32);
        _timer.Tick += (s, e) => { _timer.Stop(); HideNoActivate(); };
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get { CreateParams cp = base.CreateParams; cp.ExStyle |= 0x08000000 | 0x00000080 | 0x00000008; return cp; }
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x0021) { m.Result = new IntPtr(3); return; }
        base.WndProc(ref m);
    }
    public void HideNoActivate() { if (IsHandleCreated) ShowWindow(Handle, 0); }
    public void ShowStatus(string text, Color fg, Color border, int autoHideMs, IntPtr fgHwnd, string position) {
        _timer.Stop(); _text = text ?? ""; _fg = fg; _border = border;
        bool wantCjk = HasCjk(_text);
        if (wantCjk != _cjkFont) { if (_ownsFont && _font != null) _font.Dispose(); _cjkFont = wantCjk; _font = CreateHudFont(_cjkFont, out _ownsFont); }
        Screen scr = (fgHwnd != IntPtr.Zero) ? Screen.FromHandle(fgHwnd) : Screen.PrimaryScreen;
        if (scr == null) scr = Screen.PrimaryScreen;
        Rectangle wa = (scr != null) ? scr.WorkingArea : new Rectangle(0, 0, 1920, 1080);
        Size sz = TextRenderer.MeasureText(_text, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
        int w = Math.Min(Math.Max(156, wa.Width - 24), Math.Max(156, sz.Width + 36)), h = Math.Max(36, sz.Height + 16);
        bool isTop = string.Equals((position ?? "").Trim(), "top", StringComparison.OrdinalIgnoreCase);
        int x = wa.Left + (wa.Width - w) / 2;
        int y = isTop ? (wa.Top + 36) : Math.Max(wa.Top + 8, wa.Bottom - h - 14);
        IntPtr rgn = CreateRoundRectRgn(0, 0, w + 1, h + 1, h, h);
        Region old = Region; Region = Region.FromHrgn(rgn); DeleteObject(rgn);
        if (old != null) old.Dispose();
        IntPtr hdl = Handle;
        SetWindowPos(hdl, new IntPtr(-1), x, y, w, h, 0x0010 | 0x0040);
        Invalidate(); Update();
        if (autoHideMs > 0) { _timer.Interval = autoHideMs; _timer.Start(); }
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        SmoothingMode prevSmooth = e.Graphics.SmoothingMode;
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        float inset = 1.25f;
        float d = Math.Max(4f, Height - inset * 2f - 1f);
        float wArc = Math.Max(d, Width - inset * 2f - 1f);
        using (GraphicsPath path = new GraphicsPath())
        using (Pen p = new Pen(_border, 1.5f)) {
            path.AddArc(inset, inset, d, d, 90f, 180f);
            path.AddArc(inset + wArc - d, inset, d, d, 270f, 180f);
            path.CloseFigure();
            e.Graphics.DrawPath(p, path);
        }
        e.Graphics.SmoothingMode = prevSmooth;
        Rectangle rc = new Rectangle(10, 0, Math.Max(1, Width - 20), Height);
        TextRenderer.DrawText(e.Graphics, _text, _font, rc, _fg, DrawFlags);
    }
    protected override void Dispose(bool disposing) {
        if (disposing) { _timer.Dispose(); if (_ownsFont && _font != null) _font.Dispose(); }
        base.Dispose(disposing);
    }
}

public static class VoiceWinShim {
    private const int WH_KEYBOARD_LL = 13, WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101, WM_SYSKEYDOWN = 0x0104, WM_SYSKEYUP = 0x0105;
    private const uint WM_QUIT = 0x0012, WM_APP_WAKE = 0x8001, WM_TIMER = 0x0113, LLKHF_EXTENDED = 0x01, LLKHF_LOWER_IL_INJECTED = 0x02, LLKHF_INJECTED = 0x10, INPUT_KEYBOARD = 1, KEYEVENTF_KEYUP = 0x0002;
    private const ushort VK_LCONTROL = 0xA2, VK_V = 0x56;
    public delegate IntPtr LowLevelKeyboardProc(int nCode, IntPtr wParam, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)] private struct KBDLLHOOKSTRUCT { public uint vkCode, scanCode, flags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)] private struct POINT { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] private struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam, lParam; public uint time; public POINT pt; }
    [StructLayout(LayoutKind.Sequential)] private struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Explicit)] private struct INPUT_UNION { [FieldOffset(0)] public KEYBDINPUT ki; [FieldOffset(0)] private long _p0; [FieldOffset(8)] private long _p1; [FieldOffset(16)] private long _p2; [FieldOffset(24)] private long _p3; }
    [StructLayout(LayoutKind.Sequential)] private struct INPUT { public uint type; public INPUT_UNION u; }

    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr SetWindowsHookEx(int idHook, LowLevelKeyboardProc lpfn, IntPtr hMod, uint dwThreadId);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool UnhookWindowsHookEx(IntPtr hhk);
    [DllImport("user32.dll")] private static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] private static extern int GetMessage(out MSG lpMsg, IntPtr hWnd, uint min, uint max);
    [DllImport("user32.dll")] private static extern bool PeekMessage(out MSG lpMsg, IntPtr hWnd, uint min, uint max, uint remove);
    [DllImport("user32.dll")] private static extern bool TranslateMessage(ref MSG lpMsg);
    [DllImport("user32.dll")] private static extern IntPtr DispatchMessage(ref MSG lpMsg);
    [DllImport("user32.dll")] private static extern bool PostThreadMessage(uint idThread, uint msg, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] private static extern IntPtr SetTimer(IntPtr hWnd, IntPtr nIDEvent, uint uElapse, IntPtr lpTimerFunc);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int lpdwProcessId);
    [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll")] public static extern ulong GetTickCount64();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetModuleHandle(string lpModuleName);
    [DllImport("winmm.dll")] public static extern uint waveInGetNumDevs();
    [DllImport("winmm.dll", CharSet = CharSet.Unicode)] public static extern int mciSendString(string cmd, StringBuilder ret, int retLen, IntPtr hwnd);

    public static readonly ConcurrentQueue<string> CmdQueue = new ConcurrentQueue<string>();
    public static readonly ConcurrentQueue<ShimEvent> EvtQueue = new ConcurrentQueue<ShimEvent>();
    public static volatile bool StdinClosed = false;
    private static IntPtr _hook = IntPtr.Zero;
    private static LowLevelKeyboardProc _procRef;
    private static HudPillForm _hud;
    private static string _hotkeyLabel = "\u53F3 Ctrl";
    private static readonly Dictionary<string, SoundPlayer> _tones = new Dictionary<string, SoundPlayer>(StringComparer.OrdinalIgnoreCase);
    private static uint _mainTid = 0, _targetVk = 0xA3, _ignoredCompanionVk = 0x11;
    private static ulong _tapMaxMs = 1200, _debounceMs = 180, _downTick = 0, _lastFireTick = 0;
    private static bool _isPtt = false, _keyTest = false, _hasQuitTimer = false, _targetDown = false, _pttActive = false, _chordTainted = false;
    private static readonly HashSet<uint> _otherDown = new HashSet<uint>();
    private static readonly Dictionary<string, uint> _keyMap = new Dictionary<string, uint>(StringComparer.OrdinalIgnoreCase) {
        {"rcontrol",0xA3},{"rctrl",0xA3},{"lcontrol",0xA2},{"lctrl",0xA2},{"ralt",0xA5},{"rmenu",0xA5},
        {"lalt",0xA4},{"lmenu",0xA4},{"rshift",0xA1},{"lshift",0xA0},{"capslock",0x14},{"scrolllock",0x91},
        {"pause",0x13},{"insert",0x2D},{"space",0x20}
    };

    public static string GetHostUiCulture() {
        string n = "";
        try { if (CultureInfo.CurrentUICulture != null) n = CultureInfo.CurrentUICulture.Name ?? ""; } catch { }
        if (string.IsNullOrWhiteSpace(n)) { try { if (CultureInfo.InstalledUICulture != null) n = CultureInfo.InstalledUICulture.Name ?? ""; } catch { } }
        return n.Trim();
    }

    public static uint ResolveKeyName(string raw) {
        string k = (raw ?? "RControl").Trim();
        _hotkeyLabel = (k.Equals("rcontrol", StringComparison.OrdinalIgnoreCase) || k.Equals("rctrl", StringComparison.OrdinalIgnoreCase)) ? "\u53F3 Ctrl" : (" " + k);
        uint vk; int fn;
        if (k.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) return Convert.ToUInt32(k.Substring(2), 16);
        if (_keyMap.TryGetValue(k, out vk)) return vk;
        if ((k.StartsWith("f", StringComparison.OrdinalIgnoreCase)) && int.TryParse(k.Substring(1), out fn) && fn >= 1 && fn <= 24) return (uint)(0x6F + fn);
        throw new ArgumentException("Unsupported hotkey name: " + raw);
    }

    private static byte[] BuildToneWav(int[] freqs, int[] dursMs) {
        const int sr = 16000;
        int total = 0; for (int i = 0; i < dursMs.Length; i++) total += (sr * dursMs[i]) / 1000;
        byte[] buf = new byte[44 + total * 2];
        Encoding.ASCII.GetBytes("RIFF").CopyTo(buf, 0); BitConverter.GetBytes(36 + total * 2).CopyTo(buf, 4);
        Encoding.ASCII.GetBytes("WAVEfmt ").CopyTo(buf, 8); BitConverter.GetBytes(16).CopyTo(buf, 16);
        BitConverter.GetBytes((short)1).CopyTo(buf, 20); BitConverter.GetBytes((short)1).CopyTo(buf, 22);
        BitConverter.GetBytes(sr).CopyTo(buf, 24); BitConverter.GetBytes(sr * 2).CopyTo(buf, 28);
        BitConverter.GetBytes((short)2).CopyTo(buf, 32); BitConverter.GetBytes((short)16).CopyTo(buf, 34);
        Encoding.ASCII.GetBytes("data").CopyTo(buf, 36); BitConverter.GetBytes(total * 2).CopyTo(buf, 40);
        int pos = 44, ramp = (sr * 6) / 1000;
        for (int seg = 0; seg < freqs.Length; seg++) {
            int n = (sr * dursMs[seg]) / 1000, f = freqs[seg];
            for (int i = 0; i < n; i++) {
                short s = 0;
                if (f > 0) {
                    double env = (i < ramp) ? ((double)i / ramp) : ((n - 1 - i < ramp) ? ((double)(n - 1 - i) / ramp) : 1.0);
                    s = (short)(Math.Sin(2.0 * Math.PI * f * i / sr) * 7200.0 * env);
                }
                BitConverter.GetBytes(s).CopyTo(buf, pos); pos += 2;
            }
        }
        return buf;
    }

    public static void PlayTone(string kind, bool enabled) {
        if (!enabled) return;
        try {
            string k = string.IsNullOrEmpty(kind) ? "error" : kind.ToLowerInvariant();
            SoundPlayer sp;
            if (!_tones.TryGetValue(k, out sp)) {
                byte[] wav = (k == "start") ? BuildToneWav(new int[]{660, 880}, new int[]{40, 55})
                           : (k == "stop")  ? BuildToneWav(new int[]{540}, new int[]{55})
                           : (k == "focus") ? BuildToneWav(new int[]{660, 0, 660}, new int[]{45, 30, 45})
                           :                  BuildToneWav(new int[]{330, 0, 260}, new int[]{50, 25, 60});
                sp = new SoundPlayer(new MemoryStream(wav)); sp.Load(); _tones[k] = sp;
            }
            sp.Play();
        } catch { try { SystemSounds.Asterisk.Play(); } catch { } }
    }

    public static void UpdateHud(string state, string customText, int durationMs, string position = "bottom") {
        try {
            string st = (state ?? "").Trim().ToLowerInvariant();
            if (st == "" || st == "idle" || st == "hide") { if (_hud != null) _hud.HideNoActivate(); return; }
            if (_hud == null || _hud.IsDisposed) _hud = new HudPillForm();
            bool zh = GetHostUiCulture().StartsWith("zh", StringComparison.OrdinalIgnoreCase);
            string txt; Color fg, bd; int ms = 0;
            if (st == "recording") { txt = zh ? ("\u25CF \u6B63\u5728\u5F55\u97F3\u2026 (\u518D\u6309" + _hotkeyLabel + " \u5B8C\u6210)") : "\u25CF Listening\u2026"; fg = Color.FromArgb(255, 102, 102); bd = Color.FromArgb(190, 62, 62); }
            else if (st == "recognizing" || st == "processing") { txt = zh ? "\u25D0 \u6B63\u5728\u8BED\u97F3\u8BC6\u522B\u2026" : "\u25D0 Transcribing\u2026"; fg = Color.FromArgb(34, 211, 238); bd = Color.FromArgb(20, 148, 175); }
            else if (st == "refining") { txt = zh ? "\u25C8 \u6B63\u5728\u6574\u7406\u7CBE\u4FEE\u2026" : "\u25C8 Refining\u2026"; fg = Color.FromArgb(251, 191, 36); bd = Color.FromArgb(185, 132, 28); }
            else if (st == "done") { txt = zh ? "\u2713 \u5DF2\u586B\u5165" : "\u2713 Inserted"; fg = Color.FromArgb(74, 222, 128); bd = Color.FromArgb(46, 150, 88); ms = 900; }
            else if (st == "copied") { txt = zh ? "\u2713 \u5DF2\u590D\u5236(\u7A97\u53E3\u5DF2\u5207\u6362)" : "\u2713 Copied (window switched)"; fg = Color.FromArgb(129, 140, 248); bd = Color.FromArgb(99, 102, 241); ms = 900; }
            else if (st == "empty") { txt = zh ? "\u25CB \u672A\u68C0\u6D4B\u5230\u8BED\u97F3" : "\u25CB No speech detected"; fg = Color.FromArgb(203, 213, 225); bd = Color.FromArgb(100, 116, 139); ms = 900; }
            else { txt = zh ? "\u2715 \u8BC6\u522B\u5931\u8D25" : "\u2715 Transcription failed"; fg = Color.FromArgb(248, 113, 113); bd = Color.FromArgb(185, 65, 65); ms = 900; }
            if (!string.IsNullOrEmpty(customText)) txt = customText;
            if (durationMs > 0) ms = durationMs;
            _hud.ShowStatus(txt, fg, bd, ms, GetForegroundWindow(), position);
        } catch { }
    }

    public static void QueryForeground(out long hwnd, out string procName, out string title) {
        IntPtr h = GetForegroundWindow(); hwnd = h.ToInt64(); procName = ""; title = "";
        if (h == IntPtr.Zero) return;
        StringBuilder sb = new StringBuilder(1024); GetWindowText(h, sb, sb.Capacity); title = sb.ToString();
        int pid; GetWindowThreadProcessId(h, out pid);
        if (pid > 0) { try { procName = Process.GetProcessById(pid).ProcessName ?? ""; } catch { procName = ""; } }
    }

    private static void EnqueueTrigger(string kind, uint vk, uint scan, bool ext, bool isDown, bool tainted) {
        long hwnd = 0; string proc = "", title = "";
        if (kind == "toggle" || kind == "ptt_start" || kind == "ptt_stop") QueryForeground(out hwnd, out proc, out title);
        EvtQueue.Enqueue(new ShimEvent { Kind = kind, Vk = vk, Scan = scan, Extended = ext, IsDown = isDown, Tainted = tainted, Hwnd = hwnd, Process = proc, Title = title, TsMs = GetTickCount64() });
        if (_mainTid != 0) PostThreadMessage(_mainTid, WM_APP_WAKE, IntPtr.Zero, IntPtr.Zero);
    }

    private static IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam) {
        if (nCode >= 0) {
            KBDLLHOOKSTRUCT kb = (KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(KBDLLHOOKSTRUCT));
            if ((kb.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)) == 0) {
                int msg = wParam.ToInt32();
                bool isDown = (msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN), isUp = (msg == WM_KEYUP || msg == WM_SYSKEYUP), ext = (kb.flags & LLKHF_EXTENDED) != 0;
                uint vk = kb.vkCode;
                if (_keyTest && (isDown || isUp)) {
                    if (isDown && vk == 0x1B && _mainTid != 0) { PostThreadMessage(_mainTid, WM_QUIT, IntPtr.Zero, IntPtr.Zero); return CallNextHookEx(_hook, nCode, wParam, lParam); }
                    EnqueueTrigger("key_event", vk, kb.scanCode, ext, isDown, _chordTainted);
                }
                if (vk == _targetVk) {
                    ulong now = GetTickCount64();
                    if (isDown && !_targetDown) {
                        _targetDown = true; _downTick = now; _chordTainted = (_otherDown.Count > 0);
                        if (_isPtt && !_chordTainted && (now - _lastFireTick) >= _debounceMs) { _lastFireTick = now; _pttActive = true; EnqueueTrigger("ptt_start", vk, kb.scanCode, ext, true, false); }
                    } else if (isUp && _targetDown) {
                        if (_isPtt) { if (_pttActive && !_chordTainted) { _lastFireTick = now; EnqueueTrigger("ptt_stop", vk, kb.scanCode, ext, false, false); } }
                        else if (!_chordTainted && (now - _downTick) <= _tapMaxMs && (now - _lastFireTick) >= _debounceMs) { _lastFireTick = now; EnqueueTrigger("toggle", vk, kb.scanCode, ext, false, false); }
                        _targetDown = false; _pttActive = false; _chordTainted = false;
                    }
                } else if (vk != _ignoredCompanionVk) {
                    if (isDown) _otherDown.Add(vk); else if (isUp) _otherDown.Remove(vk);
                    if (_targetDown && (isDown || isUp)) { _chordTainted = true; if (_isPtt && _pttActive) { _pttActive = false; EnqueueTrigger("ptt_cancel", vk, kb.scanCode, ext, isDown, true); } }
                }
            }
        }
        return CallNextHookEx(_hook, nCode, wParam, lParam);
    }

    public static void StartHook(uint targetVk, bool isPtt, int tapMaxMs, int debounceMs, bool keyTest, int timerMs) {
        _mainTid = GetCurrentThreadId(); MSG initMsg; PeekMessage(out initMsg, IntPtr.Zero, 0, 0, 0);
        _targetVk = targetVk; _isPtt = isPtt; _keyTest = keyTest; _hasQuitTimer = (timerMs > 0);
        _ignoredCompanionVk = (targetVk == 0xA2 || targetVk == 0xA3) ? 0x11u : ((targetVk == 0xA4 || targetVk == 0xA5) ? 0x12u : ((targetVk == 0xA0 || targetVk == 0xA1) ? 0x10u : 0u));
        _tapMaxMs = (ulong)Math.Max(50, tapMaxMs); _debounceMs = (ulong)Math.Max(0, debounceMs); _procRef = HookCallback;
        using (Process cur = Process.GetCurrentProcess()) using (ProcessModule mod = cur.MainModule) { _hook = SetWindowsHookEx(WH_KEYBOARD_LL, _procRef, GetModuleHandle(mod.ModuleName), 0); }
        if (_hook == IntPtr.Zero) throw new InvalidOperationException("SetWindowsHookEx failed: " + Marshal.GetLastWin32Error());
        if (_hasQuitTimer) SetTimer(IntPtr.Zero, new IntPtr(1), (uint)timerMs, IntPtr.Zero);
    }

    public static void StartStdinReader() {
        Thread t = new Thread(() => {
            try { string line; while ((line = Console.In.ReadLine()) != null) { CmdQueue.Enqueue(line); if (_mainTid != 0) PostThreadMessage(_mainTid, WM_APP_WAKE, IntPtr.Zero, IntPtr.Zero); } } catch { }
            StdinClosed = true; if (_mainTid != 0) PostThreadMessage(_mainTid, WM_APP_WAKE, IntPtr.Zero, IntPtr.Zero);
        });
        t.IsBackground = true; t.Start();
    }

    private static bool IsQuitTimer(ref MSG m) { return _hasQuitTimer && m.hwnd == IntPtr.Zero && m.message == WM_TIMER; }

    public static bool PumpOnce() {
        if (!CmdQueue.IsEmpty || !EvtQueue.IsEmpty) return true;
        if (StdinClosed) return false;
        MSG msg; int r = GetMessage(out msg, IntPtr.Zero, 0, 0);
        if (r <= 0 || IsQuitTimer(ref msg)) return false;
        TranslateMessage(ref msg); DispatchMessage(ref msg);
        MSG pmsg; while (PeekMessage(out pmsg, IntPtr.Zero, 0, 0, 1)) { if (pmsg.message == WM_QUIT || IsQuitTimer(ref pmsg)) return false; TranslateMessage(ref pmsg); DispatchMessage(ref pmsg); }
        return !StdinClosed || !CmdQueue.IsEmpty || !EvtQueue.IsEmpty;
    }

    private static readonly object _recSync = new object();
    private static Thread _recThread = null;
    private static ManualResetEvent _recInitEvt = null, _recStopEvt = null;
    private static MemoryStream _recPcm = null;
    private static volatile string _recErr = "";
    private static volatile bool _recInitOk = false;

    private static byte[] BuildPcm16MonoWav(byte[] pcm, int pcmLen, int sr) {
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

    private static void DrainWasapiPackets(IAudioCaptureClient cap, MemoryStream dst, byte[] zeros, ref byte[] scratch) {
        uint next = 0;
        if (cap.GetNextPacketSize(out next) != 0) return;
        while (next > 0) {
            IntPtr pData; uint frames, flags; ulong dpos, qpos;
            if (cap.GetBuffer(out pData, out frames, out flags, out dpos, out qpos) != 0) break;
            int nBytes = (int)(frames * 2);
            if (nBytes > 0) {
                if ((flags & 2u) != 0 || pData == IntPtr.Zero) {
                    int rem = nBytes;
                    while (rem > 0) { int chunk = Math.Min(rem, zeros.Length); dst.Write(zeros, 0, chunk); rem -= chunk; }
                } else {
                    if (scratch == null || scratch.Length < nBytes) scratch = new byte[Math.Max(nBytes, 8192)];
                    Marshal.Copy(pData, scratch, 0, nBytes);
                    dst.Write(scratch, 0, nBytes);
                }
            }
            cap.ReleaseBuffer(frames);
            if (cap.GetNextPacketSize(out next) != 0) break;
        }
    }

    public static bool StartRecording(bool loopback, out string error) {
        int dummyBytes; string dummyErr;
        StopRecording(out dummyBytes, out dummyErr);
        ManualResetEvent initEvt = new ManualResetEvent(false);
        ManualResetEvent stopEvt = new ManualResetEvent(false);
        MemoryStream pcm = new MemoryStream(64000);
        _recErr = ""; _recInitOk = false;
        Thread t = new Thread(() => {
            IMMDeviceEnumerator en = null; IMMDevice dev = null; IAudioClient client = null; IAudioCaptureClient cap = null;
            bool started = false;
            try {
                en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
                int hr = en.GetDefaultAudioEndpoint(loopback ? 0 : 1, 0, out dev);
                if (hr != 0 || dev == null) { _recErr = "wasapi_endpoint_hr_0x" + hr.ToString("X8"); initEvt.Set(); return; }
                Guid iidCli = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
                object oCli; hr = dev.Activate(ref iidCli, 23, IntPtr.Zero, out oCli);
                if (hr != 0 || oCli == null) { _recErr = "wasapi_activate_hr_0x" + hr.ToString("X8"); initEvt.Set(); return; }
                client = (IAudioClient)oCli;
                WAVEFORMATEX wf = new WAVEFORMATEX { wFormatTag = 1, nChannels = 1, nSamplesPerSec = 16000, wBitsPerSample = 16, nBlockAlign = 2, nAvgBytesPerSec = 32000, cbSize = 0 };
                uint sflags = 0x88000000u | (loopback ? 0x00020000u : 0u);
                hr = client.Initialize(0, sflags, 10000000L, 0L, ref wf, IntPtr.Zero);
                if (hr != 0) { _recErr = "wasapi_init_hr_0x" + hr.ToString("X8"); initEvt.Set(); return; }
                Guid iidCap = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
                object oCap; hr = client.GetService(ref iidCap, out oCap);
                if (hr != 0 || oCap == null) { _recErr = "wasapi_service_hr_0x" + hr.ToString("X8"); initEvt.Set(); return; }
                cap = (IAudioCaptureClient)oCap;
                hr = client.Start();
                if (hr != 0) { _recErr = "wasapi_start_hr_0x" + hr.ToString("X8"); initEvt.Set(); return; }
                started = true; _recInitOk = true; initEvt.Set();
                byte[] zeros = new byte[8192]; byte[] scratch = null;
                while (!stopEvt.WaitOne(10)) { DrainWasapiPackets(cap, pcm, zeros, ref scratch); }
                DrainWasapiPackets(cap, pcm, zeros, ref scratch);
            } catch (Exception ex) {
                _recErr = "wasapi_exc:" + ex.GetType().Name + ":" + ex.Message;
                try { initEvt.Set(); } catch { }
            } finally {
                if (started && client != null) { try { client.Stop(); } catch { } }
                if (cap != null) { try { Marshal.ReleaseComObject(cap); } catch { } }
                if (client != null) { try { Marshal.ReleaseComObject(client); } catch { } }
                if (dev != null) { try { Marshal.ReleaseComObject(dev); } catch { } }
                if (en != null) { try { Marshal.ReleaseComObject(en); } catch { } }
            }
        });
        t.IsBackground = true; t.SetApartmentState(ApartmentState.MTA); t.Start();
        if (!initEvt.WaitOne(3000)) {
            try { stopEvt.Set(); t.Join(300); } catch { }
            try { initEvt.Dispose(); stopEvt.Dispose(); pcm.Dispose(); } catch { }
            error = "wasapi_init_timeout"; return false;
        }
        if (!_recInitOk) {
            try { stopEvt.Set(); t.Join(500); } catch { }
            try { initEvt.Dispose(); stopEvt.Dispose(); pcm.Dispose(); } catch { }
            error = string.IsNullOrEmpty(_recErr) ? "wasapi_init_failed" : _recErr; return false;
        }
        lock (_recSync) { _recThread = t; _recInitEvt = initEvt; _recStopEvt = stopEvt; _recPcm = pcm; }
        error = ""; return true;
    }

    public static byte[] StopRecording(out int pcmBytes, out string error) {
        Thread t; ManualResetEvent initEvt, stopEvt; MemoryStream pcm;
        lock (_recSync) {
            t = _recThread; initEvt = _recInitEvt; stopEvt = _recStopEvt; pcm = _recPcm;
            _recThread = null; _recInitEvt = null; _recStopEvt = null; _recPcm = null;
        }
        pcmBytes = 0; error = "";
        if (t == null) { error = "not_recording"; return new byte[0]; }
        try { if (stopEvt != null) stopEvt.Set(); t.Join(2500); } catch { }
        if (initEvt != null) { try { initEvt.Dispose(); } catch { } }
        if (stopEvt != null) { try { stopEvt.Dispose(); } catch { } }
        byte[] raw = new byte[0];
        if (pcm != null) { try { raw = pcm.ToArray(); pcm.Dispose(); } catch { } }
        int evenLen = raw.Length - (raw.Length % 2);
        pcmBytes = evenLen;
        if (evenLen < 2) {
            error = string.IsNullOrEmpty(_recErr) ? "wasapi_zero_packets" : _recErr;
            return new byte[0];
        }
        return BuildPcm16MonoWav(raw, evenLen, 16000);
    }

    public static void StopHook() {
        int dummyBytes; string dummyErr; StopRecording(out dummyBytes, out dummyErr);
        if (_hook != IntPtr.Zero) { UnhookWindowsHookEx(_hook); _hook = IntPtr.Zero; }
        if (_hud != null) { try { _hud.Dispose(); } catch { } _hud = null; }
    }

    public static bool SendCtrlV() {
        int[] mods = new int[] { 0x12, 0xA4, 0xA5, 0x10, 0xA0, 0xA1, 0x5B, 0x5C, 0xA3 };
        for (int i = 0; i < 15; i++) {
            bool anyDown = false; foreach (int vk in mods) { if ((GetAsyncKeyState(vk) & 0x8000) != 0) { anyDown = true; break; } }
            if (!anyDown) break; Thread.Sleep(10);
        }
        INPUT[] ins = new INPUT[4];
        ins[0].type = INPUT_KEYBOARD; ins[0].u.ki.wVk = VK_LCONTROL;
        ins[1].type = INPUT_KEYBOARD; ins[1].u.ki.wVk = VK_V;
        ins[2].type = INPUT_KEYBOARD; ins[2].u.ki.wVk = VK_V; ins[2].u.ki.dwFlags = KEYEVENTF_KEYUP;
        ins[3].type = INPUT_KEYBOARD; ins[3].u.ki.wVk = VK_LCONTROL; ins[3].u.ki.dwFlags = KEYEVENTF_KEYUP;
        return SendInput(4, ins, Marshal.SizeOf(typeof(INPUT))) == 4;
    }
}
"@

function Write-JsonLine($obj) { try { [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 5)); [Console]::Out.Flush() } catch { } }
function Invoke-Tone([string]$kind) { [VoiceWinShim]::PlayTone($kind, [bool]$Beep) }

if ($Mode -eq "probe") {
    $hwnd = 0L; $proc = ""; $title = ""; [VoiceWinShim]::QueryForeground([ref]$hwnd, [ref]$proc, [ref]$title)
    Write-JsonLine @{ ok = $true; ps_version = $PSVersionTable.PSVersion.ToString(); wave_in_devs = [VoiceWinShim]::waveInGetNumDevs(); hwnd = $hwnd; process = $proc; title = $title; ui_culture = [VoiceWinShim]::GetHostUiCulture() }
    exit 0
}

$targetVk = [VoiceWinShim]::ResolveKeyName($Hotkey)
$isPtt = ($TriggerMode -eq "ptt" -or $TriggerMode -eq "push_to_talk")
$AllowedTerminals = @("windowsterminal", "openconsole", "mintty", "wezterm", "wezterm-gui", "alacritty", "ghostty", "conhost", "putty", "tabby", "hyper", "kitty", "rio")
try {
    if ($Mode -eq "key-test") {
        [VoiceWinShim]::StartHook($targetVk, $isPtt, $TapMaxMs, $DebounceMs, $true, ($Seconds * 1000))
        Write-JsonLine @{ event = "key_test_ready"; hotkey = $Hotkey; target_vk = ("0x{0:X2}" -f $targetVk); mode = $TriggerMode; seconds = $Seconds }
        while ([VoiceWinShim]::PumpOnce()) {
            $ev = New-Object ShimEvent
            while ([VoiceWinShim]::EvtQueue.TryDequeue([ref]$ev)) {
                $kname = try { ([System.Windows.Forms.Keys][int]$ev.Vk).ToString() } catch { "VK_$($ev.Vk)" }
                Write-JsonLine @{ event = $ev.Kind; vk = ("0x{0:X2}" -f $ev.Vk); key_name = $kname; scan = $ev.Scan; extended = $ev.Extended; is_down = $ev.IsDown; tainted = $ev.Tainted }
            }
        }
        exit 0
    }
    [VoiceWinShim]::StartHook($targetVk, $isPtt, $TapMaxMs, $DebounceMs, $false, 0)
    [VoiceWinShim]::StartStdinReader()
    Write-JsonLine @{ event = "ready"; target_vk = ("0x{0:X2}" -f $targetVk); mode = $TriggerMode; wave_in_devs = [VoiceWinShim]::waveInGetNumDevs(); ui_culture = [VoiceWinShim]::GetHostUiCulture() }
    :bridgeLoop while ([VoiceWinShim]::PumpOnce()) {
        try {
            $ev = New-Object ShimEvent
            while ([VoiceWinShim]::EvtQueue.TryDequeue([ref]$ev)) { Write-JsonLine @{ event = $ev.Kind; hwnd = $ev.Hwnd; process = $ev.Process; title = $ev.Title; ts_ms = $ev.TsMs } }
        } catch { }
        $rawCmd = $null
        while ([VoiceWinShim]::CmdQueue.TryDequeue([ref]$rawCmd)) {
            if ([string]::IsNullOrWhiteSpace($rawCmd)) { continue }
            $seq = 0; $op = "unknown"
            try {
                $cmdObj = $rawCmd | ConvertFrom-Json; $op = [string]$cmdObj.cmd; $seq = if ($null -ne $cmdObj.seq_id) { [int]$cmdObj.seq_id } else { 0 }
                if ($op -eq "ping") { Write-JsonLine @{ type = "reply"; reply = "ping"; seq_id = $seq; ok = $true; wave_in_devs = [VoiceWinShim]::waveInGetNumDevs() } }
                elseif ($op -eq "fg_window") { $h = 0L; $p = ""; $t = ""; [VoiceWinShim]::QueryForeground([ref]$h, [ref]$p, [ref]$t); Write-JsonLine @{ type = "reply"; reply = "fg_window"; seq_id = $seq; ok = $true; hwnd = $h; process = $p; title = $t } }
                elseif ($op -eq "beep") { Invoke-Tone ([string]$cmdObj.kind); Write-JsonLine @{ type = "reply"; reply = "beep"; seq_id = $seq; ok = $true } }
                elseif ($op -eq "hud") {
                    $dms = if ($null -ne $cmdObj.duration_ms) { [int]$cmdObj.duration_ms } else { 0 }
                    [VoiceWinShim]::UpdateHud([string]$cmdObj.state, [string]$cmdObj.text, $dms, [string]$cmdObj.position)
                    Write-JsonLine @{ type = "reply"; reply = "hud"; seq_id = $seq; ok = $true }
                } elseif ($op -eq "paste") {
                    $txt = [string]$cmdObj.text; $clipOk = $false
                    for ($i = 0; $i -lt 3; $i++) { try { [System.Windows.Forms.Clipboard]::SetText($txt); $clipOk = $true; break } catch { Start-Sleep -Milliseconds 15 } }
                    $h = 0L; $p = ""; $t = ""; [VoiceWinShim]::QueryForeground([ref]$h, [ref]$p, [ref]$t)
                    $pNorm = ($p -replace '\.exe$','').ToLowerInvariant()
                    $expectTerm = if ($null -ne $cmdObj.expect_terminal) { [bool]$cmdObj.expect_terminal } else { $true }
                    $thwnd = if ($null -ne $cmdObj.target_hwnd) { [long]$cmdObj.target_hwnd } else { 0L }
                    if ($clipOk -and (($thwnd -le 0) -or ($h -eq $thwnd)) -and ((-not $expectTerm) -or ($AllowedTerminals -contains $pNorm))) {
                        $sent = [VoiceWinShim]::SendCtrlV()
                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = [bool]$sent; clipboard = $clipOk; reason = $(if ($sent) { "ok" } else { "send_input_failed" }); fg_process = $p }
                    } else {
                        if ($clipOk) { Invoke-Tone "focus" }
                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "focus_changed" } else { "clipboard_failed" }); fg_process = $p }
                    }
                } elseif ($op -eq "rec_start") {
                    $loopback = if ($null -ne $cmdObj.loopback) { [bool]$cmdObj.loopback } else { $false }
                    $recErr = ""; $okStart = [VoiceWinShim]::StartRecording($loopback, [ref]$recErr)
                    Write-JsonLine @{ type = "reply"; reply = "rec_start"; seq_id = $seq; ok = [bool]$okStart; rec_error = [string]$recErr }
                } elseif ($op -eq "rec_stop") {
                    $pcmBytes = 0; $recErr = ""; $wavBytes = [VoiceWinShim]::StopRecording([ref]$pcmBytes, [ref]$recErr)
                    $b64 = if ($wavBytes -and $wavBytes.Length -gt 0) { [Convert]::ToBase64String($wavBytes) } else { "" }
                    Write-JsonLine @{ type = "reply"; reply = "rec_stop"; seq_id = $seq; ok = ($b64.Length -gt 0); pcm_bytes = $pcmBytes; wav_b64 = $b64; rec_error = [string]$recErr }
                } elseif ($op -eq "quit") { break bridgeLoop }
                else { Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $false; error = "unknown_cmd:$op" } }
            } catch {
                Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $false; error = [string]$_.Exception.Message }
            }
        }
    }
} finally {
    [VoiceWinShim]::StopHook()
}
