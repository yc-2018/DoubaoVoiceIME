# -*- coding: utf-8 -*-
"""API 识别引擎共用的 WebSocket、protobuf、AES、snappy、Opus 实现。"""

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse


def app_dir():
    # onefile 下 __file__ 指向临时解包目录，设备标识要放在 exe 同目录
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = app_dir()
logger = None


def log(message):
    if logger is not None:
        try:
            logger(message)
        except Exception:
            pass


def resource_path(name):
    bases = []
    meipass = getattr(__import__("sys"), "_MEIPASS", None)
    if meipass:
        bases.append(meipass)
    bases.append(APP_DIR)
    bases.append(os.path.dirname(os.path.abspath(getattr(__import__("sys"), "executable", ""))))
    for base in bases:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return ""


def now_ms():
    return int(time.time() * 1000)


def rand_lower(n):
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(n))


def load_data(name):
    path = os.path.join(APP_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def save_data(name, obj):
    path = os.path.join(APP_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except OSError:
        pass


# ---------------------------------------------------------------- protobuf

def uvarint(n):
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


class PBuf:
    def __init__(self):
        self.data = bytearray()

    def v(self, field, value):
        self.data += uvarint(field << 3)
        self.data += uvarint(int(value))
        return self

    def b(self, field, value):
        self.data += uvarint(field << 3 | 2)
        self.data += uvarint(len(value))
        self.data += value
        return self

    def s(self, field, text):
        return self.b(field, text.encode("utf-8"))

    def m(self, field, sub):
        return self.b(field, bytes(sub.data))

    def kv(self, field, key, value):
        return self.m(field, PBuf().s(1, key).s(2, value))


def read_varint(buf, i):
    value = 0
    shift = 0
    while True:
        if i[0] >= len(buf) or shift >= 64:
            raise ValueError("protobuf varint truncated")
        b = buf[i[0]]
        i[0] += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value
        shift += 7


def pb_parse(buf):
    out = []
    i = [0]
    while i[0] < len(buf):
        tag = read_varint(buf, i)
        field = tag >> 3
        kind = tag & 7
        if kind == 0:
            out.append((field, read_varint(buf, i)))
        elif kind in (1, 5):
            i[0] += 8 if kind == 1 else 4
            if i[0] > len(buf):
                raise ValueError("protobuf field truncated")
            out.append((field, None))
        elif kind == 2:
            n = read_varint(buf, i)
            if i[0] + n > len(buf):
                raise ValueError("protobuf field truncated")
            out.append((field, buf[i[0]:i[0] + n]))
            i[0] += n
        else:
            raise ValueError("unsupported protobuf wire type")
    return out


def pb_varint(fields, field):
    for k, v in fields:
        if k == field and isinstance(v, int):
            return v
    return None


def pb_bytes(fields, field):
    for k, v in fields:
        if k == field and isinstance(v, (bytes, bytearray)):
            return bytes(v)
    return None


def pb_string(fields, field):
    raw = pb_bytes(fields, field)
    return raw.decode("utf-8", "replace") if raw is not None else ""


# ------------------------------------------------------------------- snappy

def snappy_compress(data):
    out = bytearray(uvarint(len(data)))
    for i in range(0, len(data), 60):
        chunk = data[i:i + 60]
        out.append((len(chunk) - 1) << 2)
        out += chunk
    return bytes(out)


def snappy_decompress(buf):
    i = [0]
    length = read_varint(buf, i)

    def byte():
        if i[0] >= len(buf):
            raise ValueError("snappy truncated")
        i[0] += 1
        return buf[i[0] - 1]

    out = bytearray()
    while i[0] < len(buf):
        tag = byte()
        kind = tag & 3
        if kind == 0:
            n = tag >> 2
            if n >= 60:
                width = n - 59
                n = 0
                for k in range(width):
                    n |= byte() << (8 * k)
            n += 1
            if i[0] + n > len(buf) or len(out) + n > length:
                raise ValueError("snappy literal overflow")
            out += buf[i[0]:i[0] + n]
            i[0] += n
        else:
            if kind == 1:
                off = ((tag >> 5) << 8) | byte()
                n = ((tag >> 2) & 7) + 4
            elif kind == 2:
                off = byte() | (byte() << 8)
                n = (tag >> 2) + 1
            else:
                off = 0
                for k in range(4):
                    off |= byte() << (8 * k)
                n = (tag >> 2) + 1
            if off == 0 or off > len(out) or len(out) + n > length:
                raise ValueError("snappy copy overflow")
            for _ in range(n):
                out.append(out[-off])
    if len(out) != length:
        raise ValueError("snappy length mismatch")
    return bytes(out)


# ---------------------------------------------------------------------- AES

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_INV_SBOX = bytes.fromhex(
    "52096ad53036a538bf40a39e81f3d7fb7ce339829b2fff87348e4344c4dee9cb"
    "547b9432a6c2233dee4c950b42fac34e082ea16628d924b2765ba2496d8bd125"
    "72f8f66486689816d4a45ccc5d65b6926c704850fdedb9da5e154657a78d9d84"
    "90d8ab008cbcd30af7e45805b8b34506d02c1e8fca3f0f02c1afbd0301138a6b"
    "3a9111414f67dcea97f2cfcef0b4e67396ac7422e7ad3585e2f937e81c75df6e"
    "47f11a711d29c5896fb7620eaa18be1bfc563e4bc6d279209adbc0fe78cd5af4"
    "1fdda8338807c731b11210592780ec5f60517fa919b54a0d2de57a9f93c99cef"
    "a0e03b4dae2af5b0c8ebbb3c83539961172b047eba77d626e169146355210c7d"
)
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D)


def _xtime(b):
    b <<= 1
    return (b ^ 0x1B) & 0xFF if b & 0x100 else b


def _mul(a, b):
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _shift_rows(s, inverse=False):
    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = s[4 * ((c - r) % 4 if inverse else (c + r) % 4) + r]
    return out


def _mix_columns(s, inverse=False):
    out = bytearray(16)
    matrix = ((14, 11, 13, 9), (9, 14, 11, 13), (13, 9, 14, 11), (11, 13, 9, 14)) \
        if inverse else ((2, 3, 1, 1), (1, 2, 3, 1), (1, 1, 2, 3), (3, 1, 1, 2))
    for c in range(4):
        col = s[4 * c:4 * c + 4]
        for r in range(4):
            value = 0
            for k in range(4):
                value ^= _mul(col[k], matrix[r][k])
            out[4 * c + r] = value
    return out


class AES:
    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError("invalid AES key length")
        nk = len(key) // 4
        self.nr = nk + 6
        words = [list(key[i:i + 4]) for i in range(0, len(key), 4)]
        rcon = 1
        for i in range(nk, 4 * (self.nr + 1)):
            t = list(words[i - 1])
            if i % nk == 0:
                t = [_SBOX[b] for b in t[1:] + t[:1]]
                t[0] ^= rcon
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                t = [_SBOX[b] for b in t]
            words.append([words[i - nk][j] ^ t[j] for j in range(4)])
        self.round_keys = [sum(words[4 * r:4 * r + 4], []) for r in range(self.nr + 1)]

    def encrypt_block(self, block):
        s = bytearray(a ^ b for a, b in zip(block, self.round_keys[0]))
        for r in range(1, self.nr):
            s = _shift_rows(bytearray(_SBOX[b] for b in s))
            s = _mix_columns(s)
            s = bytearray(a ^ b for a, b in zip(s, self.round_keys[r]))
        s = _shift_rows(bytearray(_SBOX[b] for b in s))
        return bytes(a ^ b for a, b in zip(s, self.round_keys[self.nr]))

    def decrypt_block(self, block):
        s = bytearray(a ^ b for a, b in zip(block, self.round_keys[self.nr]))
        for r in range(self.nr - 1, 0, -1):
            s = _shift_rows(bytearray(_INV_SBOX[b] for b in s), inverse=True)
            s = bytearray(a ^ b for a, b in zip(s, self.round_keys[r]))
            s = _mix_columns(s, inverse=True)
        s = _shift_rows(bytearray(_INV_SBOX[b] for b in s), inverse=True)
        return bytes(a ^ b for a, b in zip(s, self.round_keys[0]))


def pkcs7_pad(data):
    n = 16 - len(data) % 16
    return data + bytes([n]) * n


def pkcs7_unpad(data):
    if not data or len(data) % 16:
        raise ValueError("invalid AES block length")
    n = data[-1]
    if not 1 <= n <= 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("invalid AES padding")
    return data[:-n]


def aes_ecb_encrypt(key, data):
    cipher = AES(key)
    data = pkcs7_pad(data)
    return b"".join(cipher.encrypt_block(data[i:i + 16]) for i in range(0, len(data), 16))


def aes_ecb_decrypt(key, data):
    cipher = AES(key)
    if not data or len(data) % 16:
        raise ValueError("invalid AES block length")
    plain = b"".join(cipher.decrypt_block(data[i:i + 16]) for i in range(0, len(data), 16))
    return pkcs7_unpad(plain)


def aes_cbc_encrypt(key, iv, data):
    cipher = AES(key)
    data = pkcs7_pad(data)
    prev = iv
    out = bytearray()
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = cipher.encrypt_block(block)
        out += prev
    return bytes(out)


# ------------------------------------------------------------- WebSocket

class WebSocket:
    def __init__(self, sock, leftover=b""):
        self.sock = sock
        self.buf = bytearray(leftover)
        self.send_lock = threading.Lock()
        self.closed = False

    def _read_exact(self, n, deadline):
        while len(self.buf) < n:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TimeoutError("websocket read timeout")
            self.sock.settimeout(remain)
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(bytes(header) + masked)

    def send_binary(self, payload):
        self._send_frame(0x2, payload)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        fragments = None
        while True:
            b1, b2 = self._read_exact(2, deadline)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2, deadline))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8, deadline))[0]
            mask = self._read_exact(4, deadline) if masked else b""
            payload = self._read_exact(n, deadline) if n else b""
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                self.closed = True
                raise ConnectionError("websocket closed by server")
            if opcode in (1, 2):
                if fin:
                    return payload
                fragments = bytearray(payload)
                continue
            if opcode == 0 and fragments is not None:
                fragments += payload
                if fin:
                    return bytes(fragments)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def ws_connect(url, headers=(), timeout=10.0):
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "wss" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    sock = socket.create_connection((host, port), timeout)
    if parts.scheme == "wss":
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in headers:
        lines.append(f"{name}: {value}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("websocket handshake closed")
        data += chunk
        if len(data) > 65536:
            raise ConnectionError("websocket handshake too large")
    head, rest = data.split(b"\r\n\r\n", 1)
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status + b" ":
        raise ConnectionError("websocket handshake failed: " + status.decode("latin-1", "replace"))
    expected = base64.b64encode(hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest())
    if expected not in head:
        raise ConnectionError("websocket accept mismatch")
    return WebSocket(sock, rest)


# --------------------------------------------------------------------- Opus

class OpusEncoder:
    APPLICATION_VOIP = 2048
    APPLICATION_AUDIO = 2049
    SET_BITRATE = 4002
    SET_COMPLEXITY = 4010
    SET_VBR = 4006
    GET_FINAL_RANGE = 4036

    def __init__(self, bitrate=64000, complexity=9, application=APPLICATION_AUDIO,
                 vbr=None):
        path = resource_path("opus.dll")
        if not path:
            raise RuntimeError("缺少 opus.dll")
        self.dll = ctypes.CDLL(path)
        self.dll.opus_encoder_create.restype = ctypes.c_void_p
        self.dll.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.dll.opus_encode.restype = ctypes.c_int
        self.dll.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                         ctypes.c_void_p, ctypes.c_int]
        self.dll.opus_encoder_ctl.restype = ctypes.c_int
        self.dll.opus_encoder_ctl.argtypes = [ctypes.c_void_p]
        self.dll.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        err = ctypes.c_int()
        self.enc = self.dll.opus_encoder_create(16000, 1, application, ctypes.byref(err))
        if not self.enc or err.value != 0:
            raise RuntimeError(f"Opus 编码器创建失败 {err.value}")
        self.dll.opus_encoder_ctl(self.enc, self.SET_BITRATE, bitrate)
        self.dll.opus_encoder_ctl(self.enc, self.SET_COMPLEXITY, complexity)
        if vbr is not None:
            self.dll.opus_encoder_ctl(self.enc, self.SET_VBR, vbr)
        self.buf = ctypes.create_string_buffer(4000)

    def encode(self, pcm):
        samples = len(pcm) // 2
        src = (ctypes.c_int16 * samples).from_buffer_copy(pcm)
        n = self.dll.opus_encode(self.enc, ctypes.cast(src, ctypes.c_void_p), samples,
                                 ctypes.cast(self.buf, ctypes.c_void_p), len(self.buf))
        if n <= 0:
            raise RuntimeError(f"Opus 编码失败 {n}")
        return self.buf.raw[:n]

    def encode_baidu(self, pcm):
        data = self.encode(pcm)
        rng = ctypes.c_uint32()
        self.dll.opus_encoder_ctl(self.enc, self.GET_FINAL_RANGE, ctypes.byref(rng))
        return struct.pack(">II", len(data), rng.value) + data

    def close(self):
        if self.enc:
            self.dll.opus_encoder_destroy(self.enc)
            self.enc = None
