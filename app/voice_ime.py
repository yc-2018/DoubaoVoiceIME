#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按住右 Alt 说话，松开后文字直接输入到当前焦点窗口。"""

import argparse
import ctypes
import json
import os
import re
import struct
import sys
import threading
import time
import traceback
import wave
import winreg
from ctypes import wintypes

import api_common
import api_smart
import api_baidu
import api_doubao
import api_ifly
import api_qwen
import api_wetype


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
LOG_DIR = os.path.join(APP_DIR, "logs")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HOTWORDS_PATH = os.path.join(APP_DIR, "hotwords.txt")
HOTWORDS_HEADER = ("# 热词替换：每行一条，格式：原文 -> 替换为\n"
                   "# 以 # 开头的行会被忽略，修改后下次识别自动生效\n")
ICON_NAME = "icon.ico"
FLOAT_ICON_NAME = "float.ico"
APP_RELEASE = "2.2.0"
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


def ensure_hotwords_file():
    if os.path.exists(HOTWORDS_PATH):
        return
    try:
        with open(HOTWORDS_PATH, "w", encoding="utf-8") as f:
            f.write(HOTWORDS_HEADER)
    except OSError:
        log("create hotwords failed")


def load_hotwords():
    try:
        with open(HOTWORDS_PATH, "r", encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeError):
        return []
    rules = {}
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        item = item.replace("→", "->").replace("=>", "->")
        if "->" not in item:
            continue
        src, dst = item.split("->", 1)
        src, dst = src.strip(), dst.strip()
        if not src or not dst:
            continue
        rules[src] = dst
    return sorted(rules.items(), key=lambda pair: len(pair[0]), reverse=True)


def apply_hotwords(text, rules=None):
    rules = load_hotwords() if rules is None else rules
    if not text or not rules:
        return text
    mapping = dict(rules)
    pattern = re.compile("|".join(re.escape(src) for src, _ in rules))
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def add_hotword(src, dst):
    src, dst = (src or "").strip(), (dst or "").strip()
    if (not src or not dst or src.startswith("#")
            or any(tok in src for tok in ("->", "=>", "→"))):
        return False
    ensure_hotwords_file()
    try:
        with open(HOTWORDS_PATH, "r", encoding="utf-8-sig") as f:
            data = f.read()
    except (OSError, UnicodeError):
        data = ""
    prefix = "" if not data or data.endswith("\n") else "\n"
    try:
        with open(HOTWORDS_PATH, "a", encoding="utf-8") as f:
            f.write(f"{prefix}{src} -> {dst}\n")
    except OSError:
        log("save hotword failed")
        return False
    log(f"hotword added {src!r} -> {dst!r}")
    return True


def open_hotwords_file():
    ensure_hotwords_file()
    try:
        os.startfile(HOTWORDS_PATH)
        return True
    except OSError:
        log("open hotwords failed")
        return False


PUNCT_MODES = ("keep_all", "remove_period", "remove_trailing", "space_replace")
PARAGRAPH_MODES = ("none", "blank", "single")
VOICE_MODES = ("hold", "hands_free")
ASR_ENGINES = ("doubao", "doubao_api", "qwen", "wetype", "baidu", "ifly")
ENGINE_ITEMS = (("doubao", "豆包输入法（本地 DLL）"),
                ("doubao_api", "豆包（云端 API）"),
                ("qwen", "千问（云端 API）"),
                ("wetype", "微信输入法（云端 API）"),
                ("baidu", "百度输入法（云端 API）"),
                ("ifly", "讯飞输入法（云端 API）"))
ENGINE_NAMES = {"doubao": "豆包", "doubao_api": "豆包 API", "qwen": "千问",
                "wetype": "微信输入法", "baidu": "百度输入法", "ifly": "讯飞输入法"}
ENGINE_LABELS = dict(ENGINE_ITEMS)
MIN_HOLD_MS = (0, 150, 250, 400)
MIN_HOLD_DEFAULT = 250
_SENT_CHUNK_RE = re.compile(r".+?(?:[。！？!?；;]+[”’」』）】》〉]*|$)")
_REPLACE_PUNCT = "，。！？；：、…—～"
_TRAIL_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:…—～]+([”’」』）】》〉]*)$")
_TRAIL_PERIOD_RE = re.compile(r"[。．.]+([”’」』）】》〉]*)$")


def normalize_paragraphs(text, mode):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if mode not in ("blank", "single"):
        return text
    parts = []
    for line in text.split("\n"):
        for piece in _SENT_CHUNK_RE.findall(line.strip()):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    if not parts:
        return ""
    return ("\n\n" if mode == "blank" else "\n").join(parts)


def format_text(text, punctuation="keep_all", paragraph="none"):
    text = (text or "").strip()
    if not text:
        return ""
    text = normalize_paragraphs(text, paragraph)
    if punctuation == "remove_period":
        text = "\n".join(_TRAIL_PERIOD_RE.sub(r"\1", line.rstrip())
                         for line in text.split("\n"))
    elif punctuation == "remove_trailing":
        text = "\n".join(_TRAIL_PUNCT_RE.sub(r"\1", line.rstrip())
                         for line in text.split("\n"))
    elif punctuation == "space_replace":
        text = text.translate({ord(ch): " " for ch in _REPLACE_PUNCT})
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" ([”’」』）】》〉])", r"\1", text)
        text = re.sub(r"([“‘「『（【《〈]) ", r"\1", text)
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------ smart organize

ASR_DLLS = ("audioeffect-mt.dll", "sscronet.dll")
SMART_TEXT_CAP = 12000
SMART_MIN_CHARS = 5
SMART_AVAILABLE = True
_SMART_CLIP_SENTINEL = "\u2063doubao-smart-copy\u2063"


def _bundled_dll_dir():
    return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "dll")


def prepare_asr_dlls():
    bundled = _bundled_dll_dir()
    if all(os.path.isfile(os.path.join(bundled, f)) for f in ASR_DLLS):
        return bundled
    raise RuntimeError("缺少语音引擎文件")


def smart_organize(text):
    try:
        content, no_rewrite = api_smart.organize(text)
    except Exception as exc:
        return "error", str(exc)
    if no_rewrite:
        return "same", ""
    content = content.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return ("rewrite", content) if content else ("same", "")


def icon_file():
    for base in (getattr(sys, "_MEIPASS", None), APP_DIR):
        if not base:
            continue
        path = os.path.join(base, ICON_NAME)
        if os.path.exists(path):
            return path
    return ""


def float_icon_file():
    for base in (getattr(sys, "_MEIPASS", None), APP_DIR):
        if not base:
            continue
        path = os.path.join(base, FLOAT_ICON_NAME)
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


def log_path(now=None):
    stamp = now if now is not None else time.localtime()
    return os.path.join(LOG_DIR, time.strftime("voice_ime-%Y-%m-%d.log", stamp))


def log(msg):
    now = time.localtime()
    line = time.strftime("%H:%M:%S ", now) + str(msg)
    try:
        with _log_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(log_path(now), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


api_common.logger = log


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
        self.extra = {}
        # frontier(QUIC/UDP 443) 在很多网络被拦截，默认改走 cfg 里的 WebSocket(TCP)
        self.use_frontier = False

    def new_session(self, verbose=False, on_text=None):
        return AsrSession(self, verbose, on_text)

    def _keep_bytes(self, b):
        buf = ctypes.create_string_buffer(b)
        self._keep.append(buf)
        return ctypes.addressof(buf)

    def load(self):
        dll_dir = prepare_asr_dlls()
        os.add_dll_directory(dll_dir)
        try:
            self.dll = ctypes.CDLL(os.path.join(dll_dir, "audioeffect-mt.dll"))
        except OSError:
            log(f"asr dll load error dir={dll_dir} " +
                traceback.format_exc().replace("\n", " | "))
            raise
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

        if self.use_frontier:
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
        else:
            log("transport=websocket")

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
        put64(0x68, self._keep_bytes(json.dumps(self.extra, separators=(",", ":")).encode()))
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

    def set_extra(self, extra):
        self.extra = dict(extra)
        if self.cfg is not None:
            data = json.dumps(self.extra, separators=(",", ":")).encode()
            ctypes.c_uint64.from_buffer(self.cfg, 0x68).value = self._keep_bytes(data)
            log(f"asr extra={self.extra}")

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


def create_engine(kind):
    if kind == "doubao_api":
        return api_doubao.DoubaoApiEngine()
    if kind == "qwen":
        return api_qwen.QwenEngine()
    if kind == "wetype":
        return api_wetype.WeChatEngine()
    if kind == "baidu":
        return api_baidu.BaiduEngine()
    if kind == "ifly":
        return api_ifly.IflyEngine()
    return AsrEngine()


# ------------------------------------------------------------------ capture

class Mic:
    def __init__(self, device=None):
        self.device = device
        self.stream = None
        self.sink = None
        self.lock = threading.Lock()
        self.pre = bytearray()
        self.level = 0.0

    def preload(self):
        import sounddevice

    def open(self):
        import sounddevice as sd
        if self.stream is not None:
            return
        stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                   device=self.device, blocksize=0,
                                   callback=self._cb)
        with self.lock:
            del self.pre[:]
        self.stream = stream
        try:
            stream.start()
        except Exception:
            self.stream = None
            try:
                stream.close()
            except Exception:
                pass
            raise
        log(f"mic opened: {sd.query_devices(self.device)['name'] if self.device is not None else 'default'}")

    def _cb(self, indata, frames, t, status):
        try:
            b = bytes(indata)
        except Exception:
            return
        n = len(b) // 2
        if n:
            samples = struct.unpack("<%dh" % n, b[:n * 2])
            self.level = max(max(samples), -min(samples)) / 32768.0
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
        stream = self.stream
        self.stream = None
        self.level = 0.0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            log("mic closed")
        with self.lock:
            del self.pre[:]


class MicFanout:
    def __init__(self, sinks):
        self.sinks = list(sinks)

    def push(self, data):
        for sink in self.sinks:
            try:
                sink.push(data)
            except Exception:
                pass


def list_input_devices():
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
        out = []
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) <= 0:
                continue
            hname = hostapis[d["hostapi"]]["name"]
            if hname not in ("MME", "Windows DirectSound"):
                continue
            out.append({"index": i, "name": d.get("name") or f"设备 {i}",
                        "hostapi": hname})
        return out
    except Exception:
        log("list devices error " + traceback.format_exc().replace("\n", " | "))
        return []


def resolve_mic_device(sel):
    if not isinstance(sel, dict) or not sel.get("name"):
        return None
    try:
        import sounddevice as sd
        name, hname = sel["name"], sel.get("hostapi")
        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
        for i, d in enumerate(devs):
            if (d.get("max_input_channels", 0) > 0 and d.get("name") == name
                    and (not hname or hostapis[d["hostapi"]]["name"] == hname)):
                return i
        for i, d in enumerate(devs):
            if d.get("max_input_channels", 0) > 0 and d.get("name") == name:
                return i
    except Exception:
        log("resolve device error " + traceback.format_exc().replace("\n", " | "))
    return None


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
    # 编辑控件普遍忽略 U+000A/U+000D；用 Shift+Enter 插入换行，避免聊天框 Enter 直接发送
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    events = []
    for i, line in enumerate(text.split("\n")):
        if i:
            events.extend([_vk_event(0x10), _vk_event(0x0D), _vk_event(0x0D, True),
                           _vk_event(0x10, True)])
        data = line.encode("utf-16-le")
        for j in range(0, len(data), 2):
            unit = int.from_bytes(data[j:j + 2], "little")
            events.append(_uni_event(unit, False))
            events.append(_uni_event(unit, True))
    sent = 0
    for j in range(0, len(events), 64):
        sent += _send(events[j:j + 64])
        time.sleep(0.002)
    return sent, len(events)


user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = ctypes.c_void_p
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]


def _set_clipboard(text):
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\0\0"
        h = kernel32.GlobalAlloc(0x0042, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(13, ctypes.c_void_p(h))
    finally:
        user32.CloseClipboard()
    return True


def _get_clipboard_text():
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        return None
    try:
        if not user32.IsClipboardFormatAvailable(13):
            return None
        handle = user32.GetClipboardData(13)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(ctypes.c_void_p(handle))
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(ctypes.c_void_p(handle))
    finally:
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
WM_GETTEXTLENGTH = 0x000E
EM_GETSEL = 0x00B0
EM_SETSEL = 0x00B1
CONTROL_TEXT_CAP = 65536
WEAK_MAX_CHARS = 4000
WEAK_CLASS_HINTS = ("chrome", "mozilla", "firefox", "electron", "webview",
                    "scintilla", "qt5")

user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]


def _send_timeout(hwnd, msg, wp=0, lp=0, timeout=120):
    if not hwnd:
        return None
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), msg, ctypes.c_size_t(wp).value,
                                    ctypes.c_void_p(ctypes.c_size_t(lp).value),
                                    SMTO_ABORTIFHUNG, timeout, ctypes.byref(res))
    if not ok:
        return None
    return int(res.value)


def _control_text(hwnd, limit=4096, timeout=120):
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(limit + 1)
    res = ctypes.c_size_t(0)
    ok = user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), WM_GETTEXT, limit + 1,
                                    ctypes.cast(buf, ctypes.c_void_p),
                                    SMTO_ABORTIFHUNG, timeout, ctypes.byref(res))
    if not ok:
        return None
    return buf.value


def _control_length(hwnd):
    return _send_timeout(hwnd, WM_GETTEXTLENGTH)


def _read_control(hwnd, cap=CONTROL_TEXT_CAP):
    total = _control_length(hwnd)
    if not total or total < 0 or total > cap:
        return None, total or 0
    text = _control_text(hwnd, total)
    if text is None or len(text) != total:
        return None, total
    return text, total


def _read_control_settled(hwnd, tries=3, delay=0.12):
    text, total = _read_control(hwnd)
    for _ in range(tries - 1):
        if text is not None:
            break
        time.sleep(delay)
        text, total = _read_control(hwnd)
    return text, total


def _class_name(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 128)
    return buf.value


def _weak_allowed(hwnd):
    cls = _class_name(hwnd).lower()
    return any(hint in cls for hint in WEAK_CLASS_HINTS)


def _tail_match(text, expected):
    if not text or not expected:
        return 0
    if text.endswith(expected):
        return len(expected)
    variant = expected.replace("\r\n", "\n").replace("\n", "\r\n")
    if variant != expected and text.endswith(variant):
        return len(variant)
    return 0


def _text_variants(text):
    out = [text]
    variant = text.replace("\r\n", "\n").replace("\n", "\r\n")
    if variant != text:
        out.append(variant)
    return out


def _async_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def _wait_modifiers_released(timeout=2.0):
    end = time.monotonic() + timeout
    while True:
        if not any(_async_down(vk) for vk in (0x11, 0x12, 0x10, 0x5B, 0x5C)):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def _select_backward(count):
    if count <= 0:
        return 0
    sent = _send([_vk_event(0x10)])
    left = count
    while left > 0:
        n = min(left, 400)
        batch = []
        for _ in range(n):
            batch.append(_vk_event(0x25, False, True))
            batch.append(_vk_event(0x25, True, True))
        sent += _send(batch)
        left -= n
        time.sleep(0.003)
    sent += _send([_vk_event(0x10, True)])
    return sent


def _weak_safe(expected):
    if len(expected) > WEAK_MAX_CHARS:
        return False
    for ch in expected:
        if ord(ch) > 0xFFFF or ch in "\u200d\ufe0f\ufe0e":
            return False
    return True


def _collapse_selection():
    _send([_vk_event(0x27, False, True), _vk_event(0x27, True, True)])


def _select_suffix(hwnd, text, total, suffix):
    # WM_GETTEXT 把换行算 2 个字符，RichEdit 的 EM_SETSEL 位置只算 1 个，先全选校准
    if total > 60000:
        return False
    if _send_timeout(hwnd, EM_SETSEL, 0, -1, timeout=300) is None:
        return False
    sel = _send_timeout(hwnd, EM_GETSEL)
    if sel is None:
        _collapse_selection()
        return False
    lo, hi = sel & 0xFFFF, (sel >> 16) & 0xFFFF
    newlines = text.count("\r\n")
    if hi == total:
        start = total - len(suffix)
    elif hi == total - newlines:
        start = hi - (len(suffix) - suffix.count("\r\n"))
    else:
        _collapse_selection()
        log(f"select: model unknown sel={lo},{hi} total={total} nl={newlines}")
        return False
    if lo != 0 or start < 0 or start > hi:
        _collapse_selection()
        return False
    _send_timeout(hwnd, EM_SETSEL, start, -1, timeout=300)
    check = _send_timeout(hwnd, EM_GETSEL)
    if check is not None:
        lo2, hi2 = check & 0xFFFF, (check >> 16) & 0xFFFF
        if lo2 != start or hi2 != hi:
            _collapse_selection()
            log(f"select: rejected sel={lo2},{hi2} want={start},{hi}")
            return False
    return True


