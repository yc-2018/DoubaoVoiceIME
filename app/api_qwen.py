# -*- coding: utf-8 -*-
"""千问输入法云端识别：WSS + protobuf 帧，PCM 16k 单声道。"""

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import uuid

import api_common as common
from api_common import PBuf, log, now_ms

VE = "1.2.35.46"
ORIGIN = "https://www.qianwen.com"
HMAC_KEY = b"2d473b000fdb53e617446f805d4eaaacbb324bd16ee9f6d8237abd0ab79c5e48"
AES_KEY = b"a0a6237b2b735a54"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 TONGYI_DESKTOP/0.1.0 QuarkPC/ime_voice")
CHUNK = 3840
LEAD = 6400
_DEBUG = bool(os.environ.get("QWEN_DEBUG"))


def _debug(tag, data):
    if _DEBUG:
        log(f"qwen {tag} {data[:800]!r}")


def _qenc(text):
    return urllib.parse.quote(text, safe="-._~")


def _utdid():
    obj = common.load_data("qwen.json")
    utdid = obj.get("utdid")
    if isinstance(utdid, str) and utdid:
        return utdid
    utdid = "VK" + uuid.uuid4().hex.upper()[:20]
    common.save_data("qwen.json", {"utdid": utdid})
    return utdid


class QwenAuth:
    def __init__(self):
        packed = b"\x4e\xa4" + common.aes_cbc_encrypt(AES_KEY, AES_KEY, _utdid().encode("ascii"))
        self.ut = base64.b64encode(packed).decode("ascii")
        self.chid = f"{uuid.uuid4()}_1"
        self.reqt = str(now_ms())
        key = f"{self.ut}{VE}{self.chid}{self.reqt}".encode("utf-8")
        self.sign = "4ea4" + hmac.new(HMAC_KEY, key, hashlib.sha1).hexdigest()

    def query(self):
        return (f"chid={self.chid}&biz_id=ai_qwen_input&from=kkframenew_quark_asr"
                f"&uc_param_str=vepffrprsvchut&ut={_qenc(self.ut)}&ve={VE}&pf=8002&pr=qwen"
                f"&fr=mac&sv=release&ch=qianwen-ime")

    def headers(self):
        return [
            ("clt-acs-ut", _qenc(self.ut)),
            ("clt-acs-ve", VE),
            ("clt-acs-kp", ""),
            ("clt-acs-reqt", self.reqt),
            ("clt-acs-wsgnver", "1"),
            ("clt-acs-request-params", "chid"),
            ("clt-acs-sign", self.sign),
            ("clt-acs-caer", "tlbe"),
            ("x-wpk-reqid", self.chid),
        ]


class QwenEngine:
    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()

    def warmup(self):
        self.ready.set()

    def set_extra(self, extra):
        pass

    def new_session(self, verbose=False, on_text=None):
        return QwenSession(self, verbose, on_text)


