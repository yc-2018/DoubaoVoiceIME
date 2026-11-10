# -*- coding: utf-8 -*-
"""百度输入法云端识别：HTTP 分块上下行 + deflate 首包 + Opus。"""

import http.client
import json
import struct
import threading
import time
import uuid
import zlib

import api_common as common
from api_common import log

HOST = "vse.baidu.com"
BASE = "/v2"
VER = "4.8.3.285"
PID = 597
KEY = "com.baidu.input_nlu"
APP = "com.baidu.input"
PACK = 8
FRAME = 640
LEAD = 6400
_REALTIME_LOG = b'{"realtime-log":"INPUT&a1&13.3.16.2&0&com.baidu.input"}'


def _pkt(typ, payload):
    return struct.pack("<I", len(payload) + 1) + bytes([typ]) + payload


def _start_pkt(sn, cuid):
    obj = {
        "key": KEY, "appid": PID, "pam": "", "vp_pam": "", "wwd": "",
        "ctl": "{\"s_wake\":true,\"s_sdk_vad_mode\":1,\"s_link_switch\":\"ws\"}",
        "cuid": cuid, "sdk_vad_mode": 0, "pfm": "", "ver": VER,
        "use_mapping": 0, "map_multi_info": "", "pid": PID, "smr": 16000,
        "trig": 0, "enable_combined_tts": 1, "sn": sn, "lsn": sn,
        "rgn_id": "", "app": APP, "offline_auth_ctrl": 0, "cid": 0,
        "prop_list": [10005], "debug": 0, "msn": sn, "crt": 0,
        "dep": "{\"pkg\":\"com.apple.mobilenotes\"}", "cookie": "",
        "masr": 0, "midx": 1, "fun": 537198592,
    }
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(raw) + compressor.flush()
    payload = (b"u{" + bytes([8, 0, 0, 0, 0, 0, 0, 3]) + deflated
               + struct.pack("<I", zlib.crc32(raw) & 0xFFFFFFFF)
               + struct.pack("<I", len(raw)))
    return _pkt(0, payload)


def _cuid():
    saved = common.load_data("baidu.json")
    value = saved.get("cuid")
    if isinstance(value, str) and value:
        return value
    value = f"{uuid.uuid4().hex.upper()}|{common.rand_lower(9).upper()}"
    common.save_data("baidu.json", {"cuid": value})
    return value


class BaiduEngine:
    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()

    def warmup(self):
        self.ready.set()

    def set_extra(self, extra):
        pass

    def new_session(self, verbose=False, on_text=None):
        return BaiduSession(self, verbose, on_text)


