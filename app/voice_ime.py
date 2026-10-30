#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按住右 Alt 说话，松开后文字直接输入到当前焦点窗口。"""

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import traceback
import wave
import winreg
from ctypes import wintypes


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
DLL_DIR = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "dll")
LOG_PATH = os.path.join(APP_DIR, "voice_ime.log")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
ICON_NAME = "icon.ico"
DEFAULT_HOTKEY = "RALT"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DoubaoVoice"
_log_lock = threading.Lock()


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        log("save config failed")


def icon_file():
    for base in (getattr(sys, "_MEIPASS", None), APP_DIR):
        if not base:
            continue
        path = os.path.join(base, ICON_NAME)
        if os.path.exists(path):
            return path
    return ""


def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
        return bool(value)
    except OSError:
        return False


def set_autostart(enabled):
    try:
        if enabled:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        log("set autostart failed")
        return False


def refresh_autostart():
    if not autostart_enabled():
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            current, _ = winreg.QueryValueEx(key, RUN_NAME)
    except OSError:
        return
    if current != autostart_command():
        set_autostart(True)
        log("autostart path refreshed")


def log(msg):
    line = time.strftime("%H:%M:%S ") + str(msg)
    try:
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


if sys.stdout:
    def say(msg):
        try:
            print(msg, flush=True)
        except Exception:
            pass
else:
    def say(msg):
        pass


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.ReadProcessMemory.restype = ctypes.c_int
kernel32.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_PROC = kernel32.GetCurrentProcess()


def safe_read(addr, n):
    if not addr:
        return b""
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(_PROC, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
        return buf.raw[:got.value]
    return b""


def rd_i32(p):
    return ctypes.c_int32.from_address(p).value


def rd_u64(p):
    return ctypes.c_uint64.from_address(p).value


# ---------------------------------------------------------------- ASR engine

PROTO = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

QUIC_URL = b"wss://frontier-audio-ime-quic.doubao.com/api/v1/ws"
WS_URL = b"wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"
PROTO_JSON = b'{"proto-version":"v2"}'
APPKEY = b"OrnqKvSSrs"
DEVICE_ID = "3330883888090203"
AID = 685343
APP_VERSION = "0.9.3"
TOKEN = json.dumps({"device_id": DEVICE_ID, "aid": str(AID)})
CTX = json.dumps({
    "screen_width": 1920, "screen_height": 1080,
    "device_platform": "windows", "device_brand": "PC", "device_model": "PC",
    "os_version": "Windows 10", "device_type": "release", "channel": "ImeService",
    "app_name": "oime_windows", "aid": str(AID), "os": "windows",
    "device_id": DEVICE_ID, "version_name": APP_VERSION,
    "app_version": APP_VERSION, "cellularProvider": "", "region": "cn",
}, separators=(",", ":")).encode()


class AsrEngine:
    def __init__(self):
        self.loaded = False
        self.ready = threading.Event()
        self.current = None
        self._keep = []
        self._cbs = [PROTO(self._make_cb(i)) for i in range(5)]
        self.cfg = None

    def _keep_bytes(self, b):
        buf = ctypes.create_string_buffer(b)
        self._keep.append(buf)
        return ctypes.addressof(buf)

    def load(self):
        os.add_dll_directory(DLL_DIR)
        self.dll = ctypes.CDLL(os.path.join(DLL_DIR, "audioeffect-mt.dll"))
        self.dll.SAMICoreInitContext.restype = ctypes.c_int
        self.dll.SAMICoreInitContext.argtypes = [ctypes.c_int, ctypes.c_void_p]
        self.dll.SAMICoreCreateHandleByIdentify.restype = ctypes.c_int
        self.dll.SAMICoreCreateHandleByIdentify.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                                            ctypes.c_int, ctypes.c_void_p]
        self.dll.SAMICoreProcess.restype = ctypes.c_int
        self.dll.SAMICoreProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.dll.SAMICoreSetProperty.restype = ctypes.c_int
        self.dll.SAMICoreSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.dll.SAMICoreDestroyHandle.restype = ctypes.c_int
        self.dll.SAMICoreDestroyHandle.argtypes = [ctypes.c_void_p]
        self.loaded = True

    def warmup(self):
        if not self.loaded:
            self.load()
        t0 = time.perf_counter()
        dll = self.dll

        ctx12 = ctypes.create_string_buffer(0x40)
        ctypes.c_uint64.from_buffer(ctx12, 0x00).value = self._keep_bytes(QUIC_URL)
        ctypes.c_uint64.from_buffer(ctx12, 0x08).value = self._keep_bytes(PROTO_JSON)
        ctypes.c_uint64.from_buffer(ctx12, 0x10).value = self._keep_bytes(CTX)
        ctypes.c_uint32.from_buffer(ctx12, 0x18).value = 10
        ctypes.c_uint32.from_buffer(ctx12, 0x1C).value = AID
        ctypes.c_uint64.from_buffer(ctx12, 0x20).value = self._keep_bytes(DEVICE_ID.encode())
        ctypes.c_uint64.from_buffer(ctx12, 0x28).value = self._keep_bytes(APP_VERSION.encode())
        ctypes.c_uint32.from_buffer(ctx12, 0x34).value = 5000
        ctypes.c_uint32.from_buffer(ctx12, 0x38).value = 3
        rc = dll.SAMICoreInitContext(12, ctx12)
        log(f"initctx12 rc={rc}")

        tmo = ctypes.c_uint32(3000)
        rc = dll.SAMICoreInitContext(14, ctypes.byref(tmo))
        log(f"initctx14 rc={rc}")

        pool7 = ctypes.create_string_buffer(0x10)
        ctypes.c_uint32.from_buffer(pool7, 0x00).value = 5
        ctypes.c_uint32.from_buffer(pool7, 0x04).value = 5000
        ctypes.c_uint32.from_buffer(pool7, 0x08).value = 500
        rc = dll.SAMICoreInitContext(7, pool7)
        log(f"initctx7 rc={rc}")

        pool = ctypes.create_string_buffer(0x50)
        ctypes.c_uint64.from_buffer(pool, 0x00).value = self._keep_bytes(b"oime_windows")
        ctypes.c_uint64.from_buffer(pool, 0x08).value = self._keep_bytes(QUIC_URL + b";")
        ctypes.c_uint64.from_buffer(pool, 0x10).value = self._keep_bytes(APPKEY)
        ctypes.c_uint64.from_buffer(pool, 0x18).value = self._keep_bytes(TOKEN.encode())
        ctypes.c_uint32.from_buffer(pool, 0x20).value = 5000
        ctypes.c_uint32.from_buffer(pool, 0x24).value = 0
        ctypes.c_uint64.from_buffer(pool, 0x28).value = self._keep_bytes(PROTO_JSON)
        ctypes.c_uint64.from_buffer(pool, 0x30).value = self._keep_bytes(CTX)
        ctypes.c_uint32.from_buffer(pool, 0x38).value = 1
        ctypes.c_uint32.from_buffer(pool, 0x3C).value = 3
        ctypes.c_uint32.from_buffer(pool, 0x40).value = 2
        ctypes.c_uint32.from_buffer(pool, 0x44).value = 5000
        rc = dll.SAMICoreInitContext(8, pool)
        log(f"initctx8 rc={rc}")

        time.sleep(3.5)
        self._build_cfg()
        self.ready.set()
        log(f"engine ready in {time.perf_counter() - t0:.2f}s")

    def _build_cfg(self):
        cfg = ctypes.create_string_buffer(0x100)
        cb_array = ctypes.create_string_buffer(0x40)
        for i in range(5):
            ctypes.c_uint64.from_buffer(cb_array, i * 8).value = ctypes.cast(
                self._cbs[i], ctypes.c_void_p).value

        def put64(off, val):
            ctypes.c_uint64.from_buffer(cfg, off).value = val

        def put32(off, val):
            ctypes.c_uint32.from_buffer(cfg, off).value = val

        put64(0x00, self._keep_bytes(WS_URL))
        put64(0x08, self._keep_bytes(APPKEY))
        put64(0x10, self._keep_bytes(PROTO_JSON))
        put64(0x18, self._keep_bytes(CTX))
        put64(0x28, self._keep_bytes(b""))
        put32(0x38, 1)
        put64(0x40, self._keep_bytes(TOKEN.encode()))
        put64(0x48, self._keep_bytes(b"speech_opus"))
        put32(0x50, 16000)
        put32(0x54, 1)
        put32(0x58, 5000)
        put32(0x5C, 1)
        put64(0x60, self._keep_bytes(b"zh"))
        put64(0x68, self._keep_bytes(b"{}"))
        put64(0xA0, ctypes.addressof(cb_array))
        put64(0xA8, self._keep_bytes(b"oime_windows"))
        ctypes.c_uint8.from_buffer(cfg, 0xB0).value = 1
        put32(0xB8, 1)
        put32(0xBC, 7)
        put32(0xCC, 9500)
        put32(0xD0, 6000)
        put32(0xD4, 8500)
        put32(0xD8, 40)
        put32(0xE0, 1)
        put64(0xE8, self._keep_bytes(b""))
        self.cfg = cfg
        self._cb_array = cb_array

    def _make_cb(self, slot):
        def cb(block, ud):
            sess = self.current
            if sess is None or sess.closed:
                return
            try:
                sess.on_block(slot, block)
            except Exception:
                log("cb error " + traceback.format_exc().replace("\n", " | "))
        return cb