def replace_recent_text(expected, new, fg, focus, mode="unicode", guard=None):
    """选中刚输入的尾段并替换为 new。返回 ok/weak/window/focus/keys/changed/
    mismatch/select/unreadable/unsafe/verify/unknown。"""
    if not expected or not new:
        return "empty"
    fg_now = int(user32.GetForegroundWindow() or 0)
    if fg_now != fg:
        return "window"
    tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
    focus_now, _ = _thread_focus(tid)
    if focus and focus_now != focus:
        return "focus"
    if not focus_now:
        return "focus"
    if guard and not guard():
        return "changed"
    if not _wait_modifiers_released():
        return "keys"
    if guard and not guard():
        return "changed"
    text, total = _read_control_settled(focus_now)
    matched = _tail_match(text, expected)
    if text and matched:
        if guard and not guard():
            return "changed"
        start = total - matched
        prefix = text[:start]
        if not _select_suffix(focus_now, text, total, text[start:]):
            # Scintilla（Notepad2 等）的 EM_GETSEL 位置模型与 WM_GETTEXT 不一致
            if not _weak_allowed(focus_now):
                return "select"
            if not _weak_safe(expected):
                return "unsafe"
            if guard and not guard():
                return "changed"
            _select_backward(len(expected))
            inject_text(new, mode)
            time.sleep(0.4)
            return "weak"
        if guard and not guard():
            _collapse_selection()
            return "changed"
        inject_text(new, mode)
        deadline = time.monotonic() + 2.0 + len(new) / 3000.0
        after = None
        while True:
            time.sleep(0.15)
            after, _ = _read_control(focus_now)
            if after is not None:
                for variant in _text_variants(new):
                    if after == prefix + variant:
                        return "ok"
            if time.monotonic() >= deadline:
                break
        if after is None:
            return "unknown"
        log(f"replace: verify failed total={len(after)} want={len(prefix) + len(new)}")
        return "verify"
    if (not text or not matched) and _weak_allowed(focus_now):
        if not _weak_safe(expected):
            return "unsafe"
        if guard and not guard():
            return "changed"
        _select_backward(len(expected))
        inject_text(new, mode)
        time.sleep(0.4)
        return "weak"
    if not text:
        return "unreadable"
    return "mismatch"


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


def _same_target(snap):
    fg = int(user32.GetForegroundWindow() or 0)
    if fg != snap["fg"]:
        return False
    if not snap["focus"]:
        return True
    tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
    focus, _ = _thread_focus(tid)
    return focus == snap["focus"]


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
WH_MOUSE_LL = 14
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN = 0x0201, 0x0204, 0x0207
WM_MBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP = 0x0208, 0x020B, 0x020C
WM_NCMBUTTONDOWN, WM_NCMBUTTONUP = 0x00A7, 0x00A8
WM_NCXBUTTONDOWN, WM_NCXBUTTONUP = 0x00AB, 0x00AC
VK_RMENU, VK_MENU, VK_ESCAPE = 0xA5, 0x12, 0x1B
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint32,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


MOD_GROUPS = {
    "CTRL": ("LCTRL", "RCTRL"),
    "ALT": ("LALT", "RALT"),
    "SHIFT": ("LSHIFT", "RSHIFT"),
}
_MOD_TOKENS = {"LCTRL", "RCTRL", "LALT", "RALT", "LSHIFT", "RSHIFT",
               "CTRL", "ALT", "SHIFT"}
_LEFT_MOD_TOKENS = {"LCTRL", "LALT", "LSHIFT"}
_MOD_FALLBACK = {"CTRL": "RCTRL", "ALT": "RALT", "SHIFT": "RSHIFT"}
_MOUSE_BUTTON_TOKENS = {"XBUTTON1", "XBUTTON2"}
_STANDALONE_TOKENS = _MOUSE_BUTTON_TOKENS | {"MBUTTON", "BROWSER_BACK", "BROWSER_FORWARD"}
_MOD_ORDER = ["CTRL", "LCTRL", "RCTRL", "ALT", "LALT", "RALT",
              "SHIFT", "LSHIFT", "RSHIFT"]
_TOKEN_VK = {"LCTRL": 0xA2, "RCTRL": 0xA3, "LALT": 0xA4, "RALT": 0xA5,
             "LSHIFT": 0xA0, "RSHIFT": 0xA1, "CTRL": 0x11, "ALT": 0x12,
             "SHIFT": 0x10, "MBUTTON": 0x04, "XBUTTON1": 0x05,
             "XBUTTON2": 0x06, "SPACE": 0x20, "ENTER": 0x0D, "TAB": 0x09,
             "BACKSPACE": 0x08, "BROWSER_BACK": 0xA6,
             "BROWSER_FORWARD": 0xA7}


def _token_vk(tok):
    vk = _TOKEN_VK.get(tok)
    if vk:
        return vk
    if _is_fkey(tok):
        return 0x6F + int(tok[1:])
    if len(tok) == 1 and (tok.isdigit() or "A" <= tok <= "Z"):
        return ord(tok)
    return 0


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
    if vk == 0xA6:
        return "BROWSER_BACK"
    if vk == 0xA7:
        return "BROWSER_FORWARD"
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    return "VK%02X" % vk


def _is_fkey(tok):
    return (len(tok) >= 2 and tok[0] == "F" and tok[1:].isdigit()
            and 1 <= int(tok[1:]) <= 12)


def _split_hotkey(spec):
    tokens = []
    for part in str(spec or "").upper().split("+"):
        part = part.strip()
        if part == "CONTROL":
            part = "CTRL"
        if part and part not in tokens:
            tokens.append(part)
    return tokens


def parse_hotkey(spec):
    tokens = _split_hotkey(spec)
    if not tokens or "ESC" in tokens:
        return ["RALT"]
    if len(tokens) == 1:
        # 左侧修饰键单独作快捷键会抢占正常 Ctrl/Alt/Shift 操作，只允许右侧
        if tokens[0] in _LEFT_MOD_TOKENS:
            return [DEFAULT_HOTKEY]
        if tokens[0] in _MOD_FALLBACK:
            return [_MOD_FALLBACK[tokens[0]]]
    base = [t for t in tokens if t not in _MOD_TOKENS]
    has_mod = any(t in _MOD_TOKENS for t in tokens)
    if (base and not has_mod
            and not all(_is_fkey(t) or t in _STANDALONE_TOKENS for t in base)):
        return ["RALT"]
    return tokens


def hotkey_display(tokens):
    names = {"CTRL": "Ctrl", "LCTRL": "左 Ctrl", "RCTRL": "右 Ctrl",
             "ALT": "Alt", "LALT": "左 Alt", "RALT": "右 Alt",
             "SHIFT": "Shift", "LSHIFT": "左 Shift", "RSHIFT": "右 Shift",
             "SPACE": "Space", "ENTER": "Enter", "TAB": "Tab",
             "BACKSPACE": "Backspace",
             "XBUTTON1": "鼠标侧键1", "XBUTTON2": "鼠标侧键2",
             "MBUTTON": "鼠标中键",
             "BROWSER_BACK": "浏览器后退", "BROWSER_FORWARD": "浏览器前进"}
    return "+".join(names.get(t, t) for t in tokens)


def parse_smart_hotkey(spec):
    tokens = _split_hotkey(spec)
    if not tokens or "ESC" in tokens:
        return []
    if len(tokens) == 1:
        if tokens[0] in _LEFT_MOD_TOKENS:
            return []
        if tokens[0] in _MOD_FALLBACK:
            return [_MOD_FALLBACK[tokens[0]]]
    base = [t for t in tokens if t not in _MOD_TOKENS]
    has_mod = any(t in _MOD_TOKENS for t in tokens)
    if (base and not has_mod
            and not all(_is_fkey(t) or t in _STANDALONE_TOKENS for t in base)):
        return []
    return tokens


def _token_overlap(a, b):
    if a == b:
        return True
    if a in MOD_GROUPS and b in MOD_GROUPS[a]:
        return True
    if b in MOD_GROUPS and a in MOD_GROUPS[b]:
        return True
    return False


def _combo_covers(big, small):
    return bool(small) and all(
        any(_token_overlap(tok, other) for other in big) for tok in small)


def hotkeys_conflict(a, b):
    if not a or not b:
        return False
    return _combo_covers(a, b) or _combo_covers(b, a)


def is_undo_combo(tokens):
    base = [t for t in tokens if t not in _MOD_TOKENS]
    mods = [t for t in tokens if t in _MOD_TOKENS]
    return (len(base) == 1 and base[0] == "Z" and len(mods) == 1
            and mods[0] in ("CTRL", "LCTRL", "RCTRL"))


class Hotkey:
    def __init__(self, app, spec=DEFAULT_HOTKEY, smart_spec=""):
        self.app = app
        self.set_combo(spec)
        self.set_smart_combo(smart_spec)
        self.hook = None
        self.mouse_hook = None
        self.proc = HOOKPROC(self._cb)
        self.mouse_proc = HOOKPROC(self._mouse_cb)
        self.thread = None
        self.down = set()
        self.swallowed = set()
        self.cap_mods = set()
        self.cap_seen_mods = set()
        self.active = False
        self.active_mode = "hold"
        self.float_active = False
        self.active_session = None

    def display(self):
        return hotkey_display(self.combo)

    def release_hint(self):
        mode = self.active_mode if self.active else self.app.voice_mode
        if mode == "hands_free":
            if self.float_active:
                return "点击悬浮按钮或任意键结束"
            return f"再按 {self.display()} 或任意键结束"
        if self.single_mod:
            return f"松开{self.display()}结束"
        if "MBUTTON" in self.combo:
            return "松开鼠标中键结束"
        if any(t in ("XBUTTON1", "XBUTTON2") for t in self.combo):
            return "松开鼠标侧键结束"
        return "松开按键结束"

    def set_combo(self, spec):
        self.combo = parse_hotkey(spec)
        self.single_mod = len(self.combo) == 1 and self.combo[0] in _MOD_TOKENS
        self.active = False
        self.swallowed = set()

    def set_smart_combo(self, spec):
        self.smart_combo = parse_smart_hotkey(spec)

    def smart_display(self):
        return hotkey_display(self.smart_combo) if self.smart_combo else "未设置"

    def wait_released(self, timeout=8.0):
        vks = [vk for vk in (_token_vk(t) for t in self.smart_combo) if vk]
        if not vks:
            return True
        end = time.monotonic() + timeout
        while any(_async_down(vk) for vk in vks):
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)
        return True

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
        self.mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self.mouse_proc,
                                                   kernel32.GetModuleHandleW(None), 0)
        log(f"keyboard hook={self.hook} mouse hook={self.mouse_hook} hotkey={self.display()}")
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

    def _smart_satisfied(self):
        return bool(self.smart_combo) and all(
            self._token_down(t) for t in self.smart_combo)

    def _swallow_smart_down(self, tok):
        if not self.smart_combo:
            return False
        if len(self.smart_combo) == 1 and self.smart_combo[0] in _MOD_TOKENS:
            return self._token_matches(self.smart_combo[0], tok)
        base = [t for t in self.smart_combo if t not in _MOD_TOKENS]
        if not base:
            return tok in _MOD_TOKENS and any(
                self._token_matches(c, tok) for c in self.smart_combo)
        if any(self._token_matches(b, tok) for b in base):
            mods = [t for t in self.smart_combo if t not in base]
            return all(self._token_down(t) for t in mods)
        return False

    def _smart_trigger(self, tok, repeated, injected):
        if not self.smart_combo or repeated or injected or self.active:
            return False
        if not self._smart_satisfied():
            return False
        if not self.app.on_smart_hotkey():
            return False
        if self._swallow_smart_down(tok):
            self.swallowed.add(tok)
        return True

    def _cb(self, nCode, wParam, lParam):
        if nCode == 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.vkCode in (0, 0xE7):
                return user32.CallNextHookEx(self.hook, nCode, wParam, lParam)
            tok = canon_key(kb.vkCode, kb.flags & LLKHF_EXTENDED)
            is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)
            if (is_down or is_up) and self._handle_input(
                    tok, is_down, bool(kb.flags & LLKHF_INJECTED)):
                return 1
        return user32.CallNextHookEx(self.hook, nCode, wParam, lParam)

    def _handle_input(self, tok, is_down, injected):
        """键盘键和鼠标键共用的状态机，返回 True 表示吞掉该事件。"""
        if self.app.capturing:
            return self._capture(tok, is_down)
        if is_down:
            repeated = tok in self.down
            self.down.add(tok)
            if tok == "ESC" and self.app.on_cancel():
                self.active = False
                return True
            if not injected:
                if (not repeated and self._ctrl_z(tok)
                        and self.app.on_undo_hotkey()):
                    self.swallowed.add(tok)
                    return True
                if tok not in _MOD_TOKENS:
                    self.app.note_user_input(tok)
            mode = self.active_mode if self.active else self.app.voice_mode
            if mode == "hands_free":
                if self.active:
                    if not repeated and not injected:
                        if self._satisfied():
                            self.active = False
                            self.app.on_key_up("hands_free")
                            self.swallowed.add(tok)
                            return True
                        if tok not in _MOD_TOKENS:
                            self.active = False
                            self.app.on_key_up("hands_free")
                elif not repeated and self._satisfied():
                    self.active = True
                    self.active_mode = "hands_free"
                    self.float_active = False
                    self.app.on_key_down()
                    if self._swallow_down(tok):
                        self.swallowed.add(tok)
                        return True
                elif self._smart_trigger(tok, repeated, injected):
                    return True
            else:
                if not self.active and self._satisfied():
                    self.active = True
                    self.active_mode = "hold"
                    self.float_active = False
                    self.app.on_key_down()
                    if self._swallow_down(tok):
                        self.swallowed.add(tok)
                        return True
                elif self._smart_trigger(tok, repeated, injected):
                    return True
                elif self._swallow_down(tok):
                    self.swallowed.add(tok)
                    return True
        else:
            self.down.discard(tok)
            if (self.active and self.active_mode == "hold"
                    and any(self._token_matches(c, tok) for c in self.combo)):
                self.active = False
                self.app.on_key_up(self.active_mode)
            if tok in self.swallowed:
                self.swallowed.discard(tok)
                return True
        return False

    def _ctrl_z(self, tok):
        if tok != "Z":
            return False
        if any(self._token_matches(c, tok) for c in self.combo):
            return False
        if not (_async_down(0x11) or self._token_down("CTRL")):
            return False
        for vk in (0x12, 0x10, 0x5B, 0x5C):
            if _async_down(vk):
                return False
        return True

    def _mouse_cb(self, nCode, wParam, lParam):
        if nCode == 0:
            ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            injected = bool(ms.flags & LLMHF_INJECTED)
            if wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP,
                          WM_NCXBUTTONDOWN, WM_NCXBUTTONUP):
                tok = "XBUTTON%d" % ((ms.mouseData >> 16) & 0xFFFF)
                if tok in _MOUSE_BUTTON_TOKENS:
                    is_down = wParam in (WM_XBUTTONDOWN, WM_NCXBUTTONDOWN)
                    # 本程序不注入鼠标键，注入的鼠标键事件也按真实按键处理
                    if self._handle_input(tok, is_down, False):
                        return 1
            elif wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP,
                            WM_NCMBUTTONDOWN, WM_NCMBUTTONUP):
                is_down = wParam in (WM_MBUTTONDOWN, WM_NCMBUTTONDOWN)
                if self._handle_input("MBUTTON", is_down, False):
                    return 1
            elif wParam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN) and not injected:
                self.app.note_user_input("mouse")
                if self.active and self.active_mode == "hands_free":
                    fb = self.app.float_btn
                    if fb is not None and fb.contains(ms.pt.x, ms.pt.y):
                        fb.consumed = True
                    self.active = False
                    self.float_active = False
                    self.app.on_key_up("hands_free")
        return user32.CallNextHookEx(self.mouse_hook, nCode, wParam, lParam)

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
            if not (self.cap_mods or _is_fkey(tok) or tok in _STANDALONE_TOKENS):
                log(f"capture invalid {tok}")
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
                if tok in _LEFT_MOD_TOKENS:
                    self.app.capture_left_mod_rejected(tok)
                else:
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
        self.toast = False
        self.gen = 0

    def set(self, kind, msg="", live=None, toast=False):
        with self.lock:
            self.kind = kind
            self.msg = msg
            self.toast = toast
            if live is not None:
                self.live = live
            elif kind in ("none", "done"):
                self.live = ""
            self.gen += 1
            return self.gen

    def get(self):
        with self.lock:
            return self.kind, self.msg, self.live, self.toast, self.gen


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
            "SetWindowRgn": ([p, p, ctypes.c_bool], i),
            "DrawIconEx": ([p, i, i, p, i, i, u, p, u], ctypes.c_bool),
            "GetWindowRect": ([p, ctypes.c_void_p], ctypes.c_bool),
            "GetCursorPos": ([ctypes.c_void_p], ctypes.c_bool),
            "LoadCursorW": ([p, p], p),
            "GetSystemMetrics": ([i], i),
            "KillTimer": ([p, ctypes.c_size_t], ctypes.c_bool),
            "PostMessageW": ([p, u, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_bool),
        }),
        (gdi32, {
            "SelectObject": ([p, p], p),
            "GetTextExtentPoint32W": ([p, wintypes.LPCWSTR, i, ctypes.POINTER(wintypes.SIZE)],
                                      ctypes.c_bool),
            "SetBkMode": ([p, i], i),
            "SetBkColor": ([p, u], u),
            "SetTextColor": ([p, u], u),
            "CreateSolidBrush": ([u], p),
            "CreateFontW": ([i, i, i, i, i, u, u, u, u, u, u, u, u, wintypes.LPCWSTR], p),
            "CreatePen": ([i, i, u], p),
            "RoundRect": ([p, i, i, i, i, i, i], ctypes.c_bool),
            "CreateRoundRectRgn": ([i, i, i, i, i, i], p),
            "CreateCompatibleDC": ([p], p),
            "CreateCompatibleBitmap": ([p, i, i], p),
            "BitBlt": ([p, i, i, i, i, p, i, i, u], ctypes.c_bool),
            "DeleteObject": ([p], ctypes.c_bool),
            "DeleteDC": ([p], ctypes.c_bool),
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
    WAVE_N = 16

    def __init__(self, state, mic=None):
        self.state = state
        self.mic = mic
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
        self._frame_pen = None
        self._font = None
        self._font_small = None
        self.radius = 10
        self._wave = [0.0] * self.WAVE_N
        self._wave_level = 0.0
        self._wave_brush = None
        self.wave_bw = 3
        self.wave_gap = 2
        self.wave_w = self.WAVE_N * 3 + (self.WAVE_N - 1) * 2
        self.wave_tgap = 10
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
        self._frame_pen = gdi32.CreatePen(0, 1, 0x00443C3C)
        self._wave_brush = gdi32.CreateSolidBrush(0x00F09A4D)

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
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 178, 0x00000002)
        user32.GetDpiForWindow.restype = ctypes.c_uint32
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        dpi = user32.GetDpiForWindow(self.hwnd)
        s = self.scale = max(1.0, dpi / 96.0)
        self.pad = int(self.PAD * s)
        self.status_h = int(self.STATUS_H * s)
        self.gap = int(self.GAP * s)
        self.line_h = int(self.LINE_H * s)
        self.max_w = int(self.MAX_TEXT_W * s)
        self.wave_bw = max(2, int(3 * s))
        self.wave_gap = max(1, int(2 * s))
        self.wave_w = self.WAVE_N * self.wave_bw + (self.WAVE_N - 1) * self.wave_gap
        self.wave_tgap = int(10 * s)
        self.radius = int(10 * s)
        self._font = gdi32.CreateFontW(-int(21 * s), 0, 0, 0, 600, 0, 0, 0, 0, 0, 0, 5, 0,
                                       "Microsoft YaHei UI")
        self._font_small = gdi32.CreateFontW(-int(16 * s), 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 5, 0,
                                             "Microsoft YaHei UI")
        log(f"overlay hwnd={self.hwnd} dpi={dpi} scale={s:.2f}")

    def _anchor(self, toast=False):
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
        if toast:
            m = int(24 * self.scale)
            return (max(r.left, r.right - self.w - m),
                    max(r.top, r.bottom - self.h - m))
        return (r.left + r.right) // 2 - self.w // 2, r.bottom - self.h - int(100 * self.scale)

    def _apply(self):
        kind, msg, live, toast, gen = self.state.get()
        if gen == self.seen_gen:
            return
        self.seen_gen = gen
        if kind == "none":
            self.kind = "none"
            self._work = None
            user32.ShowWindow(self.hwnd, self.SW_HIDE)
            log("overlay hide")
            return
        self.kind = kind
        self.text = msg
        self.live = live
        self.disp_live = ""
        if kind != "rec":
            self._wave_level = 0.0
            self._wave = [0.0] * self.WAVE_N
        if kind == "rec" and msg.startswith("● "):
            msg = msg[2:]
            self.text = msg
        two = kind in ("rec", "wait") and bool(live.strip())
        hdc = user32.GetDC(self.hwnd)
        if two:
            old = gdi32.SelectObject(hdc, ctypes.c_void_p(self._font_small))
            sz = wintypes.SIZE()
            gdi32.GetTextExtentPoint32W(hdc, msg, len(msg), ctypes.byref(sz))
            status_w = sz.cx
            if kind == "rec":
                status_w += self.wave_w + self.wave_tgap
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
            if kind == "rec":
                self.w = max(int(200 * self.scale),
                             self.pad + self.wave_w + self.wave_tgap + size.cx + self.pad)
            else:
                self.w = max(int(160 * self.scale), size.cx + int(48 * self.scale))
            self.h = int(46 * self.scale)
        user32.ReleaseDC(self.hwnd, hdc)
        x, y = self._anchor(toast)
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 165 if toast else 178, 0x00000002)
        ok = user32.SetWindowPos(self.hwnd, self.HWND_TOPMOST, x, y, self.w, self.h,
                                 self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW)
        rgn = gdi32.CreateRoundRectRgn(0, 0, self.w + 1, self.h + 1,
                                       self.radius * 2, self.radius * 2)
        user32.SetWindowRgn(self.hwnd, rgn, True)
        user32.InvalidateRect(self.hwnd, None, True)
        now = time.monotonic()
        if kind != self._last_kind or now - self._log_t >= 1.0:
            self._log_t = now
            log(f"overlay show kind={kind} gen={gen} pos={x},{y} {self.w}x{self.h} "
                f"toast={toast} ok={ok} err={ctypes.get_last_error()}")
        self._last_kind = kind

    def _measure(self, hdc, text):
        sz = wintypes.SIZE()
        gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(sz))
        w = min(sz.cx, self.max_w)
        rc = wintypes.RECT(0, 0, max(w, int(24 * self.scale)), 0)
        user32.DrawTextW(hdc, text, -1, ctypes.byref(rc),
                         self.DT_CALCRECT | self.DT_WORDBREAK | self.DT_NOPREFIX)
        return w, rc.bottom

    def _tick_wave(self):
        raw = self.mic.level if self.mic is not None else 0.0
        self._wave_level = max(raw, self._wave_level * 0.72)
        self._wave.append(self._wave_level)
        del self._wave[0]
        if self.disp_live:
            r = wintypes.RECT(self.pad, self.pad,
                              self.pad + self.wave_w + 1, self.pad + self.status_h)
        else:
            r = wintypes.RECT(self.pad, 0, self.pad + self.wave_w + 1, self.h)
        user32.InvalidateRect(self.hwnd, ctypes.byref(r), True)

    def _paint_wave(self, hdc, x, cy, max_h):
        for v in self._wave:
            bh = max(int(2 * self.scale), int((v ** 0.5) * max_h))
            top = cy - bh // 2
            r = wintypes.RECT(x, top, x + self.wave_bw, top + bh)
            user32.FillRect(hdc, ctypes.byref(r), ctypes.c_void_p(self._wave_brush))
            x += self.wave_bw + self.wave_gap

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            return self._wndproc_inner(hwnd, msg, wp, lp)
        except Exception:
            log("wndproc error " + traceback.format_exc().replace("\n", " | "))
            return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _wndproc_inner(self, hwnd, msg, wp, lp):
        if msg == self.WM_TIMER:
            self._apply()
            if self.kind == "rec":
                self._tick_wave()
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
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, rc.right, rc.bottom)
        old_bmp = gdi32.SelectObject(mem, ctypes.c_void_p(bmp))
        self._draw(mem, rc)
        gdi32.BitBlt(hdc, 0, 0, rc.right, rc.bottom, mem, 0, 0, 0x00CC0020)
        gdi32.SelectObject(mem, old_bmp)
        gdi32.DeleteObject(ctypes.c_void_p(bmp))
        gdi32.DeleteDC(mem)

    def _draw(self, hdc, rc):
        old_brush = gdi32.SelectObject(hdc, ctypes.c_void_p(self._brush))
        old_pen = gdi32.SelectObject(hdc, ctypes.c_void_p(self._frame_pen))
        gdi32.RoundRect(hdc, 0, 0, rc.right, rc.bottom, self.radius * 2, self.radius * 2)
        gdi32.SelectObject(hdc, old_pen)
        gdi32.SelectObject(hdc, old_brush)
        inner = wintypes.RECT(0, 0, rc.right, rc.bottom)
        gdi32.SetBkMode(hdc, 1)
        if self.disp_live:
            old = gdi32.SelectObject(hdc, ctypes.c_void_p(self._font_small))
            gdi32.SetTextColor(hdc, 0x009E988F)
            sr = wintypes.RECT(self.pad, self.pad, rc.right - self.pad,
                               self.pad + self.status_h)
            if self.kind == "rec":
                self._paint_wave(hdc, self.pad, self.pad + self.status_h // 2,
                                 self.status_h - int(4 * self.scale))
                sr.left += self.wave_w + self.wave_tgap
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
            tr = inner
            if self.kind == "rec":
                self._paint_wave(hdc, self.pad, rc.bottom // 2,
                                 rc.bottom - int(12 * self.scale))
                tr = wintypes.RECT(self.pad + self.wave_w + self.wave_tgap, 0,
                                   rc.right - self.pad, rc.bottom)
            user32.DrawTextW(hdc, self.text, -1, ctypes.byref(tr),
                             self.DT_CENTER | self.DT_VCENTER | self.DT_SINGLELINE
                             | self.DT_NOPREFIX)
        gdi32.SelectObject(hdc, old)


