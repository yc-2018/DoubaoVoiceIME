#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超轻量现代 Web 设置中心与 6 引擎并发测速面板（基于系统 Edge 独立应用窗口，零额外框架依赖）。"""

import ctypes
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ui_gdiplus
import ui_themes

user32 = ctypes.WinDLL("user32", use_last_error=True)

WINDOW_TITLE = "豆包语音输入 · 设置"
PROFILE_DIR = os.path.join(tempfile.gettempdir(), "DoubaoVoiceWebProfile")


def _find_edge():
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return shutil.which("msedge") or shutil.which("chrome") or ""


def _find_settings_hwnd():
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0 or n > 200:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if WINDOW_TITLE in buf.value:
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found[0] if found else None


def _focus_hwnd(hwnd, is_dark):
    if not hwnd:
        return False
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = ctypes.c_bool
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_bool
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_bool
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    else:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
    ui_gdiplus.apply_window_dark_titlebar(hwnd, is_dark)
    user32.SetForegroundWindow(hwnd)
    return True


def _sync_titlebar_async(is_dark, delay=0.0):
    def worker():
        if delay > 0:
            time.sleep(delay)
        for _ in range(18):
            hwnd = _find_settings_hwnd()
            if hwnd:
                ui_gdiplus.apply_window_dark_titlebar(hwnd, is_dark)
                return
            time.sleep(0.18)
    threading.Thread(target=worker, daemon=True).start()


# ============================================================ Engine Benchmark

class EngineTestController:
    """复用原版 6 引擎并发识别逻辑，向 Web 设置面板提供实时状态接口。"""

    def __init__(self, app, helpers):
        self.app = app
        self.h = helpers
        self._lock = threading.Lock()
        self._active = False
        self._engines = None
        self._preparing = False
        self._sessions = {}
        self._status = {}
        self._text = {}
        self._times = {}
        self._errors = {}
        self._runner_done = {}
        self._pending = set()
        self._state = "idle"
        self._record_t = 0.0
        self._stop_t = 0.0
        self._stop_evt = None
        self._fanout = None
        self._fastest = None
        self._slowest = None

    def ensure_ready(self):
        with self._lock:
            self._active = True
            self.app.testing = True
            if self._engines is not None or self._preparing:
                return
            self._preparing = True
        threading.Thread(target=self._prepare_engines, daemon=True).start()

    def _prepare_engines(self):
        engines = {}
        for kind in self.h["ASR_ENGINES"]:
            try:
                if kind == "doubao":
                    eng = self.app.test_local_engine()
                else:
                    eng = self.h["create_engine"](kind)
                    if hasattr(eng, "use_frontier"):
                        eng.use_frontier = self.app.doubao_quic
                eng.set_extra(self.app._engine_extra())
                engines[kind] = eng
            except Exception:
                self.h["log"](f"web engine test {kind} prepare error "
                              + traceback.format_exc().replace("\n", " | "))
        with self._lock:
            self._engines = engines
            self._preparing = False
        local = engines.get("doubao")
        if local is not None and not local.ready.is_set():
            try:
                local.warmup()
            except Exception:
                self.h["log"]("web engine test local warmup error "
                              + traceback.format_exc().replace("\n", " | "))

    def start_record(self):
        with self._lock:
            if self._state != "idle" or self._engines is None:
                return False, "引擎尚未就绪或正在测试中"
            with self.app.lock:
                if self.app.session is not None:
                    return False, "当前正在语音输入，请结束后再测试"
            self._active = True
            self.app.testing = True
            self._state = "recording"
            self._record_t = time.monotonic()
            self._stop_t = 0.0
            self._stop_evt = threading.Event()
            self._sessions = {}
            self._status = {}
            self._text = {}
            self._times = {}
            self._errors = {}
            self._runner_done = {}
            self._pending = set()
            self._fastest = None
            self._slowest = None
            engines = dict(self._engines)

        sessions = {}
        for kind in self.h["ASR_ENGINES"]:
            eng = engines.get(kind)
            if eng is None:
                self._set_error(kind, "引擎不可用")
                continue
            try:
                sess = eng.new_session(
                    False, on_text=lambda text, k=kind: self._on_text(k, text))
            except Exception:
                self._set_error(kind, "创建会话失败")
                continue
            sessions[kind] = sess
            with self._lock:
                self._status[kind] = "connecting"

        if not sessions:
            with self._lock:
                self._state = "idle"
            return False, "没有可用的识别引擎"

        fanout = self.h["MicFanout"](sessions.values())
        mic = self.app.mic
        opened = False
        try:
            if mic.stream is None:
                mic.open()
                opened = True
            mic.attach(fanout)
        except Exception:
            for sess in sessions.values():
                try:
                    sess.cancel()
                    sess.close()
                except Exception:
                    pass
            if opened:
                try:
                    mic.close()
                except Exception:
                    pass
            for kind in sessions:
                self._set_error(kind, "麦克风打开失败")
            with self._lock:
                self._state = "idle"
            return False, "麦克风打开失败"

        with self._lock:
            self._sessions = sessions
            self._fanout = fanout
        for kind, sess in sessions.items():
            threading.Thread(target=self._run_one,
                             args=(kind, sess, engines[kind]), daemon=True).start()
        return True, "ok"

    def _on_text(self, kind, text):
        if not text:
            return
        with self._lock:
            if not self._active or kind in self._times:
                return
            self._text[kind] = text
            self._status[kind] = "started"

    def _run_one(self, kind, sess, engine):
        try:
            if not engine.ready.wait(20.0):
                self._set_error(kind, "引擎未就绪")
                return
            with self._lock:
                active = self._active
                stop_evt = self._stop_evt
            if not active:
                sess.cancel()
                return
            sess.start()
            with self._lock:
                if kind not in self._times:
                    self._status[kind] = "started"
            if stop_evt is not None:
                stop_evt.wait(120.0)
            with self._lock:
                active = self._active
                t0 = self._stop_t
            if not active:
                sess.cancel()
                return
            sess.request_release()
            sess.finish()
            elapsed = max(0.0, time.monotonic() - t0)
            text = (sess.text or "").strip()
            self._set_result(kind, elapsed, text)
        except Exception:
            self.h["log"](f"web engine test {kind} error "
                          + traceback.format_exc().replace("\n", " | "))
            self._set_error(kind, "识别失败")
        finally:
            try:
                sess.close()
            except Exception:
                pass
            self._runner_finished(kind)

    def _set_error(self, kind, message):
        with self._lock:
            if kind in self._times:
                return
            self._errors[kind] = message
            self._status.pop(kind, None)

    def _set_result(self, kind, elapsed, text):
        with self._lock:
            if not self._active:
                return
            self._times[kind] = elapsed
            self._text[kind] = text
            self._status[kind] = "done"

    def _runner_finished(self, kind):
        with self._lock:
            self._runner_done[kind] = True
            if self._state != "finishing":
                return
            self._pending.discard(kind)
            if self._pending:
                return
        self._complete()

    def stop_record(self):
        with self._lock:
            if self._state != "recording":
                return False
            self._state = "finishing"
            self._stop_t = time.monotonic()
            stop_evt = self._stop_evt
            pending = {k for k in self._sessions if not self._runner_done.get(k)}
            self._pending = pending
            fanout = self._fanout
            self._fanout = None
        if stop_evt is not None:
            stop_evt.set()
        if fanout is not None:
            self.app.mic.detach(fanout)
        if self.app.mic_mode != "always":
            try:
                self.app.mic.close()
            except Exception:
                pass
        if not pending:
            self._complete()
        return True

    def _complete(self):
        with self._lock:
            if self._state != "finishing":
                return
            ok = [(k, self._times[k]) for k in self._times if (self._text.get(k) or "").strip()]
            self._fastest = None
            self._slowest = None
            if len(ok) >= 2:
                f = min(ok, key=lambda x: x[1])[0]
                s = max(ok, key=lambda x: x[1])[0]
                if f != s:
                    self._fastest = f
                    self._slowest = s
            self._state = "idle"
            self._pending = set()

    def leave(self):
        with self._lock:
            self._active = False
            self.app.testing = False
            stop_evt = self._stop_evt
            sessions = list(self._sessions.values())
            fanout = self._fanout
            self._fanout = None
            self._state = "idle"
        if stop_evt is not None:
            stop_evt.set()
        if fanout is not None:
            self.app.mic.detach(fanout)
        for sess in sessions:
            try:
                sess.cancel()
            except Exception:
                pass
        if self.app.mic_mode != "always":
            try:
                self.app.mic.close()
            except Exception:
                pass

    def snapshot(self):
        with self._lock:
            state = self._state
            ready = self._engines is not None
            rec_sec = (time.monotonic() - self._record_t) if state == "recording" and self._record_t else 0.0
            fin_sec = (time.monotonic() - self._stop_t) if state == "finishing" and self._stop_t else 0.0
            max_t = max(self._times.values()) if self._times else 0.0
            rows = []
            for kind in self.h["ASR_ENGINES"]:
                err = self._errors.get(kind)
                t = self._times.get(kind)
                txt = self._text.get(kind, "")
                st = self._status.get(kind, "")
                badge = ""
                if t is not None:
                    if not txt.strip():
                        status_label = f"无结果 · {t:.2f}s"
                        status_kind = "empty"
                    else:
                        status_label = f"{t:.2f}s"
                        status_kind = "done"
                        if kind == self._fastest:
                            badge = "fastest"
                        elif kind == self._slowest:
                            badge = "slowest"
                elif err:
                    status_label = err
                    status_kind = "error"
                elif state == "recording":
                    status_label = "录音中" if st == "started" else "连接中…"
                    status_kind = "rec"
                elif state == "finishing":
                    status_label = f"识别中 {fin_sec:.1f}s"
                    status_kind = "wait"
                elif ready:
                    status_label = "就绪"
                    status_kind = "idle"
                else:
                    status_label = "准备中…"
                    status_kind = "prep"
                rows.append({
                    "id": kind,
                    "label": self.h["ENGINE_LABELS"][kind],
                    "status": status_label,
                    "status_kind": status_kind,
                    "badge": badge,
                    "time": round(t, 2) if t is not None else None,
                    "pct": round(min(100.0, (t / max(max_t, 0.2)) * 100.0), 1) if t is not None else 0,
                    "text": txt,
                    "is_current": kind == self.app.engine_kind,
                })
        return {
            "state": state,
            "ready": ready,
            "rec_sec": round(rec_sec, 1),
            "rows": rows,
        }


