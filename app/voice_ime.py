#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按住右 Alt 说话，松开后文字直接输入到当前焦点窗口。"""

import argparse
import ctypes
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import wave
import winreg
import zipfile
from ctypes import wintypes

import api_common
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
LOG_PATH = os.path.join(APP_DIR, "voice_ime.log")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HOTWORDS_PATH = os.path.join(APP_DIR, "hotwords.txt")
HOTWORDS_HEADER = ("# 热词替换：每行一条，格式：原文 -> 替换为\n"
                   "# 以 # 开头的行会被忽略，修改后下次识别自动生效\n")
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

SMART_FILES = ("ImeService.exe", "ui.dll", "oime-config.dll", "audioeffect-mt.dll",
               "applogrs.dll", "doubaoime-rpc-new.dll", "parfait.dll", "sscronet.dll")
ASR_DLLS = ("audioeffect-mt.dll", "sscronet.dll")
SMART_ZIP_NAME = "smart_runtime.zip"
SMART_VERSION_NAME = "smart_runtime.version"
SMART_TEXT_CAP = 12000
SMART_MIN_CHARS = 5
SMART_MANIFEST = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
    '<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security>'
    '<requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false">'
    '</requestedExecutionLevel></requestedPrivileges></security></trustInfo>'
    '<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>'
    '<dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>'
    '<dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">'
    'PerMonitorV2,PerMonitor</dpiAwareness>'
    '<activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8'
    '</activeCodePage></windowsSettings></application></assembly>'
)
_smart_lock = threading.Lock()
_smart_exe = None
_CLI_LOG_RE = re.compile(rb"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def _version_key(name):
    nums = re.findall(r"\d+", name)
    return tuple(int(n) for n in nums) if nums else (0,)


def find_ime_runtime_source():
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        roots.append(os.path.join(base, "DoubaoIME", "versions"))
        roots.append(os.path.join(base, "Programs", "DoubaoIME", "versions"))
    seen = set()
    found = []
    for root in roots:
        key = os.path.normcase(os.path.abspath(root))
        if key in seen:
            continue
        seen.add(key)
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if all(os.path.isfile(os.path.join(path, f)) for f in SMART_FILES):
                found.append((_version_key(name), name, path))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return found[0][2], found[0][1]


def _smart_resource(name):
    meipass = getattr(sys, "_MEIPASS", None)
    bases = (meipass,) if getattr(sys, "frozen", False) else (meipass, APP_DIR)
    for base in bases:
        if not base:
            continue
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return ""


def _bundled_smart_version():
    path = _smart_resource(SMART_VERSION_NAME)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                version = f.read().strip()
        except (OSError, UnicodeError):
            version = ""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", version):
            return version
    return "bundled"


def _detect_smart_available():
    if _smart_resource(SMART_ZIP_NAME):
        return True
    if getattr(sys, "frozen", False):
        return False
    return find_ime_runtime_source() is not None


SMART_AVAILABLE = _detect_smart_available()


def _locate_manifest(data):
    if data[:2] != b"MZ":
        raise ValueError("not a PE file")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("bad PE signature")
    nsec = struct.unpack_from("<H", data, pe_off + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe_off + 20)[0]
    opt = pe_off + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dd = opt + (112 if magic == 0x20B else 96)
    res_rva = struct.unpack_from("<I", data, dd + 16)[0]
    sections = []
    sec = opt + opt_size
    for i in range(nsec):
        base = sec + i * 40
        vsize, va, rawsize, raw = struct.unpack_from("<IIII", data, base + 8)
        sections.append((va, max(vsize, rawsize), raw))

    def rva2off(rva):
        for va, size, raw in sections:
            if va <= rva < va + size:
                return raw + rva - va
        raise ValueError("RVA not mapped")

    base_off = rva2off(res_rva)

    def entries(rel):
        off = base_off + rel
        n_named, n_id = struct.unpack_from("<HH", data, off + 12)
        out = []
        for i in range(n_named + n_id):
            name, child = struct.unpack_from("<II", data, off + 16 + i * 8)
            out.append((name, child))
        return out

    for name, child in entries(0):
        if name != 24 or not child & 0x80000000:
            continue
        for _rid, c2 in entries(child & 0x7FFFFFFF):
            if not c2 & 0x80000000:
                continue
            for _lang, c3 in entries(c2 & 0x7FFFFFFF):
                entry = base_off + (c3 & 0x7FFFFFFF)
                rva, size = struct.unpack_from("<II", data, entry)
                return rva, size, rva2off(rva)
    raise ValueError("manifest resource not found")


def _patch_exe_manifest(path):
    with open(path, "rb") as f:
        data = bytearray(f.read())
    _rva, size, off = _locate_manifest(bytes(data))
    manifest = SMART_MANIFEST.encode("utf-8")
    if len(manifest) > size:
        raise ValueError("manifest does not fit")
    data[off:off + size] = manifest + b" " * (size - len(manifest))
    with open(path, "wb") as f:
        f.write(data)


def _bundled_dll_dir():
    return os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "dll")