class QwenSession:
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
        self._auth = None
        self._ws = None
        self._reader = None
        self._sender = None
        self._session_id = ""
        self._round_id = ""
        self._sent = 0
        self._t0 = 0.0

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
        self._auth = QwenAuth()
        url = "wss://voice-input.qianwen.com/ws/v1/voice?" + self._auth.query()
        headers = [("Cache-Control", "no-cache"), ("User-Agent", UA), ("Origin", ORIGIN)]
        headers += self._auth.headers()
        self._ws = common.ws_connect(url, headers, timeout=10.0)
        log(f"qwen connected in {time.monotonic() - self._t0:.2f}s")
        self._attach()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()

    def _attach(self):
        reqt = str(now_ms())
        asr = {
            "body": {"bitDepth": "16", "channel": "mono", "format": "pcm",
                     "maxEndSilence": "180000", "maxStartSilence": "180000",
                     "sampleRate": "16000", "type": "manualStreamStop"},
            "chid": self._auth.chid,
            "header": {"clt-acs-reqt": reqt, "ai_polish_mode": "off"},
            "param": {},
            "route": "/app/live/init",
        }
        pipe = {
            "biz_data": {}, "biz_id": "ai_command", "chat_client": "native",
            "client_tm": reqt, "endpoint_config": {}, "from": "kkframenew_quark_asr",
            "scene": "voice_input_assistant", "ai_polish_mode": "off",
        }
        packet = (PBuf().s(1, "/voice_assistant/channel/attach").s(2, self._auth.chid)
                  .kv(3, "clt-acs-reqt", reqt)
                  .kv(5, "asrConfig", json.dumps(asr, separators=(",", ":")))
                  .kv(5, "pipelineContext", json.dumps(pipe, separators=(",", ":")))
                  .kv(5, "trigger", "local_device"))
        self._ws.send_binary(bytes(packet.data))
        deadline = time.monotonic() + 8.0
        while not self._session_id and time.monotonic() < deadline:
            try:
                self._handle(self._ws.recv(min(2.0, deadline - time.monotonic())))
            except TimeoutError:
                continue
        if not self._session_id:
            raise RuntimeError("千问连接未返回 sessionId")
        log(f"qwen attached {self._session_id} round={self._round_id}")

    def _base(self, route):
        return (PBuf().s(1, route).s(2, self._auth.chid)
                .kv(3, "clt-acs-reqt", str(now_ms()))
                .kv(4, "roundId", self._round_id)
                .kv(4, "sessionId", self._session_id))

    def _send_chunk(self, pcm):
        audio = PBuf().s(1, "audio").s(2, "pcm").b(3, pcm)
        packet = self._base("/voice_assistant/channel/asr/send").kv(
            5, "streamInputState", "process").m(6, audio)
        self._ws.send_binary(bytes(packet.data))
        self._sent += len(pcm)

    def _complete(self):
        packet = self._base("/voice_assistant/channel/asr/complete")
        self._ws.send_binary(bytes(packet.data))

    def _send_loop(self):
        pending = bytearray(b"\0" * LEAD)
        next_send = time.monotonic()
        try:
            while not self._stop.is_set():
                with self._lock:
                    if self._audio:
                        pending += self._audio
                        self._audio.clear()
                while len(pending) >= CHUNK:
                    delay = next_send - time.monotonic()
                    if delay > 0:
                        if self._stop.wait(delay):
                            return
                    self._send_chunk(bytes(pending[:CHUNK]))
                    del pending[:CHUNK]
                    next_send = max(next_send + 0.12, time.monotonic() - 0.12)
                if self.release_evt.is_set():
                    break
                if self._stop.wait(0.01):
                    return
            with self._lock:
                if self._audio:
                    pending += self._audio
                    self._audio.clear()
            while len(pending) >= CHUNK:
                self._send_chunk(bytes(pending[:CHUNK]))
                del pending[:CHUNK]
            if pending:
                tail = bytes(pending) + b"\0" * (CHUNK - len(pending))
                self._send_chunk(tail)
            self._complete()
            log(f"qwen sent {self._sent} bytes, waiting final")
            self._done.wait(2.0)
        except Exception as exc:
            log(f"qwen send error {exc!r}")
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
                    log(f"qwen read error {exc!r}")
                return
            try:
                self._handle(data)
            except Exception:
                common.log("qwen handle error")

    def _handle(self, data):
        _debug("down", data)
        try:
            obj = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, UnicodeError):
            return
        if not isinstance(obj, dict):
            return
        info = obj.get("data")
        if isinstance(info, dict):
            sid = info.get("sessionId")
            if isinstance(sid, str) and sid:
                if not self._session_id:
                    self._session_id = sid
                    self._round_id = info.get("roundId") or f"ro_{sid}_0"
                new_round = info.get("newRoundId")
                if new_round:
                    self._round_id = new_round
                    self._done.set()
                    self._stop.set()
        self._walk(obj)

    def _walk(self, value):
        if isinstance(value, dict):
            content = value.get("content")
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    self._set_text(text)
            for item in value.values():
                self._walk(item)
        elif isinstance(value, list):
            for item in value:
                self._walk(item)

    def _set_text(self, text):
        self.text = text
        now = time.monotonic()
        if self.on_text is not None and (now - self._last_live >= 0.08):
            self._last_live = now
            try:
                self.on_text(text)
            except Exception:
                pass

    def finish(self):
        if self.cancelled:
            return
        if self._sender is not None:
            self._sender.join(6.0)
        self._stop.set()
        self._close_ws()
        log(f"qwen final after {time.monotonic() - self._t0:.2f}s "
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
