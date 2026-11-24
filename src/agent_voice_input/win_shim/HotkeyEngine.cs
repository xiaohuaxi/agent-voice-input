using System;
using System.Collections.Generic;
using System.Windows.Forms;

public struct ShimEvent { public string Kind, Process, Title, Reason; public uint Vk, Scan; public bool Extended, IsDown, Tainted; public long Hwnd, RecGen; public ulong TsMs; public int GapMs, HoldMs; }

public struct HotkeyAction {
    public string Kind;
    public uint Vk, Scan;
    public bool Extended, IsDown, Tainted;
    public int HoldMs;
}

public struct HotkeyStepResult {
    public List<HotkeyAction> Events;
    public bool ArmTimers;
    public bool CancelTimers;
    public ulong TimerGen;
    public int HintDelayMs;
    public int CancelDelayMs;
    public bool ShowHoldPreview;
    public bool ClearHoldPreview;
    public bool ArmPreviewSafetyClear;
}

public sealed class WinHotkeyEngine {
    private readonly uint _targetVk;
    private readonly uint _ignoredCompanionVk;
    private readonly bool _isPtt;
    private readonly bool _keyTest;
    private readonly ulong _tapMaxMs;
    private readonly ulong _holdCancelMs;
    private readonly ulong _holdHintMs;
    private readonly ulong _debounceMs;

    private bool _targetDown = false;
    private bool _pttActive = false;
    private bool _chordTainted = false;
    private bool _holdFired = false;
    private bool _holdHintShown = false;
    private bool _hasFiredOnce = false;
    private ulong _holdGen = 0UL;
    private ulong _downTick = 0UL;
    private ulong _lastFireTick = 0UL;
    private uint _downScan = 0u;
    private bool _downExt = false;
    private readonly HashSet<uint> _otherDown = new HashSet<uint>();

    public WinHotkeyEngine(uint targetVk, bool isPtt, int tapMaxMs, int holdCancelMs, int holdHintMs, int debounceMs, bool keyTest) {
        _targetVk = targetVk;
        _ignoredCompanionVk = (targetVk == 0xA2u || targetVk == 0xA3u) ? 0x11u : ((targetVk == 0xA4u || targetVk == 0xA5u) ? 0x12u : ((targetVk == 0xA0u || targetVk == 0xA1u) ? 0x10u : 0u));
        _isPtt = isPtt;
        _keyTest = keyTest;
        _tapMaxMs = (ulong)Math.Max(50, tapMaxMs);
        _debounceMs = (ulong)Math.Max(0, debounceMs);
        if (holdCancelMs <= 0) {
            _holdCancelMs = 0UL;
            _holdHintMs = 0UL;
        } else {
            ulong hc = (ulong)holdCancelMs;
            if (hc <= _tapMaxMs) hc = _tapMaxMs + 300UL;
            _holdCancelMs = hc;
            if (holdHintMs <= 0 || _holdCancelMs <= 1UL) {
                _holdHintMs = 0UL;
            } else {
                ulong maxHint = _holdCancelMs / 2UL;
                _holdHintMs = Math.Min((ulong)holdHintMs, maxHint);
            }
        }
    }

    public uint TargetVk { get { return _targetVk; } }
    public bool TargetDown { get { return _targetDown; } }
    public bool PttActive { get { return _pttActive; } }
    public bool ChordTainted { get { return _chordTainted; } }
    public bool HoldFired { get { return _holdFired; } }
    public bool HoldHintShown { get { return _holdHintShown; } }
    public ulong HoldGen { get { return _holdGen; } }
    public int OtherDownCount { get { return _otherDown.Count; } }
    public ulong TapMaxMs { get { return _tapMaxMs; } }
    public ulong HoldCancelMs { get { return _holdCancelMs; } }
    public ulong HoldHintMs { get { return _holdHintMs; } }

    private bool DebouncePassed(ulong nowMs) {
        return !_hasFiredOnce || (nowMs >= _lastFireTick && (nowMs - _lastFireTick) >= _debounceMs);
    }

    private void PruneStaleOtherKeys(Func<uint, bool> isKeyPhysicallyDown) {
        if (_otherDown.Count == 0 || isKeyPhysicallyDown == null) return;
        List<uint> stale = null;
        foreach (uint k in _otherDown) {
            if (!isKeyPhysicallyDown(k)) {
                if (stale == null) stale = new List<uint>();
                stale.Add(k);
            }
        }
        if (stale != null) {
            for (int i = 0; i < stale.Count; i++) _otherDown.Remove(stale[i]);
        }
    }

