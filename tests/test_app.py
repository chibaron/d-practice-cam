"""動作確認用テスト（受信器・Firebase が無くても動く）。

    python -m pytest tests  または  python tests/test_app.py
"""
import logging
import os
import queue
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import types

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config import Config, validate_root            # noqa: E402
from app.laptimer import LapTimer, MarkerDetector, MAX_LAP, make_detector  # noqa: E402
from fgm import FGM_LEFT, FGM_RIGHT, FgmDetector            # noqa: E402
from fgm.markers import fgm_marker                         # noqa: E402
from app.qr import QRReader, clean_name                    # noqa: E402
from app.sound import announce_parts, join_wavs, speech_text  # noqa: E402
from app import sound as _snd                              # noqa: E402

# テストでは、この PC で起動している本物の VOICEVOX を使わない（使うテストは偽のサーバーに向ける）
_snd.VOICEVOX_URL = "http://127.0.0.1:1"


# ---------- 偽の firebase_admin（メモリ上のDB） ----------
class FakeDB:
    def __init__(self):
        self.tree = {}
        self.listeners = []
        self.n = 0
        self.offline = False    # True にすると通信エラー（オフライン）
        self.calls = 0

    def check(self):
        self.calls += 1
        if self.offline:
            raise ConnectionError("offline")

    def _get(self, path):
        node = self.tree
        for k in [p for p in path.split("/") if p]:
            if not isinstance(node, dict) or k not in node:
                return None
            node = node[k]
        return node

    def _set(self, path, value):
        keys = [p for p in path.split("/") if p]
        node = self.tree
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value


def install_fake_firebase():
    fake = FakeDB()

    class Ref:
        def __init__(self, path):
            self.path = path

        def get(self):
            fake.check()
            return fake._get(self.path)

        def set(self, v):
            fake.check()
            fake._set(self.path, dict(v) if isinstance(v, dict) else v)

        def update(self, d):
            fake.check()
            cur = fake._get(self.path) or {}
            cur.update(d)
            fake._set(self.path, cur)
            for path, cb in fake.listeners:
                if path == self.path:
                    for k, v in d.items():
                        cb(types.SimpleNamespace(event_type="patch", path="/" + k, data=v))

        def push(self, v):
            fake.check()
            fake.n += 1
            fake._set(f"{self.path}/-push{fake.n:03d}", dict(v))

        def listen(self, cb):
            fake.listeners.append((self.path, cb))
            cb(types.SimpleNamespace(event_type="put", path="/", data=self.get()))
            return types.SimpleNamespace(close=lambda: None)

    fa = types.ModuleType("firebase_admin")
    fa.initialize_app = lambda cred, opts: opts
    cr = types.ModuleType("firebase_admin.credentials")
    cr.Certificate = lambda p: p
    db = types.ModuleType("firebase_admin.db")
    db.reference = Ref
    fa.credentials, fa.db = cr, db
    sys.modules.update({"firebase_admin": fa, "firebase_admin.credentials": cr, "firebase_admin.db": db})
    return fake


# ---------- 合成映像 ----------
DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)


def frame_with_markers(ids=(0, 1), size=120, w=640, h=480):
    img = np.full((h, w, 3), 90, np.uint8)
    for k, i in enumerate(ids):
        m = cv2.aruco.generateImageMarker(DICT, i, size)
        m = cv2.copyMakeBorder(m, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
        y, x = 100, 60 + k * 260
        img[y:y + m.shape[0], x:x + m.shape[1]] = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
    return img


def frame_with_fgm(left=True, right=True, module=5, angle=0.0, swap=False, height=20, xs=(180, 460),
                    w=640, h=480):
    """左右の FGM（高さ height マス、中心 xs）。swap なら左右を入れ替えて貼る（向きが逆）。"""
    img = np.full((h, w), 90, np.uint8)
    pats = [(FGM_LEFT, left, xs[0]), (FGM_RIGHT, right, xs[1])]
    if swap:
        pats = [(FGM_RIGHT, right, xs[0]), (FGM_LEFT, left, xs[1])]
    for pat, on, cx in pats:
        if on:
            m = fgm_marker(pat, module, height)
            y, x = (h - m.shape[0]) // 2, cx - m.shape[1] // 2
            img[y:y + m.shape[0], x:x + m.shape[1]] = m
    if angle:
        img = cv2.warpAffine(img, cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1), (w, h),
                             borderValue=90)
    img = cv2.GaussianBlur(img, (0, 0), 1.0)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def blank(w=640, h=480):
    return np.full((h, w, 3), 90, np.uint8)