class AsrSession:
    def __init__(self, engine, verbose=False, on_text=None):
        self.engine = engine
        self.verbose = verbose
        self.on_text = on_text
        self._last_live = 0.0
        self.h = None
        self.lock = threading.Lock()
        self.audio = bytearray()
        self.feed_stop = threading.Event()
        self.release_evt = threading.Event()
        self.final_evt = threading.Event()
        self.cancelled = False
        self.closed = False
        self.text = ""
        self.seq = 0
        self.feed_thread = None

    @property
    def dll(self):
        return self.engine.dll

    def push(self, data):
        if self.cancelled or self.closed:
            return
        with self.lock:
            self.audio += data
            cap = 16000 * 2 * 30
            if len(self.audio) > cap:
                del self.audio[:len(self.audio) - cap]

    def request_release(self):
        self.release_evt.set()

    def cancel(self):
        self.cancelled = True
        self.feed_stop.set()
        self.release_evt.set()

    def start(self):
        h = ctypes.c_void_p()
        t = time.perf_counter()
        rc = self.dll.SAMICoreCreateHandleByIdentify(ctypes.byref(h), 670, self.engine.cfg)
        log(f"create rc={rc} in {time.perf_counter() - t:.2f}s")
        if rc != 0 or not h.value:
            raise RuntimeError(f"create failed rc={rc}")
        self.h = h
        self.engine.current = self
        self.feed_thread = threading.Thread(target=self._feed, daemon=True)
        self.feed_thread.start()

    def _feed(self):
        class BLK(ctypes.Structure):
            _fields_ = [("type", ctypes.c_int32), ("size", ctypes.c_int32),
                        ("data", ctypes.c_void_p), ("a", ctypes.c_int64), ("b", ctypes.c_int64)]

        t0 = time.perf_counter()
        n = 0
        errors = 0
        while not self.feed_stop.is_set():
            with self.lock:
                if len(self.audio) >= 1280:
                    chunk = bytes(self.audio[:1280])
                    del self.audio[:1280]
                else:
                    chunk = b""
            if not chunk:
                time.sleep(0.004)
                continue
            buf = ctypes.create_string_buffer(chunk, len(chunk))
            blk = BLK(0, len(chunk), ctypes.addressof(buf), 0, 0)
            rc = self.dll.SAMICoreProcess(self.h, ctypes.byref(blk), None)
            n += 1
            if rc != 0:
                errors += 1
                log(f"process rc={rc} at chunk {n}")
                if errors >= 3:
                    break
            with self.lock:
                backlog = len(self.audio)
            if backlog < 4800:
                dt = t0 + n * 0.04 - time.perf_counter()
                if dt > 0:
                    time.sleep(dt)
        if not self.cancelled and self.h:
            with self.lock:
                tail = bytes(self.audio)
                self.audio.clear()
            if tail:
                buf = ctypes.create_string_buffer(tail, len(tail))
                blk = BLK(0, len(tail), ctypes.addressof(buf), 0, 0)
                self.dll.SAMICoreProcess(self.h, ctypes.byref(blk), None)
        log(f"fed {n} chunks")

    def on_block(self, slot, block):
        t = rd_i32(block)
        dp = rd_u64(block + 8)
        if t == 0x258 and dp:
            obj = safe_read(dp, 0x40)
            if len(obj) < 0x38:
                return
            jp = int.from_bytes(obj[0x30:0x38], "little")
            raw = safe_read(jp, 65536).split(b"\0")[0] if jp else b""
            if raw[:1] == b"{":
                self._on_json(raw)
        elif t == 0x259 and dp:
            obj = safe_read(dp, 0x18)
            if len(obj) >= 0x18 and int.from_bytes(obj[0:4], "little") == 4:
                p = int.from_bytes(obj[0x10:0x18], "little")
                s = safe_read(p, 128).split(b"\0")[0] if p else b""
                if s:
                    log(f"event {s[:60]!r}")

    def _on_json(self, raw):
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return
        results = obj.get("results") or []
        if not results or not isinstance(results[0], dict):
            return
        r0 = results[0]
        text = (r0.get("text") or "").strip()
        interim = r0.get("is_interim")
        if text:
            self.text = text
        if text and (self.verbose or interim is False):
            log(f"asr interim={interim} {text}")
        if text and self.on_text is not None:
            now = time.monotonic()
            if interim is False or now - self._last_live >= 0.08:
                self._last_live = now
                try:
                    self.on_text(text)
                except Exception:
                    pass
        if interim is False:
            self.final_evt.set()

    def finish(self):
        if self.cancelled or not self.h:
            return
        self.feed_stop.set()
        if self.feed_thread:
            with self.lock:
                remain = len(self.audio)
            self.feed_thread.join(remain / 32000.0 + 5.0)
        t = time.perf_counter()
        rc = self.dll.SAMICoreSetProperty(self.h, 0xA2A, None)
        log(f"finish rc={rc}")
        self.final_evt.wait(4.0)
        log(f"final after {time.perf_counter() - t:.2f}s text={self.text!r}")

    def close(self):
        self.closed = True
        self.feed_stop.set()
        if self.feed_thread is not None and self.feed_thread.is_alive():
            self.feed_thread.join(2.0)
        if self.h:
            self.dll.SAMICoreDestroyHandle(self.h)
            self.h = None
        if self.engine.current is self:
            self.engine.current = None