# ============================================================ WebSettings Server

class WebSettings:
    """现代设置面板（对外完全兼容原版 Settings 的 start/open/open_capture/refresh 接口）。"""

    def __init__(self, app, helpers):
        self.app = app
        self.h = helpers
        self.hwnd = None
        self._token = secrets.token_urlsafe(20)
        self._server = None
        self._port = 0
        self._lock = threading.Lock()
        self._pending_tab = None
        self._mic_cache = None
        self._preview_gen = 0
        self.bench = EngineTestController(app, helpers)

    def start(self):
        self._ensure_server()

    def _ensure_server(self):
        with self._lock:
            if self._server is not None:
                return
            ws = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    pass

                def _send_json(self, obj, code=200):
                    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw)

                def _send_html(self, html):
                    raw = html.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(raw)

                def _check_token(self, parsed):
                    if self.headers.get("X-Token") == ws._token:
                        return True
                    qs = urllib.parse.parse_qs(parsed.query)
                    return qs.get("token", [""])[0] == ws._token

                def do_GET(self):
                    parsed = urllib.parse.urlparse(self.path)
                    if not self._check_token(parsed):
                        self.send_error(403, "Forbidden")
                        return
                    if parsed.path in ("/", "/index.html"):
                        html = _HTML_TEMPLATE.replace("__AUTH_TOKEN__", ws._token)
                        self._send_html(html)
                        return
                    if parsed.path == "/api/state":
                        self._send_json(ws._build_state())
                        return
                    if parsed.path == "/api/test/status":
                        self._send_json(ws.bench.snapshot())
                        return
                    self.send_error(404)

                def do_POST(self):
                    parsed = urllib.parse.urlparse(self.path)
                    if not self._check_token(parsed):
                        self.send_error(403, "Forbidden")
                        return
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    raw = self.rfile.read(min(length, 262144)) if length > 0 else b"{}"
                    try:
                        body = json.loads(raw.decode("utf-8") or "{}")
                    except ValueError:
                        body = {}
                    try:
                        if parsed.path == "/api/option":
                            res = ws._handle_option(body)
                            self._send_json(res)
                        elif parsed.path == "/api/action":
                            res = ws._handle_action(body)
                            self._send_json(res)
                        elif parsed.path == "/api/test/init":
                            ws.bench.ensure_ready()
                            self._send_json({"ok": True, "bench": ws.bench.snapshot()})
                        elif parsed.path == "/api/test/start":
                            ok, msg = ws.bench.start_record()
                            self._send_json({"ok": ok, "msg": msg, "bench": ws.bench.snapshot()})
                        elif parsed.path == "/api/test/stop":
                            ws.bench.stop_record()
                            self._send_json({"ok": True, "bench": ws.bench.snapshot()})
                        elif parsed.path == "/api/test/leave":
                            ws.bench.leave()
                            self._send_json({"ok": True})
                        else:
                            self.send_error(404)
                    except Exception as exc:
                        ws.h["log"]("web api error " + traceback.format_exc().replace("\n", " | "))
                        self._send_json({"ok": False, "msg": str(exc)}, code=500)

            srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            srv.daemon_threads = True
            self._server = srv
            self._port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.h["log"](f"web settings server ready on 127.0.0.1:{self._port}")

    def open(self, tab=None):
        self._ensure_server()
        if tab:
            self._pending_tab = tab
        self._mic_cache = None  # 每次打开刷新麦克风列表
        theme = ui_themes.get_theme(self.app.theme_id)
        existing = _find_settings_hwnd()
        if existing:
            self.hwnd = existing
            _focus_hwnd(existing, theme["is_dark"])
            return
        url = f"http://127.0.0.1:{self._port}/?token={self._token}"
        edge = _find_edge()
        if edge:
            os.makedirs(PROFILE_DIR, exist_ok=True)
            cmd = [
                edge,
                f"--app={url}",
                f"--user-data-dir={PROFILE_DIR}",
                "--window-size=980,690",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            try:
                subprocess.Popen(cmd, creationflags=0x08000000)  # CREATE_NO_WINDOW
                _sync_titlebar_async(theme["is_dark"], delay=0.35)
                return
            except Exception:
                self.h["log"]("launch edge app failed " + traceback.format_exc().replace("\n", " | "))
        webbrowser.open(url)

    def open_capture(self, target="voice"):
        self.open(tab="smart" if target == "smart" else "voice")
        self.app.start_capture(target)

    def open_engine_test(self):
        self.bench.ensure_ready()
        self.open(tab="benchmark")

    def refresh(self):
        theme = ui_themes.get_theme(self.app.theme_id)
        hwnd = _find_settings_hwnd()
        if hwnd:
            self.hwnd = hwnd
            ui_gdiplus.apply_window_dark_titlebar(hwnd, theme["is_dark"])

    def shutdown(self):
        try:
            self.bench.leave()
        except Exception:
            pass
        srv = self._server
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def _get_mics(self, force=False):
        if self._mic_cache is None or force:
            try:
                self._mic_cache = self.h["list_input_devices"]()
            except Exception:
                self._mic_cache = []
        return self._mic_cache

    def _build_state(self):
        app = self.app
        tab = self._pending_tab
        self._pending_tab = None
        rules = [{"src": s, "dst": d} for s, d in self.h["load_hotwords"]()]
        with app.ui.lock:
            cap_msg = app.ui.msg if app.capturing else ""
        return {
            "pending_tab": tab,
            "appearance": {
                "theme": app.theme_id,
                "overlay_pos": app.overlay_pos,
                "overlay_style": app.overlay_style,
                "overlay_opacity": app.overlay_opacity,
                "overlay_minimal": app.overlay_minimal,
                "themes": ui_themes.theme_list(),
            },
            "voice": {
                "engine_kind": app.engine_kind,
                "engines": [{"id": k, "label": v} for k, v in self.h["ENGINE_ITEMS"]],
                "voice_mode": app.voice_mode,
                "min_hold_ms": app.min_hold_ms,
                "hotkey_display": app.hotkey.display(),
                "capturing": bool(app.capturing),
                "capture_target": app.capture_target,
                "capture_msg": cap_msg,
            },
            "mic": {
                "mic_sel": app.mic_sel,
                "mic_mode": app.mic_mode,
                "devices": self._get_mics(),
            },
            "format": {
                "punctuation": app.punctuation,
                "zh_space": app.zh_space,
                "paragraph_mode": app.paragraph_mode,
                "local_engine": app.engine_kind == "doubao",
            },
            "smart": {
                "available": self.h["SMART_AVAILABLE"],
                "enabled": app.smart_text,
                "hotkey_display": app.smart_hotkey_display(),
                "has_hotkey": bool(app.smart_hotkey),
            },
            "hotwords": {
                "rules": rules,
                "count": len(rules),
            },
            "system": {
                "float_visible": app.float_visible,
                "autostart": self.h["autostart_enabled"](),
                "release": self.h["APP_RELEASE"],
                "project_url": self.h["PROJECT_URL"],
                "log_dir": self.h["LOG_DIR"],
            },
        }

    def _handle_option(self, body):
        app = self.app
        key = body.get("key")
        val = body.get("value")
        ok = True
        msg = ""
        if key == "theme":
            ok = app.set_theme(val)
            if ok:
                self.refresh()
        elif key == "overlay_pos":
            ok = app.set_overlay_pos(val)
        elif key == "overlay_style":
            ok = app.set_overlay_style(val)
        elif key == "overlay_opacity":
            ok = app.set_overlay_opacity(val)
        elif key == "overlay_minimal":
            ok = app.set_overlay_minimal(bool(val))
        elif key == "asr_engine":
            ok = app.set_asr_engine(val)
            if not ok:
                msg = "正在录音或测速中，请稍后再切换"
        elif key == "voice_mode":
            app.set_voice_mode(val)
        elif key == "min_hold_ms":
            app.set_min_hold(int(val))
        elif key == "mic_device":
            ok = app.set_mic_selection(val if isinstance(val, dict) and val.get("name") else None)
            if not ok:
                msg = "麦克风切换失败，已恢复原设备"
        elif key == "mic_mode":
            ok = app.set_mic_mode(val)
            if not ok:
                msg = "切换麦克风模式失败"
        elif key == "punctuation":
            app.set_punctuation(val)
        elif key == "zh_space":
            app.set_zh_space(bool(val))
        elif key == "paragraph_mode":
            app.set_paragraph_mode(val)
        elif key == "smart_text":
            ok = app.set_smart_text(bool(val))
        elif key == "float_visible":
            app.set_float_visible(bool(val))
        elif key == "autostart":
            ok = app.set_autostart_enabled(bool(val))
            if not ok:
                msg = "设置开机自启失败（权限不足）"
        else:
            return {"ok": False, "msg": f"未知配置项: {key}"}
        return {"ok": ok, "msg": msg, "state": self._build_state()}

    def _run_preview(self):
        app = self.app
        if app.session is not None or app.capturing:
            return
        self._preview_gen += 1
        gen = self._preview_gen
        tname = ui_themes.get_theme(app.theme_id)["name"]

        def demo():
            app._preview_active = True
            try:
                app.set_ui("rec", f"● 聆听中…  当前主题：{tname}", live="")
                time.sleep(0.75)
                if self._preview_gen != gen or app.session is not None:
                    return
                app.set_ui("rec", f"● 聆听中…  当前主题：{tname}",
                           live="你好，这是升级后的灵动岛语音悬浮条效果预览")
                time.sleep(1.35)
                if self._preview_gen != gen or app.session is not None:
                    return
                app.set_ui("wait", "识别中…",
                           live="你好，这是升级后的灵动岛语音悬浮条效果预览")
                time.sleep(0.75)
                if self._preview_gen != gen or app.session is not None:
                    return
                app.set_ui("done", "✓ 预览完成 · 主题已实时生效", auto_hide=1500)
            finally:
                app._preview_active = False

        threading.Thread(target=demo, daemon=True).start()

    def _handle_action(self, body):
        app = self.app
        act = body.get("action")
        if act == "capture_voice":
            app.start_capture("voice")
            return {"ok": True, "state": self._build_state()}
        if act == "capture_smart":
            app.start_capture("smart")
            return {"ok": True, "state": self._build_state()}
        if act == "capture_cancel":
            app.capture_cancel()
            return {"ok": True, "state": self._build_state()}
        if act == "clear_smart_hotkey":
            app.clear_smart_hotkey()
            return {"ok": True, "state": self._build_state()}
        if act == "preview_overlay":
            self._run_preview()
            return {"ok": True}
        if act == "refresh_mics":
            self._get_mics(force=True)
            return {"ok": True, "state": self._build_state()}
        if act == "hotword_add":
            src = (body.get("src") or "").strip()
            dst = (body.get("dst") or "").strip()
            if not src or not dst:
                return {"ok": False, "msg": "请填写原文与替换为"}
            if not self.h["add_hotword"](src, dst):
                return {"ok": False, "msg": "原文不能以 # 开头或包含箭头符号"}
            return {"ok": True, "state": self._build_state()}
        if act == "hotword_delete":
            src = (body.get("src") or "").strip()
            ok = self.h["delete_hotword"](src)
            return {"ok": ok, "state": self._build_state()}
        if act == "hotword_open":
            ok = self.h["open_hotwords_file"]()
            return {"ok": ok, "msg": "" if ok else "打开 hotwords.txt 失败"}
        if act == "open_logs":
            try:
                os.makedirs(self.h["LOG_DIR"], exist_ok=True)
                os.startfile(self.h["LOG_DIR"])
                return {"ok": True}
            except OSError:
                return {"ok": False, "msg": "打开日志目录失败"}
        if act == "open_updates":
            os.startfile(f"{self.h['PROJECT_URL']}/releases/latest")
            return {"ok": True}
        if act == "open_feedback":
            os.startfile(f"{self.h['PROJECT_URL']}/issues/new")
            return {"ok": True}
        return {"ok": False, "msg": f"未知操作: {act}"}


# ============================================================ Embedded Modern UI

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>豆包语音输入 · 设置</title>
<style>
  :root {
    --bg: #0D1017;
    --bg-grad: none;
    --sidebar: #121622;
    --card: rgba(24, 29, 44, 0.86);
    --card-hover: rgba(32, 39, 58, 0.94);
    --input: #0E121C;
    --border: rgba(148, 163, 184, 0.16);
    --text: #F1F5F9;
    --muted: #94A3B8;
    --accent: #6366F1;
    --accent2: #38BDF8;
    --accent-soft: rgba(99, 102, 241, 0.18);
    --success: #34D399;
    --danger: #FB7185;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    height: 100%;
    font-family: "Microsoft YaHei UI", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif;
    background-color: var(--bg);
    background-image: var(--bg-grad);
    color: var(--text);
    overflow: hidden;
    user-select: none;
    transition: background-color .25s ease, color .25s ease;
  }
  .shell {
    display: flex;
    height: 100vh;
    width: 100vw;
  }
  /* Sidebar */
  .sidebar {
    width: 216px;
    flex-shrink: 0;
    background: var(--sidebar);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    padding: 20px 14px 16px;
    transition: background .25s ease, border-color .25s ease;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 11px;
    padding: 4px 8px 20px;
  }
  .brand-logo {
    width: 36px;
    height: 36px;
    border-radius: 12px;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    display: grid;
    place-items: center;
    box-shadow: 0 6px 18px -4px var(--accent-soft);
    flex-shrink: 0;
  }
  .brand-logo svg { width: 20px; height: 20px; color: #fff; }
  .brand-title { font-size: 15px; font-weight: 700; letter-spacing: .2px; }
  .brand-sub { font-size: 11px; color: var(--muted); margin-top: 2px; }

  .nav { display: flex; flex-direction: column; gap: 4px; flex: 1; }
  .nav-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 10px;
    cursor: pointer;
    color: var(--muted);
    font-size: 13.5px;
    font-weight: 500;
    border: 1px solid transparent;
    transition: all .18s ease;
  }
  .nav-item svg { width: 17px; height: 17px; flex-shrink: 0; opacity: .85; }
  .nav-item:hover { background: var(--accent-soft); color: var(--text); }
  .nav-item.active {
    background: var(--accent-soft);
    color: var(--text);
    border-color: var(--border);
    font-weight: 600;
  }
  .nav-item.active svg { color: var(--accent); opacity: 1; }

  .sidebar-footer {
    padding: 12px 10px 2px;
    border-top: 1px solid var(--border);
    font-size: 11.5px;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .pill-ver {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 99px;
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 600;
  }

  /* Main Content */
  .main {
    flex: 1;
    overflow-y: auto;
    padding: 26px 30px 36px;
  }
  .main::-webkit-scrollbar { width: 7px; }
  .main::-webkit-scrollbar-track { background: transparent; }
  .main::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; }

  .page { display: none; animation: fadeIn .2s ease; }
  .page.active { display: block; }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .page-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 20px;
  }
  .page-title { font-size: 21px; font-weight: 700; letter-spacing: -.2px; }
  .page-desc { font-size: 13px; color: var(--muted); margin-top: 4px; }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 20px;
    margin-bottom: 16px;
    backdrop-filter: blur(12px);
    transition: background .2s ease, border-color .2s ease;
  }
  .card-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .card-title small { font-weight: 400; color: var(--muted); font-size: 12px; }

  /* Theme Grid */
  .theme-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
  }
  @media (max-width: 820px) {
    .theme-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  .theme-card {
    position: relative;
    border-radius: 13px;
    padding: 12px;
    cursor: pointer;
    border: 1.5px solid var(--border);
    background: var(--input);
    transition: transform .16s ease, border-color .18s ease, box-shadow .18s ease;
    overflow: hidden;
  }
  .theme-card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .theme-card.active {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-soft);
  }
  .mini-stage {
    height: 54px;
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 10px;
    position: relative;
    overflow: hidden;
  }
  .mini-capsule {
    height: 26px;
    padding: 0 11px;
    border-radius: 99px;
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10.5px;
    font-weight: 600;
    box-shadow: 0 4px 12px rgba(0,0,0,.22);
  }
  .mini-dot { width: 6px; height: 6px; border-radius: 50%; }
  .mini-bars { display: flex; align-items: center; gap: 2px; height: 12px; }
  .mini-bars span { width: 2px; border-radius: 2px; display: block; }
  .theme-meta { display: flex; align-items: center; justify-content: space-between; }
  .theme-name { font-size: 13px; font-weight: 600; }
  .theme-tag {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 99px;
    background: var(--accent-soft);
    color: var(--muted);
  }
  .theme-desc {
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
    line-height: 1.35;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .theme-check {
    position: absolute;
    top: 8px;
    right: 8px;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    color: #fff;
    display: none;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 700;
    box-shadow: 0 2px 6px rgba(0,0,0,.25);
  }
  .theme-card.active .theme-check { display: flex; }

  /* Rows & Controls */
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 11px 0;
    border-bottom: 1px solid var(--border);
  }
  .row:last-child { border-bottom: none; padding-bottom: 2px; }
  .row:first-of-type { padding-top: 2px; }
  .row-info { flex: 1; min-width: 0; }
  .row-label { font-size: 13.5px; font-weight: 500; }
  .row-hint { font-size: 12px; color: var(--muted); margin-top: 3px; line-height: 1.4; }

  /* Toggle Switch */
  .switch {
    position: relative;
    width: 44px;
    height: 24px;
    flex-shrink: 0;
    cursor: pointer;
  }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute;
    inset: 0;
    background: var(--border);
    border-radius: 99px;
    transition: .2s ease;
  }
  .slider::before {
    content: "";
    position: absolute;
    width: 18px;
    height: 18px;
    left: 3px;
    top: 3px;
    border-radius: 50%;
    background: #fff;
    box-shadow: 0 2px 5px rgba(0,0,0,.25);
    transition: .2s ease;
  }
  .switch input:checked + .slider {
    background: linear-gradient(135deg, var(--accent), var(--accent2));
  }
  .switch input:checked + .slider::before { transform: translateX(20px); }

  /* Segmented Control */
  .seg {
    display: inline-flex;
    background: var(--input);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 3px;
    gap: 3px;
  }
  .seg button {
    border: none;
    background: transparent;
    color: var(--muted);
    padding: 6px 13px;
    border-radius: 7px;
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    transition: all .16s ease;
  }
  .seg button.active {
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    color: #fff;
    font-weight: 600;
    box-shadow: 0 2px 8px var(--accent-soft);
  }

  /* Inputs & Selects */
  select, input[type="text"] {
    background: var(--input);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 8px 12px;
    font-size: 13px;
    font-family: inherit;
    outline: none;
    transition: border-color .15s ease;
  }
  select:focus, input[type="text"]:focus { border-color: var(--accent); }
  select:disabled { opacity: .45; cursor: not-allowed; }
  select { min-width: 210px; cursor: pointer; }
  option { background: var(--sidebar); color: var(--text); }

  .range-wrap { display: flex; align-items: center; gap: 10px; }
  input[type="range"] {
    width: 150px;
    accent-color: var(--accent);
    cursor: pointer;
  }
  .range-val {
    font-size: 12.5px;
    font-weight: 600;
    color: var(--accent);
    min-width: 38px;
    text-align: right;
  }

  /* Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 8px 15px;
    border-radius: 9px;
    border: 1px solid var(--border);
    background: var(--input);
    color: var(--text);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    font-family: inherit;
    transition: all .16s ease;
    white-space: nowrap;
  }
  .btn:hover { background: var(--card-hover); border-color: var(--accent); }
  .btn:disabled { opacity: .45; cursor: not-allowed; }
  .btn-primary {
    background: linear-gradient(135deg, var(--accent), var(--accent2));
    border-color: transparent;
    color: #fff;
    font-weight: 600;
    box-shadow: 0 6px 16px -4px var(--accent-soft);
  }
  .btn-primary:hover { filter: brightness(1.08); border-color: transparent; }
  .btn-danger {
    background: rgba(251, 113, 133, 0.14);
    border-color: rgba(251, 113, 133, 0.35);
    color: var(--danger);
  }
  .btn-danger:hover { background: rgba(251, 113, 133, 0.24); }
  .btn-sm { padding: 5px 11px; font-size: 12px; border-radius: 7px; }

  /* Hotkey KBD & Capture Banner */
  .kbd {
    display: inline-flex;
    align-items: center;
    padding: 5px 12px;
    border-radius: 8px;
    background: var(--input);
    border: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    color: var(--text);
    box-shadow: inset 0 -2px 0 var(--border);
  }
  .capture-banner {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 16px;
    border-radius: 12px;
    margin-bottom: 16px;
    background: var(--accent-soft);
    border: 1.5px solid var(--accent);
    animation: pulseBorder 1.5s infinite;
  }
  .capture-banner.show { display: flex; }
  @keyframes pulseBorder {
    0%, 100% { box-shadow: 0 0 0 0 var(--accent-soft); }
    50% { box-shadow: 0 0 0 5px var(--accent-soft); }
  }

  /* Hotwords */
  .hw-form { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
  .hw-form input { flex: 1; }
  .hw-arrow { color: var(--accent); font-weight: 700; }
  .hw-list {
    display: flex;
    flex-direction: column;
    gap: 7px;
    max-height: 240px;
    overflow-y: auto;
    padding-right: 4px;
  }
  .hw-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-radius: 9px;
    background: var(--input);
    border: 1px solid var(--border);
    font-size: 13px;
  }
  .hw-pair { display: flex; align-items: center; gap: 10px; }
  .hw-src { color: var(--muted); text-decoration: line-through; }
  .hw-dst { font-weight: 600; color: var(--text); }
  .hw-del {
    border: none;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    font-size: 15px;
    padding: 2px 6px;
    border-radius: 6px;
  }
  .hw-del:hover { color: var(--danger); background: rgba(251,113,133,.14); }
  .empty-tip { text-align: center; color: var(--muted); font-size: 12.5px; padding: 20px 0; }

  /* Benchmark */
  .bench-bar {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 16px;
  }
  .bench-rec-btn {
    min-width: 210px;
    height: 44px;
    font-size: 14px;
    border-radius: 12px;
  }
  .bench-rec-btn.recording {
    background: linear-gradient(135deg, #F43F5E, #E11D48);
    box-shadow: 0 0 0 4px rgba(244, 63, 94, 0.24);
  }
  .bench-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .bench-card {
    background: var(--input);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 13px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .bench-card.fastest { border-color: var(--success); }
  .bench-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .bench-name { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; }
  .badge {
    font-size: 10.5px;
    padding: 2px 7px;
    border-radius: 99px;
    font-weight: 600;
  }
  .badge-fast { background: rgba(52,211,153,.18); color: var(--success); }
  .badge-slow { background: rgba(251,113,133,.16); color: var(--danger); }
  .badge-cur { background: var(--accent-soft); color: var(--accent); }
  .bench-status { font-size: 12px; color: var(--muted); font-weight: 500; }
  .bench-text {
    font-size: 13px;
    min-height: 36px;
    padding: 8px 10px;
    border-radius: 8px;
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    line-height: 1.45;
    user-select: text;
    word-break: break-all;
  }
  .bench-meter {
    height: 4px;
    border-radius: 99px;
    background: var(--border);
    overflow: hidden;
  }
  .bench-meter-fill {
    height: 100%;
    border-radius: 99px;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    transition: width .3s ease;
  }

  /* Toast */
  .toast {
    position: fixed;
    bottom: 22px;
    right: 26px;
    padding: 10px 16px;
    border-radius: 10px;
    background: var(--sidebar);
    border: 1px solid var(--accent);
    color: var(--text);
    font-size: 13px;
    font-weight: 500;
    box-shadow: 0 10px 28px rgba(0,0,0,.35);
    opacity: 0;
    transform: translateY(10px);
    pointer-events: none;
    transition: all .2s ease;
    z-index: 99;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-logo">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="9" y="2" width="6" height="12" rx="3"/>
          <path d="M5 10a7 7 0 0 0 14 0"/>
          <line x1="12" y1="17" x2="12" y2="22"/>
          <line x1="8" y1="22" x2="16" y2="22"/>
        </svg>
      </div>
      <div>
        <div class="brand-title">豆包语音输入</div>
        <div class="brand-sub">独立极速语音工具</div>
      </div>
    </div>

    <nav class="nav" id="nav">
      <div class="nav-item active" data-tab="appearance">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="13.5" cy="6.5" r="1.5"/><circle cx="17.5" cy="10.5" r="1.5"/><circle cx="8.5" cy="7.5" r="1.5"/><circle cx="6.5" cy="12.5" r="1.5"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.9 0 1.6-.7 1.6-1.6 0-.4-.2-.8-.4-1.1-.3-.3-.4-.7-.4-1.1 0-.9.7-1.6 1.6-1.6H16c3.3 0 6-2.7 6-6 0-4.9-4.5-8.6-10-8.6z"/></svg>
        外观与主题
      </div>
      <div class="nav-item" data-tab="voice">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/></svg>
        语音与热键
      </div>
      <div class="nav-item" data-tab="mic">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
        麦克风与格式
      </div>
      <div class="nav-item" data-tab="smart">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 5.8L20 10l-6.1 1.2L12 17l-1.9-5.8L4 10l6.1-1.2z"/></svg>
        智能整理与热词
      </div>
      <div class="nav-item" data-tab="benchmark">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        引擎测速对比
      </div>
      <div class="nav-item" data-tab="system">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.26.6.85 1 1.51 1H21a2 2 0 1 1 0 4h-.09c-.66 0-1.25.4-1.51 1z"/></svg>
        系统与关于
      </div>
    </nav>

    <div class="sidebar-footer">
      <span>轻量原生版</span>
      <span class="pill-ver" id="ver-badge">v2.2.6</span>
    </div>
  </aside>

  <main class="main">
    <div class="capture-banner" id="capture-banner">
      <div>
        <div style="font-weight:600;font-size:13.5px">正在录制快捷键…</div>
        <div style="font-size:12px;color:var(--muted);margin-top:2px" id="capture-msg">请按下想要设置的键盘组合或鼠标中键/侧键，按 Esc 取消</div>
      </div>
      <button class="btn btn-sm" onclick="doAction('capture_cancel')">取消录制</button>
    </div>

    <!-- 1. 外观与主题 -->
    <section class="page active" id="page-appearance">
      <div class="page-head">
        <div>
          <div class="page-title">外观与主题</div>
          <div class="page-desc">一键切换设置中心、录音悬浮胶囊与悬浮球的整套视觉风格</div>
        </div>
        <button class="btn btn-primary" onclick="doAction('preview_overlay')">✨ 演示桌面悬浮条</button>
      </div>

      <div class="card">
        <div class="card-title">
          <span>内置精选主题（8 套）</span>
          <small>点击立即生效并自动记忆</small>
        </div>
        <div class="theme-grid" id="theme-grid"></div>
      </div>

      <div class="card">
        <div class="card-title"><span>悬浮胶囊与悬浮球样式</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">胶囊造型</div>
            <div class="row-hint">在灵动岛全圆角胶囊与现代磨砂卡片之间切换</div>
          </div>
          <div class="seg" id="seg-style">
            <button data-val="island" onclick="setOpt('overlay_style','island')">灵动胶囊</button>
            <button data-val="card" onclick="setOpt('overlay_style','card')">磨砂卡片</button>
          </div>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">悬浮条位置</div>
            <div class="row-hint">支持经典屏幕底部居中，或类似 macOS 的顶部居中</div>
          </div>
          <div class="seg" id="seg-pos">
            <button data-val="bottom" onclick="setOpt('overlay_pos','bottom')">屏幕底部</button>
            <button data-val="top" onclick="setOpt('overlay_pos','top')">屏幕顶部</button>
          </div>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">悬浮条不透明度</div>
            <div class="row-hint">微调悬浮胶囊与桌面背景的通透融合度</div>
          </div>
          <div class="range-wrap">
            <input type="range" id="range-opacity" min="70" max="100" step="2" />
            <span class="range-val" id="val-opacity">94%</span>
          </div>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">极简灵动岛模式</div>
            <div class="row-hint">隐藏冗长的快捷键提示，只显示「聆听中」+ 实时秒表与声波，胶囊更精致小巧</div>
          </div>
          <label class="switch">
            <input type="checkbox" id="chk-minimal" onchange="setOpt('overlay_minimal', this.checked)" />
            <span class="slider"></span>
          </label>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">显示桌面悬浮录音球</div>
            <div class="row-hint">高颜值可拖动悬浮球，单击即可说话，右键快速隐藏</div>
          </div>
          <label class="switch">
            <input type="checkbox" id="chk-float" onchange="setOpt('float_visible', this.checked)" />
            <span class="slider"></span>
          </label>
        </div>
      </div>
    </section>

    <!-- 2. 语音与热键 -->
    <section class="page" id="page-voice">
      <div class="page-head">
        <div>
          <div class="page-title">语音与热键</div>
          <div class="page-desc">选择语音识别引擎、触发模式及全局语音快捷键</div>
        </div>
        <button class="btn" onclick="switchTab('benchmark')">⚡ 测试全部引擎</button>
      </div>

      <div class="card">
        <div class="card-title"><span>识别引擎与说话方式</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">语音识别引擎</div>
            <div class="row-hint">豆包本地 DLL 响应极快；也可随时切换五大云端识别引擎</div>
          </div>
          <select id="sel-engine" onchange="setOpt('asr_engine', this.value)"></select>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">语音触发模式</div>
            <div class="row-hint">长按模式松开即输入；免按模式按一下开始、再按一下或任意键结束</div>
          </div>
          <div class="seg" id="seg-vmode">
            <button data-val="hold" onclick="setOpt('voice_mode','hold')">长按说话</button>
            <button data-val="hands_free" onclick="setOpt('voice_mode','hands_free')">免按模式</button>
          </div>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">长按最短触发时长</div>
            <div class="row-hint">短于该时长的误触会自动忽略，不会上传识别</div>
          </div>
          <select id="sel-minhold" onchange="setOpt('min_hold_ms', Number(this.value))">
            <option value="0">不限制</option>
            <option value="150">0.15 秒</option>
            <option value="250">0.25 秒（推荐）</option>
            <option value="400">0.40 秒</option>
          </select>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>全局语音快捷键</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">当前语音快捷键</div>
            <div class="row-hint">支持右侧 Ctrl/Alt/Shift 单键、组合键、F1–F12 或鼠标中键/侧键</div>
          </div>
          <div style="display:flex;align-items:center;gap:10px">
            <span class="kbd" id="lbl-hotkey">右 Alt</span>
            <button class="btn btn-primary" onclick="doAction('capture_voice')">更改快捷键</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 3. 麦克风与格式 -->
    <section class="page" id="page-mic">
      <div class="page-head">
        <div>
          <div class="page-title">麦克风与格式</div>
          <div class="page-desc">指定音频输入设备与语音转文字的标点排版规则</div>
        </div>
        <button class="btn" onclick="doAction('refresh_mics')">🔄 刷新麦克风列表</button>
      </div>

      <div class="card">
        <div class="card-title"><span>麦克风设置</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">输入设备</div>
            <div class="row-hint">默认自动跟随系统首选录音设备</div>
          </div>
          <select id="sel-mic" style="max-width:340px" onchange="onMicChange(this.value)"></select>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">麦克风预热</div>
            <div class="row-hint">空闲时保持连接但不上传，仅缓存最近 0.15 秒防止吞掉开口第一个字；蓝牙耳机建议按需开启</div>
          </div>
          <label class="switch">
            <input type="checkbox" id="chk-mic-always" onchange="setOpt('mic_mode', this.checked ? 'always' : 'press')" />
            <span class="slider"></span>
          </label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>文字格式</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">标点展示</div>
            <div class="row-hint">控制句末及句中标点的保留与替换方式</div>
          </div>
          <select id="sel-punct" onchange="setOpt('punctuation', this.value)">
            <option value="keep_all">保留所有标点</option>
            <option value="remove_period">句末不加句号</option>
            <option value="remove_trailing">句末不加标点</option>
            <option value="space_replace">空格代替标点</option>
          </select>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">中英文 / 数字间距</div>
            <div class="row-hint" id="lbl-space-hint">仅豆包本地引擎支持由识别服务自动控制中英文空格</div>
          </div>
          <select id="sel-space" onchange="setOpt('zh_space', this.value === '1')">
            <option value="1">前后加空格</option>
            <option value="0">前后无空格</option>
          </select>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">自动分段方式</div>
            <div class="row-hint">长语音输出时是否按句号/问号/感叹号自动换行</div>
          </div>
          <select id="sel-para" onchange="setOpt('paragraph_mode', this.value)">
            <option value="none">不自动分段</option>
            <option value="blank">分段后空行</option>
            <option value="single">分段不空行</option>
          </select>
        </div>
      </div>
    </section>

    <!-- 4. 智能整理与热词 -->
    <section class="page" id="page-smart">
      <div class="page-head">
        <div>
          <div class="page-title">智能整理与热词</div>
          <div class="page-desc">AI 自动顺句润色，以及专有名词/缩写词条自动替换</div>
        </div>
      </div>

      <div class="card" id="card-smart">
        <div class="card-title"><span>智能文字整理</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">语音输入后自动排版、纠错与润色</div>
            <div class="row-hint">输入完成后调用豆包服务整理口语顺句，随时可按 Ctrl+Z 撤销恢复原文</div>
          </div>
          <label class="switch">
            <input type="checkbox" id="chk-smart" onchange="setOpt('smart_text', this.checked)" />
            <span class="slider"></span>
          </label>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">选中文字整理快捷键</div>
            <div class="row-hint">在任意窗口选中 5 字以上文本后按此快捷键，一键润色替换</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="kbd" id="lbl-smart-hk">未设置</span>
            <button class="btn btn-sm" onclick="doAction('capture_smart')">设置</button>
            <button class="btn btn-sm btn-danger" id="btn-smart-clear" onclick="doAction('clear_smart_hotkey')">清除</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">
          <span>热词替换规则（<span id="hw-count">0</span> 条）</span>
          <button class="btn btn-sm" onclick="doAction('hotword_open')">打开 hotwords.txt</button>
        </div>
        <div class="hw-form">
          <input type="text" id="hw-src" placeholder="容易识别错的原文（如：豆包儿）" />
          <span class="hw-arrow">→</span>
          <input type="text" id="hw-dst" placeholder="替换为正确词条（如：豆包）" />
          <button class="btn btn-primary" onclick="addHotword()">添加规则</button>
        </div>
        <div class="hw-list" id="hw-list"></div>
      </div>
    </section>

    <!-- 5. 引擎测速对比 -->
    <section class="page" id="page-benchmark">
      <div class="page-head">
        <div>
          <div class="page-title">6 引擎并发测速对比</div>
          <div class="page-desc">同一段录音实时发给六个语音引擎，直观对比识别准确率与返回速度</div>
        </div>
      </div>

      <div class="card">
        <div class="bench-bar">
          <button class="btn btn-primary bench-rec-btn" id="btn-bench-rec"
                  onmousedown="benchDown()" onmouseup="benchUp()" onclick="benchClick()">
            🎙️ 正在准备引擎…
          </button>
          <div style="font-size:12.5px;color:var(--muted);line-height:1.5">
            支持 <b>按住说话松开结束</b>，也支持 <b>单击开始、再点一次结束</b>。<br/>
            计时从停止说话开始，方便直观挑出你网络下最快最准的引擎。
          </div>
        </div>
        <div class="bench-grid" id="bench-grid"></div>
      </div>
    </section>

    <!-- 6. 系统与关于 -->
    <section class="page" id="page-system">
      <div class="page-head">
        <div>
          <div class="page-title">系统与关于</div>
          <div class="page-desc">启动项、运行日志与开源项目信息</div>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>系统选项</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">开机自动启动</div>
            <div class="row-hint">登录 Windows 后自动在系统托盘静默待命</div>
          </div>
          <label class="switch">
            <input type="checkbox" id="chk-autostart" onchange="setOpt('autostart', this.checked)" />
            <span class="slider"></span>
          </label>
        </div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">运行日志目录</div>
            <div class="row-hint" id="lbl-logdir">logs</div>
          </div>
          <button class="btn" onclick="doAction('open_logs')">打开日志文件夹</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><span>关于豆包语音输入</span></div>
        <div class="row">
          <div class="row-info">
            <div class="row-label">DoubaoVoiceIME · 轻量主题升级版</div>
            <div class="row-hint">基于开源项目 yc-2018/DoubaoVoiceIME 升级原生 GDI+ 灵动岛悬浮条与现代多主题设置中心</div>
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-sm" onclick="doAction('open_updates')">检查更新</button>
            <button class="btn btn-sm" onclick="doAction('open_feedback')">意见反馈</button>
          </div>
        </div>
      </div>
    </section>
  </main>
</div>

<div class="toast" id="toast"></div>

<script>
const TOKEN = "__AUTH_TOKEN__";
let currentState = null;
let currentTab = "appearance";
let benchPoll = null;
let benchPressTime = 0;
let benchMode = null;
let benchIgnoreClick = false;
let toastTimer = null;

async function api(path, body) {
  const opts = {
    method: body ? "POST" : "GET",
    headers: { "X-Token": TOKEN }
  };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  return await r.json();
}

function showToast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

function applyWebTheme(themeId, themes) {
  const t = (themes || []).find(x => x.id === themeId) || (themes || [])[0];
  if (!t || !t.web) return;
  const root = document.documentElement.style;
  const w = t.web;
  root.setProperty("--bg", w.bg);
  root.setProperty("--bg-grad", w.bg_grad);
  root.setProperty("--sidebar", w.sidebar);
  root.setProperty("--card", w.card);
  root.setProperty("--card-hover", w.card_hover);
  root.setProperty("--input", w.input);
  root.setProperty("--border", w.border);
  root.setProperty("--text", w.text);
  root.setProperty("--muted", w.muted);
  root.setProperty("--accent", w.accent);
  root.setProperty("--accent2", w.accent2);
  root.setProperty("--accent-soft", w.accent_soft);
  root.setProperty("--success", w.success);
  root.setProperty("--danger", w.danger);
}

function switchTab(tab) {
  if (currentTab === "benchmark" && tab !== "benchmark") {
    api("/api/test/leave", {});
    clearInterval(benchPoll);
    benchPoll = null;
  }
  currentTab = tab;
  document.querySelectorAll(".nav-item").forEach(n => {
    n.classList.toggle("active", n.dataset.tab === tab);
  });
  document.querySelectorAll(".page").forEach(p => {
    p.classList.toggle("active", p.id === "page-" + tab);
  });
  if (tab === "benchmark") {
    api("/api/test/init", {}).then(res => {
      if (res && res.bench) renderBench(res.bench);
    });
    if (!benchPoll) {
      benchPoll = setInterval(async () => {
        if (currentTab !== "benchmark") return;
        const b = await api("/api/test/status");
        renderBench(b);
      }, 220);
    }
  }
}

document.querySelectorAll(".nav-item").forEach(el => {
  el.addEventListener("click", () => switchTab(el.dataset.tab));
});

function renderThemes(ap) {
  const grid = document.getElementById("theme-grid");
  const cur = ap.theme;
  const sig = cur + "|" + ap.themes.map(t => t.id).join(",");
  if (grid.dataset.sig === sig) return;
  grid.dataset.sig = sig;
  grid.innerHTML = ap.themes.map(t => {
    const ov = t.overlay;
    const active = t.id === cur ? "active" : "";
    const stageBg = t.is_dark
      ? `linear-gradient(135deg, ${t.web.bg}, ${t.web.sidebar})`
      : `linear-gradient(135deg, #DDE7F6, #F1F5FB)`;
    const capBg = `linear-gradient(180deg, ${ov.bg_top}, ${ov.bg_bottom})`;
    return `
      <div class="theme-card ${active}" onclick="setOpt('theme','${t.id}')">
        <div class="theme-check">✓</div>
        <div class="mini-stage" style="background:${stageBg}">
          <div class="mini-capsule" style="background:${capBg};border:1px solid ${ov.border};color:${ov.text}">
            <span class="mini-dot" style="background:${ov.rec_dot}"></span>
            <span class="mini-bars">
              <span style="height:5px;background:${ov.accent}"></span>
              <span style="height:11px;background:${ov.accent}"></span>
              <span style="height:8px;background:${ov.accent2}"></span>
              <span style="height:12px;background:${ov.accent2}"></span>
              <span style="height:6px;background:${ov.accent}"></span>
            </span>
            <span>聆听中</span>
          </div>
        </div>
        <div class="theme-meta">
          <span class="theme-name">${t.name}</span>
          <span class="theme-tag">${t.is_dark ? "深色" : "浅色"}</span>
        </div>
        <div class="theme-desc" title="${t.desc}">${t.desc}</div>
      </div>`;
  }).join("");
}

function setSeg(id, val) {
  document.querySelectorAll(`#${id} button`).forEach(b => {
    b.classList.toggle("active", b.dataset.val === String(val));
  });
}

function renderState(st) {
  if (!st) return;
  currentState = st;
  if (st.pending_tab && st.pending_tab !== currentTab) {
    switchTab(st.pending_tab);
  }
  const ap = st.appearance;
  applyWebTheme(ap.theme, ap.themes);
  renderThemes(ap);
  setSeg("seg-style", ap.overlay_style);
  setSeg("seg-pos", ap.overlay_pos);
  const ro = document.getElementById("range-opacity");
  if (document.activeElement !== ro) {
    ro.value = ap.overlay_opacity;
    document.getElementById("val-opacity").textContent = ap.overlay_opacity + "%";
  }
  document.getElementById("chk-minimal").checked = !!ap.overlay_minimal;
  document.getElementById("chk-float").checked = st.system.float_visible;

  // Capture banner
  const cb = document.getElementById("capture-banner");
  cb.classList.toggle("show", st.voice.capturing);
  if (st.voice.capturing && st.voice.capture_msg) {
    document.getElementById("capture-msg").textContent = st.voice.capture_msg;
  }

  // Voice
  const selEng = document.getElementById("sel-engine");
  if (selEng.options.length !== st.voice.engines.length) {
    selEng.innerHTML = st.voice.engines.map(e => `<option value="${e.id}">${e.label}</option>`).join("");
  }
  selEng.value = st.voice.engine_kind;
  setSeg("seg-vmode", st.voice.voice_mode);
  document.getElementById("sel-minhold").value = String(st.voice.min_hold_ms);
  document.getElementById("lbl-hotkey").textContent = st.voice.hotkey_display;

  // Mic
  const selMic = document.getElementById("sel-mic");
  const curMicKey = st.mic.mic_sel ? JSON.stringify(st.mic.mic_sel) : "";
  const opts = [`<option value="">自动检测（系统默认）</option>`].concat(
    st.mic.devices.map(d => {
      const k = JSON.stringify({name: d.name, hostapi: d.hostapi}).replace(/"/g, "&quot;");
      return `<option value="${k}">${esc(d.name)} [${esc(d.hostapi)}]</option>`;
    })
  ).join("");
  if (selMic.dataset.sig !== opts) {
    selMic.innerHTML = opts;
    selMic.dataset.sig = opts;
  }
  selMic.value = curMicKey;
  document.getElementById("chk-mic-always").checked = (st.mic.mic_mode === "always");

  // Format
  document.getElementById("sel-punct").value = st.format.punctuation;
  const selSpace = document.getElementById("sel-space");
  selSpace.value = st.format.zh_space ? "1" : "0";
  selSpace.disabled = !st.format.local_engine;
  document.getElementById("lbl-space-hint").textContent = st.format.local_engine
    ? "格式在下次识别时生效；数字/英文空格由本地识别服务处理"
    : "格式在下次识别时生效；当前云端引擎不提供空格选项";
  document.getElementById("sel-para").value = st.format.paragraph_mode;

  // Smart
  document.getElementById("card-smart").style.display = st.smart.available ? "" : "none";
  document.getElementById("chk-smart").checked = st.smart.enabled;
  document.getElementById("lbl-smart-hk").textContent = st.smart.hotkey_display;
  document.getElementById("btn-smart-clear").disabled = !st.smart.has_hotkey;

  // Hotwords
  document.getElementById("hw-count").textContent = st.hotwords.count;
  const hwList = document.getElementById("hw-list");
  const hwSig = JSON.stringify(st.hotwords.rules);
  if (hwList.dataset.sig !== hwSig) {
    hwList.dataset.sig = hwSig;
    if (!st.hotwords.rules.length) {
      hwList.innerHTML = `<div class="empty-tip">暂无热词规则，在上方添加后下次识别自动生效</div>`;
    } else {
      hwList.innerHTML = st.hotwords.rules.map(r => `
        <div class="hw-item">
          <div class="hw-pair">
            <span class="hw-src">${esc(r.src)}</span>
            <span class="hw-arrow">→</span>
            <span class="hw-dst">${esc(r.dst)}</span>
          </div>
          <button class="hw-del" title="删除" onclick="delHotword('${escAttr(r.src)}')">×</button>
        </div>`).join("");
    }
  }

  // System
  document.getElementById("chk-autostart").checked = st.system.autostart;
  document.getElementById("lbl-logdir").textContent = st.system.log_dir;
  document.getElementById("ver-badge").textContent = "v" + st.system.release;
}

function esc(s) {
  return String(s || "").replace(/[&<>"]/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[c]));
}
function escAttr(s) {
  return String(s || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

async function setOpt(key, value) {
  const res = await api("/api/option", { key, value });
  if (res.state) renderState(res.state);
  if (!res.ok && res.msg) showToast(res.msg);
  else if (key === "theme") {
    const t = res.state.appearance.themes.find(x => x.id === value);
    if (t) showToast(`已切换主题：${t.name}`);
  }
}

function onMicChange(val) {
  setOpt("mic_device", val ? JSON.parse(val) : null);
}

async function doAction(action, extra) {
  const res = await api("/api/action", Object.assign({ action }, extra || {}));
  if (res.state) renderState(res.state);
  if (res.msg) showToast(res.msg);
  else if (action === "preview_overlay") showToast("正在桌面演示当前主题悬浮条…");
  else if (action === "refresh_mics") showToast("已刷新麦克风列表");
}

async function addHotword() {
  const s = document.getElementById("hw-src");
  const d = document.getElementById("hw-dst");
  const res = await api("/api/action", { action: "hotword_add", src: s.value, dst: d.value });
  if (res.ok) {
    s.value = "";
    d.value = "";
    s.focus();
    if (res.state) renderState(res.state);
    showToast("已添加热词规则");
  } else if (res.msg) {
    showToast(res.msg);
  }
}

async function delHotword(src) {
  const res = await api("/api/action", { action: "hotword_delete", src });
  if (res.state) renderState(res.state);
  showToast("已删除热词规则");
}

const rangeOp = document.getElementById("range-opacity");
rangeOp.addEventListener("input", () => {
  document.getElementById("val-opacity").textContent = rangeOp.value + "%";
});
rangeOp.addEventListener("change", () => {
  setOpt("overlay_opacity", Number(rangeOp.value));
});

// Benchmark UI
let lastBenchState = "idle";
function renderBench(b) {
  if (!b) return;
  lastBenchState = b.state;
  const btn = document.getElementById("btn-bench-rec");
  btn.classList.toggle("recording", b.state === "recording");
  btn.disabled = !b.ready || b.state === "finishing";
  if (!b.ready) btn.textContent = "⏳ 正在准备引擎…";
  else if (b.state === "recording") btn.textContent = `● 录音中 ${b.rec_sec.toFixed(1)}s · 松开或点击结束`;
  else if (b.state === "finishing") btn.textContent = "⚡ 正在并发识别…";
  else btn.textContent = "🎙️ 按住说话 / 点击开始";

  document.getElementById("bench-grid").innerHTML = b.rows.map(r => {
    const badges = [];
    if (r.is_current) badges.push(`<span class="badge badge-cur">当前默认</span>`);
    if (r.badge === "fastest") badges.push(`<span class="badge badge-fast">⚡ 最快</span>`);
    if (r.badge === "slowest") badges.push(`<span class="badge badge-slow">最慢</span>`);
    return `
      <div class="bench-card ${r.badge === 'fastest' ? 'fastest' : ''}">
        <div class="bench-head">
          <div class="bench-name">${esc(r.label)} ${badges.join("")}</div>
          <span class="bench-status">${esc(r.status)}</span>
        </div>
        <div class="bench-text">${esc(r.text) || '<span style="color:var(--muted)">等待识别结果…</span>'}</div>
        <div style="display:flex;align-items:center;gap:10px">
          <div class="bench-meter" style="flex:1"><div class="bench-meter-fill" style="width:${r.pct}%"></div></div>
          ${r.is_current ? '' : `<button class="btn btn-sm" onclick="useEngine('${r.id}')">设为默认</button>`}
        </div>
      </div>`;
  }).join("");
}

async function useEngine(id) {
  await api("/api/test/leave", {});
  await setOpt("asr_engine", id);
  showToast("已切换默认识别引擎");
  if (currentTab === "benchmark") {
    const res = await api("/api/test/init", {});
    if (res && res.bench) renderBench(res.bench);
  }
}

async function benchDown() {
  if (lastBenchState === "idle") {
    benchPressTime = Date.now();
    benchMode = "hold";
    const r = await api("/api/test/start", {});
    if (r.bench) renderBench(r.bench);
    if (!r.ok && r.msg) showToast(r.msg);
  } else if (lastBenchState === "recording" && benchMode === "tap") {
    benchIgnoreClick = true;
    const r = await api("/api/test/stop", {});
    if (r.bench) renderBench(r.bench);
  }
}

async function benchUp() {
  if (lastBenchState !== "recording" || benchMode !== "hold") return;
  const dt = Date.now() - benchPressTime;
  if (dt < 320) {
    benchMode = "tap";
    benchIgnoreClick = true;
  } else {
    benchIgnoreClick = true;
    const r = await api("/api/test/stop", {});
    if (r.bench) renderBench(r.bench);
  }
}

function benchClick() {
  if (benchIgnoreClick) { benchIgnoreClick = false; return; }
}

window.addEventListener("mouseup", benchUp);
window.addEventListener("beforeunload", () => {
  if (currentTab === "benchmark") {
    navigator.sendBeacon(`/api/test/leave?token=${TOKEN}`, "{}");
  }
});

// Initial load & lightweight sync when visible (for hotkey capture / tray toggles)
async function pollState() {
  if (document.visibilityState !== "visible") return;
  try {
    const st = await api("/api/state");
    renderState(st);
  } catch (e) {}
}

pollState();
setInterval(pollState, 650);
</script>
</body>
</html>
"""