def frame_with_qr(text="TARO", size=240, w=640, h=480):
    img = blank(w, h)
    q = cv2.QRCodeEncoder.create().encode(text)
    q = cv2.resize(q, (size, size), interpolation=cv2.INTER_NEAREST)
    q = cv2.copyMakeBorder(q, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    img[100:100 + q.shape[0], 180:180 + q.shape[1]] = cv2.cvtColor(q, cv2.COLOR_GRAY2BGR)
    return img


# ---------- テスト ----------
def test_detector():
    det = MarkerDetector(ids=[0, 1, 2, 3], min_size_pct=5)
    assert det.detect(frame_with_markers()).visible
    assert not det.detect(blank()).visible
    assert not MarkerDetector(ids=[5]).detect(frame_with_markers()).visible           # 対象外ID
    assert not MarkerDetector(min_size_pct=40).detect(frame_with_markers()).visible  # 小さすぎ
    assert not MarkerDetector(min_markers=3).detect(frame_with_markers()).visible     # 個数不足


def test_make_detector():
    """FGM の検出は fpv-gate-marker（fgm）。既定は FGM で、設定で ArUco と切り替えられ、ch ごとに別の検出器になる。"""
    assert isinstance(make_detector({}), FgmDetector)                 # 既定は FGM
    assert isinstance(make_detector({"marker_type": "aruco"}), MarkerDetector)
    det = make_detector({"marker_type": "fgm", "fgm_line_step": 2, "fgm_min_lines": 4, "min_size_pct": 6})
    assert isinstance(det, FgmDetector) and (det.line_step, det.min_lines, det.min_size_pct) == (2, 4, 6)
    assert make_detector({"marker_type": "aruco", "ids": [5]}).ids == {0, 1, 2, 3}   # ArUco の ID は 0〜3 に固定
    assert det.tol == 0.25 and make_detector({"marker_type": "fgm", "fgm_tol": 0.4}).tol == 0.4   # 縞のずれの許容
    assert det.detect(frame_with_fgm()).visible
    assert det.detect(frame_with_fgm(right=False)).visible          # 片方が残っている間は見えたのまま
    assert not make_detector({"marker_type": "fgm"}).detect(frame_with_fgm(right=False)).visible
    assert not det.detect(frame_with_markers()).visible               # ArUco は FGM として読まない


def test_laptimer_appear():
    """false→true の待ち: appear_ms 続けて見えて初めて「見えた」。短い誤検出では通過にならず、確定も取り消さない。"""
    def run(seq, **kw):
        t = LapTimer(flicker_ms=100, appear_ms=50, **kw)
        return [p for p in (t.update(ts, v) for ts, v in seq) if p is not None]

    # 1フレームだけの誤検出は通過にならない（0.00、0.20 とも）
    assert run([(0.00, True), (0.03, False), (0.20, True), (0.23, False), (0.40, False)]) == []
    # 続けて見えれば通過。通過時刻は最後に見えたフレーム
    assert run([(0.00, True), (0.03, True), (0.06, True), (0.09, True), (0.12, False), (0.25, False)]) == [0.09]
    # ちょうど待ち時間で「見えた」になる
    assert run([(0.00, True), (0.05, True), (0.08, False), (0.20, False)]) == [0.05]
    # 途中で途切れたら数え直す
    assert run([(0.00, True), (0.03, False), (0.06, True), (0.09, False), (0.12, True), (0.15, False), (0.40, False)]) == []
    # LOST 中の一瞬の誤検出: 確定を取り消さず、通過時刻も動かさない
    seq = [(0.00, True), (0.06, True), (0.09, False), (0.15, True), (0.18, False), (0.21, False)]
    assert run(seq) == [0.06]
    # LOST 中に続けて見えたら SEEN に戻り（一瞬の見失い）、通過時刻は最後に見えたフレーム
    seq = [(0.00, True), (0.06, True), (0.09, False), (0.12, True), (0.15, True), (0.18, True), (0.21, False), (0.40, False)]
    assert run(seq) == [0.18]
    # LOST で待ち時間を過ぎても、見え続けている間は確定せず、誤検出と分かったら確定する（二重に通過にしない）
    seq = [(0.00, True), (0.06, True), (0.09, False), (0.18, True), (0.20, False), (0.23, False)]
    assert run(seq) == [0.06]
    seq = [(0.00, True), (0.06, True), (0.09, False), (0.18, True), (0.21, True), (0.24, True), (0.27, False), (0.45, False)]
    assert run(seq) == [0.24]
    # 映像が途切れたら、数えかけの検出は捨てて確定する
    t = LapTimer(flicker_ms=100, appear_ms=50)
    for ts, v in [(0.00, True), (0.06, True), (0.09, False), (0.18, True)]:
        t.update(ts, v)
    assert t.check_timeout(0.30) == 0.06 and t.t_appear is None
    # 0 なら今までどおりすぐ「見えた」
    t = LapTimer(flicker_ms=100, appear_ms=0)
    assert [p for p in (t.update(ts, v) for ts, v in [(0.0, True), (0.03, False), (0.2, False)]) if p is not None] == [0.0]
    # 既定は 30fps で1フレーム分: 1フレームだけの検出は無視し、2フレーム続けば「見えた」
    with tempfile.TemporaryDirectory() as d:
        ms = Config(os.path.join(d, "config.yaml")).load().aruco["appear_ms"]
    assert ms == 30
    frame = 1 / 30

    def at_30fps(*visible):
        t = LapTimer(flicker_ms=100, appear_ms=ms)
        return [p for i, v in enumerate(visible) if (p := t.update(i * frame, v)) is not None]

    assert at_30fps(True, False, False, False, False, False) == []
    assert at_30fps(True, True, False, False, False, False, False) == [frame]
    # 設定が ChannelWorker に届く
    from app.capture import ChannelWorker
    w = ChannelWorker(0, "F1", "x.mp4", None, {}, None, 100, lambda e: None, lambda: 1, qr_cfg={"enabled": False}, appear_ms=80)
    assert w.timer.appear == 0.08


def test_laptimer():
    t = LapTimer(flicker_ms=100)
    seq = [(0.00, True), (0.03, True), (0.06, False), (0.09, True),   # 一瞬消えても確定しない
           (0.12, False), (0.15, False), (0.20, False), (0.25, False)]
    passes = [p for p in (t.update(ts, v) for ts, v in seq) if p is not None]
    assert passes == [0.09], passes                   # 最後に見えたフレームの時刻
    assert t.on_pass(0.09, 10) == MAX_LAP             # 最初の通過は 999（起点）
    assert t.on_pass(5.00, 10) is None                # minLap 以下は無視
    assert t.on_pass(12.3456, 10) == 12.25            # 10ms 未満切り捨て
    assert t.lap_count == 1 and t.total(12.3456) == 12.25   # 起点からの周回数とトータル（無視した通過は数えない）
    assert t.on_pass(25.0, 10) == 12.65 and t.lap_count == 2 and t.total(25.0) == 24.91
    assert t.on_pass(25.0 + 2000, 10) == MAX_LAP      # 999 秒以上は 999


def test_qr_reader():
    r = QRReader()
    name, pts = r.read(frame_with_qr("TARO"))
    assert name == "TARO" and pts.shape == (4, 2)
    assert r.read(blank()) == ("", None)
    assert r.read(frame_with_markers()) == ("", None)     # ArUco は QR として読まない
    assert clean_name("  Pilot\n01 ") == "Pilot01"
    assert len(clean_name("x" * 100)) == 32


def test_speech_text():
    cfg = {"text": "{name}、エントリー"}
    assert speech_text(cfg, "F1", "TARO") == "TARO、エントリー"
    assert speech_text({"text": "{band} {name}"}, "F1", "TARO") == "F1 TARO"
    assert speech_text({"text": "{nam}"}, "F1", "TARO") == "TARO"          # 書き間違いは名前だけ
    assert speech_text(cfg, "F1", "TARO", "start_text") == "TARO、スタート"    # 既定のスタート文
    assert speech_text(cfg, "F1", "TARO", "lap_text", 11.06) == "TARO、11.0"   # 0.1 秒単位に切り捨て


def test_announce_parts():
    """lap2firebase の wav があれば「名前.wav → entry/start.wav」、無ければ音声合成。"""
    cfg = {"text": "{name}、エントリー", "start_text": "{name}、スタート", "lap_text": "{name}、{time}",
           "clear_text": "{band}、クリア"}
    with tempfile.TemporaryDirectory() as d:
        for f in ("CHO!.wav", "entry.wav", "start.wav", "F1.wav", "110.wav", "113.wav", "F1_clear.wav",
                  "lap3.wav", "total35.wav", "newbest.wav"):
            open(os.path.join(d, f), "wb").close()
        assert announce_parts(cfg, "F1", "CHO!", "text", d) == \
            [("file", os.path.join(d, "CHO!.wav")), ("file", os.path.join(d, "entry.wav"))]
        assert announce_parts(cfg, "F1", "F1", "start_text", d) == \
            [("file", os.path.join(d, "F1.wav")), ("file", os.path.join(d, "start.wav"))]
        # 周回ラップ: 名前.wav → タイムの wav（0.1 秒単位に切り捨て。浮動小数の誤差で 112 にならない）
        assert announce_parts(cfg, "F1", "CHO!", "lap_text", d, 11.06) == \
            [("file", os.path.join(d, "CHO!.wav")), ("file", os.path.join(d, "110.wav"))]
        assert announce_parts(cfg, "F1", "CHO!", "lap_text", d, 11.3) == \
            [("file", os.path.join(d, "CHO!.wav")), ("file", os.path.join(d, "113.wav"))]
        assert announce_parts(cfg, "F1", "CHO!", "lap_text", d, 75.5) == [("tts", "CHO!、75.5")]  # タイムの wav 無し
        # 周回数とトータル: 名前 → タイム → lap3.wav → total35.wav（35.99 秒は 35）
        assert [os.path.basename(p) for _, p in announce_parts(cfg, "F1", "CHO!", "lap_text", d, 11.06, 3, 35.99)] \
            == ["CHO!.wav", "110.wav", "lap3.wav", "total35.wav"]
        # その wav が無ければ、その部分だけ省く
        assert [os.path.basename(p) for _, p in announce_parts(cfg, "F1", "CHO!", "lap_text", d, 11.06, 4, 47.0)] \
            == ["CHO!.wav", "110.wav"]
        # 音声合成: タイムに続けて周回数とトータル
        assert announce_parts(cfg, "F1", "TARO", "lap_text", d, 11.06, 3, 35.99) == \
            [("tts", "TARO、11.0、3周、トータル35秒")]
        # ベストラップ更新: いちばん最後に newbest.wav（音声合成では best_text）。周回ラップのときだけ
        names = [os.path.basename(p) for _, p in announce_parts(cfg, "F1", "CHO!", "lap_text", d, 11.06, 3, 35.99,
                                                                best=True)]
        assert names == ["CHO!.wav", "110.wav", "lap3.wav", "total35.wav", "newbest.wav"], names
        names = [os.path.basename(p) for _, p in announce_parts(cfg, "F1", "CHO!", "lap_text", d, 11.06, best=True)]
        assert names == ["CHO!.wav", "110.wav", "newbest.wav"], names
        assert announce_parts(cfg, "F1", "CHO!", "start_text", d, best=True) == \
            [("file", os.path.join(d, "CHO!.wav")), ("file", os.path.join(d, "start.wav"))]
        assert announce_parts(cfg, "F1", "TARO", "lap_text", d, 11.06, 3, 35.99, best=True) == \
            [("tts", "TARO、11.0、3周、トータル35秒、ベストラップ更新")]
        os.remove(os.path.join(d, "newbest.wav"))       # newbest.wav が無ければ、その部分だけ音声合成
        assert announce_parts(cfg, "F1", "CHO!", "lap_text", d, 11.06, best=True)[-1] == ("tts", "ベストラップ更新")
        # クリア: lap2firebase と同じく「バンド_clear.wav」だけ。無ければ音声合成
        assert announce_parts(cfg, "F1", "CHO!", "clear_text", d) == [("file", os.path.join(d, "F1_clear.wav"))]
        assert announce_parts(cfg, "R1", "R1", "clear_text", d) == [("tts", "R1、クリア")]
        assert announce_parts(cfg, "F1", "TARO", "text", d) == [("tts", "TARO、エントリー")]   # wav 無し
        assert announce_parts(cfg, "F1", "A/B", "text", d) == [("tts", "A/B、エントリー")]     # ファイル名に使えない
    assert announce_parts(cfg, "F1", "CHO!", "start_text", "") == [("tts", "CHO!、スタート")]   # フォルダ未設定
    assert announce_parts(cfg, "F1", "CHO!", "text", "Z:/no/such/dir") == [("tts", "CHO!、エントリー")]


def test_pilot_wav():
    """パイロットの名前.wav は pilot_dir（無ければ wav_dir）。ユーザーが置く。無い名前は Windows の声で作ってから鳴らす。"""
    from app import sound as snd
    cfg = {"text": "{name}、エントリー", "start_text": "{name}、スタート"}
    assert not hasattr(snd, "Voicevox") and not hasattr(snd, "make_name_wav")      # VOICEVOX では作らない
    with tempfile.TemporaryDirectory() as d:
        voice, pilot = os.path.join(d, "voice"), os.path.join(d, "pilot")
        os.makedirs(voice)
        os.makedirs(pilot)
        for f in ("entry.wav", "start.wav", "F1.wav", "OLD.wav"):
            open(os.path.join(voice, f), "wb").close()
        assert announce_parts(cfg, "F1", "F1", "start_text", voice, pilot_dir=pilot)[0] == \
            ("file", os.path.join(voice, "F1.wav"))                  # バンド名は voice（前の置き方の名前も）
        assert snd.name_wav("OLD", pilot, voice) == os.path.join(voice, "OLD.wav")
        assert announce_parts(cfg, "F1", "TARO", "text", voice, pilot_dir=pilot) == [("tts", "TARO、エントリー")]
        open(os.path.join(pilot, "TARO.wav"), "wb").close()        # ユーザーが VOICEVOX で作って置いた
        assert announce_parts(cfg, "F1", "TARO", "text", voice, pilot_dir=pilot) == \
            [("file", os.path.join(pilot, "TARO.wav")), ("file", os.path.join(voice, "entry.wav"))]
        open(os.path.join(pilot, "OLD.wav"), "wb").close()
        assert snd.name_wav("OLD", pilot, voice) == os.path.join(pilot, "OLD.wav")     # pilot が先
        assert snd.name_wav("A/B", pilot, voice) is None                                # ファイル名に使えない

        made = []

        def fake_sapi(name, pdir, voice_obj):
            made.append(name)
            path = os.path.join(pdir, f"{name}.wav")
            open(path, "wb").close()
            return path

        config = types.SimpleNamespace(data={"speech": {**cfg, "wav_dir": voice, "pilot_dir": pilot}},
                                       resolve_path=lambda p: p)
        sp = snd.SoundPlayer.__new__(snd.SoundPlayer)       # 鳴らすスレッドは動かさない
        sp.config, sp._say, sp._gen, sp._wake = config, queue.Queue(), queue.Queue(), threading.Event()
        sp._get_gen_voice = lambda want, language="ja": object()
        real_sapi, snd.make_sapi_name_wav = snd.make_sapi_name_wav, fake_sapi
        try:
            # 名前.wav が無ければ別スレッドで Windows の声で作ってからアナウンスする（順番どおり）。あれば直ぐ
            sp.announce("F1", "JIRO", "text")
            sp.announce("F1", "JIRO", "start_text")
            sp.announce("F1", "TARO", "text")
            assert sp._say.get_nowait()[0][0] == ("file", os.path.join(pilot, "TARO.wav")) and sp._gen.qsize() == 2
            threading.Thread(target=sp._gen_run, daemon=True).start()
            got = [sp._say.get(timeout=5)[0] for _ in range(2)]
            assert [os.path.basename(p) for parts in got for _, p in parts] == \
                ["JIRO.wav", "entry.wav", "JIRO.wav", "start.wav"] and made == ["JIRO"]   # 作るのは1回
            # 作ったら、次からはその wav を使う（VOICEVOX の声にしたいときはユーザーが上書きする）
            sp.announce("F1", "JIRO", "text")
            assert sp._say.get_nowait()[0][0] == ("file", os.path.join(pilot, "JIRO.wav")) and made == ["JIRO"]

            # 日本語音声（wav_dir）を入れていなければ、作らず音声合成で読む
            config.data["speech"]["wav_dir"] = os.path.join(d, "no_voice_pack")
            sp.announce("F1", "SABU", "text")
            assert sp._say.get(timeout=5)[0] == [("tts", "SABU、エントリー")] and sp._gen.empty()
            assert "SABU" not in made
            # Windows の声でも作れなければ、文を音声合成で読む
            config.data["speech"]["wav_dir"] = voice
            snd.make_sapi_name_wav = lambda name, pdir, voice_obj: None
            sp.announce("F1", "NAO", "text")
            assert sp._say.get(timeout=5)[0] == [("tts", "NAO、エントリー")]
        finally:
            snd.make_sapi_name_wav = real_sapi


def test_pilot_wav_voicevox():
    """名前.wav を作るとき、VOICEVOX が起動していれば VOICEVOX で作る（無ければ Windows の声）。"""
    import http.server
    import json
    import urllib.parse
    from app import sound as snd
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(b'"0.14.0"', "application/json")

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            q = urllib.parse.parse_qs(u.query)
            calls.append((u.path, q, json.loads(body) if body else None))
            if u.path == "/audio_query":
                self._send(json.dumps({"speedScale": 1.0, "kana": q["text"][0]}).encode(), "application/json")
            else:
                self._send(b"RIFF" + b"\0" * 40, "audio/wav")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dead = snd.VOICEVOX_URL
    try:
        with tempfile.TemporaryDirectory() as d:
            pilot, voice = os.path.join(d, "pilot"), os.path.join(d, "voice")
            os.makedirs(voice)
            open(os.path.join(voice, "entry.wav"), "wb").close()
            # 起動していなければ作らない（すぐ諦める）
            t0 = time.monotonic()
            assert snd.make_voicevox_name_wav("TARO", pilot) is None and time.monotonic() - t0 < 3
            assert not os.path.exists(os.path.join(pilot, "TARO.wav"))
            # 起動していれば、四国めたん（speaker 2）・速さ 1.2・24kHz モノラルで作る
            snd.VOICEVOX_URL = f"http://127.0.0.1:{server.server_address[1]}"
            assert snd.make_voicevox_name_wav("TARO", pilot) == os.path.join(pilot, "TARO.wav")
            with open(os.path.join(pilot, "TARO.wav"), "rb") as f:
                assert f.read(4) == b"RIFF"
            assert calls[0][0] == "/audio_query" and calls[0][1] == {"text": ["TARO"], "speaker": ["2"]}
            assert calls[1][0] == "/synthesis" and calls[1][1] == {"speaker": ["2"]}
            assert calls[1][2]["speedScale"] == 1.2 and calls[1][2]["outputSamplingRate"] == 24000                 and calls[1][2]["outputStereo"] is False and calls[1][2]["kana"] == "TARO"
            assert snd.make_voicevox_name_wav("A/B", pilot) is None                 # ファイル名に使えない
            # SoundPlayer: 名前.wav が無ければ VOICEVOX で作ってからアナウンスする（Windows の声では作らない）
            cfg = {"enabled": True, "language": "ja", "text": "{name}、エントリー", "pilot_dir": pilot}
            config = types.SimpleNamespace(data={"speech": cfg}, resolve_path=lambda p: p)
            sp = snd.SoundPlayer.__new__(snd.SoundPlayer)       # 鳴らすスレッドは動かさない
            sp.config, sp._say, sp._gen, sp._wake = config, queue.Queue(), queue.Queue(), threading.Event()
            sp._get_gen_voice = lambda want, language="ja": object()
            sapi = []
            real_sapi, real_dir = snd.make_sapi_name_wav, snd.VOICE_DIR
            snd.make_sapi_name_wav = lambda name, pdir, v: sapi.append(name)
            snd.VOICE_DIR = voice
            try:
                threading.Thread(target=sp._gen_run, daemon=True).start()
                sp.announce("F1", "JIRO", "text")
                assert sp._say.get(timeout=5)[0] ==                     [("file", os.path.join(pilot, "JIRO.wav")), ("file", os.path.join(voice, "entry.wav"))]
                assert sapi == [] and calls[-1][0] == "/synthesis"
                # VOICEVOX を閉じた後は、Windows の声で作る
                snd.VOICEVOX_URL = dead
                sp.announce("F1", "KEN", "text")
                sp._say.get(timeout=5)
                assert sapi == ["KEN"]
            finally:
                snd.make_sapi_name_wav, snd.VOICE_DIR = real_sapi, real_dir
    finally:
        snd.VOICEVOX_URL = dead
        server.shutdown()


def test_make_voice():
    """日本語の声（sound/voice）は tools/make_voice.py で VOICEVOX から作り直せる（wav はリポジトリに入れない）。"""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import make_voice
    from app import sound as snd
    items = dict(make_voice.voice_items())
    assert items["50.wav"] == "5.0" and items["599.wav"] == "59.9" and "49.wav" not in items
    assert items["lap1.wav"] == "1周" and items["lap99.wav"] == "99周" and "lap100.wav" not in items
    assert items["total35.wav"] == "35秒" and items["total65.wav"] == "1分5秒" and items["total599.wav"] == "9分59秒"
    assert items["entry.wav"] == "エントリー" and items["newbest.wav"] == "ベストラップ更新"
    assert items["F1.wav"] == "F1" and items["F1_clear.wav"] == "F1、クリア" and "L8_clear.wav" in items
    with tempfile.TemporaryDirectory() as d:
        # アプリが鳴らす wav の名前と揃っている（全部あれば wav だけで鳴らす）
        for fn in items:
            open(os.path.join(d, fn), "wb").close()
        cfg = {"text": "{name}、エントリー", "lap_text": "{name}、{time}", "total_text": "{lap}周、トータル{total}秒",
               "clear_text": "{band}、クリア", "best_text": "ベストラップ更新"}
        for key in ("lap_text", "text", "start_text", "clear_text"):
            parts = snd.announce_parts(cfg, "F1", "F1", key, d, 12.34, 3, 35.0, "", True)
            assert parts and all(kind == "file" for kind, _ in parts), (key, parts)
        for fn in items:
            os.remove(os.path.join(d, fn))
        texts = []
        orig = snd.voicevox_synth
        try:
            snd.voicevox_synth = lambda text: None                       # VOICEVOX が起動していない
            assert make_voice.make_voice(d, log=lambda *a: None) == (0, -1)
            snd.voicevox_synth = lambda text: texts.append(text) or b"RIFF"
            open(os.path.join(d, "entry.wav"), "wb").close()             # あるものは作らない
            made, failed = make_voice.make_voice(d, log=lambda *a: None)
            assert (made, failed) == (len(items) - 1, 0) and "エントリー" not in texts[1:]
            assert len(os.listdir(d)) == len(items)
            assert make_voice.make_voice(d, log=lambda *a: None) == (0, 0)     # 2回目は何もしない
            assert make_voice.make_voice(d, force=True, log=lambda *a: None)[0] == len(items)
        finally:
            snd.voicevox_synth = orig


def test_trim_silence():
    """Windows の声で作った wav の前後の長い無音を、sound/voice の wav と同じくらいに切る。"""
    import wave
    from app import sound as snd
    sr = 24000
    x = np.zeros(int(sr * 2.0), np.int16)
    x[int(sr * 0.5):int(sr * 1.0)] = 5000          # 0.5〜1.0 秒だけ音
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "a.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(x.tobytes())
        snd.trim_silence(path)
        with wave.open(path) as w:
            assert (w.getnchannels(), w.getframerate()) == (1, sr)
            assert abs(w.getnframes() / sr - (0.5 + sum(snd.SAPI_TRIM))) < 0.01


def test_speech_language():
    """アナウンスは英語（既定、音声合成だけ）と日本語（wav・音声合成）。"""
    from app import sound as snd
    ja = {"text": "{name}、エントリー", "lap_text": "{name}、{time}", "total_text": "{lap}周、トータル{total}秒",
          "voice": "Haruka", "wav_dir": "./v", "pilot_dir": "./p"}
    assert snd.effective_speech(ja) is ja and snd.effective_speech({**ja, "language": "ja"})["voice"] == "Haruka"
    en = snd.effective_speech({**ja, "language": "en", "en": {"lap_text": "{name} {time}"}})
    assert en["lap_text"] == "{name} {time}" and en["text"] == "{name}, entry"     # 足りない文は英語の既定
    assert en["voice"] == "" and not en["wav_dir"] and not en["pilot_dir"]
    en = snd.effective_speech({**ja, "language": "en"})
    assert announce_parts(en, "F1", "TARO", "lap_text", en["wav_dir"], 11.06, 3, 35.99) == \
        [("tts", "TARO, 11.0, lap 3, total 35 seconds")]
    assert announce_parts(en, "F1", "F1", "clear_text", en["wav_dir"]) == [("tts", "F1, clear")]
    assert announce_parts(en, "F1", "TARO", "lap_text", en["wav_dir"], 11.06, best=True) == \
        [("tts", "TARO, 11.0, new best lap")]

    # 英語では wav があっても鳴らさず、名前.wav も作らない。声は英語で選ぶ
    with tempfile.TemporaryDirectory() as d:
        for f in ("TARO.wav", "entry.wav"):
            open(os.path.join(d, f), "wb").close()
        config = types.SimpleNamespace(data={"speech": {**ja, "wav_dir": d, "pilot_dir": d, "language": "en"}},
                                       resolve_path=lambda p: p)
        sp = snd.SoundPlayer.__new__(snd.SoundPlayer)
        sp.config, sp._say, sp._gen, sp._wake = config, queue.Queue(), queue.Queue(), threading.Event()
        sp.announce("F1", "NEW", "text")
        assert sp._say.get_nowait() == ([("tts", "NEW, entry")], "", "en") and sp._gen.empty()

    class FakeVoices(list):
        Count = property(len)
        Item = list.__getitem__

    class FakeToken:
        def __init__(self, desc):
            self.desc = desc

        def GetDescription(self):
            return self.desc

    class FakeVoice:
        Voice = None

        def GetVoices(self, attr=""):
            return FakeVoices({"Language=409": [FakeToken("Zira")], "": [FakeToken("Haruka"), FakeToken("Zira")]}
                              .get(attr, []))

    v = FakeVoice()
    snd.SoundPlayer._select_voice(v, "", "en")
    assert v.Voice.desc == "Zira"
    snd.SoundPlayer._select_voice(v, "haruka", "en")     # 名前で指定した声が先
    assert v.Voice.desc == "Haruka"

    # 既定は英語。言語の項目が無い config.yaml も英語になり、書いてあれば日本語のまま。日本語の文は消えない
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        assert Config(path).load().data["speech"]["language"] == "en"
        with open(path, "w", encoding="utf-8") as f:
            f.write("speech:\n  text: '{name}です'\n")
        c = Config(path).load()
        assert c.data["speech"]["language"] == "en" and c.data["speech"]["text"] == "{name}です"
        assert c.data["speech"]["en"]["text"] == "{name}, entry"
        with open(path, "w", encoding="utf-8") as f:
            f.write("speech:\n  language: ja\n")
        assert Config(path).load().data["speech"]["language"] == "ja"


def test_logs_are_english():
    """ログは英語だけ（コードのコメントは日本語でよい）。"""
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(root, "main.py")] + \
        [os.path.join(root, "app", f) for f in os.listdir(os.path.join(root, "app")) if f.endswith(".py")]
    for path in paths:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and \
                    getattr(n.func.value, "id", "") == "log":
                for c in ast.walk(n):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        assert c.value.isascii(), f"{path}:{c.lineno}: {c.value}"