# --------------------------------------------------------------------- float

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_APP = 0x8000


class FloatButton:
    WM_PAINT = 0x000F
    WM_ERASEBKGND = 0x0014
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
    SIZE = 48
    DRAG_TIMER = 2
    STATE_TIMER = 1

    def __init__(self, app):
        self.app = app
        self.hwnd = None
        self.thread = None
        self.ready = threading.Event()
        self.consumed = False
        self.visible = False
        self.rec = False
        self.size = self.SIZE
        self.scale = 1.0
        self._hicon = None
        self._brush_idle = None
        self._brush_rec = None
        self._pen_idle = None
        self._pen_rec = None
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
        return r.left <= x < r.right and r.top <= y < r.bottom

    def _run(self):
        try:
            self._create()
        except Exception:
            log("float create error " + traceback.format_exc().replace("\n", " | "))
            self.ready.set()
            return
        self.ready.set()
        user32.SetTimer(self.hwnd, self.STATE_TIMER, 120, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._brush_idle = gdi32.CreateSolidBrush(0x00FFFFFF)
        self._brush_rec = gdi32.CreateSolidBrush(0x00FBE6D6)
        self._pen_idle = gdi32.CreatePen(0, 1, 0x00C8C8C8)
        self._pen_rec = gdi32.CreatePen(0, 2, 0x00F09A4D)

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
        cls.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32649))
        cls.lpszClassName = "DoubaoVoiceFloat"
        user32.RegisterClassExW.restype = ctypes.c_ushort
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW(ctypes.byref(cls))
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        ex = (self.WS_EX_TOPMOST | self.WS_EX_TOOLWINDOW | self.WS_EX_NOACTIVATE
              | self.WS_EX_LAYERED)
        self.hwnd = user32.CreateWindowExW(ex, "DoubaoVoiceFloat", "豆包语音悬浮按钮",
                                           self.WS_POPUP, 0, 0, self.size, self.size,
                                           None, None, hinst, None)
        if not self.hwnd:
            return
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 235, 0x00000002)
        user32.GetDpiForWindow.restype = ctypes.c_uint32
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        dpi = user32.GetDpiForWindow(self.hwnd)
        self.scale = max(1.0, dpi / 96.0)
        self.size = int(self.SIZE * self.scale)
        x, y = self._initial_pos()
        user32.SetWindowPos(self.hwnd, ctypes.c_void_p(self.HWND_TOPMOST),
                            x, y, self.size, self.size, self.SWP_NOACTIVATE)
        rgn = gdi32.CreateRoundRectRgn(0, 0, self.size + 1, self.size + 1,
                                       self.size, self.size)
        user32.SetWindowRgn(self.hwnd, rgn, True)
        icon = float_icon_file()
        if icon:
            isz = int(self.size * 0.56)
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT]
            self._hicon = user32.LoadImageW(None, icon, IMAGE_ICON, isz, isz,
                                            LR_LOADFROMFILE)
        log(f"float hwnd={self.hwnd} dpi={dpi} size={self.size} pos={x},{y} "
            f"icon={bool(self._hicon)} visible={self.app.float_visible}")
        if self.app.float_visible:
            self._show()

    def _initial_pos(self):
        pos = load_config().get("float_pos")
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)
        if (isinstance(pos, (list, tuple)) and len(pos) == 2
                and all(isinstance(v, int) for v in pos)):
            x, y = pos
        else:
            work = _monitor_work_area(_cursor_monitor())
            if work is not None:
                x = work.right - self.size - int(24 * self.scale)
                y = work.bottom - self.size - int(96 * self.scale)
            else:
                x, y = vx + vw - self.size - 24, vy + vh - self.size - 96
        x = min(max(x, vx), vx + vw - self.size)
        y = min(max(y, vy), vy + vh - self.size)
        return x, y

    def _show(self):
        self.visible = True
        user32.ShowWindow(self.hwnd, self.SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, ctypes.c_void_p(self.HWND_TOPMOST),
                            0, 0, 0, 0, self.SWP_NOMOVE | self.SWP_NOSIZE
                            | self.SWP_NOACTIVATE)

    def _hide(self):
        self.visible = False
        user32.ShowWindow(self.hwnd, self.SW_HIDE)

    def _save_pos(self):
        r = wintypes.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        self.app._save_option(float_pos=[r.left, r.top])

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            return self._inner(hwnd, msg, wp, lp)
        except Exception:
            log("float error " + traceback.format_exc().replace("\n", " | "))
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
                with self.app.lock:
                    rec = self.app.session is not None
                if rec != self.rec:
                    self.rec = rec
                    user32.InvalidateRect(hwnd, None, True)
            return 0
        if msg == self.WM_ERASEBKGND:
            return 1
        if msg == self.WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == self.WM_LBUTTONDOWN:
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            self._drag = (pt.x, pt.y, r.left, r.top, False)
            user32.SetTimer(hwnd, self.DRAG_TIMER, 15, None)
            return 0
        if msg == self.WM_LBUTTONUP:
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
        # 窗口不抢焦点（NOACTIVATE），SetCapture 对后台窗口不可靠，
        # 改用 GetAsyncKeyState 轮询左键完成拖动与点击判定
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
            x = min(max(wx + dx, vx), vx + vw - self.size)
            y = min(max(wy + dy, vy), vy + vh - self.size)
            user32.SetWindowPos(hwnd, ctypes.c_void_p(self.HWND_TOPMOST),
                                x, y, 0, 0, self.SWP_NOSIZE | self.SWP_NOACTIVATE)
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

    def _paint(self, hwnd):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rc = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        w, h = rc.right, rc.bottom
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mem, bmp)
        old_br = gdi32.SelectObject(mem, self._brush_rec if self.rec
                                    else self._brush_idle)
        old_pen = gdi32.SelectObject(mem, self._pen_rec if self.rec
                                     else self._pen_idle)
        gdi32.RoundRect(mem, 0, 0, w, h, w, h)
        gdi32.SelectObject(mem, old_pen)
        gdi32.SelectObject(mem, old_br)
        if self._hicon:
            isz = int(self.size * 0.56)
            off = (w - isz) // 2
            user32.DrawIconEx(mem, off, off, self._hicon, isz, isz, 0, None, 0x0003)
        gdi32.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, 0x00CC0020)
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.EndPaint(hwnd, ctypes.byref(ps))


# ---------------------------------------------------------------------- tray

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
    ID_MIC_ALWAYS = 1005
    ID_SETTINGS = 1006
    ID_SMART = 1007
    ID_SET_SMART_HOTKEY = 1008
    ID_FLOAT = 1009

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
        nid.szTip = self.app.tray_tip()
        self._nid = nid
        ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        log(f"tray icon={ok} err={ctypes.get_last_error()}")
        if ok:
            nid.uFlags = NIF_INFO
            nid.dwInfoFlags = NIIF_INFO
            nid.szInfoTitle = "豆包语音输入"
            nid.szInfo = (f"已在后台运行：{self.app.mode_hint()}。\n"
                          "右击托盘图标可打开设置或退出。")
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
        user32.AppendMenuW(hmenu, MF_GRAYED | MF_STRING, 1, self.app.mode_hint())
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_SETTINGS, "设置…")
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_SET_HOTKEY, "设置语音快捷键…")
        if SMART_AVAILABLE:
            user32.AppendMenuW(hmenu, MF_STRING, self.ID_SET_SMART_HOTKEY,
                               "设置整理快捷键…")
        auto = MF_STRING | (MF_CHECKED if autostart_enabled() else 0)
        user32.AppendMenuW(hmenu, auto, self.ID_AUTOSTART, "开机自启")
        mic = MF_STRING | (MF_CHECKED if self.app.mic_mode == "always" else 0)
        user32.AppendMenuW(hmenu, mic, self.ID_MIC_ALWAYS, "麦克风预热")
        flt = MF_STRING | (MF_CHECKED if self.app.float_visible else 0)
        user32.AppendMenuW(hmenu, flt, self.ID_FLOAT, "显示悬浮按钮")
        if SMART_AVAILABLE:
            smart = MF_STRING | (MF_CHECKED if self.app.smart_text else 0)
            user32.AppendMenuW(hmenu, smart, self.ID_SMART, "智能文字整理")
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_OPEN_LOG, "打开日志文件夹")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_QUIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        if cmd == self.ID_SETTINGS:
            self.app.open_settings()
        elif cmd == self.ID_SET_HOTKEY:
            self.app.open_settings()
            if self.app.settings is not None:
                self.app.settings.open_capture()
        elif cmd == self.ID_SET_SMART_HOTKEY:
            self.app.open_settings()
            if self.app.settings is not None:
                self.app.settings.open_capture("smart")
        elif cmd == self.ID_AUTOSTART:
            self.app.toggle_autostart()
        elif cmd == self.ID_MIC_ALWAYS:
            self.app.toggle_mic_mode()
        elif cmd == self.ID_FLOAT:
            self.app.toggle_float()
        elif cmd == self.ID_SMART:
            self.app.toggle_smart_text()
        elif cmd == self.ID_OPEN_LOG:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                os.startfile(LOG_DIR)
            except OSError:
                pass
        elif cmd == self.ID_QUIT:
            self.app.quit()