    private static void AddEvent(ref HotkeyStepResult res, string kind, uint vk, uint scan, bool ext, bool isDown, bool tainted, int holdMs = 0) {
        if (res.Events == null) res.Events = new List<HotkeyAction>(2);
        res.Events.Add(new HotkeyAction { Kind = kind, Vk = vk, Scan = scan, Extended = ext, IsDown = isDown, Tainted = tainted, HoldMs = holdMs });
    }

    public HotkeyStepResult ProcessKeyEvent(uint vk, uint scan, bool ext, bool isDown, bool isUp, ulong nowMs, Func<uint, bool> isKeyPhysicallyDown = null) {
        HotkeyStepResult res = new HotkeyStepResult();
        PruneStaleOtherKeys(isKeyPhysicallyDown);
        if (_keyTest && (isDown || isUp)) {
            AddEvent(ref res, "key_event", vk, scan, ext, isDown, _chordTainted);
        }
        if (vk == _targetVk) {
            if (isDown && !_targetDown) {
                _targetDown = true;
                _downTick = nowMs;
                _downScan = scan;
                _downExt = ext;
                _holdFired = false;
                _holdHintShown = false;
                _holdGen++;
                _chordTainted = (_otherDown.Count > 0);
                if (_isPtt) {
                    if (!_chordTainted && DebouncePassed(nowMs)) {
                        _lastFireTick = nowMs;
                        _hasFiredOnce = true;
                        _pttActive = true;
                        AddEvent(ref res, "ptt_start", vk, scan, ext, true, false);
                    }
                } else if (!_chordTainted && _holdCancelMs > 0UL) {
                    res.ArmTimers = true;
                    res.TimerGen = _holdGen;
                    res.HintDelayMs = (int)_holdHintMs;
                    res.CancelDelayMs = (int)_holdCancelMs;
                }
            } else if (isUp && _targetDown) {
                _holdGen++;
                res.CancelTimers = true;
                bool wasHoldFired = _holdFired;
                bool wasHintShown = _holdHintShown;
                if (_isPtt) {
                    if (_pttActive && !_chordTainted) {
                        _lastFireTick = nowMs;
                        _hasFiredOnce = true;
                        AddEvent(ref res, "ptt_stop", vk, scan, ext, false, false);
                    }
                } else if (!wasHoldFired && !_chordTainted && nowMs >= _downTick && DebouncePassed(nowMs)) {
                    ulong held = nowMs - _downTick;
                    if (held <= _tapMaxMs) {
                        _lastFireTick = nowMs;
                        _hasFiredOnce = true;
                        AddEvent(ref res, "toggle", vk, scan, ext, false, false);
                    } else if (_holdCancelMs > 0UL && held >= _holdCancelMs) {
                        _lastFireTick = nowMs;
                        _hasFiredOnce = true;
                        wasHoldFired = true;
                        AddEvent(ref res, "hold_cancel", vk, scan, ext, false, false, (int)held);
                    }
                }
                if (wasHintShown) {
                    if (wasHoldFired) res.ArmPreviewSafetyClear = true;
                    else res.ClearHoldPreview = true;
                }
                _targetDown = false;
                _pttActive = false;
                _chordTainted = false;
                _holdFired = false;
                _holdHintShown = false;
            }
        } else if (vk != _ignoredCompanionVk) {
            if (isDown) _otherDown.Add(vk);
            else if (isUp) _otherDown.Remove(vk);
            if (_targetDown && (isDown || isUp)) {
                _chordTainted = true;
                _holdGen++;
                res.CancelTimers = true;
                if (_holdHintShown) {
                    _holdHintShown = false;
                    res.ClearHoldPreview = true;
                }
                if (_isPtt && _pttActive) {
                    _pttActive = false;
                    AddEvent(ref res, "ptt_cancel", vk, scan, ext, isDown, true);
                }
            }
        }
        return res;
    }

    public HotkeyStepResult OnHoldHintTimeout(ulong gen, ulong nowMs, Func<uint, bool> isKeyPhysicallyDown = null) {
        HotkeyStepResult res = new HotkeyStepResult();
        if (_isPtt || !_targetDown || _chordTainted || _holdFired || gen != _holdGen || _holdCancelMs == 0UL || _holdHintMs == 0UL) {
            return res;
        }
        PruneStaleOtherKeys(isKeyPhysicallyDown);
        if (isKeyPhysicallyDown != null && !isKeyPhysicallyDown(_targetVk)) {
            bool hadHint = _holdHintShown;
            _targetDown = false; _pttActive = false; _chordTainted = false; _holdFired = false; _holdHintShown = false;
            _holdGen++;
            res.CancelTimers = true;
            if (hadHint) res.ClearHoldPreview = true;
            return res;
        }
        if (_otherDown.Count > 0) {
            _chordTainted = true;
            _holdGen++;
            res.CancelTimers = true;
            return res;
        }
        ulong held = (nowMs >= _downTick) ? (nowMs - _downTick) : 0UL;
        if (held < _holdHintMs || held >= _holdCancelMs) return res;
        _holdHintShown = true;
        res.ShowHoldPreview = true;
        return res;
    }

