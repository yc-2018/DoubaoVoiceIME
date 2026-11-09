# -*- coding: utf-8 -*-
"""微信输入法云端识别：protobuf over WSS + snappy + AES-256-ECB + secp128r1。"""

import hashlib
import secrets
import threading
import time
import uuid

import api_common as common
from api_common import PBuf, log, now_ms

HOST = "wetype.weixin.qq.com"
SIGN_KEY = "zN7rB3bL4pO8jW1o"
BOOT_KEY = b"D4Y5U3Y2M0C0T7N4P1P7O2N6E1I2Y1U6"
VERSION = "2.2.3(657)"
OS_TYPE = "5"
PLATFORM = "2"
DEVICE_MODEL = "Mac16,12"
CMD_DH = 2147483646
CMD_UIN = 0x7FFFFDFD
CMD_NOTIFY = 8074
CMD_VOICE = 4548
PACKETS_PER_REQUEST = 6
FRAME_BYTES = 640
LEAD = 6400


def _md5_upper(data):
    return hashlib.md5(data).hexdigest().upper()


def _aes_enc(key, data):
    return common.aes_ecb_encrypt(key, data)


def _aes_dec(key, data):
    return common.aes_ecb_decrypt(key, data)


class _PointOps:
    P = 0xFFFFFFFDFFFFFFFFFFFFFFFFFFFFFFFF
    A = 0xFFFFFFFDFFFFFFFFFFFFFFFFFFFFFFFC
    N = 0xFFFFFFFE0000000075A30D1B9038A115
    G = (0x161FF7528B899B2D0C28607CA52C5B86, 0xCF5AC8395BAFEB13C02DA292DDED7A83)

    @classmethod
    def _add_point(cls, p, q):
        if p is None:
            return q
        if q is None:
            return p
        x1, y1 = p
        x2, y2 = q
        if x1 == x2 and (y1 + y2) % cls.P == 0:
            return None
        if p == q:
            slope = (3 * x1 * x1 + cls.A) * pow(2 * y1, cls.P - 2, cls.P) % cls.P
        else:
            slope = (y2 - y1) * pow((x2 - x1) % cls.P, cls.P - 2, cls.P) % cls.P
        x3 = (slope * slope - x1 - x2) % cls.P
        return x3, (slope * (x1 - x3) - y1) % cls.P

    @classmethod
    def _multiply(cls, k, point):
        result = None
        addend = point
        while k:
            if k & 1:
                result = cls._add_point(result, addend)
            addend = cls._add_point(addend, addend)
            k >>= 1
        return result

    @classmethod
    def keypair(cls):
        k = secrets.randbelow(cls.N - 1) + 1
        x, y = cls._multiply(k, cls.G)
        return k, f"04{x:032X}{y:032X}"

    @classmethod
    def shared_x(cls, k, public):
        if len(public) != 66 or not public.startswith("04"):
            raise ValueError("微信服务端公钥无效")
        x = int(public[2:34], 16)
        y = int(public[34:], 16)
        point = cls._multiply(k, (x, y))
        if point is None:
            raise ValueError("微信密钥协商失败")
        return f"{point[0]:032X}"


class WeChatEngine:
    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()

    def warmup(self):
        self.ready.set()

    def set_extra(self, extra):
        pass

    def new_session(self, verbose=False, on_text=None):
        return WeChatSession(self, verbose, on_text)


