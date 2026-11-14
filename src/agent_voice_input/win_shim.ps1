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
using System.Drawing.Imaging;
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
    private static readonly Color RecHintColor = Color.FromArgb(214, 188, 194);
    private static readonly Color WarnBgColor = Color.FromArgb(72, 38, 12);
    private static readonly Color WarnBorderColor = Color.FromArgb(224, 138, 28);
    private static readonly Color WarnWaveActiveColor = Color.FromArgb(255, 251, 191, 36);
    private static readonly Color WarnWaveQuietColor = Color.FromArgb(220, 251, 191, 36);
    private static readonly Color WarnMainColor = Color.FromArgb(254, 252, 232);
    private static readonly Color WarnHintColor = Color.FromArgb(253, 224, 71);
    private static readonly float[] BellWeight = new float[] { 1.00f, 0.92f, 0.78f, 0.58f, 0.40f };
    private Font _font;
    private bool _ownsFont, _cjkFont;
    private int _dpi = 96;
    private readonly System.Windows.Forms.Timer _timer = new System.Windows.Forms.Timer();
    private readonly System.Windows.Forms.Timer _animTimer = new System.Windows.Forms.Timer();
    private readonly float[] _waveHist = new float[5];
    private readonly float[] _barNorms = new float[9];
    private float _smoothLevel = 0f;
    private int _frameIdx = 0;
    private long _recStartTick = 0L, _elapsedOverrideSec = -1L;
    private int _maxDurationSec = 0, _warnRemainSec = 0;
    private bool _isWaveRecording = false, _shown = false;
    private string _text = "", _position = "bottom", _iconKind = "";
    private Color _bgColor = DefaultBgColor, _iconColor = Color.White, _mainColor = Color.White, _hintColor = RecHintColor, _borderColor = Color.Gray;
    private IntPtr _fgHwnd = IntPtr.Zero;
    private int _lastX = int.MinValue, _lastY = int.MinValue, _lastW = 0, _lastH = 0;

    public bool IsWaveAnimating { get { return _isWaveRecording && _animTimer.Enabled; } }
    public int LastWidth { get { return _lastW; } }
    public int LastHeight { get { return _lastH; } }
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

    private static Font CreateHudFont(bool preferCjk, int dpi, out bool owns) {
        float pxSize = Math.Max(12f, (float)Math.Round(13.0f * dpi / 96.0f));
        string[] fams = preferCjk ? new string[] { "Microsoft YaHei UI", "Segoe UI" } : new string[] { "Segoe UI", "Microsoft YaHei UI" };
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
        if (iconSeg == "\u25CB") return "empty";
        return "error";
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
            } else if (kind == "empty") {
                float ins = 1.0f * dpiScale;
                float d = Math.Max(2.0f, box - ins * 2.0f);
                using (Pen p = new Pen(color, 1.6f * dpiScale)) {
                    g.DrawEllipse(p, x + ins, y + ins, d, d);
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

    public HudPillForm() {
        _cjkFont = VoiceWinShim.GetHostUiCulture().StartsWith("zh", StringComparison.OrdinalIgnoreCase);
        _dpi = VoiceWinShim.GetTargetDpi(IntPtr.Zero);
        _font = CreateHudFont(_cjkFont, _dpi, out _ownsFont);
        AutoScaleMode = AutoScaleMode.None;
        FormBorderStyle = FormBorderStyle.None; StartPosition = FormStartPosition.Manual;
        ShowInTaskbar = false; BackColor = DefaultBgColor;
        _timer.Tick += (s, e) => { _timer.Stop(); HideNoActivate(); };
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
        _timer.Stop(); _animTimer.Stop(); _isWaveRecording = false; _shown = false;
        _maxDurationSec = 0; _warnRemainSec = 0;
        if (IsHandleCreated) ShowWindow(Handle, 0);
    }
    public void ShowStatus(string text, Color bgColor, Color iconColor, Color mainColor, Color hintColor, Color border, int autoHideMs, IntPtr fgHwnd, string position, bool waveMode = false, string iconKind = "", int maxDurationSec = 0, int warnRemainSec = 0) {
        _timer.Stop();
        _text = text ?? ""; _bgColor = bgColor; _iconColor = iconColor; _mainColor = mainColor; _hintColor = hintColor; _borderColor = border;
        _iconKind = iconKind ?? ""; _fgHwnd = fgHwnd; _position = position ?? "bottom";
        int dpi = VoiceWinShim.GetTargetDpi(fgHwnd);
        bool wantCjk = HasCjk(_text);
        if (_font == null || wantCjk != _cjkFont || dpi != _dpi) {
            if (_ownsFont && _font != null) _font.Dispose();
            _cjkFont = wantCjk; _dpi = dpi;
            _font = CreateHudFont(_cjkFont, _dpi, out _ownsFont);
        }
        if (waveMode) {
            _maxDurationSec = (maxDurationSec > 0) ? maxDurationSec : 0;
            _warnRemainSec = (_maxDurationSec > 0) ? ((warnRemainSec > 0) ? warnRemainSec : VoiceWinShim.RecordingCountdownWarnSec(_maxDurationSec)) : 0;
            if (!_isWaveRecording) {
                _isWaveRecording = true;
                _recStartTick = (long)VoiceWinShim.GetTickCount64();
                ResetWaveEnvelope();
                float initLvl = VoiceWinShim.GetLiveAudioLevel();
                if (initLvl > 0f) StepWaveEnvelope(initLvl);
            }
            RenderPill(true);
            if (!_animTimer.Enabled) _animTimer.Start();
        } else {
            _animTimer.Stop();
            _isWaveRecording = false;
            _maxDurationSec = 0; _warnRemainSec = 0;
            RenderPill(true);
        }
        if (autoHideMs > 0) { _timer.Interval = autoHideMs; _timer.Start(); }
    }

    public Bitmap RenderOffscreenBitmap(string text, Color bgColor, Color iconColor, Color mainColor, Color hintColor, Color border, int dpi, Rectangle workArea, bool waveMode, string iconKind, float[] barNormsOverride, long elapsedSec, int maxDurationSec, int warnRemainSec) {
        _text = text ?? ""; _bgColor = bgColor; _iconColor = iconColor; _mainColor = mainColor; _hintColor = hintColor; _borderColor = border;
        _iconKind = iconKind ?? ""; _position = "bottom"; _isWaveRecording = waveMode;
        _maxDurationSec = (waveMode && maxDurationSec > 0) ? maxDurationSec : 0;
        _warnRemainSec = (_maxDurationSec > 0) ? ((warnRemainSec > 0) ? warnRemainSec : VoiceWinShim.RecordingCountdownWarnSec(_maxDurationSec)) : 0;
        _elapsedOverrideSec = elapsedSec;
        Array.Clear(_barNorms, 0, _barNorms.Length);
        if (barNormsOverride != null) {
            int n = Math.Min(9, barNormsOverride.Length);
            for (int i = 0; i < n; i++) _barNorms[i] = barNormsOverride[i];
        }
        int clampedDpi = (dpi < 96) ? 96 : ((dpi > 480) ? 480 : dpi);
        bool wantCjk = HasCjk(_text);
        if (_font == null || wantCjk != _cjkFont || clampedDpi != _dpi) {
            if (_ownsFont && _font != null) _font.Dispose();
            _cjkFont = wantCjk; _dpi = clampedDpi;
            _font = CreateHudFont(_cjkFont, _dpi, out _ownsFont);
        }
        int dummyX, dummyY;
        return BuildPillPArgbBitmap(workArea, out dummyX, out dummyY);
    }

    private Bitmap BuildPillPArgbBitmap(Rectangle wa, out int x, out int y) {
        string iconSeg, mainSeg, hintSeg; bool spBeforeHint;
        VoiceWinShim.SplitHudSegments(_text, out iconSeg, out mainSeg, out hintSeg, out spBeforeHint);
        int padL = ScalePx(13), padR = ScalePx(14), minW = ScalePx(64);
        int maxW = Math.Max(minW, wa.Width - ScalePx(24));

        float dpiScale = _dpi / 96.0f;
        float barW = 2.4f * dpiScale, barGap = 2.2f * dpiScale;
        int waveBoxW = (int)Math.Ceiling(9.0f * barW + 8.0f * barGap);
        string durStr = "", effHint = hintSeg;
        int slotW = 0, hintSlotW = 0, iconBox = 0, gap1 = 0, gap2 = 0, iconGap = 0, hintGap = 0, totalTextW = 0, textH = 0;
        Size mainSz = Size.Empty, hintSz = Size.Empty;
        Color effBg = _bgColor, effBorder = _borderColor, effMain = _mainColor, effHintCol = _hintColor;
        Color effWaveActive = RecWaveActiveColor, effWaveQuiet = RecWaveQuietColor;

        if (_isWaveRecording) {
            long elapsedSec = GetEffectiveElapsedSec();
            bool isWarn = false;
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
            durStr = VoiceWinShim.FormatRecordingDuration(elapsedSec);
            string slotTpl = VoiceWinShim.DurationSlotTemplate(elapsedSec);
            Size actDurSz = TextRenderer.MeasureText(durStr, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
            Size tplDurSz = TextRenderer.MeasureText(slotTpl, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
            slotW = Math.Max(actDurSz.Width, tplDurSz.Width);
            if (effHint.Length > 0) {
                hintSz = TextRenderer.MeasureText(effHint, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
                if (isWarn) {
                    string hintTpl = VoiceWinShim.CountdownHintSlotTemplate((int)remainSec, zhHint);
                    Size tplHintSz = TextRenderer.MeasureText(hintTpl, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
                    hintSlotW = Math.Max(hintSz.Width, tplHintSz.Width);
                } else {
                    hintSlotW = hintSz.Width;
                }
            }
            gap1 = ScalePx(7);
            gap2 = (effHint.Length > 0) ? ScalePx(6) : 0;
            totalTextW = waveBoxW + gap1 + slotW + gap2 + hintSlotW;
            textH = Math.Max(actDurSz.Height, hintSz.Height);
        } else {
            iconBox = (iconSeg.Length > 0) ? ScalePx(11) : 0;
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
        int h = Math.Max(ScalePx(31), textH + ScalePx(12));
        bool isTop = string.Equals((_position ?? "").Trim(), "top", StringComparison.OrdinalIgnoreCase);
        x = wa.Left + (wa.Width - w) / 2;
        y = isTop ? (wa.Top + ScalePx(36)) : Math.Max(wa.Top + ScalePx(4), wa.Bottom - h - ScalePx(4));

        Bitmap outBmp = new Bitmap(w, h, PixelFormat.Format32bppPArgb);
        using (Bitmap rgbBmp = new Bitmap(w, h, PixelFormat.Format32bppRgb))
        using (Bitmap maskBmp = new Bitmap(w, h, PixelFormat.Format32bppArgb)) {
            using (Graphics g = Graphics.FromImage(rgbBmp)) {
                g.Clear(effBg);
                int availW = Math.Max(1, w - padL - padR);
                int bleed = ScalePx(2);
                if (_isWaveRecording) {
                    int curX = padL + (truncated ? 0 : Math.Max(0, (availW - totalTextW) / 2));
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
                    curX += waveBoxW + gap1;
                    TextRenderer.DrawText(g, durStr, _font, new Rectangle(curX - bleed, 0, slotW + bleed * 2, h), effMain, DurDrawFlags);
                    curX += slotW + gap2;
                    if (effHint.Length > 0) {
                        int remW = w - padR - curX;
                        if (remW > 0) {
                            if (truncated) {
                                TextRenderer.DrawText(g, effHint, _font, new Rectangle(curX, 0, remW, h), effHintCol, SegDrawFlags | TextFormatFlags.EndEllipsis);
                            } else {
                                TextRenderer.DrawText(g, effHint, _font, new Rectangle(curX, 0, hintSlotW + bleed, h), effHintCol, SegDrawFlags);
                            }
                        }
                    }
                } else if (truncated) {
                    if (iconBox > 0) {
                        DrawStatusIcon(g, ResolveIconKind(_iconKind, iconSeg), _iconColor, (float)padL, (h - iconBox) * 0.5f, (float)iconBox, dpiScale);
                        int tx = padL + iconBox + iconGap;
                        string restTxt = mainSeg + ((hintSeg.Length > 0) ? ((spBeforeHint ? " " : "") + hintSeg) : "");
                        if (w - padR - tx > 0 && restTxt.Length > 0) {
                            TextRenderer.DrawText(g, restTxt, _font, new Rectangle(tx, 0, w - padR - tx, h), effMain, SegDrawFlags | TextFormatFlags.EndEllipsis);
                        }
                    } else {
                        TextRenderer.DrawText(g, _text, _font, new Rectangle(padL, 0, availW, h), effMain, TruncDrawFlags);
                    }
                } else {
                    int curX = padL + Math.Max(0, (availW - totalTextW) / 2);
                    if (iconBox > 0) {
                        DrawStatusIcon(g, ResolveIconKind(_iconKind, iconSeg), _iconColor, (float)curX, (h - iconBox) * 0.5f, (float)iconBox, dpiScale);
                        curX += iconBox + iconGap;
                    }
                    if (mainSeg.Length > 0) {
                        TextRenderer.DrawText(g, mainSeg, _font, new Rectangle(curX, 0, mainSz.Width + bleed, h), effMain, SegDrawFlags);
                        curX += mainSz.Width + hintGap;
                    }
                    if (hintSeg.Length > 0) {
                        TextRenderer.DrawText(g, hintSeg, _font, new Rectangle(curX, 0, hintSz.Width + bleed, h), effHintCol, SegDrawFlags);
                    }
                }
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                float penW = Math.Max(1.0f, 1.1f * (_dpi / 96.0f));
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
        Rectangle wa = VoiceWinShim.GetTargetWorkArea(_fgHwnd);
        int prevX = _lastX, prevY = _lastY, prevW = _lastW, prevH = _lastH;
        int x, y;
        IntPtr hdl = Handle;
        using (Bitmap outBmp = BuildPillPArgbBitmap(wa, out x, out y)) {
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
        if (disposing) { _timer.Dispose(); _animTimer.Dispose(); if (_ownsFont && _font != null) _font.Dispose(); }
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

    public static void SplitHudSegments(string raw, out string icon, out string main, out string hint, out bool spaceBeforeHint) {
        icon = ""; main = ""; hint = ""; spaceBeforeHint = false;
        string s = (raw ?? "").Trim();
        if (s.Length == 0) return;
        string rest = s;
        int spIdx = s.IndexOf(' ');
        if (spIdx >= 1 && spIdx <= 3 && spIdx + 1 < s.Length) {
            bool hasAlphaNum = false;
            for (int i = 0; i < spIdx; i++) {
                if (char.IsLetterOrDigit(s[i])) { hasAlphaNum = true; break; }
            }
            if (!hasAlphaNum) {
                icon = s.Substring(0, spIdx);
                int afterIdx = spIdx + 1;
                while (afterIdx < s.Length && s[afterIdx] == ' ') afterIdx++;
                rest = s.Substring(afterIdx);
            }
        }
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
        if (_wasapiActive) return _wasapiLevel;
        if ((long)GetTickCount64() - Interlocked.Read(ref _extLevelTick) <= 260L) return _extLevel;
        return 0f;
    }

    private static void ResolveHudVisualStyle(string st, bool zh, string customText, out string txt, out Color bg, out Color ic, out Color mc, out Color hc, out Color bd, out int ms, out bool waveMode, out string iconKind) {
        ms = 0; waveMode = false; iconKind = "";
        if (st == "recording") {
            string hkEn = (_hotkeyLabel == "\u53F3 Ctrl") ? "Right Ctrl" : _hotkeyLabel.Trim();
            txt = zh
                ? ("\u25CF \u6B63\u5728\u5F55\u97F3\u2026 (" + (_isPtt ? "\u677E\u5F00" : "\u518D\u6309") + _hotkeyLabel + " \u5B8C\u6210)")
                : ("\u25CF Listening\u2026 (" + (_isPtt ? "release " : "press ") + hkEn + " to finish)");
            bg = Color.FromArgb(64, 18, 26); ic = Color.FromArgb(255, 104, 112); mc = Color.FromArgb(248, 250, 252); hc = Color.FromArgb(214, 188, 194); bd = Color.FromArgb(196, 66, 80);
            string trimmedCustom = (customText ?? "").TrimStart();
            waveMode = string.IsNullOrEmpty(trimmedCustom) || trimmedCustom.StartsWith("\u25CF ", StringComparison.Ordinal);
        } else if (st == "recognizing" || st == "processing") {
            txt = zh ? "\u25D0 \u6B63\u5728\u8BED\u97F3\u8BC6\u522B\u2026" : "\u25D0 Transcribing\u2026";
            bg = Color.FromArgb(16, 40, 72); ic = Color.FromArgb(56, 189, 248); mc = Color.FromArgb(244, 248, 252); hc = Color.FromArgb(176, 198, 222); bd = Color.FromArgb(52, 148, 212); iconKind = "recognizing";
        } else if (st == "refining") {
            txt = zh ? "\u25C8 \u6B63\u5728\u6574\u7406\u7CBE\u4FEE\u2026" : "\u25C8 Refining\u2026";
            bg = Color.FromArgb(46, 22, 76); ic = Color.FromArgb(208, 156, 255); mc = Color.FromArgb(248, 245, 255); hc = Color.FromArgb(198, 182, 226); bd = Color.FromArgb(156, 88, 224); iconKind = "refining";
        } else if (st == "done") {
            txt = zh ? "\u2713 \u5DF2\u586B\u5165" : "\u2713 Inserted";
            bg = Color.FromArgb(14, 54, 36); ic = Color.FromArgb(74, 222, 128); mc = Color.FromArgb(242, 253, 246); hc = Color.FromArgb(174, 218, 192); bd = Color.FromArgb(48, 168, 102); ms = 900; iconKind = "done";
        } else if (st == "copied") {
            txt = zh ? "\u2713 \u5DF2\u590D\u5236(\u7A97\u53E3\u5DF2\u5207\u6362)" : "\u2713 Copied (window switched)";
            bg = Color.FromArgb(28, 32, 82); ic = Color.FromArgb(144, 156, 255); mc = Color.FromArgb(240, 244, 255); hc = Color.FromArgb(186, 196, 238); bd = Color.FromArgb(108, 116, 242); ms = 2000; iconKind = "done";
        } else if (st == "empty") {
            txt = zh ? "\u25CB \u672A\u68C0\u6D4B\u5230\u8BED\u97F3" : "\u25CB No speech detected";
            bg = Color.FromArgb(66, 44, 12); ic = Color.FromArgb(251, 191, 36); mc = Color.FromArgb(254, 243, 199); hc = Color.FromArgb(226, 202, 148); bd = Color.FromArgb(212, 148, 20); ms = 2200; iconKind = "empty";
        } else {
            txt = zh ? "\u2715 \u8BC6\u522B\u5931\u8D25" : "\u2715 Transcription failed";
            bg = Color.FromArgb(84, 18, 24); ic = Color.FromArgb(252, 129, 129); mc = Color.FromArgb(254, 226, 226); hc = Color.FromArgb(238, 178, 182); bd = Color.FromArgb(239, 68, 68); ms = 2600; iconKind = "error";
        }
        if (!string.IsNullOrEmpty(customText)) txt = customText;
    }

    public static Bitmap RenderHudSnapshotBitmap(string state, string customText, int dpi = 192, int workWidth = 1920, int workHeight = 1080, float[] barNorms = null, long elapsedSec = 0L, int maxDurationSec = 0, int warnRemainSec = 0, bool zh = true) {
        string st = (state ?? "error").Trim().ToLowerInvariant();
        string txt, iconKind; Color bg, ic, mc, hc, bd; int ms; bool waveMode;
        ResolveHudVisualStyle(st, zh, customText, out txt, out bg, out ic, out mc, out hc, out bd, out ms, out waveMode, out iconKind);
        using (HudPillForm f = new HudPillForm()) {
            return f.RenderOffscreenBitmap(txt, bg, ic, mc, hc, bd, dpi, new Rectangle(0, 0, workWidth, workHeight), waveMode, iconKind, barNorms, elapsedSec, maxDurationSec, warnRemainSec);
        }
    }

    public static void UpdateHud(string state, string customText, int durationMs, string position = "bottom", int maxDurationSec = 0, int warnRemainSec = 0) {
        try {
            string st = (state ?? "").Trim().ToLowerInvariant();
            if (st == "" || st == "idle" || st == "hide") { if (_hud != null) _hud.HideNoActivate(); return; }
            if (_hud == null || _hud.IsDisposed) _hud = new HudPillForm();
            bool zh = GetHostUiCulture().StartsWith("zh", StringComparison.OrdinalIgnoreCase);
            string txt, iconKind; Color bg, ic, mc, hc, bd; int ms; bool waveMode;
            ResolveHudVisualStyle(st, zh, customText, out txt, out bg, out ic, out mc, out hc, out bd, out ms, out waveMode, out iconKind);
            if (durationMs > 0) ms = durationMs;
            _hud.ShowStatus(txt, bg, ic, mc, hc, bd, ms, GetForegroundWindow(), position, waveMode, iconKind, maxDurationSec, warnRemainSec);
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
    private static volatile float _wasapiLevel = 0f, _extLevel = 0f;
    private static long _extLevelTick = 0L;
    private static volatile bool _wasapiActive = false;

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
                    _wasapiLevel = 0f;
                    int rem = nBytes;
                    while (rem > 0) { int chunk = Math.Min(rem, zeros.Length); dst.Write(zeros, 0, chunk); rem -= chunk; }
                } else {
                    if (scratch == null || scratch.Length < nBytes) scratch = new byte[Math.Max(nBytes, 8192)];
                    Marshal.Copy(pData, scratch, 0, nBytes);
                    dst.Write(scratch, 0, nBytes);
                    _wasapiLevel = ComputePcm16RmsLevel(scratch, nBytes);
                }
            }
            cap.ReleaseBuffer(frames);
            if (cap.GetNextPacketSize(out next) != 0) break;
        }
    }

    public static bool StartRecording(bool loopback, out string error) {
        int dummyBytes; string dummyErr;
        StopRecording(out dummyBytes, out dummyErr);
        _wasapiLevel = 0f; _wasapiActive = true;
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
            _wasapiActive = false; _wasapiLevel = 0f;
            try { stopEvt.Set(); t.Join(300); } catch { }
            try { initEvt.Dispose(); stopEvt.Dispose(); pcm.Dispose(); } catch { }
            error = "wasapi_init_timeout"; return false;
        }
        if (!_recInitOk) {
            _wasapiActive = false; _wasapiLevel = 0f;
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
        if (t == null) { _wasapiActive = false; _wasapiLevel = 0f; error = "not_recording"; return new byte[0]; }
        try { if (stopEvt != null) stopEvt.Set(); t.Join(2500); } catch { }
        _wasapiActive = false; _wasapiLevel = 0f;
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

[VoiceWinShim]::EnableDpiAwareness()

if ($Mode -eq "probe") {
    $hwnd = 0L; $proc = ""; $title = ""; [VoiceWinShim]::QueryForeground([ref]$hwnd, [ref]$proc, [ref]$title)
    Write-JsonLine @{ ok = $true; ps_version = $PSVersionTable.PSVersion.ToString(); wave_in_devs = [VoiceWinShim]::waveInGetNumDevs(); hwnd = $hwnd; process = $proc; title = $title; ui_culture = [VoiceWinShim]::GetHostUiCulture(); dpi = [VoiceWinShim]::GetTargetDpi([IntPtr]$hwnd) }
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
                    $maxDur = if ($null -ne $cmdObj.max_duration_sec) { [int]$cmdObj.max_duration_sec } else { 0 }
                    $warnRem = if ($null -ne $cmdObj.warn_remain_sec) { [int]$cmdObj.warn_remain_sec } else { 0 }
                    [VoiceWinShim]::UpdateHud([string]$cmdObj.state, [string]$cmdObj.text, $dms, [string]$cmdObj.position, $maxDur, $warnRem)
                    Write-JsonLine @{ type = "reply"; reply = "hud"; seq_id = $seq; ok = $true }
                } elseif ($op -eq "hud_level") {
                    $lvl = if ($null -ne $cmdObj.level) { [float]$cmdObj.level } else { 0.0 }
                    [VoiceWinShim]::SetExternalAudioLevel($lvl)
                    if ($seq -gt 0) { Write-JsonLine @{ type = "reply"; reply = "hud_level"; seq_id = $seq; ok = $true } }
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
                if ($seq -gt 0 -or $op -ne "hud_level") { Write-JsonLine @{ type = "reply"; reply = $op; seq_id = $seq; ok = $false; error = [string]$_.Exception.Message } }
            }
        }
    }
} finally {
    [VoiceWinShim]::StopHook()
}
