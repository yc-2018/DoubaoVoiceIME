# -*- coding: utf-8 -*-
"""讯飞输入法云端识别：IAT WebSocket + HMAC-SHA256，PCM 16k 单声道。"""

import base64
import email.utils
import hashlib
import hmac
import json
import threading
import time
import urllib.parse

import api_common as common
from api_common import log

HOST = "100ime-iat-api.xfyun.cn"
PATH = "/v2/iat"
APPID = "100IME"
API_KEY = "15461402826c8360ebcf1270cbba88e5"
API_SECRET = "sH8v2wSSxxHTy1UNfN6dFaSqVjlmR7BB"
CHUNK = 1280
LEAD = 6400
_BUSINESS = {"language": "zh_cn", "domain": "iat", "accent": "mandarin",
             "vad_eos": 3000, "dwa": "wpgs", "nunum": 1, "ptt": 1}


def _quote(text):
    return urllib.parse.quote(text, safe="-._~")


def _auth_url():
    date = email.utils.formatdate(usegmt=True)
    origin = f"host: {HOST}\ndate: {date}\nGET {PATH} HTTP/1.1"
    digest = hmac.new(API_SECRET.encode("utf-8"), origin.encode("utf-8"),
                      hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("ascii")
    auth_origin = (f'api_key="{API_KEY}", algorithm="hmac-sha256", '
                   f'headers="host date request-line", signature="{signature}"')
    auth = base64.b64encode(auth_origin.encode("utf-8")).decode("ascii")
    return (f"wss://{HOST}{PATH}?authorization={_quote(auth)}"
            f"&date={_quote(date)}&host={_quote(HOST)}")


def _frame(status, audio, first):
    data = {"status": status, "format": "audio/L16;rate=16000",
            "encoding": "raw", "audio": base64.b64encode(audio).decode("ascii")}
    obj = {"common": {"app_id": APPID}, "business": _BUSINESS, "data": data} \
        if first else {"data": data}
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _pgs_text(segs, obj):
    result = (obj.get("data") or {}).get("result")
    if not isinstance(result, dict):
        return None
    sn = result.get("sn")
    if not isinstance(sn, int):
        return None
    text = ""
    for word in result.get("ws") or []:
        candidates = word.get("cw") or []
        if candidates and isinstance(candidates[0].get("w"), str):
            text += candidates[0]["w"]
    if result.get("pgs") == "rpl":
        rg = result.get("rg") or []
        if len(rg) == 2:
            start, end = rg
            for key in [k for k in segs if not (k < start or k > end)]:
                segs.pop(key, None)
    segs[sn] = text
    return "".join(segs[k] for k in sorted(segs))


class IflyEngine:
    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()

    def warmup(self):
        self.ready.set()

    def set_extra(self, extra):
        pass

    def new_session(self, verbose=False, on_text=None):
        return IflySession(self, verbose, on_text)


class IflySession:
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
        self._ws = None
        self._reader = None
        self._sender = None
        self._segs = {}
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
        self._close_ws()

    def start(self):
        self._t0 = time.monotonic()
        self._ws = common.ws_connect(_auth_url(), timeout=10.0)
        log(f"ifly connected in {time.monotonic() - self._t0:.2f}s")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()

    def _send_chunk(self, audio, first):
        self._ws.send_text(_frame(0 if first else 1, audio, first))
        self._sent += len(audio)

    def _send_loop(self):
        pending = bytearray(b"\0" * LEAD)
        next_send = time.monotonic()
        first = True
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._audio:
                        pending += self._audio
                        self._audio.clear()
                while len(pending) >= CHUNK:
                    delay = next_send - time.monotonic()
                    if delay > 0 and self._stop.wait(delay):
                        return
                    self._send_chunk(bytes(pending[:CHUNK]), first)
                    first = False
                    del pending[:CHUNK]
                    next_send = max(next_send + 0.04, time.monotonic() - 0.04)
                if self.release_evt.is_set():
                    break
                if self._stop.wait(0.01):
                    return
            with self._lock:
                if self._audio:
                    pending += self._audio
                    self._audio.clear()
            while len(pending) >= CHUNK:
                self._send_chunk(bytes(pending[:CHUNK]), first)
                first = False
                del pending[:CHUNK]
            if pending:
                self._send_chunk(bytes(pending), first)
                first = False
            self._ws.send_text(_frame(2, b"", first))
            log(f"ifly sent {self._sent} bytes, waiting final")
            self._done.wait(4.0)
        except Exception as exc:
            self._error = exc
            log(f"ifly send error {exc!r}")
        finally:
            self._stop.set()

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                data = self._ws.recv(0.5)
            except TimeoutError:
                continue
            except Exception as exc:
                if not self._stop.is_set() and not self.closed and not self.cancelled:
                    self._error = exc
                    log(f"ifly read error {exc!r}")
                return
            try:
                self._handle(data)
            except Exception:
                log("ifly handle error")

    def _handle(self, data):
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return
        if not isinstance(obj, dict):
            return
        if obj.get("code", 0) != 0:
            self._error = obj.get("message") or "识别失败"
            log(f"ifly server error {obj.get('code')} {self._error}")
            self._done.set()
            return
        text = _pgs_text(self._segs, obj)
        if text is not None and text != self.text:
            self._set_text(text)
        if (obj.get("data") or {}).get("status") == 2:
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
            self._sender.join(8.0)
        self._stop.set()
        self._close_ws()
        if self._error is not None and not self.text:
            log(f"ifly failed: {self._error}")
        log(f"ifly final after {time.monotonic() - self._t0:.2f}s "
            f"text={self.text!r}")

    def _close_ws(self):
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def close(self):
        self.closed = True
        self._stop.set()
        self._close_ws()
        for thread in (self._reader, self._sender):
            if thread is not None and thread.is_alive():
                thread.join(2.0)
