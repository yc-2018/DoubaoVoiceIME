# -*- coding: utf-8 -*-
"""豆包输入法云端识别：protobuf over WSS + 匿名设备注册 + Opus。"""

import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import uuid

import api_common as common
from api_common import PBuf, log, now_ms

WS_URL = "wss://frontier-audio-ime-ws.doubao.com/ocean/api/v1/ws"
APP_ID = "401734"
OK = 20000000
APP_KEY = "OrnqKvSSrs"
FIRST = 1
MIDDLE = 3
LAST = 9
FRAME = 640

_APP = (("aid", APP_ID), ("app_name", "oime"), ("channel", "official"),
        ("version_code", "100406010"), ("version_name", "1.4.6"),
        ("manifest_version_code", "100406010"), ("update_version_code", "100406010"),
        ("package", "com.bytedance.android.doubaoime"))
_PHONES = (
    ("google", "Pixel 7 Pro", "16", "36", "BP2A.250605.031.A2", "1080*2340", "420"),
    ("google", "Pixel 8", "15", "35", "AP4A.250205.002", "1080*2400", "420"),
    ("google", "Pixel 9", "15", "35", "AP4A.250205.002", "1080*2424", "420"),
    ("google", "Pixel 6", "14", "34", "AP2A.240805.005", "1080*2400", "420"),
    ("samsung", "SM-S9210", "14", "34", "UP1A.231005.007", "1080*2340", "480"),
    ("samsung", "SM-S9280", "14", "34", "UP1A.231005.007", "1080*2340", "450"),
    ("samsung", "SM-S9180", "14", "34", "UP1A.231005.007", "1080*2316", "450"),
    ("Xiaomi", "23127PN0CC", "14", "34", "UKQ1.230804.001", "1200*2670", "440"),
    ("Xiaomi", "2211133C", "14", "34", "UKQ1.230804.001", "1080*2400", "440"),
    ("Xiaomi", "24129PN74C", "15", "35", "AQ3A.240912.001", "1200*2670", "440"),
    ("Redmi", "23090RA98C", "14", "34", "UKQ1.231003.002", "1220*2712", "440"),
    ("Redmi", "23113RKC6C", "14", "34", "UKQ1.231003.002", "1440*3200", "560"),
    ("vivo", "V2309A", "14", "34", "UP1A.231005.007", "1260*2800", "480"),
    ("vivo", "V2324A", "14", "34", "UP1A.231005.007", "1260*2800", "480"),
    ("OPPO", "PHZ110", "14", "34", "UKQ1.231108.001", "1264*2780", "480"),
    ("OnePlus", "PJD110", "14", "34", "UKQ1.230924.001", "1440*3168", "560"),
    ("OnePlus", "PJE110", "14", "34", "UKQ1.230924.001", "1264*2780", "480"),
)
_PHONE_KEYS = ("brand", "model", "os", "api", "build", "resolution", "dpi")
_DATA_FILE = "doubao_api.json"


def _phone():
    return dict(zip(_PHONE_KEYS, secrets.choice(_PHONES)))


def _ua(phone):
    return ("com.bytedance.android.doubaoime/100406010 (Linux; U; Android {}; en_US; {}; "
            "Build/{}; Cronet/TTNetVersion:94cf429a 2025-11-17 QuicVersion:1f89f732 2025-05-08"
            ).format(phone["os"], phone["model"], phone["build"])


def _params(phone):
    items = list(_APP) + [
        ("device_platform", "android"), ("os", "android"), ("os_api", phone["api"]),
        ("os_version", phone["os"]), ("device_type", phone["model"]),
        ("device_brand", phone["brand"]), ("device_model", phone["model"]),
        ("resolution", phone["resolution"]), ("dpi", phone["dpi"]),
        ("language", "zh"), ("timezone", "8"), ("access", "wifi"),
        ("rom", phone["build"]), ("rom_version", phone["build"]),
    ]
    return items