class BaiduSession:
    def __init__(self, engine, verbose=False, on_text=None):
        self.engine = engine
        self.verbose = verbose
        self.on_text = on_text
        self.text = ""
        self.cancelled = False
        self.closed = False
        self.release_evt = threading.Event()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._audio = bytearray()
        self._last_live = 0.0
        self._sn = uuid.uuid4().hex
        self._cuid = ""
        self._down_conn = None
        self._down_resp = None
        self._up_conn = None
        self._down_thread = None
        self._sender = None
        self._error = None
        self._t0 = 0.0
        self._sent = 0

    def push(self, data):
        if self.cancelled or self.closed:
            return
        with self._lock:
            self._audio += data
            cap = 16000 * 2 * 30
            if len(self._audio) > cap:
                del self._audio[:len(self._audio) - cap]

    def request_release(self):
        self.release_evt.set()

    def cancel(self):
        self.cancelled = True
        self._stop.set()
        self.release_evt.set()
        self._done.set()
        self._close_http()

    def start(self):
        self._t0 = time.monotonic()
        self._cuid = _cuid()
        path = f"{BASE}/down?sn={self._sn}"
        self._down_conn = http.client.HTTPSConnection(HOST, timeout=10)
        self._down_conn.request(
            "POST", path, body=b"",
            headers={"Content-Type": "text/json", "Accept-Encoding": "identity"})
        self._up_conn = http.client.HTTPSConnection(HOST, timeout=10)
        self._up_conn.putrequest("POST", f"{BASE}/up?sn={self._sn}")
        self._up_conn.putheader("Content-Type", "application/octet-stream")
        self._up_conn.putheader("Transfer-Encoding", "chunked")
        self._up_conn.endheaders()
        self._write_up(_start_pkt(self._sn, self._cuid))
        self._write_up(_pkt(5, _REALTIME_LOG))
        log(f"baidu connected in {time.monotonic() - self._t0:.2f}s")
        self._down_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._down_thread.start()
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()

    def _write_up(self, data):
        self._up_conn.send(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")

    def _send_batch(self, batch, first):
        payload = (b"\x44\x00\x00\x00" if first else b"") + b"".join(batch)
        self._write_up(_pkt(1, payload))

    def _send_loop(self):
        pending = bytearray(b"\0" * LEAD)
        next_send = time.monotonic()
        first = True
        encoder = None
        try:
            encoder = common.OpusEncoder(32000, 10, application=common.OpusEncoder.APPLICATION_AUDIO,
                                         vbr=0)
            batch = []
            frames = 0
            while not self._stop.is_set():
                with self._lock:
                    if self._audio:
                        pending += self._audio
                        self._audio.clear()
                while len(pending) >= FRAME:
                    frame = bytes(pending[:FRAME])
                    del pending[:FRAME]
                    batch.append(encoder.encode_baidu(frame))
                    frames += 1
                    if len(batch) >= PACK:
                        delay = next_send - time.monotonic()
                        if delay > 0 and self._stop.wait(delay):
                            return
                        self._send_batch(batch, first)
                        first = False
                        batch = []
                        self._sent += frames * FRAME
                        frames = 0
                        next_send = max(next_send + 0.16, time.monotonic() - 0.16)
                if self.release_evt.is_set():
                    break
                if self._stop.wait(0.01):
                    return
            with self._lock:
                if self._audio:
                    pending += self._audio
                    self._audio.clear()
            while len(pending) >= FRAME:
                batch.append(encoder.encode_baidu(bytes(pending[:FRAME])))
                del pending[:FRAME]
            if pending:
                tail = bytes(pending).ljust(FRAME, b"\0")
                batch.append(encoder.encode_baidu(tail))
            while len(batch) >= PACK:
                self._send_batch(batch[:PACK], first)
                first = False
                del batch[:PACK]
            if batch:
                self._send_batch(batch, first)
            self._write_up(_pkt(9, b""))
            self._write_up(_pkt(7, b""))
            try:
                self._up_conn.send(b"0\r\n\r\n")
            except Exception:
                pass
            log(f"baidu sent {self._sent} bytes, waiting final")
            self._done.wait(8.0)
        except Exception as exc:
            self._error = exc
            log(f"baidu send error {exc!r}")
        finally:
            if encoder is not None:
                try:
                    encoder.close()
                except Exception:
                    pass
            self._stop.set()

    def _read_exact(self, n):
        out = b""
        while len(out) < n:
            chunk = self._down_resp.read(n - len(out))
            if not chunk:
                raise ConnectionError("百度下行关闭")
            out += chunk
        return out

    def _read_loop(self):
        try:
            self._down_resp = self._down_conn.getresponse()
            if self._down_resp.status != 200:
                raise RuntimeError(f"百度下行 HTTP {self._down_resp.status}")
            while not self._stop.is_set():
                head = self._read_exact(4)
                n = struct.unpack("<I", head)[0]
                if n == 0 or n > 1000000:
                    raise RuntimeError("百度下行帧长度异常")
                frame = self._read_exact(n)
                self._handle(frame[0], frame[1:])
                if self._done.is_set():
                    break
        except Exception as exc:
            if not self._stop.is_set() and not self.closed and not self.cancelled:
                self._error = exc
                log(f"baidu read error {exc!r}")
        finally:
            self._done.set()

    def _handle(self, typ, body):
        if typ in (243, 244) or not body:
            if typ == 243:
                self._done.set()
            return
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return
        if not isinstance(obj, dict):
            return
        result = obj.get("result")
        if isinstance(result, dict):
            words = result.get("word") or []
            if words and isinstance(words[0], str) and words[0] != self.text:
                self._set_text(words[0])
        err_no = obj.get("err_no") or 0
        if err_no != 0 and not self.text:
            self._error = obj.get("err_msg") or "识别失败"
            log(f"baidu server error {err_no} {self._error}")
            self._done.set()
        if obj.get("result_type") == "TS_RESULT_TYPE_ONEBSET":
            self._done.set()

    def _set_text(self, text):
        self.text = text
        now = time.monotonic()
        if self.on_text is not None and now - self._last_live >= 0.08:
            self._last_live = now
            try:
                self.on_text(text)
            except Exception:
                pass

    def finish(self):
        if self.cancelled:
            return
        if self._sender is not None:
            self._sender.join(12.0)
        self._stop.set()
        self._close_http()
        if self._error is not None and not self.text:
            log(f"baidu failed: {self._error}")
        log(f"baidu final after {time.monotonic() - self._t0:.2f}s "
            f"text={self.text!r}")

    def _close_http(self):
        for conn in (self._up_conn, self._down_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def close(self):
        self.closed = True
        self._stop.set()
        self._close_http()
        for thread in (self._down_thread, self._sender):
            if thread is not None and thread.is_alive():
                thread.join(2.0)