def test_join_wavs():
    """「名前.wav → entry.wav」を1つの wav につなぐ。形式が違えば None。"""
    import wave

    def make(path, seconds, rate=24000):
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * int(seconds * rate))

    with tempfile.TemporaryDirectory() as d:
        a, b, c, out = (os.path.join(d, n) for n in ("a.wav", "b.wav", "c.wav", "out.wav"))
        make(a, 0.5)
        make(b, 0.25)
        make(c, 0.5, rate=16000)
        assert abs(join_wavs([a, b], out) - 0.75) < 1e-6
        with wave.open(out) as w:
            assert w.getnframes() == int(0.75 * 24000) and w.getframerate() == 24000
        assert join_wavs([a, c], out) is None


def test_make_sounds():
    """効果音はこのアプリで作る（16bit モノラル、頭に無音なし）。sound/ の wav もこれで作ったもの。"""
    import wave
    from tools import make_sounds as ms
    for fn, path in ((ms.lap, "lap.wav"), (ms.best, "bestlap.wav"), (ms.error, "errorlap.wav"),
                     (ms.boot, "boot.wav")):
        x = fn()
        assert 0.1 < len(x) / ms.SR < 2 and np.abs(x[:int(ms.SR * 0.01)]).max() > 0, path   # すぐ鳴る
        with wave.open(os.path.join(ROOT, "sound", path)) as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, ms.SR), path
            assert w.getnframes() == len(x), path       # sound/ の wav は make_sounds.py で作ったもの


def _snow(rng):
    """実機の砂嵐に似た映像（横に流れる白黒の線）。"""
    rows = rng.integers(0, 2, (480, 1)) * 200 + 15
    img = np.repeat(rows, 640, axis=1).astype(np.uint8)
    mask = rng.integers(0, 2, (480, 640)).astype(bool)
    img[mask] = 255 - img[mask]
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _scene(shift=0):
    """ふつうの映像（なだらかな明暗＋少し動く）。"""
    x = np.linspace(40, 220, 640 + 64)[shift:shift + 640]
    return cv2.cvtColor(np.tile(x.astype(np.uint8), (480, 1)), cv2.COLOR_GRAY2BGR)