def _runtime_source():
    zip_path = _smart_resource(SMART_ZIP_NAME)
    if zip_path:
        return zip_path, "", _bundled_smart_version(), "bundled"
    src = find_ime_runtime_source()
    if not src:
        raise RuntimeError("未检测到智能整理运行时")
    return "", src[0], src[1], src[0]


def _runtime_dir(version):
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    dst = os.path.join(local, "DoubaoVoice", "runtime", version)
    os.makedirs(dst, exist_ok=True)
    return dst


def _copy_runtime_files(dst, zip_path, src_dir, names):
    if zip_path:
        with zipfile.ZipFile(zip_path) as zf:
            for name in names:
                d = os.path.join(dst, name)
                size = zf.getinfo(name).file_size
                try:
                    if os.path.isfile(d) and os.path.getsize(d) == size:
                        continue
                except OSError:
                    pass
                with zf.open(name) as srcf, open(d, "wb") as outf:
                    shutil.copyfileobj(srcf, outf, 1 << 20)
    else:
        for name in names:
            s = os.path.join(src_dir, name)
            d = os.path.join(dst, name)
            try:
                if os.path.isfile(d) and os.path.getsize(d) == os.path.getsize(s):
                    continue
            except OSError:
                pass
            shutil.copyfile(s, d)