class WeChatSession:
    def __init__(self, engine, verbose=False, on_text=None):
        self.engine = engine
        self.verbose = verbose
        self.on_text = on_text
        self.text = ""
        self.polished = ""
        self.cancelled = False
        self.closed = False
        self.release_evt = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._audio = bytearray()
        self._last_live = 0.0
        self._client = None
        self._opus = None
        self._sender = None
        self._t0 = 0.0
        self._seq = 0
        self._total = 0
        self._voice_id = uuid.uuid4().hex
        self._error = None

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
        self._close_client()

    def start(self):
        self._t0 = time.monotonic()
        self._opus = common.OpusEncoder(64000, 9)
        self._client = Client.open()
        log(f"wetype connected in {time.monotonic() - self._t0:.2f}s uin={self._client.uin}")
        self._sender = threading.Thread(target=self._send_loop, daemon=True)
        self._sender.start()

    def _voice_packet(self, payload, seq, is_end):
        inner = PBuf().s(2, self._voice_id)
        if payload:
            inner = inner.b(4, payload)
        inner = inner.v(5, 5)
        if is_end:
            inner = inner.v(6, 1)
        inner = inner.v(7, seq)
        if self._total > 0:
            inner = inner.v(11, self._total)
        inner = inner.v(22, 1).v(23, 1).v(24, 1)
        return inner

    def _send(self, opus, seq, is_end):
        packet = self._voice_packet(opus, seq, is_end)
        text, polished = self._client.voice(bytes(packet.data))
        if text:
            self.text = text
            now = time.monotonic()
            if self.on_text is not None and now - self._last_live >= 0.08:
                self._last_live = now
                try:
                    self.on_text(text)
                except Exception:
                    pass
        if polished:
            self.polished = polished

    def _upload(self, packets, is_end):
        self._seq += 1
        framed = bytearray()
        if self._seq == 1:
            framed += b"#!OPUS_RAW_V1"
            framed += b"\x02\x01\x00"
        for packet in packets:
            framed += len(packet).to_bytes(2, "little")
            framed += packet
        self._total += len(framed)
        self._send(bytes(framed), self._seq, is_end)

    def _take_audio(self):
        with self._lock:
            data = bytes(self._audio)
            self._audio.clear()
        return data

    def _encode_available(self, pending, packets):
        while len(pending) >= FRAME_BYTES:
            frame = bytes(pending[:FRAME_BYTES])
            del pending[:FRAME_BYTES]
            packets.append(self._opus.encode(frame))

    def _send_loop(self):
        pending = bytearray(b"\0" * LEAD)
        packets = []
        try:
            while not self._stop.is_set():
                pending += self._take_audio()
                self._encode_available(pending, packets)
                while len(packets) >= PACKETS_PER_REQUEST:
                    self._upload(packets[:PACKETS_PER_REQUEST], False)
                    del packets[:PACKETS_PER_REQUEST]
                if self.release_evt.is_set():
                    break
                if self._stop.wait(0.01):
                    return
            pending += self._take_audio()
            self._encode_available(pending, packets)
            while len(packets) >= PACKETS_PER_REQUEST:
                self._upload(packets[:PACKETS_PER_REQUEST], False)
                del packets[:PACKETS_PER_REQUEST]
            self._upload(packets, True)
            for _ in range(8):
                if self.polished:
                    break
                if self._stop.wait(0.25):
                    return
                self._send(None, 0, True)
            log(f"wetype done {time.monotonic() - self._t0:.2f}s")
        except Exception as exc:
            self._error = exc
            log(f"wetype error {exc!r}")
        finally:
            self._stop.set()

    def finish(self):
        if self.cancelled:
            return
        if self._sender is not None:
            self._sender.join(10.0)
        self._stop.set()
        self._close_client()
        if self.polished:
            self.text = self.polished
        log(f"wetype final after {time.monotonic() - self._t0:.2f}s "
            f"text={self.text!r}")

    def _close_client(self):
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def close(self):
        self.closed = True
        self._stop.set()
        self._close_client()
        if self._sender is not None and self._sender.is_alive():
            self._sender.join(2.0)
        if self._opus is not None:
            try:
                self._opus.close()
            except Exception:
                pass
            self._opus = None


