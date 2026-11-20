param(
    [string]$Mode = "bridge", [string]$Hotkey = "RControl", [string]$TriggerMode = "tap_toggle",
    [int]$TapMaxMs = 1200, [int]$DebounceMs = 180, [int]$Seconds = 10, [int]$Beep = 0,
    [Alias("AllowedTerminals")][string]$AllowedTerminalsCsv = "",
    [string]$CacheDir = ""
)
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$csSource = @"
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Media;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

public struct ShimEvent { public string Kind, Process, Title, Reason; public uint Vk, Scan; public bool Extended, IsDown, Tainted; public long Hwnd, RecGen; public ulong TsMs; public int GapMs, HoldMs; }

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

public struct HudLayoutMetrics {
    public int Width, Height, MaxWidth, PadL, PadR, Dpi;
    public bool IsToast, IsWaveRecording, Truncated, IsCountdownWarning;
    public int BgCount;
    public Rectangle DurRect, WaveRect, HintRect, BadgeRect, IconRect, MainTextRect;
    public int DurMeasuredW, DurSlotW, HintMeasuredW, HintSlotW, BadgeMeasuredW, BadgeSlotW, MainMeasuredW, HintSegMeasuredW;
    public string DurationText, HintText, BadgeText, MainText;
}

public sealed class HudPillForm : Form {
    [StructLayout(LayoutKind.Sequential)] private struct WinPoint { public int x, y; public WinPoint(int x, int y) { this.x = x; this.y = y; } }
    [StructLayout(LayoutKind.Sequential)] private struct WinSize { public int cx, cy; public WinSize(int cx, int cy) { this.cx = cx; this.cy = cy; } }
    [StructLayout(LayoutKind.Sequential, Pack = 1)] private struct BlendFunc { public byte BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat; }
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool UpdateLayeredWindow(IntPtr hwnd, IntPtr hdcDst, ref WinPoint pptDst, ref WinSize psize, IntPtr hdcSrc, ref WinPoint pptSrc, int crKey, ref BlendFunc pblend, int dwFlags);
    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr hdc);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr hdc);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr hdc, IntPtr h);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr hObject);
    private const TextFormatFlags MeasureFlags = TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding;
    private const TextFormatFlags SegDrawFlags = TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding;
    private const TextFormatFlags DurDrawFlags = TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding;
    private const TextFormatFlags TruncDrawFlags = TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding | TextFormatFlags.EndEllipsis;
    private static readonly Color DefaultBgColor = Color.FromArgb(64, 18, 26);
    private static readonly Color RecBgColor = Color.FromArgb(64, 18, 26);
    private static readonly Color RecBorderColor = Color.FromArgb(196, 66, 80);
    private static readonly Color RecWaveActiveColor = Color.FromArgb(255, 255, 104, 112);
    private static readonly Color RecWaveQuietColor = Color.FromArgb(255, 232, 92, 102);
    private static readonly Color RecMainColor = Color.FromArgb(248, 250, 252);
    private static readonly Color RecHintColor = Color.FromArgb(230, 188, 178, 192);
    private static readonly Color WarnBgColor = Color.FromArgb(72, 38, 12);
    private static readonly Color WarnBorderColor = Color.FromArgb(224, 138, 28);
    private static readonly Color WarnWaveActiveColor = Color.FromArgb(255, 251, 191, 36);
    private static readonly Color WarnWaveQuietColor = Color.FromArgb(220, 251, 191, 36);
    private static readonly Color WarnMainColor = Color.FromArgb(254, 252, 232);
    private static readonly Color WarnHintColor = Color.FromArgb(255, 251, 191, 36);
    private static readonly Color BadgeBgColor = Color.FromArgb(64, 56, 189, 248);
    private static readonly Color BadgeBorderColor = Color.FromArgb(148, 56, 189, 248);
    private static readonly Color BadgeTextColor = Color.FromArgb(255, 186, 230, 253);
    private static readonly float[] BellWeight = new float[] { 1.00f, 0.92f, 0.78f, 0.58f, 0.40f };
    private readonly bool _isToast;
    private Font _font, _badgeFont;
    private bool _ownsFont, _ownsBadgeFont, _cjkFont;
    private int _dpi = 96;
    private readonly System.Windows.Forms.Timer _hideTimer = new System.Windows.Forms.Timer();
    private readonly System.Windows.Forms.Timer _animTimer = new System.Windows.Forms.Timer();
    private readonly float[] _waveHist = new float[5];
    private readonly float[] _barNorms = new float[9];
    private float _smoothLevel = 0f;
    private int _frameIdx = 0;
    private long _recStartTick = 0L, _elapsedOverrideSec = -1L;
    private int _maxDurationSec = 0, _warnRemainSec = 0, _bgCount = 0;
    private bool _isWaveRecording = false, _shown = false;
    private string _text = "", _position = "bottom", _iconKind = "";
    private Color _bgColor = DefaultBgColor, _iconColor = Color.White, _mainColor = Color.White, _hintColor = RecHintColor, _borderColor = Color.Gray;
    private IntPtr _fgHwnd = IntPtr.Zero;
    private Rectangle _anchoredWorkArea = Rectangle.Empty;
    private int _lastX = int.MinValue, _lastY = int.MinValue, _lastW = 0, _lastH = 0;
    private int _lastAutoHideMs = 0;
    private int _showUpdates = 0;

    public bool IsToast { get { return _isToast; } }
    public bool IsShown { get { return _shown; } }
    public string CurrentText { get { return _text; } }
    public int CurrentBgCount { get { return _bgCount; } }
    public int LastAutoHideMs { get { return _lastAutoHideMs; } }
    public int ShowUpdateCount { get { return _showUpdates; } }
    public Rectangle AnchoredWorkArea { get { return _anchoredWorkArea; } }
    public int AnchoredDpi { get { return _dpi; } }
    public bool IsWaveAnimating { get { return _isWaveRecording && _animTimer.Enabled; } }
    public int LastX { get { return _lastX; } }
    public int LastY { get { return _lastY; } }
    public int LastWidth { get { return _lastW; } }
    public int LastHeight { get { return _lastH; } }
    public bool IsWaveRecording { get { return _isWaveRecording; } }
    public bool IsCountdownWarning {
        get {
            if (!_isWaveRecording || _maxDurationSec <= 0 || _warnRemainSec <= 0) return false;
            long rem = Math.Max(0L, (long)_maxDurationSec - GetEffectiveElapsedSec());
            return rem <= (long)_warnRemainSec;
        }
    }
    public float[] CurrentBarNorms { get { float[] c = new float[9]; Array.Copy(_barNorms, c, 9); return c; } }
    public string CurrentDurationText { get { return VoiceWinShim.FormatRecordingDuration(GetEffectiveElapsedSec()); } }

    private int ScalePx(int px) { return (int)Math.Round(px * _dpi / 96.0); }

    private long GetEffectiveElapsedSec() {
        if (_elapsedOverrideSec >= 0L) return _elapsedOverrideSec;
        long diffMs = (long)VoiceWinShim.GetTickCount64() - _recStartTick;
        return (diffMs > 0L) ? (diffMs / 1000L) : 0L;
    }

    private void ResetWaveEnvelope() {
        _smoothLevel = 0f; _frameIdx = 0; _elapsedOverrideSec = -1L;
        Array.Clear(_waveHist, 0, _waveHist.Length);
        Array.Clear(_barNorms, 0, _barNorms.Length);
    }

    private void StepWaveEnvelope(float rawLvl) {
        float r = (rawLvl < 0f) ? 0f : ((rawLvl > 1f) ? 1f : rawLvl);
        _smoothLevel = (r > _smoothLevel) ? (_smoothLevel * 0.35f + r * 0.65f) : (_smoothLevel * 0.70f + r * 0.30f);
        for (int k = _waveHist.Length - 1; k > 0; k--) _waveHist[k] = _waveHist[k - 1];
        _waveHist[0] = _smoothLevel;
        _frameIdx++;
        for (int i = 0; i < 9; i++) {
            int dist = Math.Abs(i - 4);
            float v = _waveHist[dist] * BellWeight[dist];
            if (v > 0.03f) v *= (float)(0.86 + 0.14 * Math.Sin(_frameIdx * 0.75 + i * 1.15));
            else v = 0f;
            _barNorms[i] = (v < 0f) ? 0f : ((v > 1f) ? 1f : v);
        }
    }

    public void StepTestRecordingFrame(float rawLevel, long elapsedSec) {
        _elapsedOverrideSec = elapsedSec;
        StepWaveEnvelope(rawLevel);
        RenderPill(false);
    }

    private static bool HasCjk(string s) {
        if (string.IsNullOrEmpty(s)) return false;
        for (int i = 0; i < s.Length; i++) { if (s[i] >= 0x2E80 && s[i] <= 0x9FFF) return true; }
        return false;
    }

    private static Font CreateHudFont(bool preferCjk, int dpi, bool isToast, out bool owns) {
        float pxSize = isToast
            ? Math.Max(11f, (float)Math.Round(12.25f * dpi / 96.0f))
            : Math.Max(12f, (float)Math.Round(13.0f * dpi / 96.0f));
        string[] fams = (isToast || preferCjk)
            ? new string[] { "Microsoft YaHei UI", "Segoe UI" }
            : new string[] { "Segoe UI", "Microsoft YaHei UI" };
        foreach (string n in fams) {
            try {
                using (FontFamily ff = new FontFamily(n)) {
                    FontStyle st = ff.IsStyleAvailable(FontStyle.Regular) ? FontStyle.Regular : FontStyle.Bold;
                    owns = true;
                    return new Font(ff, pxSize, st, GraphicsUnit.Pixel);
                }
            } catch { }
        }
        try { owns = true; return new Font(FontFamily.GenericSansSerif, pxSize, FontStyle.Regular, GraphicsUnit.Pixel); }
        catch { owns = false; return SystemFonts.DefaultFont; }
    }

    private static Font CreateBadgeFont(int dpi, out bool owns) {
        float pxSize = Math.Max(10f, (float)Math.Round(11.0f * dpi / 96.0f));
        string[] fams = new string[] { "Segoe UI", "Microsoft YaHei UI" };
        foreach (string n in fams) {
            try {
                using (FontFamily ff = new FontFamily(n)) {
                    FontStyle st = ff.IsStyleAvailable(FontStyle.Bold) ? FontStyle.Bold : FontStyle.Regular;
                    owns = true;
                    return new Font(ff, pxSize, st, GraphicsUnit.Pixel);
                }
            } catch { }
        }
        try { owns = true; return new Font(FontFamily.GenericSansSerif, pxSize, FontStyle.Bold, GraphicsUnit.Pixel); }
        catch { owns = false; return SystemFonts.DefaultFont; }
    }

    private void EnsureFontsFor(bool wantCjk, int dpi) {
        bool dpiChanged = (dpi != _dpi);
        if (_font == null || wantCjk != _cjkFont || dpiChanged) {
            if (_ownsFont && _font != null) _font.Dispose();
            _cjkFont = wantCjk; _dpi = dpi;
            _font = CreateHudFont(_cjkFont, _dpi, _isToast, out _ownsFont);
        }
        if (_badgeFont == null || dpiChanged) {
            if (_ownsBadgeFont && _badgeFont != null) _badgeFont.Dispose();
            _badgeFont = CreateBadgeFont(_dpi, out _ownsBadgeFont);
        }
    }

    private static GraphicsPath BuildCapsulePath(float x, float y, float rw, float rh) {
        GraphicsPath path = new GraphicsPath();
        float d = Math.Max(2f, Math.Min(rw, rh));
        path.AddArc(x, y, d, d, 90f, 180f);
        path.AddArc(x + rw - d, y, d, d, 270f, 180f);
        path.CloseFigure();
        return path;
    }

    private static GraphicsPath BuildVerticalBarPath(float x, float y, float bw, float bh) {
        GraphicsPath p = new GraphicsPath();
        if (bh <= bw + 0.05f) {
            p.AddEllipse(x, y, bw, bw);
        } else {
            p.AddArc(x, y, bw, bw, 180f, 180f);
            p.AddArc(x, y + bh - bw, bw, bw, 0f, 180f);
            p.CloseFigure();
        }
        return p;
    }

    private static string ResolveIconKind(string iconKind, string iconSeg) {
        if (!string.IsNullOrEmpty(iconKind)) return iconKind;
        if (iconSeg == "\u25D0") return "recognizing";
        if (iconSeg == "\u25C8") return "refining";
        if (iconSeg == "\u2713") return "done";
        if (iconSeg == "\u25CB") return "empty_ring";
        if (iconSeg == "!" || iconSeg == "\uFF01" || (!string.IsNullOrEmpty(iconSeg) && iconSeg[0] == '\u26A0')) return "warning";
        return "cross";
    }

    private static void AddQuadCurve(GraphicsPath gp, PointF p0, PointF ctrl, PointF p1) {
        PointF c1 = new PointF(p0.X + (ctrl.X - p0.X) * (2.0f / 3.0f), p0.Y + (ctrl.Y - p0.Y) * (2.0f / 3.0f));
        PointF c2 = new PointF(p1.X + (ctrl.X - p1.X) * (2.0f / 3.0f), p1.Y + (ctrl.Y - p1.Y) * (2.0f / 3.0f));
        gp.AddBezier(p0, c1, c2, p1);
    }

    private static void DrawStatusIcon(Graphics g, string kind, Color color, float x, float y, float box, float dpiScale) {
        SmoothingMode prevSm = g.SmoothingMode;
        PixelOffsetMode prevPo = g.PixelOffsetMode;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;
        try {
            if (kind == "recognizing") {
                float ins = 1.0f * dpiScale;
                float d = Math.Max(2.0f, box - ins * 2.0f);
                using (Pen p = new Pen(color, 1.6f * dpiScale)) {
                    g.DrawEllipse(p, x + ins, y + ins, d, d);
                }
                using (GraphicsPath semi = new GraphicsPath())
                using (SolidBrush b = new SolidBrush(color)) {
                    semi.AddArc(x + ins, y + ins, d, d, 90f, 180f);
                    semi.CloseFigure();
                    g.FillPath(b, semi);
                }
            } else if (kind == "refining") {
                float cx = x + box * 0.5f, cy = y + box * 0.5f;
                PointF top = new PointF(cx, y + box * 0.04f);
                PointF right = new PointF(x + box * 0.96f, cy);
                PointF bottom = new PointF(cx, y + box * 0.96f);
                PointF left = new PointF(x + box * 0.04f, cy);
                float pull = box * 0.085f;
                using (GraphicsPath sp = new GraphicsPath())
                using (SolidBrush b = new SolidBrush(color)) {
                    AddQuadCurve(sp, top, new PointF(cx + pull, cy - pull), right);
                    AddQuadCurve(sp, right, new PointF(cx + pull, cy + pull), bottom);
                    AddQuadCurve(sp, bottom, new PointF(cx - pull, cy + pull), left);
                    AddQuadCurve(sp, left, new PointF(cx - pull, cy - pull), top);
                    sp.CloseFigure();
                    g.FillPath(b, sp);
                }
            } else if (kind == "done") {
                using (Pen p = new Pen(color, 1.8f * dpiScale)) {
                    p.StartCap = LineCap.Round;
                    p.EndCap = LineCap.Round;
                    p.LineJoin = LineJoin.Round;
                    g.DrawLines(p, new PointF[] {
                        new PointF(x + box * 0.14f, y + box * 0.54f),
                        new PointF(x + box * 0.40f, y + box * 0.80f),
                        new PointF(x + box * 0.88f, y + box * 0.24f)
                    });
                }
            } else if (kind == "empty" || kind == "empty_ring") {
                float ins = 1.0f * dpiScale;
                float d = Math.Max(2.0f, box - ins * 2.0f);
                using (Pen p = new Pen(color, 1.6f * dpiScale)) {
                    g.DrawEllipse(p, x + ins, y + ins, d, d);
                }
            } else if (kind == "warning") {
                float cx = x + box * 0.5f;
                float strokeW = Math.Max(1.5f, 1.8f * dpiScale);
                using (Pen p = new Pen(color, strokeW)) {
                    p.StartCap = LineCap.Round;
                    p.EndCap = LineCap.Round;
                    g.DrawLine(p, cx, y + box * 0.15f, cx, y + box * 0.52f);
                }
                float dotD = Math.Max(2.0f, 2.1f * dpiScale);
                using (SolidBrush b = new SolidBrush(color)) {
                    g.FillEllipse(b, cx - dotD * 0.5f, y + box * 0.80f - dotD * 0.5f, dotD, dotD);
                }
            } else {
                float ins = 1.8f * dpiScale;
                using (Pen p = new Pen(color, 1.8f * dpiScale)) {
                    p.StartCap = LineCap.Round;
                    p.EndCap = LineCap.Round;
                    g.DrawLine(p, x + ins, y + ins, x + box - ins, y + box - ins);
                    g.DrawLine(p, x + box - ins, y + ins, x + ins, y + box - ins);
                }
            }
        } finally {
            g.SmoothingMode = prevSm;
            g.PixelOffsetMode = prevPo;
        }
    }

    public HudPillForm(bool isToast = false) {
        _isToast = isToast;
        _cjkFont = VoiceWinShim.GetHostUiCulture().StartsWith("zh", StringComparison.OrdinalIgnoreCase);
        _dpi = VoiceWinShim.GetTargetDpi(IntPtr.Zero);
        _font = CreateHudFont(_cjkFont, _dpi, _isToast, out _ownsFont);
        _badgeFont = CreateBadgeFont(_dpi, out _ownsBadgeFont);
        AutoScaleMode = AutoScaleMode.None;
        FormBorderStyle = FormBorderStyle.None; StartPosition = FormStartPosition.Manual;
        ShowInTaskbar = false; BackColor = DefaultBgColor;
        _hideTimer.Tick += (s, e) => { _hideTimer.Stop(); HideNoActivate(); };
        _animTimer.Interval = 60;
        _animTimer.Tick += (s, e) => {
            if (!_isWaveRecording || IsDisposed) { _animTimer.Stop(); return; }
            StepWaveEnvelope(VoiceWinShim.GetLiveAudioLevel());
            RenderPill(false);
        };
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams {
        get { CreateParams cp = base.CreateParams; cp.ExStyle |= 0x00080000 | 0x00000020 | 0x08000000 | 0x00000080 | 0x00000008; return cp; }
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x0021) { m.Result = new IntPtr(3); return; }
        base.WndProc(ref m);
    }
    public void HideNoActivate() {
        _hideTimer.Stop(); _animTimer.Stop(); _isWaveRecording = false; _shown = false;
        _maxDurationSec = 0; _warnRemainSec = 0; _bgCount = 0;
        if (IsHandleCreated) ShowWindow(Handle, 0);
    }
    public void ShowStatus(string text, Color bgColor, Color iconColor, Color mainColor, Color hintColor, Color border, int autoHideMs, IntPtr fgHwnd, string position, bool waveMode = false, string iconKind = "", int maxDurationSec = 0, int warnRemainSec = 0, Rectangle? workAreaOverride = null, int dpiOverride = 0, int bgCount = 0, bool headlessNoShow = false) {
        _hideTimer.Stop();
        _text = text ?? ""; _bgColor = bgColor; _iconColor = iconColor; _mainColor = mainColor; _hintColor = hintColor; _borderColor = border;
        _iconKind = iconKind ?? ""; _fgHwnd = fgHwnd; _position = position ?? "bottom";
        _bgCount = (waveMode && !_isToast && bgCount > 0) ? bgCount : 0;
        _lastAutoHideMs = (autoHideMs > 0) ? autoHideMs : 0;
        _showUpdates++;
        int dpi = (dpiOverride > 0) ? dpiOverride : VoiceWinShim.GetTargetDpi(fgHwnd);
        _anchoredWorkArea = (workAreaOverride.HasValue && workAreaOverride.Value.Width > 0 && workAreaOverride.Value.Height > 0)
            ? workAreaOverride.Value
            : VoiceWinShim.GetTargetWorkArea(fgHwnd);
        EnsureFontsFor(HasCjk(_text), dpi);
        if (waveMode && !_isToast) {
            _maxDurationSec = (maxDurationSec > 0) ? maxDurationSec : 0;
            _warnRemainSec = (_maxDurationSec > 0) ? ((warnRemainSec > 0) ? warnRemainSec : VoiceWinShim.RecordingCountdownWarnSec(_maxDurationSec)) : 0;
            if (!_isWaveRecording) {
                _isWaveRecording = true;
                _recStartTick = (long)VoiceWinShim.GetTickCount64();
                ResetWaveEnvelope();
                float initLvl = VoiceWinShim.GetLiveAudioLevel();
                if (initLvl > 0f) StepWaveEnvelope(initLvl);
            }
            if (headlessNoShow) {
                int dx, dy; HudLayoutMetrics dm;
                using (Bitmap tmp = BuildPillPArgbBitmap(_anchoredWorkArea, out dx, out dy, out dm)) { _shown = true; }
            } else {
                RenderPill(true);
                if (!_animTimer.Enabled) _animTimer.Start();
            }
        } else {
            _animTimer.Stop();
            _isWaveRecording = false;
            _maxDurationSec = 0; _warnRemainSec = 0;
            if (headlessNoShow) {
                int dx, dy; HudLayoutMetrics dm;
                using (Bitmap tmp = BuildPillPArgbBitmap(_anchoredWorkArea, out dx, out dy, out dm)) { _shown = true; }
            } else {
                RenderPill(true);
            }
        }
        if (!headlessNoShow && autoHideMs > 0) { _hideTimer.Interval = autoHideMs; _hideTimer.Start(); }
    }

    public void SyncAnchor(Rectangle workArea, int dpi, string position, bool headlessNoShow = false) {
        if (!_shown || IsDisposed || workArea.Width <= 0 || workArea.Height <= 0) return;
        _anchoredWorkArea = workArea;
        if (!string.IsNullOrEmpty(position)) _position = position;
        if (dpi > 0 && dpi != _dpi) {
            EnsureFontsFor(_cjkFont, dpi);
        }
        if (headlessNoShow) {
            int dx, dy; HudLayoutMetrics dm;
            using (Bitmap tmp = BuildPillPArgbBitmap(_anchoredWorkArea, out dx, out dy, out dm)) { }
        } else {
            RenderPill(false);
        }
    }

    public Bitmap RenderOffscreenBitmap(string text, Color bgColor, Color iconColor, Color mainColor, Color hintColor, Color border, int dpi, Rectangle workArea, bool waveMode, string iconKind, float[] barNormsOverride, long elapsedSec, int maxDurationSec, int warnRemainSec, string position = "bottom", int bgCount = 0) {
        HudLayoutMetrics unused;
        return RenderOffscreenBitmapWithMetrics(text, bgColor, iconColor, mainColor, hintColor, border, dpi, workArea, waveMode, iconKind, barNormsOverride, elapsedSec, maxDurationSec, warnRemainSec, out unused, position, bgCount);
    }

    public Bitmap RenderOffscreenBitmapWithMetrics(string text, Color bgColor, Color iconColor, Color mainColor, Color hintColor, Color border, int dpi, Rectangle workArea, bool waveMode, string iconKind, float[] barNormsOverride, long elapsedSec, int maxDurationSec, int warnRemainSec, out HudLayoutMetrics layout, string position = "bottom", int bgCount = 0) {
        _text = text ?? ""; _bgColor = bgColor; _iconColor = iconColor; _mainColor = mainColor; _hintColor = hintColor; _borderColor = border;
        _iconKind = iconKind ?? ""; _position = position ?? "bottom"; _isWaveRecording = waveMode && !_isToast;
        _maxDurationSec = (_isWaveRecording && maxDurationSec > 0) ? maxDurationSec : 0;
        _warnRemainSec = (_maxDurationSec > 0) ? ((warnRemainSec > 0) ? warnRemainSec : VoiceWinShim.RecordingCountdownWarnSec(_maxDurationSec)) : 0;
        _bgCount = (_isWaveRecording && bgCount > 0) ? bgCount : 0;
        _elapsedOverrideSec = elapsedSec;
        Array.Clear(_barNorms, 0, _barNorms.Length);
        if (barNormsOverride != null) {
            int n = Math.Min(9, barNormsOverride.Length);
            for (int i = 0; i < n; i++) _barNorms[i] = barNormsOverride[i];
        }
        int clampedDpi = (dpi < 96) ? 96 : ((dpi > 480) ? 480 : dpi);
        EnsureFontsFor(HasCjk(_text), clampedDpi);
        int dummyX, dummyY;
        return BuildPillPArgbBitmap(workArea, out dummyX, out dummyY, out layout);
    }

    private Bitmap BuildPillPArgbBitmap(Rectangle wa, out int x, out int y, out HudLayoutMetrics layout) {
        string iconSeg, mainSeg, hintSeg; bool spBeforeHint;
        VoiceWinShim.SplitHudSegments(_text, out iconSeg, out mainSeg, out hintSeg, out spBeforeHint);
        int padL = _isToast ? ScalePx(12) : ScalePx(15);
        int padR = _isToast ? ScalePx(12) : ScalePx(15);
        int minW = _isToast ? ScalePx(56) : ScalePx(64);
        int maxW = Math.Max(minW, Math.Min(ScalePx(680), wa.Width - ScalePx(24)));

        float dpiScale = _dpi / 96.0f;
        int textDy = _cjkFont ? -Math.Max(1, (int)Math.Round(0.5f * dpiScale)) : -ScalePx(1);
        float barW = 2.5f * dpiScale, barGap = 2.5f * dpiScale;
        int waveBoxW = (int)Math.Ceiling(9.0f * barW + 8.0f * barGap);
        string durStr = "", effHint = hintSeg, badgeStr = "";
        int slotW = 0, hintSlotW = 0, badgeSlotW = 0, badgeH = 0, iconBox = 0;
        int gap1 = 0, gap2 = 0, badgeGap = 0, iconGap = 0, hintGap = 0, totalTextW = 0, textH = 0;
        int durMeasuredW = 0, hintMeasuredW = 0, badgeMeasuredW = 0;
        bool isWarn = false;
        Size mainSz = Size.Empty, hintSz = Size.Empty, badgeTextSz = Size.Empty;
        Color effBg = _bgColor, effBorder = _borderColor, effMain = _mainColor, effHintCol = _hintColor;
        Color effWaveActive = RecWaveActiveColor, effWaveQuiet = RecWaveQuietColor;

        if (_isWaveRecording) {
            long elapsedSec = GetEffectiveElapsedSec();
            long remainSec = 0L;
            if (_maxDurationSec > 0 && _warnRemainSec > 0) {
                remainSec = Math.Max(0L, (long)_maxDurationSec - elapsedSec);
                isWarn = (remainSec <= (long)_warnRemainSec);
            }
            bool zhHint = (_text.Length > 0) ? HasCjk(_text) : _cjkFont;
            if (isWarn) {
                effBg = WarnBgColor;
                effBorder = WarnBorderColor;
                effWaveActive = WarnWaveActiveColor;
                effWaveQuiet = WarnWaveQuietColor;
                effMain = WarnMainColor;
                effHintCol = WarnHintColor;
                effHint = VoiceWinShim.FormatCountdownHint((int)remainSec, zhHint);
            } else {
                effBg = RecBgColor;
                effBorder = RecBorderColor;
                effWaveActive = RecWaveActiveColor;
                effWaveQuiet = RecWaveQuietColor;
                effMain = RecMainColor;
                effHintCol = RecHintColor;
            }
            effHint = VoiceWinShim.StripOuterParens(effHint);
            durStr = VoiceWinShim.FormatRecordingDuration(elapsedSec);
            string slotTpl = VoiceWinShim.DurationSlotTemplate(elapsedSec);
            Size actDurSz = TextRenderer.MeasureText(durStr, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
            Size tplDurSz = TextRenderer.MeasureText(slotTpl, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
            durMeasuredW = actDurSz.Width;
            slotW = Math.Max(actDurSz.Width, tplDurSz.Width);
            if (effHint.Length > 0) {
                hintSz = TextRenderer.MeasureText(effHint, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
                hintMeasuredW = hintSz.Width;
                if (isWarn) {
                    string hintTpl = VoiceWinShim.StripOuterParens(VoiceWinShim.CountdownHintSlotTemplate((int)remainSec, zhHint));
                    Size tplHintSz = TextRenderer.MeasureText(hintTpl, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
                    hintSlotW = Math.Max(hintSz.Width, tplHintSz.Width);
                } else {
                    hintSlotW = hintSz.Width;
                }
            }
            if (_bgCount > 0) {
                badgeStr = "+" + _bgCount.ToString(CultureInfo.InvariantCulture);
                Font bf = _badgeFont ?? _font;
                badgeTextSz = TextRenderer.MeasureText(badgeStr, bf, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
                badgeMeasuredW = badgeTextSz.Width;
                int badgePadX = ScalePx(5);
                badgeSlotW = Math.Max(ScalePx(20), badgeTextSz.Width + badgePadX * 2);
                badgeH = Math.Max(ScalePx(18), badgeTextSz.Height + ScalePx(4));
            }
            gap1 = ScalePx(9);
            gap2 = (effHint.Length > 0) ? ScalePx(9) : 0;
            badgeGap = (badgeSlotW > 0) ? ScalePx((effHint.Length > 0) ? 7 : 9) : 0;
            totalTextW = slotW + gap1 + waveBoxW + gap2 + hintSlotW + badgeGap + badgeSlotW;
            textH = Math.Max(Math.Max(actDurSz.Height, hintSz.Height), badgeH);
        } else {
            iconBox = (iconSeg.Length > 0 || (_isToast && !string.IsNullOrEmpty(_iconKind))) ? ScalePx(11) : 0;
            mainSz = (mainSeg.Length > 0) ? TextRenderer.MeasureText(mainSeg, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags) : Size.Empty;
            hintSz = (hintSeg.Length > 0) ? TextRenderer.MeasureText(hintSeg, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags) : Size.Empty;
            iconGap = (iconBox > 0 && (mainSeg.Length > 0 || hintSeg.Length > 0)) ? ScalePx(6) : 0;
            hintGap = (hintSeg.Length > 0 && mainSeg.Length > 0) ? ScalePx(spBeforeHint ? 4 : 2) : 0;
            totalTextW = iconBox + iconGap + mainSz.Width + hintGap + hintSz.Width;
            textH = Math.Max(iconBox, Math.Max(mainSz.Height, hintSz.Height));
        }
        if (textH <= 0) textH = TextRenderer.MeasureText("Ag", _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags).Height;

        int rawW = Math.Max(minW, totalTextW + padL + padR);
        bool truncated = rawW > maxW;
        int w = Math.Min(maxW, rawW);
        int h = _isToast ? Math.Max(ScalePx(30), textH + ScalePx(10)) : Math.Max(ScalePx(34), textH + ScalePx(14));
        bool isTop = string.Equals((_position ?? "").Trim(), "top", StringComparison.OrdinalIgnoreCase);
        x = wa.Left + (wa.Width - w) / 2;
        if (_isToast) {
            int mainSlotH = VoiceWinShim.GetMainSlotHeight(ScalePx(34));
            y = isTop
                ? (wa.Top + ScalePx(36) + mainSlotH + ScalePx(8))
                : Math.Max(wa.Top + ScalePx(4), wa.Bottom - ScalePx(8) - mainSlotH - ScalePx(8) - h);
        } else {
            y = isTop ? (wa.Top + ScalePx(36)) : Math.Max(wa.Top + ScalePx(4), wa.Bottom - h - ScalePx(8));
        }

        layout = new HudLayoutMetrics {
            Width = w, Height = h, MaxWidth = maxW, PadL = padL, PadR = padR, Dpi = _dpi,
            IsToast = _isToast, IsWaveRecording = _isWaveRecording, Truncated = truncated, IsCountdownWarning = isWarn,
            BgCount = _bgCount,
            DurMeasuredW = durMeasuredW, DurSlotW = slotW,
            HintMeasuredW = hintMeasuredW, HintSlotW = hintSlotW,
            BadgeMeasuredW = badgeMeasuredW, BadgeSlotW = badgeSlotW,
            MainMeasuredW = mainSz.Width, HintSegMeasuredW = _isWaveRecording ? 0 : hintSz.Width,
            DurationText = durStr, HintText = effHint, BadgeText = badgeStr, MainText = mainSeg
        };

        Bitmap outBmp = new Bitmap(w, h, PixelFormat.Format32bppPArgb);
        using (Bitmap rgbBmp = new Bitmap(w, h, PixelFormat.Format32bppRgb))
        using (Bitmap maskBmp = new Bitmap(w, h, PixelFormat.Format32bppArgb)) {
            using (Graphics g = Graphics.FromImage(rgbBmp)) {
                g.Clear(effBg);
                int availW = Math.Max(1, w - padL - padR);
                int bleed = ScalePx(2);
                if (_isWaveRecording) {
                    int curX = padL + (truncated ? 0 : Math.Max(0, (availW - totalTextW) / 2));
                    layout.DurRect = new Rectangle(curX, 0, slotW, h);
                    TextRenderer.DrawText(g, durStr, _font, new Rectangle(curX - bleed, textDy, slotW + bleed * 2, h), effMain, DurDrawFlags);
                    curX += slotW + gap1;
                    layout.WaveRect = new Rectangle(curX, 0, waveBoxW, h);
                    g.SmoothingMode = SmoothingMode.AntiAlias;
                    g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    float maxBarH = Math.Min(h - 8.0f * dpiScale, 15.0f * dpiScale);
                    if (maxBarH < barW) maxBarH = barW;
                    using (SolidBrush quietBrush = new SolidBrush(effWaveQuiet))
                    using (SolidBrush activeBrush = new SolidBrush(effWaveActive)) {
                        for (int i = 0; i < 9; i++) {
                            float norm = _barNorms[i];
                            float bh = barW + norm * (maxBarH - barW);
                            float bx = curX + i * (barW + barGap);
                            float by = (h - bh) * 0.5f;
                            using (GraphicsPath bp = BuildVerticalBarPath(bx, by, barW, bh)) {
                                g.FillPath((norm > 0.01f) ? activeBrush : quietBrush, bp);
                            }
                        }
                    }
                    curX += waveBoxW + gap2;
                    int reservedBadgeW = (badgeSlotW > 0) ? (badgeGap + badgeSlotW) : 0;
                    if (effHint.Length > 0) {
                        int remW = Math.Max(0, w - padR - reservedBadgeW - curX);
                        int drawHintW = truncated ? remW : hintSlotW;
                        if (drawHintW > 0) {
                            layout.HintRect = new Rectangle(curX, 0, drawHintW, h);
                            if (truncated) {
                                TextRenderer.DrawText(g, effHint, _font, new Rectangle(curX, textDy, drawHintW, h), effHintCol, SegDrawFlags | TextFormatFlags.EndEllipsis);
                            } else {
                                TextRenderer.DrawText(g, effHint, _font, new Rectangle(curX, textDy, hintSlotW + bleed, h), effHintCol, SegDrawFlags);
                            }
                            curX += drawHintW;
                        }
                    }
                    if (badgeSlotW > 0) {
                        curX += badgeGap;
                        if (curX + badgeSlotW <= w - padR + 1) {
                            int bH = Math.Min(h - ScalePx(6), Math.Max(ScalePx(16), badgeH));
                            int bY = (h - bH) / 2;
                            Rectangle bRc = new Rectangle(curX, bY, badgeSlotW, bH);
                            layout.BadgeRect = bRc;
                            g.SmoothingMode = SmoothingMode.AntiAlias;
                            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                            using (GraphicsPath bPath = BuildCapsulePath(bRc.X + 0.5f, bRc.Y + 0.5f, Math.Max(2f, bRc.Width - 1.0f), Math.Max(2f, bRc.Height - 1.0f)))
                            using (SolidBrush bFill = new SolidBrush(BadgeBgColor))
                            using (Pen bPen = new Pen(BadgeBorderColor, Math.Max(1.0f, 0.9f * dpiScale))) {
                                g.FillPath(bFill, bPath);
                                g.DrawPath(bPen, bPath);
                            }
                            Font bf = _badgeFont ?? _font;
                            int badgeDy = -Math.Max(1, (int)Math.Round(0.5f * dpiScale));
                            TextRenderer.DrawText(g, badgeStr, bf, new Rectangle(bRc.X - 1, bRc.Y + badgeDy, bRc.Width + 2, bRc.Height), BadgeTextColor, DurDrawFlags);
                        }
                    }
                } else if (truncated) {
                    if (iconBox > 0) {
                        int iy = (int)Math.Round((h - iconBox) * 0.5f);
                        layout.IconRect = new Rectangle(padL, iy, iconBox, iconBox);
                        DrawStatusIcon(g, ResolveIconKind(_iconKind, iconSeg), _iconColor, (float)padL, (h - iconBox) * 0.5f, (float)iconBox, dpiScale);
                        int tx = padL + iconBox + iconGap;
                        string restTxt = mainSeg + ((hintSeg.Length > 0) ? ((spBeforeHint ? " " : "") + hintSeg) : "");
                        int remTxtW = w - padR - tx;
                        if (remTxtW > 0 && restTxt.Length > 0) {
                            layout.MainTextRect = new Rectangle(tx, 0, remTxtW, h);
                            TextRenderer.DrawText(g, restTxt, _font, new Rectangle(tx, textDy, remTxtW, h), effMain, SegDrawFlags | TextFormatFlags.EndEllipsis);
                        }
                    } else {
                        layout.MainTextRect = new Rectangle(padL, 0, availW, h);
                        TextRenderer.DrawText(g, _text, _font, new Rectangle(padL, textDy, availW, h), effMain, TruncDrawFlags);
                    }
                } else {
                    int curX = padL + Math.Max(0, (availW - totalTextW) / 2);
                    if (iconBox > 0) {
                        int iy = (int)Math.Round((h - iconBox) * 0.5f);
                        layout.IconRect = new Rectangle(curX, iy, iconBox, iconBox);
                        DrawStatusIcon(g, ResolveIconKind(_iconKind, iconSeg), _iconColor, (float)curX, (h - iconBox) * 0.5f, (float)iconBox, dpiScale);
                        curX += iconBox + iconGap;
                    }
                    int textStartX = curX;
                    int textTotalSpan = 0;
                    if (mainSeg.Length > 0) {
                        TextRenderer.DrawText(g, mainSeg, _font, new Rectangle(curX, textDy, mainSz.Width + bleed, h), effMain, SegDrawFlags);
                        curX += mainSz.Width + hintGap;
                        textTotalSpan += mainSz.Width;
                    }
                    if (hintSeg.Length > 0) {
                        TextRenderer.DrawText(g, hintSeg, _font, new Rectangle(curX, textDy, hintSz.Width + bleed, h), effHintCol, SegDrawFlags);
                        textTotalSpan += ((mainSeg.Length > 0) ? hintGap : 0) + hintSz.Width;
                    }
                    if (textTotalSpan > 0) {
                        layout.MainTextRect = new Rectangle(textStartX, 0, textTotalSpan, h);
                    }
                }
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                float penW = _isToast ? Math.Max(1.0f, _dpi / 96.0f) : Math.Max(1.0f, 1.1f * (_dpi / 96.0f));
                float inset = penW * 0.5f + 0.5f;
                using (GraphicsPath borderPath = BuildCapsulePath(inset, inset, Math.Max(4f, w - inset * 2.0f), Math.Max(4f, h - inset * 2.0f)))
                using (Pen borderPen = new Pen(effBorder, penW)) {
                    g.DrawPath(borderPen, borderPath);
                }
            }
            using (Graphics gm = Graphics.FromImage(maskBmp)) {
                gm.Clear(Color.FromArgb(0, 0, 0, 0));
                gm.SmoothingMode = SmoothingMode.AntiAlias;
                gm.PixelOffsetMode = PixelOffsetMode.HighQuality;
                using (GraphicsPath maskPath = BuildCapsulePath(0.5f, 0.5f, Math.Max(4f, w - 1.0f), Math.Max(4f, h - 1.0f)))
                using (SolidBrush maskBrush = new SolidBrush(Color.FromArgb(242, 255, 255, 255))) {
                    gm.FillPath(maskBrush, maskPath);
                }
            }
            Rectangle fullRc = new Rectangle(0, 0, w, h);
            BitmapData rgbData = null, maskData = null, outData = null;
            try {
                rgbData = rgbBmp.LockBits(fullRc, ImageLockMode.ReadOnly, PixelFormat.Format32bppRgb);
                maskData = maskBmp.LockBits(fullRc, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
                outData = outBmp.LockBits(fullRc, ImageLockMode.WriteOnly, PixelFormat.Format32bppPArgb);
                int byteLen = outData.Stride * h;
                byte[] rgbBytes = new byte[byteLen];
                byte[] maskBytes = new byte[byteLen];
                byte[] outBytes = new byte[byteLen];
                Marshal.Copy(rgbData.Scan0, rgbBytes, 0, byteLen);
                Marshal.Copy(maskData.Scan0, maskBytes, 0, byteLen);
                for (int idx = 0; idx < byteLen; idx += 4) {
                    int a = maskBytes[idx + 3];
                    if (a != 0) {
                        outBytes[idx]     = (byte)((rgbBytes[idx]     * a + 127) / 255);
                        outBytes[idx + 1] = (byte)((rgbBytes[idx + 1] * a + 127) / 255);
                        outBytes[idx + 2] = (byte)((rgbBytes[idx + 2] * a + 127) / 255);
                        outBytes[idx + 3] = (byte)a;
                    }
                }
                Marshal.Copy(outBytes, 0, outData.Scan0, byteLen);
            } finally {
                if (outData != null) outBmp.UnlockBits(outData);
                if (maskData != null) maskBmp.UnlockBits(maskData);
                if (rgbData != null) rgbBmp.UnlockBits(rgbData);
            }
        }
        _lastX = x; _lastY = y; _lastW = w; _lastH = h;
        return outBmp;
    }

    private void RenderPill(bool forceTopMost) {
        Rectangle wa = (_anchoredWorkArea.Width > 0 && _anchoredWorkArea.Height > 0)
            ? _anchoredWorkArea
            : VoiceWinShim.GetTargetWorkArea(_fgHwnd);
        int prevX = _lastX, prevY = _lastY, prevW = _lastW, prevH = _lastH;
        int x, y;
        HudLayoutMetrics unusedLayout;
        IntPtr hdl = Handle;
        using (Bitmap outBmp = BuildPillPArgbBitmap(wa, out x, out y, out unusedLayout)) {
            int w = outBmp.Width, h = outBmp.Height;
            IntPtr screenDc = IntPtr.Zero, memDc = IntPtr.Zero, hBmp = IntPtr.Zero, oldBmp = IntPtr.Zero;
            try {
                screenDc = GetDC(IntPtr.Zero);
                memDc = CreateCompatibleDC(screenDc);
                hBmp = outBmp.GetHbitmap(Color.FromArgb(0, 0, 0, 0));
                oldBmp = SelectObject(memDc, hBmp);
                WinPoint ptDst = new WinPoint(x, y);
                WinSize szWnd = new WinSize(w, h);
                WinPoint ptSrc = new WinPoint(0, 0);
                BlendFunc blend = new BlendFunc { BlendOp = 0, BlendFlags = 0, SourceConstantAlpha = 255, AlphaFormat = 1 };
                UpdateLayeredWindow(hdl, screenDc, ref ptDst, ref szWnd, memDc, ref ptSrc, 0, ref blend, 2);
            } finally {
                if (memDc != IntPtr.Zero && oldBmp != IntPtr.Zero) SelectObject(memDc, oldBmp);
                if (hBmp != IntPtr.Zero) DeleteObject(hBmp);
                if (memDc != IntPtr.Zero) DeleteDC(memDc);
                if (screenDc != IntPtr.Zero) ReleaseDC(IntPtr.Zero, screenDc);
            }
            if (forceTopMost || !_shown || x != prevX || y != prevY || w != prevW || h != prevH) {
                SetWindowPos(hdl, new IntPtr(-1), x, y, w, h, 0x0010 | 0x0040);
                _shown = true;
            }
        }
    }
    protected override void Dispose(bool disposing) {
        if (disposing) {
            _hideTimer.Dispose();
            _animTimer.Dispose();
            if (_ownsFont && _font != null) _font.Dispose();
            if (_ownsBadgeFont && _badgeFont != null) _badgeFont.Dispose();
        }
        base.Dispose(disposing);
    }
}

public static class VoiceWinShim {
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

    public static readonly ConcurrentQueue<string> CmdQueue = new ConcurrentQueue<string>();
    public static readonly ConcurrentQueue<ShimEvent> EvtQueue = new ConcurrentQueue<ShimEvent>();
    public static volatile bool StdinClosed = false;
    private static IntPtr _hook = IntPtr.Zero;
    private static LowLevelKeyboardProc _procRef;
    private static HudPillForm _hud;
    private static HudPillForm _toastHud;
    private static bool _headlessHudMode = false;
    private static Rectangle _headlessDefaultWa = Rectangle.Empty;
    private static int _headlessDefaultDpi = 96;
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

    public static int GetMainSlotHeight(int fallbackPx) {
        if (_hud != null && !_hud.IsDisposed && _hud.IsShown && _hud.Visible) {
            int hh = (_hud.LastHeight > 0) ? _hud.LastHeight : _hud.Height;
            if (hh > 0) return hh;
        }
        return fallbackPx;
    }

    public static string StripLeadingStatusSymbol(string raw, out string symbol) {
        symbol = "";
        string s = (raw ?? "").Trim();
        if (s.Length == 0) return "";
        char c0 = s[0];
        bool isKnownSym = (
            c0 == '\u25CF' || c0 == '\u25D0' || c0 == '\u25C8' || c0 == '\u2713' ||
            c0 == '\u25CB' || c0 == '\u2715' || c0 == '\u2716' || c0 == '!' ||
            c0 == '\uFF01' || c0 == '\u26A0'
        );
        if (isKnownSym) {
            int idx = 1;
            if (idx < s.Length && s[idx] == '\uFE0F') idx++;
            if (idx < s.Length && char.IsWhiteSpace(s[idx])) {
                symbol = s.Substring(0, idx);
                while (idx < s.Length && char.IsWhiteSpace(s[idx])) idx++;
                return s.Substring(idx);
            }
            if (c0 == '!' || c0 == '\uFF01' || c0 == '\u26A0') {
                symbol = s.Substring(0, idx);
                return s.Substring(idx).TrimStart();
            }
        }
        int spIdx = s.IndexOf(' ');
        if (spIdx >= 1 && spIdx <= 3 && spIdx + 1 < s.Length) {
            bool hasAlphaNum = false;
            for (int i = 0; i < spIdx; i++) {
                if (char.IsLetterOrDigit(s[i])) { hasAlphaNum = true; break; }
            }
            if (!hasAlphaNum) {
                symbol = s.Substring(0, spIdx);
                int afterIdx = spIdx + 1;
                while (afterIdx < s.Length && s[afterIdx] == ' ') afterIdx++;
                return s.Substring(afterIdx);
            }
        }
        return s;
    }

    public static void SplitHudSegments(string raw, out string icon, out string main, out string hint, out bool spaceBeforeHint) {
        icon = ""; main = ""; hint = ""; spaceBeforeHint = false;
        string s = (raw ?? "").Trim();
        if (s.Length == 0) return;
        string rest = StripLeadingStatusSymbol(s, out icon);
        if (rest.EndsWith(")")) {
            int openIdx = rest.LastIndexOf('(');
            if (openIdx > 0) {
                int trimEnd = openIdx;
                while (trimEnd > 0 && rest[trimEnd - 1] == ' ') trimEnd--;
                if (trimEnd > 0) {
                    main = rest.Substring(0, trimEnd);
                    spaceBeforeHint = (trimEnd < openIdx);
                    hint = rest.Substring(openIdx);
                    return;
                }
            }
        }
        main = rest;
    }

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

    public static string FormatRecordingDuration(long elapsedSec) {
        long sec = (elapsedSec < 0L) ? 0L : elapsedSec;
        if (sec < 60L) return sec.ToString(CultureInfo.InvariantCulture) + "s";
        long m = sec / 60L, s = sec % 60L;
        return m.ToString(CultureInfo.InvariantCulture) + "m " + s.ToString("00", CultureInfo.InvariantCulture) + "s";
    }

    public static string DurationSlotTemplate(long elapsedSec) {
        if (elapsedSec < 10L) return "8s";
        if (elapsedSec < 60L) return "88s";
        if (elapsedSec < 600L) return "8m 88s";
        return "88m 88s";
    }

    public static int RecordingCountdownWarnSec(int maxDurationSec) {
        if (maxDurationSec <= 0) return 0;
        if (maxDurationSec >= 45) return 15;
        int third = maxDurationSec / 3;
        return Math.Max(1, Math.Min(10, (third > 0) ? third : 1));
    }

    public static string StripOuterParens(string s) {
        if (string.IsNullOrEmpty(s)) return "";
        string t = s.Trim();
        if (t.Length >= 2 && t[0] == '(' && t[t.Length - 1] == ')') {
            return t.Substring(1, t.Length - 2).Trim();
        }
        return t;
    }

    public static string FormatCountdownHint(int remainSec, bool zh) {
        int r = (remainSec < 0) ? 0 : remainSec;
        string rs = r.ToString(CultureInfo.InvariantCulture);
        return zh ? ("(" + rs + "s \u540E\u81EA\u52A8\u5B8C\u6210)") : ("(auto-finish in " + rs + "s)");
    }

    public static string CountdownHintSlotTemplate(int remainSec, bool zh) {
        string tpl = (remainSec >= 10) ? "88" : "8";
        return zh ? ("(" + tpl + "s \u540E\u81EA\u52A8\u5B8C\u6210)") : ("(auto-finish in " + tpl + "s)");
    }

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

    public static string ResolveHudLayer(string st, string explicitLayer) {
        if (st == "" || st == "idle" || st == "hide" || st == "hide_toast" || st == "hide_all") return "control";
        string lyr = (explicitLayer ?? "").Trim().ToLowerInvariant();
        if (lyr == "main" || lyr == "toast") return lyr;
        if (st == "recording" || st == "recognizing" || st == "processing" || st == "refining" || st == "done" || st == "copied") return "main";
        return "toast";
    }

    private static void ResolveHudVisualStyle(string st, bool zh, string customText, bool isToast, out string txt, out Color bg, out Color ic, out Color mc, out Color hc, out Color bd, out int ms, out bool waveMode, out string iconKind) {
        ms = 0; waveMode = false; iconKind = "";
        Color toastBg = Color.FromArgb(30, 31, 36);
        Color toastMain = Color.FromArgb(244, 245, 248);
        Color toastHint = Color.FromArgb(190, 195, 208);
        Color toastAmber = Color.FromArgb(251, 191, 36);
        Color toastCoral = Color.FromArgb(248, 113, 113);
        if (st == "recording") {
            string hkEn = (_hotkeyLabel == "\u53F3 Ctrl") ? "Right Ctrl" : _hotkeyLabel.Trim();
            txt = zh
                ? ("\u25CF \u5F55\u97F3\u4E2D\u2026 (" + (_isPtt ? "\u677E\u5F00" : "\u6309") + _hotkeyLabel + " \u7ED3\u675F)")
                : ("\u25CF Listening\u2026 (" + (_isPtt ? "release " : "press ") + hkEn + " to end)");
            bg = Color.FromArgb(64, 18, 26); ic = Color.FromArgb(255, 104, 112); mc = Color.FromArgb(248, 250, 252); hc = Color.FromArgb(230, 188, 178, 192); bd = Color.FromArgb(196, 66, 80);
            string trimmedCustom = (customText ?? "").TrimStart();
            waveMode = !isToast && (string.IsNullOrEmpty(trimmedCustom) || trimmedCustom.StartsWith("\u25CF ", StringComparison.Ordinal));
        } else if (st == "recognizing" || st == "processing") {
            txt = zh ? "\u25D0 \u8BED\u97F3\u8BC6\u522B\u2026" : "\u25D0 Transcribing\u2026";
            bg = Color.FromArgb(16, 40, 72); ic = Color.FromArgb(56, 189, 248); mc = Color.FromArgb(244, 248, 252); hc = Color.FromArgb(176, 198, 222); bd = Color.FromArgb(52, 148, 212); iconKind = "recognizing";
        } else if (st == "refining") {
            txt = zh ? "\u25C8 \u6574\u7406\u7CBE\u4FEE\u2026" : "\u25C8 Refining\u2026";
            bg = Color.FromArgb(46, 22, 76); ic = Color.FromArgb(208, 156, 255); mc = Color.FromArgb(248, 245, 255); hc = Color.FromArgb(198, 182, 226); bd = Color.FromArgb(156, 88, 224); iconKind = "refining";
        } else if (st == "done") {
            txt = zh ? "\u2713 \u5DF2\u586B\u5165" : "\u2713 Inserted";
            bg = Color.FromArgb(14, 54, 36); ic = Color.FromArgb(74, 222, 128); mc = Color.FromArgb(242, 253, 246); hc = Color.FromArgb(174, 218, 192); bd = Color.FromArgb(48, 168, 102); ms = 900; iconKind = "done";
        } else if (st == "copied") {
            txt = zh ? "\u2713 \u5DF2\u590D\u5236(\u7A97\u53E3\u5DF2\u5207\u6362)" : "\u2713 Copied (window switched)";
            bg = Color.FromArgb(28, 32, 82); ic = Color.FromArgb(144, 156, 255); mc = Color.FromArgb(240, 244, 255); hc = Color.FromArgb(186, 196, 238); bd = Color.FromArgb(108, 116, 242); ms = 2000; iconKind = "done";
        } else if (st == "empty") {
            txt = zh ? "\u25CB \u672A\u68C0\u6D4B\u5230\u8BED\u97F3" : "\u25CB No speech detected";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmber; ms = 2200; iconKind = "empty_ring";
        } else if (st == "cancelled") {
            txt = zh ? "\u25CB \u5DF2\u53D6\u6D88" : "\u25CB Cancelled";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmber; ms = 1600; iconKind = "empty_ring";
        } else if (st == "mic_unavailable") {
            txt = zh ? "! \u9EA6\u514B\u98CE\u6682\u5FD9\uFF0C\u8BF7\u518D\u8BD5\u4E00\u6B21" : "! Microphone busy, try again";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmber; ms = 2600; iconKind = "warning";
        } else if (st == "mic_no_stream") {
            txt = zh ? "! \u9EA6\u514B\u98CE\u65E0\u97F3\u9891\u6D41\uFF0C\u8BF7\u68C0\u67E5\u8033\u673A/\u8BBE\u5907" : "! No mic audio stream, check headset";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmber; ms = 2600; iconKind = "warning";
        } else if (st == "audio_recovered") {
            txt = zh ? "! \u9EA6\u514B\u98CE\u77ED\u6682\u4E2D\u65AD\uFF0C\u5DF2\u81EA\u52A8\u6062\u590D" : "! Mic briefly interrupted, resumed";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmber; ms = 2400; iconKind = "warning";
        } else if (st == "no_mic") {
            txt = zh ? "\u2715 \u65E0\u53EF\u7528\u9EA6\u514B\u98CE" : "\u2715 No microphone found";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoral; ms = 2200; iconKind = "cross";
        } else if (st == "mic_permission") {
            txt = zh ? "\u2715 \u9EA6\u514B\u98CE\u6743\u9650\u672A\u5F00\u542F" : "\u2715 Microphone access denied";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoral; ms = 3200; iconKind = "cross";
        } else if (st == "mic_disconnected") {
            txt = zh ? "\u2715 \u9EA6\u514B\u98CE\u5DF2\u65AD\u5F00" : "\u2715 Microphone disconnected";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoral; ms = 2200; iconKind = "cross";
        } else if (st == "paste_failed") {
            txt = zh ? "\u2715 \u586B\u5165\u5931\u8D25" : "\u2715 Failed to insert text";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoral; ms = 2600; iconKind = "cross";
        } else {
            txt = zh ? "\u2715 \u8BC6\u522B\u5931\u8D25" : "\u2715 Transcription failed";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoral; ms = 2600; iconKind = "cross";
        }
        if (isToast && bg != toastBg) {
            bg = toastBg; mc = toastMain; hc = toastHint;
        }
        if (!string.IsNullOrEmpty(customText)) txt = customText;
    }

    public static Bitmap RenderHudSnapshotBitmap(string state, string customText) {
        return RenderHudSnapshotBitmap(state, customText, 192, 1920, 1080);
    }

    public static Bitmap RenderHudSnapshotBitmap(string state, string customText, int dpi, int workWidth, int workHeight) {
        string st = (state ?? "error").Trim().ToLowerInvariant();
        bool autoToast = (ResolveHudLayer(st, "") == "toast");
        return RenderHudSnapshotBitmap(state, customText, dpi, workWidth, workHeight, autoToast);
    }

    public static Bitmap RenderHudSnapshotBitmap(string state, string customText, int dpi, int workWidth, int workHeight, bool isToast) {
        return RenderHudSnapshotBitmap(state, customText, dpi, workWidth, workHeight, null, 0L, 0, 0, true, isToast);
    }

    public static Bitmap RenderHudSnapshotBitmap(string state, string customText, int dpi, int workWidth, int workHeight, float[] barNorms, long elapsedSec = 0L, int maxDurationSec = 0, int warnRemainSec = 0, bool zh = true, bool isToast = false, int bgCount = 0) {
        HudLayoutMetrics unused;
        return RenderHudSnapshotWithMetrics(state, customText, dpi, workWidth, workHeight, barNorms, out unused, elapsedSec, maxDurationSec, warnRemainSec, zh, isToast, bgCount);
    }

    public static Bitmap RenderHudSnapshotWithMetrics(string state, string customText, int dpi, int workWidth, int workHeight, float[] barNorms, out HudLayoutMetrics layout, long elapsedSec = 0L, int maxDurationSec = 0, int warnRemainSec = 0, bool zh = true, bool isToast = false, int bgCount = 0) {
        string st = (state ?? "error").Trim().ToLowerInvariant();
        string txt, iconKind; Color bg, ic, mc, hc, bd; int ms; bool waveMode;
        ResolveHudVisualStyle(st, zh, customText, isToast, out txt, out bg, out ic, out mc, out hc, out bd, out ms, out waveMode, out iconKind);
        using (HudPillForm f = new HudPillForm(isToast)) {
            return f.RenderOffscreenBitmapWithMetrics(txt, bg, ic, mc, hc, bd, dpi, new Rectangle(0, 0, workWidth, workHeight), waveMode, iconKind, barNorms, elapsedSec, maxDurationSec, warnRemainSec, out layout, "bottom", bgCount);
        }
    }

    public static void UpdateHud(string state, string customText, int durationMs, string position = "bottom", int maxDurationSec = 0, int warnRemainSec = 0, string layer = "", bool? clearMain = null, bool? clearToast = null, int bgCount = 0) {
        try {
            string st = (state ?? "").Trim().ToLowerInvariant();
            if (st == "" || st == "idle" || st == "hide") {
                if (_hud != null) _hud.HideNoActivate();
                if (clearToast == true && _toastHud != null) _toastHud.HideNoActivate();
                return;
            }
            if (st == "hide_toast") {
                if (_toastHud != null) _toastHud.HideNoActivate();
                return;
            }
            if (st == "hide_all") {
                if (_hud != null) _hud.HideNoActivate();
                if (_toastHud != null) _toastHud.HideNoActivate();
                return;
            }
            string effLayer = ResolveHudLayer(st, layer);
            bool isToast = (effLayer == "toast");
            bool zh = GetHostUiCulture().StartsWith("zh", StringComparison.OrdinalIgnoreCase);
            string txt, iconKind; Color bg, ic, mc, hc, bd; int ms; bool waveMode;
            ResolveHudVisualStyle(st, zh, customText, isToast, out txt, out bg, out ic, out mc, out hc, out bd, out ms, out waveMode, out iconKind);
            if (durationMs > 0) ms = durationMs;
            bool headless = _headlessHudMode;
            IntPtr fg = headless ? IntPtr.Zero : GetForegroundWindow();
            if (!isToast) {
                bool shouldClearToast = clearToast.HasValue ? clearToast.Value : (st == "recording");
                if (shouldClearToast && _toastHud != null) _toastHud.HideNoActivate();
                if (_hud == null || _hud.IsDisposed) _hud = new HudPillForm(false);
                Rectangle? inheritWa = null;
                int inheritDpi = 0;
                bool inPlaceRecordingUpdate = (st == "recording" && waveMode && _hud.IsShown && _hud.IsWaveRecording);
                if ((st != "recording" || inPlaceRecordingUpdate) && _hud.IsShown && _hud.AnchoredWorkArea.Width > 0) {
                    inheritWa = _hud.AnchoredWorkArea;
                    inheritDpi = _hud.AnchoredDpi;
                } else if (!shouldClearToast && _toastHud != null && !_toastHud.IsDisposed && _toastHud.IsShown && _toastHud.AnchoredWorkArea.Width > 0) {
                    inheritWa = _toastHud.AnchoredWorkArea;
                    inheritDpi = _toastHud.AnchoredDpi;
                } else if (headless && _headlessDefaultWa.Width > 0 && _headlessDefaultWa.Height > 0) {
                    inheritWa = _headlessDefaultWa;
                    inheritDpi = _headlessDefaultDpi;
                }
                _hud.ShowStatus(txt, bg, ic, mc, hc, bd, ms, fg, position, waveMode, iconKind, maxDurationSec, warnRemainSec, inheritWa, inheritDpi, bgCount, headless);
                if (!shouldClearToast && _toastHud != null && !_toastHud.IsDisposed && _toastHud.IsShown) {
                    _toastHud.SyncAnchor(_hud.AnchoredWorkArea, _hud.AnchoredDpi, position, headless);
                }
            } else {
                Rectangle? anchorWa = null;
                int anchorDpi = 0;
                if (_hud != null && !_hud.IsDisposed && _hud.IsShown && _hud.AnchoredWorkArea.Width > 0) {
                    anchorWa = _hud.AnchoredWorkArea;
                    anchorDpi = _hud.AnchoredDpi;
                } else if (_toastHud != null && !_toastHud.IsDisposed && _toastHud.IsShown && _toastHud.AnchoredWorkArea.Width > 0) {
                    anchorWa = _toastHud.AnchoredWorkArea;
                    anchorDpi = _toastHud.AnchoredDpi;
                } else if (headless && _headlessDefaultWa.Width > 0 && _headlessDefaultWa.Height > 0) {
                    anchorWa = _headlessDefaultWa;
                    anchorDpi = _headlessDefaultDpi;
                }
                if (clearMain == true && _hud != null) _hud.HideNoActivate();
                if (_toastHud == null || _toastHud.IsDisposed) _toastHud = new HudPillForm(true);
                _toastHud.ShowStatus(txt, bg, ic, mc, hc, bd, ms, fg, position, false, iconKind, 0, 0, anchorWa, anchorDpi, 0, headless);
            }
        } catch { }
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
                        else if (!_chordTainted && (now - _lastFireTick) >= _debounceMs) {
                            ulong held = (now >= _downTick) ? (now - _downTick) : 0UL;
                            if (held <= _tapMaxMs) { _lastFireTick = now; EnqueueTrigger("toggle", vk, kb.scanCode, ext, false, false); }
                            else if (held <= 8000UL) { _lastFireTick = now; EnqueueTrigger("hold_cancel", vk, kb.scanCode, ext, false, false, (int)held); }
                        }
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
    private static readonly object _recSync = new object();
    private static volatile RecSession _recSess = null;
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

    private static long ElapsedStopwatchMs(long startTick, long endTick) {
        if (endTick <= startTick) return 0L;
        return (long)((endTick - startTick) * 1000.0 / Stopwatch.Frequency);
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
        foreach (string raw in CmdQueue) {
            if (string.IsNullOrEmpty(raw)) continue;
            string s = raw.TrimStart();
            if (s.StartsWith("{\"cmd\":\"rec_stop\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\": \"rec_stop\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\":\"quit\"", StringComparison.Ordinal) ||
                s.StartsWith("{\"cmd\": \"quit\"", StringComparison.Ordinal)) {
                return true;
            }
        }
        return false;
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
        StopRecording(out dummyBytes, out dummyErr);
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
        return StopRecording(out dummyBytes, out error);
    }

    public static byte[] StopRecording(out int pcmBytes, out string error) {
        RecSession sess;
        lock (_recSync) {
            sess = _recSess;
            _recSess = null;
        }
        pcmBytes = 0; error = "";
        if (sess == null || sess.Worker == null) { error = "not_recording"; return new byte[0]; }
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
            return new byte[0];
        }
        byte[] raw = new byte[0];
        if (sess.Pcm != null) { try { raw = sess.Pcm.ToArray(); } catch { } }
        sess.DisposeResources();
        int evenLen = raw.Length - (raw.Length % 2);
        pcmBytes = evenLen;
        if (evenLen < 2) {
            error = string.IsNullOrEmpty(sess.Err) ? "wasapi_zero_packets" : sess.Err;
            return new byte[0];
        }
        error = sess.Err ?? "";
        return BuildPcm16MonoWav(raw, evenLen, 16000);
    }

    private const uint CF_DIB = 8u, CF_UNICODETEXT = 13u, CF_HDROP = 15u, CF_DIBV5 = 17u, GMEM_MOVEABLE = 0x0002u, GMEM_ZEROINIT = 0x0040u;
    public const int MaxClipSnapshotBytes = 100 * 1024 * 1024;
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

    private static ClipSnap CaptureOpenClip(IClipStore store) {
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

    private static bool WriteClipTextCore(ClipGuardState st, IClipStore store, string text, bool transientForPaste, Action armTimerFn, Action stopTimerFn) {
        if (!transientForPaste) {
            CancelClipRestoreCore(st, stopTimerFn);
            if (!store.Open()) return false;
            try {
                if (!store.EmptyWhenOpen()) return false;
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
            bool reuseExisting = st.RestorePending && (store.SequenceNumber == st.ExpectedSeq) && HasMatchingTransientTokenWhenOpen(store, st.ExpectedToken);
            bool ownerHung = (!reuseExisting) && (preHung || store.IsOwnerHung());
            nextSnap = reuseExisting ? st.SavedClip : (ownerHung ? ClipSnap.Unrestorable : CaptureOpenClip(store));
            myToken = ++st.NextToken;
            if (myToken == 0UL) myToken = ++st.NextToken;
            if (store.EmptyWhenOpen() && store.SetFormatBytesWhenOpen(CF_UNICODETEXT, EncodeUnicodeClipBytes(text ?? ""))) {
                WriteExcludeMarkersWhenOpen(store);
                if (store.FormatAviTransient != 0u) store.SetFormatBytesWhenOpen(store.FormatAviTransient, BitConverter.GetBytes(myToken));
                wroteOk = true;
            }
        } finally {
            store.Close();
        }
        if (!wroteOk) return false;
        if (stopTimerFn != null) stopTimerFn();
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
        return WriteClipTextCore(_clipState, _win32ClipStore, text, transientForPaste, ArmMainClipTimer, StopMainClipTimer);
    }

    public static void FlushPendingClipRestore() {
        FlushClipRestoreCore(_clipState, _win32ClipStore, null, StopMainClipTimer);
    }

    private sealed class MemClipStore : IClipStore {
        public uint Seq = 10u;
        public bool Hung = false;
        public int FailNextOpens = 0;
        public int OversizeFmt = 0;
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
            if ((uint)OversizeFmt == fmt) return ClipReadStatus.Oversize;
            byte[] src;
            if (!Formats.TryGetValue(fmt, out src) || src == null) return ClipReadStatus.Missing;
            if (src.Length > maxBytes) return ClipReadStatus.Oversize;
            data = new byte[src.Length];
            Buffer.BlockCopy(src, 0, data, 0, src.Length);
            return ClipReadStatus.Ok;
        }
        public bool EmptyWhenOpen() { Formats.Clear(); Seq++; return true; }
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
        if (MaxClipSnapshotBytes != 100 * 1024 * 1024) throw new InvalidOperationException("MaxClipSnapshotBytes expected 100MiB");
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

        // 5. Oversize (>100MiB) or hung owner -> Unrestorable, and #2 burst does not snapshot #1's transient text
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
    }

    public static void StopHook() {
        int dummyBytes; string dummyErr; StopRecording(out dummyBytes, out dummyErr);
        FlushPendingClipRestore();
        if (_clipRestoreTimer != null) { try { _clipRestoreTimer.Stop(); _clipRestoreTimer.Dispose(); } catch { } _clipRestoreTimer = null; }
        if (_clipHwnd != IntPtr.Zero) { try { DestroyWindow(_clipHwnd); } catch { } _clipHwnd = IntPtr.Zero; }
        if (_hook != IntPtr.Zero) { UnhookWindowsHookEx(_hook); _hook = IntPtr.Zero; }
        if (_hud != null) { try { _hud.Dispose(); } catch { } _hud = null; }
        if (_toastHud != null) { try { _toastHud.Dispose(); } catch { } _toastHud = null; }
    }

    public static bool IsAnyPasteBlockingKeyDown() {
        if (_targetDown) return true;
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

    private static void VerifyHudLayoutCase(string label, string state, string customText, int dpi, int workW, int workH, bool zh, bool isToast, long elapsedSec, int maxDurSec, int warnRemSec, int bgCount) {
        float[] bars = new float[] { 0.15f, 0.35f, 0.65f, 0.90f, 1.00f, 0.85f, 0.60f, 0.30f, 0.12f };
        HudLayoutMetrics m;
        using (Bitmap bmp = RenderHudSnapshotWithMetrics(state, customText, dpi, workW, workH, bars, out m, elapsedSec, maxDurSec, warnRemSec, zh, isToast, bgCount)) {
            if (bmp == null) throw new InvalidOperationException(label + ": null bitmap");
            if (bmp.Width != m.Width || bmp.Height != m.Height)
                throw new InvalidOperationException(label + ": bitmap size mismatch");
            if (m.Width <= 0 || m.Height <= 0 || m.Width > m.MaxWidth || m.Width < m.PadL + m.PadR)
                throw new InvalidOperationException(label + ": pill bounds invalid w=" + m.Width + " maxW=" + m.MaxWidth + " h=" + m.Height);
            if (m.Truncated)
                throw new InvalidOperationException(label + ": unexpected truncation w=" + m.Width + " maxW=" + m.MaxWidth);

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
            "mic_permission", "mic_disconnected", "audio_recovered", "paste_failed", "error"
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
            int[] expDurs = new int[] { 2600, 2000, 1600, 2400 };
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
            UpdateHud("hide_all", "", 0);
            if (_hud.IsShown || _toastHud.IsShown)
                throw new InvalidOperationException("single-slot check: hide_all failed");
        } finally {
            _headlessHudMode = false;
            _headlessDefaultWa = Rectangle.Empty;
            _headlessDefaultDpi = 96;
            if (_hud != null) { try { _hud.Dispose(); } catch { } _hud = null; }
            if (_toastHud != null) { try { _toastHud.Dispose(); } catch { } _toastHud = null; }
        }
    }

    public static int RunHeadlessSelfCheck() {
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
        RunToastSingleSlotSelfCheck();
        return RunHudLayoutSelfCheck();
    }
}
"@
$csCacheHit = $false
if (-not ('VoiceWinShim' -as [type])) {
    try {
        $resolvedCacheDir = ""
        if (-not [string]::IsNullOrWhiteSpace($CacheDir)) {
            $resolvedCacheDir = $CacheDir.Trim()
        } elseif (-not [string]::IsNullOrWhiteSpace($env:AGENT_VOICE_INPUT_WIN_CACHE_DIR)) {
            $resolvedCacheDir = $env:AGENT_VOICE_INPUT_WIN_CACHE_DIR.Trim()
        } elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            $resolvedCacheDir = Join-Path $env:LOCALAPPDATA "agent-voice-input\cache"
        }
        if (-not [string]::IsNullOrWhiteSpace($resolvedCacheDir)) {
            $hashInput = "v1|" + $PSVersionTable.PSVersion.ToString() + "|" + $PSVersionTable.CLRVersion.ToString() + "|" + [IntPtr]::Size + "|" + $csSource
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try {
                $hashBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($hashInput))
            } finally {
                $sha.Dispose()
            }
            $csHash = ([System.BitConverter]::ToString($hashBytes, 0, 8) -replace '-', '').ToLowerInvariant()
            $dllName = "VoiceWinShim_$csHash.dll"
            $dllPath = Join-Path $resolvedCacheDir $dllName
            if (Test-Path -LiteralPath $dllPath) {
                try {
                    Add-Type -Path $dllPath -ErrorAction Stop
                    if ('VoiceWinShim' -as [type]) { $csCacheHit = $true }
                } catch {
                    if (-not ('VoiceWinShim' -as [type])) {
                        try { [System.IO.File]::Move($dllPath, "$dllPath.bad.$PID") } catch { }
                    }
                }
            }
            if (-not ('VoiceWinShim' -as [type])) {
                try { [void][System.IO.Directory]::CreateDirectory($resolvedCacheDir) } catch { }
                $randHex = [Guid]::NewGuid().ToString("N").Substring(0, 8)
                $tmpDll = "$dllPath.$PID.$randHex.tmp.dll"
                $provider = New-Object Microsoft.CSharp.CSharpCodeProvider
                try {
                    $cp = New-Object System.CodeDom.Compiler.CompilerParameters
                    $cp.GenerateInMemory = $false
                    $cp.GenerateExecutable = $false
                    $cp.IncludeDebugInformation = $false
                    $cp.OutputAssembly = $tmpDll
                    $refLocs = @(
                        [System.Uri].Assembly.Location,
                        [System.Linq.Enumerable].Assembly.Location,
                        [System.Drawing.Bitmap].Assembly.Location,
                        [System.Windows.Forms.Form].Assembly.Location
                    )
                    foreach ($loc in $refLocs) {
                        if ((-not [string]::IsNullOrWhiteSpace($loc)) -and (-not $cp.ReferencedAssemblies.Contains($loc))) {
                            [void]$cp.ReferencedAssemblies.Add($loc)
                        }
                    }
                    $cr = $provider.CompileAssemblyFromSource($cp, $csSource)
                    if ((-not $cr.Errors.HasErrors) -and (Test-Path -LiteralPath $tmpDll)) {
                        try {
                            [System.IO.File]::Move($tmpDll, $dllPath)
                        } catch [System.IO.IOException] {
                            try { [System.IO.File]::Delete($tmpDll) } catch { }
                        }
                        if (Test-Path -LiteralPath $dllPath) {
                            Add-Type -Path $dllPath -ErrorAction Stop
                        }
                    }
                } finally {
                    $provider.Dispose()
                    if (Test-Path -LiteralPath $tmpDll) {
                        try { [System.IO.File]::Delete($tmpDll) } catch { }
                    }
                }
                try {
                    $staleCutoff = [DateTime]::UtcNow.AddMinutes(-5)
                    foreach ($f in [System.IO.Directory]::GetFiles($resolvedCacheDir, "VoiceWinShim_*")) {
                        $fn = [System.IO.Path]::GetFileName($f)
                        if ($fn.Equals($dllName, [StringComparison]::OrdinalIgnoreCase)) { continue }
                        if ($fn.EndsWith(".tmp.dll", [StringComparison]::OrdinalIgnoreCase)) {
                            try {
                                if ([System.IO.File]::GetLastWriteTimeUtc($f) -gt $staleCutoff) { continue }
                            } catch { continue }
                        }
                        try { [System.IO.File]::Delete($f) } catch { }
                    }
                } catch { }
            }
        }
    } catch { }
    if (-not ('VoiceWinShim' -as [type])) {
        Add-Type -ReferencedAssemblies System.Windows.Forms, System.Drawing -TypeDefinition $csSource
    }
} else {
    $csCacheHit = $true
}

function Write-JsonLine($obj) { try { [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 5)); [Console]::Out.Flush() } catch { } }
function Invoke-Tone([string]$kind) { [VoiceWinShim]::PlayTone($kind, [bool]$Beep) }
function Set-ClipboardSafe([string]$val) {
    for ($i = 0; $i -lt 3; $i++) {
        try {
            if ([VoiceWinShim]::WriteClipTextOnce($val, $false)) { return $true }
        } catch { }
        if ($i -lt 2) { Start-Sleep -Milliseconds 10 }
    }
    return $false
}
function Set-ClipboardTransient([string]$val) {
    for ($i = 0; $i -lt 3; $i++) {
        try {
            if ([VoiceWinShim]::WriteClipTextOnce($val, $true)) { return $true }
        } catch { }
        if ($i -lt 2) { Start-Sleep -Milliseconds 10 }
    }
    return $false
}
function Get-TitleSessionToken([string]$title) {
    if ([string]::IsNullOrWhiteSpace($title)) { return "" }
    $m1 = [regex]::Matches($title, '(?i)\[(agy|claude|codex):#?([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8,36})\]')
    if ($m1.Count -gt 0) { return $m1[$m1.Count - 1].Groups[2].Value.ToLowerInvariant() }
    $m2 = [regex]::Matches($title, '(?i)\[#([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8,36})\](?=$|[^0-9A-Za-z_])')
    if ($m2.Count -gt 0) { return $m2[$m2.Count - 1].Groups[1].Value.ToLowerInvariant() }
    $m3 = [regex]::Matches($title, '(?i)(?:^|[^0-9a-f-])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=$|[^0-9a-f-])')
    if ($m3.Count -gt 0) { return $m3[$m3.Count - 1].Groups[1].Value.ToLowerInvariant() }
    return ""
}

[VoiceWinShim]::EnableDpiAwareness()

if ($Mode -eq "self-check") {
    try {
        $parseErrors = $null
        if (-not [string]::IsNullOrWhiteSpace($PSCommandPath)) {
            [void][System.Management.Automation.Language.Parser]::ParseFile($PSCommandPath, [ref]$null, [ref]$parseErrors)
            if ($null -ne $parseErrors -and $parseErrors.Count -gt 0) {
                throw ("AST parse errors: " + $parseErrors.Count)
            }
        }
        $hudLayoutCases = [VoiceWinShim]::RunHeadlessSelfCheck()
        $t1 = "agy " + ([char]0x00B7) + " ~/repo [#a1b2c3d4]"
        if ((Get-TitleSessionToken $t1) -ne "a1b2c3d4") { throw "Get-TitleSessionToken hash tag failed" }
        if ((Get-TitleSessionToken "claude [claude:#DEADBEEF]") -ne "deadbeef") { throw "Get-TitleSessionToken prefixed tag failed" }
        if ((Get-TitleSessionToken "feat/codex-config") -ne "") { throw "Get-TitleSessionToken false positive" }
        if ((Get-TitleSessionToken "old [#11112222] new [#33334444]") -ne "33334444") { throw "Get-TitleSessionToken multi-tag tail failed" }
        $protoSample = (@{ type = "reply"; reply = "ping"; seq_id = 1; ok = $true } | ConvertTo-Json -Compress) | ConvertFrom-Json
        if ((-not $protoSample.ok) -or ($protoSample.reply -ne "ping") -or ([int]$protoSample.seq_id -ne 1)) {
            throw "JSON roundtrip check failed"
        }
        Write-JsonLine @{ ok = $true; mode = "self-check"; ps_version = $PSVersionTable.PSVersion.ToString(); clr_version = $PSVersionTable.CLRVersion.ToString(); cache_hit = [bool]$csCacheHit; hud_layout_cases = [int]$hudLayoutCases }
        exit 0
    } catch {
        [Console]::Error.WriteLine("win_shim self-check failed: " + [string]$_.Exception.Message)
        exit 1
    }
}
if ($Mode -eq "clip-self-check") {
    [VoiceWinShim]::RunClipStateSelfCheck()
    Write-JsonLine @{ ok = $true; mode = "clip-self-check" }
    exit 0
}
if ($Mode -eq "probe") {
    $hwnd = 0L; $proc = ""; $title = ""; [VoiceWinShim]::QueryForeground([ref]$hwnd, [ref]$proc, [ref]$title)
    $wasapiState = ""; $wasapiOk = [VoiceWinShim]::ProbeDefaultWasapiCapture([ref]$wasapiState)
    Write-JsonLine @{ ok = $true; ps_version = $PSVersionTable.PSVersion.ToString(); wasapi_ok = [bool]$wasapiOk; wasapi_state = [string]$wasapiState; wave_in_devs = [VoiceWinShim]::waveInGetNumDevs(); hwnd = $hwnd; process = $proc; title = $title; ui_culture = [VoiceWinShim]::GetHostUiCulture(); dpi = [VoiceWinShim]::GetTargetDpi([IntPtr]$hwnd) }
    exit 0
}

$targetVk = [VoiceWinShim]::ResolveKeyName($Hotkey)
$isPtt = ($TriggerMode -eq "ptt" -or $TriggerMode -eq "push_to_talk")
$AllowedTerminals = @("windowsterminal", "openconsole", "mintty", "wezterm", "wezterm-gui", "alacritty", "ghostty", "conhost", "putty", "tabby", "hyper", "kitty", "rio", "warp")
if (-not [string]::IsNullOrWhiteSpace($AllowedTerminalsCsv)) {
    $AllowedTerminals = @($AllowedTerminalsCsv.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ -ne "" })
}
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
            while ([VoiceWinShim]::EvtQueue.TryDequeue([ref]$ev)) {
                if ($ev.Kind -eq "rec_lost") {
                    Write-JsonLine @{ event = "rec_lost"; reason = [string]$ev.Reason; rec_gen = [long]$ev.RecGen }
                } elseif ($ev.Kind -eq "rec_recovered") {
                    Write-JsonLine @{ event = "rec_recovered"; gap_ms = [int]$ev.GapMs; rec_gen = [long]$ev.RecGen }
                } elseif ($ev.Kind -eq "hold_cancel") {
                    Write-JsonLine @{ event = "hold_cancel"; hold_ms = [int]$ev.HoldMs; hwnd = $ev.Hwnd; process = $ev.Process; title = $ev.Title; ts_ms = $ev.TsMs }
                } else {
                    Write-JsonLine @{ event = $ev.Kind; hwnd = $ev.Hwnd; process = $ev.Process; title = $ev.Title; ts_ms = $ev.TsMs }
                }
            }
        } catch { }
        $rawCmd = $null
        while ([VoiceWinShim]::CmdQueue.TryDequeue([ref]$rawCmd)) {
            if ([string]::IsNullOrWhiteSpace($rawCmd)) { continue }
            $seq = 0; $op = "unknown"
            try {
                $cmdObj = $rawCmd | ConvertFrom-Json; $op = [string]$cmdObj.cmd; $seq = if ($null -ne $cmdObj.seq_id) { [int]$cmdObj.seq_id } else { 0 }
                if ($op -eq "ping") { Write-JsonLine @{ type = "reply"; reply = "ping"; seq_id = $seq; ok = $true; wave_in_devs = [VoiceWinShim]::waveInGetNumDevs() } }
                elseif ($op -eq "fg_window") { $h = 0L; $p = ""; $t = ""; [VoiceWinShim]::QueryForeground([ref]$h, [ref]$p, [ref]$t); Write-JsonLine @{ type = "reply"; reply = "fg_window"; seq_id = $seq; ok = $true; hwnd = $h; process = $p; title = $t } }
                elseif ($op -eq "key_state") { Write-JsonLine @{ type = "reply"; reply = "key_state"; seq_id = $seq; ok = $true; hotkey_held = [bool][VoiceWinShim]::IsAnyPasteBlockingKeyDown() } }
                elseif ($op -eq "beep") { Invoke-Tone ([string]$cmdObj.kind); Write-JsonLine @{ type = "reply"; reply = "beep"; seq_id = $seq; ok = $true } }
                elseif ($op -eq "hud") {
                    $dms = if ($null -ne $cmdObj.duration_ms) { [int]$cmdObj.duration_ms } else { 0 }
                    $maxDur = if ($null -ne $cmdObj.max_duration_sec) { [int]$cmdObj.max_duration_sec } else { 0 }
                    $warnRem = if ($null -ne $cmdObj.warn_remain_sec) { [int]$cmdObj.warn_remain_sec } else { 0 }
                    $lyr = if ($null -ne $cmdObj.layer) { [string]$cmdObj.layer } else { "" }
                    $clrMain = if ($null -ne $cmdObj.clear_main) { [bool]$cmdObj.clear_main } else { $null }
                    $clrToast = if ($null -ne $cmdObj.clear_toast) { [bool]$cmdObj.clear_toast } else { $null }
                    $bgCnt = if ($null -ne $cmdObj.bg_count) { [int]$cmdObj.bg_count } else { 0 }
                    [VoiceWinShim]::UpdateHud([string]$cmdObj.state, [string]$cmdObj.text, $dms, [string]$cmdObj.position, $maxDur, $warnRem, $lyr, $clrMain, $clrToast, $bgCnt)
                    Write-JsonLine @{ type = "reply"; reply = "hud"; seq_id = $seq; ok = $true }
                } elseif ($op -eq "hud_level") {
                    $lvl = if ($null -ne $cmdObj.level) { [float]$cmdObj.level } else { 0.0 }
                    [VoiceWinShim]::SetExternalAudioLevel($lvl)
                    if ($seq -gt 0) { Write-JsonLine @{ type = "reply"; reply = "hud_level"; seq_id = $seq; ok = $true } }
                } elseif ($op -eq "paste") {
                    $txt = [string]$cmdObj.text
                    $fbTxt = if ($null -ne $cmdObj.fallback_text -and ([string]$cmdObj.fallback_text).Length -gt 0) { [string]$cmdObj.fallback_text } else { $txt }
                    $forceClipOnly = if ($null -ne $cmdObj.force_clipboard_only) { [bool]$cmdObj.force_clipboard_only } else { $false }
                    $suppressBeep = if ($null -ne $cmdObj.suppress_beep) { [bool]$cmdObj.suppress_beep } else { $false }
                    $expectTerm = if ($null -ne $cmdObj.expect_terminal) { [bool]$cmdObj.expect_terminal } else { $true }
                    $expectProc = if ($null -ne $cmdObj.expect_process) { ([string]$cmdObj.expect_process -replace '\.exe$','').Trim().ToLowerInvariant() } else { "" }
                    $expectTok = if ($null -ne $cmdObj.expect_session_token) { ([string]$cmdObj.expect_session_token).Trim().ToLowerInvariant() } else { "" }
                    $pasteChord = if ($null -ne $cmdObj.paste_chord -and ([string]$cmdObj.paste_chord).Length -gt 0) { [string]$cmdObj.paste_chord } else { "ctrl_v" }
                    $thwnd = if ($null -ne $cmdObj.target_hwnd) { [long]$cmdObj.target_hwnd } else { 0L }
                    $h = 0L; $p = ""; $t = ""; [VoiceWinShim]::QueryForeground([ref]$h, [ref]$p, [ref]$t)
                    $pNorm = ($p -replace '\.exe$','').ToLowerInvariant()
                    $curTok = if ($expectTok -ne "") { Get-TitleSessionToken $t } else { "" }
                    $tokConflict = ($expectTok -ne "") -and ($curTok -ne "") -and ($curTok -ne $expectTok)
                    $winMatch = (($thwnd -le 0) -or ($h -eq $thwnd)) -and (($expectProc -eq "") -or ($pNorm -eq $expectProc)) -and ((-not $expectTerm) -or ($AllowedTerminals -contains $pNorm)) -and (-not $tokConflict)
                    if ($winMatch -and (-not $forceClipOnly)) {
                        if ([VoiceWinShim]::IsAnyPasteBlockingKeyDown()) {
                            Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $false; reason = "hotkey_held"; fg_process = $p }
                        } else {
                            $clipOk = Set-ClipboardTransient $txt
                            if (-not $clipOk) {
                                Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $false; reason = "clipboard_failed"; fg_process = $p }
                            } else {
                                $h2 = 0L; $p2 = ""; $t2 = ""; [VoiceWinShim]::QueryForeground([ref]$h2, [ref]$p2, [ref]$t2)
                                $p2Norm = ($p2 -replace '\.exe$','').ToLowerInvariant()
                                $curTok2 = if ($expectTok -ne "") { Get-TitleSessionToken $t2 } else { "" }
                                $tokConflict2 = ($expectTok -ne "") -and ($curTok2 -ne "") -and ($curTok2 -ne $expectTok)
                                $winMatch2 = (($thwnd -le 0) -or ($h2 -eq $thwnd)) -and (($expectProc -eq "") -or ($p2Norm -eq $expectProc)) -and ((-not $expectTerm) -or ($AllowedTerminals -contains $p2Norm)) -and (-not $tokConflict2)
                                if (-not $winMatch2) {
                                    if ($fbTxt -ne $txt) { $clipOk = Set-ClipboardSafe $fbTxt }
                                    else { $clipOk = Set-ClipboardSafe $txt }
                                    if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                                    Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "focus_changed" } else { "clipboard_failed" }); fg_process = $p2 }
                                } elseif ([VoiceWinShim]::IsAnyPasteBlockingKeyDown()) {
                                    if ($fbTxt -ne $txt) { [void](Set-ClipboardTransient $fbTxt) }
                                    Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = [bool]$clipOk; reason = "hotkey_held"; fg_process = $p2 }
                                } else {
                                    $sent = [VoiceWinShim]::SendPasteChord($pasteChord)
                                    if ($sent) {
                                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $true; clipboard = $true; reason = "ok"; fg_process = $p2 }
                                    } else {
                                        if ($fbTxt -ne $txt) { $clipOk = Set-ClipboardSafe $fbTxt }
                                        else { $clipOk = Set-ClipboardSafe $txt }
                                        if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "send_input_failed" } else { "clipboard_failed" }); fg_process = $p2 }
                                    }
                                }
                            }
                        }
                    } else {
                        $clipOk = Set-ClipboardSafe $fbTxt
                        if ($clipOk -and (-not $suppressBeep)) { Invoke-Tone "focus" }
                        Write-JsonLine @{ type = "reply"; reply = "paste"; seq_id = $seq; ok = $true; pasted = $false; clipboard = $clipOk; reason = $(if ($clipOk) { "focus_changed" } else { "clipboard_failed" }); fg_process = $p }
                    }
                } elseif ($op -eq "rec_start") {
                    $loopback = if ($null -ne $cmdObj.loopback) { [bool]$cmdObj.loopback } else { $false }
                    $recGen = if ($null -ne $cmdObj.rec_gen) { [long]$cmdObj.rec_gen } else { 0L }
                    $recErr = ""; $okStart = [VoiceWinShim]::StartRecording($loopback, $recGen, [ref]$recErr)
                    Write-JsonLine @{ type = "reply"; reply = "rec_start"; seq_id = $seq; ok = [bool]$okStart; rec_error = [string]$recErr }
                } elseif ($op -eq "rec_stop") {
                    $pcmBytes = 0; $recErr = ""; $wavBytes = [VoiceWinShim]::StopRecording([ref]$pcmBytes, [ref]$recErr)
                    $b64 = if ($wavBytes -and $wavBytes.Length -gt 0) { [Convert]::ToBase64String($wavBytes) } else { "" }
                    Write-JsonLine @{ type = "reply"; reply = "rec_stop"; seq_id = $seq; ok = ($b64.Length -gt 0); pcm_bytes = $pcmBytes; wav_b64 = $b64; rec_error = [string]$recErr }
                } elseif ($op -eq "quit") { break bridgeLoop }
                else { Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $false; error = "unknown_cmd:$op" } }
            } catch {
                if ($seq -gt 0 -or $op -ne "hud_level") { Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $false; error = [string]$_.Exception.Message } }
            }
        }
    }
} finally {
    [VoiceWinShim]::StopHook()
}