# ------------------------------------------------------------------ capture

class Mic:
    def __init__(self, device=None):
        self.device = device
        self.stream = None
        self.sink = None
        self.lock = threading.Lock()
        self.pre = bytearray()

    def open(self):
        import sounddevice as sd
        self.stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                        device=self.device, blocksize=0,
                                        callback=self._cb)
        self.stream.start()
        log(f"mic opened: {sd.query_devices(self.device)['name'] if self.device is not None else 'default'}")

    def _cb(self, indata, frames, t, status):
        try:
            b = bytes(indata)
        except Exception:
            return
        with self.lock:
            self.pre += b
            if len(self.pre) > 4800:
                del self.pre[:len(self.pre) - 4800]
            sink = self.sink
        if sink is not None:
            sink.push(b)

    def attach(self, sess):
        with self.lock:
            self.sink = sess
            pre = bytes(self.pre)
        sess.push(pre)

    def detach(self, sess):
        with self.lock:
            if self.sink is sess:
                self.sink = None

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


def wav_pusher(sess, path):
    try:
        w = wave.open(path, "rb")
        if (w.getnchannels(), w.getframerate(), w.getsampwidth()) != (1, 16000, 2):
            raise RuntimeError("wav 必须是 16k 单声道 16bit")
        data = w.readframes(w.getnframes())
        w.close()
    except Exception:
        log("wav error " + traceback.format_exc().replace("\n", " | "))
        sess.request_release()
        return
    t0 = time.perf_counter()
    n = 0
    for off in range(0, len(data), 1280):
        sess.push(data[off:off + 1280])
        n += 1
        dt = t0 + n * 0.04 - time.perf_counter()
        if dt > 0:
            time.sleep(dt)
    sess.request_release()


# ---------------------------------------------------------------- injection

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_void_p)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16),
                ("wParamH", ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


user32.SendInput.restype = ctypes.c_uint32
user32.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int]


def _send(events):
    arr = (INPUT * len(events))(*events)
    return int(user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)))


def _vk_event(vk, up=False, extended=False):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, flags, 0, None))


def _uni_event(unit, up=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, unit, flags, 0, None))


def type_text(text):
    units = [int.from_bytes(text.encode("utf-16-le")[i:i + 2], "little")
             for i in range(0, len(text.encode("utf-16-le")), 2)]
    expected = 2 * len(units)
    sent = 0
    batch = []
    for u in units:
        batch.append(_uni_event(u, False))
        batch.append(_uni_event(u, True))
        if len(batch) >= 64:
            sent += _send(batch)
            batch = []
            time.sleep(0.002)
    if batch:
        sent += _send(batch)
    return sent, expected


def _set_clipboard(text):
    user32.OpenClipboard(None)
    user32.EmptyClipboard()
    data = text.encode("utf-16-le") + b"\0\0"
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    h = kernel32.GlobalAlloc(0x0042, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    user32.SetClipboardData(13, ctypes.c_void_p(h))
    user32.CloseClipboard()


def paste_text(text):
    _set_clipboard(text)
    time.sleep(0.05)
    sent = _send([_vk_event(0x11), _vk_event(0x56), _vk_event(0x56, True), _vk_event(0x11, True)])
    return sent, 4


def _fg_info():
    hwnd = user32.GetForegroundWindow()
    cls = ctypes.create_unicode_buffer(128)
    title = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(ctypes.c_void_p(hwnd), cls, 128)
    user32.GetWindowTextW(ctypes.c_void_p(hwnd), title, 256)
    return f"{int(hwnd or 0):#x} {cls.value} {title.value!r}"


def inject_text(text, mode="unicode"):
    if not text:
        return 0, 0
    log("foreground " + _fg_info())
    if mode == "paste":
        return paste_text(text)
    return type_text(text)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p), ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", wintypes.RECT)]


user32.GetGUIThreadInfo.restype = ctypes.c_bool
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
user32.SendMessageTimeoutW.restype = ctypes.c_void_p
user32.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM,
                                       ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
SMTO_ABORTIFHUNG = 0x0002
WM_GETTEXT = 0x000D


def _control_text(hwnd):
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(4096)
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), WM_GETTEXT, 4096,
                                    ctypes.cast(buf, ctypes.c_void_p),
                                    SMTO_ABORTIFHUNG, 120, ctypes.byref(res))
    if not ok:
        return None
    return buf.value


def _thread_focus(tid):
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not tid or not user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return 0, None
    focus = int(info.hwndFocus or 0)
    caret = (info.rcCaret.left, info.rcCaret.top) if info.hwndCaret else None
    return focus, caret


def focus_snapshot():
    fg = int(user32.GetForegroundWindow() or 0)
    tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None) if fg else 0
    focus, caret = _thread_focus(tid)
    return {"fg": fg, "focus": focus, "caret": caret,
            "text": _control_text(focus) if focus else None}


def verify_injection(snap, typed):
    fg = int(user32.GetForegroundWindow() or 0)
    if fg != snap["fg"]:
        return "fail"
    tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
    focus, caret = _thread_focus(tid)
    if snap["focus"] and focus == snap["focus"]:
        now_text = _control_text(focus)
        if snap["text"] is not None and now_text is not None and now_text != snap["text"]:
            return "ok"
        if snap["caret"] is not None and caret is not None and caret != snap["caret"]:
            return "ok"
        if snap["text"] or snap["caret"] is not None:
            return "fail"
    return "unknown"


# ------------------------------------------------------------------- hotkey

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
VK_RMENU, VK_MENU, VK_ESCAPE = 0xA5, 0x12, 0x1B
LLKHF_EXTENDED = 0x01
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint32,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


