using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Windows.Forms;

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
    private static readonly Color RecBorderColor = Color.FromArgb(174, 232, 88, 102);
    private static readonly Color RecWaveActiveColor = Color.FromArgb(255, 255, 104, 112);
    private static readonly Color RecWaveQuietColor = Color.FromArgb(132, 244, 104, 114);
    private static readonly Color RecMainColor = Color.FromArgb(248, 250, 252);
    private static readonly Color RecHintColor = Color.FromArgb(200, 178, 182);
    private static readonly Color WarnBgColor = Color.FromArgb(72, 38, 12);
    private static readonly Color WarnBorderColor = Color.FromArgb(184, 245, 158, 11);
    private static readonly Color WarnWaveActiveColor = Color.FromArgb(255, 251, 191, 36);
    private static readonly Color WarnWaveQuietColor = Color.FromArgb(132, 251, 191, 36);
    private static readonly Color WarnMainColor = Color.FromArgb(254, 252, 232);
    private static readonly Color WarnHintColor = Color.FromArgb(255, 251, 191, 36);
    private static readonly Color RecBadgeBgColor = Color.FromArgb(36, 255, 255, 255);
    private static readonly Color RecBadgeBorderColor = Color.FromArgb(72, 255, 214, 220);
    private static readonly Color RecBadgeTextColor = Color.FromArgb(255, 255, 241, 242);
    private static readonly Color WarnBadgeBgColor = Color.FromArgb(38, 254, 243, 199);
    private static readonly Color WarnBadgeBorderColor = Color.FromArgb(92, 251, 191, 36);
    private static readonly Color WarnBadgeTextColor = Color.FromArgb(255, 254, 243, 199);
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
    private bool _isWaveRecording = false, _shown = false, _holdPreview = false;
    private string _text = "", _position = "bottom", _iconKind = "", _stateName = "", _lastRenderedHint = "";
    private Color _bgColor = DefaultBgColor, _iconColor = Color.White, _mainColor = Color.White, _hintColor = RecHintColor, _borderColor = RecBorderColor;
    private IntPtr _fgHwnd = IntPtr.Zero;
    private Rectangle _anchoredWorkArea = Rectangle.Empty;
    private int _lastX = int.MinValue, _lastY = int.MinValue, _lastW = 0, _lastH = 0;
    private int _lastAutoHideMs = 0;
    private int _showUpdates = 0;

    public bool IsToast { get { return _isToast; } }
    public bool IsShown { get { return _shown; } }
    public string CurrentText { get { return _text; } }
    public string CurrentStateName { get { return _stateName; } }
    public string LastRenderedHint { get { return _lastRenderedHint; } }
    public bool HoldCancelPreviewActive { get { return _holdPreview; } }
    public bool CanShowHoldCancelPreview { get { return _shown && !_isToast && !IsDisposed && VoiceWinShim.IsCancellableMainHudState(_stateName); } }
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
        float semiPx = Math.Max(10f, (float)Math.Round(11.5f * dpi / 96.0f));
        try {
            using (FontFamily ff = new FontFamily("Segoe UI Semibold")) {
                FontStyle st = ff.IsStyleAvailable(FontStyle.Regular) ? FontStyle.Regular : FontStyle.Bold;
                owns = true;
                return new Font(ff, semiPx, st, GraphicsUnit.Pixel);
            }
        } catch { }
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
        _hideTimer.Stop(); _animTimer.Stop(); _isWaveRecording = false; _shown = false; _holdPreview = false;
        _stateName = ""; _lastRenderedHint = "";
        _maxDurationSec = 0; _warnRemainSec = 0; _bgCount = 0;
        if (IsHandleCreated) ShowWindow(Handle, 0);
    }
    public void SetHoldCancelPreview(bool active, bool headlessNoShow = false) {
        bool target = active && CanShowHoldCancelPreview;
        if (_holdPreview == target) return;
        _holdPreview = target;
        if (!_shown || IsDisposed) return;
        if (headlessNoShow) {
            int dx, dy; HudLayoutMetrics dm;
            Rectangle wa = (_anchoredWorkArea.Width > 0 && _anchoredWorkArea.Height > 0) ? _anchoredWorkArea : new Rectangle(0, 0, 1920, 1080);
            using (Bitmap tmp = BuildPillPArgbBitmap(wa, out dx, out dy, out dm)) { }
        } else {
            RenderPill(false);
        }
    }
    public void ShowStatus(string text, Color bgColor, Color iconColor, Color mainColor, Color hintColor, Color border, int autoHideMs, IntPtr fgHwnd, string position, bool waveMode = false, string iconKind = "", int maxDurationSec = 0, int warnRemainSec = 0, Rectangle? workAreaOverride = null, int dpiOverride = 0, int bgCount = 0, bool headlessNoShow = false, string stateName = "", bool preserveHoldPreview = false) {
        _hideTimer.Stop();
        _stateName = (stateName ?? "").Trim().ToLowerInvariant();
        _holdPreview = preserveHoldPreview && !_isToast && VoiceWinShim.IsCancellableMainHudState(_stateName);
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

    public Bitmap RenderOffscreenBitmapWithMetrics(string text, Color bgColor, Color iconColor, Color mainColor, Color hintColor, Color border, int dpi, Rectangle workArea, bool waveMode, string iconKind, float[] barNormsOverride, long elapsedSec, int maxDurationSec, int warnRemainSec, out HudLayoutMetrics layout, string position = "bottom", int bgCount = 0, bool holdCancelPreview = false, string stateName = "") {
        _text = text ?? ""; _bgColor = bgColor; _iconColor = iconColor; _mainColor = mainColor; _hintColor = hintColor; _borderColor = border;
        _iconKind = iconKind ?? ""; _position = position ?? "bottom"; _isWaveRecording = waveMode && !_isToast;
        _stateName = (stateName ?? "").Trim().ToLowerInvariant();
        _holdPreview = holdCancelPreview && !_isToast && (string.IsNullOrEmpty(_stateName) || VoiceWinShim.IsCancellableMainHudState(_stateName));
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
        int padR = _isToast ? ScalePx(12) : ((_isWaveRecording && _bgCount > 0) ? ScalePx(10) : ScalePx(15));
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
        Color effBadgeBg = RecBadgeBgColor, effBadgeBorder = RecBadgeBorderColor, effBadgeText = RecBadgeTextColor;

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
                effBadgeBg = WarnBadgeBgColor;
                effBadgeBorder = WarnBadgeBorderColor;
                effBadgeText = WarnBadgeTextColor;
                effHint = VoiceWinShim.FormatCountdownHint((int)remainSec, zhHint);
            } else {
                effBg = RecBgColor;
                effBorder = RecBorderColor;
                effWaveActive = RecWaveActiveColor;
                effWaveQuiet = RecWaveQuietColor;
                effMain = RecMainColor;
                effHintCol = RecHintColor;
                effBadgeBg = RecBadgeBgColor;
                effBadgeBorder = RecBadgeBorderColor;
                effBadgeText = RecBadgeTextColor;
            }
            if (_holdPreview) {
                effHint = VoiceWinShim.FormatHoldCancelHint(zhHint);
                effHintCol = WarnHintColor;
            } else {
                effHint = VoiceWinShim.StripOuterParens(effHint);
            }
            durStr = VoiceWinShim.FormatRecordingDuration(elapsedSec);
            string slotTpl = VoiceWinShim.DurationSlotTemplate(elapsedSec);
            Size actDurSz = TextRenderer.MeasureText(durStr, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
            Size tplDurSz = TextRenderer.MeasureText(slotTpl, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
            durMeasuredW = actDurSz.Width;
            slotW = Math.Max(actDurSz.Width, tplDurSz.Width);
            if (effHint.Length > 0) {
                hintSz = TextRenderer.MeasureText(effHint, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags);
                hintMeasuredW = hintSz.Width;
                if (isWarn && !_holdPreview) {
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
                int badgePadX = ScalePx(6);
                badgeSlotW = Math.Max(ScalePx(22), badgeTextSz.Width + badgePadX * 2);
                badgeH = Math.Max(ScalePx(19), badgeTextSz.Height + ScalePx(4));
            }
            gap1 = ScalePx(9);
            gap2 = (effHint.Length > 0) ? ScalePx(9) : 0;
            badgeGap = (badgeSlotW > 0) ? ScalePx(9) : 0;
            totalTextW = slotW + gap1 + waveBoxW + gap2 + hintSlotW + badgeGap + badgeSlotW;
            textH = Math.Max(Math.Max(actDurSz.Height, hintSz.Height), badgeH);
        } else {
            if (_holdPreview) {
                bool zhHint = (_text.Length > 0) ? HasCjk(_text) : _cjkFont;
                hintSeg = "(" + VoiceWinShim.FormatHoldCancelHint(zhHint) + ")";
                spBeforeHint = true;
                effHintCol = WarnHintColor;
                effHint = hintSeg;
            }
            iconBox = (iconSeg.Length > 0 || (_isToast && !string.IsNullOrEmpty(_iconKind))) ? ScalePx(11) : 0;
            mainSz = (mainSeg.Length > 0) ? TextRenderer.MeasureText(mainSeg, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags) : Size.Empty;
            hintSz = (hintSeg.Length > 0) ? TextRenderer.MeasureText(hintSeg, _font, new Size(int.MaxValue, int.MaxValue), MeasureFlags) : Size.Empty;
            iconGap = (iconBox > 0 && (mainSeg.Length > 0 || hintSeg.Length > 0)) ? ScalePx(6) : 0;
            hintGap = (hintSeg.Length > 0 && mainSeg.Length > 0) ? ScalePx(spBeforeHint ? 4 : 2) : 0;
            totalTextW = iconBox + iconGap + mainSz.Width + hintGap + hintSz.Width;
            textH = Math.Max(iconBox, Math.Max(mainSz.Height, hintSz.Height));
        }
        _lastRenderedHint = effHint;
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
                            using (SolidBrush bFill = new SolidBrush(effBadgeBg))
                            using (Pen bPen = new Pen(effBadgeBorder, Math.Max(1.0f, 1.0f * dpiScale))) {
                                g.FillPath(bFill, bPath);
                                g.DrawPath(bPen, bPath);
                            }
                            Font bf = _badgeFont ?? _font;
                            int badgeDy = -Math.Max(1, (int)Math.Round(0.5f * dpiScale));
                            TextRenderer.DrawText(g, badgeStr, bf, new Rectangle(bRc.X - 1, bRc.Y + badgeDy, bRc.Width + 2, bRc.Height), effBadgeText, DurDrawFlags);
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
                float penW = Math.Max(1.0f, 1.0f * dpiScale);
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

public static partial class VoiceWinShim {
    private static HudPillForm _hud;
    private static HudPillForm _toastHud;
    private static bool _headlessHudMode = false;
    private static Rectangle _headlessDefaultWa = Rectangle.Empty;
    private static int _headlessDefaultDpi = 96;
    private static string _hotkeyLabel = "\u53F3 Ctrl";
    private static System.Windows.Forms.Timer _holdPreviewSafetyTimer = null;

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

    public static bool IsCancellableMainHudState(string st) {
        string s = (st ?? "").Trim().ToLowerInvariant();
        return s == "recording" || s == "recognizing" || s == "processing" || s == "refining";
    }

    public static string FormatHoldCancelHint(bool zh) {
        return zh ? "\u7EE7\u7EED\u6309\u4F4F\u53D6\u6D88\u2026" : "keep holding to cancel\u2026";
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
        Color toastBg = Color.FromArgb(28, 28, 30);
        Color toastMain = Color.FromArgb(244, 245, 248);
        Color toastHint = Color.FromArgb(190, 195, 208);
        Color toastAmber = Color.FromArgb(251, 191, 36);
        Color toastAmberBorder = Color.FromArgb(148, 251, 191, 36);
        Color toastCoral = Color.FromArgb(248, 113, 113);
        Color toastCoralBorder = Color.FromArgb(156, 248, 113, 113);
        if (st == "recording") {
            string hkEn = (_hotkeyLabel == "\u53F3 Ctrl") ? "Right Ctrl" : _hotkeyLabel.Trim();
            txt = zh
                ? ("\u25CF \u5F55\u97F3\u4E2D\u2026 (" + (_isPtt ? "\u677E\u5F00" : "\u6309") + _hotkeyLabel + " \u7ED3\u675F)")
                : ("\u25CF Listening\u2026 (" + (_isPtt ? "release " : "press ") + hkEn + " to end)");
            bg = Color.FromArgb(64, 18, 26); ic = Color.FromArgb(255, 104, 112); mc = Color.FromArgb(248, 250, 252); hc = Color.FromArgb(200, 178, 182); bd = Color.FromArgb(174, 232, 88, 102);
            string trimmedCustom = (customText ?? "").TrimStart();
            waveMode = !isToast && (string.IsNullOrEmpty(trimmedCustom) || trimmedCustom.StartsWith("\u25CF ", StringComparison.Ordinal));
        } else if (st == "recognizing" || st == "processing") {
            txt = zh ? "\u25D0 \u8BED\u97F3\u8BC6\u522B\u2026" : "\u25D0 Transcribing\u2026";
            bg = Color.FromArgb(16, 40, 72); ic = Color.FromArgb(56, 189, 248); mc = Color.FromArgb(244, 248, 252); hc = Color.FromArgb(176, 198, 222); bd = Color.FromArgb(166, 56, 189, 248); iconKind = "recognizing";
        } else if (st == "refining") {
            txt = zh ? "\u25C8 \u6574\u7406\u7CBE\u4FEE\u2026" : "\u25C8 Refining\u2026";
            bg = Color.FromArgb(46, 22, 76); ic = Color.FromArgb(208, 156, 255); mc = Color.FromArgb(248, 245, 255); hc = Color.FromArgb(198, 182, 226); bd = Color.FromArgb(166, 192, 132, 252); iconKind = "refining";
        } else if (st == "done") {
            txt = zh ? "\u2713 \u5DF2\u586B\u5165" : "\u2713 Inserted";
            bg = Color.FromArgb(14, 54, 36); ic = Color.FromArgb(74, 222, 128); mc = Color.FromArgb(242, 253, 246); hc = Color.FromArgb(174, 218, 192); bd = Color.FromArgb(158, 74, 222, 128); ms = 900; iconKind = "done";
        } else if (st == "copied") {
            txt = zh ? "\u2713 \u5DF2\u590D\u5236(\u7A97\u53E3\u5DF2\u5207\u6362)" : "\u2713 Copied (window switched)";
            bg = Color.FromArgb(28, 32, 82); ic = Color.FromArgb(144, 156, 255); mc = Color.FromArgb(240, 244, 255); hc = Color.FromArgb(186, 196, 238); bd = Color.FromArgb(174, 129, 140, 248); ms = 5000; iconKind = "done";
        } else if (st == "empty") {
            txt = zh ? "\u25CB \u672A\u68C0\u6D4B\u5230\u8BED\u97F3" : "\u25CB No speech detected";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 2200; iconKind = "empty_ring";
        } else if (st == "cancelled") {
            txt = zh ? "\u25CB \u5DF2\u53D6\u6D88" : "\u25CB Cancelled";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 1600; iconKind = "empty_ring";
        } else if (st == "mic_unavailable") {
            txt = zh ? "! \u9EA6\u514B\u98CE\u6682\u5FD9\uFF0C\u8BF7\u518D\u8BD5\u4E00\u6B21" : "! Microphone busy, try again";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 2600; iconKind = "warning";
        } else if (st == "mic_no_stream") {
            txt = zh ? "! \u9EA6\u514B\u98CE\u65E0\u97F3\u9891\u6D41\uFF0C\u8BF7\u68C0\u67E5\u8033\u673A/\u8BBE\u5907" : "! No mic audio stream, check headset";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 2600; iconKind = "warning";
        } else if (st == "audio_recovered") {
            txt = zh ? "! \u9EA6\u514B\u98CE\u77ED\u6682\u4E2D\u65AD\uFF0C\u5DF2\u81EA\u52A8\u6062\u590D" : "! Mic briefly interrupted, resumed";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 2400; iconKind = "warning";
        } else if (st == "partial_asr") {
            txt = zh ? "! \u90E8\u5206\u8BED\u97F3\u672A\u8BC6\u522B\uFF0C\u53EF\u91CD\u8BD5" : "! Partial speech missed, retry available";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 3200; iconKind = "warning";
        } else if (st == "partial_asr_unsaved") {
            txt = zh ? "! \u90E8\u5206\u8BED\u97F3\u672A\u8BC6\u522B" : "! Partial speech missed";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 3200; iconKind = "warning";
        } else if (st == "partial_asr_copied") {
            txt = zh ? "! \u5DF2\u590D\u5236\uFF0C\u90E8\u5206\u672A\u8BC6\u522B\u53EF\u91CD\u8BD5" : "! Copied; partial missed, can retry";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 3200; iconKind = "warning";
        } else if (st == "partial_asr_copied_unsaved") {
            txt = zh ? "! \u5DF2\u590D\u5236\uFF0C\u90E8\u5206\u8BED\u97F3\u672A\u8BC6\u522B" : "! Copied; partial speech missed";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 3200; iconKind = "warning";
        } else if (st == "asr_empty_retry") {
            txt = zh ? "! \u8BC6\u522B\u4E3A\u7A7A\uFF0C\u5DF2\u5B58\u5F55\u97F3\u53EF\u91CD\u8BD5" : "! Empty transcript, audio saved for retry";
            bg = toastBg; ic = toastAmber; mc = toastMain; hc = toastHint; bd = toastAmberBorder; ms = 3200; iconKind = "warning";
        } else if (st == "asr_empty") {
            txt = zh ? "\u2715 \u8BC6\u522B\u4E3A\u7A7A" : "\u2715 Empty transcript";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoralBorder; ms = 2600; iconKind = "cross";
        } else if (st == "paste_failed_retry") {
            txt = zh ? "\u2715 \u586B\u5165\u5931\u8D25\uFF0C\u5DF2\u5B58\u5F55\u97F3\u53EF\u91CD\u8BD5" : "\u2715 Insert failed, audio saved for retry";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoralBorder; ms = 3200; iconKind = "cross";
        } else if (st == "no_mic") {
            txt = zh ? "\u2715 \u65E0\u53EF\u7528\u9EA6\u514B\u98CE" : "\u2715 No microphone found";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoralBorder; ms = 2200; iconKind = "cross";
        } else if (st == "mic_permission") {
            txt = zh ? "\u2715 \u9EA6\u514B\u98CE\u6743\u9650\u672A\u5F00\u542F" : "\u2715 Microphone access denied";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoralBorder; ms = 3200; iconKind = "cross";
        } else if (st == "mic_disconnected") {
            txt = zh ? "\u2715 \u9EA6\u514B\u98CE\u5DF2\u65AD\u5F00" : "\u2715 Microphone disconnected";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoralBorder; ms = 2200; iconKind = "cross";
        } else if (st == "paste_failed") {
            txt = zh ? "\u2715 \u586B\u5165\u5931\u8D25" : "\u2715 Failed to insert text";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoralBorder; ms = 2600; iconKind = "cross";
        } else {
            txt = zh ? "\u2715 \u8BC6\u522B\u5931\u8D25" : "\u2715 Transcription failed";
            bg = toastBg; ic = toastCoral; mc = toastMain; hc = toastHint; bd = toastCoralBorder; ms = 2600; iconKind = "cross";
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

    public static Bitmap RenderHudSnapshotWithMetrics(string state, string customText, int dpi, int workWidth, int workHeight, float[] barNorms, out HudLayoutMetrics layout, long elapsedSec = 0L, int maxDurationSec = 0, int warnRemainSec = 0, bool zh = true, bool isToast = false, int bgCount = 0, bool holdCancelPreview = false) {
        string st = (state ?? "error").Trim().ToLowerInvariant();
        string txt, iconKind; Color bg, ic, mc, hc, bd; int ms; bool waveMode;
        ResolveHudVisualStyle(st, zh, customText, isToast, out txt, out bg, out ic, out mc, out hc, out bd, out ms, out waveMode, out iconKind);
        using (HudPillForm f = new HudPillForm(isToast)) {
            return f.RenderOffscreenBitmapWithMetrics(txt, bg, ic, mc, hc, bd, dpi, new Rectangle(0, 0, workWidth, workHeight), waveMode, iconKind, barNorms, elapsedSec, maxDurationSec, warnRemainSec, out layout, "bottom", bgCount, holdCancelPreview, st);
        }
    }

    private static void StopHoldPreviewSafetyTimer() {
        if (_holdPreviewSafetyTimer != null) { try { _holdPreviewSafetyTimer.Stop(); } catch { } }
    }

    private static void ArmHoldPreviewSafetyTimer() {
        if (_headlessHudMode) return;
        if (_holdPreviewSafetyTimer == null) {
            _holdPreviewSafetyTimer = new System.Windows.Forms.Timer();
            _holdPreviewSafetyTimer.Interval = 400;
            _holdPreviewSafetyTimer.Tick += (s, e) => {
                StopHoldPreviewSafetyTimer();
                if (_hud != null && !_hud.IsDisposed && _hud.HoldCancelPreviewActive) {
                    _hud.SetHoldCancelPreview(false, _headlessHudMode);
                }
            };
        }
        _holdPreviewSafetyTimer.Stop();
        _holdPreviewSafetyTimer.Interval = 400;
        _holdPreviewSafetyTimer.Start();
    }

    public static void UpdateHud(string state, string customText, int durationMs, string position = "bottom", int maxDurationSec = 0, int warnRemainSec = 0, string layer = "", bool? clearMain = null, bool? clearToast = null, int bgCount = 0) {
        try {
            string st = (state ?? "").Trim().ToLowerInvariant();
            if (st == "" || st == "idle" || st == "hide") {
                StopHoldPreviewSafetyTimer();
                if (_hud != null) _hud.HideNoActivate();
                if (clearToast == true && _toastHud != null) _toastHud.HideNoActivate();
                return;
            }
            if (st == "hide_toast") {
                if (_toastHud != null) _toastHud.HideNoActivate();
                return;
            }
            if (st == "hide_all") {
                StopHoldPreviewSafetyTimer();
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
                StopHoldPreviewSafetyTimer();
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
                bool keepHoldPreview = (_hotkeyEngine != null && _hotkeyEngine.HoldHintShown && !_hotkeyEngine.HoldFired && IsCancellableMainHudState(st));
                _hud.ShowStatus(txt, bg, ic, mc, hc, bd, ms, fg, position, waveMode, iconKind, maxDurationSec, warnRemainSec, inheritWa, inheritDpi, bgCount, headless, st, keepHoldPreview);
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
                if (clearMain == true) {
                    StopHoldPreviewSafetyTimer();
                    if (_hud != null) _hud.HideNoActivate();
                }
                if (_toastHud == null || _toastHud.IsDisposed) _toastHud = new HudPillForm(true);
                _toastHud.ShowStatus(txt, bg, ic, mc, hc, bd, ms, fg, position, false, iconKind, 0, 0, anchorWa, anchorDpi, 0, headless, st);
            }
        } catch { }
    }
}