def test_video_monitor():
    """砂嵐と受信器の単色の画面を見分ける。ふつうの映像は、動きが大きくても映像無しにしない。"""
    from app import video_monitor as vm
    rng = np.random.default_rng(1)

    def kinds(*frames):
        m = vm.VideoMonitor(check_sec=0, lost_check_sec=0)      # 毎フレーム調べる
        return [m.update(f, i * 0.033) for i, f in enumerate(frames)]

    assert kinds(_snow(rng), _snow(rng), _snow(rng)) == ["", "snow", "snow"]      # 1フレーム目は比べる相手が無い
    noise = lambda: rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert kinds(noise(), noise())[1] == "snow"                                   # 画素ごとのノイズ

    blue = np.zeros((480, 640, 3), np.uint8)
    blue[:] = (230, 0, 0)
    cv2.putText(blue, "T C1 5705", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)   # 受信器の文字が出ても
    black = np.full((480, 640, 3), 3, np.uint8)
    assert kinds(blue, blue)[1] == "flat" and kinds(black, black)[1] == "flat"
    assert kinds(_scene(0), blue) == ["", "flat"]                                 # 映像から変わった直後でも分かる
    assert kinds(_scene(0), _snow(rng)) == ["", "snow"]

    # 映像無しにしないもの: ふつうの映像、白っぽい空、細かい模様が大きく動く映像、1回だけ変わった画面、大きさが変わった映像
    white = np.full((480, 640, 3), (235, 235, 240), np.uint8)
    tex = cv2.normalize(cv2.GaussianBlur(rng.integers(0, 255, (480, 1000, 3), dtype=np.uint8), (3, 3), 0),
                        None, 0, 255, cv2.NORM_MINMAX)
    assert kinds(_scene(0), _scene(2), _scene(4)) == ["", "", ""]
    assert kinds(white, white) == ["", ""]
    assert kinds(tex[:, :640], tex[:, 300:940]) == ["", ""]
    assert kinds(_scene(0), np.full((240, 320, 3), 128, np.uint8)) == ["", ""]
    assert kinds(frame_with_markers(), frame_with_markers()) == ["", ""]

    # 調べる間隔: 映像がある間は1秒、映像が無い間は0.1秒。その間は前の結果を返す
    assert vm.CHECK_SEC == 1.0 and vm.LOST_CHECK_SEC == 0.1
    checks = []
    orig = vm.classify
    vm.classify = lambda f, p: checks.append(1) or orig(f, p)
    try:
        def checked(m, *pairs):
            """[(フレーム, 時刻)] を渡して、[(結果, 調べたか)] を返す。"""
            out = []
            for f, t in pairs:
                checks.clear()
                out.append((m.update(f, t), bool(checks)))
            return out

        m = vm.VideoMonitor()
        s1, s2, s3 = _snow(rng), _snow(rng), _snow(rng)
        a, b, c = _scene(0), _scene(1), _scene(2)
        assert checked(m, (a, 0.0), (b, 0.5), (c, 0.99)) == [("", True), ("", False), ("", False)]
        assert checked(m, (a, 1.0)) == [("", True)]                     # 1秒たった
        assert checked(m, (s1, 1.5)) == [("", False)]                   # 砂嵐になっても、次に調べるまでは分からない
        assert checked(m, (s2, 2.0), (s3, 2.05)) == [("snow", True), ("snow", False)]
        assert checked(m, (s1, 2.1)) == [("snow", True)]                # 0.1秒たった
        assert checked(m, (a, 2.15), (b, 2.2)) == [("snow", False), ("", True)]
        assert checked(m, (c, 2.3)) == [("", False)]                    # 映像が戻ると1秒おき
    finally:
        vm.classify = orig

    # クリアするまでの時間。途切れたら数え直す。0 ならクリアしない。フレームが読めなくても数える
    m = vm.VideoMonitor(500, check_sec=0, lost_check_sec=0)
    m.update(_snow(rng), 0.0)
    m.update(_snow(rng), 0.1)
    assert m.lost_since == 0.1 and not m.should_clear(0.5) and m.should_clear(0.6)
    m.update(_scene(), 0.7)
    assert m.lost_since is None and m.kind == "" and not m.should_clear(9.0)
    m.mark_lost(10.0)
    assert m.kind == "none" and not m.should_clear(10.4) and m.should_clear(10.5)
    z = vm.VideoMonitor(0)
    z.mark_lost(0.0)
    assert not z.should_clear(100.0)


def test_no_video_channel():
    """砂嵐と分かったらマーカーを探さず、続いたらクリアする。短い砂嵐ならセッションは続く。"""
    from app.capture import ChannelWorker
    from app.laptimer import Detection
    rng = np.random.default_rng(2)
    MARKER, CLEAR = _scene(0), _scene(1)
    snows = [_snow(rng) for _ in range(4)]
    # (映像, 秒): 通過（起点）→ 短い砂嵐 → 通過（周回。セッションは続いている）→ 長い砂嵐でクリア → 通過（起点）
    plan = [(MARKER, 0.3), (CLEAR, 0.4), ("snow", 0.3), (CLEAR, 0.2), (MARKER, 0.3), (CLEAR, 0.4), ("snow", 2.0),
            (CLEAR, 0.4), (MARKER, 0.3), (CLEAR, 0.4)]
    frames = []
    for what, sec in plan:
        for i in range(int(sec * 30)):
            frames.append(snows[i % 4] if isinstance(what, str) else what)
    calls = []

    class Det:
        def detect(self, frame):
            calls.append(frame)
            return Detection(frame is MARKER)

    class Cap:
        def __init__(self):
            self.i = 0

        def isOpened(self):
            return True

        def read(self):
            time.sleep(1 / 30)
            if self.i >= len(frames):
                return True, CLEAR
            self.i += 1
            return True, frames[self.i - 1]

        def release(self):
            pass

    events, clears = [], []
    w = ChannelWorker(0, "F1", 0, None, {}, Det(), 100, events.append, lambda: 0.1, qr_cfg={"enabled": False},
                      on_clear=lambda ch, band, name: clears.append((band, name)), appear_ms=30,
                      no_video_cfg={"enabled": True, "clear_ms": 500})
    w._open = Cap
    w.set_measuring(True)
    w.start()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and len(events) < 3:
        time.sleep(0.05)
    time.sleep(0.3)
    w.stop()
    laps = [e.lap for e in events]
    assert len(laps) == 3 and laps[0] == 999.0 and laps[1] < 999.0 and laps[2] == 999.0, laps
    assert clears == [("F1", "F1")], clears                                         # クリアは1回
    # 砂嵐と分かった後はマーカーを探さない（69フレームのうち、分かるまでの1秒以内の分だけ）
    assert sum(1 for c in calls if any(c is s for s in snows)) < 50


def test_laprf_packets():
    """LapRF のパケット。CRC と形式は、実機の DESCRIPTOR の返事と同じになる。"""
    from app import laprf as lr
    device = bytes.fromhex("5a 11 00 2b 0c 08 da 20 04 06 00 00 01 21 01 02 5b")   # TinyViewPlus_Livetime のソースのコメントより
    assert lr.build_packet(lr.MSG_DESCRIPTOR, lr.DESCRIPTOR_BODY) == device
    assert lr.message_id(device) == lr.MSG_DESCRIPTOR
    assert lr.parse_fields(device) == [(0x20, bytes((6, 0, 0, 1))), (0x21, b"\x02")]

    # エスケープ: 0x58〜0x5f を置き換え、受信側で戻す。分かれて届いても、前にゴミがあっても読める
    raw = lr.build_packet(lr.MSG_DETECTION, lr.field(0x21, "I", 0x5a5b5c58) + lr.field(0x22, "H", 0x5f60))
    sent = lr.escape(raw)
    assert all(b not in (lr.START, lr.END) for b in sent[1:-1]) and sent[0] == lr.START and sent[-1] == lr.END
    p = lr.PacketParser()
    assert p.feed(b"\x00\x01" + sent[:5]) == [] and p.feed(sent[5:]) == [raw]
    assert p.feed(sent + sent) == [raw, raw]
    assert p.feed(sent[:6] + sent) == [raw]        # 途中で切れたパケットは捨てる
    assert p.feed(b"\x5a" + b"\x00" * (lr.MAX_PACKET + 1)) == [] and p.buf is None     # 長すぎるものは捨てる

    # バンド名と周波数（TinyViewPlus_Livetime の値と同じ）。分からない名前は None
    assert lr.band_info("E1") == (3, 1, 5705) and lr.band_info("f4") == (1, 4, 5800) and lr.band_info("A8") == (5, 8, 5725)
    assert lr.band_info("R1") == (2, 1, 5658) and lr.band_info("B8") == (4, 8, 5866) and lr.band_info("L1") == (0, 1, 5362)
    assert lr.band_info("CH1") is None and lr.band_info("X1") is None and lr.band_info("F9") is None


def test_laprf_server():
    """TCP で接続して、問い合わせへの返事・通過・STATUS を受け取る。切断してもサーバーは動き続ける。"""
    from app import laprf as lr
    srv = lr.LapRFServer(0, lambda: ["F1", "E1", "CH3"], host="127.0.0.1")
    assert srv.start()

    def connect():
        c = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        deadline = time.monotonic() + 5
        n = srv.client_count
        while time.monotonic() < deadline and srv.client_count == n:
            time.sleep(0.01)
        return c

    def receive(c, parser, want):
        """メッセージ want のパケットを1つ受け取る（STATUS は混ざってくる）。"""
        while True:
            for pkt in parser.pending:
                if lr.message_id(pkt) == want:
                    parser.pending.remove(pkt)
                    return pkt
            parser.pending += parser.feed(c.recv(4096))

    try:
        c = connect()
        parser = lr.PacketParser()
        parser.pending = []
        c.sendall(lr.encode(lr.MSG_DESCRIPTOR))
        assert receive(c, parser, lr.MSG_DESCRIPTOR) == lr.build_packet(lr.MSG_DESCRIPTOR, lr.DESCRIPTOR_BODY)

        # RF_SETTINGS: 8スロット分。受信器のある ch だけ有効で、分からないバンド名（CH3）は無効
        msg = lr.encode(lr.MSG_RF_SETTINGS)
        c.sendall(msg[:4])
        time.sleep(0.05)
        c.sendall(msg[4:])       # 分かれて届いても読む
        slots = [dict(lr.parse_fields(receive(c, parser, lr.MSG_RF_SETTINGS))) for _ in range(8)]
        assert [s[0x01][0] for s in slots] == list(range(1, 9))
        assert [s[0x20][0] for s in slots] == [1, 1, 0, 0, 0, 0, 0, 0]
        assert struct.unpack("<H", slots[0][0x25])[0] == 5740 and struct.unpack("<H", slots[1][0x25])[0] == 5705
        assert slots[1][0x22][0] == 3 and slots[1][0x21][0] == 1 and struct.unpack("<f", slots[0][0x23])[0] == 800.0
        assert all(struct.unpack("<H", s[0x25])[0] > 0 for s in slots)      # 無効なスロットにも周波数を入れる

        # TIME と通過は同じ時計（time.monotonic のマイクロ秒）
        c.sendall(lr.encode(lr.MSG_TIME))
        now = struct.unpack("<Q", dict(lr.parse_fields(receive(c, parser, lr.MSG_TIME)))[0x02])[0]
        assert abs(now / 1e6 - time.monotonic()) < 2

        c2 = connect()           # 通過は全員に届く
        p2 = lr.PacketParser()
        p2.pending = []
        srv.passing(1, 123.456789)
        srv.passing(1, 130.0)
        srv.passing(0, 131.0)
        srv.passing(99, 131.0)   # 範囲外は無視
        for con, par in ((c, parser), (c2, p2)):
            got = [dict(lr.parse_fields(receive(con, par, lr.MSG_DETECTION))) for _ in range(3)]
            assert [g[0x01][0] for g in got] == [2, 2, 1]
            assert [struct.unpack("<I", g[0x21])[0] for g in got] == [1, 2, 1]         # スロットごとの通し番号
            assert struct.unpack("<Q", got[0][0x02])[0] == 123456789                  # 最後に見えたフレームの時刻
            assert struct.unpack("<I", got[0][0x20])[0] == lr.DECODER_ID and got[0][0x23] == b"\0\0"

        status = lr.parse_fields(receive(c, parser, lr.MSG_STATUS))       # STATUS は定期的に届く
        assert [v[0] for f, v in status if f == 0x01] == list(range(1, 9))
        assert [struct.unpack("<f", v)[0] for f, v in status if f == 0x22] == [960.0, 960.0] + [0.0] * 6

        c2.close()               # 切断してもほかのクライアントには届く
        deadline = time.monotonic() + 5
        while srv.client_count != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert srv.client_count == 1
        srv.passing(2, 140.0)
        assert dict(lr.parse_fields(receive(c, parser, lr.MSG_DETECTION)))[0x01][0] == 3
        c.close()
    finally:
        srv.stop()
    assert srv.client_count == 0

    # ポートが使用中なら False（アプリは止まらない）
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    assert not lr.LapRFServer(blocker.getsockname()[1], host="127.0.0.1").start()
    blocker.close()