MOD_GROUPS = {
    "CTRL": ("LCTRL", "RCTRL"),
    "ALT": ("LALT", "RALT"),
    "SHIFT": ("LSHIFT", "RSHIFT"),
}
_MOD_TOKENS = {"LCTRL", "RCTRL", "LALT", "RALT", "LSHIFT", "RSHIFT",
               "CTRL", "ALT", "SHIFT"}
_MOD_ORDER = ["CTRL", "LCTRL", "RCTRL", "ALT", "LALT", "RALT",
              "SHIFT", "LSHIFT", "RSHIFT"]


def canon_key(vk, extended):
    if vk == 0xA4:
        return "LALT"
    if vk == 0xA5:
        return "RALT"
    if vk == 0xA2:
        return "LCTRL"
    if vk == 0xA3:
        return "RCTRL"
    if vk == 0xA0:
        return "LSHIFT"
    if vk == 0xA1:
        return "RSHIFT"
    if vk == 0x11:
        return "RCTRL" if extended else "LCTRL"
    if vk == 0x12:
        return "RALT" if extended else "LALT"
    if vk == 0x10:
        return "RSHIFT" if extended else "LSHIFT"
    if vk == 0x1B:
        return "ESC"
    if vk == 0x0D:
        return "ENTER"
    if vk == 0x09:
        return "TAB"
    if vk == 0x08:
        return "BACKSPACE"
    if vk == 0x20:
        return "SPACE"
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    return "VK%02X" % vk


def _is_fkey(tok):
    return (len(tok) >= 2 and tok[0] == "F" and tok[1:].isdigit()
            and 1 <= int(tok[1:]) <= 12)


def parse_hotkey(spec):
    tokens = []
    for part in str(spec or "").upper().split("+"):
        part = part.strip()
        if part == "CONTROL":
            part = "CTRL"
        if part and part not in tokens:
            tokens.append(part)
    if not tokens or "ESC" in tokens:
        return ["RALT"]
    base = [t for t in tokens if t not in _MOD_TOKENS]
    has_mod = any(t in _MOD_TOKENS for t in tokens)
    if base and not has_mod and not all(_is_fkey(t) for t in base):
        return ["RALT"]
    return tokens


def hotkey_display(tokens):
    names = {"CTRL": "Ctrl", "LCTRL": "左 Ctrl", "RCTRL": "右 Ctrl",
             "ALT": "Alt", "LALT": "左 Alt", "RALT": "右 Alt",
             "SHIFT": "Shift", "LSHIFT": "左 Shift", "RSHIFT": "右 Shift",
             "SPACE": "Space", "ENTER": "Enter", "TAB": "Tab",
             "BACKSPACE": "Backspace"}
    return "+".join(names.get(t, t) for t in tokens)


class Hotkey:
    def __init__(self, app, spec=DEFAULT_HOTKEY):
        self.app = app
        self.set_combo(spec)
        self.hook = None
        self.proc = HOOKPROC(self._cb)
        self.thread = None
        self.down = set()
        self.swallowed = set()
        self.cap_mods = set()
        self.cap_seen_mods = set()
        self.active = False

    def display(self):
        return hotkey_display(self.combo)

    def release_hint(self):
        if self.single_mod:
            return f"松开{self.display()}结束"
        return "松开按键结束"

    def set_combo(self, spec):
        self.combo = parse_hotkey(spec)
        self.single_mod = len(self.combo) == 1 and self.combo[0] in _MOD_TOKENS
        self.active = False
        self.swallowed = set()

    def begin_capture(self):
        self.down.clear()
        self.cap_mods.clear()
        self.cap_seen_mods.clear()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.c_uint32]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                                       ctypes.c_uint32, ctypes.c_uint32]
        self.hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.proc,
                                             kernel32.GetModuleHandleW(None), 0)
        log(f"keyboard hook={self.hook} hotkey={self.display()}")
        if not self.hook:
            log(f"hook failed err={ctypes.get_last_error()}")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _token_down(self, tok):
        if tok in MOD_GROUPS:
            return any(t in self.down for t in MOD_GROUPS[tok])
        return tok in self.down

    def _token_matches(self, combo_tok, tok):
        if combo_tok in MOD_GROUPS:
            return tok in MOD_GROUPS[combo_tok]
        return combo_tok == tok

    def _satisfied(self):
        return all(self._token_down(t) for t in self.combo)

    def _base_tokens(self):
        return [t for t in self.combo if t not in _MOD_TOKENS]

    def _swallow_down(self, tok):
        if self.single_mod:
            return self._token_matches(self.combo[0], tok)
        base = self._base_tokens()
        if base and any(self._token_matches(b, tok) for b in base):
            mods = [t for t in self.combo if t not in base]
            return all(self._token_down(t) for t in mods)
        return False

    def _cb(self, nCode, wParam, lParam):
        if nCode == 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.vkCode in (0, 0xE7):
                return user32.CallNextHookEx(self.hook, nCode, wParam, lParam)
            tok = canon_key(kb.vkCode, kb.flags & LLKHF_EXTENDED)
            is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)
            if is_down or is_up:
                if self.app.capturing:
                    if self._capture(tok, is_down):
                        return 1
                elif is_down:
                    self.down.add(tok)
                    if tok == "ESC" and self.app.on_cancel():
                        return 1
                    if not self.active and self._satisfied():
                        self.active = True
                        self.app.on_key_down()
                        if self._swallow_down(tok):
                            self.swallowed.add(tok)
                            return 1
                    elif self._swallow_down(tok):
                        self.swallowed.add(tok)
                        return 1
                else:
                    self.down.discard(tok)
                    if self.active and any(self._token_matches(c, tok) for c in self.combo):
                        self.active = False
                        self.app.on_key_up()
                    if tok in self.swallowed:
                        self.swallowed.discard(tok)
                        return 1
        return user32.CallNextHookEx(self.hook, nCode, wParam, lParam)

    def _capture(self, tok, is_down):
        if is_down:
            if tok == "ESC":
                self.app.capture_cancel()
                self.swallowed.add(tok)
                return True
            if tok in _MOD_TOKENS:
                self.cap_mods.add(tok)
                self.cap_seen_mods.add(tok)
                self.swallowed.add(tok)
                return True
            if not (self.cap_mods or _is_fkey(tok)):
                self.app.capture_invalid()
                self.swallowed.add(tok)
                return True
            ordered = [t for t in _MOD_ORDER if t in self.cap_mods] + [tok]
            self.cap_mods.clear()
            self.cap_seen_mods.clear()
            self.app.capture_done("+".join(ordered))
            self.swallowed.add(tok)
            return True
        was_swallowed = tok in self.swallowed
        self.swallowed.discard(tok)
        if tok in _MOD_TOKENS:
            self.cap_mods.discard(tok)
            if self.cap_seen_mods == {tok} and not self.cap_mods:
                self.cap_seen_mods.clear()
                self.app.capture_done(tok)
            return True
        return was_swallowed


