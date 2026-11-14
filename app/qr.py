"""QRコードからパイロット名を読む（ドローン起動時に映像へ表示されるQR）。"""
from __future__ import annotations

import logging

import cv2
import numpy as np

log = logging.getLogger(__name__)

MAX_NAME_LEN = 32


def clean_name(text: str) -> str:
    """QRの文字列をパイロット名として使える形にする（制御文字を除き、前後の空白を取り、長さを制限）。"""
    s = "".join(ch for ch in (text or "") if ch.isprintable()).strip()
    return s[:MAX_NAME_LEN]


class QRReader:
    def __init__(self):
        self.detector = cv2.QRCodeDetector()

    def read(self, frame: np.ndarray) -> tuple[str, np.ndarray | None]:
        """パイロット名と QR の四隅を返す。読めなければ ("", None)。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        try:
            text, pts, _ = self.detector.detectAndDecode(gray)
        except cv2.error as e:
            log.debug("QR read failed: %s", e)
            return "", None
        name = clean_name(text)
        if not name or pts is None:
            return "", None
        return name, pts.reshape(4, 2)