def test_laprf_settings():
    """既定はオフ。設定を変えたら知らせる。"""
    from app.settings_dialog import apply_general
    with tempfile.TemporaryDirectory() as d:
        c = Config(os.path.join(d, "config.yaml")).load()
        assert c.data["laprf"] == {"enabled": False}             # ポートは 5403 に固定（設定しない）
        ar = c.aruco
        general = {"root": c.firebase["root"], "credentials": c.firebase["credentials"],
                   "bands": [c.band_for(i) for i in range(4)], "device_names": c.receiver["device_names"],
                   **{k: ar[k] for k in ("marker_type", "min_size_pct", "min_markers", "fgm_line_step",
                                         "fgm_min_lines", "fgm_tol", "appear_ms", "flicker_ms")},
                   "no_video_clear_ms": 3000, "laprf_enabled": False}
        assert c.data["no_video"] == {"enabled": True, "clear_ms": 3000}      # 映像が無くなってからクリアするまでの既定は3秒
        assert not apply_general(c, general)["laprf"]
        assert apply_general(c, {**general, "laprf_enabled": True})["laprf"]
        assert c.data["display"]["theme"] == "dark"                           # 配色の既定はダーク
        assert not any(apply_general(c, {**general, "laprf_enabled": True,
                                         "theme": "light"}).values())         # 配色だけなら開き直さない
        c.save()
        c2 = Config(c.path).load()
        assert c2.data["laprf"] == {"enabled": True} and c2.data["display"]["theme"] == "light"

        # アプリのラップ確定から、最後に見えた時刻で通過が届く。起点も届く。オフなら何もしない
        import main
        from app import laprf as lr
        from app.capture import PassEvent
        c.data["laprf"]["enabled"] = True
        main.LAPRF_PORT = 0                       # テストでは空いているポートで開く
        app = main.App(c)
        app.sound = types.SimpleNamespace(play=lambda *a: None, announce=lambda *a, **k: None)
        app.restart_laprf()
        try:
            con = socket.create_connection(("127.0.0.1", app.laprf.port), timeout=5)
            deadline = time.monotonic() + 5
            while app.laprf.client_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            app.on_lap(PassEvent(1, "F4", 50.25, time.time(), 999.0, "TARO"))
            parser, got = lr.PacketParser(), []
            while not got:
                got = [p for p in parser.feed(con.recv(4096)) if lr.message_id(p) == lr.MSG_DETECTION]
            fields = dict(lr.parse_fields(got[0]))
            assert fields[0x01][0] == 2 and struct.unpack("<Q", fields[0x02])[0] == 50250000
            con.close()
            c.data["laprf"]["enabled"] = False
            app.restart_laprf()
            assert app.laprf is None
            app.on_lap(PassEvent(1, "F4", 60.5, time.time(), 10.25, "TARO", 1, 10.25))
        finally:
            if app.laprf:
                app.laprf.stop()