# -------------------------------------------------------------------- overlay

class UiState:
    def __init__(self):
        self.lock = threading.Lock()
        self.kind = "none"
        self.msg = ""
        self.live = ""
        self.gen = 0

    def set(self, kind, msg="", live=None):
        with self.lock:
            self.kind = kind
            self.msg = msg
            if live is not None:
                self.live = live
            elif kind in ("none", "done"):
                self.live = ""
            self.gen += 1
            return self.gen

    def get(self):
        with self.lock:
            return self.kind, self.msg, self.live, self.gen


def _init_gdi_apis():
    p, i, u = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
    for dll_lib, sig in (
        (user32, {
            "GetForegroundWindow": ([], p),
            "GetDesktopWindow": ([], p),
            "MonitorFromWindow": ([p, u], p),
            "GetDC": ([p], p),
            "ReleaseDC": ([p, p], i),
            "FillRect": ([p, ctypes.c_void_p, p], i),
            "FrameRect": ([p, ctypes.c_void_p, p], i),
            "ShowWindow": ([p, i], ctypes.c_bool),
            "SetWindowPos": ([p, p, i, i, i, i, u], ctypes.c_bool),
            "InvalidateRect": ([p, ctypes.c_void_p, ctypes.c_bool], ctypes.c_bool),
            "BeginPaint": ([p, ctypes.c_void_p], p),
            "EndPaint": ([p, ctypes.c_void_p], ctypes.c_bool),
            "GetClientRect": ([p, ctypes.c_void_p], ctypes.c_bool),
            "DefWindowProcW": ([p, u, ctypes.c_size_t, ctypes.c_ssize_t], LRESULT),
            "SetTimer": ([p, ctypes.c_size_t, u, p], ctypes.c_size_t),
            "GetMonitorInfoW": ([p, p], ctypes.c_bool),
            "DrawTextW": ([p, wintypes.LPCWSTR, i, ctypes.c_void_p, u], i),
        }),
        (gdi32, {
            "SelectObject": ([p, p], p),
            "GetTextExtentPoint32W": ([p, wintypes.LPCWSTR, i, ctypes.POINTER(wintypes.SIZE)],
                                      ctypes.c_bool),
            "SetBkMode": ([p, i], i),
            "SetTextColor": ([p, u], u),
            "CreateSolidBrush": ([u], p),
            "CreateFontW": ([i, i, i, i, i, u, u, u, u, u, u, u, u, wintypes.LPCWSTR], p),
        }),
    ):
        for name, (args, ret) in sig.items():
            fn = getattr(dll_lib, name)
            fn.argtypes = args
            fn.restype = ret


