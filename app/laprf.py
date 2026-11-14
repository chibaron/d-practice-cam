"""LapRF プロトコルで通過を知らせる TCP サーバー（LiveTime などが接続する）。ImmersionRC LapRF のふりをする。

ゲート通過（ラップ確定）を DETECTION で全クライアントに送る。問い合わせ（DESCRIPTOR・RF_SETTINGS・TIME）に答え、
STATUS を定期的に送る。計測には関係しないので、失敗しても計測は止めない。
送信は専用スレッドで行い、取り込みスレッドを待たせない。

パケット: 0x5a, 長さ(2), CRC(2), メッセージID(2), [フィールドID(1), サイズ(1), 値]..., 0x5b（値はリトルエンディアン）。
長さは 0x5a から 0x5b まで。CRC は CRC-16/ARC で、CRC 欄を 0 にして全体から計算する。
0x5a と 0x5b の間で上位5ビットが 0x5c と同じバイト（0x58〜0x5f）は、0x5c, 値+0x40 に置き換える。
"""
from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

DEFAULT_PORT = 5403
START, END, ESCAPE = 0x5a, 0x5b, 0x5c
ESCAPE_MASK = 0xf8
ESCAPE_ADD = 0x40
MAX_PACKET = 1500

MSG_RF_SETTINGS = 0xda02
MSG_DESCRIPTOR = 0xda08
MSG_DETECTION = 0xda09
MSG_STATUS = 0xda0a
MSG_TIME = 0xda0c

SLOTS = 8                   # LapRF のスロット数。受信器の ch を前から割り当て、残りは無効にする
STATUS_SEC = 1.5            # STATUS を送る間隔
TIMEOUT_SEC = 1.0           # ソケットの待ち時間。これ以上送れないクライアントは切る
# 実機の値が決まっていないものは、LiveTime で動いている実装（TinyViewPlus_Livetime）と同じ値にする
DECODER_ID = 0x002f0022
PEAK_HEIGHT = 0x0b3e
GAIN = 0x23
THRESHOLD = 800.0
BATTERY_MV = 0x101b
RSSI = 960.0
DESCRIPTOR_BODY = bytes((0x20, 0x04, 0x06, 0x00, 0x00, 0x01, 0x21, 0x01, 0x02))

# 通信で使うバンドの番号と、各 ch の周波数（MHz）。バンド名（F1 など）の1文字目で引く
BANDS = {
    "L": (0, (5362, 5399, 5436, 5473, 5510, 5547, 5584, 5621)),
    "F": (1, (5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880)),
    "R": (2, (5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917)),
    "E": (3, (5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945)),
    "B": (4, (5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866)),
    "A": (5, (5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725)),
}


def band_info(name: str) -> tuple[int, int, int] | None:
    """バンド名（F1 など）を (バンド番号, ch 1〜8, 周波数) にする。分からない名前は None。"""
    name = str(name).strip().upper()
    if len(name) != 2 or name[0] not in BANDS or name[1] not in "12345678":
        return None
    index, freqs = BANDS[name[0]]
    return index, int(name[1]), freqs[int(name[1]) - 1]


def crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xa001 if crc & 1 else crc >> 1
    return crc


def field(field_id: int, fmt: str, value) -> bytes:
    """fmt は struct の形式（B, H, I, Q, f）。"""
    return bytes((field_id, struct.calcsize(fmt))) + struct.pack("<" + fmt, value)


def build_packet(msg_id: int, body: bytes = b"") -> bytes:
    """エスケープ前のパケット。"""
    length = 7 + len(body) + 1
    raw = bytearray(struct.pack("<BHHH", START, length, 0, msg_id) + body + bytes((END,)))
    raw[3:5] = struct.pack("<H", crc16(raw))
    return bytes(raw)


def escape(packet: bytes) -> bytes:
    out = bytearray((START,))
    for b in packet[1:-1]:
        if b & ESCAPE_MASK == ESCAPE & ESCAPE_MASK:
            out += bytes((ESCAPE, b + ESCAPE_ADD))
        else:
            out.append(b)
    out.append(END)
    return bytes(out)


def encode(msg_id: int, body: bytes = b"") -> bytes:
    return escape(build_packet(msg_id, body))


class PacketParser:
    """受信したバイト列からパケットを取り出す（TCP なので、分かれても続けて届いてもよい）。"""

    def __init__(self):
        self.buf = None         # 0x5a を見つけるまで None
        self.escaped = False

    def feed(self, data: bytes) -> list[bytes]:
        packets = []
        for b in data:
            if self.buf is None:
                if b == START:
                    self.buf = bytearray((b,))
                    self.escaped = False
            elif self.escaped:
                self.buf.append((b - ESCAPE_ADD) & 0xff)
                self.escaped = False
            elif b == ESCAPE:
                self.escaped = True
            elif b == START:        # 途中で切れたパケットは捨てて、次から読む
                self.buf = bytearray((b,))
            else:
                self.buf.append(b)
                if b == END:
                    packets.append(bytes(self.buf))
                    self.buf = None
            if self.buf is not None and len(self.buf) > MAX_PACKET:
                self.buf = None
        return packets


def message_id(packet: bytes) -> int | None:
    """パケットのメッセージID。短すぎれば None。"""
    return struct.unpack_from("<H", packet, 5)[0] if len(packet) >= 8 else None


def parse_fields(packet: bytes) -> list[tuple[int, bytes]]:
    """パケットの [(フィールドID, 値のバイト列)]。"""
    out, i, end = [], 7, len(packet) - 1
    while i + 2 <= end:
        size = packet[i + 1]
        out.append((packet[i], packet[i + 2:i + 2 + size]))
        i += 2 + size
    return out