# ------------------------------------------------------------------ settings

WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_SETFONT = 0x0030
WM_SETICON = 0x0080
WM_CTLCOLORBTN, WM_CTLCOLORSTATIC = 0x0135, 0x0138
WM_SETTINGS_SHOW = WM_APP + 2
WM_SETTINGS_REFRESH = WM_APP + 3
WM_SETTINGS_CAPTURE = WM_APP + 4
WS_CHILD, WS_VISIBLE, WS_TABSTOP = 0x40000000, 0x10000000, 0x00010000
WS_GROUP = 0x00020000
WS_CLIPCHILDREN = 0x02000000
WS_VSCROLL = 0x00200000
WS_OVERLAPPED, WS_CAPTION, WS_SYSMENU = 0x00000000, 0x00C00000, 0x00080000
WS_EX_CONTROLPARENT = 0x00010000
BS_AUTORADIOBUTTON, BS_AUTOCHECKBOX = 0x09, 0x03
SS_ETCHEDHORZ = 0x10
ES_AUTOHSCROLL = 0x0080
WS_BORDER = 0x00800000
CBS_DROPDOWNLIST, CBS_HASSTRINGS = 0x0003, 0x0200
CB_ADDSTRING, CB_SETCURSEL, CB_GETCURSEL = 0x0143, 0x014E, 0x0147
CB_SETDROPPEDWIDTH = 0x015F
BM_SETCHECK, BM_GETCHECK = 0x00F1, 0x00F0
BST_CHECKED = 1
BN_CLICKED, CBN_SELCHANGE = 0, 1
SW_HIDE, SW_SHOWNORMAL = 0, 1
SM_CXICON, SM_CYICON = 11, 12
IDC_ARROW = 32512
COLOR_BTNFACE = 15
COLOR_BTNFACE_BRUSH = COLOR_BTNFACE + 1

SETTINGS_WIDTH = 880
SETTINGS_HEIGHT = 510 if SMART_AVAILABLE else 474
WM_DPICHANGED = 0x02E0
MONITOR_DEFAULTTONEAREST = 2
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

ENGINE_TEST_WIDTH = 720
ENGINE_TEST_HEIGHT = 452
WM_PARENTNOTIFY = 0x0021
WM_CREATE_MSG = 0x0001
WM_TIMER_MSG = 0x0113
ES_READONLY = 0x0800
SS_RIGHT = 0x0002


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _cursor_monitor():
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)


def _monitor_work_area(hmon):
    if not hmon:
        return None
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    return mi.rcWork


def _monitor_dpi(hmon):
    try:
        shcore = ctypes.WinDLL("shcore")
        x, y = ctypes.c_uint(), ctypes.c_uint()
        shcore.GetDpiForMonitor.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_uint),
                                            ctypes.POINTER(ctypes.c_uint)]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
        if shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
            return int(x.value) or 96
    except (AttributeError, OSError):
        pass
    return 0