_init_gdi_apis()


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", ctypes.c_void_p), ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class Overlay:
    WM_PAINT = 0x000F
    WM_ERASEBKGND = 0x0014
    WM_TIMER = 0x0113
    WM_PRINTCLIENT = 0x0318
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
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
    DT_CENTER = 0x0001
    DT_VCENTER = 0x0004
    DT_SINGLELINE = 0x0020
    DT_WORDBREAK = 0x0010
    DT_CALCRECT = 0x0400
    DT_NOPREFIX = 0x0800
    PAD = 14
    STATUS_H = 22
    GAP = 2
    LINE_H = 28
    MAX_TEXT_W = 600

    def __init__(self, state):
        self.state = state
        self.seen_gen = -1
        self.text = ""
        self.kind = "none"
        self.live = ""
        self.disp_live = ""
        self._log_t = 0.0
        self._last_kind = None
        self.hwnd = None
        self.thread = None
        self.w = 260
        self.h = 46
        self.scale = 1.0
        self.pad = self.PAD
        self.status_h = self.STATUS_H
        self.gap = self.GAP
        self.line_h = self.LINE_H
        self.max_w = self.MAX_TEXT_W
        self._work = None
        self._brush = None
        self._frame_brush = None
        self._font = None
        self._font_small = None
        self._proc = WNDPROC(self._wndproc)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        self._create()
        user32.SetTimer(self.hwnd, 1, 50, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._brush = gdi32.CreateSolidBrush(0x001F1B1B)
        self._frame_brush = gdi32.CreateSolidBrush(0x00443C3C)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("style", ctypes.c_uint32),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                        ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", ctypes.c_void_p)]

        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.style = 0x0002 | 0x0001
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.hbrBackground = self._brush
        cls.lpszClassName = "DoubaoVoiceOverlay"
        user32.RegisterClassExW.restype = ctypes.c_ushort
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW(ctypes.byref(cls))

        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        ex = (self.WS_EX_TOPMOST | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE
              | self.WS_EX_TRANSPARENT | self.WS_EX_LAYERED)
        self.hwnd = user32.CreateWindowExW(ex, "DoubaoVoiceOverlay", "DoubaoVoiceOverlay",
                                           0x80000000, 0, 0, self.w, self.h,
                                           None, None, hinst, None)
        user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                      ctypes.c_ubyte, ctypes.c_uint32]
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 235, 0x00000002)
        user32.GetDpiForWindow.restype = ctypes.c_uint32
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        dpi = user32.GetDpiForWindow(self.hwnd)
        s = self.scale = max(1.0, dpi / 96.0)
        self.pad = int(self.PAD * s)
        self.status_h = int(self.STATUS_H * s)
        self.gap = int(self.GAP * s)
        self.line_h = int(self.LINE_H * s)
        self.max_w = int(self.MAX_TEXT_W * s)
        self._font = gdi32.CreateFontW(-int(21 * s), 0, 0, 0, 600, 0, 0, 0, 0, 0, 0, 5, 0,
                                       "Microsoft YaHei UI")
        self._font_small = gdi32.CreateFontW(-int(16 * s), 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 5, 0,
                                             "Microsoft YaHei UI")
        log(f"overlay hwnd={self.hwnd} dpi={dpi} scale={s:.2f}")

    def _anchor(self):
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_uint32)]

        user32.MonitorFromWindow.restype = ctypes.c_void_p
        if self._work is None:
            hwnd = user32.GetForegroundWindow() or user32.GetDesktopWindow()
            mon = user32.MonitorFromWindow(hwnd, 2)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            user32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi))
            self._work = mi.rcWork
        r = self._work
        return (r.left + r.right) // 2 - self.w // 2, r.bottom - self.h - int(100 * self.scale)

    def _apply(self):
        kind, msg, live, gen = self.state.get()
        if gen == self.seen_gen:
            return
        self.seen_gen = gen
        if kind == "none":
            self._work = None
            user32.ShowWindow(self.hwnd, self.SW_HIDE)
            log("overlay hide")
            return
        self.kind = kind
        self.text = msg
        self.live = live
        self.disp_live = ""
        two = kind in ("rec", "wait") and bool(live.strip())
        hdc = user32.GetDC(self.hwnd)
        if two:
            old = gdi32.SelectObject(hdc, ctypes.c_void_p(self._font_small))
            sz = wintypes.SIZE()
            gdi32.GetTextExtentPoint32W(hdc, msg, len(msg), ctypes.byref(sz))
            status_w = sz.cx
            gdi32.SelectObject(hdc, ctypes.c_void_p(self._font))
            disp = live
            tw, th = 0, self.line_h
            for _ in range(6):
                tw, th = self._measure(hdc, disp)
                if th <= self.line_h * 2 or len(disp) <= 10:
                    break
                disp = "…" + disp[max(1, len(disp) // 4):]
            self.disp_live = disp
            gdi32.SelectObject(hdc, old)
            self.w = max(int(240 * self.scale), max(status_w, tw) + 2 * self.pad)
            self.h = self.pad + self.status_h + self.gap + th + self.pad
        else:
            old = gdi32.SelectObject(hdc, ctypes.c_void_p(self._font))
            size = wintypes.SIZE()
            gdi32.GetTextExtentPoint32W(hdc, msg, len(msg), ctypes.byref(size))
            gdi32.SelectObject(hdc, old)
            self.w = max(int(160 * self.scale), size.cx + int(48 * self.scale))
            self.h = int(46 * self.scale)
        user32.ReleaseDC(self.hwnd, hdc)
        x, y = self._anchor()
        ok = user32.SetWindowPos(self.hwnd, self.HWND_TOPMOST, x, y, self.w, self.h,
                                 self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW)
        user32.InvalidateRect(self.hwnd, None, True)
        now = time.monotonic()
        if kind != self._last_kind or now - self._log_t >= 1.0:
            self._log_t = now
            log(f"overlay show kind={kind} gen={gen} pos={x},{y} {self.w}x{self.h} ok={ok} err={ctypes.get_last_error()}")
        self._last_kind = kind

    def _measure(self, hdc, text):
        sz = wintypes.SIZE()
        gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
        w = min(sz.cx, self.max_w)
        rc = wintypes.RECT(0, 0, max(w, int(24 * self.scale)), 0)
        user32.DrawTextW(hdc, text, -1, ctypes.byref(rc),
                         self.DT_CALCRECT | self.DT_WORDBREAK | self.DT_NOPREFIX)
        return w, rc.bottom

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            return self._wndproc_inner(hwnd, msg, wp, lp)
        except Exception:
            log("wndproc error " + traceback.format_exc().replace("\n", " | "))
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _wndproc_inner(self, hwnd, msg, wp, lp):
        if msg == self.WM_TIMER:
            self._apply()
            return 0
        if msg == self.WM_ERASEBKGND:
            return 1
        if msg == self.WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            self._paint(hdc)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if msg == self.WM_PRINTCLIENT:
            self._paint(ctypes.c_void_p(wp))
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _paint(self, hdc):
        rc = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rc))
        user32.FillRect(hdc, ctypes.byref(rc), ctypes.c_void_p(self._brush))
        inner = wintypes.RECT(0, 0, rc.right, rc.bottom)
        user32.FrameRect(hdc, ctypes.byref(inner), ctypes.c_void_p(self._frame_brush))
        gdi32.SetBkMode(hdc, 1)
        if self.disp_live:
            old = gdi32.SelectObject(hdc, ctypes.c_void_p(self._font_small))
            gdi32.SetTextColor(hdc, 0x009E988F)
            sr = wintypes.RECT(self.pad, self.pad, rc.right - self.pad,
                               self.pad + self.status_h)
            user32.DrawTextW(hdc, self.text, -1, ctypes.byref(sr),
                             self.DT_CENTER | self.DT_VCENTER | self.DT_SINGLELINE
                             | self.DT_NOPREFIX)
            gdi32.SelectObject(hdc, ctypes.c_void_p(self._font))
            gdi32.SetTextColor(hdc, 0x00F2F0F0)
            tr = wintypes.RECT(self.pad, self.pad + self.status_h + self.gap,
                               rc.right - self.pad, rc.bottom - self.pad)
            user32.DrawTextW(hdc, self.disp_live, -1, ctypes.byref(tr),
                             self.DT_CENTER | self.DT_WORDBREAK | self.DT_NOPREFIX)
        else:
            old = gdi32.SelectObject(hdc, ctypes.c_void_p(self._font))
            gdi32.SetTextColor(hdc, 0x00A0A0F5 if self.kind == "fail" else 0x00F2F0F0)
            user32.DrawTextW(hdc, self.text, -1, ctypes.byref(inner),
                             self.DT_CENTER | self.DT_VCENTER | self.DT_SINGLELINE
                             | self.DT_NOPREFIX)
        gdi32.SelectObject(hdc, old)


# ---------------------------------------------------------------------- tray

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_RBUTTONUP, WM_LBUTTONUP, WM_CONTEXTMENU = 0x0205, 0x0202, 0x007B
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x00000000, 0x00000001, 0x00000002
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO = 0x1
IDI_APPLICATION = 32512
MF_STRING, MF_GRAYED, MF_SEPARATOR, MF_CHECKED = 0x0000, 0x0001, 0x0800, 0x0008
TPM_RIGHTBUTTON, TPM_RETURNCMD = 0x0002, 0x0100
LR_LOADFROMFILE, IMAGE_ICON = 0x0010, 1
SM_CXSMICON, SM_CYSMICON = 49, 50


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", ctypes.c_void_p),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", ctypes.c_void_p),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", ctypes.c_void_p)]


