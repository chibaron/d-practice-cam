"""chごとの映像取り込みとラップ判定（1chにつき1スレッド）。"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

import cv2

from .laptimer import Detection, LapTimer, MarkerDetector
from .qr import QRReader
from .video_monitor import VideoMonitor

log = logging.getLogger(__name__)

NO_SIGNAL_SEC = 1.5     # これ以上フレームが来なければ NO SIGNAL
REOPEN_SEC = 2.0        # 再接続を試みる間隔
QR_SHOW_SEC = 0.5       # QR の枠を画面に残す時間
QR_NEW_SEC = 3.0        # QR がこれ以上見えていなかった後に読んだら、新しい QR（機体を起動し直した）とみなす


@dataclass
class PassEvent:
    ch: int             # 0始まり
    band: str
    t_pass: float       # time.monotonic() 基準の通過時刻
    wall: float         # 通過時刻（time.time() 基準）
    lap: float          # ラップ（秒）。999 は起点
    pilot_name: str = ""    # Firebase の pilotName。QR で読んだパイロット名、無ければバンド名
    lap_count: int = 0      # 起点からの周回数（アナウンス用）
    total: float = 0.0      # 起点からのトータル（秒、アナウンス用）


class ChannelWorker:
    def __init__(self, ch: int, band: str, source, backend: int | None,
                 capture_cfg: dict, detector: MarkerDetector, flicker_ms: float,
                 on_lap: Callable[[PassEvent], None], min_lap_fn: Callable[[], float],
                 name: str = "", path: str = "",
                 on_ignored: Callable[[int, str], None] | None = None,
                 qr_cfg: dict | None = None,
                 on_pilot: Callable[[int, str, str], None] | None = None,
                 on_clear: Callable[[int, str, str], None] | None = None,
                 appear_ms: float = 0, no_video_cfg: dict | None = None):
        self.ch = ch
        self.band = band
        self.source = source            # デバイス番号、または動画ファイルのパス（テスト用）
        self.backend = backend
        self.capture_cfg = capture_cfg
        self.detector = detector
        self.timer = LapTimer(flicker_ms, appear_ms)
        # 砂嵐などの間はマーカーを探さず、続いたらクリアする。設定が無ければ（テスト用）見ない
        self.video = VideoMonitor(no_video_cfg.get("clear_ms", 3000)) \
            if no_video_cfg and no_video_cfg.get("enabled", True) else None
        self.on_lap = on_lap
        self.min_lap_fn = min_lap_fn
        self.on_ignored = on_ignored    # minLap 以下で無視した通過を (ch, バンド, ラップ秒) で知らせる（音を鳴らすため）
        self.on_pilot = on_pilot        # QR で新しいパイロット名を読んだら知らせる（読み上げのため）
        self.on_clear = on_clear        # 通過が無く起点をリセットしたら知らせる（クリアのアナウンスのため）
        self._pending_clear = None      # on_clear に渡す名前（lock の外で知らせる）
        self.name = name
        self.path = path                # 受信器のデバイスパス（バンドを受信器ごとに覚えるため）

        self.measuring = False          # 計測中なら通過をラップにして on_lap に渡す
        self.lock = threading.Lock()
        self.frame = None
        self.detection = Detection(False)
        self.last_frame_time = 0.0
        self.fps = 0.0

        # QR で読んだパイロット名。ゲート通過か QR の読み取りが pilot_timeout 秒無ければ、
        # 計測の起点をリセットし（次の通過がスタート）、パイロット名もバンド名に戻す
        qr_cfg = qr_cfg or {}
        self.qr = QRReader() if qr_cfg.get("enabled", True) else None
        self.qr_interval = max(0.02, float(qr_cfg.get("interval_ms", 500)) / 1000.0)
        self.pilot_timeout = float(qr_cfg.get("pilot_timeout_sec", 60) or 0)
        self.pilot = None
        self.active_t = 0.0             # 最後にゲート通過か QR の読み取りがあった時刻
        self.qr_pts = None              # 画面表示用の QR の四隅
        self.qr_t = 0.0
        self._seq = 0                   # フレーム番号（QR スレッドが同じフレームを読み直さないため）

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"ch{ch + 1}", daemon=True)
        self._qr_thread = threading.Thread(target=self._qr_run, name=f"ch{ch + 1}-qr", daemon=True) \
            if self.qr else None

    # ---- 外部から呼ぶ ----
    def start(self):
        self._thread.start()
        if self._qr_thread:
            self._qr_thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)
        if self._qr_thread:
            self._qr_thread.join(timeout=3)

    def set_measuring(self, on: bool):
        with self.lock:
            self.measuring = on
            self.timer.reset()

    def snapshot(self):
        with self.lock:
            frame = None if self.frame is None else self.frame.copy()
            qr_pts = self.qr_pts if time.monotonic() - self.qr_t < QR_SHOW_SEC else None
            # 4つ目は最後に数えた通過（起点を含む）の時刻。画面のタイマーはここから数える（無ければ None）
            return frame, self.detection, self.last_frame_time, self.timer.last_pass, self.fps, \
                self.pilot, qr_pts

    def pilot_state(self):
        """受信器を検索し直したときに引き継ぐため、パイロット名と最終時刻を返す。"""
        with self.lock:
            return self.pilot, self.active_t

    def restore_pilot(self, pilot, active_t: float):
        with self.lock:
            self.pilot, self.active_t = pilot, active_t

    @property
    def no_signal(self) -> bool:
        return time.monotonic() - self.last_frame_time > NO_SIGNAL_SEC

    # ---- スレッド本体 ----
    def _open(self):
        if isinstance(self.source, str):
            cap = cv2.VideoCapture(self.source)
        else:
            cap = cv2.VideoCapture(self.source, self.backend) if self.backend is not None \
                else cv2.VideoCapture(self.source)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.capture_cfg.get("width", 640))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.capture_cfg.get("height", 480))
            cap.set(cv2.CAP_PROP_FPS, self.capture_cfg.get("fps", 30))
        return cap

    def _run(self):
        is_file = isinstance(self.source, str)
        cap = None
        fails = 0
        fps_count, fps_t0 = 0, time.monotonic()
        file_interval, file_next = 0.0, 0.0
        while not self._stop.is_set():
            if cap is None or not cap.isOpened():
                cap = self._open()
                if not cap.isOpened():
                    log.warning("ch%d: cannot open receiver. Retrying in %.0f s", self.ch + 1, REOPEN_SEC)
                    self._handle_timeout()
                    self._stop.wait(REOPEN_SEC)
                    continue
                fails = 0
                if is_file:
                    file_fps = cap.get(cv2.CAP_PROP_FPS) or 30
                    file_interval = 1.0 / file_fps
                    file_next = time.monotonic()

            ok, frame = cap.read()
            t = time.monotonic()
            if not ok or frame is None:
                if is_file:     # 動画ファイルは最後まで再生したら止める
                    self._handle_timeout()
                    self._stop.wait(0.2)
                    continue
                fails += 1
                self._handle_timeout()
                if fails > 30:
                    log.warning("ch%d: video lost. Reconnecting", self.ch + 1)
                    cap.release()
                    cap = None
                    self._stop.wait(REOPEN_SEC)
                else:
                    self._stop.wait(0.01)
                continue
            fails = 0

            no_video = bool(self.video and self.video.update(frame, t))
            det = Detection(False) if no_video else self.detector.detect(frame)
            with self.lock:
                measuring = self.measuring
                t_pass = self.timer.update(t, det.visible)
                self._check_idle(t, t_pass is not None)
                if t_pass is None:
                    self._check_video_lost(t)
                self.frame = frame
                self._seq += 1
                self.detection = det
                self.last_frame_time = t
            self._notify_clear()
            if t_pass is not None and measuring:
                self._emit(t_pass, t)

            fps_count += 1
            if t - fps_t0 >= 1.0:
                self.fps = fps_count / (t - fps_t0)
                fps_count, fps_t0 = 0, t
            if is_file:     # 動画ファイルは実時間で再生する
                # 待ちすぎた分は次のフレームで詰める（Windows のタイマーは待ちが長めになるため）
                file_next += file_interval
                wait = file_next - time.monotonic()
                if wait > 0:
                    self._stop.wait(wait)
        if cap is not None:
            cap.release()

    def _handle_timeout(self):
        t = time.monotonic()
        with self.lock:
            measuring = self.measuring
            t_pass = self.timer.check_timeout(t)
            self._check_idle(t, t_pass is not None)
            if self.video and not isinstance(self.source, str):     # 動画ファイルは最後まで再生しただけ
                self.video.mark_lost(t)
                if t_pass is None:
                    self._check_video_lost(t)
        self._notify_clear()
        if t_pass is not None and measuring:
            self._emit(t_pass, t)

    def _check_idle(self, t: float, passed: bool):
        """lock を取った状態で呼ぶ。ゲート通過が pilot_timeout 秒無ければ、計測の起点をリセットし
        （次の通過をスタートにする）、パイロット名もバンド名に戻す。バンド名の ch も同じ。
        ゲート通過（minLap 以下を含む）なら期限を延ばす。"""
        active = self.pilot is not None or self.timer.last_pass is not None
        if active and self.pilot_timeout > 0 and t - self.active_t > self.pilot_timeout:
            if self.timer.last_pass is not None:
                log.info("ch%d %s: no gate pass for %.0f s. The next pass will be the start",
                         self.ch + 1, self.pilot or self.band, self.pilot_timeout)
            self._clear()
        if passed:
            self.active_t = t

    def _clear(self):
        """lock を取った状態で呼ぶ。計測の起点とパイロット名を消して、クリアをアナウンスする。"""
        if self.pilot is not None:
            log.info("ch%d: pilot name %s reverted to band %s", self.ch + 1, self.pilot, self.band)
        self._pending_clear = self.pilot or self.band
        self.timer.reset()
        self.pilot = None

    def _check_video_lost(self, t: float):
        """lock を取った状態で呼ぶ。映像が無い状態が clear_ms 続いたらクリアする（周波数を解放する）。"""
        if self.video and self.video.should_clear(t) and (self.pilot is not None or self.timer.last_pass is not None):
            log.info("ch%d %s: video lost (%s). Clearing the channel", self.ch + 1, self.pilot or self.band,
                     self.video.kind)
            self._clear()

    def _notify_clear(self):
        """_check_idle で起点をリセットしていたら on_clear で知らせる（lock の外で呼ぶ）。"""
        with self.lock:
            name, self._pending_clear = self._pending_clear, None
        if name and self.on_clear:
            self.on_clear(self.ch, self.band, name)

    def _qr_run(self):
        """QR を探すスレッド。最新のフレームだけを読むので、遅れても取り込み・ArUco は待たない。"""
        seen = -1
        while not self._stop.wait(self.qr_interval):
            with self.lock:
                self._check_idle(time.monotonic(), False)
                frame, seq = self.frame, self._seq
            self._notify_clear()
            if frame is None or seq == seen or (self.video and self.video.kind):     # 砂嵐の間は読まない
                continue
            seen = seq
            name, pts = self.qr.read(frame)
            if not name:
                continue
            t = time.monotonic()
            with self.lock:
                changed = name != self.pilot
                # QR がしばらく見えていなかった = 機体を起動し直した。同じ名前でも、60秒以内でも新しいエントリー
                reappeared = t - self.qr_t >= QR_NEW_SEC
                if changed or reappeared:
                    log.info("ch%d %s: read pilot name %s from QR. The next pass will be the start",
                             self.ch + 1, self.band, name)
                    self.timer.reset()
                self.pilot, self.active_t = name, t
                self.qr_pts, self.qr_t = pts, t
            if (changed or reappeared) and self.on_pilot:   # 表示されている QR を読み直しただけなら知らせない
                self.on_pilot(self.ch, self.band, name)

    def _emit(self, t_pass: float, t_now: float):
        min_lap = float(self.min_lap_fn())
        with self.lock:
            if not self.measuring:
                return
            last = self.timer.last_pass
            lap = self.timer.on_pass(t_pass, min_lap)
            pilot_name = self.pilot or self.band
            lap_count, total = self.timer.lap_count, self.timer.total(t_pass)
        if lap is None:
            short = math.floor((t_pass - last) * 100) / 100      # 無視したラップ（前に数えた通過から）
            log.info("ch%d %s: ignored a pass at or below minLap (%.2fs <= %.2fs)",
                     self.ch + 1, self.band, short, min_lap)
            if self.on_ignored:
                self.on_ignored(self.ch, self.band, short)
            return
        wall = time.time() - (t_now - t_pass)
        self.on_lap(PassEvent(self.ch, self.band, t_pass, wall, lap, pilot_name, lap_count, total))