def test_ranking():
    """当日の全ラップをファイルに残し、ランキング（ベストラップ、3周連続、90秒の周回数）はそこから計算する。"""
    from app import laplog
    from app import ranking as rk
    from app import ui
    with tempfile.TemporaryDirectory() as d:
        folder = os.path.join(d, "laps")
        r = laplog.LapLog(folder)
        clock = [1000.0]

        def add(rx, pilot, lap, count=0, total=0.0):
            clock[0] += 1
            return r.add(rx, "F1", pilot, lap, count, total, clock[0])

        def fly(rx, pilot, laps):          # 起点から laps のラップで飛ぶ
            add(rx, pilot, 999.0)
            total = 0.0
            for i, lap in enumerate(laps):
                total = round(total + lap, 2)
                add(rx, pilot, lap, i + 1, total)

        def rank(sec=90):
            return r.ranking(sec)[:3]

        fly("usb0", "TARO", [15.0] * 7)        # 90.00 で6周（ちょうど90秒も数える）、7周目は数えない
        fly("usb1", "HANA", [14.0] * 6 + [9.5])  # 84.00 では足りず、93.50 で7周
        fly("usb2", "JIRO", [12.0] * 3)        # 90秒に届かない（耐久の記録なし）
        fly("usb0", "TARO", [16.0] * 6)        # 2回目は 96.00 で6周（1回目の方が良い）
        best, consecutive, end = rank()
        assert best == [("HANA", 9.5), ("JIRO", 12.0), ("TARO", 15.0)], best
        assert consecutive == [("JIRO", 36.0), ("HANA", 37.5), ("TARO", 45.0)], consecutive   # 続けて飛んだ3周の最短
        assert end == [("HANA", 7, 93.5), ("TARO", 6, 90.0)], end
        # 一覧に付ける印: 3周連続の記録の3周と、90秒の記録になった通過（パイロットごと）
        marks = r.ranking(90)[4]
        hana = r.laps["usb1"]
        assert {l.wall for l in hana[5:8]} <= marks["consecutive"] and hana[4].wall not in marks["consecutive"]
        assert len(marks["consecutive"]) == 9                     # 3人 × 3周
        assert marks["endurance"] == {hana[7].wall, r.laps["usb0"][6].wall}   # HANA の7周目、TARO の1回目の6周目
        lines = ui.lap_list_lines([l.row() for l in reversed(hana)], r.ranking(90)[3], marks)
        assert [x[5] for x in lines] == [True, True, True, False, False, False, False, False]
        assert [x[6] for x in lines] == [True] + [False] * 7
        assert rank(100)[2] == [("TARO", 7, 105.0)]                      # 耐久の秒数を変えると計算し直す

        # 3周連続: セッション（起点）を跨いだ3周は数えない。3周ごとの区切りではなく、どの続けて飛んだ3周でもよい
        fly("usb0", "KEN", [5.0, 5.0])
        fly("usb0", "KEN", [5.0])
        assert "KEN" not in dict(rank()[1])
        fly("usb1", "MIO", [20.0, 10.0, 10.0, 10.0, 20.0])
        assert dict(rank()[1])["MIO"] == 30.0
        for i in range(3):                  # 起点を見ていない受信器の耐久と3周連続は数えない
            add("usb3", "SABU", 20.0, 5 + i, 100.0 + i)
        assert "SABU" not in dict(rank()[1]) and "SABU" not in [e[0] for e in rank()[2]]
        assert ("SABU", 20.0) in rank()[0]
        # ベストラップの更新（アナウンス用）: その日の最初のラップも更新。同じタイム・遅いタイムは更新しない
        assert add("usb2", "NEW", 999.0) is False                  # 起点
        assert add("usb2", "NEW", 12.0, 1, 12.0) is True           # 最初のラップ
        assert add("usb2", "NEW", 12.0, 2, 24.0) is False          # 同じタイム
        assert add("usb2", "NEW", 13.0, 3, 37.0) is False
        assert add("usb2", "NEW", 11.5, 4, 48.5) is True
        before = rank()

        # メイン画面の一覧は新しい順
        rows = r.newest("usb2", 0, 3)
        assert [x[1] for x in rows] == [11.5, 13.0, 12.0] and r.count("usb2") == 9
        assert [x[1] for x in r.newest("usb2", 7, 5)] == [12.0, 999.0]

        # ミス計測のラップを無効にする: ランキングから外し、2番目の記録が繰り上がる。もう一度で戻す
        hana_95 = next(l for l in r.laps["usb1"] if l.lap == 9.5)
        assert r.toggle_void("usb1", hana_95.wall).void is True
        best, consecutive, end = rank()
        assert dict(best)["HANA"] == 14.0 and dict(consecutive)["HANA"] == 42.0
        assert "HANA" not in dict((e[0], e) for e in end)         # そのセッションの耐久は数えない
        assert r.ranking(90)[3]["HANA"] == r.laps["usb1"][1].wall # 緑にするのは繰り上がった 14.0（最初の1周）
        jiro = r.laps["usb2"][2]                                   # 3周連続の途中を無効にすると、そこで途切れる
        r.toggle_void("usb2", jiro.wall)
        assert "JIRO" not in dict(rank()[1]) and dict(rank()[0])["JIRO"] == 12.0
        assert r.newest("usb2", r.count("usb2") - 3, 1)[0][5] is True       # 一覧の行にも無効が付く
        assert r.toggle_void("usb2", r.laps["usb2"][0].wall) is None        # 起点は無効にできない
        assert r.toggle_void("usb2", 1.0) is None                           # 無いラップ
        r2 = laplog.LapLog(folder)                                  # 起動し直しても、無効にしたものも含めて残る
        assert r2.ranking(90)[:3] == rank()
        r.toggle_void("usb1", hana_95.wall)
        r.toggle_void("usb2", jiro.wall)
        assert rank() == before
        with open(r.path, "a", encoding="utf-8") as f:              # 落ちたときの書きかけの行は飛ばす
            f.write('{"wall": 12')
        assert laplog.LapLog(folder).ranking(90)[:3] == before

        img = rk.render_ranking(*before)
        assert img.shape == (rk.WIN_H, rk.WIN_W, 3)
        for y in (5, rk.WIN_H // 3 + 5, rk.WIN_H * 2 // 3 + 5):         # 3つの表を縦に並べる（各表の見出しの帯）
            assert (img[y] == ui.theme().panel).all(axis=1).sum() > rk.WIN_W // 2
        assert rk.laps_time_str(10, 93.5) == "10/1:33.50" and rk.laps_time_str(6, 59.999) == "6/1:00.00"
        assert rk.render_ranking([], [], []).shape == (rk.WIN_H, rk.WIN_W, 3)
        best, consecutive, end = before
        assert rk.render_ranking(best * 50, consecutive * 50, end * 50, 90, 500, 300).shape == (300, 500, 3)   # 入りきらない

        # ファイルは日付で分けない1つだけ（laps/laps.jsonl）
        assert os.listdir(folder) == ["laps.jsonl"] and r.path == laplog.laps_path(folder)


def test_clear_laps_on_start():
    """起動時にラップのファイルがあれば消すか聞き、OK なら消して空から始める。Cancel なら残して続ける。"""
    import main
    from app import laplog
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(os.path.join(d, "config.yaml")).load()
        folder = main.laps_folder(cfg)
        asked = []

        def ask(answer):
            def f(n, path):
                asked.append((n, path))
                return answer
            return f

        main.confirm_clear_laps(folder, ask(True))           # ファイルが無ければ聞かない
        assert asked == [] and laplog.stored_passes(folder) is None
        r = laplog.LapLog(folder)
        r.add("usb0", "F1", "TARO", 999.0, 0, 0.0, 1000.0)
        r.add("usb0", "F1", "TARO", 10.0, 1, 10.0, 1010.0)
        r.toggle_void("usb0", 1010.0)                       # 無効化の行は通過に数えない
        main.confirm_clear_laps(folder, ask(False))          # Cancel: 残して、続きから書き足す
        assert asked == [(2, laplog.laps_path(folder))] and laplog.LapLog(folder).count("usb0") == 2
        main.confirm_clear_laps(folder, ask(True))           # OK: 消して空から
        assert not os.path.exists(laplog.laps_path(folder)) and laplog.LapLog(folder).count("usb0") == 0

        def broken(n, path):
            raise RuntimeError("no tkinter")
        laplog.LapLog(folder).add("usb0", "F1", "TARO", 999.0, 0, 0.0, 2000.0)
        main.confirm_clear_laps(folder, broken)             # ダイアログが開けなければ消さない
        assert laplog.stored_passes(folder) == 1


def test_error_sound():
    """minLap 以下の通過は errorlap の音。ただし3秒以下（ゲートの二重検出など）は鳴らさない。"""
    import main
    from app.capture import ChannelWorker
    played, ignored, laps = [], [], []
    with tempfile.TemporaryDirectory() as d:
        app = main.App(Config(os.path.join(d, "config.yaml")).load())
        app.sound = types.SimpleNamespace(play=lambda k: played.append(k), announce=lambda *a, **k: None)
        app.on_ignored(0, "F1", 2.99)
        app.on_ignored(0, "F1", 3.0)
        assert played == []
        app.on_ignored(0, "F1", 3.01)
        assert played == ["error"]
    # 取り込み側は、前に数えた通過からのラップを知らせる
    w = types.SimpleNamespace(lock=threading.Lock(), measuring=True, timer=LapTimer(), min_lap_fn=lambda: 5.0,
                              pilot=None, band="F1", ch=0, on_ignored=lambda *a: ignored.append(a),
                              on_lap=laps.append)
    for t in (10.0, 11.5, 14.25, 20.0):
        ChannelWorker._emit(w, t, t)
    assert ignored == [(0, "F1", 1.5), (0, "F1", 4.25)], ignored
    assert [e.lap for e in laps] == [999.0, 10.0]


def test_void_lap():
    """メイン画面の一覧の行をクリックして選び、Delete でミス計測のラップを無効にする（もう一度で戻す）。"""
    import main
    from app import ui
    from app.capture import PassEvent
    with tempfile.TemporaryDirectory() as d:
        app = main.App(Config(os.path.join(d, "config.yaml")).load())
        app.sound = types.SimpleNamespace(play=lambda *a: None, announce=lambda *a, **k: None)
        app.workers = [types.SimpleNamespace(path=f"usb{i}", source=i) for i in range(2)]
        app.measuring = True
        app.on_lap(PassEvent(0, "F1", 1.0, 1000.0, 999.0, "TARO"))
        for i, lap in enumerate((10.0, 8.0, 9.0)):
            app.on_lap(PassEvent(0, "F1", 2.0 + i, 1001.0 + i, lap, "TARO", i + 1, 10.0 * (i + 1)))
        assert app.laplog.ranking(90)[0] == [("TARO", 8.0)]
        lx, ly, lw, lh = ui.list_rects(2)[0]

        def click(row):
            app.on_mouse(cv2.EVENT_LBUTTONDOWN, lx + lw // 2, ly + 6 + row * ui.LIST_ROW_H + 5, 0, None)

        click(1)                                    # 新しい方から2行目（8.00）
        assert app.selected == ("usb0", 1002.0) and app.clicked_ch is None
        assert app.lap_lists()[0][3] == 1002.0 and app.lap_lists()[1][3] is None
        app.void_selected()
        entries = app.lap_lists()[0][0]
        assert entries[1][1] == 8.0 and entries[1][5] is True
        assert app.laplog.ranking(90)[0] == [("TARO", 9.0)]                # 繰り上がる
        assert ui.lap_list_lines(entries)[1][3:5] == (ui.theme().sub, True)  # 灰色で線を引く
        canvas = np.zeros((ui.CANVAS_H, ui.CANVAS_W, 3), np.uint8)
        e, s, t, sel = app.lap_lists()[0]
        ui.draw_lap_list(canvas, ui.list_rects(2)[0], e, {}, s, t, sel)
        assert canvas.any()
        app.void_selected()                         # もう一度で戻す
        assert app.laplog.ranking(90)[0] == [("TARO", 8.0)]
        click(1)                                    # 同じ行をもう一度クリックすると選ぶのをやめる
        assert app.selected is None
        click(3)                                    # 起点（区切り）と空の行は選ばない
        assert app.selected is None
        click(0)
        click(10)
        assert app.selected is None
        app.void_selected()                         # 何も選んでいなければ何もしない
        assert ui.list_row_at(0, lx + 5, ly + 2, 2) is None and ui.list_row_at(0, lx + 5, ly + 7, 2) == 0


def test_render_pacer():
    """取り込みの FPS が 28 を下回ったら描画を間引き、戻って3秒続いたら元に戻す。"""
    from app.ui import RenderPacer
    p = RenderPacer(fps=30, low_fps=10, threshold=28, recover_sec=3)
    assert not p.update(0.0, [30, 29.5]) and not p.low
    assert p.update(1.0, [30, 25]) and p.low                 # 1ch でも下回れば間引く
    assert not p.update(2.0, [30, 30]) and p.low             # 戻ってもすぐには戻さない
    assert not p.update(3.0, [30, 27]) and p.low             # また下がったら数え直し
    assert not p.update(4.0, [30, 30]) and p.low
    assert p.update(7.0, [30, 30]) and not p.low             # 3秒続いたら戻す
    assert not p.update(8.0, [30, 0]) and not p.low          # 映像の来ていない ch（0）は見ない
    # 描く間隔: ふだん 30fps、間引くと 10fps
    p = RenderPacer(fps=30, low_fps=10)
    times = [i / 100 for i in range(100)]                    # 1秒を 10ms 刻み
    assert 29 <= sum(p.due(t) for t in times) <= 31
    p = RenderPacer(fps=30, low_fps=10)
    p.update(0.0, [20])
    assert sum(p.due(t) for t in times) == 10


def test_elapsed_timer():
    """映像の右下のタイマー: 最後の通過からの経過時間（10ms 未満切り捨て）。通過が無ければ --.--。"""
    from app import ui
    from app.capture import ChannelWorker
    from app.laptimer import Detection
    assert ui.format_elapsed(None) == "--.--"
    assert ui.format_elapsed(0) == "0.00" and ui.format_elapsed(8.799) == "8.79"
    assert ui.format_elapsed(59.999) == "59.99" and ui.format_elapsed(65.3) == "1:05.30"
    assert ui.format_elapsed(-0.01) == "0.00"
    canvas = np.zeros((ui.CANVAS_H, ui.CANVAS_W, 3), np.uint8)
    rect = ui.tile_rects(2)[0]
    frame = np.full((480, 640, 3), 90, np.uint8)
    for last_pass in (None, 100.0):
        ui.draw_tile(canvas, rect, "F1", frame, Detection(False), False, last_pass, True, 30.0, now=112.5)
    # ワーカーは最後に数えた通過（起点を含む）の時刻を返す
    w = ChannelWorker(0, "F1", 0, None, {}, make_detector({}), 100, lambda ev: None, lambda: 3,
                      qr_cfg={"enabled": False})
    assert w.snapshot()[3] is None
    w.timer.on_pass(5.0, 3)
    assert w.snapshot()[3] == 5.0


def test_version():
    """バージョンは日付（YYYYMMDD）。exe は作った日、ソースからは最後のコミットの日付に -dev。"""
    import re
    from app import ui, version
    assert re.fullmatch(r"\d{8}(-dev)?|dev", version.VERSION), version.VERSION
    assert not os.path.exists(os.path.join(ROOT, "app", "_build_version.py"))   # ビルドの後に残っていない
    sys.modules["app._build_version"] = types.SimpleNamespace(VERSION="20261110")  # exe のとき
    try:
        assert version.get_version() == "20261110"
    finally:
        del sys.modules["app._build_version"]
    for measuring in (False, True):         # 待機中は帯にバージョンを出す
        ui.render([], measuring, 0, "OK", 0, "x", fb_local=True, version="20261110")
    assert ui.version_str("20261110") == "d-practice-cam ver. 20261110"


def test_theme():
    """配色: dark（既定）と light（白い地）。メイン画面とランキングの地がいまの配色になる。"""
    from app import ranking as rk
    from app import ui
    try:
        assert ui.DEFAULT_THEME == "dark" and ui.set_theme("unknown").name == "dark"
        for name in ("light", "dark"):
            t = ui.set_theme(name)
            img = ui.render([], False, 0, "OK", 0, "x", fb_local=True)
            assert tuple(img[ui.CANVAS_H - 5, 5]) == t.bg and tuple(img[5, 5]) == t.panel
            assert tuple(rk.render_ranking([], [], [])[rk.WIN_H - 5, 5]) == t.tile
        light, dark = ui.THEMES["light"], ui.THEMES["dark"]
        assert sum(light.bg) > 700 and sum(light.text) < 50          # 白い地に黒い文字
        assert sum(dark.bg) < 100 and sum(dark.text) > 700
    finally:
        ui.set_theme(ui.DEFAULT_THEME)


def test_lap_scroll():
    """映像の下のタイムをマウスホイールで ch ごとにスクロールする。古い所を見ている間は新しい行が来てもずらさない。"""
    import main
    from app import ui
    from app.capture import PassEvent
    with tempfile.TemporaryDirectory() as d:
        app = main.App(Config(os.path.join(d, "config.yaml")).load())
        app.sound = types.SimpleNamespace(play=lambda *a: None, announce=lambda *a, **k: None)
        app.workers = [types.SimpleNamespace(path=f"usb{i}", source=i) for i in range(4)]
        n_vis = ui.list_visible_rows(4)

        def lap(ch, i):
            app.on_lap(PassEvent(ch, "F1", 10.0 + i, 1000.0 + i, 10.0 + i, "TARO", i + 1, 10.0 * (i + 1)))

        for i in range(30):
            lap(0, i)
        entries, scroll, total, _ = app.lap_lists()[0]
        assert (scroll, total) == (0, 30) and entries[0][1] == 39.0          # 最新が一番上
        assert app.lap_lists()[1] == ([], 0, 0, None)
        # マウスの下の列だけ、ホイールを下へ回すと3行ずつ古い方へ
        lx, ly, lw, lh = ui.list_rects(4)[0]
        app.on_mouse(cv2.EVENT_MOUSEMOVE, lx + lw // 2, ly + lh // 2, 0, None)
        app.on_mouse(cv2.EVENT_MOUSEWHEEL, 0, 0, (-120 & 0xFFFF) << 16, None)
        entries, scroll, _, _ = app.lap_lists()[0]
        assert scroll == 3 and entries[0][1] == 36.0
        lap(0, 30)                                                          # 新しい行が来ても見ている所は同じ
        entries, scroll, total, _ = app.lap_lists()[0]
        assert scroll == 4 and entries[0][1] == 36.0 and total == 31
        app.on_mouse(cv2.EVENT_MOUSEWHEEL, 0, 0, 120 << 16, None)            # 上へ回すと新しい方へ
        assert app.lap_lists()[0][1] == 1
        app.scroll_laps(0, -100)
        assert app.lap_lists()[0][1] == 0
        app.scroll_laps(0, 999)                                             # いちばん古い行が一番下まで
        assert app.lap_lists()[0][1] == 31 - n_vis
        app.on_mouse(cv2.EVENT_MOUSEMOVE, *ui.tile_rects(4)[0][:2], 0, None)  # 映像の上では何もしない
        app.on_mouse(cv2.EVENT_MOUSEWHEEL, 0, 0, 120 << 16, None)
        assert app.lap_lists()[0][1] == 31 - n_vis
        assert ui.list_at(lx + 1, ly + 1, 4) == 0 and ui.list_at(lx + 1, ly - 1, 4) is None
        # 描いてもスクロールバーが出るだけで落ちない
        canvas = np.zeros((ui.CANVAS_H, ui.CANVAS_W, 3), np.uint8)
        e, s, t, _ = app.lap_lists()[0]
        ui.draw_lap_list(canvas, ui.list_rects(4)[0], e, {}, s, t)
        assert canvas.any()


def test_lap_list():
    """メイン画面: ch を横に並べ、映像の下にその ch のタイムを新しい順に出す（起点は ---）。"""
    from app import ui
    for n in (1, 2, 3, 4):
        tiles, lists = ui.tile_rects(n), ui.list_rects(n)
        assert len(tiles) == len(lists) == n
        for (x, y, w, h), (lx, ly, lw, lh) in zip(tiles, lists):
            assert (lx, lw) == (x, w) and ly == y + h and ly + lh == ui.CANVAS_H and lh > 4 * ui.LIST_ROW_H
    # 新しい順。2つ目のセッション（TARO）と、その前のセッション（F1）
    # 新しい順の (pilot, lap, lap_count, total, wall)。2つ目のセッション（TARO）と、その前のセッション（F1）
    entries = [("TARO", 9.5, 3, 28.4, 108.0), ("TARO", 8.8, 2, 18.9, 99.0), ("TARO", 10.1, 1, 10.1, 90.0),
               ("TARO", 999, 0, 0.0, 80.0), ("F1", 12.0, 1, 12.0, 50.0), ("F1", 999, 0, 0.0, 38.0)]
    lines = ui.lap_list_lines(entries)
    sep = [ui.start_label(80.0), ui.start_label(38.0)]
    assert [l[0] for l in lines] == ["TARO", "TARO", "TARO", sep[0], "F1", sep[1]]
    assert re.fullmatch(r"----\d\d:\d\d:\d\d----", sep[0])                 # 起点は ----HH:MM:SS----
    assert lines[0][1:3] == ("9.50", "[03] 0:28.40")                       # ラップ、周回数とトータル
    assert lines[3][1:3] == ("", "")                                       # 起点（区切り）
    t = ui.theme()
    assert all(l[3] in (t.text, t.sub) for l in lines)                    # ランキングが無ければ緑は無い
    # ランキングの BEST LAP に載ったラップ（通過時刻で見分ける）だけを緑にする。1周だけのパイロットも
    lines = ui.lap_list_lines(entries, {"TARO": 99.0, "F1": 50.0, "HANAKO": 1.0})
    assert [l[3] for l in lines] == [t.text, t.good, t.text, t.sub, t.good, t.sub]
    # 同じタイムが2回でも、ランキングに載った方（通過時刻が同じ方）だけ。別の ch・前のセッションのベストなら緑は無い
    same = [("TARO", 9.0, 2, 19.0, 20.0), ("TARO", 9.0, 1, 10.0, 11.0), ("TARO", 999, 0, 0.0, 1.0)]
    assert [l[3] for l in ui.lap_list_lines(same, {"TARO": 11.0})] == [t.text, t.good, t.sub]
    assert [l[3] for l in ui.lap_list_lines(same, {"TARO": 5.0})] == [t.text, t.text, t.sub]
    assert ui.lap_list_lines([("野比", 9.0, 1, 9.0, 1.0)])[0][0] == "??"    # 日本語は描けないので ?
    assert ui.session_str(5, 77.25) == "[05] 1:17.25" and ui.session_str(12, 59.999) == "[12] 1:00.00"
    canvas = np.zeros((ui.CANVAS_H, ui.CANVAS_W, 3), np.uint8)
    ui.draw_lap_list(canvas, ui.list_rects(4)[0], entries * 20)            # 入りきらない分は出さない
    assert canvas.any()


def test_lap_scrollbar():
    """スクロールバーのつまみをドラッグ、つまみの上下をクリックで1画面ずつ動く。計測中もできる。"""
    import main
    from app import ui
    from app.capture import PassEvent
    with tempfile.TemporaryDirectory() as d:
        app = main.App(Config(os.path.join(d, "config.yaml")).load())
        app.sound = types.SimpleNamespace(play=lambda *a: None, announce=lambda *a, **k: None)
        app.workers = [types.SimpleNamespace(path=f"usb{i}", source=i) for i in range(4)]
        app.measuring = True
        n_vis = ui.list_visible_rows(4)
        total = n_vis * 5
        for i in range(total):
            app.on_lap(PassEvent(1, "F1", 10.0 + i, 1000.0 + i, 10.0 + i, "TARO", i + 1, 10.0 * (i + 1)))
        max_scroll = total - n_vis
        lx, ly, lw, lh = ui.list_rects(4)[1]
        bx = lx + lw - 6                                    # バーの上
        _, _, top, span, thumb, ty = ui._scrollbar(ui.list_rects(4)[1], total, 0)
        # つまみの下をクリックすると1画面古い方へ、上をクリックすると新しい方へ
        app.on_mouse(cv2.EVENT_LBUTTONDOWN, bx, top + span - 2, 0, None)
        assert app.lap_lists()[1][1] == n_vis and app.clicked_ch is None    # バンドの変更にはしない
        app.on_mouse(cv2.EVENT_LBUTTONUP, bx, top + span - 2, 0, None)
        _, _, _, _, _, ty = ui._scrollbar(ui.list_rects(4)[1], total, n_vis)
        app.on_mouse(cv2.EVENT_LBUTTONDOWN, bx, top + 1, 0, None)
        app.on_mouse(cv2.EVENT_LBUTTONUP, bx, top + 1, 0, None)
        assert app.lap_lists()[1][1] == 0
        # つまみをつかんで一番下までドラッグ → いちばん古い所。一番上まで戻すと最新
        app.on_mouse(cv2.EVENT_LBUTTONDOWN, bx, top + 3, 0, None)
        assert app.sb_drag == (1, 3)
        app.on_mouse(cv2.EVENT_MOUSEMOVE, bx, top + span + 50, cv2.EVENT_FLAG_LBUTTON, None)
        assert app.lap_lists()[1][1] == max_scroll
        app.on_mouse(cv2.EVENT_MOUSEMOVE, bx + 40, top + (span - thumb) // 2 + 3, cv2.EVENT_FLAG_LBUTTON, None)
        assert abs(app.lap_lists()[1][1] - max_scroll // 2) <= 1          # 横にずれても真ん中なら真ん中
        app.on_mouse(cv2.EVENT_MOUSEMOVE, bx, top - 50, cv2.EVENT_FLAG_LBUTTON, None)
        assert app.lap_lists()[1][1] == 0
        app.on_mouse(cv2.EVENT_LBUTTONUP, bx, top, 0, None)
        assert app.sb_drag is None
        app.on_mouse(cv2.EVENT_MOUSEMOVE, bx, top + span, 0, None)         # 離した後は動かさない
        assert app.lap_lists()[1][1] == 0
        # バーの外のクリックや、入りきっている ch（バーが無い）ではスクロールしない
        assert not app.click_scrollbar(lx + lw // 2, ly + lh // 2)
        assert not app.click_scrollbar(ui.list_rects(4)[0][0] + ui.list_rects(4)[0][2] - 6, top + 5)


def test_view_size():
    """ウィンドウの大きさで描く。タイムの一覧は文字の大きさを変えず、ウィンドウが高いほど行が増える。"""
    from app import ui
    from app.laptimer import Detection

    class W:            # 描くのに要るだけの受信器
        band, no_signal = "F1", False

        def snapshot(self):
            return np.full((480, 640, 3), 90, np.uint8), Detection(False), None, None, 30.0, "", None

    entries = [("TARO", 9.5, i, 9.5 * i, float(i)) for i in range(200, 0, -1)]
    try:
        for n in (1, 2, 4):
            rows = {}
            for size in ((1280, 720), (1920, 1080), (1920, 1200), (3840, 2160), (1024, 768), (800, 600)):
                v = ui.set_view(*size, n)
                img = ui.render([W()] * n, True, 0.0, "OK", 0, "x", lap_lists=[(entries, 0, len(entries))] * n)
                assert img.shape == (size[1], size[0], 3), (n, size, img.shape)
                rows[size] = ui.list_visible_rows(n)
                lists, tiles = ui.list_rects(n), ui.view_tile_rects(n)
                for (lx, ly, lw, lh), (tx, ty, tw, th) in zip(lists, tiles):
                    assert (lx, lw) == (tx, tw) and ly == ty + th and ly + lh == size[1]     # 映像の真下から下端まで
                assert lists[-1][0] + lists[-1][2] <= size[0]
                assert rows[size] >= ui.MIN_LIST_ROWS
                assert ui.list_at(lists[0][0] + 1, lists[0][1] + 1, n) == 0
                assert ui.list_at(lists[0][0] + 1, lists[0][1] - 1, n) is None
            assert rows[(1920, 1200)] > rows[(1920, 1080)] >= rows[(1280, 720)]    # 高いほど行が多い
            assert rows[(3840, 2160)] > 2 * rows[(1280, 720)]
        # 1280×720 は今までと同じ配置
        ui.set_view(1280, 720, 4)
        assert ui.view_tile_rects(4) == ui.tile_rects(4) and ui.view().scale == 1.0
        # 横長すぎるウィンドウでは、一覧の行を残すため上の段を縮めて中央に寄せる
        v = ui.set_view(2560, 600, 1)
        assert v.scale < 2560 / ui.CANVAS_W and v.x0 > 0 and ui.list_visible_rows(1) >= ui.MIN_LIST_ROWS
    finally:
        ui.set_view(ui.CANVAS_W, ui.CANVAS_H, 4)


def test_first_run_language():
    """初めて起動したとき（config.yaml が無い）、sound/voice に wav があれば（日本語版の zip）日本語で始める。"""
    with tempfile.TemporaryDirectory() as d:
        assert Config(os.path.join(d, "config.yaml")).load().data["speech"]["language"] == "en"   # 英語版
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "sound", "voice"))
        open(os.path.join(d, "sound", "voice", "entry.wav"), "wb").close()
        path = os.path.join(d, "config.yaml")
        assert Config(path).load().data["speech"]["language"] == "ja"                          # 日本語版
        c = Config(path).load()
        c.data["speech"]["language"] = "en"
        c.save()
        assert Config(path).load().data["speech"]["language"] == "en"     # config.yaml があればその値（上書きしない）


def test_settings_qr_speech():
    from app.settings_dialog import apply_qr_speech
    with tempfile.TemporaryDirectory() as d:
        c = Config(os.path.join(d, "config.yaml")).load()
        sp = {"enabled": True, "text": "{name}", "voice": ""}
        assert not apply_qr_speech(c, {"qr": dict(c.qr), "speech": sp})        # QR は変わらない
        assert c.data["speech"]["text"] == "{name}"
        assert apply_qr_speech(c, {"qr": {**c.qr, "interval_ms": 300}, "speech": sp})
        c.data["speech"]["readings"] = {"A": "えー"}      # 廃止した読み方の辞書は読み込み時に消す
        c.data["speech"]["wav_dir"] = "./wav"             # 音声パックのフォルダは sound/voice に固定（読み込み時に消す）
        c.save()
        c2 = Config(c.path).load()
        assert c2.qr["interval_ms"] == 300 and c2.data["speech"]["text"] == "{name}"
        assert "readings" not in c2.data["speech"] and "wav_dir" not in c2.data["speech"]


def test_config_bar_to_fgm():
    """旧名（marker_type: bar、bar_*）の config.yaml も FGM として読める。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("aruco:\n  marker_type: bar\n  bar_line_step: 2\n  bar_min_lines: 5\n")
        ar = Config(path).load().aruco
        assert ar["marker_type"] == "fgm" and ar["fgm_line_step"] == 2 and ar["fgm_min_lines"] == 5
        assert "bar_line_step" not in ar and isinstance(make_detector(ar), FgmDetector)


def test_config_and_root():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "config.yaml")
        c = Config(p).load()                          # 無ければ作る
        assert c.receiver["device_names"] == ["USB2.0 PC CAMERA", "USB Video"]
        fp = os.path.join(d, "firebase.yaml")
        assert os.path.exists(p) and not os.path.exists(fp)    # firebase.yaml はアプリからは作らない
        c.firebase["root"] = "changed"
        c.save()
        assert not os.path.exists(fp)
        with open(fp, "w", encoding="utf-8") as f:              # 自分で作ると読み書きされる
            f.write("database_url: https://example.firebaseio.com/\nroot: testroot\n")
        c = Config(p).load()
        assert c.firebase["root"] == "testroot" and c.firebase["timeout_sec"] == 10   # 足りない項目は既定値
        c.track_setting["minLap"] = 7
        c.save()
        c2 = Config(p).load()
        assert c2.firebase["root"] == "testroot" and c2.track_setting["minLap"] == 7
        assert c2.firebase["database_url"] == "https://example.firebaseio.com/"
        # Firebase の設定は firebase.yaml だけに書き、config.yaml には書かない
        import yaml
        assert "firebase" not in yaml.safe_load(open(p, encoding="utf-8"))
        assert yaml.safe_load(open(fp, encoding="utf-8"))["root"] == "testroot"

    # config.yaml に firebase: が書いてあっても読まない（Firebase の設定は firebase.yaml だけ）
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("firebase:\n  root: oldroot\n  database_url: https://old.example/\ntrack_setting:\n  minLap: 8\n")
        c = Config(p).load()
        assert c.firebase["database_url"] == "" and c.firebase["root"] == "practice"
        assert c.track_setting["minLap"] == 8
        assert not os.path.exists(os.path.join(d, "firebase.yaml"))      # 移さない
    assert validate_root("practice") is None
    for bad in ["", "a.b", "a/b", "a#b", "a$b", "a[b]"]:
        assert validate_root(bad)


def test_firebase_and_video_end_to_end():
    fake = install_fake_firebase()
    from app.capture import ChannelWorker
    from app.firebase_sync import FirebaseSync, today_str

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(os.path.join(d, "config.yaml")).load()
        open(os.path.join(d, "serviceAccount.json"), "w").write("{}")
        cfg.firebase["root"] = "testroot"
        cfg.firebase["database_url"] = "https://example.firebaseio.com/"
        cfg.track_setting["minLap"] = 2

        fb = FirebaseSync(cfg)
        assert fb.connect()
        fb.ensure_day_node()
        time.sleep(0.3)
        laps_path = f"testroot/laps_{today_str()}"
        assert fake._get(f"{laps_path}/!trackSetting")["minLap"] == 2     # 日付ノードを新規作成
        assert fb.min_lap() == 2

        # 既存の !trackSetting は上書きしない
        fake._get(f"{laps_path}/!trackSetting")["extra"] = "keep"
        fb.ensure_day_node()
        time.sleep(0.2)
        assert fake._get(f"{laps_path}/!trackSetting")["extra"] == "keep"

        # 設定画面からの変更（変えた項目だけ更新）
        fb.update_track_setting({"minLap": 2.5})
        time.sleep(0.2)
        ts = fake._get(f"{laps_path}/!trackSetting")
        assert ts["minLap"] == 2.5 and ts["extra"] == "keep" and fb.min_lap() == 2.5

        # 合成動画: 1.0s, 4.0s, 5.0s(短すぎ→無視), 7.5s にゲート通過
        fps = 30
        video = os.path.join(d, "test.avi")
        vw = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 480))
        visible_until = [0.7, 1.0, 3.7, 4.0, 4.7, 5.0, 7.2, 7.5]
        spans = list(zip(visible_until[::2], visible_until[1::2]))
        for i in range(int(8.5 * fps)):
            ts = i / fps
            vis = any(a <= ts < b for a, b in spans) and i != int(3.85 * fps)  # 途中で1フレーム欠け
            vw.write(frame_with_markers() if vis else blank())
        vw.release()

        laps = []

        def on_lap(ev):
            laps.append(ev.lap)
            fb.push_lap(ev.pilot_name, ev.lap, ev.wall)

        det = MarkerDetector(ids=[0, 1, 2, 3])
        w = ChannelWorker(0, "F4", video, None, cfg.capture, det, 100, on_lap, fb.min_lap)
        w.set_measuring(True)
        w.start()
        time.sleep(9.5)
        w.stop()
        time.sleep(0.3)

        print("laps:", laps)
        assert laps[0] == 999
        assert len(laps) == 3, laps
        assert abs(laps[1] - 3.0) < 0.1 and abs(laps[2] - 3.5) < 0.1
        sent = [v for k, v in fake._get(laps_path).items() if not k.startswith("!")]
        assert [s["lapTime"] for s in sent] == laps
        assert all(s["pilotName"] == "F4" and len(s["time"]) == 8 for s in sent)
        print("sent:", sent)



def test_pilot_name_from_qr():
    """起動時の QR でパイロット名を読む。新しい QR（3秒以上見えなかった後）なら同じ名前でもすぐ起点にし、
    ゲート通過が pilot_timeout_sec 無ければクリアしてバンド名に戻す。"""
    from app.capture import ChannelWorker
    with tempfile.TemporaryDirectory() as d:
        # QR: 0〜0.6s（TARO。表示中に何度も読むがエントリーは1回）,
        #     4.0〜4.6s（同じ TARO を起動し直した。タイムアウト前でもリセットしてエントリー）
        # 通過: 1.0s 起点, 2.5s, 5.0s 起点, 6.5s → 3秒通過が無いので 9.6s ごろクリア
        fps = 30
        video = os.path.join(d, "qr.avi")
        vw = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 480))
        spans = [(0.7, 1.0), (2.2, 2.5), (4.7, 5.0), (6.2, 6.5)]
        for i in range(int(10.2 * fps)):
            ts = i / fps
            if ts < 0.6 or 4.0 <= ts < 4.6:
                vw.write(frame_with_qr("TARO"))
            elif any(a <= ts < b for a, b in spans):
                vw.write(frame_with_markers())
            else:
                vw.write(blank())
        vw.release()

        events, pilots, clears = [], [], []
        qr_cfg = {"enabled": True, "interval_ms": 100, "pilot_timeout_sec": 3}
        w = ChannelWorker(0, "F4", video, None, {}, MarkerDetector(ids=[0, 1, 2, 3]), 100,
                          events.append, lambda: 1, qr_cfg=qr_cfg,
                          on_pilot=lambda ch, band, name: pilots.append(name),
                          on_clear=lambda ch, band, name: clears.append((band, name)))
        w.set_measuring(True)
        w.start()
        time.sleep(11.0)
        w.stop()

        names = [(e.pilot_name, e.lap) for e in events]
        print("pilot names:", names)
        assert [n for n, _ in names] == ["TARO"] * 4, names
        laps = [lap for _, lap in names]
        assert laps[0] == 999 and laps[2] == 999, names               # QR を読んだ後の通過は起点（同じ名前でも）
        assert abs(laps[1] - 1.5) < 0.1 and abs(laps[3] - 1.5) < 0.1, names
        assert pilots == ["TARO", "TARO"], pilots   # 表示中の読み直しでは読み上げず、起動し直したら読み上げる
        assert clears == [("F4", "TARO")], clears   # 3秒通過が無かったところでクリアを1回
        assert w.pilot is None


def test_idle_reset_band():
    """バンド名の ch でも、pilot_timeout_sec 秒ゲート通過が無ければ次の通過を起点（999）にする。"""
    from app.capture import ChannelWorker
    with tempfile.TemporaryDirectory() as d:
        # 通過: 1.0s 起点, 2.5s, 6.0s（2秒以上通過が無かったので起点）
        fps = 30
        video = os.path.join(d, "idle.avi")
        vw = cv2.VideoWriter(video, cv2.VideoWriter_fourcc(*"MJPG"), fps, (640, 480))
        spans = [(0.7, 1.0), (2.2, 2.5), (5.7, 6.0)]
        for i in range(int(6.8 * fps)):
            ts = i / fps
            vw.write(frame_with_markers() if any(a <= ts < b for a, b in spans) else blank())
        vw.release()

        events, clears = [], []
        w = ChannelWorker(0, "F4", video, None, {}, MarkerDetector(ids=[0, 1, 2, 3]), 100,
                          events.append, lambda: 1, qr_cfg={"enabled": False, "pilot_timeout_sec": 2},
                          on_clear=lambda ch, band, name: clears.append((band, name)))
        w.set_measuring(True)
        w.start()
        time.sleep(7.5)
        w.stop()

        laps = [(e.pilot_name, e.lap) for e in events]
        print("idle laps:", laps)
        assert [n for n, _ in laps] == ["F4"] * 3, laps
        assert laps[0][1] == 999 and abs(laps[1][1] - 1.5) < 0.1 and laps[2][1] == 999, laps
        assert clears == [("F4", "F4")], clears     # バンド名の ch でもクリアを1回
        assert [e.lap_count for e in events] == [0, 1, 0], events     # 周回数は起点で 0 に戻る
        assert abs(events[1].total - 1.5) < 0.1, events


def test_firebase_offline():
    """オフラインでは1回だけ試して捨て（再送しない）、OFFLINE にする。つながれば OK に戻る。"""
    fake = install_fake_firebase()
    import importlib
    import app.firebase_sync as fs
    importlib.reload(fs)
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(os.path.join(d, "config.yaml")).load()
        open(os.path.join(d, "serviceAccount.json"), "w").write("{}")
        cfg.firebase["root"] = "offroot"
        cfg.firebase["database_url"] = "https://example.firebaseio.com/"
        fb = fs.FirebaseSync(cfg)
        fake.offline = True                 # 起動時からオフライン
        assert fb.connect()
        fb.ensure_day_node()
        fb.push_lap("CHO!", 12.3, time.time())
        time.sleep(0.5)
        assert fb.status == "OFFLINE" and fb.pending == 0
        calls = fake.calls
        time.sleep(1.5)
        assert fake.calls == calls          # 再送しない
        laps_path = f"offroot/laps_{fs.today_str()}"
        assert fake._get(laps_path) is None

        fake.offline = False                # つながった
        fb.push_lap("CHO!", 11.1, time.time())
        time.sleep(0.5)
        assert fb.status == "OK"
        node = fake._get(laps_path)
        assert node["!trackSetting"]["minLap"] == cfg.track_setting["minLap"]   # 日付ノードを作り直した
        sent = [v["lapTime"] for k, v in node.items() if not k.startswith("!")]
        assert sent == [11.1], sent         # オフライン中のラップは送られない


def test_firebase_local_mode():
    """firebase.yaml（URL）や鍵が無ければ、最初からオフラインで動く（送らない・灰色の OFFLINE）。"""
    install_fake_firebase()
    import importlib
    import app.firebase_sync as fs
    importlib.reload(fs)
    logged = []

    class Capture(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    capture = Capture()
    fs.log.addHandler(capture)
    fs.log.setLevel(logging.INFO)
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(os.path.join(d, "config.yaml")).load()     # firebase.yaml が無い → URL が空
        fb = fs.FirebaseSync(cfg)
        assert not fb.connect()
        assert fb.status == "OFFLINE" and fb.local and not fb.enabled
        assert not logged, logged                                # 隠し機能なので、設定していなければログに出さない
        fb.ensure_day_node()
        fb.push_lap("CHO!", 12.3, time.time())                   # 何もしない（落ちない）
        assert fb.pending == 0

        cfg.firebase["database_url"] = "https://example.firebaseio.com/"   # URL はあるが鍵が無い
        fb = fs.FirebaseSync(cfg)
        assert not fb.connect() and fb.status == "OFFLINE" and fb.local
        assert not logged, logged

        with open(cfg.firebase_path, "w", encoding="utf-8") as f:   # firebase.yaml を置いたのに鍵が無ければ、原因をログに出す
            f.write("database_url: https://example.firebaseio.com/\n")
        fb = fs.FirebaseSync(cfg)
        assert not fb.connect() and fb.local
        assert len(logged) == 1 and "Key file not found" in logged[0], logged
    fs.log.removeHandler(capture)


def test_firebase_not_built():
    """Firebase を入れていない exe（公開用の Firebase なし）は、firebase.yaml と鍵があってもつながず、読まず・書かない。"""
    install_fake_firebase()
    import importlib
    import app.config as cf
    import app.firebase_sync as fs
    importlib.reload(fs)
    old = cf.FIREBASE_BUILT
    cf.FIREBASE_BUILT = False
    try:
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "firebase.yaml")
            with open(fp, "w", encoding="utf-8") as f:
                f.write("database_url: https://example.firebaseio.com/\nroot: testroot\ncredentials: key.json\n")
            with open(os.path.join(d, "key.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            cfg = Config(os.path.join(d, "config.yaml")).load()
            assert cfg.firebase["database_url"] == "" and cfg.firebase["root"] == "practice"   # 読まない
            assert not cfg.has_firebase_file()                  # 設定画面にもルート・鍵を出さない
            cfg.firebase["database_url"] = "https://example.firebaseio.com/"
            fb = fs.FirebaseSync(cfg)
            assert not fb.connect() and fb.local and not fb.enabled
            cfg.save()
            with open(fp, encoding="utf-8") as f:
                assert "testroot" in f.read()                   # 書き戻さない（置いたまま）
    finally:
        cf.FIREBASE_BUILT = old


def test_local_sync():
    """Firebase なしの exe で FirebaseSync の代わりに使う LocalSync は、main.py が呼ぶものを全部持ち、何も送らない。"""
    import re
    from app.localsync import LocalSync
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        used = set(re.findall(r"self\.fb\.([a-z_]+)", f.read()))
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(os.path.join(d, "config.yaml")).load()
        fb = LocalSync(cfg)
        missing = [n for n in used if not hasattr(fb, n)]
        assert not missing, missing
        assert not fb.connect() and fb.local and not fb.enabled and fb.pending == 0
        fb.ensure_day_node()
        fb.push_lap("CHO!", 12.3, time.time())
        assert fb.min_lap() == float(cfg.track_setting["minLap"])
        fb.update_track_setting({"minLap": 7})
        assert fb.min_lap() == 7.0 and fb.track_setting()["minLap"] == 7


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
