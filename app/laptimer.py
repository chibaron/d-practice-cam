"""マーカーの検出（ArUco。FGM は fpv-gate-marker の fgm）と、ゲート通過（ラップ）の判定。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np
from fgm import FGM_TOL, FgmDetector   # FpvGateMarker（別プロジェクト fpv-gate-marker）

MAX_LAP = 999.0  # lap2firebase と同じく 999 秒以上は 999。最初の通過（起点）も 999


ARUCO_IDS = (0, 1, 2, 3)   # 使う ArUco の ID（固定。TinyViewPlus のマーカーと同じ。設定はしない）


@dataclass
class Detection:
    visible: bool                 # 有効なマーカーが見えているか
    corners: list = field(default_factory=list)   # 描画用（有効なマーカーの四隅）
    ids: list = field(default_factory=list)


class MarkerDetector:
    def __init__(self, dictionary: str = "DICT_4X4_50", ids=ARUCO_IDS,
                 min_size_pct: float = 5.0, min_markers: int = 1):
        dict_id = getattr(cv2.aruco, dictionary)
        self.detector = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(dict_id), cv2.aruco.DetectorParameters())
        self.ids = set(int(i) for i in ids)
        self.min_size_pct = float(min_size_pct)
        self.min_markers = max(1, int(min_markers))

    def detect(self, frame: np.ndarray) -> Detection:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        corners, ids, _ = self.detector.detectMarkers(gray)
        if ids is None:
            return Detection(False)
        min_side = gray.shape[0] * self.min_size_pct / 100.0
        ok_corners, ok_ids = [], []
        for c, i in zip(corners, ids.flatten()):
            if int(i) not in self.ids:
                continue
            pts = c.reshape(4, 2)
            side = np.mean([np.linalg.norm(pts[k] - pts[(k + 1) % 4]) for k in range(4)])
            if side < min_side:
                continue
            ok_corners.append(pts)
            ok_ids.append(int(i))
        return Detection(len(ok_ids) >= self.min_markers, ok_corners, ok_ids)


def make_detector(cfg: dict):
    """設定の `aruco.marker_type` で検出器を選ぶ（aruco / fgm）。"""
    kind = cfg.get("marker_type", "fgm")
    if kind == "fgm":
        return FgmDetector(cfg.get("fgm_line_step", 4), cfg.get("fgm_min_lines", 3),
                           cfg.get("min_size_pct", 5), cfg.get("fgm_tol", FGM_TOL))
    return MarkerDetector(cfg.get("dictionary", "DICT_4X4_50"), ARUCO_IDS,
                          cfg.get("min_size_pct", 5), cfg.get("min_markers", 1))


class LapTimer:
    """1ch分のゲート通過判定。

    IDLE → (appear 秒続けて見えた) → SEEN → (見えなくなった) → LOST
    LOST で flicker 秒以内に再び appear 秒続けて見えたら SEEN に戻る。経過したら通過確定。
    appear 秒に満たない検出は誤検出として無視する（LOST の確定は、見えている間だけ待つ）。
    通過時刻は「最後にマーカーが見えたフレームの時刻」。
    """

    IDLE, SEEN, LOST = "IDLE", "SEEN", "LOST"

    def __init__(self, flicker_ms: float = 200, appear_ms: float = 0):
        self.flicker = flicker_ms / 1000.0
        self.appear = appear_ms / 1000.0
        self.reset()

    def reset(self) -> None:
        self.state = self.IDLE
        self.t_last = None        # 最後にマーカーが見えた時刻
        self.t_lost = None        # 見えなくなった時刻
        self.t_appear = None      # SEEN になる前に、続けて見え始めた時刻
        self.last_pass = None     # 前回の通過時刻（None なら次の通過が起点）
        self.start_pass = None    # 起点の通過時刻（トータルのアナウンス用）
        self.lap_count = 0        # 起点からの周回数（アナウンス用）

    def update(self, t: float, visible: bool) -> float | None:
        """フレームごとに呼ぶ。通過が確定したら t_last を返す。"""
        if visible:
            if self.state != self.SEEN:
                if self.t_appear is None:
                    self.t_appear = t
                if t - self.t_appear < self.appear:     # まだ誤検出かもしれない
                    return None
                self.state = self.SEEN
                self.t_appear = None
                self.t_lost = None
            self.t_last = t
            return None
        self.t_appear = None
        if self.state == self.SEEN:
            self.state = self.LOST
            self.t_lost = t
            return None
        if self.state == self.LOST and t - self.t_lost >= self.flicker:
            self.state = self.IDLE
            return self.t_last
        return None

    def check_timeout(self, t: float) -> float | None:
        """映像が途切れたときなど、フレームが来なくても確定させるために呼ぶ。"""
        self.t_appear = None
        if self.state == self.LOST and t - self.t_lost >= self.flicker:
            self.state = self.IDLE
            return self.t_last
        return None

    def on_pass(self, t_pass: float, min_lap: float) -> float | None:
        """通過を受けてラップ（秒）を返す。minLap 以下なら None（無視）。
        最初の通過は 999（起点）を返す。"""
        if self.last_pass is None:
            self.last_pass = self.start_pass = t_pass
            self.lap_count = 0
            return MAX_LAP
        lap = math.floor((t_pass - self.last_pass) * 100) / 100   # 10ms 未満は切り捨て
        if lap > MAX_LAP:
            lap = MAX_LAP
        if lap <= min_lap:
            return None
        self.last_pass = t_pass
        self.lap_count += 1
        return lap

    def total(self, t_pass: float) -> float:
        """起点からのトータル（秒、10ms 未満切り捨て）。"""
        if self.start_pass is None:
            return 0.0
        return math.floor((t_pass - self.start_pass) * 100) / 100