def rtc_micros(t_monotonic: float) -> int:
    """time.monotonic() の時刻を、LapRF の RTC_TIME（マイクロ秒）にする。"""
    return int(t_monotonic * 1_000_000)


class LapRFServer:
    def __init__(self, port: int = DEFAULT_PORT, bands_fn=lambda: [], host: str = "0.0.0.0"):
        """bands_fn はいまの受信器のバンド名のリスト（ch 順）を返す。"""
        self.host, self.port = host, port
        self.bands_fn = bands_fn
        self._stop = threading.Event()
        self._q: queue.Queue = queue.Queue()    # (送り先 or None=全員, バイト列)
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._counts = [0] * SLOTS              # スロットごとの通過の通し番号
        self._listener = None
        self._threads = []

    # ---------- 外から呼ぶ ----------
    def start(self) -> bool:
        """待ち受けを始める。ポートを開けなければ False（計測には関係しない）。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind((self.host, self.port))
            s.listen(4)
            s.settimeout(TIMEOUT_SEC / 2)
        except OSError as e:
            log.warning("LapRF: cannot listen on port %d: %s", self.port, e)
            return False
        self._listener = s
        self.port = s.getsockname()[1]
        for target, name in ((self._accept_run, "laprf-accept"), (self._send_run, "laprf-send")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        log.info("LapRF: listening on port %d", self.port)
        return True

    def stop(self) -> None:
        self._stop.set()
        self._q.put((None, b""))
        for t in self._threads:
            t.join(timeout=TIMEOUT_SEC * 2)
        if self._listener:
            self._listener.close()
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            c.close()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def passing(self, ch: int, t_pass: float) -> None:
        """ゲート通過を全クライアントに知らせる。t_pass は time.monotonic() 基準の通過時刻。"""
        if not 0 <= ch < SLOTS:
            return
        self._counts[ch] += 1
        body = field(0x20, "I", DECODER_ID) + field(0x01, "B", ch + 1) + field(0x21, "I", self._counts[ch]) \
            + field(0x02, "Q", rtc_micros(t_pass)) + field(0x22, "H", PEAK_HEIGHT) + field(0x23, "H", 0)
        self._q.put((None, encode(MSG_DETECTION, body)))

    # ---------- 返事 ----------
    def _slot_bands(self) -> list:
        """スロットごとの band_info（使っていないスロットは None）。"""
        infos = [band_info(b) for b in list(self.bands_fn())[:SLOTS]]
        return infos + [None] * (SLOTS - len(infos))

    def _rf_settings(self) -> list[bytes]:
        out = []
        for i, info in enumerate(self._slot_bands()):
            band, channel, freq = info or (2, i + 1, BANDS["R"][1][i])     # 無効なスロットはレースバンドの値を入れておく
            out.append(encode(MSG_RF_SETTINGS, field(0x01, "B", i + 1) + field(0x20, "H", 1 if info else 0)
                              + field(0x22, "H", band) + field(0x21, "H", channel) + field(0x24, "H", GAIN)
                              + field(0x23, "f", THRESHOLD) + field(0x25, "H", freq)))
        return out

    def _status(self) -> bytes:
        body = field(0x21, "H", BATTERY_MV) + field(0x23, "B", 1) + field(0x24, "I", 0)
        for i, info in enumerate(self._slot_bands()):
            body += field(0x01, "B", i + 1) + field(0x22, "f", RSSI if info else 0.0)
        return encode(MSG_STATUS, body + field(0x03, "H", 4))

    def replies(self, packet: bytes) -> list[bytes]:
        """受け取ったパケットへの返事。LiveTime が設定を書き込んできても変えず、いまの設定を返す。"""
        msg = message_id(packet)
        if msg == MSG_DESCRIPTOR:
            return [encode(MSG_DESCRIPTOR, DESCRIPTOR_BODY)]
        if msg == MSG_RF_SETTINGS:
            return self._rf_settings()
        if msg == MSG_TIME:
            return [encode(MSG_TIME, field(0x02, "Q", rtc_micros(time.monotonic())))]
        return []

    # ---------- スレッド ----------
    def _accept_run(self):
        while not self._stop.is_set():
            try:
                c, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.settimeout(TIMEOUT_SEC)
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._clients.append(c)
            log.info("LapRF: client connected: %s", addr[0])
            threading.Thread(target=self._recv_run, args=(c, addr), name="laprf-recv", daemon=True).start()

    def _recv_run(self, c, addr):
        parser = PacketParser()
        while not self._stop.is_set():
            try:
                data = c.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            for packet in parser.feed(data):
                for reply in self.replies(packet):
                    self._q.put((c, reply))
        self._drop(c, addr)

    def _drop(self, c, addr=None):
        with self._lock:
            if c not in self._clients:
                return
            self._clients.remove(c)
        c.close()
        log.info("LapRF: client disconnected%s", f": {addr[0]}" if addr else "")

    def _send_run(self):
        next_status = time.monotonic() + STATUS_SEC
        while not self._stop.is_set():
            try:
                target, data = self._q.get(timeout=max(0.0, next_status - time.monotonic()))
            except queue.Empty:
                target, data = None, self._status()
                next_status = time.monotonic() + STATUS_SEC
            if not data:
                continue
            with self._lock:
                clients = [target] if target in self._clients else [] if target else list(self._clients)
            for c in clients:
                try:
                    c.sendall(data)
                except OSError as e:
                    log.warning("LapRF: cannot send, closing the client: %s", e)
                    self._drop(c)