class Settings:
    ID_MODE_HOLD = 2101
    ID_MODE_FREE = 2102
    ID_HOTKEY = 2103
    ID_MIC = 2104
    ID_MIC_ALWAYS = 2105
    ID_PUNCT = 2106
    ID_SPACE = 2107
    ID_PARAGRAPH = 2108
    ID_AUTOSTART = 2109
    ID_CLOSE = 2110
    ID_HOTKEY_LABEL = 2111
    ID_MIC_HINT = 2112
    ID_FORMAT_HINT = 2113
    ID_MIC_HINT2 = 2114
    ID_SMART = 2115
    ID_SMART_HINT = 2116
    ID_SMART_HINT2 = 2117
    ID_HOTKEY_HINT = 2118
    ID_HOTKEY_HINT2 = 2119
    ID_MIN_HOLD = 2120
    ID_MIN_HOLD_LABEL = 2121
    ID_MIN_HOLD_HINT = 2122
    ID_ENGINE = 2123
    ID_ENGINE_LABEL = 2124
    ID_ENGINE_TEST = 2125
    ID_SMART_HOTKEY = 2126
    ID_SMART_HOTKEY_LABEL = 2127
    ID_SMART_HOTKEY_CLEAR = 2128
    ID_FLOAT = 2129
    ID_HOTWORD_SECTION = 2218
    ID_HOTWORD_LINE = 2219
    ID_HOTWORD_SRC = 2220
    ID_HOTWORD_DST = 2221
    ID_HOTWORD_ADD = 2222
    ID_HOTWORD_OPEN = 2223
    ID_HOTWORD_HINT = 2224
    ID_HOTWORD_ARROW = 2225

    PUNCT_ITEMS = [("keep_all", "保留所有标点"),
                   ("remove_period", "句末不加句号"),
                   ("remove_trailing", "句末不加标点"),
                   ("space_replace", "空格代替标点")]
    SPACE_ITEMS = [(True, "前后加空格"), (False, "前后无空格")]
    PARA_ITEMS = [("none", "不自动分段"),
                  ("blank", "分段后空行"),
                  ("single", "分段不空行")]
    MIN_HOLD_ITEMS = [(0, "不限制"),
                      (150, "0.15 秒"),
                      (250, "0.25 秒"),
                      (400, "0.40 秒")]

    def __init__(self, app):
        self.app = app
        self.hwnd = None
        self.thread = None
        self.ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)
        self._hwnds = {}
        self._font = None
        self._font_small = None
        self._font_header = None
        self._hinst = None
        self._scale = 1.0
        self._style = 0
        self._bg_brush = None
        self._bg_color = 0
        self.mic_items = []

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def open(self):
        if self.thread is None:
            self.start()
        if self.ready.wait(2.0) and self.hwnd:
            user32.PostMessageW.restype = ctypes.c_bool
            user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                            wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW(self.hwnd, WM_SETTINGS_SHOW, 0, 0)

    def open_capture(self, target="voice"):
        if self.thread is None:
            self.start()
        if self.ready.wait(2.0) and self.hwnd:
            user32.PostMessageW.restype = ctypes.c_bool
            user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                            wintypes.WPARAM, wintypes.LPARAM]
            # 先显示窗口再开始捕获，消息在同一窗口队列里按序处理
            user32.PostMessageW(self.hwnd, WM_SETTINGS_SHOW, 0, 0)
            user32.PostMessageW(self.hwnd, WM_SETTINGS_CAPTURE,
                                2 if target == "smart" else 1, 0)

    def refresh(self):
        if self.hwnd:
            user32.PostMessageW.restype = ctypes.c_bool
            user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                            wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW(self.hwnd, WM_SETTINGS_REFRESH, 0, 0)

    def _s(self, v):
        return int(round(v * self._scale))

    def _outer_size(self, style, dpi, work=None):
        def measure():
            rect = wintypes.RECT(0, 0, self._s(SETTINGS_WIDTH), self._s(SETTINGS_HEIGHT))
            try:
                user32.AdjustWindowRectExForDpi(ctypes.byref(rect), style, False, 0, dpi)
            except Exception:
                pass
            return rect.right - rect.left, rect.bottom - rect.top

        w, h = measure()
        if work is not None:
            avail_w = max(320, work.right - work.left - 16)
            avail_h = max(240, work.bottom - work.top - 16)
            fit = min(1.0, avail_w / float(w), avail_h / float(h))
            if fit < 1.0:
                self._scale = max(0.5, self._scale * fit)
                w, h = measure()
        return w, h

    def _make_fonts(self):
        self._font = gdi32.CreateFontW(-self._s(15), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
                                       "Microsoft YaHei UI")
        self._font_small = gdi32.CreateFontW(-self._s(13), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5,
                                             0, "Microsoft YaHei UI")
        self._font_header = gdi32.CreateFontW(-self._s(14), 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5,
                                              0, "Microsoft YaHei UI")

    def _rebuild_controls(self):
        for hwnd in list(self._hwnds.values()):
            user32.DestroyWindow(hwnd)
        self._hwnds.clear()
        for font in (self._font, self._font_small, self._font_header):
            if font:
                gdi32.DeleteObject(ctypes.c_void_p(font))
        self._font = self._font_small = self._font_header = None
        self._make_fonts()
        self._build_controls()

    def _on_dpi_changed(self, wp, lp):
        dpi = wp & 0xFFFF
        if not dpi or not self.hwnd:
            return
        suggested = ctypes.cast(lp, ctypes.POINTER(wintypes.RECT)).contents
        hmon = user32.MonitorFromWindow(ctypes.c_void_p(self.hwnd), MONITOR_DEFAULTTONEAREST)
        work = _monitor_work_area(hmon)
        self._scale = max(1.0, dpi / 96.0)
        w, h = self._outer_size(self._style, dpi, work)
        x, y = suggested.left, suggested.top
        if work is not None:
            x = min(max(x, work.left), max(work.left, work.right - w))
            y = min(max(y, work.top), max(work.top, work.bottom - h))
        user32.SetWindowPos(ctypes.c_void_p(self.hwnd), None, x, y, w, h,
                            SWP_NOZORDER | SWP_NOACTIVATE)
        self._rebuild_controls()
        log(f"settings dpi change dpi={dpi} scale={self._scale:.2f} "
            f"size={w}x{h} pos={x},{y}")

    def _run(self):
        try:
            self._create()
        except Exception:
            log("settings create error " + traceback.format_exc().replace("\n", " | "))
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        user32.IsDialogMessageW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.MSG)]
        user32.IsDialogMessageW.restype = wintypes.BOOL
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if not user32.IsDialogMessageW(self.hwnd, ctypes.byref(msg)):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

    def _create(self):
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        hinst = kernel32.GetModuleHandleW(None)
        self._hinst = hinst
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        user32.SendMessageW.restype = LRESULT
        user32.SendMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.SetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.SetFocus.argtypes = [ctypes.c_void_p]
        user32.GetDpiForSystem.restype = ctypes.c_uint32
        user32.LoadCursorW.restype = ctypes.c_void_p
        user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.AdjustWindowRectExForDpi.argtypes = [ctypes.POINTER(wintypes.RECT),
                                                    wintypes.DWORD, wintypes.BOOL,
                                                    wintypes.DWORD, wintypes.UINT]
        user32.AdjustWindowRectExForDpi.restype = wintypes.BOOL
        gdi32.CreateFontW.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = ctypes.c_bool
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        user32.DestroyWindow.restype = ctypes.c_bool
        user32.EnableWindow.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        user32.EnableWindow.restype = wintypes.BOOL
        user32.GetSysColorBrush.restype = ctypes.c_void_p
        user32.GetSysColorBrush.argtypes = [ctypes.c_int]
        user32.GetSysColor.restype = wintypes.DWORD
        user32.GetSysColor.argtypes = [ctypes.c_int]
        self._bg_brush = user32.GetSysColorBrush(COLOR_BTNFACE)
        self._bg_color = user32.GetSysColor(COLOR_BTNFACE)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("style", ctypes.c_uint32),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                        ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", ctypes.c_void_p)]

        icon_path = icon_file()
        hicon = None
        if icon_path:
            hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON,
                                      user32.GetSystemMetrics(SM_CXICON) or 32,
                                      user32.GetSystemMetrics(SM_CYICON) or 32,
                                      LR_LOADFROMFILE)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.hIcon = hicon
        cls.hIconSm = hicon
        cls.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        cls.hbrBackground = ctypes.c_void_p(COLOR_BTNFACE_BRUSH)
        cls.lpszClassName = "DoubaoVoiceSettings"
        user32.RegisterClassExW.restype = ctypes.c_ushort
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW(ctypes.byref(cls))

        hmon = _cursor_monitor()
        work = _monitor_work_area(hmon)
        dpi = _monitor_dpi(hmon) or (user32.GetDpiForSystem() or 96)
        self._scale = max(1.0, dpi / 96.0)
        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_CLIPCHILDREN
        self._style = style
        w, h = self._outer_size(style, dpi, work)
        if work is not None:
            x = work.left + max(0, (work.right - work.left - w) // 2)
            y = work.top + max(0, (work.bottom - work.top - h) // 2)
        else:
            x = max(0, (user32.GetSystemMetrics(0) - w) // 2)
            y = max(0, (user32.GetSystemMetrics(1) - h) // 2)
        self.hwnd = user32.CreateWindowExW(WS_EX_CONTROLPARENT, "DoubaoVoiceSettings",
                                           f"豆包语音输入设置 v{APP_RELEASE}", style,
                                           x, y, w, h, None, None, hinst, None)
        log(f"settings hwnd={self.hwnd} dpi={dpi} scale={self._scale:.2f} "
            f"size={w}x{h} pos={x},{y}")
        if not self.hwnd:
            return
        if hicon:
            user32.SendMessageW(self.hwnd, WM_SETICON, 0, hicon)
            user32.SendMessageW(self.hwnd, WM_SETICON, 1, hicon)

        self._make_fonts()
        self._build_controls()

    def _mk(self, cls, text, style, x, y, w, h, cid, small=False, header=False):
        hwnd = user32.CreateWindowExW(0, cls, text,
                                      WS_CHILD | WS_VISIBLE | style,
                                      self._s(x), self._s(y), self._s(w), self._s(h),
                                      self.hwnd, ctypes.c_void_p(cid), self._hinst, None)
        font = self._font_header if header else (self._font_small if small else self._font)
        user32.SendMessageW(hwnd, WM_SETFONT, font or 0, 1)
        self._hwnds[cid] = hwnd
        return hwnd

    def _section(self, text, y, sid, lid, x=16, width=SETTINGS_WIDTH - 32):
        self._mk("STATIC", text, 0, x, y, width, 20, sid, header=True)
        self._mk("STATIC", "", SS_ETCHEDHORZ, x, y + 22, width, 2, lid)

    def _combo_add(self, cid, text):
        buf = ctypes.c_wchar_p(text)
        user32.SendMessageW(self._hwnds[cid], CB_ADDSTRING, 0,
                            ctypes.cast(buf, ctypes.c_void_p).value)

    def _combo_select(self, cid, index):
        user32.SendMessageW(self._hwnds[cid], CB_SETCURSEL, max(0, index), 0)

    def _combo_index(self, cid):
        return int(user32.SendMessageW(self._hwnds[cid], CB_GETCURSEL, 0, 0))

    def _check(self, cid, on):
        user32.SendMessageW(self._hwnds[cid], BM_SETCHECK, BST_CHECKED if on else 0, 0)

    def _checked(self, cid):
        return int(user32.SendMessageW(self._hwnds[cid], BM_GETCHECK, 0, 0)) == BST_CHECKED

    def _text(self, cid):
        hwnd = self._hwnds.get(cid)
        if not hwnd:
            return ""
        n = int(user32.GetWindowTextLengthW(hwnd))
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _set_hotword_hint(self, text):
        hwnd = self._hwnds.get(self.ID_HOTWORD_HINT)
        if hwnd:
            user32.SetWindowTextW(hwnd, text)

    def _build_controls(self):
        LX, RX, LW, RW = 16, 464, 400, 400
        self._section("语音输入方式", 12, 2201, 2211, LX, LW)
        self._mk("STATIC", "识别引擎", 0, LX, 48, 82, 20, self.ID_ENGINE_LABEL)
        combo = self._mk("COMBOBOX", "", CBS_DROPDOWNLIST | CBS_HASSTRINGS | WS_TABSTOP
                         | WS_VSCROLL, LX + 90, 44, 180, 200, self.ID_ENGINE)
        user32.SendMessageW(combo, CB_SETDROPPEDWIDTH, self._s(300), 0)
        self._mk("BUTTON", "测试全部引擎", WS_TABSTOP,
                 LX + 280, 40, 120, 28, self.ID_ENGINE_TEST)
        self._mk("BUTTON", "长按说话（松开结束）", BS_AUTORADIOBUTTON | WS_TABSTOP | WS_GROUP,
                 LX, 78, 190, 22, self.ID_MODE_HOLD)
        self._mk("STATIC", "最短按住", 0, LX, 106, 82, 20, self.ID_MIN_HOLD_LABEL)
        combo = self._mk("COMBOBOX", "", CBS_DROPDOWNLIST | CBS_HASSTRINGS | WS_TABSTOP
                         | WS_VSCROLL, LX + 90, 102, 110, 200, self.ID_MIN_HOLD)
        user32.SendMessageW(combo, CB_SETDROPPEDWIDTH, self._s(240), 0)
        self._mk("STATIC", "短于该时长自动忽略，不上传", 0,
                 LX + 210, 106, LW - 210, 18, self.ID_MIN_HOLD_HINT, small=True)
        self._mk("BUTTON", "免按模式（再按热键或任意键结束）", BS_AUTORADIOBUTTON | WS_TABSTOP,
                 LX, 136, 340, 22, self.ID_MODE_FREE)

        self._section("快捷键", 170, 2202, 2212, LX, LW)
        self._mk("STATIC", "", 0, LX, 204, 220, 20, self.ID_HOTKEY_LABEL)
        self._mk("BUTTON", "更改…", WS_TABSTOP, LX + LW - 100, 200, 86, 26, self.ID_HOTKEY)
        self._mk("STATIC", "可用 Ctrl/Alt/Shift 单键或组合、F1–F12、鼠标中键/侧键", 0,
                 LX, 228, LW, 18, self.ID_HOTKEY_HINT, small=True)
        self._mk("STATIC", "鼠标侧键被厂商驱动改成键盘宏时，会按键盘键识别", 0,
                 LX, 248, LW, 18, self.ID_HOTKEY_HINT2, small=True)

        self._section("热词替换", 286, self.ID_HOTWORD_SECTION,
                      self.ID_HOTWORD_LINE, LX, LW)
        self._mk("EDIT", "", ES_AUTOHSCROLL | WS_BORDER | WS_TABSTOP,
                 LX, 318, 142, 24, self.ID_HOTWORD_SRC)
        self._mk("STATIC", "→", 0, LX + 150, 321, 16, 20, self.ID_HOTWORD_ARROW)
        self._mk("EDIT", "", ES_AUTOHSCROLL | WS_BORDER | WS_TABSTOP,
                 LX + 170, 318, 138, 24, self.ID_HOTWORD_DST)
        self._mk("BUTTON", "添加", WS_TABSTOP,
                 LX + 314, 316, 70, 28, self.ID_HOTWORD_ADD)
        self._mk("BUTTON", "打开配置文件", WS_TABSTOP,
                 LX, 352, 120, 26, self.ID_HOTWORD_OPEN)
        self._mk("STATIC", "", 0, LX + 128, 356, LW - 128, 18,
                 self.ID_HOTWORD_HINT, small=True)

        self._section("麦克风", 12, 2203, 2213, RX, RW)
        self._mk("STATIC", "输入设备", 0, RX, 46, 64, 20, 2204)
        combo = self._mk("COMBOBOX", "", CBS_DROPDOWNLIST | CBS_HASSTRINGS | WS_TABSTOP
                         | WS_VSCROLL, RX + 84, 42, RW - 98, 220, self.ID_MIC)
        user32.SendMessageW(combo, CB_SETDROPPEDWIDTH, self._s(520), 0)
        self._mk("BUTTON", "麦克风预热（空闲时保持连接，不上传）", BS_AUTOCHECKBOX | WS_TABSTOP,
                 RX, 74, RW, 22, self.ID_MIC_ALWAYS)
        self._mk("STATIC", "空闲时不上传、不识别，仅缓存最近约 0.15 秒用于补上开口", 0,
                 RX, 98, RW, 18, self.ID_MIC_HINT, small=True)
        self._mk("STATIC", "关闭后仅在录音时打开；通话/蓝牙设备启动可能延迟几十毫秒", 0,
                 RX, 118, RW, 18, self.ID_MIC_HINT2, small=True)

        self._section("文字格式", 148, 2205, 2215, RX, RW)
        rows = (("标点展示", self.ID_PUNCT, 2206),
                ("数字、英文", self.ID_SPACE, 2207),
                ("分段方式", self.ID_PARAGRAPH, 2208))
        for i, (label, cid, _) in enumerate(rows):
            ry = 182 + i * 36
            self._mk("STATIC", label, 0, RX, ry + 4, 82, 20, 2209 + i)
            c = self._mk("COMBOBOX", "", CBS_DROPDOWNLIST | CBS_HASSTRINGS | WS_TABSTOP
                         | WS_VSCROLL, RX + 90, ry, RW - 104, 200, cid)
            user32.SendMessageW(c, CB_SETDROPPEDWIDTH, self._s(280), 0)
        self._mk("STATIC", "格式在下次识别时生效；数字/英文空格由识别服务处理", 0,
                 RX, 286, RW, 18, self.ID_FORMAT_HINT, small=True)

        if SMART_AVAILABLE:
            self._section("智能文字整理", 316, 2216, 2217, RX, RW)
            self._mk("BUTTON", "语音输入后自动排版、纠错、润色", BS_AUTOCHECKBOX | WS_TABSTOP,
                     RX, 346, RW, 22, self.ID_SMART)
            self._mk("STATIC", "", 0, RX, 376, 220, 20, self.ID_SMART_HOTKEY_LABEL)
            self._mk("BUTTON", "设置…", WS_TABSTOP,
                     RX + RW - 172, 372, 80, 26, self.ID_SMART_HOTKEY)
            self._mk("BUTTON", "清除", WS_TABSTOP,
                     RX + RW - 86, 372, 78, 26, self.ID_SMART_HOTKEY_CLEAR)
            self._mk("STATIC", "选中文字后按整理快捷键，至少 5 字；结果替换选区，Ctrl+Z 恢复", 0,
                     RX, 404, RW, 18, self.ID_SMART_HINT, small=True)
            self._mk("STATIC", "文字会发送至豆包服务处理", 0,
                     RX, 422, RW, 18, self.ID_SMART_HINT2, small=True)
            bottom = 478
        else:
            bottom = 426
        self._mk("BUTTON", "显示悬浮按钮（单击说话，可拖动，右键隐藏）",
                 BS_AUTOCHECKBOX | WS_TABSTOP, LX, bottom - 32, 440, 22, self.ID_FLOAT)
        self._mk("BUTTON", "开机自启", BS_AUTOCHECKBOX | WS_TABSTOP,
                 LX, bottom, 160, 22, self.ID_AUTOSTART)
        self._mk("BUTTON", "关闭", WS_TABSTOP, RX + RW - 88, bottom - 4, 88, 30, self.ID_CLOSE)

        for cid, items in ((self.ID_ENGINE, ENGINE_ITEMS),
                           (self.ID_MIN_HOLD, self.MIN_HOLD_ITEMS),
                           (self.ID_PUNCT, self.PUNCT_ITEMS),
                           (self.ID_SPACE, self.SPACE_ITEMS),
                           (self.ID_PARAGRAPH, self.PARA_ITEMS)):
            for item in items:
                self._combo_add(cid, item[1])
        self.mic_items = list_input_devices()
        self._combo_add(self.ID_MIC, "自动检测（系统默认）")
        for item in self.mic_items:
            self._combo_add(self.ID_MIC, f"{item['name']}  [{item['hostapi']}]")
        self._refresh_controls()

    def _refresh_controls(self):
        if not self.hwnd or self.ID_HOTKEY_LABEL not in self._hwnds:
            return
        app = self.app
        self._check(self.ID_MODE_HOLD, app.voice_mode != "hands_free")
        self._check(self.ID_MODE_FREE, app.voice_mode == "hands_free")
        user32.SetWindowTextW(self._hwnds[self.ID_HOTKEY_LABEL],
                              f"当前：{app.hotkey.display()}")
        self._check(self.ID_MIC_ALWAYS, app.mic_mode == "always")
        if self.ID_SMART in self._hwnds:
            self._check(self.ID_SMART, app.smart_text)
            user32.SetWindowTextW(self._hwnds[self.ID_SMART_HOTKEY_LABEL],
                                  f"整理快捷键：{app.smart_hotkey_display()}")
            user32.EnableWindow(self._hwnds[self.ID_SMART_HOTKEY_CLEAR],
                                bool(app.smart_hotkey))
        self._check(self.ID_AUTOSTART, autostart_enabled())
        self._check(self.ID_FLOAT, app.float_visible)
        mh = app.min_hold_ms if app.min_hold_ms in MIN_HOLD_MS else MIN_HOLD_DEFAULT
        self._combo_select(self.ID_MIN_HOLD, MIN_HOLD_MS.index(mh))
        self._combo_select(self.ID_PUNCT, PUNCT_MODES.index(app.punctuation))
        self._combo_select(self.ID_SPACE, 0 if app.zh_space else 1)
        self._combo_select(self.ID_PARAGRAPH, PARAGRAPH_MODES.index(app.paragraph_mode))
        engine_index = 0
        for i, (key, _) in enumerate(ENGINE_ITEMS):
            if key == app.engine_kind:
                engine_index = i
                break
        self._combo_select(self.ID_ENGINE, engine_index)
        local = app.engine_kind == "doubao"
        user32.EnableWindow(self._hwnds[self.ID_SPACE], local)
        user32.SetWindowTextW(self._hwnds[self.ID_FORMAT_HINT],
                              "格式在下次识别时生效；数字/英文空格由识别服务处理" if local
                              else "格式在下次识别时生效；云端引擎不提供空格选项")
        mi = 0
        if app.mic_sel:
            for i, item in enumerate(self.mic_items):
                if (item["name"] == app.mic_sel.get("name")
                        and item["hostapi"] == app.mic_sel.get("hostapi")):
                    mi = i + 1
                    break
        self._combo_select(self.ID_MIC, mi)
        if self.ID_HOTWORD_HINT in self._hwnds:
            self._set_hotword_hint(
                f"共 {len(load_hotwords())} 条；每行一条：原文 -> 替换为")

    def _on_command(self, wp):
        cid = wp & 0xFFFF
        code = (wp >> 16) & 0xFFFF
        app = self.app
        if cid == 2:
            app.capture_cancel()
            user32.ShowWindow(self.hwnd, SW_HIDE)
            return
        if cid == self.ID_CLOSE and code == BN_CLICKED:
            app.capture_cancel()
            user32.ShowWindow(self.hwnd, SW_HIDE)
        elif cid == self.ID_HOTKEY and code == BN_CLICKED:
            app.start_capture()
        elif cid == self.ID_SMART_HOTKEY and code == BN_CLICKED:
            app.start_capture("smart")
        elif cid == self.ID_SMART_HOTKEY_CLEAR and code == BN_CLICKED:
            app.clear_smart_hotkey()
            self.refresh()
        elif cid == self.ID_MODE_HOLD and code == BN_CLICKED:
            app.set_voice_mode("hold")
        elif cid == self.ID_MODE_FREE and code == BN_CLICKED:
            app.set_voice_mode("hands_free")
        elif cid == self.ID_ENGINE and code == CBN_SELCHANGE:
            idx = self._combo_index(self.ID_ENGINE)
            if 0 <= idx < len(ENGINE_ITEMS):
                app.set_asr_engine(ENGINE_ITEMS[idx][0])
                self.refresh()
        elif cid == self.ID_ENGINE_TEST and code == BN_CLICKED:
            app.open_engine_test()
        elif cid == self.ID_MIC_ALWAYS and code == BN_CLICKED:
            want = "always" if self._checked(self.ID_MIC_ALWAYS) else "press"
            if not app.set_mic_mode(want):
                self.refresh()
        elif cid == self.ID_AUTOSTART and code == BN_CLICKED:
            if not app.set_autostart_enabled(self._checked(self.ID_AUTOSTART)):
                self.refresh()
        elif cid == self.ID_FLOAT and code == BN_CLICKED:
            app.set_float_visible(self._checked(self.ID_FLOAT))
        elif cid == self.ID_SMART and code == BN_CLICKED:
            if not app.set_smart_text(self._checked(self.ID_SMART)):
                self.refresh()
        elif cid == self.ID_HOTWORD_ADD and code == BN_CLICKED:
            src = self._text(self.ID_HOTWORD_SRC).strip()
            dst = self._text(self.ID_HOTWORD_DST).strip()
            if not src or not dst:
                self._set_hotword_hint("请填写原文和替换为")
            elif not add_hotword(src, dst):
                self._set_hotword_hint("原文不能以 # 开头或包含箭头")
            else:
                user32.SetWindowTextW(self._hwnds[self.ID_HOTWORD_SRC], "")
                user32.SetWindowTextW(self._hwnds[self.ID_HOTWORD_DST], "")
                user32.SetFocus(self._hwnds[self.ID_HOTWORD_SRC])
                self._set_hotword_hint(f"已添加（共 {len(load_hotwords())} 条）")
        elif cid == self.ID_HOTWORD_OPEN and code == BN_CLICKED:
            if not open_hotwords_file():
                self._set_hotword_hint("打开失败，请手动打开 hotwords.txt")
        elif cid == self.ID_MIC and code == CBN_SELCHANGE:
            idx = self._combo_index(self.ID_MIC)
            sel = None
            if 1 <= idx <= len(self.mic_items):
                item = self.mic_items[idx - 1]
                sel = {"name": item["name"], "hostapi": item["hostapi"]}
            if not app.set_mic_selection(sel):
                self.refresh()
        elif cid == self.ID_MIN_HOLD and code == CBN_SELCHANGE:
            idx = self._combo_index(self.ID_MIN_HOLD)
            if 0 <= idx < len(MIN_HOLD_MS):
                app.set_min_hold(MIN_HOLD_MS[idx])
        elif cid == self.ID_PUNCT and code == CBN_SELCHANGE:
            idx = self._combo_index(self.ID_PUNCT)
            if 0 <= idx < len(PUNCT_MODES):
                app.set_punctuation(PUNCT_MODES[idx])
        elif cid == self.ID_SPACE and code == CBN_SELCHANGE:
            idx = self._combo_index(self.ID_SPACE)
            if 0 <= idx < len(self.SPACE_ITEMS):
                app.set_zh_space(self.SPACE_ITEMS[idx][0])
        elif cid == self.ID_PARAGRAPH and code == CBN_SELCHANGE:
            idx = self._combo_index(self.ID_PARAGRAPH)
            if 0 <= idx < len(PARAGRAPH_MODES):
                app.set_paragraph_mode(PARAGRAPH_MODES[idx])

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_COMMAND:
                self._on_command(wp)
                return 0
            if msg == WM_SETTINGS_SHOW:
                self.app.capture_cancel()
                self._refresh_controls()
                user32.ShowWindow(self.hwnd, SW_SHOWNORMAL)
                user32.SetForegroundWindow(self.hwnd)
                user32.SetFocus(self._hwnds.get(self.ID_MODE_HOLD) or self.hwnd)
                return 0
            if msg == WM_SETTINGS_CAPTURE:
                self.app.start_capture("smart" if wp == 2 else "voice")
                return 0
            if msg == WM_SETTINGS_REFRESH:
                self._refresh_controls()
                return 0
            if msg == WM_DPICHANGED:
                self._on_dpi_changed(wp, lp)
                return 0
            if msg == WM_CLOSE:
                self.app.capture_cancel()
                user32.ShowWindow(self.hwnd, SW_HIDE)
                return 0
            if msg in (WM_CTLCOLORSTATIC, WM_CTLCOLORBTN):
                gdi32.SetBkColor(wp, self._bg_color)
                gdi32.SetBkMode(wp, 1)
                return self._bg_brush or COLOR_BTNFACE_BRUSH
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log("settings wndproc error " + traceback.format_exc().replace("\n", " | "))
        return user32.DefWindowProcW(hwnd, msg, wp, lp)


# ----------------------------------------------------------------- engine test

class EngineTest:
    ID_RECORD = 2301
    ID_CLOSE = 2302
    ID_HINT = 2303
    ID_TITLE = 2304
    ID_SUBTITLE = 2305
    ROW_NAME = 2310
    ROW_STATUS = 2320
    ROW_TEXT = 2330
    TIMER_ID = 1
    _class_seq = 0

    def __init__(self, app, owner=None, activate=True):
        self.app = app
        self.owner = owner
        self.activate = activate
        EngineTest._class_seq += 1
        self._class_name = f"DoubaoVoiceEngineTest{EngineTest._class_seq}"
        self.hwnd = None
        self.thread = None
        self.ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)
        self._hwnds = {}
        self._ui_cache = {}
        self._font = None
        self._font_small = None
        self._font_header = None
        self._hinst = None
        self._scale = 1.0
        self._style = 0
        self._bg_brush = None
        self._bg_color = 0
        self._lock = threading.Lock()
        self._closed = False
        self._engines = None
        self._sessions = {}
        self._status = {}
        self._text = {}
        self._times = {}
        self._errors = {}
        self._runner_done = {}
        self._pending = set()
        self._state = "idle"
        self._rec_state = None
        self._press_t = 0.0
        self._ignore_click = False
        self._ignore_t = 0.0
        self._record_t = 0.0
        self._stop_t = 0.0
        self._stop_evt = None
        self._fanout = None
        self._mic_opened = False
        self._fastest = None
        self._slowest = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def focus(self):
        if self.hwnd:
            user32.ShowWindow(ctypes.c_void_p(self.hwnd), SW_SHOWNORMAL)
            user32.SetForegroundWindow(ctypes.c_void_p(self.hwnd))

    def _s(self, v):
        return int(round(v * self._scale))

    def _outer_size(self, style, dpi, work=None):
        def measure():
            rect = wintypes.RECT(0, 0, self._s(ENGINE_TEST_WIDTH),
                                 self._s(ENGINE_TEST_HEIGHT))
            try:
                user32.AdjustWindowRectExForDpi(ctypes.byref(rect), style, False, 0, dpi)
            except Exception:
                pass
            return rect.right - rect.left, rect.bottom - rect.top

        w, h = measure()
        if work is not None:
            avail_w = max(320, work.right - work.left - 16)
            avail_h = max(240, work.bottom - work.top - 16)
            fit = min(1.0, avail_w / float(w), avail_h / float(h))
            if fit < 1.0:
                self._scale = max(0.5, self._scale * fit)
                w, h = measure()
        return w, h

    def _make_fonts(self):
        self._font = gdi32.CreateFontW(-self._s(15), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0,
                                       "Microsoft YaHei UI")
        self._font_small = gdi32.CreateFontW(-self._s(13), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5,
                                             0, "Microsoft YaHei UI")
        self._font_header = gdi32.CreateFontW(-self._s(14), 0, 0, 0, 700, 0, 0, 0, 1, 0, 0, 5,
                                              0, "Microsoft YaHei UI")

    def _rebuild_controls(self):
        for hwnd in list(self._hwnds.values()):
            user32.DestroyWindow(ctypes.c_void_p(hwnd))
        self._hwnds.clear()
        self._ui_cache.clear()
        for font in (self._font, self._font_small, self._font_header):
            if font:
                gdi32.DeleteObject(ctypes.c_void_p(font))
        self._font = self._font_small = self._font_header = None
        self._make_fonts()
        self._build_controls()

    def _on_dpi_changed(self, wp, lp):
        dpi = wp & 0xFFFF
        if not dpi or not self.hwnd:
            return
        suggested = ctypes.cast(lp, ctypes.POINTER(wintypes.RECT)).contents
        hmon = user32.MonitorFromWindow(ctypes.c_void_p(self.hwnd), MONITOR_DEFAULTTONEAREST)
        work = _monitor_work_area(hmon)
        self._scale = max(1.0, dpi / 96.0)
        w, h = self._outer_size(self._style, dpi, work)
        x, y = suggested.left, suggested.top
        if work is not None:
            x = min(max(x, work.left), max(work.left, work.right - w))
            y = min(max(y, work.top), max(work.top, work.bottom - h))
        user32.SetWindowPos(ctypes.c_void_p(self.hwnd), None, x, y, w, h,
                            SWP_NOZORDER | SWP_NOACTIVATE)
        self._rebuild_controls()
        log(f"engine test dpi change dpi={dpi} scale={self._scale:.2f} "
            f"size={w}x{h} pos={x},{y}")

    def _run(self):
        try:
            self._create()
        except Exception:
            log("engine test create error " + traceback.format_exc().replace("\n", " | "))
            self.app.testing = False
            self.app.engine_test = None
            self.ready.set()
            return
        if not self.hwnd:
            log("engine test create failed: no hwnd")
            if self.app.engine_test is self:
                self.app.engine_test = None
                self.app.testing = False
            self.ready.set()
            return
        self.ready.set()
        user32.IsDialogMessageW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.MSG)]
        user32.IsDialogMessageW.restype = wintypes.BOOL
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if not user32.IsDialogMessageW(ctypes.c_void_p(self.hwnd), ctypes.byref(msg)):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        self.ready.clear()
        self.hwnd = None
        self.thread = None
        if self.app.engine_test is self or self.app.engine_test is None:
            self.app.engine_test = None
            self.app.testing = False

    def _create(self):
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        hinst = kernel32.GetModuleHandleW(None)
        self._hinst = hinst
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint32, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        user32.SendMessageW.restype = LRESULT
        user32.SendMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.SetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        user32.SetWindowTextW.restype = wintypes.BOOL
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.EnableWindow.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        user32.EnableWindow.restype = wintypes.BOOL
        user32.LoadCursorW.restype = ctypes.c_void_p
        user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetDpiForSystem.restype = ctypes.c_uint32
        user32.SetTimer.restype = ctypes.c_size_t
        user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32,
                                    ctypes.c_void_p]
        user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        user32.KillTimer.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.AdjustWindowRectExForDpi.argtypes = [ctypes.POINTER(wintypes.RECT),
                                                    wintypes.DWORD, wintypes.BOOL,
                                                    wintypes.DWORD, wintypes.UINT]
        user32.AdjustWindowRectExForDpi.restype = wintypes.BOOL
        gdi32.CreateFontW.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = ctypes.c_bool
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        user32.DestroyWindow.restype = ctypes.c_bool
        user32.GetSysColorBrush.restype = ctypes.c_void_p
        user32.GetSysColorBrush.argtypes = [ctypes.c_int]
        user32.GetSysColor.restype = wintypes.DWORD
        user32.GetSysColor.argtypes = [ctypes.c_int]
        self._bg_brush = user32.GetSysColorBrush(COLOR_BTNFACE)
        self._bg_color = user32.GetSysColor(COLOR_BTNFACE)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint32), ("style", ctypes.c_uint32),
                        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                        ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", ctypes.c_void_p)]

        icon_path = icon_file()
        hicon = None
        if icon_path:
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT,
                                          ctypes.c_int, ctypes.c_int, wintypes.UINT]
            hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON,
                                      user32.GetSystemMetrics(SM_CXICON) or 32,
                                      user32.GetSystemMetrics(SM_CYICON) or 32,
                                      LR_LOADFROMFILE)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.hIcon = hicon
        cls.hIconSm = hicon
        cls.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        cls.hbrBackground = ctypes.c_void_p(COLOR_BTNFACE_BRUSH)
        cls.lpszClassName = self._class_name
        user32.RegisterClassExW.restype = ctypes.c_ushort
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.RegisterClassExW(ctypes.byref(cls))

        hmon = _cursor_monitor()
        work = _monitor_work_area(hmon)
        dpi = _monitor_dpi(hmon) or (user32.GetDpiForSystem() or 96)
        self._scale = max(1.0, dpi / 96.0)
        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_CLIPCHILDREN
        self._style = style
        w, h = self._outer_size(style, dpi, work)
        if work is not None:
            x = work.left + max(0, (work.right - work.left - w) // 2)
            y = work.top + max(0, (work.bottom - work.top - h) // 2)
        else:
            x = max(0, (user32.GetSystemMetrics(0) - w) // 2)
            y = max(0, (user32.GetSystemMetrics(1) - h) // 2)
        ex = WS_EX_CONTROLPARENT | 0x00000080  # WS_EX_TOOLWINDOW
        self.hwnd = user32.CreateWindowExW(ex, self._class_name, "测试全部引擎",
                                           style, x, y, w, h,
                                           ctypes.c_void_p(self.owner or 0), None, hinst, None)
        log(f"engine test hwnd={self.hwnd} dpi={dpi} scale={self._scale:.2f} "
            f"size={w}x{h} pos={x},{y}")
        if not self.hwnd:
            return
        if hicon:
            user32.SendMessageW(self.hwnd, WM_SETICON, 0, hicon)
            user32.SendMessageW(self.hwnd, WM_SETICON, 1, hicon)

        self.app.testing = True
        if self.owner:
            user32.EnableWindow(ctypes.c_void_p(self.owner), False)
        self._make_fonts()
        self._build_controls()
        user32.SetTimer(ctypes.c_void_p(self.hwnd), self.TIMER_ID, 120, None)
        threading.Thread(target=self._prepare_engines, daemon=True).start()
        if self.activate:
            user32.ShowWindow(ctypes.c_void_p(self.hwnd), SW_SHOWNORMAL)
            user32.SetForegroundWindow(ctypes.c_void_p(self.hwnd))
        else:
            user32.ShowWindow(ctypes.c_void_p(self.hwnd), 4)  # SW_SHOWNOACTIVATE

    def _mk(self, cls, text, style, x, y, w, h, cid, small=False, header=False):
        hwnd = user32.CreateWindowExW(0, cls, text,
                                      WS_CHILD | WS_VISIBLE | style,
                                      self._s(x), self._s(y), self._s(w), self._s(h),
                                      ctypes.c_void_p(self.hwnd), ctypes.c_void_p(cid),
                                      self._hinst, None)
        font = self._font_header if header else (self._font_small if small else self._font)
        user32.SendMessageW(hwnd, WM_SETFONT, font or 0, 1)
        self._hwnds[cid] = hwnd
        return hwnd

    def _build_controls(self):
        self._mk("STATIC", "同一段录音，六个引擎同时识别", 0, 16, 10, 688, 20,
                 self.ID_TITLE, header=True)
        self._mk("STATIC", "计时从停止说话开始；最快/最慢只统计有结果的引擎", 0,
                 16, 32, 688, 18, self.ID_SUBTITLE, small=True)
        for i, kind in enumerate(ASR_ENGINES):
            top = 56 + i * 58
            self._mk("STATIC", "", 0, 16, top, 190, 20, self.ROW_NAME + i, header=True)
            self._mk("STATIC", "", SS_RIGHT, 430, top + 2, 274, 18,
                     self.ROW_STATUS + i, small=True)
            self._mk("EDIT", "", ES_READONLY | ES_AUTOHSCROLL | WS_BORDER,
                     16, top + 22, 688, 24, self.ROW_TEXT + i)
        self._mk("BUTTON", "按住说话 / 点击开始", WS_TABSTOP,
                 16, 406, 220, 38, self.ID_RECORD)
        self._mk("STATIC", "按住松开即结束；点击开始，再点一次结束", 0,
                 248, 416, 360, 18, self.ID_HINT, small=True)
        self._mk("BUTTON", "关闭", WS_TABSTOP, 616, 410, 88, 30, self.ID_CLOSE)
        self._tick()

    def _set_text(self, cid, text):
        text = text or ""
        if self._ui_cache.get(cid) == text:
            return
        hwnd = self._hwnds.get(cid)
        if hwnd:
            user32.SetWindowTextW(ctypes.c_void_p(hwnd), text)
            self._ui_cache[cid] = text

    def _tick(self):
        with self._lock:
            state = self._state
            status = dict(self._status)
            times = dict(self._times)
            texts = dict(self._text)
            errors = dict(self._errors)
            fastest = self._fastest
            slowest = self._slowest
            stop_t = self._stop_t
            rec_state = self._rec_state
            engine_ready = self._engines is not None
        now = time.monotonic()
        for i, kind in enumerate(ASR_ENGINES):
            if kind in times:
                t = times[kind]
                body = (texts.get(kind) or "").strip()
                if not body:
                    state_text = f"无结果 {t:.2f} s"
                else:
                    state_text = f"✓ {t:.2f} s"
                    if kind == fastest:
                        state_text += " · 最快"
                    elif kind == slowest:
                        state_text += " · 最慢"
            elif errors.get(kind):
                state_text = errors[kind]
            elif state == "recording":
                state_text = ("录音中" if status.get(kind) == "started"
                              else "连接中…")
            elif state == "finishing":
                state_text = (f"识别中 {now - stop_t:.1f} s" if stop_t
                              else "识别中…")
            elif state == "closing":
                state_text = "正在关闭…"
            elif engine_ready:
                state_text = "等待开始"
            else:
                state_text = "准备中…"
            self._set_text(self.ROW_NAME + i, ENGINE_LABELS[kind])
            self._set_text(self.ROW_STATUS + i, state_text)
            self._set_text(self.ROW_TEXT + i, texts.get(kind, ""))
        if state == "recording":
            btn = ("● 录音中… 点击结束" if rec_state == "tap"
                   else "● 录音中… 松开结束")
        elif state == "finishing":
            btn = "识别中…"
        elif not engine_ready:
            btn = "正在准备引擎…"
        else:
            btn = "按住说话 / 点击开始"
        self._set_text(self.ID_RECORD, btn)
        btn_hwnd = self._hwnds.get(self.ID_RECORD)
        if btn_hwnd:
            user32.EnableWindow(ctypes.c_void_p(btn_hwnd),
                                state in ("idle", "recording") and engine_ready)

    def _prepare_engines(self):
        engines = {}
        for kind in ASR_ENGINES:
            try:
                if kind == "doubao":
                    eng = self.app.test_local_engine()
                else:
                    eng = create_engine(kind)
                    if hasattr(eng, "use_frontier"):
                        eng.use_frontier = self.app.doubao_quic
                eng.set_extra(self.app._engine_extra())
                engines[kind] = eng
            except Exception:
                log(f"engine test {kind} prepare error "
                    + traceback.format_exc().replace("\n", " | "))
        with self._lock:
            self._engines = engines
        local = engines.get("doubao")
        if local is not None and not local.ready.is_set():
            try:
                local.warmup()
            except Exception:
                log("engine test local warmup error "
                    + traceback.format_exc().replace("\n", " | "))

    def _on_text(self, kind, text):
        if not text:
            return
        with self._lock:
            if self._closed or kind in self._times:
                return
            self._text[kind] = text
            self._status[kind] = "started"

    def _record_down(self):
        if self._closed:
            return
        with self._lock:
            state = self._state
            rec_state = self._rec_state
        if state == "idle":
            self._start_record()
            with self._lock:
                if self._state == "recording":
                    self._rec_state = "hold"
                    self._press_t = time.monotonic()
        elif state == "recording" and rec_state == "tap":
            self._ignore_click = True
            self._ignore_t = time.monotonic()
            self._stop_record()

    def _record_click(self):
        now = time.monotonic()
        if self._ignore_click:
            if now - self._ignore_t < 0.6:
                self._ignore_click = False
                return
            self._ignore_click = False
        with self._lock:
            state = self._state
            rec_state = self._rec_state
            press_t = self._press_t
        if state == "idle":
            self._start_record()
            with self._lock:
                if self._state == "recording":
                    self._rec_state = "tap"
            return
        if state != "recording":
            return
        if rec_state == "hold":
            if now - press_t < 0.35:
                with self._lock:
                    self._rec_state = "tap"
            else:
                self._stop_record()
        elif rec_state == "tap":
            self._stop_record()

    def _start_record(self):
        with self._lock:
            if self._state != "idle" or self._closed or self._engines is None:
                return
            with self.app.lock:
                if self.app.session is not None:
                    return
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
            self._rec_state = None
            self._ignore_click = False
            self._ignore_t = 0.0
            engines = dict(self._engines)
        sessions = {}
        for kind in ASR_ENGINES:
            eng = engines.get(kind)
            if eng is None:
                self._set_error(kind, "引擎不可用")
                continue
            try:
                sess = eng.new_session(
                    False, on_text=lambda text, k=kind: self._on_text(k, text))
            except Exception:
                log(f"engine test {kind} session error "
                    + traceback.format_exc().replace("\n", " | "))
                self._set_error(kind, "创建会话失败")
                continue
            sessions[kind] = sess
            with self._lock:
                self._status[kind] = "connecting"
        if not sessions:
            with self._lock:
                self._state = "idle"
            return
        fanout = MicFanout(sessions.values())
        mic = self.app.mic
        opened = False
        try:
            if mic.stream is None:
                mic.open()
                opened = True
            mic.attach(fanout)
        except Exception:
            log("engine test mic error " + traceback.format_exc().replace("\n", " | "))
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
            return
        with self._lock:
            self._sessions = sessions
            self._fanout = fanout
            self._mic_opened = opened
        for kind, sess in sessions.items():
            threading.Thread(target=self._run_one,
                             args=(kind, sess, engines[kind]), daemon=True).start()

    def _run_one(self, kind, sess, engine):
        try:
            if not engine.ready.wait(20.0):
                self._set_error(kind, "引擎未就绪")
                return
            with self._lock:
                closed = self._closed
                stop_evt = self._stop_evt
            if closed:
                sess.cancel()
                return
            sess.start()
            with self._lock:
                if kind not in self._times:
                    self._status[kind] = "started"
            if stop_evt is not None:
                stop_evt.wait(600.0)
            with self._lock:
                closed = self._closed
                t0 = self._stop_t
            if closed:
                sess.cancel()
                return
            sess.request_release()
            sess.finish()
            elapsed = max(0.0, time.monotonic() - t0)
            text = (sess.text or "").strip()
            self._set_result(kind, elapsed, text)
        except Exception:
            log(f"engine test {kind} error " + traceback.format_exc().replace("\n", " | "))
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
            if self._closed:
                return
            self._times[kind] = elapsed
            self._text[kind] = text
            self._status[kind] = "done"
        log(f"engine test {kind} {elapsed:.2f}s text={text!r}")

    def _runner_finished(self, kind):
        with self._lock:
            self._runner_done[kind] = True
            if self._state != "finishing":
                return
            self._pending.discard(kind)
            if self._pending:
                return
        self._complete_test()

    def _stop_record(self):
        with self._lock:
            if self._state != "recording":
                return
            self._state = "finishing"
            self._stop_t = time.monotonic()
            stop_evt = self._stop_evt
            pending = {k for k in self._sessions
                       if not self._runner_done.get(k)}
            self._pending = pending
            fanout = self._fanout
            self._fanout = None
            self._mic_opened = False
            self._rec_state = None
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
            self._complete_test()

    def _complete_test(self):
        with self._lock:
            if self._state != "finishing":
                return
            ok = [(k, self._times[k]) for k in self._times
                  if (self._text.get(k) or "").strip()]
            self._fastest = None
            self._slowest = None
            if len(ok) >= 2:
                fastest = min(ok, key=lambda item: item[1])[0]
                slowest = max(ok, key=lambda item: item[1])[0]
                if fastest != slowest:
                    self._fastest = fastest
                    self._slowest = slowest
            self._state = "idle"
            self._rec_state = None
            self._pending = set()
            times = dict(self._times)
        log(f"engine test done fastest={self._fastest} slowest={self._slowest} "
            f"times={times}")

    def _shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._state = "closing"
            stop_evt = self._stop_evt
            sessions = list(self._sessions.values())
            fanout = self._fanout
            self._fanout = None
            self._mic_opened = False
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

    def _on_command(self, wp):
        cid = wp & 0xFFFF
        code = (wp >> 16) & 0xFFFF
        if cid == self.ID_RECORD and code == BN_CLICKED:
            self._record_click()
        elif cid == self.ID_CLOSE and code == BN_CLICKED:
            user32.PostMessageW(ctypes.c_void_p(self.hwnd), WM_CLOSE, 0, 0)

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_COMMAND:
                self._on_command(wp)
                return 0
            if msg == WM_PARENTNOTIFY:
                if (int(wp) & 0xFFFF) == WM_LBUTTONDOWN:
                    if int(lp) == self._hwnds.get(self.ID_RECORD):
                        self._record_down()
                return 0
            if msg == WM_TIMER_MSG:
                if int(wp) == self.TIMER_ID:
                    self._update_hold_state()
                    self._tick()
                return 0
            if msg == WM_DPICHANGED:
                self._on_dpi_changed(wp, lp)
                return 0
            if msg == WM_CLOSE:
                self._shutdown()
                user32.KillTimer(ctypes.c_void_p(self.hwnd), self.TIMER_ID)
                if self.owner:
                    user32.EnableWindow(ctypes.c_void_p(self.owner), True)
                    user32.SetForegroundWindow(ctypes.c_void_p(self.owner))
                user32.DestroyWindow(ctypes.c_void_p(self.hwnd))
                return 0
            if msg in (WM_CTLCOLORSTATIC, WM_CTLCOLORBTN):
                gdi32.SetBkColor(wp, self._bg_color)
                gdi32.SetBkMode(wp, 1)
                return self._bg_brush or COLOR_BTNFACE_BRUSH
            if msg == WM_DESTROY:
                user32.KillTimer(ctypes.c_void_p(self.hwnd), self.TIMER_ID)
                if self.app.engine_test is self or self.app.engine_test is None:
                    self.app.engine_test = None
                    self.app.testing = False
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log("engine test wndproc error "
                + traceback.format_exc().replace("\n", " | "))
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _update_hold_state(self):
        with self._lock:
            if self._state != "recording" or self._rec_state != "hold":
                return
            press_t = self._press_t
        if user32.GetAsyncKeyState(0x01) & 0x8000:
            return
        now = time.monotonic()
        if now - press_t < 0.35:
            with self._lock:
                self._rec_state = "tap"
            self._ignore_click = True
            self._ignore_t = now
        else:
            self._ignore_click = True
            self._ignore_t = now
            self._stop_record()


# ---------------------------------------------------------------------- app

class App:
    def __init__(self, args):
        self.args = args
        cfg = load_config()
        kind = getattr(args, "engine", None)
        if kind not in ASR_ENGINES:
            kind = cfg.get("asr_engine")
        self.engine_kind = kind if kind in ASR_ENGINES else "doubao"
        self.local_engine = None
        self.engine = self._make_engine(self.engine_kind)
        self.doubao_quic = cfg.get("doubao_quic") is True
        if hasattr(self.engine, "use_frontier"):
            self.engine.use_frontier = self.doubao_quic
        self.ui = UiState()
        self.lock = threading.Lock()
        self.session = None
        self.once_done = threading.Event()
        self.accept_live = False
        self.capturing = False
        self.capture_gen = 0
        self.tray = None
        self.settings = None
        self.float_btn = None
        self.float_visible = cfg.get("float_visible") is True
        self.engine_test = None
        self.testing = False
        self.voice_mode = (cfg.get("voice_mode") if cfg.get("voice_mode") in VOICE_MODES
                           else "hold")
        mh = cfg.get("min_hold_ms")
        self.min_hold_ms = (mh if isinstance(mh, int) and not isinstance(mh, bool)
                            and mh in MIN_HOLD_MS else MIN_HOLD_DEFAULT)
        self.punctuation = (cfg.get("punctuation_mode")
                            if cfg.get("punctuation_mode") in PUNCT_MODES else "keep_all")
        self.paragraph_mode = (cfg.get("paragraph_mode")
                               if cfg.get("paragraph_mode") in PARAGRAPH_MODES else "none")
        self.zh_space = cfg.get("zh_space") is not False
        sel = cfg.get("mic_device")
        self.mic_sel = sel if isinstance(sel, dict) and sel.get("name") else None
        dev = args.device if args.device is not None else resolve_mic_device(self.mic_sel)
        if self.mic_sel and dev is None:
            log(f"configured mic not found: {self.mic_sel}")
        self.mic = Mic(dev)
        self.engine.set_extra(self._engine_extra())
        spec = cfg.get("hotkey") or DEFAULT_HOTKEY
        parts = _split_hotkey(spec)
        if len(parts) == 1 and parts[0] in _LEFT_MOD_TOKENS:
            log(f"hotkey {spec} is a left modifier, reset to {DEFAULT_HOTKEY}")
            spec = DEFAULT_HOTKEY
            cfg["hotkey"] = spec
            save_config(cfg)
        voice_combo = parse_hotkey(spec)
        smart_spec = cfg.get("smart_hotkey")
        if not isinstance(smart_spec, str):
            smart_spec = ""
        smart_combo = parse_smart_hotkey(smart_spec)
        if smart_spec and (not smart_combo
                           or hotkeys_conflict(smart_combo, voice_combo)
                           or is_undo_combo(smart_combo)):
            log(f"smart hotkey {smart_spec!r} rejected")
            smart_spec = ""
            cfg["smart_hotkey"] = ""
            save_config(cfg)
        else:
            smart_spec = "+".join(smart_combo)
        self.smart_hotkey = smart_spec
        self.hotkey = Hotkey(self, spec, smart_spec)
        self.mic_mode = "press" if cfg.get("mic_mode") == "press" else "always"
        self.smart_text = SMART_AVAILABLE and cfg.get("smart_text") is True
        self.input_lock = threading.Lock()
        self.user_input_seq = 0
        self.undo_rec = None
        self.smart_job = False
        self.capture_target = "voice"
        self._smart_notice_t = 0.0
        self._session_t0 = 0.0

    def _engine_extra(self):
        if self.zh_space:
            return {}
        return {"remove_space_between_han_num": True,
                "remove_space_between_han_eng": True}

    def _make_engine(self, kind):
        if kind != "doubao":
            return create_engine(kind)
        if self.local_engine is None:
            self.local_engine = create_engine("doubao")
        return self.local_engine

    def test_local_engine(self):
        eng = self._make_engine("doubao")
        if hasattr(eng, "use_frontier"):
            eng.use_frontier = self.doubao_quic
        eng.set_extra(self._engine_extra())
        return eng

    def _save_option(self, **values):
        cfg = load_config()
        cfg.update(values)
        save_config(cfg)

    def tray_tip(self):
        return f"{ENGINE_NAMES[self.engine_kind]}语音输入（{self.hotkey.display()}）"

    def set_asr_engine(self, kind):
        if kind not in ASR_ENGINES:
            return False
        with self.lock:
            busy = self.session is not None or self.testing
        if busy:
            self.set_ui("done", "正在录音，请结束后再切换识别引擎", auto_hide=2200)
            return False
        if kind == self.engine_kind:
            return True
        self.engine_kind = kind
        self.engine = self._make_engine(kind)
        if hasattr(self.engine, "use_frontier"):
            self.engine.use_frontier = self.doubao_quic
        self.engine.set_extra(self._engine_extra())
        self._save_option(asr_engine=kind)
        log(f"asr engine={kind}")
        if self.tray is not None:
            try:
                self.tray.set_tip(self.tray_tip())
            except Exception:
                pass
        self.set_ui("done", f"已切换为{ENGINE_NAMES[kind]}", auto_hide=1800)
        threading.Thread(target=self._warm_engine, daemon=True).start()
        return True

    def mode_hint(self):
        hint = self.hotkey.display()
        if self.voice_mode == "hands_free":
            return f"按 {hint} 开始说话，再按 {hint} 或任意键结束"
        return f"按住 {hint} 说话，松开输入"

    def set_voice_mode(self, mode):
        if mode not in VOICE_MODES:
            return
        self.voice_mode = mode
        self._save_option(voice_mode=mode)
        log(f"voice mode={mode}")
        self.set_ui("done", ("已切换为免按模式" if mode == "hands_free"
                             else "已切换为长按说话"), auto_hide=1800)

    def set_min_hold(self, ms):
        if ms not in MIN_HOLD_MS:
            return
        self.min_hold_ms = ms
        self._save_option(min_hold_ms=ms)
        log(f"min hold={ms}ms")

    def set_punctuation(self, mode):
        if mode not in PUNCT_MODES:
            return
        self.punctuation = mode
        self._save_option(punctuation_mode=mode)
        log(f"punctuation={mode}")

    def set_paragraph_mode(self, mode):
        if mode not in PARAGRAPH_MODES:
            return
        self.paragraph_mode = mode
        self._save_option(paragraph_mode=mode)
        log(f"paragraph={mode}")

    def set_zh_space(self, enabled):
        self.zh_space = bool(enabled)
        self.engine.set_extra(self._engine_extra())
        self._save_option(zh_space=self.zh_space)
        log(f"zh_space={self.zh_space}")

    def set_smart_text(self, enabled):
        enabled = bool(enabled)
        if not SMART_AVAILABLE:
            return False
        self.smart_text = enabled
        self._save_option(smart_text=enabled)
        log(f"smart_text={enabled}")
        if enabled:
            self.set_ui("done", "已开启智能文字整理", auto_hide=1800)
        else:
            with self.input_lock:
                self.undo_rec = None
            self.set_ui("done", "已关闭智能文字整理", auto_hide=1800)
        return True

    def toggle_smart_text(self):
        if not SMART_AVAILABLE:
            return
        self.set_smart_text(not self.smart_text)
        if self.settings is not None:
            self.settings.refresh()

    def smart_hotkey_display(self):
        return self.hotkey.smart_display()

    def set_smart_hotkey(self, spec):
        combo = parse_smart_hotkey(spec)
        if combo and (hotkeys_conflict(combo, self.hotkey.combo)
                      or is_undo_combo(combo)):
            return False
        normalized = "+".join(combo)
        self.smart_hotkey = normalized
        self.hotkey.set_smart_combo(normalized)
        self._save_option(smart_hotkey=normalized)
        log(f"smart hotkey={normalized or 'none'}")
        if self.settings is not None:
            self.settings.refresh()
        if normalized:
            self.set_ui("done", f"已设置整理快捷键：{self.hotkey.smart_display()}",
                        auto_hide=2000)
        else:
            self.set_ui("done", "已清除整理快捷键", auto_hide=1800)
        return True

    def clear_smart_hotkey(self):
        if self.capturing:
            self.capture_cancel()
        return self.set_smart_hotkey("")

    def on_smart_hotkey(self):
        if (not SMART_AVAILABLE or self.capturing or self.testing
                or self.session is not None):
            return False
        if self.hotkey.active:
            return False
        with self.input_lock:
            if self.smart_job:
                return True
            self.smart_job = True
        log("smart hotkey pressed")
        threading.Thread(target=self._smart_selection_worker, daemon=True).start()
        return True

    def note_user_input(self, tok=""):
        with self.input_lock:
            self.user_input_seq += 1
            self.undo_rec = None

    def smart_guard(self, seq):
        def check():
            if self.capturing or self.testing or self.session is not None:
                return False
            with self.input_lock:
                return self.user_input_seq == seq
        return check

    def on_undo_hotkey(self):
        if (self.capturing or self.testing or self.session is not None
                or self.hotkey.active):
            return False
        with self.input_lock:
            rec = self.undo_rec
            if rec is None:
                return False
            if self.user_input_seq != rec["seq"] or time.monotonic() - rec["t"] > 180:
                self.undo_rec = None
                return False
            self.undo_rec = None
        fg_now = int(user32.GetForegroundWindow() or 0)
        if fg_now != rec["fg"]:
            return False
        tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg_now), None)
        focus_now, _ = _thread_focus(tid)
        if rec["focus"] and focus_now != rec["focus"]:
            return False
        log(f"undo hotkey accepted focus={focus_now:#x}")
        threading.Thread(target=self._restore_undo, args=(rec,), daemon=True).start()
        return True

    def _restore_undo(self, rec):
        try:
            guard = self.smart_guard(rec["seq"])
            res = replace_recent_text(rec["new"], rec["original"], rec["fg"], rec["focus"],
                                      self.args.inject, guard)
            log(f"undo restore={res} {len(rec['new'])}->{len(rec['original'])}")
            if res in ("ok", "weak"):
                self.set_ui("done", "已恢复原文", auto_hide=1600)
            elif res in ("window", "focus", "changed", "keys"):
                pass
            else:
                self.set_ui("fail", "未能恢复原文，可手动撤销", auto_hide=2800)
        except Exception:
            log("undo error " + traceback.format_exc().replace("\n", " | "))

    def set_mic_selection(self, sel):
        with self.lock:
            busy = self.session is not None
        if busy:
            self.set_ui("done", "正在录音，请结束后再更换麦克风", auto_hide=2000)
            return False
        dev = resolve_mic_device(sel) if sel else None
        if sel and dev is None:
            self.set_ui("fail", "未找到该麦克风", auto_hide=2600)
            return False
        old_dev = self.mic.device
        if self.mic_mode == "always":
            self.mic.close()
            self.mic.device = dev
            try:
                self.mic.open()
            except Exception:
                log("mic open error " + traceback.format_exc().replace("\n", " | "))
                self.mic.device = old_dev
                try:
                    self.mic.open()
                except Exception:
                    log("mic reopen error " + traceback.format_exc().replace("\n", " | "))
                self.set_ui("fail", "麦克风打开失败，已恢复原设备", auto_hide=3000)
                return False
        else:
            self.mic.device = dev
        self.mic_sel = sel
        self._save_option(mic_device=sel)
        log(f"mic selection={sel} device={dev}")
        self.set_ui("done", "已切换麦克风", auto_hide=1600)
        return True

    def set_mic_mode(self, want):
        if want not in ("always", "press"):
            return False
        with self.lock:
            busy = self.session is not None
        if busy:
            self.set_ui("done", "正在录音，请结束后再切换", auto_hide=1800)
            return False
        if want == self.mic_mode:
            return True
        if want == "always":
            try:
                self.mic.open()
            except Exception:
                log("mic open error " + traceback.format_exc().replace("\n", " | "))
                self.set_ui("fail", "麦克风打开失败", auto_hide=3000)
                return False
        else:
            self.mic.close()
        self.mic_mode = want
        self._save_option(mic_mode=want)
        log(f"mic mode={want}")
        self.set_ui("done", "麦克风已预热" if want == "always" else "麦克风改为录音时打开",
                    auto_hide=2400)
        return True

    def set_autostart_enabled(self, want):
        if not set_autostart(want):
            self.set_ui("fail", "设置开机自启失败（权限不足）", auto_hide=2600)
            return False
        log(f"autostart={'on' if want else 'off'}")
        self.set_ui("done", "已开启开机自启" if want else "已关闭开机自启", auto_hide=1800)
        return True

    def _arm_capture_timeout(self):
        self.capture_gen += 1
        gen = self.capture_gen
        timer = threading.Timer(12.0, lambda: self._capture_timeout(gen))
        timer.daemon = True
        timer.start()

    def start_capture(self, target="voice"):
        if self.session is not None:
            self.set_ui("done", "正在录音，请结束后再设置快捷键", auto_hide=1800)
            return
        if target == "smart" and not SMART_AVAILABLE:
            return
        self.capture_target = target
        self.capturing = True
        self.hotkey.begin_capture()
        if target == "smart":
            self.set_ui("capture", "按下整理文字的快捷键或鼠标键…  Esc 取消")
        else:
            self.set_ui("capture", "按下新的快捷键或鼠标键…  Esc 取消")
        log(f"capture start target={target}")
        self._arm_capture_timeout()

    def _capture_timeout(self, gen):
        if self.capturing and self.capture_gen == gen:
            log("capture timeout")
            self.capture_cancel()

    def capture_left_mod_rejected(self, tok):
        if not self.capturing:
            return
        name = hotkey_display([tok])
        self.set_ui("capture", f"{name} 不能单独作快捷键，"
                               "请用右侧 Ctrl/Alt/Shift 或组合键")
        self._arm_capture_timeout()

    def capture_invalid(self):
        if not self.capturing:
            return
        self.set_ui("capture", "请包含 Ctrl/Alt/Shift、使用 F1–F12，或直接按鼠标中键/侧键。Esc 取消")
        self._arm_capture_timeout()

    def capture_done(self, spec):
        if not self.capturing:
            return
        if self.capture_target == "smart":
            combo = parse_smart_hotkey(spec)
            if not combo:
                self.capture_invalid()
                return
            if is_undo_combo(combo):
                self.set_ui("capture", "Ctrl+Z 用于撤销，请换一个。Esc 取消")
                self._arm_capture_timeout()
                return
            if hotkeys_conflict(combo, self.hotkey.combo):
                self.set_ui("capture", "与语音输入快捷键冲突，请换一个。Esc 取消")
                self._arm_capture_timeout()
                return
            self.capturing = False
            self.capture_target = "voice"
            self.smart_hotkey = "+".join(combo)
            self.hotkey.set_smart_combo(self.smart_hotkey)
            cfg = load_config()
            cfg["smart_hotkey"] = self.smart_hotkey
            save_config(cfg)
            log(f"smart hotkey set {self.smart_hotkey}")
            if self.settings is not None:
                self.settings.refresh()
            self.set_ui("done", f"已设置整理快捷键：{self.hotkey.smart_display()}",
                        auto_hide=2000)
            return
        combo = parse_hotkey(spec)
        if hotkeys_conflict(combo, self.hotkey.smart_combo):
            self.set_ui("capture", "与整理快捷键冲突，请换一个。Esc 取消")
            self._arm_capture_timeout()
            return
        self.capturing = False
        self.capture_target = "voice"
        spec = "+".join(combo)
        self.hotkey.set_combo(spec)
        cfg = load_config()
        cfg["hotkey"] = spec
        save_config(cfg)
        log(f"hotkey set {spec}")
        if self.tray is not None:
            self.tray.set_tip(self.tray_tip())
        if self.settings is not None:
            self.settings.refresh()
        self.set_ui("done", f"已设置快捷键：{self.hotkey.display()}", auto_hide=2000)

    def capture_cancel(self):
        if not self.capturing:
            return
        self.capturing = False
        self.capture_target = "voice"
        log("capture cancel")
        if self.settings is not None:
            self.settings.refresh()
        self.set_ui("done", "已取消设置快捷键", auto_hide=1500)

    def toggle_autostart(self):
        self.set_autostart_enabled(not autostart_enabled())
        if self.settings is not None:
            self.settings.refresh()

    def toggle_mic_mode(self):
        self.set_mic_mode("press" if self.mic_mode == "always" else "always")
        if self.settings is not None:
            self.settings.refresh()

    def set_float_visible(self, on):
        self.float_visible = bool(on)
        self._save_option(float_visible=self.float_visible)
        if self.float_btn is not None:
            self.float_btn.set_visible(self.float_visible)
        if self.settings is not None:
            self.settings.refresh()
        log(f"float visible={self.float_visible}")

    def toggle_float(self):
        self.set_float_visible(not self.float_visible)

    def float_click(self):
        fb = self.float_btn
        if fb is not None and fb.take_consumed():
            return
        with self.lock:
            sess = self.session
            testing = self.testing
        if sess is not None:
            self.on_key_up("hands_free")
            return
        if testing:
            return
        hk = self.hotkey
        hk.active = True
        hk.active_mode = "hands_free"
        hk.float_active = True
        hk.active_session = self.start_session()
        if hk.active_session is None:
            hk.active = False
            hk.float_active = False

    def open_settings(self):
        if self.settings is None:
            self.settings = Settings(self)
            self.settings.start()
        self.settings.open()

    def open_engine_test(self):
        if self.testing:
            if self.engine_test is not None:
                self.engine_test.focus()
            return False
        with self.lock:
            if self.session is not None:
                self.set_ui("done", "正在录音，请结束后再测试", auto_hide=2200)
                return False
        self.testing = True
        owner = self.settings.hwnd if self.settings is not None else None
        self.engine_test = EngineTest(self, owner)
        self.engine_test.start()
        log("engine test opened")
        return True

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

    def set_ui(self, kind, msg="", auto_hide=None, live=None, toast=False):
        gen = self.ui.set(kind, msg, live, toast)
        if auto_hide:
            def later():
                with self.ui.lock:
                    if self.ui.gen == gen:
                        self.ui.kind = "none"
                        self.ui.msg = ""
                        self.ui.live = ""
                        self.ui.toast = False
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

    def _warm_engine(self):
        try:
            self.engine.warmup()
        except Exception:
            log("warmup error " + traceback.format_exc().replace("\n", " | "))
            self.set_ui("done", "语音引擎启动失败", auto_hide=4000)

    def warmup_async(self):
        threading.Thread(target=self._warm_engine, daemon=True).start()

    def start_session(self):
        with self.lock:
            if self.session is not None or self.testing:
                return None
            sess = self.engine.new_session(self.args.verbose, on_text=self.update_live)
            self.session = sess
            self._session_t0 = time.monotonic()
        threading.Thread(target=self._run_session, args=(sess,), daemon=True).start()
        return sess

    def _run_session(self, sess):
        self.accept_live = True
        press_mic = self.mic_mode == "press" and not self.args.wav
        try:
            if sess.cancelled:
                return
            self.set_ui("rec", f"● 聆听中…  {self.hotkey.release_hint()}", live="")
            if not self.args.wav:
                if self.mic_mode == "press" or self.mic.stream is None:
                    try:
                        self.mic.open()
                    except Exception:
                        log("mic open error " + traceback.format_exc().replace("\n", " | "))
                        self.set_ui("done", "麦克风打开失败", auto_hide=4000)
                        return
                self.mic.attach(sess)
                if sess.cancelled:
                    return
            if not self.engine.ready.wait(20):
                log("engine not ready")
                self.set_ui("done", "语音引擎未就绪", auto_hide=2500)
                return
            if sess.cancelled:
                return
            sess.start()
            if self.args.wav:
                threading.Thread(target=wav_pusher, args=(sess, self.args.wav), daemon=True).start()
            sess.release_evt.wait(120)
            if sess.cancelled:
                return
            if not self.args.wav:
                self.mic.detach(sess)
                if press_mic:
                    self.mic.close()
            self.set_ui("wait", "识别中…")
            sess.finish()
            self.accept_live = False
            text = apply_hotwords(format_text(sess.text, self.punctuation,
                                              self.paragraph_mode))
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
            if press_mic:
                self.mic.close()
            self.mic.detach(sess)
            with self.lock:
                self.session = None
            hk = self.hotkey
            if hk.active_session is sess:
                hk.active = False
                hk.float_active = False
                hk.active_session = None
            with self.ui.lock:
                kind = self.ui.kind
            if kind != "done":
                self.set_ui("none")
            self.once_done.set()

    def inject_and_report(self, text):
        with self.input_lock:
            self.undo_rec = None
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
        sent_ok = not expected or sent == expected
        # 微信 Qt、Chrome/Electron 的编辑框读不回文字，校验必然 fail/unknown，
        # 但按键实际已送达，按弱校验继续，后续用按键回退替换
        weak_ok = sent_ok and res in ("fail", "unknown") and _weak_allowed(snap["focus"])
        if weak_ok:
            log(f"inject weak-ok class={_class_name(snap['focus'])}")
        if (res == "ok" or weak_ok) and sent_ok:
            self.set_ui("done", f"✓ 已输入 {len(text)} 字", auto_hide=1500)
            self.start_smart(text, snap["fg"], snap["focus"])
        elif res == "fail" or not sent_ok:
            if self.args.inject != "paste":
                _set_clipboard(text)
            self.set_ui("fail", "未能输入，文字已复制到剪贴板", auto_hide=3200)
        else:
            _set_clipboard(text)
            self.set_ui("done", f"已输入，无法确认（已存剪贴板）", auto_hide=2600)

    def start_smart(self, original, fg=None, focus=None):
        if not SMART_AVAILABLE or not self.smart_text or self.args.once:
            return
        if len(original.strip()) < SMART_MIN_CHARS or len(original) > SMART_TEXT_CAP:
            log(f"smart skip: length {len(original)}")
            return
        # 目标窗口取注入时的快照，用户随后切换窗口时替换会因 fg 不符而放弃
        if not fg:
            fg = int(user32.GetForegroundWindow() or 0)
            if not fg:
                return
            tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
            focus, _ = _thread_focus(tid)
        with self.input_lock:
            rec = {"seq": self.user_input_seq, "fg": fg, "focus": focus,
                   "t": time.monotonic(), "original": original}
        log(f"smart start {len(original)} chars focus={focus:#x}")
        threading.Thread(target=self._smart_worker, args=(rec,), daemon=True).start()

    def _smart_worker(self, rec):
        if not self.smart_text:
            return
        try:
            verdict, new = smart_organize(rec["original"])
        except Exception:
            log("smart error " + traceback.format_exc().replace("\n", " | "))
            verdict, new = "error", "exception"
        if verdict == "error":
            log(f"smart organize failed: {new}")
            self._smart_notice("智能整理未生效（已保留原文）")
            return
        if verdict == "rewrite" and new.strip():
            new = apply_hotwords(new)
        if verdict != "rewrite" or not new.strip() or new == rec["original"]:
            log(f"smart organize kept original ({verdict})")
            return
        self._apply_smart(rec, new)

    def _smart_notice(self, msg):
        now = time.monotonic()
        if now - self._smart_notice_t < 120:
            return
        self._smart_notice_t = now
        self.set_ui("done", msg, auto_hide=2800)

    def _apply_smart(self, rec, new):
        if self.capturing or self.session is not None:
            log("smart skip: busy")
            return
        if time.monotonic() - rec["t"] > 20:
            log("smart skip: stale")
            return
        guard = self.smart_guard(rec["seq"])
        if not guard():
            log("smart skip: input changed")
            return
        res = replace_recent_text(rec["original"], new, rec["fg"], rec["focus"],
                                  self.args.inject, guard)
        log(f"smart apply={res} {len(rec['original'])}->{len(new)}")
        if res in ("ok", "weak"):
            with self.input_lock:
                if self.user_input_seq == rec["seq"]:
                    self.undo_rec = {"fg": rec["fg"], "focus": rec["focus"],
                                     "seq": rec["seq"], "t": time.monotonic(),
                                     "original": rec["original"], "new": new}
            self.set_ui("done", "✓ 已智能整理 · Ctrl+Z 恢复原文", auto_hide=1400, toast=True)
        elif res in ("window", "focus", "changed", "keys"):
            pass
        else:
            self._smart_notice("智能整理未生效（已保留原文）")

    def _smart_selection_worker(self):
        try:
            self._organize_selection()
        except Exception:
            log("smart selection error " + traceback.format_exc().replace("\n", " | "))
            self.set_ui("fail", "整理选中文字出错", auto_hide=2800)
        finally:
            with self.input_lock:
                self.smart_job = False

    def _copy_selection(self):
        old = _get_clipboard_text()
        if old is not None:
            _set_clipboard(_SMART_CLIP_SENTINEL)
        sent = _send([_vk_event(0x11), _vk_event(0x43),
                      _vk_event(0x43, True), _vk_event(0x11, True)])
        deadline = time.monotonic() + 1.5
        text = None
        while time.monotonic() < deadline:
            time.sleep(0.05)
            cur = _get_clipboard_text()
            if cur is not None and cur != _SMART_CLIP_SENTINEL:
                text = cur
                break
        log(f"smart selection copy sent={sent} chars={len(text) if text else 0}")
        return text, old

    def _restore_clipboard(self, old):
        if old is not None:
            _set_clipboard(old)

    def _organize_selection(self):
        snap = focus_snapshot()
        if not snap["fg"]:
            self.set_ui("fail", "未找到输入窗口，请先选中文字", auto_hide=2600)
            return
        if not self.hotkey.wait_released():
            log("smart selection: hotkey still held")
            self.set_ui("done", "请松开快捷键后再试", auto_hide=2200)
            return
        if (not _same_target(snap) or self.capturing or self.testing
                or self.session is not None):
            log("smart selection: target changed")
            self.set_ui("fail", "窗口已切换，已取消整理", auto_hide=2400)
            return
        focus = snap["focus"]
        before_text, _ = _read_control(focus) if focus else (None, 0)
        with self.input_lock:
            seq = self.user_input_seq
        guard = self.smart_guard(seq)
        text, old_clip = self._copy_selection()
        if not text or not text.strip():
            self._restore_clipboard(old_clip)
            self.set_ui("done", "请先选中要整理的文字", auto_hide=2600)
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        chars = len(text.strip())
        # 微信 Qt、Chrome/Electron 等窗口的 WM_GETTEXT 往往只返回窗口标题，
        # 读回内容不含选区时无法据此判断是否替换成功
        readback_ok = (before_text is not None and chars > 0
                       and not _weak_allowed(focus)
                       and text.strip()
                       in before_text.replace("\r\n", "\n").replace("\r", "\n"))
        if chars < SMART_MIN_CHARS:
            self._restore_clipboard(old_clip)
            self.set_ui("done", f"选中文字不足 {SMART_MIN_CHARS} 字，未整理",
                        auto_hide=2600)
            return
        if chars > SMART_TEXT_CAP:
            self._restore_clipboard(old_clip)
            self.set_ui("fail", f"选中文字过长（最多 {SMART_TEXT_CAP} 字）",
                        auto_hide=3000)
            return
        self._restore_clipboard(old_clip)
        self.set_ui("wait", "正在整理选中文字…")
        log(f"smart selection start {chars} chars focus={focus:#x}")
        verdict, new = smart_organize(text)
        if verdict == "error":
            log(f"smart selection organize failed: {new}")
            self.set_ui("fail", "智能整理未生效，已保留原文", auto_hide=3000)
            return
        if verdict != "rewrite" or not new.strip() or new.strip() == text.strip():
            log(f"smart selection kept original ({verdict})")
            self.set_ui("done", "选中文字无需整理", auto_hide=2200)
            return
        new = apply_hotwords(new)
        if not new.strip() or new.strip() == text.strip():
            self.set_ui("done", "选中文字无需整理", auto_hide=2200)
            return
        if not guard():
            log("smart selection: input changed before paste")
            _set_clipboard(new)
            self.set_ui("done", "窗口有新的输入，整理结果已复制", auto_hide=3000)
            return
        if not _same_target(snap):
            log("smart selection: window changed before paste")
            _set_clipboard(new)
            self.set_ui("done", "窗口已切换，整理结果已复制", auto_hide=3000)
            return
        if not self.hotkey.wait_released():
            log("smart selection: hotkey held before paste")
            return
        _set_clipboard(new)
        time.sleep(0.06)
        sent = _send([_vk_event(0x11), _vk_event(0x56),
                      _vk_event(0x56, True), _vk_event(0x11, True)])
        time.sleep(0.55)
        if old_clip is not None:
            _set_clipboard(old_clip)
        verify = "unreadable"
        if readback_ok:
            after_text, _ = _read_control(focus) if focus else (None, 0)
            deadline = time.monotonic() + 1.0
            while (after_text is not None and after_text == before_text
                   and time.monotonic() < deadline):
                time.sleep(0.15)
                after_text, _ = _read_control(focus) if focus else (None, 0)
            verify = ("unchanged" if after_text is not None
                      and after_text == before_text else "changed")
        if verify == "unchanged":
            log(f"smart selection verify unchanged paste={sent} "
                f"class={_class_name(focus)} before={len(before_text)}")
            _set_clipboard(new)
            self.set_ui("fail", "未能替换选中文字，整理结果已复制", auto_hide=3200)
            return
        log(f"smart selection applied {chars}->{len(new)} paste={sent} verify={verify}")
        self.set_ui("done", "✓ 已整理选中文字 · Ctrl+Z 恢复", auto_hide=1400, toast=True)

    def on_key_down(self):
        self.hotkey.active_session = self.start_session()

    def on_key_up(self, mode=None):
        with self.lock:
            sess = self.session
            t0 = self._session_t0
        if sess is None:
            return
        if (mode == "hold" and self.min_hold_ms
                and time.monotonic() - t0 < self.min_hold_ms / 1000.0):
            log(f"hold {int((time.monotonic() - t0) * 1000)}ms ignored")
            sess.cancel()
            self.set_ui("done", "按住时间太短，已忽略", auto_hide=1500)
            return
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
    ap.add_argument("--engine", choices=ASR_ENGINES, default=None,
                    help="识别引擎：doubao / doubao_api / qwen / wetype / baidu / ifly（默认读取设置）")
    ap.add_argument("--inject", choices=["unicode", "paste"], default="unicode",
                    help="文字注入方式：unicode 逐字输入 / paste 剪贴板粘贴")
    ap.add_argument("--no-inject", action="store_true", help="只识别不输入")
    ap.add_argument("--no-ui", action="store_true", help="不显示悬浮提示")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    ap.add_argument("--settings", action="store_true", help="启动时打开设置窗口")
    ap.add_argument("--verbose", action="store_true", help="日志记录每个中间结果")
    ap.add_argument("--once", action="store_true", help="启动后立即识别一次并退出（需 --wav）")
    args = ap.parse_args()

    if args.once and not args.wav:
        ap.error("--once 需要配合 --wav")

    set_dpi_awareness()
    log("=== start " + " ".join(sys.argv[1:]))
    ensure_hotwords_file()

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
    log(f"hotkey={app.hotkey.display()} smart_hotkey={app.smart_hotkey_display()} "
        f"config={CONFIG_PATH} autostart={autostart_enabled()} "
        f"engine={app.engine_kind} voice_mode={app.voice_mode} punctuation={app.punctuation} "
        f"paragraph={app.paragraph_mode} zh_space={app.zh_space} mic={app.mic.device}")
    refresh_autostart()
    app.warmup_async()

    if not args.wav:
        if app.mic_mode == "always":
            try:
                app.mic.open()
            except Exception:
                log("mic open error " + traceback.format_exc().replace("\n", " | "))
                app.set_ui("done", "麦克风打开失败", auto_hide=4000)
        else:
            try:
                app.mic.preload()
            except Exception:
                log("mic preload error " + traceback.format_exc().replace("\n", " | "))
            log("mic mode=press, stream opens on demand")

    if not args.no_ui:
        Overlay(app.ui, app.mic).start()

    if args.once:
        app.run_once()
        app.mic.close()
        return

    app.hotkey.start()
    if not args.no_ui:
        fb = FloatButton(app)
        app.float_btn = fb
        fb.start()
    if not args.no_tray:
        tray = Tray(app)
        app.tray = tray
        tray.start()
    if args.settings:
        app.open_settings()
    hint = app.mode_hint()
    if not args.no_ui:
        app.set_ui("info", f"语音输入已就绪：{hint}，Esc 取消", auto_hide=3000)
    say(f"已启动：{hint}。Esc 取消。托盘图标可退出。日志目录: " + LOG_DIR)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.mic.close()


if __name__ == "__main__":
    main()
