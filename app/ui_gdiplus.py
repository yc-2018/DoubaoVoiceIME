#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基于 Windows 原生 GDI+ 与 UpdateLayeredWindow 的高颜值抗锯齿悬浮胶囊与悬浮球。"""

import ctypes
import math
import threading
import time
import traceback
from ctypes import wintypes

import ui_themes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdiplus = ctypes.WinDLL("gdiplus")
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except OSError:
    dwmapi = None

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

# --- Win32 structures ---

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("style", ctypes.c_uint32),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", ctypes.c_void_p),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", ctypes.c_uint32),
    ]


# --- GDI+ structures & API ---

class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
    ]


class PointF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float)]


_gp_lock = threading.Lock()
_gp_token = ctypes.c_void_p(0)
_gp_ready = False


def _init_apis():
    p, i, u, f = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_float

    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = ctypes.c_ushort
    user32.CreateWindowExW.argtypes = [
        u, wintypes.LPCWSTR, wintypes.LPCWSTR, u,
        i, i, i, i, p, p, p, p
    ]
    user32.CreateWindowExW.restype = p
    user32.DefWindowProcW.argtypes = [p, u, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.DefWindowProcW.restype = LRESULT
    user32.UpdateLayeredWindow.argtypes = [
        p, p, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
        p, ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
        ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.GetDC.argtypes = [p]
    user32.GetDC.restype = p
    user32.ReleaseDC.argtypes = [p, p]
    user32.ReleaseDC.restype = i
    user32.ShowWindow.argtypes = [p, i]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [p, p, i, i, i, i, u]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetTimer.argtypes = [p, ctypes.c_size_t, u, p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [p, ctypes.c_size_t]
    user32.KillTimer.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [p, u, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [p, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.LoadCursorW.argtypes = [p, p]
    user32.LoadCursorW.restype = p
    user32.GetSystemMetrics.argtypes = [i]
    user32.GetSystemMetrics.restype = i
    user32.GetAsyncKeyState.argtypes = [i]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = p
    user32.GetDesktopWindow.argtypes = []
    user32.GetDesktopWindow.restype = p
    user32.MonitorFromWindow.argtypes = [p, u]
    user32.MonitorFromWindow.restype = p
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, u]
    user32.MonitorFromPoint.restype = p
    user32.GetMonitorInfoW.argtypes = [p, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetDpiForWindow.argtypes = [p]
    user32.GetDpiForWindow.restype = u
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), p, u, u]
    user32.GetMessageW.restype = i
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.TranslateMessage.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.PostQuitMessage.argtypes = [i]
    user32.PostQuitMessage.restype = None

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = p

    gdi32.CreateCompatibleDC.argtypes = [p]
    gdi32.CreateCompatibleDC.restype = p
    gdi32.CreateDIBSection.argtypes = [p, ctypes.POINTER(BITMAPINFO), u,
                                       ctypes.POINTER(p), p, u]
    gdi32.CreateDIBSection.restype = p
    gdi32.SelectObject.argtypes = [p, p]
    gdi32.SelectObject.restype = p
    gdi32.DeleteObject.argtypes = [p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [p]
    gdi32.DeleteDC.restype = wintypes.BOOL

    # GDI+ flat API
    gp_sigs = {
        "GdiplusStartup": ([ctypes.POINTER(p), ctypes.POINTER(GdiplusStartupInput), p], i),
        "GdipCreateFromHDC": ([p, ctypes.POINTER(p)], i),
        "GdipDeleteGraphics": ([p], i),
        "GdipGraphicsClear": ([p, u], i),
        "GdipSetSmoothingMode": ([p, i], i),
        "GdipSetTextRenderingHint": ([p, i], i),
        "GdipSetPixelOffsetMode": ([p, i], i),
        "GdipCreateSolidFill": ([u, ctypes.POINTER(p)], i),
        "GdipCreateLineBrushFromRect": ([ctypes.POINTER(RectF), u, u, i, i, ctypes.POINTER(p)], i),
        "GdipDeleteBrush": ([p], i),
        "GdipCreatePen1": ([u, f, i, ctypes.POINTER(p)], i),
        "GdipSetPenLineCap197819": ([p, i, i, i], i),
        "GdipSetPenLineJoin": ([p, i], i),
        "GdipDeletePen": ([p], i),
        "GdipCreatePath": ([i, ctypes.POINTER(p)], i),
        "GdipDeletePath": ([p], i),
        "GdipAddPathArc": ([p, f, f, f, f, f, f], i),
        "GdipClosePathFigure": ([p], i),
        "GdipFillPath": ([p, p, p], i),
        "GdipDrawPath": ([p, p, p], i),
        "GdipFillEllipse": ([p, p, f, f, f, f], i),
        "GdipDrawEllipse": ([p, p, f, f, f, f], i),
        "GdipDrawArc": ([p, p, f, f, f, f, f, f], i),
        "GdipDrawLine": ([p, p, f, f, f, f], i),
        "GdipDrawLines": ([p, p, ctypes.POINTER(PointF), i], i),
        "GdipCreateFontFamilyFromName": ([wintypes.LPCWSTR, p, ctypes.POINTER(p)], i),
        "GdipDeleteFontFamily": ([p], i),
        "GdipCreateFont": ([p, f, i, i, ctypes.POINTER(p)], i),
        "GdipDeleteFont": ([p], i),
        "GdipCreateStringFormat": ([i, ctypes.c_ushort, ctypes.POINTER(p)], i),
        "GdipDeleteStringFormat": ([p], i),
        "GdipSetStringFormatAlign": ([p, i], i),
        "GdipSetStringFormatLineAlign": ([p, i], i),
        "GdipSetStringFormatTrimming": ([p, i], i),
        "GdipSetStringFormatFlags": ([p, i], i),
        "GdipMeasureString": ([p, wintypes.LPCWSTR, i, p, ctypes.POINTER(RectF), p,
                               ctypes.POINTER(RectF), ctypes.POINTER(i), ctypes.POINTER(i)], i),
        "GdipDrawString": ([p, wintypes.LPCWSTR, i, p, ctypes.POINTER(RectF), p, p], i),
    }
    for name, (args, ret) in gp_sigs.items():
        fn = getattr(gdiplus, name)
        fn.argtypes = args
        fn.restype = ret


_init_apis()


def ensure_gdiplus():
    global _gp_ready
    with _gp_lock:
        if _gp_ready:
            return True
        inp = GdiplusStartupInput(1, None, 0, 0)
        st = gdiplus.GdiplusStartup(ctypes.byref(_gp_token), ctypes.byref(inp), None)
        _gp_ready = (st == 0)
        return _gp_ready


def apply_window_dark_titlebar(hwnd, is_dark):
    """同步 Windows 10/11 原生标题栏明暗色（DWMWA_USE_IMMERSIVE_DARK_MODE）。"""
    if not dwmapi or not hwnd:
        return
    try:
        val = ctypes.c_int(1 if is_dark else 0)
        dwmapi.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        for attr in (20, 19):
            if dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr,
                                            ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


# --- GDI+ Drawing Helpers ---

def _round_rect_path(x, y, w, h, r):
    path = ctypes.c_void_p()
    gdiplus.GdipCreatePath(0, ctypes.byref(path))
    r = max(0.5, min(float(r), float(w) * 0.5, float(h) * 0.5))
    d = r * 2.0
    gdiplus.GdipAddPathArc(path, x, y, d, d, 180.0, 90.0)
    gdiplus.GdipAddPathArc(path, x + w - d, y, d, d, 270.0, 90.0)
    gdiplus.GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0.0, 90.0)
    gdiplus.GdipAddPathArc(path, x, y + h - d, d, d, 90.0, 90.0)
    gdiplus.GdipClosePathFigure(path)
    return path


def _fill_round_rect(g, x, y, w, h, r, brush):
    path = _round_rect_path(x, y, w, h, r)
    gdiplus.GdipFillPath(g, brush, path)
    gdiplus.GdipDeletePath(path)


def _draw_round_rect(g, x, y, w, h, r, pen):
    path = _round_rect_path(x, y, w, h, r)
    gdiplus.GdipDrawPath(g, pen, path)
    gdiplus.GdipDeletePath(path)


def _solid_brush(color_argb):
    b = ctypes.c_void_p()
    gdiplus.GdipCreateSolidFill(color_argb, ctypes.byref(b))
    return b


def _linear_brush(x, y, w, h, c1, c2, mode=1):
    b = ctypes.c_void_p()
    rect = RectF(float(x), float(y), max(1.0, float(w)), max(1.0, float(h)))
    gdiplus.GdipCreateLineBrushFromRect(ctypes.byref(rect), c1, c2, mode, 3, ctypes.byref(b))
    return b


def _pen(color_argb, width=1.0, round_cap=False):
    p = ctypes.c_void_p()
    gdiplus.GdipCreatePen1(color_argb, float(width), 2, ctypes.byref(p))  # UnitPixel = 2
    if round_cap:
        gdiplus.GdipSetPenLineCap197819(p, 2, 2, 0)  # LineCapRound = 2
        gdiplus.GdipSetPenLineJoin(p, 2)  # LineJoinRound = 2
    return p


def _make_font(family_name, px_size, bold=False):
    fam = ctypes.c_void_p()
    st = gdiplus.GdipCreateFontFamilyFromName(family_name, None, ctypes.byref(fam))
    if st != 0 or not fam:
        gdiplus.GdipCreateFontFamilyFromName("Microsoft YaHei", None, ctypes.byref(fam))
    font = ctypes.c_void_p()
    style = 1 if bold else 0
    gdiplus.GdipCreateFont(fam, float(px_size), style, 2, ctypes.byref(font))  # UnitPixel = 2
    return fam, font


def _create_dib_surface(w, h):
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down 32bpp ARGB
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(screen_dc, ctypes.byref(bmi), 0,
                                  ctypes.byref(bits), None, 0)
    old_bmp = gdi32.SelectObject(mem_dc, hbmp)
    return screen_dc, mem_dc, hbmp, old_bmp


def _commit_layered(hwnd, x, y, w, h, screen_dc, mem_dc, alpha=255):
    pt_dst = wintypes.POINT(int(x), int(y))
    sz = wintypes.SIZE(int(w), int(h))
    pt_src = wintypes.POINT(0, 0)
    bf = BLENDFUNCTION(0, 0, max(0, min(255, int(alpha))), 1)
    return bool(user32.UpdateLayeredWindow(
        hwnd, screen_dc, ctypes.byref(pt_dst), ctypes.byref(sz),
        mem_dc, ctypes.byref(pt_src), 0, ctypes.byref(bf), 0x00000002
    ))


def _release_dib_surface(screen_dc, mem_dc, hbmp, old_bmp):
    gdi32.SelectObject(mem_dc, old_bmp)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen_dc)


def _blend_hex(hex1, hex2, t):
    r1, g1, b1 = ui_themes.hex_to_rgb(hex1)
    r2, g2, b2 = ui_themes.hex_to_rgb(hex2)
    t = max(0.0, min(1.0, float(t)))
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


# ===================================================================== Overlay

class Overlay:
    """现代灵动岛/磨砂卡片悬浮胶囊（纯 GDI+ 每像素抗锯齿 + 多主题联动）。"""

    WM_TIMER = 0x0113
    SW_HIDE = 0
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    WS_EX_TOPMOST = 0x00000008
    WS_EX_LAYERED = 0x00080000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WAVE_N = 14

    def __init__(self, state, mic=None, app=None, logger=None):
        self.state = state
        self.mic = mic
        self.app = app
        self._log = logger or (lambda m: None)
        self.seen_gen = -1
        self._theme_ver = -1
        self.text = ""
        self.raw_text = ""
        self.kind = "none"
        self.live = ""
        self.disp_live = ""
        self.toast = False
        self._log_t = 0.0
        self._last_kind = None
        self.hwnd = None
        self.thread = None
        self.scale = 1.0
        self.w = 240
        self.h = 56
        self._target_w = 240.0
        self._target_h = 56.0
        self._cur_w = 240.0
        self._cur_h = 56.0
        self._session_min_w = 0.0
        self._fade = 0.0
        self._fade_target = 0.0
        self._shown = False
        self._rec_t0 = 0.0
        self._timer_ms = 50
        self.x = 0
        self.y = 0
        self.shadow_pad = 14
        self._work = None
        self._wave = [0.08] * self.WAVE_N
        self._wave_level = 0.0
        self._phase = 0.0
        self._fam = None
        self._font_main = None
        self._font_sub = None
        self._sf_left = None
        self._sf_right = None
        self._sf_center = None
        self._sf_wrap = None
        self._proc = WNDPROC(self._wndproc)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _set_timer_rate(self, ms):
        if self.hwnd and self._timer_ms != ms:
            self._timer_ms = ms
            user32.SetTimer(self.hwnd, 1, ms, None)

    def _run(self):
        try:
            ensure_gdiplus()
            self._create()
        except Exception:
            self._log("overlay create error " + traceback.format_exc().replace("\n", " | "))
            return
        self._timer_ms = 50
        user32.SetTimer(self.hwnd, 1, 50, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.style = 0x0002 | 0x0001
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = "DoubaoVoiceOverlay"
        user32.RegisterClassExW(ctypes.byref(cls))

        ex = (self.WS_EX_TOPMOST | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE
              | self.WS_EX_TRANSPARENT | self.WS_EX_LAYERED)
        self.hwnd = user32.CreateWindowExW(
            ex, "DoubaoVoiceOverlay", "DoubaoVoiceOverlay",
            0x80000000, 0, 0, 280, 64, None, None, hinst, None
        )
        dpi = user32.GetDpiForWindow(self.hwnd) or 96
        s = self.scale = max(1.0, dpi / 96.0)
        self.shadow_pad = int(round(14 * s))

        self._fam, self._font_main = _make_font("Microsoft YaHei UI", 15.5 * s, bold=True)
        _, self._font_sub = _make_font("Microsoft YaHei UI", 12.2 * s, bold=False)

        self._sf_left = ctypes.c_void_p()
        gdiplus.GdipCreateStringFormat(0, 0, ctypes.byref(self._sf_left))
        gdiplus.GdipSetStringFormatAlign(self._sf_left, 0)       # Near
        gdiplus.GdipSetStringFormatLineAlign(self._sf_left, 1)   # Center
        gdiplus.GdipSetStringFormatFlags(self._sf_left, 0x00001000)  # NoWrap

        self._sf_right = ctypes.c_void_p()
        gdiplus.GdipCreateStringFormat(0, 0, ctypes.byref(self._sf_right))
        gdiplus.GdipSetStringFormatAlign(self._sf_right, 2)      # Far
        gdiplus.GdipSetStringFormatLineAlign(self._sf_right, 1)  # Center
        gdiplus.GdipSetStringFormatFlags(self._sf_right, 0x00001000)

        self._sf_center = ctypes.c_void_p()
        gdiplus.GdipCreateStringFormat(0, 0, ctypes.byref(self._sf_center))
        gdiplus.GdipSetStringFormatAlign(self._sf_center, 1)
        gdiplus.GdipSetStringFormatLineAlign(self._sf_center, 1)
        gdiplus.GdipSetStringFormatFlags(self._sf_center, 0x00001000)

        self._sf_wrap = ctypes.c_void_p()
        gdiplus.GdipCreateStringFormat(0, 0, ctypes.byref(self._sf_wrap))
        gdiplus.GdipSetStringFormatAlign(self._sf_wrap, 0)       # Left aligned readable text
        gdiplus.GdipSetStringFormatLineAlign(self._sf_wrap, 0)   # Top
        gdiplus.GdipSetStringFormatTrimming(self._sf_wrap, 3)    # EllipsisCharacter

        self._log(f"overlay gdi+ hwnd={self.hwnd} dpi={dpi} scale={s:.2f}")

    def _appearance(self):
        if self.app is not None:
            theme = ui_themes.get_theme(getattr(self.app, "theme_id", ui_themes.DEFAULT_THEME))
            pos = getattr(self.app, "overlay_pos", ui_themes.DEFAULT_OVERLAY_POS)
            style = getattr(self.app, "overlay_style", ui_themes.DEFAULT_OVERLAY_STYLE)
            opacity = getattr(self.app, "overlay_opacity", ui_themes.DEFAULT_OVERLAY_OPACITY)
            minimal = getattr(self.app, "overlay_minimal", ui_themes.DEFAULT_OVERLAY_MINIMAL)
            ver = getattr(self.app, "theme_ver", 0)
            return theme, pos, style, opacity, minimal, ver
        return (ui_themes.get_theme(ui_themes.DEFAULT_THEME),
                ui_themes.DEFAULT_OVERLAY_POS,
                ui_themes.DEFAULT_OVERLAY_STYLE,
                ui_themes.DEFAULT_OVERLAY_OPACITY,
                ui_themes.DEFAULT_OVERLAY_MINIMAL, 0)

    def _display_status_text(self, kind, raw_msg, minimal):
        msg = raw_msg
        if kind == "rec" and msg.startswith("● "):
            msg = msg[2:]
        if not minimal:
            return msg
        if kind == "rec":
            # 预览提示保留主题名，其余正常录音精简为灵动岛短文案
            if "当前主题：" in msg:
                return msg.split("当前主题：", 1)[1].strip()
            return "聆听中"
        if kind == "wait":
            if msg.startswith("识别中"):
                return "识别中…"
        return msg

    def _timer_label(self):
        if self.kind != "rec" or self._rec_t0 <= 0.0:
            return ""
        sec = max(0.0, time.monotonic() - self._rec_t0)
        if sec < 60.0:
            return f"{sec:.1f}s"
        m = int(sec // 60)
        s = int(sec % 60)
        return f"{m}:{s:02d}"

    def _measure_text(self, text, font, sf, max_w=1000.0):
        screen_dc = user32.GetDC(None)
        g = ctypes.c_void_p()
        gdiplus.GdipCreateFromHDC(screen_dc, ctypes.byref(g))
        gdiplus.GdipSetTextRenderingHint(g, 3)  # AntiAliasGridFit
        layout = RectF(0.0, 0.0, float(max_w), 2000.0)
        bound = RectF()
        chars = ctypes.c_int(0)
        lines = ctypes.c_int(0)
        gdiplus.GdipMeasureString(g, text, len(text), font, ctypes.byref(layout), sf,
                                  ctypes.byref(bound), ctypes.byref(chars), ctypes.byref(lines))
        gdiplus.GdipDeleteGraphics(g)
        user32.ReleaseDC(None, screen_dc)
        return math.ceil(bound.Width), math.ceil(bound.Height), lines.value

    def _anchor(self, toast, pos_mode, w=None, h=None):
        if self._work is None:
            hwnd = user32.GetForegroundWindow() or user32.GetDesktopWindow()
            mon = user32.MonitorFromWindow(hwnd, 2)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
            self._work = mi.rcWork
        r = self._work
        sp = self.shadow_pad
        ww = int(round(self._cur_w if w is None else w))
        hh = int(round(self._cur_h if h is None else h))
        cw = ww - 2 * sp
        ch = hh - 2 * sp
        # 柔和入场/退场 6px 纵向微移
        slide = int(round((1.0 - self._fade) * 6.0 * self.scale))
        if toast:
            m = int(22 * self.scale)
            return (max(r.left, r.right - cw - m) - sp,
                    max(r.top, r.bottom - ch - m) - sp + slide)
        cx = (r.left + r.right) // 2 - cw // 2 - sp
        if pos_mode == "top":
            cy = r.top + int(22 * self.scale) - sp - slide
        else:
            cy = r.bottom - ch - int(76 * self.scale) - sp + slide
        return cx, cy

    def _compute_layout(self, minimal):
        s = self.scale
        sp = self.shadow_pad
        pad_x = int(18 * s)
        pad_y = int(12 * s)
        icon_w = int(20 * s)
        wave_bw = max(2, int(round(3 * s)))
        wave_gap = max(2, int(round(2.5 * s)))
        wave_w = self.WAVE_N * wave_bw + (self.WAVE_N - 1) * wave_gap
        timer_reserve = int(46 * s) if (minimal and self.kind == "rec") else 0
        max_text_w = int(520 * s)

        two = self.kind in ("rec", "wait") and bool(self.live.strip())
        if two:
            sw, sh, _ = self._measure_text(self.text, self._font_sub, self._sf_left, max_text_w)
            header_w = sw + icon_w + int(10 * s) + timer_reserve
            if self.kind == "rec":
                header_w += wave_w + int(14 * s)
            disp = self.live.strip()
            lw, lh = 0, int(24 * s)
            for _ in range(8):
                lw, lh, nlines = self._measure_text(disp, self._font_main, self._sf_wrap, max_text_w)
                if nlines <= 2 or len(disp) <= 12:
                    break
                disp = "…" + disp[max(1, len(disp) // 4):]
            self.disp_live = disp
            inner_w = max(int(250 * s), min(max_text_w, max(header_w, lw)))
            status_h = max(int(22 * s), sh)
            card_w = inner_w + pad_x * 2
            card_h = pad_y + status_h + int(8 * s) + max(int(24 * s), lh) + pad_y
        else:
            self.disp_live = ""
            tw, th, _ = self._measure_text(self.text, self._font_main, self._sf_left, max_text_w)
            extra = icon_w + int(10 * s) + timer_reserve
            if self.kind == "rec":
                extra += wave_w + int(14 * s)
            min_w = int(218 * s) if (minimal and self.kind == "rec") else int(172 * s)
            card_w = max(min_w, min(int(620 * s), tw + extra + pad_x * 2))
            card_h = max(int(48 * s), th + int(22 * s))

        tw_total = float(card_w + sp * 2)
        th_total = float(card_h + sp * 2)

        # 同一轮录音/识别过程中保持宽度稳定，只平滑展开不来回抽搐缩回
        if self.kind in ("rec", "wait"):
            if tw_total > self._session_min_w:
                self._session_min_w = tw_total
            else:
                tw_total = self._session_min_w
        else:
            self._session_min_w = 0.0

        self._target_w = tw_total
        self._target_h = th_total
        if not self._shown:
            self._cur_w = tw_total
            self._cur_h = th_total
        self.w = int(round(self._cur_w))
        self.h = int(round(self._cur_h))

    def _apply(self):
        kind, msg, live, toast, gen = self.state.get()
        theme, pos_mode, style, opacity, minimal, tver = self._appearance()
        need_recalc = (gen != self.seen_gen) or (tver != self._theme_ver)
        if not need_recalc:
            return False
        self.seen_gen = gen
        self._theme_ver = tver
        if kind == "none":
            if self._shown:
                self._fade_target = 0.0
                self._set_timer_rate(16)
            else:
                self.kind = "none"
                self._work = None
                self._session_min_w = 0.0
            return False

        was_rec = (self.kind == "rec")
        if kind == "rec" and not was_rec:
            self._rec_t0 = time.monotonic()
            self._session_min_w = 0.0
        elif kind not in ("rec", "wait"):
            self._rec_t0 = 0.0

        self.kind = kind
        self.raw_text = msg
        self.text = self._display_status_text(kind, msg, minimal)
        self.live = live
        self.toast = toast
        if kind != "rec":
            self._wave_level = 0.0
            self._wave = [0.08] * self.WAVE_N

        self._compute_layout(minimal)
        self._fade_target = 1.0
        if not self._shown:
            self._fade = 0.08
            self._shown = True
        self._set_timer_rate(16)

        self.x, self.y = self._anchor(toast, pos_mode)
        self._render(theme, style, opacity, minimal)
        ok = user32.SetWindowPos(
            self.hwnd, ctypes.c_void_p(self.HWND_TOPMOST),
            self.x, self.y, self.w, self.h,
            self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW
        )
        now = time.monotonic()
        if kind != self._last_kind or now - self._log_t >= 1.0:
            self._log_t = now
            self._log(f"overlay show kind={kind} gen={gen} pos={self.x},{self.y} "
                      f"{self.w}x{self.h} theme={theme['id']} minimal={minimal} ok={ok}")
        self._last_kind = kind
        return True

    def _tick_anim(self):
        if not self._shown:
            return
        theme, pos_mode, style, opacity, minimal, _ = self._appearance()
        dirty = False

        # 1) 120ms 柔和淡入淡出（16ms * ~7.5 帧 ≈ 120ms）
        if abs(self._fade - self._fade_target) > 0.01:
            step = 0.145 if self._fade_target > self._fade else -0.155
            self._fade = max(0.0, min(1.0, self._fade + step))
            if abs(self._fade - self._fade_target) <= 0.02:
                self._fade = self._fade_target
            dirty = True
            if self._fade <= 0.0 and self._fade_target <= 0.0:
                self._shown = False
                self.kind = "none"
                self._work = None
                self._session_min_w = 0.0
                user32.ShowWindow(self.hwnd, self.SW_HIDE)
                self._set_timer_rate(50)
                self._log("overlay hide")
                return

        # 2) 灵动岛平滑宽高缓动插值
        dw = self._target_w - self._cur_w
        dh = self._target_h - self._cur_h
        if abs(dw) > 0.8 or abs(dh) > 0.8:
            self._cur_w += dw * 0.28
            self._cur_h += dh * 0.28
            if abs(self._target_w - self._cur_w) < 1.2:
                self._cur_w = self._target_w
            if abs(self._target_h - self._cur_h) < 1.2:
                self._cur_h = self._target_h
            self.w = int(round(self._cur_w))
            self.h = int(round(self._cur_h))
            dirty = True

        # 3) 声波、旋转加载环、录音脉冲与秒表动画
        anim_active = self.kind in ("rec", "wait", "capture")
        if anim_active:
            self._phase += 0.105
            dirty = True
            if self.kind == "rec":
                raw = self.mic.level if self.mic is not None else 0.0
                if raw < 0.015 and getattr(self.app, "_preview_active", False):
                    raw = 0.28 + 0.48 * abs(math.sin(self._phase * 0.7)) * abs(math.cos(self._phase * 1.3))
                self._wave_level = max(raw, self._wave_level * 0.82)
                mid = (self.WAVE_N - 1) / 2.0
                nxt = []
                for idx in range(self.WAVE_N):
                    dist = abs(idx - mid) / max(1.0, mid)
                    env = max(0.22, 1.0 - 0.65 * (dist ** 1.6))
                    ripple = 0.55 + 0.45 * math.sin(self._phase * 1.45 - idx * 0.58)
                    target = max(0.10, min(1.0, (0.14 + (self._wave_level ** 0.52) * 0.92) * env * ripple))
                    prev = self._wave[idx]
                    nxt.append(prev * 0.55 + target * 0.45)
                self._wave = nxt

        if dirty:
            self.x, self.y = self._anchor(self.toast, pos_mode)
            self._render(theme, style, opacity, minimal)
        else:
            # 静止展示状态（如 done/info 保持阶段）自动降频省电
            self._set_timer_rate(50)

    def _draw_status_icon(self, g, cx, cy, r, ov):
        s = self.scale
        if self.kind == "rec":
            pulse = 0.5 + 0.5 * math.sin(self._phase * 1.2)
            glow_r = r * (1.28 + 0.35 * pulse)
            gb = _solid_brush(ui_themes.argb(ov["rec_dot"], int(68 * (1.0 - 0.4 * pulse))))
            gdiplus.GdipFillEllipse(g, gb, cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2)
            gdiplus.GdipDeleteBrush(gb)
            db = _solid_brush(ui_themes.argb(ov["rec_dot"], 255))
            dr = r * 0.68
            gdiplus.GdipFillEllipse(g, db, cx - dr, cy - dr, dr * 2, dr * 2)
            gdiplus.GdipDeleteBrush(db)
        elif self.kind == "wait":
            # 顺滑旋转加载弧
            bg_p = _pen(ui_themes.argb(ov["accent"], 55), 2.2 * s, round_cap=True)
            gdiplus.GdipDrawEllipse(g, bg_p, cx - r * 0.78, cy - r * 0.78, r * 1.56, r * 1.56)
            gdiplus.GdipDeletePen(bg_p)
            p = _pen(ui_themes.argb(ov["accent2"], 255), 2.4 * s, round_cap=True)
            start_deg = (self._phase * 58.0) % 360.0
            gdiplus.GdipDrawArc(g, p, cx - r * 0.78, cy - r * 0.78, r * 1.56, r * 1.56,
                                start_deg, 115.0)
            gdiplus.GdipDeletePen(p)
        elif self.kind == "done":
            bg = _solid_brush(ui_themes.argb(ov["done"], 38))
            gdiplus.GdipFillEllipse(g, bg, cx - r, cy - r, r * 2, r * 2)
            gdiplus.GdipDeleteBrush(bg)
            p = _pen(ui_themes.argb(ov["done"], 255), 2.1 * s, round_cap=True)
            pts = (PointF * 3)(
                PointF(cx - r * 0.42, cy + r * 0.02),
                PointF(cx - r * 0.10, cy + r * 0.36),
                PointF(cx + r * 0.46, cy - r * 0.30),
            )
            gdiplus.GdipDrawLines(g, p, pts, 3)
            gdiplus.GdipDeletePen(p)
        elif self.kind == "fail":
            bg = _solid_brush(ui_themes.argb(ov["fail"], 40))
            gdiplus.GdipFillEllipse(g, bg, cx - r, cy - r, r * 2, r * 2)
            gdiplus.GdipDeleteBrush(bg)
            p = _pen(ui_themes.argb(ov["fail"], 255), 2.1 * s, round_cap=True)
            d = r * 0.36
            gdiplus.GdipDrawLine(g, p, cx - d, cy - d, cx + d, cy + d)
            gdiplus.GdipDrawLine(g, p, cx + d, cy - d, cx - d, cy + d)
            gdiplus.GdipDeletePen(p)
        elif self.kind == "capture":
            pulse = 0.5 + 0.5 * math.sin(self._phase * 1.1)
            bg = _solid_brush(ui_themes.argb(ov["accent"], int(42 + 40 * pulse)))
            gdiplus.GdipFillEllipse(g, bg, cx - r, cy - r, r * 2, r * 2)
            gdiplus.GdipDeleteBrush(bg)
            p = _pen(ui_themes.argb(ov["accent2"], 255), 1.8 * s, round_cap=True)
            gdiplus.GdipDrawEllipse(g, p, cx - r * 0.55, cy - r * 0.55, r * 1.1, r * 1.1)
            gdiplus.GdipDeletePen(p)
        else:
            # info
            bg = _solid_brush(ui_themes.argb(ov["accent"], 46))
            gdiplus.GdipFillEllipse(g, bg, cx - r, cy - r, r * 2, r * 2)
            gdiplus.GdipDeleteBrush(bg)
            db = _solid_brush(ui_themes.argb(ov["accent2"], 255))
            dr = r * 0.46
            gdiplus.GdipFillEllipse(g, db, cx - dr, cy - dr, dr * 2, dr * 2)
            gdiplus.GdipDeleteBrush(db)

    def _draw_wave(self, g, x, cy, max_h, ov):
        s = self.scale
        bw = max(2.0, 3.0 * s)
        gap = max(2.0, 2.5 * s)
        n = len(self._wave)
        for i, v in enumerate(self._wave):
            t = i / max(1, n - 1)
            col = _blend_hex(ov["accent"], ov["accent2"], t)
            bh = max(3.5 * s, v * max_h)
            top = cy - bh * 0.5
            br = _solid_brush(ui_themes.argb(col, 245))
            _fill_round_rect(g, x, top, bw, bh, bw * 0.5, br)
            gdiplus.GdipDeleteBrush(br)
            x += bw + gap

    def _render(self, theme, style, opacity_pct, minimal=True):
        if not self.hwnd or self.w <= 0 or self.h <= 0:
            return
        ov = theme["overlay"]
        s = self.scale
        sp = float(self.shadow_pad)
        cw = float(self.w) - sp * 2.0
        ch = float(self.h) - sp * 2.0

        # 灵动岛胶囊 vs 现代圆角卡片
        if style == "card":
            radius = min(14.0 * s, ch * 0.36)
        else:
            radius = (ch * 0.5) if not self.disp_live else min(22.0 * s, ch * 0.34)

        screen_dc, mem_dc, hbmp, old_bmp = _create_dib_surface(self.w, self.h)
        try:
            g = ctypes.c_void_p()
            gdiplus.GdipCreateFromHDC(mem_dc, ctypes.byref(g))
            gdiplus.GdipGraphicsClear(g, 0x00000000)
            gdiplus.GdipSetSmoothingMode(g, 4)        # AntiAlias
            gdiplus.GdipSetTextRenderingHint(g, 3)    # AntiAliasGridFit
            gdiplus.GdipSetPixelOffsetMode(g, 4)      # Half

            # 1) 多层柔和弥散投影
            sh_base = ov["shadow_alpha"]
            for step in range(4, 0, -1):
                spread = step * 2.1 * s
                off_y = step * 0.9 * s
                alpha = max(4, int(sh_base * (0.14 if step == 4 else 0.20 if step == 3 else 0.28 if step == 2 else 0.40)))
                sb = _solid_brush(ui_themes.argb(ov["shadow"], alpha))
                _fill_round_rect(g, sp - spread * 0.45, sp + off_y,
                                 cw + spread * 0.9, ch + spread * 0.45,
                                 radius + spread * 0.35, sb)
                gdiplus.GdipDeleteBrush(sb)

            # 2) 主体渐变底板
            bg_a = ov["bg_alpha"]
            bg_br = _linear_brush(
                sp, sp, cw, ch,
                ui_themes.argb(ov["bg_top"], bg_a),
                ui_themes.argb(ov["bg_bottom"], bg_a),
                mode=1
            )
            _fill_round_rect(g, sp, sp, cw, ch, radius, bg_br)
            gdiplus.GdipDeleteBrush(bg_br)

            # 3) 录音/识别时的细微顶部流光氛围条
            if self.kind in ("rec", "wait"):
                glow_br = _linear_brush(
                    sp, sp, cw, ch,
                    ui_themes.argb(ov["accent"], 28),
                    ui_themes.argb(ov["accent2"], 16),
                    mode=0
                )
                _fill_round_rect(g, sp + 1.0, sp + 1.0, cw - 2.0, ch - 2.0, max(2.0, radius - 1.0), glow_br)
                gdiplus.GdipDeleteBrush(glow_br)

            # 4) 玻璃内高光 + 精致外描边
            hl_pen = _pen(ui_themes.argb(ov["highlight"], ov["highlight_alpha"]), 1.0 * s)
            _draw_round_rect(g, sp + 1.2, sp + 1.2, cw - 2.4, ch - 2.4, max(2.0, radius - 1.2), hl_pen)
            gdiplus.GdipDeletePen(hl_pen)

            border_col = ov["accent"] if self.kind == "rec" else (ov["fail"] if self.kind == "fail" else ov["border"])
            border_a = min(235, ov["border_alpha"] + (35 if self.kind == "rec" else 0))
            bd_pen = _pen(ui_themes.argb(border_col, border_a), 1.2 * s)
            _draw_round_rect(g, sp + 0.5, sp + 0.5, cw - 1.0, ch - 1.0, radius, bd_pen)
            gdiplus.GdipDeletePen(bd_pen)

            # 5) 内容排版
            pad_x = 18.0 * s
            pad_y = 12.0 * s
            icon_r = 8.5 * s
            wave_bw = max(2.0, 3.0 * s)
            wave_gap = max(2.0, 2.5 * s)
            wave_w = self.WAVE_N * wave_bw + (self.WAVE_N - 1) * wave_gap
            t_lbl = self._timer_label() if minimal else ""
            t_res = 48.0 * s if t_lbl else 0.0

            if self.disp_live:
                status_h = 22.0 * s
                row_cy = sp + pad_y + status_h * 0.5
                cur_x = sp + pad_x
                self._draw_status_icon(g, cur_x + icon_r, row_cy, icon_r, ov)
                cur_x += icon_r * 2.0 + 10.0 * s

                if self.kind == "rec":
                    self._draw_wave(g, cur_x, row_cy, status_h - 3.0 * s, ov)
                    cur_x += wave_w + 12.0 * s

                sub_br = _solid_brush(ui_themes.argb(ov["subtext"], 255))
                s_rect = RectF(cur_x, sp + pad_y, max(20.0, sp + cw - pad_x - t_res - cur_x), status_h)
                gdiplus.GdipDrawString(g, self.text, len(self.text), self._font_sub,
                                       ctypes.byref(s_rect), self._sf_left, sub_br)
                if t_lbl:
                    tm_rect = RectF(sp + cw - pad_x - 46.0 * s, sp + pad_y, 46.0 * s, status_h)
                    gdiplus.GdipDrawString(g, t_lbl, len(t_lbl), self._font_sub,
                                           ctypes.byref(tm_rect), self._sf_right, sub_br)
                gdiplus.GdipDeleteBrush(sub_br)

                txt_br = _solid_brush(ui_themes.argb(ov["text"], 255))
                t_top = sp + pad_y + status_h + 7.0 * s
                t_rect = RectF(sp + pad_x, t_top, cw - pad_x * 2.0, sp + ch - pad_y - t_top + 4.0 * s)
                gdiplus.GdipDrawString(g, self.disp_live, len(self.disp_live), self._font_main,
                                       ctypes.byref(t_rect), self._sf_wrap, txt_br)
                gdiplus.GdipDeleteBrush(txt_br)
            else:
                cy = sp + ch * 0.5
                cur_x = sp + pad_x
                self._draw_status_icon(g, cur_x + icon_r, cy, icon_r, ov)
                cur_x += icon_r * 2.0 + 10.0 * s

                if self.kind == "rec":
                    self._draw_wave(g, cur_x, cy, ch - 18.0 * s, ov)
                    cur_x += wave_w + 12.0 * s

                text_col = ov["fail"] if self.kind == "fail" else ov["text"]
                txt_br = _solid_brush(ui_themes.argb(text_col, 255))
                t_rect = RectF(cur_x, sp, max(20.0, sp + cw - pad_x - t_res - cur_x), ch)
                gdiplus.GdipDrawString(g, self.text, len(self.text), self._font_main,
                                       ctypes.byref(t_rect), self._sf_left, txt_br)
                gdiplus.GdipDeleteBrush(txt_br)

                if t_lbl:
                    sub_br = _solid_brush(ui_themes.argb(ov["subtext"], 240))
                    tm_rect = RectF(sp + cw - pad_x - 46.0 * s, sp, 46.0 * s, ch)
                    gdiplus.GdipDrawString(g, t_lbl, len(t_lbl), self._font_sub,
                                           ctypes.byref(tm_rect), self._sf_right, sub_br)
                    gdiplus.GdipDeleteBrush(sub_br)

            gdiplus.GdipDeleteGraphics(g)

            # 乘以淡入淡出系数（ease-out 曲线更自然）
            f_eased = 1.0 - (1.0 - self._fade) ** 2
            base_alpha = 255.0 * max(0.55, min(1.0, opacity_pct / 100.0))
            if self.toast:
                base_alpha *= 0.94
            win_alpha = int(round(base_alpha * f_eased))
            _commit_layered(self.hwnd, self.x, self.y, self.w, self.h, screen_dc, mem_dc, win_alpha)
        finally:
            _release_dib_surface(screen_dc, mem_dc, hbmp, old_bmp)

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == self.WM_TIMER:
                changed = self._apply()
                if not changed and self._shown:
                    self._tick_anim()
                return 0
        except Exception:
            self._log("overlay wndproc error " + traceback.format_exc().replace("\n", " | "))
        return user32.DefWindowProcW(hwnd, msg, wp, lp)


# ================================================================= FloatButton

WM_DESTROY = 0x0002
WM_APP = 0x8000


class FloatButton:
    """现代 GDI+ 抗锯齿悬浮麦克风球（柔和阴影 + 呼吸光环 + 主题联动）。"""

    WM_TIMER = 0x0113
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_RBUTTONUP = 0x0205
    WM_FLOAT_SHOW = WM_APP + 5
    WM_FLOAT_HIDE = WM_APP + 6
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = -1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    WS_EX_TOPMOST = 0x00000008
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    WS_POPUP = 0x80000000
    ORB_SIZE = 50
    PAD = 10
    DRAG_TIMER = 2
    STATE_TIMER = 1

    def __init__(self, app, load_config_fn=None, logger=None):
        self.app = app
        self._load_config = load_config_fn or (lambda: {})
        self._log = logger or (lambda m: None)
        self.hwnd = None
        self.thread = None
        self.ready = threading.Event()
        self.consumed = False
        self.visible = False
        self.rec = False
        self.hover = False
        self.scale = 1.0
        self.orb = self.ORB_SIZE
        self.pad = self.PAD
        self.size = self.ORB_SIZE + self.PAD * 2
        self.x = 0
        self.y = 0
        self._phase = 0.0
        self._theme_ver = -1
        self._drag = None
        self._proc = WNDPROC(self._wndproc)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_visible(self, on):
        if not self.ready.wait(2.0) or not self.hwnd:
            return
        user32.PostMessageW(self.hwnd,
                            self.WM_FLOAT_SHOW if on else self.WM_FLOAT_HIDE, 0, 0)

    def take_consumed(self):
        v = self.consumed
        self.consumed = False
        return v

    def contains(self, x, y):
        if not self.hwnd or not self.visible:
            return False
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        cx = (r.left + r.right) * 0.5
        cy = (r.top + r.bottom) * 0.5
        rad = self.orb * 0.52
        return (x - cx) ** 2 + (y - cy) ** 2 <= rad ** 2

    def _run(self):
        try:
            ensure_gdiplus()
            self._create()
        except Exception:
            self._log("float create error " + traceback.format_exc().replace("\n", " | "))
            self.ready.set()
            return
        self.ready.set()
        user32.SetTimer(self.hwnd, self.STATE_TIMER, 45, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32649))  # IDC_HAND
        cls.lpszClassName = "DoubaoVoiceFloat"
        user32.RegisterClassExW(ctypes.byref(cls))

        ex = (self.WS_EX_TOPMOST | self.WS_EX_TOOLWINDOW
              | self.WS_EX_NOACTIVATE | self.WS_EX_LAYERED)
        self.hwnd = user32.CreateWindowExW(
            ex, "DoubaoVoiceFloat", "豆包语音悬浮按钮",
            self.WS_POPUP, 0, 0, self.size, self.size,
            None, None, hinst, None
        )
        if not self.hwnd:
            return
        dpi = user32.GetDpiForWindow(self.hwnd) or 96
        self.scale = max(1.0, dpi / 96.0)
        self.orb = int(round(self.ORB_SIZE * self.scale))
        self.pad = int(round(self.PAD * self.scale))
        self.size = self.orb + self.pad * 2
        self.x, self.y = self._initial_pos()
        self._render()
        self._log(f"float gdi+ hwnd={self.hwnd} dpi={dpi} size={self.size} "
                  f"pos={self.x},{self.y} visible={self.app.float_visible}")
        if self.app.float_visible:
            self._show()

    def _initial_pos(self):
        pos = self._load_config().get("float_pos")
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)
        if (isinstance(pos, (list, tuple)) and len(pos) == 2
                and all(isinstance(v, int) for v in pos)):
            x, y = pos
        else:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            mon = user32.MonitorFromPoint(pt, 2)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
                w = mi.rcWork
                x = w.right - self.size - int(18 * self.scale)
                y = w.bottom - self.size - int(86 * self.scale)
            else:
                x, y = vx + vw - self.size - 18, vy + vh - self.size - 86
        x = min(max(x, vx), vx + vw - self.size)
        y = min(max(y, vy), vy + vh - self.size)
        return x, y

    def _show(self):
        self.visible = True
        self._render()
        user32.ShowWindow(self.hwnd, self.SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, ctypes.c_void_p(self.HWND_TOPMOST),
                            self.x, self.y, self.size, self.size, self.SWP_NOACTIVATE)

    def _hide(self):
        self.visible = False
        user32.ShowWindow(self.hwnd, self.SW_HIDE)

    def _save_pos(self):
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        self.x, self.y = r.left, r.top
        self.app._save_option(float_pos=[r.left, r.top])

    def _draw_mic_icon(self, g, cx, cy, s, color_hex):
        col = ui_themes.argb(color_hex, 255)
        # 精致矢量麦克风胶囊
        mw = 8.4 * s
        mh = 13.0 * s
        br = _solid_brush(col)
        _fill_round_rect(g, cx - mw * 0.5, cy - mh * 0.5 - 2.6 * s, mw, mh, mw * 0.5, br)
        gdiplus.GdipDeleteBrush(br)

        # U 形底座弧与立柱
        pen = _pen(col, 2.0 * s, round_cap=True)
        arc_r = 7.4 * s
        gdiplus.GdipDrawArc(g, pen, cx - arc_r, cy - arc_r - 0.5 * s,
                            arc_r * 2.0, arc_r * 2.0, 12.0, 156.0)
        gdiplus.GdipDrawLine(g, pen, cx, cy + arc_r - 0.5 * s, cx, cy + arc_r + 3.4 * s)
        gdiplus.GdipDrawLine(g, pen, cx - 4.2 * s, cy + arc_r + 3.4 * s,
                             cx + 4.2 * s, cy + arc_r + 3.4 * s)
        gdiplus.GdipDeletePen(pen)

    def _render(self):
        if not self.hwnd:
            return
        theme = ui_themes.get_theme(getattr(self.app, "theme_id", ui_themes.DEFAULT_THEME))
        fb = theme["float_btn"]
        ov = theme["overlay"]
        s = self.scale
        sz = self.size
        pad = float(self.pad)
        orb = float(self.orb)
        cx = sz * 0.5
        cy = sz * 0.5

        screen_dc, mem_dc, hbmp, old_bmp = _create_dib_surface(sz, sz)
        try:
            g = ctypes.c_void_p()
            gdiplus.GdipCreateFromHDC(mem_dc, ctypes.byref(g))
            gdiplus.GdipGraphicsClear(g, 0x00000000)
            gdiplus.GdipSetSmoothingMode(g, 4)
            gdiplus.GdipSetPixelOffsetMode(g, 4)

            # 1) 柔和环境阴影
            for step in range(3, 0, -1):
                sp = step * 1.8 * s
                alpha = int((62 if theme["is_dark"] else 34) * (0.28 if step == 3 else 0.45 if step == 2 else 0.65))
                sb = _solid_brush(ui_themes.argb(ov["shadow"], alpha))
                gdiplus.GdipFillEllipse(g, sb, pad - sp * 0.4, pad + step * 1.1 * s,
                                        orb + sp * 0.8, orb + sp * 0.4)
                gdiplus.GdipDeleteBrush(sb)

            # 2) 录音时的呼吸光环
            if self.rec:
                pulse = 0.5 + 0.5 * math.sin(self._phase * 1.5)
                ring_exp = (2.5 + 4.8 * pulse) * s
                ring_a = int(145 * (1.0 - 0.55 * pulse))
                rp = _pen(ui_themes.argb(fb["rec_ring"], ring_a), 2.4 * s)
                gdiplus.GdipDrawEllipse(g, rp, pad - ring_exp, pad - ring_exp,
                                        orb + ring_exp * 2.0, orb + ring_exp * 2.0)
                gdiplus.GdipDeletePen(rp)

            # 3) 圆形球体渐变填充
            c1 = fb["rec_top"] if self.rec else fb["bg_top"]
            c2 = fb["rec_bottom"] if self.rec else fb["bg_bottom"]
            bg = _linear_brush(pad, pad, orb, orb,
                               ui_themes.argb(c1, 248), ui_themes.argb(c2, 248), mode=2)
            gdiplus.GdipFillEllipse(g, bg, pad, pad, orb, orb)
            gdiplus.GdipDeleteBrush(bg)

            # 4) 高光内环 + 外边框
            if self.hover and not self.rec:
                hb = _solid_brush(ui_themes.argb(ov["accent"], 28))
                gdiplus.GdipFillEllipse(g, hb, pad, pad, orb, orb)
                gdiplus.GdipDeleteBrush(hb)

            b_col = fb["rec_ring"] if self.rec else (ov["accent"] if self.hover else fb["border"])
            bp = _pen(ui_themes.argb(b_col, 220), (1.8 if self.rec or self.hover else 1.2) * s)
            gdiplus.GdipDrawEllipse(g, bp, pad + 0.6, pad + 0.6, orb - 1.2, orb - 1.2)
            gdiplus.GdipDeletePen(bp)

            # 5) 居中麦克风矢量图标（或录音动态三柱）
            if self.rec:
                bar_col = fb["rec_icon"]
                for idx, off in enumerate((-6.5, 0.0, 6.5)):
                    h_fac = 0.45 + 0.55 * abs(math.sin(self._phase * 1.8 + idx * 1.05))
                    bh = (7.0 + 11.0 * h_fac) * s
                    bw = 3.4 * s
                    bb = _solid_brush(ui_themes.argb(bar_col, 255))
                    _fill_round_rect(g, cx + off * s - bw * 0.5, cy - bh * 0.5, bw, bh, bw * 0.5, bb)
                    gdiplus.GdipDeleteBrush(bb)
            else:
                self._draw_mic_icon(g, cx, cy, s, fb["icon"])

            gdiplus.GdipDeleteGraphics(g)
            _commit_layered(self.hwnd, self.x, self.y, sz, sz, screen_dc, mem_dc, 252)
        finally:
            _release_dib_surface(screen_dc, mem_dc, hbmp, old_bmp)

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            return self._inner(hwnd, msg, wp, lp)
        except Exception:
            self._log("float error " + traceback.format_exc().replace("\n", " | "))
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _inner(self, hwnd, msg, wp, lp):
        if msg == self.WM_FLOAT_SHOW:
            self._show()
            return 0
        if msg == self.WM_FLOAT_HIDE:
            self._hide()
            return 0
        if msg == self.WM_TIMER:
            if wp == self.DRAG_TIMER:
                self._drag_tick(hwnd)
            else:
                if not self.visible:
                    return 0
                with self.app.lock:
                    rec = self.app.session is not None
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                hov = self.contains(pt.x, pt.y)
                tver = getattr(self.app, "theme_ver", 0)
                dirty = (rec != self.rec) or (hov != self.hover) or (tver != self._theme_ver)
                self.rec = rec
                self.hover = hov
                self._theme_ver = tver
                if rec:
                    self._phase += 0.22
                    dirty = True
                if dirty:
                    self._render()
            return 0
        if msg == self.WM_LBUTTONDOWN:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            self._drag = (pt.x, pt.y, r.left, r.top, False)
            user32.SetTimer(hwnd, self.DRAG_TIMER, 15, None)
            return 0
        if msg == self.WM_RBUTTONUP:
            self._drag = None
            user32.KillTimer(hwnd, self.DRAG_TIMER)
            self._save_pos()
            self.app.set_float_visible(False)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _drag_tick(self, hwnd):
        if user32.GetAsyncKeyState(0x01) & 0x8000:
            if self._drag is None:
                return
            sx, sy, wx, wy, moved = self._drag
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            dx, dy = pt.x - sx, pt.y - sy
            if not moved and abs(dx) <= 3 and abs(dy) <= 3:
                return
            self._drag = (sx, sy, wx, wy, True)
            vx = user32.GetSystemMetrics(76)
            vy = user32.GetSystemMetrics(77)
            vw = user32.GetSystemMetrics(78)
            vh = user32.GetSystemMetrics(79)
            nx = min(max(wx + dx, vx), vx + vw - self.size)
            ny = min(max(wy + dy, vy), vy + vh - self.size)
            self.x, self.y = nx, ny
            user32.SetWindowPos(hwnd, ctypes.c_void_p(self.HWND_TOPMOST),
                                nx, ny, 0, 0, self.SWP_NOSIZE | self.SWP_NOACTIVATE)
            return
        user32.KillTimer(hwnd, self.DRAG_TIMER)
        drag = self._drag
        self._drag = None
        if drag is None:
            return
        if drag[4]:
            self._save_pos()
        else:
            self.app.float_click()
