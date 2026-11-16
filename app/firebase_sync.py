"""Firebase Realtime Database への送信（lap2firebase と同じ形式）。

書き込み先: {root}/laps_YYYY-MM-DD/{pushId} = {time: "HH:MM:SS", pilotName: バンド名, lapTime: 秒}
コース設定: {root}/laps_YYYY-MM-DD/!trackSetting

オフラインでも計測は止めない。送信は1回だけ試し（timeout_sec 秒であきらめる）、失敗したら
そのラップは送らずにログに残して捨てる（再送しない）。画面は OFFLINE。次の送信が通れば OK に戻る。
Firebase は隠し機能。通常はスタンドアローンで動かし、firebase.yaml（データベースURL）や鍵が無いときは
最初からオフライン（local）で動く。local のときは画面にも設定画面にも Firebase を出さない。
"""
from __future__ import annotations

import copy
import datetime
import logging
import os
import queue
import threading

from . import config as _config
from .localsync import today_str

log = logging.getLogger(__name__)


class FirebaseSync:
    def __init__(self, config):
        self.config = config
        self.enabled = False
        self.local = False              # Firebase の設定が無く、最初からオフラインで動いている
        self.status = "OFF"             # 画面表示用（英字）
        self.message = ""               # 詳細（ログ用）
        self._db = None
        self._q: queue.Queue = queue.Queue()
        self._track_setting = copy.deepcopy(config.track_setting)
        self._ts_lock = threading.Lock()
        self._listener = None
        self._ensured = None            # 日付ノードを確認・購読できたパス（オフラインになったら None）
        self.laps_path = None           # いま書き込んでいる日付ノード
        self._thread = threading.Thread(target=self._worker, name="firebase", daemon=True)

    # ---------------- 接続 ----------------
    def connect(self) -> bool:
        fb = self.config.firebase
        cred_path = self.config.resolve_path(fb.get("credentials", ""))
        if not _config.FIREBASE_BUILT:      # Firebase を入れていない exe（公開用の Firebase なし）
            return self._offline("Firebase is not included in this build")
        try:
            import firebase_admin
            from firebase_admin import credentials, db
        except ImportError:
            return self._offline("firebase-admin is not installed")
        if not fb.get("database_url"):
            return self._offline(f"No database URL ({self.config.firebase_path} is missing or database_url is empty)")
        if not os.path.exists(cred_path):
            return self._offline(f"Key file not found: {cred_path}")
        try:
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred, {
                "databaseURL": fb["database_url"],
                "databaseAuthVariableOverride": {"uid": fb.get("auth_uid", "python-script")},
                # 既定は120秒。オフラインのときに早くあきらめる
                "httpTimeout": float(fb.get("timeout_sec", 10) or 10),
            })
        except Exception as e:     # 鍵の形式が違うなど
            return self._disable("KEY ERR", f"Cannot connect to Firebase: {e}")
        self._db = db
        self.enabled = True
        self.status = "OK"
        self._thread.start()
        log.info("Firebase: connected to %s", fb["database_url"])
        return True

    def _offline(self, message: str) -> bool:
        """設定や鍵が無いとき。計測・音・画面はそのままで、Firebase には何も送らない。"""
        self.local = True
        self.enabled = False
        self.status = "OFFLINE"
        self.message = message
        if self.config.has_firebase_file():   # 隠し機能なので、設定していない人のログには出さない
            log.warning("Firebase is not available: %s", message)
        return False

    def _disable(self, status: str, message: str) -> bool:
        self.enabled = False
        self.status = status
        self.message = message
        log.warning("Stopping Firebase sending: %s", message)
        return False

    # ---------------- 外から呼ぶ ----------------
    def ensure_day_node(self) -> None:
        """今日の日付ノードと !trackSetting が無ければ作る（送信スレッドで実行）。"""
        root = self.config.firebase["root"]
        self.laps_path = f"{root}/laps_{today_str()}"
        if self.enabled:
            self._q.put(("ensure", self.laps_path, None))

    def push_lap(self, pilot_name: str, lap: float, wall: float) -> None:
        """pilot_name は QR で読んだパイロット名、無ければバンド名。"""
        if not self.enabled or self.laps_path is None:
            return
        data = {
            "time": datetime.datetime.fromtimestamp(wall).strftime("%H:%M:%S"),
            "pilotName": pilot_name,
            "lapTime": lap,
        }
        self._q.put(("push", self.laps_path, data))

    def update_track_setting(self, changes: dict) -> None:
        """設定画面で変えた項目だけを書き込む（他の項目は消さない）。"""
        with self._ts_lock:
            self._track_setting.update(changes)
        if self.enabled and self.laps_path:
            self._q.put(("update_ts", self.laps_path, dict(changes)))

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

    @property
    def pending(self) -> int:
        return self._q.qsize()

    # ---------------- 送信スレッド ----------------
    def _worker(self):
        while True:
            kind, path, data = self._q.get()
            try:
                if self._ensured != path:   # 日付ノードの確認がまだ（起動時・オフラインから戻ったとき）
                    self._ensure(path)
                if kind == "push":
                    self._db.reference(path).push(data)
                    log.info("Sent: %s %s", path, data)
                elif kind == "update_ts":
                    self._db.reference(f"{path}/!trackSetting").update(data)
                    log.info("!trackSetting updated: %s", data)
                if self.status != "OK":
                    log.info("Firebase connected")
                self.status = "OK"
            except Exception as e:   # オフラインなど。再送はしない
                if self.status != "OFFLINE":
                    log.warning("Cannot reach Firebase (offline): %s", e)
                self.status = "OFFLINE"
                self.message = str(e)
                self._ensured = None    # つながったら日付ノードの確認と購読をやり直す
                if kind == "push":
                    log.warning("Lap not sent (will not be resent): %s %s", path, data)
                elif kind == "update_ts":
                    log.warning("Cannot update !trackSetting: %s", data)

    def _ensure(self, laps_path: str):
        ts_ref = self._db.reference(f"{laps_path}/!trackSetting")
        current = ts_ref.get()
        if current is None:
            # 日付ノードごと新規作成（!trackSetting を書けばノードもできる）
            defaults = copy.deepcopy(self.config.track_setting)
            ts_ref.set(defaults)
            log.info("Created %s", laps_path)
        # 購読し直す（日付やルートが変わった場合、オフラインから戻った場合）
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass
            self._listener = None
        if self._listener is None:
            self._listen_path = laps_path
            self._listener = ts_ref.listen(self._on_track_setting)
        self._ensured = laps_path

    def _on_track_setting(self, event):
        with self._ts_lock:
            if event.path == "/":
                if isinstance(event.data, dict):
                    self._track_setting = dict(event.data)
            else:
                key = event.path.strip("/").split("/")[0]
                if event.data is None:
                    self._track_setting.pop(key, None)
                else:
                    self._track_setting[key] = event.data
        log.info("!trackSetting: %s", self._track_setting)
