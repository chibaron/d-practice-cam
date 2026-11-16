"""Firebase を入れていない exe（公開用の Firebase なし）で、FirebaseSync の代わりに使う。

何も送らず、コース設定（track_setting）を持つだけ。Firebase の送信のコード（app/firebase_sync.py）と
firebase-admin は、その exe には入れない（tools/build_exe.py）。main.py が app.config.FIREBASE_BUILT で選ぶ。
"""
from __future__ import annotations

import copy
import datetime
import threading


def today_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


class LocalSync:
    """FirebaseSync と同じ呼び方で、常にスタンドアローン（local）。"""

    def __init__(self, config):
        self.config = config
        self.enabled = False
        self.local = True
        self.status = "OFFLINE"
        self.message = ""
        self.laps_path = None
        self.pending = 0
        self._track_setting = copy.deepcopy(config.track_setting)
        self._ts_lock = threading.Lock()

    def connect(self) -> bool:
        return False

    def ensure_day_node(self) -> None:
        pass

    def push_lap(self, pilot_name: str, lap: float, wall: float) -> None:
        pass

    def update_track_setting(self, changes: dict) -> None:
        with self._ts_lock:
            self._track_setting.update(changes)

    def track_setting(self) -> dict:
        with self._ts_lock:
            return copy.deepcopy(self._track_setting)

    def min_lap(self) -> float:
        with self._ts_lock:
            v = self._track_setting.get("minLap", self.config.track_setting.get("minLap", 10))
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(self.config.track_setting.get("minLap", 10))
