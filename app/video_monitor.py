"""映像が無いこと（砂嵐、受信器が出す単色の画面）を見分ける。

アナログ受信器は電波が無くてもフレームを出し続けるので、フレームが来ているだけでは分からない。
マーカーの処理を優先するため、映像がある間は1秒ごと、映像が無い間（マーカーを探さない）は0.1秒ごとに調べる。
実機の受信器で測った値（4画素おきに間引いた映像、続いた2フレームの比較）:
    砂嵐 (USB2.0 PC CAMERA)  行間の差 95〜107  フレーム間の差 97〜105
    青画面 (USB Video)       輝度の標準偏差 2〜7  彩度 255
    内蔵カメラ               行間の差 3〜6  フレーム間の差 0.3〜9
    細かい模様が大きく動く合成画像  行間の差 25  フレーム間の差 25
"""
from __future__ import annotations

import cv2
import numpy as np

CHECK_SEC = 1.0             # 映像がある間に調べる間隔
LOST_CHECK_SEC = 0.1        # 映像が無い間に調べる間隔
SAMPLE_STEP = 4             # 何画素おきに見るか（1回 1ms 程度で済ませる）
SNOW_ROW_DIFF = 40.0        # 砂嵐: 上下の行の差とフレーム間の差が両方これ以上（カラーのノイズは輝度にすると約47）
SNOW_FRAME_DIFF = 40.0
FLAT_STD = 15.0             # 単色の画面: 輝度のばらつきがこれ未満で、
FLAT_SATURATION = 200.0     # 彩度がこれ以上（青画面）か、
FLAT_BLACK = 16.0           # 輝度がこれ未満（黒画面）のものだけを映像無しとする（白っぽい空などを間違えない）


def classify(frame: np.ndarray, prev_frame: np.ndarray | None) -> str:
    """フレームと1つ前のフレームを比べて "" / "snow" / "flat" を返す。"""
    sample = frame[::SAMPLE_STEP, ::SAMPLE_STEP]
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY).astype(np.int16)
    if float(gray.std()) < FLAT_STD:
        if float(gray.mean()) < FLAT_BLACK:
            return "flat"
        if float(cv2.cvtColor(sample, cv2.COLOR_BGR2HSV)[:, :, 1].mean()) >= FLAT_SATURATION:
            return "flat"
    if prev_frame is None or prev_frame.shape != frame.shape:
        return ""
    prev = cv2.cvtColor(prev_frame[::SAMPLE_STEP, ::SAMPLE_STEP], cv2.COLOR_BGR2GRAY).astype(np.int16)
    if float(np.abs(gray - prev).mean()) >= SNOW_FRAME_DIFF and float(np.abs(np.diff(gray, axis=0)).mean()) >= SNOW_ROW_DIFF:
        return "snow"
    return ""


class VideoMonitor:
    """1ch 分の映像の状態。映像が無くなってからの時間を数える。"""

    def __init__(self, clear_ms: float = 3000, check_sec: float = CHECK_SEC, lost_check_sec: float = LOST_CHECK_SEC):
        self.clear_sec = float(clear_ms or 0) / 1000.0     # 0 なら映像が無くてもクリアしない
        self.check_sec, self.lost_check_sec = check_sec, lost_check_sec
        self.lost_since = None      # 映像が無くなった時刻（映像があれば None）
        self.kind = ""              # "" / "snow" / "flat" / "none"（フレームが来ない）
        self._frame = None          # いまのフレームと1つ前のフレーム（参照だけ持つ。コピーしない）
        self._prev = None
        self._next_check = 0.0

    def update(self, frame: np.ndarray, t: float) -> str:
        """フレームごとに呼ぶ。映像が無ければ "snow" か "flat"、あれば ""。次に調べる時刻までは前の結果を返す。"""
        self._prev, self._frame = self._frame, frame
        if t < self._next_check:
            return self.kind
        self.kind = classify(frame, self._prev)
        if not self.kind:
            self.lost_since = None
        elif self.lost_since is None:
            self.lost_since = t
        self._next_check = t + (self.lost_check_sec if self.kind else self.check_sec)
        return self.kind

    def mark_lost(self, t: float) -> None:
        """フレームが読めなかったときに呼ぶ。次のフレームですぐ調べる。"""
        self.kind = "none"
        self._frame = self._prev = None
        self._next_check = 0.0
        if self.lost_since is None:
            self.lost_since = t

    def should_clear(self, t: float) -> bool:
        return self.clear_sec > 0 and self.lost_since is not None and t - self.lost_since >= self.clear_sec
