"""当日の全ラップ（通過）をファイルに残す。ラップの一覧とランキングはここから作る。

ファイルは laps/laps_YYYY-MM-DD.jsonl（config.yaml と同じフォルダの laps/）。通過のたびに1行（JSON）書き足すので、
アプリが途中で落ちても、それまでの分は残る。起動し直すと当日のファイルを読んで続きから。前の日のファイルは消さない。
日付が変わったら新しいファイルに切り替える（一覧とランキングは空になる）。

1行の中身:
    通過: {"wall": 通過時刻(time.time()), "rx": 受信器(USBポート), "band": "F1", "pilot": "TARO",
           "lap": 秒(999 は起点), "count": 起点からの周回数, "total": 起点からのトータル(秒)}
    取り消し: {"void": true/false, "rx": 受信器, "wall": そのラップの通過時刻}
        ミス計測のラップを無効にする（false で戻す）。元の行は書き換えない。

無効にしたラップは一覧で灰色にし、ランキングから外す（ランキングは毎回全ラップから計算し直す。ranking.compute）。
Firebase と LapRF には送ったままで、取り消さない。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass

from .localsync import today_str
from . import ranking as rk

log = logging.getLogger(__name__)

LAPS_DIR = "laps"


@dataclass
class Lap:
    rx: str             # 受信器（USBポート）。セッションはこの中で起点から次の起点まで
    band: str
    pilot: str
    lap: float          # 秒。999 は起点
    count: int          # 起点からの周回数
    total: float        # 起点からのトータル（秒）
    wall: float         # 通過時刻（time.time()）
    void: bool = False  # ミス計測として無効にした

    @property
    def is_start(self) -> bool:
        return self.lap >= 999

    def row(self) -> tuple:
        """メイン画面の一覧の1行 (pilot, lap, lap_count, total, wall, void)。"""
        return self.pilot, self.lap, self.count, self.total, self.wall, self.void


class LapLog:
    def __init__(self, folder: str):
        self.folder = folder
        self.lock = threading.Lock()
        self.ver = 0                 # 変わったら増やす（ランキングの描き直しの判定用）
        self.date = today_str()
        self.laps: dict[str, list[Lap]] = {}     # 受信器 → 古い順のラップ（起点も入れる）
        self._rank = None            # (ver, 耐久の秒数, 計算したランキング)
        self._load()

    @property
    def path(self) -> str:
        return os.path.join(self.folder, f"laps_{self.date}.jsonl")

    # ---------- 記録 ----------
    def add(self, rx: str, band: str, pilot: str, lap: float, count: int, total: float, wall: float) -> bool:
        """ラップ確定（main.App.on_lap）ごとに呼ぶ。lap が 999 なら起点。
        そのパイロットの当日のベストラップを更新したら True（その日の最初のラップも更新。lap2firebase と同じ）。"""
        with self.lock:
            self._roll_date()
            new = Lap(rx, band, pilot, round(float(lap), 2), int(count), round(float(total), 2), float(wall))
            new_best = False
            if not new.is_start:
                prev = rk.compute(self.laps, 0)[0]
                b = dict(prev).get(pilot)
                new_best = b is None or new.lap < b
            self.laps.setdefault(rx, []).append(new)
            self._write({"wall": new.wall, "rx": rx, "band": band, "pilot": pilot, "lap": new.lap,
                         "count": new.count, "total": new.total})
            self.ver += 1
            return new_best

    def toggle_void(self, rx: str, wall: float):
        """そのラップの無効と有効を切り替える。切り替えたらラップ（Lap のコピー）、見つからない・起点なら None。"""
        with self.lock:
            lap = self._find(rx, wall)
            if lap is None or lap.is_start:
                return None
            lap.void = not lap.void
            self._write({"void": lap.void, "rx": rx, "wall": wall})
            self.ver += 1
            return Lap(**lap.__dict__)

    def check_date(self) -> None:
        """日付が変わっていたら新しいファイルに切り替える（画面の更新ごとに呼ぶ）。"""
        with self.lock:
            self._roll_date()

    # ---------- 読み出し ----------
    def count(self, rx: str) -> int:
        with self.lock:
            return len(self.laps.get(rx, ()))

    def newest(self, rx: str, start: int, n: int) -> list:
        """新しい方から start 行目から n 行の (pilot, lap, lap_count, total, wall, void)。"""
        with self.lock:
            laps = self.laps.get(rx, [])
            end = len(laps) - start
            return [laps[i].row() for i in range(end - 1, max(0, end - n) - 1, -1)]

    def ranking(self, endurance_sec: float = rk.ENDURANCE_SEC):
        """(ベストラップの順位, 3周連続の順位, 耐久の順位, ベストラップの通過時刻, 印)。ranking.compute を参照。"""
        with self.lock:
            self._roll_date()
            if not self._rank or self._rank[:2] != (self.ver, endurance_sec):
                self._rank = (self.ver, endurance_sec, rk.compute(self.laps, endurance_sec))
            return self._rank[2]

    def best_walls(self) -> dict:
        """パイロット名 → BEST LAP のラップの通過時刻。メイン画面でそのラップを緑にするため。"""
        return self.ranking(self._rank[1] if self._rank else rk.ENDURANCE_SEC)[3]

    # ---------- 内部 ----------
    def _find(self, rx: str, wall: float):
        for lap in reversed(self.laps.get(rx, [])):
            if lap.wall == wall:
                return lap
        return None

    def _roll_date(self) -> None:
        today = today_str()
        if today != self.date:
            log.info("Date changed, starting a new lap file (%s -> %s)", self.date, today)
            self.date = today
            self.laps = {}
            self.ver += 1

    def _write(self, rec: dict) -> None:
        try:
            os.makedirs(self.folder, exist_ok=True)
            line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
            with open(self.path, "ab+") as f:
                f.seek(0, os.SEEK_END)
                if f.tell():                # 落ちたときの書きかけの行に続けて書かないよう、改行で終わっていなければ改行
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        line = b"\n" + line
                f.write(line)
        except OSError as e:
            log.warning("Cannot write the lap file: %s", e)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        bad = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            log.warning("Cannot read the lap file (starting fresh): %s", e)
            return
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if "lap" in rec:
                    self.laps.setdefault(str(rec["rx"]), []).append(Lap(
                        str(rec["rx"]), str(rec.get("band", "")), str(rec.get("pilot", "")), float(rec["lap"]),
                        int(rec.get("count", 0)), float(rec.get("total", 0.0)), float(rec["wall"])))
                elif "void" in rec:
                    lap = self._find(str(rec["rx"]), float(rec["wall"]))
                    if lap is not None:
                        lap.void = bool(rec["void"])
            except (ValueError, KeyError, TypeError):
                bad += 1            # 落ちたときの書きかけの行など
        if bad:
            log.warning("Skipped %d broken line(s) in the lap file %s", bad, self.path)
        n = sum(len(v) for v in self.laps.values())
        log.info("Loaded %d pass(es) of today from %s", n, self.path)