class Client:
    def __init__(self):
        self.ws = None
        self.dead = False
        self.task = 0
        self.uin = "0"
        self.device = ""
        self.session_key = None
        self.server_public = ""
        self.uin_token = ""

    @classmethod
    def open(cls):
        client = cls()
        client.ws = common.ws_connect(f"wss://{HOST}/", [("Sec-WebSocket-Protocol", "wxws_pb")], 10.0)
        try:
            client.handshake()
        except Exception:
            client.close()
            raise
        return client

    def handshake(self):
        self.roundtrip("/timestamp", b"", 0, "1", "")
        saved = common.load_data("wetype.json")
        device = saved.get("device")
        uin = saved.get("uin")
        if isinstance(device, str) and device and isinstance(uin, str) and uin:
            self.device = device
            self.uin = uin
            try:
                self.exchange_key(False)
            except Exception:
                self.register()
        else:
            self.register()
        if saved.get("device") != self.device or saved.get("uin") != self.uin:
            common.save_data("wetype.json", {"device": self.device, "uin": self.uin})
        self.roundtrip("/api_v2", _aes_enc(self.key(), b""), CMD_NOTIFY, "1", "")

    def key(self):
        if self.session_key is None:
            raise RuntimeError("微信会话密钥缺失")
        return self.session_key

    def exchange_key(self, need_uin):
        secret, public = _PointOps.keypair()
        req = PBuf().s(1, public).s(2, public)
        if need_uin:
            req = req.v(3, 1)
        req = req.s(4, self.device)
        if not need_uin:
            req = req.v(5, int(self.uin) if self.uin.isdigit() else 0)
        req = req.s(7, "").s(8, DEVICE_MODEL).s(9, "")
        body = self.request("/oauth_pubkey_v2", _aes_enc(BOOT_KEY, bytes(req.data)), CMD_DH, "")
        fields = common.pb_parse(_aes_dec(BOOT_KEY, body))
        server = common.pb_string(fields, 2)
        token = common.pb_string(fields, 4)
        if not server or (need_uin and not token):
            raise RuntimeError("微信密钥交换不完整")
        self.session_key = _PointOps.shared_x(secret, server).encode("ascii")
        self.server_public = server
        self.uin_token = token

    def register(self):
        self.uin = "0"
        body = "MAC" + "0" * (17 - len(DEVICE_MODEL)) + DEVICE_MODEL + common.rand_lower(12)
        self.device = body + _md5_upper((body + SIGN_KEY).encode("ascii"))
        self.exchange_key(True)
        req = PBuf().s(1, self.device).s(2, self.server_public).s(4, self.uin_token)
        token = self.uin_token
        reply = self.request("/gen_uin_v2", _aes_enc(self.key(), bytes(req.data)), CMD_UIN, token)
        issued = common.pb_varint(common.pb_parse(_aes_dec(self.key(), reply)), 2)
        if not issued:
            raise RuntimeError("微信未签发 UIN")
        self.uin = str(issued)

    def voice(self, inner):
        outer = PBuf().b(1, inner)
        body = _aes_enc(self.key(), common.snappy_compress(bytes(outer.data)))
        reply = self.roundtrip("/api_v2", body, CMD_VOICE, "2", "")
        if reply["status"] != 200 or not reply["body"]:
            raise RuntimeError(f"微信语音请求失败 {reply['status']}")
        plain = _aes_dec(self.key(), reply["body"])
        compress = reply["headers"].get("Kb-CompressionType") or reply["headers"].get("CompressionType")
        if compress == "2":
            plain = common.snappy_decompress(plain)
        top = common.pb_parse(plain)
        payload = common.pb_bytes(top, 1)
        fields = common.pb_parse(payload if payload is not None else plain)
        return common.pb_string(fields, 4), common.pb_string(fields, 14)

    def request(self, path, body, cmd, token):
        reply = self.roundtrip(path, body, cmd, "1", token)
        if reply["status"] != 200 or not reply["body"]:
            raise RuntimeError(f"微信 {path} 状态 {reply['status']}")
        return reply["body"]

    def roundtrip(self, path, body, cmd, compress, token):
        try:
            return self._roundtrip(path, body, cmd, compress, token)
        except Exception:
            self.dead = True
            raise

    def _roundtrip(self, path, body, cmd, compress, token):
        dh = cmd == CMD_DH
        gen_uin = cmd == CMD_UIN
        if not (dh or gen_uin):
            self.task += 1
        task_id = cmd if (dh or gen_uin) else self.task
        trace = common.rand_lower(16)
        digest = _md5_upper(body)
        ts = str(now_ms())
        if dh:
            signed = [OS_TYPE, VERSION, PLATFORM, ts, digest, trace, str(cmd)]
        elif gen_uin:
            signed = [OS_TYPE, VERSION, PLATFORM, ts, digest, token, trace, str(cmd)]
        else:
            signed = [OS_TYPE, VERSION, PLATFORM, str(cmd), "0", ts, digest,
                      self.uin, trace, str(task_id)]
        sign = hashlib.sha256(("".join(signed) + SIGN_KEY).encode("utf-8")).hexdigest().upper()

        headers = []
        if token:
            headers.append(("Kb-GenUinToken", token))
        headers.append(("Kb-Uin", self.uin))
        if dh or gen_uin:
            headers.append(("Kb-DeviceCodeRestrictionV2", "1"))
        if not dh:
            if self.session_key:
                headers.append(("Kb-SharedKeySuffix", self.session_key[-4:].decode("ascii")))
            headers.append(("Kb-CmdId", str(cmd)))
            headers.append(("Kb-SubCmdId", "0"))
        headers += [
            ("Kb-OsType", OS_TYPE), ("Kb-Version", VERSION), ("Kb-SystemVersion", "27.0.0"),
            ("Kb-PackageType", "3"), ("Use_DebugNet", "0"), ("Kb-TimeStamp", ts),
            ("Kb-BodyMd5", digest), ("Kb-TraceId", trace), ("Kb-TaskId", str(task_id)),
            ("Kb-Scene", "2"), ("Kb-Sign", sign), ("Content-Length", str(len(body))),
            ("Kb-CompressionType", compress), ("Content-Type", "application/octet-stream"),
            ("HOST", HOST),
        ]
        http = PBuf().s(1, "POST").s(3, path).s(4, "")
        for key, value in headers:
            http = http.kv(5, key, value)
        http = http.b(6, body)
        frame = PBuf().v(1, 0).v(2, 0).v(3, task_id).m(5, http)
        self.ws.send_binary(bytes(frame.data))
        raw = self.ws.recv(15.0)
        top = common.pb_parse(raw)
        payload = common.pb_bytes(top, 4)
        fields = common.pb_parse(payload) if payload is not None else []
        reply_headers = {}
        for field, value in fields:
            if field == 5 or not isinstance(value, (bytes, bytearray)):
                continue
            try:
                kv = common.pb_parse(bytes(value))
            except ValueError:
                continue
            if (len(kv) >= 2 and isinstance(kv[0][1], (bytes, bytearray))
                    and isinstance(kv[1][1], (bytes, bytearray))):
                reply_headers[kv[0][1].decode("utf-8", "replace")] = kv[1][1].decode("utf-8", "replace")
        return {"status": common.pb_varint(fields, 2) or 0,
                "headers": reply_headers,
                "body": common.pb_bytes(fields, 5) or b""}

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