def prepare_asr_dlls():
    bundled = _bundled_dll_dir()
    if all(os.path.isfile(os.path.join(bundled, f)) for f in ASR_DLLS):
        return bundled
    if not SMART_AVAILABLE:
        raise RuntimeError("缺少语音引擎文件")
    with _smart_lock:
        zip_path, src_dir, version, _source = _runtime_source()
        dst = _runtime_dir(version)
        if zip_path:
            marker = os.path.join(dst, "asr_ready.json")
            try:
                with open(marker, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                info = {}
            with zipfile.ZipFile(zip_path) as zf:
                crcs = {n: zf.getinfo(n).CRC for n in ASR_DLLS}
            ready = (info.get("zip_size") == os.path.getsize(zip_path)
                     and info.get("crc") == crcs
                     and all(os.path.isfile(os.path.join(dst, n)) for n in ASR_DLLS))
            _copy_runtime_files(dst, zip_path, src_dir, ASR_DLLS)
            if not ready:
                with open(marker, "w", encoding="utf-8") as f:
                    json.dump({"zip_size": os.path.getsize(zip_path), "crc": crcs}, f)
        else:
            _copy_runtime_files(dst, zip_path, src_dir, ASR_DLLS)
        return dst


def prepare_smart_runtime():
    global _smart_exe
    with _smart_lock:
        if _smart_exe and os.path.isfile(_smart_exe):
            return _smart_exe
        if not SMART_AVAILABLE:
            raise RuntimeError("当前版本不包含智能文字整理运行时")
        zip_path, src_dir, version, source = _runtime_source()
        dst = _runtime_dir(version)
        marker = os.path.join(dst, "ready.json")
        exe = os.path.join(dst, "ImeService.exe")
        zip_size = os.path.getsize(zip_path) if zip_path else 0
        ready = False
        if os.path.isfile(marker) and os.path.isfile(exe):
            try:
                with open(marker, "r", encoding="utf-8") as f:
                    info = json.load(f)
                ready = (info.get("source") == source
                         and info.get("zip_size", 0) == zip_size
                         and info.get("size") == os.path.getsize(exe)
                         and all(os.path.isfile(os.path.join(dst, f)) for f in SMART_FILES))
            except (OSError, ValueError):
                ready = False
        _copy_runtime_files(dst, zip_path, src_dir, SMART_FILES)
        if not ready:
            _patch_exe_manifest(exe)
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({"source": source, "version": version, "zip_size": zip_size,
                           "size": os.path.getsize(exe)}, f)
        _smart_exe = exe
        log(f"smart runtime ready {dst}" + (" (bundled)" if zip_path else ""))
        return exe


def _decode_cli(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")


def _run_smart_cli(exe, text, timeout=12.0):
    cmd = [exe, "--test-smart-organize", "--text", text,
           "--warmup-ms", "300", "--result-timeout-ms", "8000"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=0x08000000)
    chunks = []
    done = threading.Event()

    def reader():
        try:
            for line in iter(proc.stdout.readline, b""):
                s = line.strip()
                # C++ 日志行可能夹在 result 与 streamFinished 之间，直接丢弃
                if _CLI_LOG_RE.match(s):
                    continue
                chunks.append(line)
                if s.startswith(b"verdict="):
                    break
                # 只认 CLI 自己的 [<ms>] 行；C++ 日志行也含 "failed kind=" 但带时间戳
                if re.match(rb"^\[\d+ms\] failed kind=", s):
                    break
        except Exception:
            log("smart cli read error " + traceback.format_exc().replace("\n", " | "))
        finally:
            done.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    done.wait(timeout)
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(3)
    except Exception:
        pass
    thread.join(1)
    try:
        proc.stdout.close()
    except Exception:
        pass
    return _decode_cli(b"".join(chunks))


def parse_smart_output(out):
    lines = out.splitlines()
    no_rewrite = any(re.match(r"^\[\d+ms\] delta no_rewrite=1\b", line) for line in lines)
    m = re.search(r"(?m)^result=", out)
    if m:
        rest = out[m.end():]
        end = re.search(r"\r?\n\[(?:\d+ms\]|\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", rest)
        if end:
            rest = rest[:end.start()]
        rest = re.split(r"\r?\nverdict=", rest)[0]
        text = rest.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return ("rewrite", text) if text else ("same", "")
    if no_rewrite:
        return "same", ""
    failed = re.search(r"(?m)^\[\d+ms\] failed kind=(\d+)", out)
    if failed:
        return "error", f"kind={failed.group(1)}"
    return "error", "no_result"


def smart_organize(text):
    exe = prepare_smart_runtime()
    return parse_smart_output(_run_smart_cli(exe, text))


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
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            log("mic closed")
        with self.lock:
            del self.pre[:]


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
WM_GETTEXTLENGTH = 0x000E
EM_GETSEL = 0x00B0
EM_SETSEL = 0x00B1
CONTROL_TEXT_CAP = 65536
WEAK_MAX_CHARS = 4000
WEAK_CLASS_HINTS = ("chrome", "mozilla", "firefox", "electron", "webview")

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
            return "select"
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


class Hotkey:
    def __init__(self, app, spec=DEFAULT_HOTKEY):
        self.app = app
        self.set_combo(spec)
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

    def display(self):
        return hotkey_display(self.combo)

    def release_hint(self):
        mode = self.active_mode if self.active else self.app.voice_mode
        if mode == "hands_free":
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
                    self.app.on_key_down()
                    if self._swallow_down(tok):
                        self.swallowed.add(tok)
                        return True
            else:
                if not self.active and self._satisfied():
                    self.active = True
                    self.active_mode = "hold"
                    self.app.on_key_down()
                    if self._swallow_down(tok):
                        self.swallowed.add(tok)
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
                    self.active = False
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
            "SetBkColor": ([p, u], u),
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
    ID_MIC_ALWAYS = 1005
    ID_SETTINGS = 1006
    ID_SMART = 1007

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
        user32.AppendMenuW(hmenu, MF_STRING, self.ID_SET_HOTKEY, "设置快捷键…")
        auto = MF_STRING | (MF_CHECKED if autostart_enabled() else 0)
        user32.AppendMenuW(hmenu, auto, self.ID_AUTOSTART, "开机自启")
        mic = MF_STRING | (MF_CHECKED if self.app.mic_mode == "always" else 0)
        user32.AppendMenuW(hmenu, mic, self.ID_MIC_ALWAYS, "麦克风预热")
        if SMART_AVAILABLE:
            smart = MF_STRING | (MF_CHECKED if self.app.smart_text else 0)
            user32.AppendMenuW(hmenu, smart, self.ID_SMART, "智能文字整理")
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
        if cmd == self.ID_SETTINGS:
            self.app.open_settings()
        elif cmd == self.ID_SET_HOTKEY:
            self.app.open_settings()
            if self.app.settings is not None:
                self.app.settings.open_capture()
        elif cmd == self.ID_AUTOSTART:
            self.app.toggle_autostart()
        elif cmd == self.ID_MIC_ALWAYS:
            self.app.toggle_mic_mode()
        elif cmd == self.ID_SMART:
            self.app.toggle_smart_text()
        elif cmd == self.ID_OPEN_LOG:
            try:
                os.startfile(LOG_PATH)
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
SETTINGS_HEIGHT = 476 if SMART_AVAILABLE else 440
WM_DPICHANGED = 0x02E0
MONITOR_DEFAULTTONEAREST = 2
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


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
    ID_ENGINE_HINT = 2125
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

    def open_capture(self):
        if self.thread is None:
            self.start()
        if self.ready.wait(2.0) and self.hwnd:
            user32.PostMessageW.restype = ctypes.c_bool
            user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                            wintypes.WPARAM, wintypes.LPARAM]
            # 先显示窗口再开始捕获，消息在同一窗口队列里按序处理
            user32.PostMessageW(self.hwnd, WM_SETTINGS_SHOW, 0, 0)
            user32.PostMessageW(self.hwnd, WM_SETTINGS_CAPTURE, 0, 0)

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
                                           "豆包语音输入设置", style, x, y, w, h,
                                           None, None, hinst, None)
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
        self._mk("STATIC", "切换后立即生效", 0,
                 LX + 286, 48, LW - 286, 18, self.ID_ENGINE_HINT, small=True)
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
                     RX, 348, RW, 22, self.ID_SMART)
            self._mk("STATIC", "整理后按 Ctrl+Z 可恢复原文", 0,
                     RX, 372, RW, 18, self.ID_SMART_HINT, small=True)
            self._mk("STATIC", "文字会发送至豆包服务处理", 0,
                     RX, 390, RW, 18, self.ID_SMART_HINT2, small=True)
            bottom = 428
        else:
            bottom = 392
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
        self._check(self.ID_AUTOSTART, autostart_enabled())
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
        elif cid == self.ID_MODE_HOLD and code == BN_CLICKED:
            app.set_voice_mode("hold")
        elif cid == self.ID_MODE_FREE and code == BN_CLICKED:
            app.set_voice_mode("hands_free")
        elif cid == self.ID_ENGINE and code == CBN_SELCHANGE:
            idx = self._combo_index(self.ID_ENGINE)
            if 0 <= idx < len(ENGINE_ITEMS):
                app.set_asr_engine(ENGINE_ITEMS[idx][0])
                self.refresh()
        elif cid == self.ID_MIC_ALWAYS and code == BN_CLICKED:
            want = "always" if self._checked(self.ID_MIC_ALWAYS) else "press"
            if not app.set_mic_mode(want):
                self.refresh()
        elif cid == self.ID_AUTOSTART and code == BN_CLICKED:
            if not app.set_autostart_enabled(self._checked(self.ID_AUTOSTART)):
                self.refresh()
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
                self.app.start_capture()
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