class Tray:
    ID_SET_HOTKEY = 1001
    ID_OPEN_LOG = 1002
    ID_QUIT = 1003
    ID_AUTOSTART = 1004

    def __init__(self, app):
        self.app = app
        self.hwnd = None
        self.thread = None
        self._nid = None
        self._proc = WNDPROC(self._wndproc)

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("style", ctypes.c_uint32),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                        ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", ctypes.c_void_p)]

        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.lpszClassName = "DoubaoVoiceTray"
        user32.RegisterClassExW.restype = ctypes.c_ushort
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW(ctypes.byref(cls))
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.hwnd = user32.CreateWindowExW(0, "DoubaoVoiceTray", "DoubaoVoiceTray", 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        log(f"tray hwnd={self.hwnd}")
        if not self.hwnd:
            return
        user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.LoadIconW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                              ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = ctypes.c_bool
        icon_path = icon_file()
        hicon = None
        if icon_path:
            cx = user32.GetSystemMetrics(SM_CXSMICON) or 16
            cy = user32.GetSystemMetrics(SM_CYSMICON) or 16
            hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if not hicon:
            hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        log(f"tray icon image={icon_path or 'default'} hicon={bool(hicon)}")
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = f"豆包语音输入（{self.app.hotkey.display()}）"
        self._nid = nid
        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        log(f"tray icon={ok} err={ctypes.get_last_error()}")
        if ok:
            nid.uFlags = NIF_INFO
            nid.dwInfoFlags = NIIF_INFO
            nid.szInfoTitle = "豆包语音输入"
            nid.szInfo = (f"已在后台运行：按住 {self.app.hotkey.display()} 说话。\n"
                          "右击托盘图标可设置快捷键或退出。")
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def set_tip(self, text):
        nid = self._nid
        if nid is None:
            return
        nid.szTip = text
        nid.uFlags = NIF_TIP
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def remove_icon(self):
        if self._nid is None:
            return
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        self._nid = None

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            return self._inner(hwnd, msg, wp, lp)
        except Exception:
            log("tray error " + traceback.format_exc().replace("\n", " | "))
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _inner(self, hwnd, msg, wp, lp):
        if msg == WM_TRAY:
            if (lp & 0xFFFF) in (WM_RBUTTONUP, WM_LBUTTONUP, WM_CONTEXTMENU):
                self._menu()
            return 0
        if msg == WM_DESTROY:
            self.remove_icon()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _menu(self):
        user32.CreatePopupMenu.restype = ctypes.c_void_p
        user32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                       ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = ctypes.c_bool
        user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_void_p]
        user32.TrackPopupMenu.restype = ctypes.c_int
        user32.DestroyMenu.argtypes = [ctypes.c_void_p]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_GRAYED | MF_STRING, 1,
                           f"按住 {self.app.hotkey.display()} 说话")
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_SET_HOTKEY, "设置快捷键…")
        auto = MF_STRING | (MF_CHECKED if autostart_enabled() else 0)
        user32.AppendMenuW(hmenu, auto, self.ID_AUTOSTART, "开机自启")
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_OPEN_LOG, "查看日志")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_QUIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        if cmd == self.ID_SET_HOTKEY:
            self.app.start_capture()
        elif cmd == self.ID_AUTOSTART:
            self.app.toggle_autostart()
        elif cmd == self.ID_OPEN_LOG:
            try:
                os.startfile(LOG_PATH)
            except OSError:
                pass
        elif cmd == self.ID_QUIT:
            self.app.quit()


# ---------------------------------------------------------------------- app

class App:
    def __init__(self, args):
        self.args = args
        self.engine = AsrEngine()
        self.mic = Mic(args.device)
        self.ui = UiState()
        self.lock = threading.Lock()
        self.session = None
        self.once_done = threading.Event()
        self.accept_live = False
        self.capturing = False
        self.tray = None
        cfg = load_config()
        self.hotkey = Hotkey(self, cfg.get("hotkey") or DEFAULT_HOTKEY)

    def start_capture(self):
        if self.session is not None:
            self.set_ui("done", "正在录音，请结束后再设置快捷键", auto_hide=1800)
            return
        self.capturing = True
        self.hotkey.begin_capture()
        self.set_ui("capture", "按下新的快捷键…  Esc 取消")
        log("capture start")

    def capture_invalid(self):
        if not self.capturing:
            return
        self.set_ui("capture", "请包含 Ctrl/Alt/Shift，或使用 F1–F12。Esc 取消")

    def capture_done(self, spec):
        if not self.capturing:
            return
        self.capturing = False
        self.hotkey.set_combo(spec)
        cfg = load_config()
        cfg["hotkey"] = spec
        save_config(cfg)
        log(f"hotkey set {spec}")
        if self.tray is not None:
            self.tray.set_tip(f"豆包语音输入（{self.hotkey.display()}）")
        self.set_ui("done", f"已设置快捷键：{self.hotkey.display()}", auto_hide=2000)

    def capture_cancel(self):
        if not self.capturing:
            return
        self.capturing = False
        log("capture cancel")
        self.set_ui("done", "已取消设置快捷键", auto_hide=1500)

    def toggle_autostart(self):
        want = not autostart_enabled()
        if not set_autostart(want):
            self.set_ui("fail", "设置开机自启失败（权限不足）", auto_hide=2600)
            return
        log(f"autostart={'on' if want else 'off'}")
        self.set_ui("done", "已开启开机自启" if want else "已关闭开机自启", auto_hide=1800)

    def quit(self):
        log("quit")
        sess = self.session
        if sess is not None:
            try:
                sess.cancel()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.remove_icon()
            except Exception:
                pass
        time.sleep(0.2)
        os._exit(0)

    def set_ui(self, kind, msg="", auto_hide=None, live=None):
        gen = self.ui.set(kind, msg, live)
        if auto_hide:
            def later():
                with self.ui.lock:
                    if self.ui.gen == gen:
                        self.ui.kind = "none"
                        self.ui.msg = ""
                        self.ui.live = ""
                        self.ui.gen += 1
            timer = threading.Timer(auto_hide / 1000.0, later)
            timer.daemon = True
            timer.start()

    def update_live(self, text):
        if not self.accept_live:
            return
        with self.ui.lock:
            kind, msg = self.ui.kind, self.ui.msg
        if kind in ("rec", "wait"):
            self.set_ui(kind, msg, live=text)

    def warmup_async(self):
        def work():
            try:
                self.engine.warmup()
            except Exception:
                log("warmup error " + traceback.format_exc().replace("\n", " | "))
                self.set_ui("done", "语音引擎启动失败", auto_hide=4000)
        threading.Thread(target=work, daemon=True).start()

    def start_session(self):
        with self.lock:
            if self.session is not None:
                return None
            sess = AsrSession(self.engine, self.args.verbose, on_text=self.update_live)
            self.session = sess
        threading.Thread(target=self._run_session, args=(sess,), daemon=True).start()
        return sess

    def _run_session(self, sess):
        self.accept_live = True
        try:
            self.set_ui("rec", f"● 聆听中…  {self.hotkey.release_hint()}", live="")
            if not self.args.wav:
                self.mic.attach(sess)
            if not self.engine.ready.wait(20):
                log("engine not ready")
                self.set_ui("done", "语音引擎未就绪", auto_hide=2500)
                return
            sess.start()
            if self.args.wav:
                threading.Thread(target=wav_pusher, args=(sess, self.args.wav), daemon=True).start()
            sess.release_evt.wait(120)
            if sess.cancelled:
                self.set_ui("none")
                return
            if not self.args.wav:
                self.mic.detach(sess)
            self.set_ui("wait", "识别中…")
            sess.finish()
            self.accept_live = False
            text = sess.text.strip()
            if text:
                if self.args.no_inject:
                    log(f"no-inject, text={text!r}")
                    self.set_ui("done", f"✓ {text}", auto_hide=2000)
                else:
                    self.inject_and_report(text)
            else:
                self.set_ui("done", "未识别到内容", auto_hide=2500)
        except Exception:
            log("session error " + traceback.format_exc().replace("\n", " | "))
            self.set_ui("done", "识别出错，详见日志", auto_hide=3000)
        finally:
            self.accept_live = False
            try:
                sess.close()
            except Exception:
                pass
            self.mic.detach(sess)
            with self.lock:
                self.session = None
            with self.ui.lock:
                kind = self.ui.kind
            if kind != "done":
                self.set_ui("none")
            self.once_done.set()

    def inject_and_report(self, text):
        snap = focus_snapshot()
        log(f"inject target fg={snap['fg']:#x} focus={snap['focus']:#x} "
            f"caret={snap['caret']} text={'yes' if snap['text'] is not None else 'no'}")
        if not snap["fg"]:
            _set_clipboard(text)
            log("inject fail: no foreground window")
            self.set_ui("fail", "未找到输入窗口，已复制到剪贴板", auto_hide=3200)
            return
        log(f"injecting {len(text)} chars")
        sent, expected = inject_text(text, self.args.inject)
        time.sleep(0.35)
        res = verify_injection(snap, text)
        log(f"inject verify={res} sent={sent}/{expected}")
        if res == "ok" and (not expected or sent == expected):
            self.set_ui("done", f"✓ 已输入 {len(text)} 字", auto_hide=1500)
        elif res == "fail" or (expected and sent != expected):
            if self.args.inject != "paste":
                _set_clipboard(text)
            self.set_ui("fail", "未能输入，文字已复制到剪贴板", auto_hide=3200)
        else:
            _set_clipboard(text)
            self.set_ui("done", f"已输入，无法确认（已存剪贴板）", auto_hide=2600)

    def on_key_down(self):
        self.start_session()

    def on_key_up(self):
        with self.lock:
            sess = self.session
        if sess:
            sess.request_release()

    def on_cancel(self):
        with self.lock:
            sess = self.session
        if sess:
            sess.cancel()
            return True
        return False

    def run_once(self):
        self.start_session()
        self.once_done.wait(60)