def _register():
    phone = _phone()
    cdid = str(uuid.uuid4())
    now = str(now_ms())
    params = _params(phone)
    header = dict(params)
    header.update({
        "device_id": 0, "install_id": 0, "cdid": cdid,
        "openudid": format(secrets.randbits(64), "016x"),
        "clientudid": str(uuid.uuid4()), "region": "CN",
        "tz_name": "Asia/Shanghai", "tz_offset": 28800, "sim_region": "cn",
        "carrier_region": "cn", "cpu_abi": "arm64-v8a", "build_serial": "unknown",
        "not_request_sender": 0, "sig_hash": "", "google_aid": "", "mc": "",
        "serial_number": "",
    })
    body = json.dumps({"magic_tag": "ss_app_log", "header": header,
                       "_gen_time": int(now)}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    query = params + [("ssmix", "a"), ("_rticket", now), ("cdid", cdid), ("ac", "wifi")]
    url = ("https://log-klink.zijieapi.com/service/2/device_register/?"
           + urllib.parse.urlencode(query))
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": _ua(phone),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        obj = json.loads(resp.read().decode("utf-8", "replace"))
    did = obj.get("device_id_str")
    if not did:
        did = obj.get("device_id")
    did = str(did or "")
    if not did or did == "0":
        raise RuntimeError("豆包设备注册被拒绝")
    return {"did": did, "phone": phone}


def _load_device():
    saved = common.load_data(_DATA_FILE)
    did = saved.get("did")
    phone = saved.get("phone")
    if isinstance(did, str) and did and isinstance(phone, dict) \
            and all(k in phone for k in _PHONE_KEYS):
        return {"did": did, "phone": phone}
    device = _register()
    common.save_data(_DATA_FILE, device)
    return device


def _reset_device():
    try:
        os.remove(os.path.join(common.APP_DIR, _DATA_FILE))
    except OSError:
        pass


def _request(token, method, payload="", audio=b"", rid="", frame=0):
    pb = PBuf()
    if token:
        pb.s(2, token)
    pb.s(3, "ASR").s(5, method)
    if payload:
        pb.s(6, payload)
    if audio:
        pb.b(7, audio)
    pb.s(8, rid)
    if frame:
        pb.v(9, frame)
    return bytes(pb.data)


def _parse_response(data):
    fields = common.pb_parse(data)
    status = common.pb_varint(fields, 5) or 0
    if status >= 1 << 31:
        status -= 1 << 32
    return {"event": common.pb_string(fields, 4),
            "status": status,
            "message": common.pb_string(fields, 6),
            "result": common.pb_string(fields, 7)}


def _cjk(ch):
    code = ord(ch)
    return (0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
            or 0xFF00 <= code <= 0xFFEF)


def _join_sentences(parts):
    out = ""
    for text in parts:
        if not text:
            continue
        if out and not _cjk(out[-1]) and not _cjk(text[0]):
            out += " "
        out += text
    return out


class DoubaoApiEngine:
    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()

    def warmup(self):
        self.ready.set()

    def set_extra(self, extra):
        pass

    def new_session(self, verbose=False, on_text=None):
        return DoubaoApiSession(self, verbose, on_text)


class DoubaoApiSession:
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
        self._rid = ""
        self._sentences = {}
        self._current = ""
        self._next_index = 0
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
        try:
            self._handshake()
        except Exception as first:
            _reset_device()
            log(f"doubao api handshake failed, retry with new device: {first!r}")
            self._close_ws()
            self._handshake()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()

    def _handshake(self):
        device = _load_device()
        url = (f"{WS_URL}?aid={APP_ID}&device_id="
               f"{urllib.parse.quote(device['did'])}")
        headers = [("User-Agent", _ua(device["phone"])),
                   ("proto-version", "v2"), ("x-custom-keepalive", "true")]
        self._ws = common.ws_connect(url, headers, timeout=10.0)
        self._rid = str(uuid.uuid4())
        self._ws.send_binary(_request(APP_KEY, "StartTask", rid=self._rid))
        self._expect("TaskStarted")
        session = json.dumps({
            "audio_info": {"channel": 1, "format": "speech_opus", "sample_rate": 16000},
            "enable_punctuation": True,
            "enable_speech_rejection": True,
            "extra": {"app_name": "oime", "cell_compress_rate": 8,
                      "did": device["did"], "enable_asr_threepass": False,
                      "enable_asr_twopass": True, "input_mode": "stream"},
        }, separators=(",", ":"))
        self._ws.send_binary(_request(APP_KEY, "StartSession", session, rid=self._rid))
        self._expect("SessionStarted")
        log(f"doubao api connected in {time.monotonic() - self._t0:.2f}s "
            f"did={device['did']}")

    def _expect(self, event):
        deadline = time.monotonic() + 10.0
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise RuntimeError(f"豆包 {event} 超时")
            resp = _parse_response(self._ws.recv(remain))
            if resp["event"].endswith("Failed"):
                raise RuntimeError(f"豆包 {resp['event']} {resp['status']} {resp['message']}")
            if resp["event"] == event:
                if resp["status"] != OK:
                    raise RuntimeError(f"豆包 {event} {resp['status']} {resp['message']}")
                return

    def _meta(self, index, ts0):
        return json.dumps({"extra": {}, "timestamp_ms": ts0 + index * 20},
                          separators=(",", ":"))

    def _send_loop(self):
        pending = bytearray()
        next_send = time.monotonic()
        encoder = None
        index = 0
        ts0 = now_ms()
        try:
            encoder = common.OpusEncoder(16000, 5,
                                         application=common.OpusEncoder.APPLICATION_VOIP)
            while not self._stop.is_set():
                with self._lock:
                    if self._audio:
                        pending += self._audio
                        self._audio.clear()
                while len(pending) >= FRAME:
                    delay = next_send - time.monotonic()
                    if delay > 0 and self._stop.wait(delay):
                        return
                    frame = bytes(pending[:FRAME])
                    del pending[:FRAME]
                    packet = encoder.encode(frame)
                    kind = FIRST if index == 0 else MIDDLE
                    self._ws.send_binary(_request("", "TaskRequest", self._meta(index, ts0),
                                                  packet, self._rid, kind))
                    index += 1
                    self._sent += len(frame)
                    next_send = max(next_send + 0.02, time.monotonic() - 0.02)
                if self.release_evt.is_set():
                    break
                if self._stop.wait(0.01):
                    return
            with self._lock:
                if self._audio:
                    pending += self._audio
                    self._audio.clear()
            while len(pending) >= FRAME:
                packet = encoder.encode(bytes(pending[:FRAME]))
                del pending[:FRAME]
                kind = FIRST if index == 0 else MIDDLE
                self._ws.send_binary(_request("", "TaskRequest", self._meta(index, ts0),
                                              packet, self._rid, kind))
                index += 1
            if pending:
                tail = bytes(pending).ljust(FRAME, b"\0")
                kind = FIRST if index == 0 else MIDDLE
                self._ws.send_binary(_request("", "TaskRequest", self._meta(index, ts0),
                                              encoder.encode(tail), self._rid, kind))
                index += 1
            self._ws.send_binary(_request("", "TaskRequest", self._meta(index, ts0),
                                          b"", self._rid, LAST))
            self._ws.send_binary(_request(APP_KEY, "FinishSession", rid=self._rid))
            log(f"doubao api sent {self._sent} bytes, waiting final")
            self._done.wait(10.0 + index / 200.0)
        except Exception as exc:
            self._error = exc
            log(f"doubao api send error {exc!r}")
        finally:
            if encoder is not None:
                try:
                    encoder.close()
                except Exception:
                    pass
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
                    log(f"doubao api read error {exc!r}")
                return
            try:
                resp = _parse_response(data)
            except Exception:
                continue
            if resp["result"]:
                text = self._update(resp["result"])
                if text is not None and text != self.text:
                    self._set_text(text)
            if resp["event"].endswith("Failed"):
                self._error = RuntimeError(
                    f"豆包 {resp['event']} {resp['status']} {resp['message']}")
                log(f"doubao api server error {resp['event']} {resp['message']}")
                self._done.set()
                return
            if resp["event"] == "SessionFinished":
                self._done.set()
                return

    def _update(self, raw):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return None
        results = obj.get("results") if isinstance(obj, dict) else None
        if not isinstance(results, list):
            return None
        if not any(isinstance(r, dict) and "index" in r for r in results):
            first = results[0] if results else None
            text = first.get("text") if isinstance(first, dict) else None
            if not isinstance(text, str) or not text:
                return None
            self._sentences.clear()
            self._current = text
            return text
        for result in results:
            if not isinstance(result, dict):
                continue
            text = result.get("text")
            if not isinstance(text, str) or not text:
                continue
            extra = result.get("extra") or {}
            fin = (extra.get("nonstream_result") is True
                   or (result.get("is_interim") is False
                       and result.get("is_vad_finished") is True))
            if fin:
                index = result.get("index", self._next_index)
                self._sentences[index] = text
                self._next_index = max(self._next_index, index + 1)
                self._current = ""
            else:
                self._current = text
        parts = [self._sentences[k] for k in sorted(self._sentences)]
        if self._current:
            parts.append(self._current)
        return _join_sentences(parts)

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
            self._sender.join(15.0)
        self._stop.set()
        self._close_ws()
        final = _join_sentences([self._sentences[k] for k in sorted(self._sentences)]
                                + ([self._current] if self._current else []))
        if final:
            self.text = final
        if self._error is not None and not self.text:
            log(f"doubao api failed: {self._error}")
            _reset_device()
        log(f"doubao api final after {time.monotonic() - self._t0:.2f}s "
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