# ---------------------------------------------------------------------- app

class App:
    def __init__(self, args):
        self.args = args
        cfg = load_config()
        kind = getattr(args, "engine", None)
        if kind not in ASR_ENGINES:
            kind = cfg.get("asr_engine")
        self.engine_kind = kind if kind in ASR_ENGINES else "doubao"
        self.engine = create_engine(self.engine_kind)
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
        self.hotkey = Hotkey(self, spec)
        self.mic_mode = "press" if cfg.get("mic_mode") == "press" else "always"
        self.smart_text = SMART_AVAILABLE and cfg.get("smart_text") is True
        self.input_lock = threading.Lock()
        self.user_input_seq = 0
        self.undo_rec = None
        self._smart_notice_t = 0.0
        self._session_t0 = 0.0

    def _engine_extra(self):
        if self.zh_space:
            return {}
        return {"remove_space_between_han_num": True,
                "remove_space_between_han_eng": True}

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
            busy = self.session is not None
        if busy:
            self.set_ui("done", "正在录音，请结束后再切换识别引擎", auto_hide=2200)
            return False
        if kind == self.engine_kind:
            return True
        self.engine_kind = kind
        self.engine = create_engine(kind)
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
            threading.Thread(target=self._prepare_smart_async, daemon=True).start()
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

    def _prepare_smart_async(self):
        try:
            prepare_smart_runtime()
        except Exception:
            log("smart runtime error " + traceback.format_exc().replace("\n", " | "))
            self.set_ui("fail", "智能整理运行时准备失败，详见日志", auto_hide=3200)
            if self.settings is not None:
                self.settings.refresh()

    def note_user_input(self, tok=""):
        with self.input_lock:
            self.user_input_seq += 1
            self.undo_rec = None

    def smart_guard(self, seq):
        def check():
            if self.capturing or self.session is not None:
                return False
            with self.input_lock:
                return self.user_input_seq == seq
        return check

    def on_undo_hotkey(self):
        if self.capturing or self.session is not None or self.hotkey.active:
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

    def start_capture(self):
        if self.session is not None:
            self.set_ui("done", "正在录音，请结束后再设置快捷键", auto_hide=1800)
            return
        self.capture_gen += 1
        gen = self.capture_gen
        self.capturing = True
        self.hotkey.begin_capture()
        self.set_ui("capture", "按下新的快捷键或鼠标键…  Esc 取消")
        log("capture start")
        timer = threading.Timer(12.0, lambda: self._capture_timeout(gen))
        timer.daemon = True
        timer.start()

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

    def capture_invalid(self):
        if not self.capturing:
            return
        self.set_ui("capture", "请包含 Ctrl/Alt/Shift、使用 F1–F12，或直接按鼠标中键/侧键。Esc 取消")

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
            self.tray.set_tip(self.tray_tip())
        if self.settings is not None:
            self.settings.refresh()
        self.set_ui("done", f"已设置快捷键：{self.hotkey.display()}", auto_hide=2000)

    def capture_cancel(self):
        if not self.capturing:
            return
        self.capturing = False
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

    def open_settings(self):
        if self.settings is None:
            self.settings = Settings(self)
            self.settings.start()
        self.settings.open()

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

    def _warm_engine(self):
        try:
            self.engine.warmup()
        except Exception:
            log("warmup error " + traceback.format_exc().replace("\n", " | "))
            self.set_ui("done", "语音引擎启动失败", auto_hide=4000)

    def warmup_async(self):
        threading.Thread(target=self._warm_engine, daemon=True).start()
        if self.smart_text:
            def smart_work():
                try:
                    prepare_smart_runtime()
                except Exception:
                    log("smart runtime error " + traceback.format_exc().replace("\n", " | "))
            threading.Thread(target=smart_work, daemon=True).start()

    def start_session(self):
        with self.lock:
            if self.session is not None:
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
        if res == "ok" and (not expected or sent == expected):
            self.set_ui("done", f"✓ 已输入 {len(text)} 字", auto_hide=1500)
            self.start_smart(text, snap["fg"], snap["focus"])
        elif res == "fail" or (expected and sent != expected):
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
            self.set_ui("done", "✓ 已智能整理 · Ctrl+Z 恢复原文", auto_hide=3000)
        elif res in ("window", "focus", "changed", "keys"):
            pass
        else:
            self._smart_notice("智能整理未生效（已保留原文）")

    def on_key_down(self):
        self.start_session()

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
    log(f"hotkey={app.hotkey.display()} config={CONFIG_PATH} autostart={autostart_enabled()} "
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
    if args.settings:
        app.open_settings()
    hint = app.mode_hint()
    if not args.no_ui:
        app.set_ui("info", f"语音输入已就绪：{hint}，Esc 取消", auto_hide=3000)
    say(f"已启动：{hint}。Esc 取消。托盘图标可退出。日志: " + LOG_PATH)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        app.mic.close()


if __name__ == "__main__":
    main()