def set_dpi_awareness():
    try:
        user32.GetDpiForSystem.restype = ctypes.c_uint32
        ok = user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        log(f"dpi ctx={ok} system_dpi={user32.GetDpiForSystem()}")
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


_MUTEX_HANDLE = None
_INSTANCE_INFO = {"mutex": False, "overlay_pid": 0}


def acquire_single_instance():
    global _MUTEX_HANDLE
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, wintypes.LPCWSTR]
    h = kernel32.CreateMutexW(None, False, "Local\\DoubaoVoiceIME")
    if not h:
        return True
    _MUTEX_HANDLE = h
    if ctypes.get_last_error() == 183:
        _INSTANCE_INFO["mutex"] = True
        return False
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    hwnd = user32.FindWindowW("DoubaoVoiceOverlay", None)
    if hwnd:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        if pid.value and pid.value != os.getpid():
            log(f"another instance overlay hwnd={int(hwnd):#x} pid={pid.value}")
            _INSTANCE_INFO["overlay_pid"] = pid.value
            return False
    return True


def enum_overlay_pids():
    pids = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]

    def cb(hwnd, lp):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value == "DoubaoVoiceOverlay":
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.append(pid.value)
        return True

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, 0)
    return sorted(set(pids))


def process_image_name(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, ctypes.c_bool, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                    wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


def kill_process(pid):
    PROCESS_TERMINATE, SYNCHRONIZE = 0x0001, 0x00100000
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, ctypes.c_bool, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    h = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        ok = bool(kernel32.TerminateProcess(h, 1))
        kernel32.WaitForSingleObject(h, 3000)
        return ok
    finally:
        kernel32.CloseHandle(h)


def main():
    ap = argparse.ArgumentParser(description="按住快捷键说话，松开后自动输入文字")
    ap.add_argument("--device", type=int, default=None, help="麦克风设备编号")
    ap.add_argument("--wav", help="用 wav 文件代替麦克风（调试用）")
    ap.add_argument("--inject", choices=["unicode", "paste"], default="unicode",
                    help="文字注入方式：unicode 逐字输入 / paste 剪贴板粘贴")
    ap.add_argument("--no-inject", action="store_true", help="只识别不输入")
    ap.add_argument("--no-ui", action="store_true", help="不显示悬浮提示")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    ap.add_argument("--verbose", action="store_true", help="日志记录每个中间结果")
    ap.add_argument("--once", action="store_true", help="启动后立即识别一次并退出（需 --wav）")
    args = ap.parse_args()

    if args.once and not args.wav:
        ap.error("--once 需要配合 --wav")

    set_dpi_awareness()
    log("=== start " + " ".join(sys.argv[1:]))

    if not args.wav and not acquire_single_instance():
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       wintypes.LPCWSTR, wintypes.UINT]
        log(f"another instance running info={_INSTANCE_INFO}")
        if _INSTANCE_INFO["mutex"]:
            user32.MessageBoxW(None,
                               "豆包语音输入已经在运行（见任务栏托盘图标）。\n"
                               "如需重新启动，请先在托盘菜单中选择“退出”。",
                               "豆包语音输入", 0x40 | 0x10000)
            return
        pids = [p for p in enum_overlay_pids()
                if p != os.getpid() and "doubaovoice" in process_image_name(p)]
        if pids:
            ans = user32.MessageBoxW(None,
                                     f"检测到 {len(pids)} 个旧版本的豆包语音输入仍在后台运行"
                                     "（旧版没有托盘图标）。\n\n"
                                     "是否结束旧实例并启动当前版本？",
                                     "豆包语音输入", 0x04 | 0x40 | 0x10000)
            if ans != 6:
                return
            for pid in pids:
                log(f"kill old instance pid={pid} ok={kill_process(pid)}")
            for _ in range(20):
                if not enum_overlay_pids():
                    break
                time.sleep(0.1)
        else:
            return

    app = App(args)
    log(f"hotkey={app.hotkey.display()} config={CONFIG_PATH} autostart={autostart_enabled()}")
    refresh_autostart()
    app.warmup_async()

    if not args.wav:
        try:
            app.mic.open()
        except Exception:
            log("mic open error " + traceback.format_exc().replace("\n", " | "))
            app.set_ui("done", "麦克风打开失败", auto_hide=4000)

    if not args.no_ui:
        Overlay(app.ui).start()

    if args.once:
        app.run_once()
        app.mic.close()
        return

    app.hotkey.start()
    if not args.no_tray:
        tray = Tray(app)
        app.tray = tray
        tray.start()
    hint = app.hotkey.display()
    if not args.no_ui:
        app.set_ui("info", f"语音输入已就绪：按住 {hint} 说话，Esc 取消", auto_hide=3000)
    say(f"已启动：按住 {hint} 说话，松开输入。Esc 取消。托盘图标可退出。日志: " + LOG_PATH)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.mic.close()


if __name__ == "__main__":
    main()