    public HotkeyStepResult OnHoldCancelTimeout(ulong gen, ulong nowMs, Func<uint, bool> isKeyPhysicallyDown = null) {
        HotkeyStepResult res = new HotkeyStepResult();
        if (_isPtt || !_targetDown || _chordTainted || _holdFired || gen != _holdGen || _holdCancelMs == 0UL) {
            return res;
        }
        PruneStaleOtherKeys(isKeyPhysicallyDown);
        if (isKeyPhysicallyDown != null && !isKeyPhysicallyDown(_targetVk)) {
            bool hadHint = _holdHintShown;
            _targetDown = false; _pttActive = false; _chordTainted = false; _holdFired = false; _holdHintShown = false;
            _holdGen++;
            res.CancelTimers = true;
            if (hadHint) res.ClearHoldPreview = true;
            return res;
        }
        if (_otherDown.Count > 0) {
            _chordTainted = true;
            _holdGen++;
            res.CancelTimers = true;
            if (_holdHintShown) { _holdHintShown = false; res.ClearHoldPreview = true; }
            return res;
        }
        ulong held = (nowMs >= _downTick) ? (nowMs - _downTick) : 0UL;
        if (held < _holdCancelMs) return res;
        if (!DebouncePassed(nowMs)) {
            if (_holdHintShown) { _holdHintShown = false; res.ClearHoldPreview = true; }
            return res;
        }
        _holdFired = true;
        _lastFireTick = nowMs;
        _hasFiredOnce = true;
        _holdGen++;
        res.CancelTimers = true;
        if (_holdHintShown) {
            _holdHintShown = false;
            res.ArmPreviewSafetyClear = true;
        }
        AddEvent(ref res, "hold_cancel", _targetVk, _downScan, _downExt, true, false, (int)held);
        return res;
    }

    public HotkeyStepResult ResetForPowerResume() {
        HotkeyStepResult res = new HotkeyStepResult();
        bool wasPttActive = (_isPtt && _pttActive);
        uint pttScan = _downScan;
        bool pttExt = _downExt;
        _holdGen++;
        _targetDown = false;
        _pttActive = false;
        _chordTainted = false;
        _holdFired = false;
        _holdHintShown = false;
        _otherDown.Clear();
        res.CancelTimers = true;
        res.ClearHoldPreview = true;
        if (wasPttActive) {
            AddEvent(ref res, "ptt_stop", _targetVk, pttScan, pttExt, false, false);
        }
        return res;
    }
}

public sealed class PowerResumeDebouncer {
    public const int WM_POWERBROADCAST = 0x0218;
    public const int PBT_APMRESUMESUSPEND = 0x0007;
    public const int PBT_APMRESUMEAUTOMATIC = 0x0012;
    public const ulong DefaultDebounceMs = 2000UL;

    private readonly ulong _debounceMs;
    private bool _hasEmitted = false;
    private ulong _lastEmitMs = 0UL;

    public PowerResumeDebouncer(ulong debounceMs = DefaultDebounceMs) {
        _debounceMs = debounceMs;
    }

    public static bool IsResumeWParam(IntPtr wParam) {
        long val = wParam.ToInt64();
        return val == PBT_APMRESUMEAUTOMATIC || val == PBT_APMRESUMESUSPEND;
    }

    public bool ShouldEmit(ulong nowMs) {
        if (!_hasEmitted || nowMs < _lastEmitMs || (nowMs - _lastEmitMs) >= _debounceMs) {
            _hasEmitted = true;
            _lastEmitMs = nowMs;
            return true;
        }
        return false;
    }
}

internal sealed class PowerBroadcastWindow : NativeWindow {
    public PowerBroadcastWindow() {
        CreateParams cp = new CreateParams();
        cp.Caption = "AviPowerMsgWin";
        cp.Style = 0;
        cp.ExStyle = 0x08000080; // WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW (hidden top-level broadcast receiver)
        cp.Parent = IntPtr.Zero;
        CreateHandle(cp);
    }

    protected override void WndProc(ref Message m) {
        if (m.Msg == PowerResumeDebouncer.WM_POWERBROADCAST && PowerResumeDebouncer.IsResumeWParam(m.WParam)) {
            VoiceWinShim.OnPowerResume(VoiceWinShim.GetTickCount64());
        }
        base.WndProc(ref m);
    }
}
