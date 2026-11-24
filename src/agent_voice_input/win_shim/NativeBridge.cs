using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Media;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public static partial class VoiceWinShim {
    private const int WH_KEYBOARD_LL = 13, WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101, WM_SYSKEYDOWN = 0x0104, WM_SYSKEYUP = 0x0105;
    private const uint WM_QUIT = 0x0012, WM_APP_WAKE = 0x8001, WM_TIMER = 0x0113, LLKHF_EXTENDED = 0x01, LLKHF_LOWER_IL_INJECTED = 0x02, LLKHF_INJECTED = 0x10, INPUT_KEYBOARD = 1, KEYEVENTF_EXTENDEDKEY = 0x0001, KEYEVENTF_KEYUP = 0x0002;
    private const ushort VK_LCONTROL = 0xA2, VK_LSHIFT = 0xA0, VK_INSERT = 0x2D, VK_V = 0x56;
    public delegate IntPtr LowLevelKeyboardProc(int nCode, IntPtr wParam, IntPtr lParam);
    [StructLayout(LayoutKind.Sequential)] private struct KBDLLHOOKSTRUCT { public uint vkCode, scanCode, flags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)] private struct POINT { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] private struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam, lParam; public uint time; public POINT pt; }
    [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Explicit)] public struct INPUT_UNION { [FieldOffset(0)] public KEYBDINPUT ki; [FieldOffset(0)] private long _p0; [FieldOffset(8)] private long _p1; [FieldOffset(16)] private long _p2; [FieldOffset(24)] private long _p3; }
    [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public INPUT_UNION u; }

    [StructLayout(LayoutKind.Sequential)] private struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }

    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr dpiContext);
    [DllImport("shcore.dll")] private static extern int SetProcessDpiAwareness(int awareness);
    [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] private static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint dwFlags);
    [DllImport("user32.dll", CharSet = CharSet.Auto)] private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO lpmi);
    [DllImport("shcore.dll")] private static extern int GetDpiForMonitor(IntPtr hmonitor, int dpiType, out uint dpiX, out uint dpiY);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern uint GetDpiForSystem();
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
    [DllImport("user32.dll")] private static extern uint MapVirtualKey(uint uCode, uint uMapType);
    [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool OpenClipboard(IntPtr hWndNewOwner);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool CloseClipboard();
    [DllImport("user32.dll", SetLastError = true)] private static extern bool EmptyClipboard();
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr GetClipboardData(uint uFormat);
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr SetClipboardData(uint uFormat, IntPtr hMem);
    [DllImport("user32.dll")] private static extern bool IsClipboardFormatAvailable(uint format);
    [DllImport("user32.dll")] private static extern uint EnumClipboardFormats(uint format);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern uint RegisterClipboardFormat(string lpszFormat);
    [DllImport("user32.dll")] private static extern uint GetClipboardSequenceNumber();
    [DllImport("user32.dll")] private static extern IntPtr GetClipboardOwner();
    [DllImport("user32.dll")] private static extern bool IsHungAppWindow(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern IntPtr CreateWindowEx(uint dwExStyle, string lpClassName, string lpWindowName, uint dwStyle, int x, int y, int nWidth, int nHeight, IntPtr hWndParent, IntPtr hMenu, IntPtr hInstance, IntPtr lpParam);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool DestroyWindow(IntPtr hWnd);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr GlobalAlloc(uint uFlags, UIntPtr dwBytes);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr GlobalFree(IntPtr hMem);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern IntPtr GlobalLock(IntPtr hMem);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool GlobalUnlock(IntPtr hMem);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern UIntPtr GlobalSize(IntPtr hMem);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll")] public static extern ulong GetTickCount64();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetModuleHandle(string lpModuleName);
    [DllImport("winmm.dll")] public static extern uint waveInGetNumDevs();
    [DllImport("winmm.dll", CharSet = CharSet.Unicode)] public static extern int mciSendString(string cmd, StringBuilder ret, int retLen, IntPtr hwnd);

    public sealed class QueuedCmd {
        public readonly string Line;
        public readonly long EnqueuedTick;
        public QueuedCmd(string line, long enqueuedTick) { Line = line; EnqueuedTick = enqueuedTick; }
    }
    public static readonly ConcurrentQueue<QueuedCmd> CmdQueue = new ConcurrentQueue<QueuedCmd>();
    public static readonly ConcurrentQueue<ShimEvent> EvtQueue = new ConcurrentQueue<ShimEvent>();
    public static volatile bool StdinClosed = false;
    private static IntPtr _hook = IntPtr.Zero;
    private static LowLevelKeyboardProc _procRef;
    private static PowerBroadcastWindow _powerWin = null;
    private static PowerResumeDebouncer _powerResumeDebouncer = new PowerResumeDebouncer();
    private static readonly Dictionary<string, SoundPlayer> _tones = new Dictionary<string, SoundPlayer>(StringComparer.OrdinalIgnoreCase);
    private static uint _mainTid = 0, _targetVk = 0xA3;
    private static bool _isPtt = false, _keyTest = false, _hasQuitTimer = false;
    private static WinHotkeyEngine _hotkeyEngine = null;
    private static System.Windows.Forms.Timer _holdHintTimer = null;
    private static System.Windows.Forms.Timer _holdCancelTimer = null;
    private static ulong _armedTimerGen = 0UL;
    private static readonly Dictionary<string, uint> _keyMap = new Dictionary<string, uint>(StringComparer.OrdinalIgnoreCase) {
        {"rcontrol",0xA3},{"rctrl",0xA3},{"rightcontrol",0xA3},{"rightctrl",0xA3},
        {"lcontrol",0xA2},{"lctrl",0xA2},{"leftcontrol",0xA2},{"leftctrl",0xA2},{"control",0xA2},{"ctrl",0xA2},
        {"ralt",0xA5},{"rightalt",0xA5},{"rmenu",0xA5},{"roption",0xA5},{"rightoption",0xA5},
        {"lalt",0xA4},{"leftalt",0xA4},{"lmenu",0xA4},{"alt",0xA4},{"loption",0xA4},{"leftoption",0xA4},{"option",0xA4},
        {"rshift",0xA1},{"rightshift",0xA1},{"lshift",0xA0},{"leftshift",0xA0},{"shift",0xA0},
        {"rcommand",0x5C},{"rcmd",0x5C},{"rightcommand",0x5C},{"rightcmd",0x5C},{"rwin",0x5C},{"rsuper",0x5C},{"rightwin",0x5C},{"rightsuper",0x5C},
        {"lcommand",0x5B},{"lcmd",0x5B},{"leftcommand",0x5B},{"leftcmd",0x5B},{"lwin",0x5B},{"lsuper",0x5B},{"leftwin",0x5B},{"leftsuper",0x5B},{"command",0x5B},{"cmd",0x5B},{"win",0x5B},{"super",0x5B},
        {"capslock",0x14},{"scrolllock",0x91},{"pause",0x13},{"insert",0x2D},{"space",0x20}
    };

    public static void EnableDpiAwareness() {
        try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return; } catch { }
        try { if (SetProcessDpiAwareness(2) == 0) return; } catch { }
        try { SetProcessDPIAware(); } catch { }
    }

    private static int ClampDpi(int dpi) { return (dpi < 96) ? 96 : ((dpi > 480) ? 480 : dpi); }

    public static int GetTargetDpi(IntPtr fgHwnd) {
        IntPtr target = (fgHwnd != IntPtr.Zero) ? fgHwnd : GetForegroundWindow();
        try {
            IntPtr hMon = MonitorFromWindow(target, 2u);
            if (hMon != IntPtr.Zero) {
                uint dpiX, dpiY;
                if (GetDpiForMonitor(hMon, 0, out dpiX, out dpiY) == 0 && dpiX > 0) return ClampDpi((int)dpiX);
            }
        } catch { }
        if (target != IntPtr.Zero) {
            try { uint wDpi = GetDpiForWindow(target); if (wDpi > 0) return ClampDpi((int)wDpi); } catch { }
        }
        try { uint sDpi = GetDpiForSystem(); if (sDpi > 0) return ClampDpi((int)sDpi); } catch { }
        return 96;
    }

    public static Rectangle GetTargetWorkArea(IntPtr fgHwnd) {
        IntPtr target = (fgHwnd != IntPtr.Zero) ? fgHwnd : GetForegroundWindow();
        try {
            IntPtr hMon = MonitorFromWindow(target, 2u);
            if (hMon != IntPtr.Zero) {
                MONITORINFO mi = new MONITORINFO();
                mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
                if (GetMonitorInfo(hMon, ref mi) && mi.rcWork.Right > mi.rcWork.Left && mi.rcWork.Bottom > mi.rcWork.Top) {
                    return Rectangle.FromLTRB(mi.rcWork.Left, mi.rcWork.Top, mi.rcWork.Right, mi.rcWork.Bottom);
                }
            }
        } catch { }
        Screen scr = (target != IntPtr.Zero) ? Screen.FromHandle(target) : Screen.PrimaryScreen;
        if (scr == null) scr = Screen.PrimaryScreen;
        return (scr != null) ? scr.WorkingArea : new Rectangle(0, 0, 1920, 1080);
    }

    public static uint ResolveKeyName(string raw) {
        string k = (raw ?? "RControl").Trim();
        string compact = k.Replace("_", "").Replace("-", "").Replace(" ", "");
        _hotkeyLabel = (compact.Equals("rcontrol", StringComparison.OrdinalIgnoreCase) || compact.Equals("rctrl", StringComparison.OrdinalIgnoreCase) || compact.Equals("rightcontrol", StringComparison.OrdinalIgnoreCase) || compact.Equals("rightctrl", StringComparison.OrdinalIgnoreCase)) ? "\u53F3 Ctrl" : (" " + k);
        uint vk; int fn;
        if (compact.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) return Convert.ToUInt32(compact.Substring(2), 16);
        if (_keyMap.TryGetValue(compact, out vk)) return vk;
        if ((compact.StartsWith("f", StringComparison.OrdinalIgnoreCase)) && int.TryParse(compact.Substring(1), out fn) && fn >= 1 && fn <= 24) return (uint)(0x6F + fn);
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

    private static long ElapsedStopwatchMs(long startTick, long endTick) {
        if (endTick <= startTick) return 0L;
        return (long)((endTick - startTick) * 1000.0 / Stopwatch.Frequency);
    }

    public static bool IsDeadlineExceeded(long startTick, int deadlineMs) {
        if (deadlineMs <= 0) return false;
        return ElapsedStopwatchMs(startTick, Stopwatch.GetTimestamp()) >= (long)deadlineMs;
    }

    public static void QueryForeground(out long hwnd, out string procName, out string title) {
        IntPtr h = GetForegroundWindow(); hwnd = h.ToInt64(); procName = ""; title = "";
        if (h == IntPtr.Zero) return;
        StringBuilder sb = new StringBuilder(1024); GetWindowText(h, sb, sb.Capacity); title = sb.ToString();
        int pid; GetWindowThreadProcessId(h, out pid);
        if (pid > 0) { try { procName = Process.GetProcessById(pid).ProcessName ?? ""; } catch { procName = ""; } }
    }

    private static void EnqueueTrigger(string kind, uint vk, uint scan, bool ext, bool isDown, bool tainted, int holdMs = 0) {
        long hwnd = 0; string proc = "", title = "";
        if (kind == "toggle" || kind == "ptt_start" || kind == "ptt_stop" || kind == "hold_cancel") QueryForeground(out hwnd, out proc, out title);
        EvtQueue.Enqueue(new ShimEvent { Kind = kind, Vk = vk, Scan = scan, Extended = ext, IsDown = isDown, Tainted = tainted, Hwnd = hwnd, Process = proc, Title = title, HoldMs = holdMs, TsMs = GetTickCount64() });
        if (_mainTid != 0) PostThreadMessage(_mainTid, WM_APP_WAKE, IntPtr.Zero, IntPtr.Zero);
    }

    private static bool IsVkPhysicallyDown(uint vk) {
        return vk > 0u && (GetAsyncKeyState((int)vk) & 0x8000) != 0;
    }

    private static void StopHoldTimers() {
        if (_holdHintTimer != null) { try { _holdHintTimer.Stop(); } catch { } }
        if (_holdCancelTimer != null) { try { _holdCancelTimer.Stop(); } catch { } }
    }

    private static void EnsureHoldTimers() {
        if (_holdHintTimer == null) {
            _holdHintTimer = new System.Windows.Forms.Timer();
            _holdHintTimer.Tick += (s, e) => {
                _holdHintTimer.Stop();
                if (_hotkeyEngine == null) return;
                HotkeyStepResult step = _hotkeyEngine.OnHoldHintTimeout(_armedTimerGen, GetTickCount64(), IsVkPhysicallyDown);
                ApplyHotkeyStep(step);
            };
        }
        if (_holdCancelTimer == null) {
            _holdCancelTimer = new System.Windows.Forms.Timer();
            _holdCancelTimer.Tick += (s, e) => {
                _holdCancelTimer.Stop();
                if (_hotkeyEngine == null) return;
                HotkeyStepResult step = _hotkeyEngine.OnHoldCancelTimeout(_armedTimerGen, GetTickCount64(), IsVkPhysicallyDown);
                ApplyHotkeyStep(step);
            };
        }
    }

    private static void ApplyHotkeyStep(HotkeyStepResult step) {
        if (step.CancelTimers) StopHoldTimers();
        if (step.ArmTimers) {
            EnsureHoldTimers();
            StopHoldTimers();
            _armedTimerGen = step.TimerGen;
            if (step.HintDelayMs > 0) {
                _holdHintTimer.Interval = Math.Max(1, step.HintDelayMs);
                _holdHintTimer.Start();
            }
            if (step.CancelDelayMs > 0) {
                _holdCancelTimer.Interval = Math.Max(1, step.CancelDelayMs);
                _holdCancelTimer.Start();
            }
        }
        if (step.ClearHoldPreview) {
            StopHoldPreviewSafetyTimer();
            if (_hud != null && !_hud.IsDisposed) _hud.SetHoldCancelPreview(false, _headlessHudMode);
        } else if (step.ShowHoldPreview) {
            StopHoldPreviewSafetyTimer();
            if (_hud != null && !_hud.IsDisposed && _hud.CanShowHoldCancelPreview) {
                _hud.SetHoldCancelPreview(true, _headlessHudMode);
            }
        } else if (step.ArmPreviewSafetyClear) {
            if (_hud != null && !_hud.IsDisposed && _hud.HoldCancelPreviewActive) {
                ArmHoldPreviewSafetyTimer();
            }
        }
        if (step.Events != null) {
            for (int i = 0; i < step.Events.Count; i++) {
                HotkeyAction ev = step.Events[i];
                EnqueueTrigger(ev.Kind, ev.Vk, ev.Scan, ev.Extended, ev.IsDown, ev.Tainted, ev.HoldMs);
            }
        }
    }

    private static IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam) {
        if (nCode >= 0) {
            KBDLLHOOKSTRUCT kb = (KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(KBDLLHOOKSTRUCT));
            if ((kb.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)) == 0) {
                int msg = wParam.ToInt32();
                bool isDown = (msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN), isUp = (msg == WM_KEYUP || msg == WM_SYSKEYUP), ext = (kb.flags & LLKHF_EXTENDED) != 0;
                uint vk = kb.vkCode;
                if (_keyTest && isDown && vk == 0x1B && _mainTid != 0) {
                    PostThreadMessage(_mainTid, WM_QUIT, IntPtr.Zero, IntPtr.Zero);
                    return CallNextHookEx(_hook, nCode, wParam, lParam);
                }
                if (_hotkeyEngine != null && (isDown || isUp)) {
                    HotkeyStepResult step = _hotkeyEngine.ProcessKeyEvent(vk, kb.scanCode, ext, isDown, isUp, GetTickCount64(), IsVkPhysicallyDown);
                    ApplyHotkeyStep(step);
                }
            }
        }
        return CallNextHookEx(_hook, nCode, wParam, lParam);
    }

    private static bool ReinstallKeyboardHook() {
        if (_procRef == null) return false;
        try {
            IntPtr newHook = IntPtr.Zero;
            using (Process cur = Process.GetCurrentProcess()) using (ProcessModule mod = cur.MainModule) {
                newHook = SetWindowsHookEx(WH_KEYBOARD_LL, _procRef, GetModuleHandle(mod.ModuleName), 0);
            }
            if (newHook == IntPtr.Zero) return false;
            IntPtr oldHook = _hook;
            _hook = newHook;
            if (oldHook != IntPtr.Zero) {
                try { UnhookWindowsHookEx(oldHook); } catch { }
            }
            return true;
        } catch {
            return false;
        }
    }

    public static void OnPowerResume(ulong nowMs) {
        if (_keyTest) return;
        if (!_powerResumeDebouncer.ShouldEmit(nowMs)) return;
        ReinstallKeyboardHook();
        if (_hotkeyEngine != null) {
            HotkeyStepResult resetStep = _hotkeyEngine.ResetForPowerResume();
            ApplyHotkeyStep(resetStep);
        } else {
            StopHoldTimers();
            StopHoldPreviewSafetyTimer();
            if (_hud != null && !_hud.IsDisposed) _hud.SetHoldCancelPreview(false, _headlessHudMode);
        }
        EvtQueue.Enqueue(new ShimEvent { Kind = "power_resume", TsMs = nowMs });
        if (_mainTid != 0) PostThreadMessage(_mainTid, WM_APP_WAKE, IntPtr.Zero, IntPtr.Zero);
    }

    public static void StartHook(uint targetVk, bool isPtt, int tapMaxMs, int debounceMs, bool keyTest, int timerMs, int holdCancelMs = 800, int holdHintMs = 250) {
        _mainTid = GetCurrentThreadId(); MSG initMsg; PeekMessage(out initMsg, IntPtr.Zero, 0, 0, 0);
        _targetVk = targetVk; _isPtt = isPtt; _keyTest = keyTest; _hasQuitTimer = (timerMs > 0);
        _hotkeyEngine = new WinHotkeyEngine(targetVk, isPtt, tapMaxMs, holdCancelMs, holdHintMs, debounceMs, keyTest);
        _powerResumeDebouncer = new PowerResumeDebouncer();
        EnsureHoldTimers();
        _procRef = HookCallback;
        using (Process cur = Process.GetCurrentProcess()) using (ProcessModule mod = cur.MainModule) { _hook = SetWindowsHookEx(WH_KEYBOARD_LL, _procRef, GetModuleHandle(mod.ModuleName), 0); }
        if (_hook == IntPtr.Zero) throw new InvalidOperationException("SetWindowsHookEx failed: " + Marshal.GetLastWin32Error());
        if (!keyTest) {
            try { _powerWin = new PowerBroadcastWindow(); } catch { _powerWin = null; }
        }
        if (_hasQuitTimer) SetTimer(IntPtr.Zero, new IntPtr(1), (uint)timerMs, IntPtr.Zero);
    }

    public static void StartStdinReader() {
        Thread t = new Thread(() => {
            try { string line; while ((line = Console.In.ReadLine()) != null) { CmdQueue.Enqueue(new QueuedCmd(line, Stopwatch.GetTimestamp())); if (_mainTid != 0) PostThreadMessage(_mainTid, WM_APP_WAKE, IntPtr.Zero, IntPtr.Zero); } } catch { }
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

    public static void StopHook() {
        int dummyBytes; string dummyErr; StopRecording(0L, out dummyBytes, out dummyErr, false);
        ReleaseStoppedRecording(0L);
        StopHoldTimers();
        StopHoldPreviewSafetyTimer();
        if (_holdHintTimer != null) { try { _holdHintTimer.Dispose(); } catch { } _holdHintTimer = null; }
        if (_holdCancelTimer != null) { try { _holdCancelTimer.Dispose(); } catch { } _holdCancelTimer = null; }
        if (_holdPreviewSafetyTimer != null) { try { _holdPreviewSafetyTimer.Dispose(); } catch { } _holdPreviewSafetyTimer = null; }
        _hotkeyEngine = null;
        FlushPendingClipRestore();
        if (_clipRestoreTimer != null) { try { _clipRestoreTimer.Stop(); _clipRestoreTimer.Dispose(); } catch { } _clipRestoreTimer = null; }
        if (_clipHwnd != IntPtr.Zero) { try { DestroyWindow(_clipHwnd); } catch { } _clipHwnd = IntPtr.Zero; }
        if (_powerWin != null) { try { _powerWin.DestroyHandle(); } catch { } _powerWin = null; }
        if (_hook != IntPtr.Zero) { UnhookWindowsHookEx(_hook); _hook = IntPtr.Zero; }
        if (_hud != null) { try { _hud.Dispose(); } catch { } _hud = null; }
        if (_toastHud != null) { try { _toastHud.Dispose(); } catch { } _toastHud = null; }
    }

    public static bool IsAnyPasteBlockingKeyDown() {
        if (_hotkeyEngine != null && _hotkeyEngine.TargetDown) return true;
        int[] vks = new int[] { 0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5, 0x10, 0xA0, 0xA1, 0x5B, 0x5C, (int)_targetVk };
        foreach (int vk in vks) {
            if (vk > 0 && (GetAsyncKeyState(vk) & 0x8000) != 0) return true;
        }
        return false;
    }

    private static void FillKeyInput(ref INPUT inp, ushort vk, bool keyUp, bool extended) {
        inp.type = INPUT_KEYBOARD;
        inp.u.ki.wVk = vk;
        inp.u.ki.wScan = (ushort)MapVirtualKey((uint)vk, 0u);
        inp.u.ki.dwFlags = (keyUp ? KEYEVENTF_KEYUP : 0u) | (extended ? KEYEVENTF_EXTENDEDKEY : 0u);
    }

    public static INPUT[] BuildPasteChordInputs(string chord) {
        string mode = (chord ?? "ctrl_v").Trim().ToLowerInvariant();
        ushort[] mods;
        ushort mainVk;
        bool mainExt = false;
        if (mode == "ctrl_shift_v") {
            mods = new ushort[] { VK_LCONTROL, VK_LSHIFT };
            mainVk = VK_V;
        } else if (mode == "shift_insert") {
            mods = new ushort[] { VK_LSHIFT };
            mainVk = VK_INSERT;
            mainExt = true; // Dedicated Insert (E0 52) MUST set KEYEVENTF_EXTENDEDKEY on both down & up!
        } else {
            mods = new ushort[] { VK_LCONTROL };
            mainVk = VK_V;
        }
        int total = mods.Length * 2 + 2;
        INPUT[] ins = new INPUT[total];
        int idx = 0;
        for (int m = 0; m < mods.Length; m++) FillKeyInput(ref ins[idx++], mods[m], false, false);
        FillKeyInput(ref ins[idx++], mainVk, false, mainExt);
        FillKeyInput(ref ins[idx++], mainVk, true, mainExt);
        for (int m = mods.Length - 1; m >= 0; m--) FillKeyInput(ref ins[idx++], mods[m], true, false);
        return ins;
    }

    public static bool SendPasteChord(string chord) {
        INPUT[] ins = BuildPasteChordInputs(chord);
        return SendInput((uint)ins.Length, ins, Marshal.SizeOf(typeof(INPUT))) == (uint)ins.Length;
    }
}
