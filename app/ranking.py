"""パイロットごとのランキング（ベストラップ、3周連続のベスト、90秒の周回数＋タイム）。別ウィンドウに表示する。

90秒の周回数は画面では MOST LAPS IN 90s（旧名 90秒耐久。英語の endurance は数時間のレースなので改名した）。
コードでは旧名の endurance のまま。

当日の全ラップ（laplog.LapLog。laps/laps_YYYY-MM-DD.jsonl）から毎回計算する。無効にしたラップは数えない。
Firebase には送らない。

3周連続: 同じセッション（ch の起点から次の起点まで）の続けて飛んだ3周の合計がいちばん短いもの。
起点で直近の周を捨てるので、セッションを跨いだ3周は数えない。

90秒耐久: ch の起点（最初の通過 999）から数え、トータルが耐久の秒数（!trackSetting の sessionTime1、既定90）を
過ぎた最初の通過までの周回数とトータル。90秒の時点で走っている周も、ゴールすれば1周に数える。
起点から耐久の秒数を過ぎる前に終わった（リセットされた）ものは記録しない。
1人が何回も飛んだときは、いちばん良い記録（ベストラップと3周連続は速い方、耐久は周回数の多い方、同じならタイムの短い方）。
"""
from __future__ import annotations

import collections

import cv2
import numpy as np

from .ui import ascii_text, theme


WIN_W, WIN_H = 460, 900     # 3つの表を縦に並べるので縦長
ROW_H = 30
TITLE_H = 36
HEAD_H = 30
FONT = cv2.FONT_HERSHEY_SIMPLEX
SCALE = 0.6
# 配色はメイン画面と同じ（ui.theme()。light / dark）
ENDURANCE_SEC = 90.0     # !trackSetting に sessionTime1 が無いとき
CONSECUTIVE_LAPS = 3     # 連続ラップの周回数


def compute(laps_by_rx: dict, endurance_sec: float = ENDURANCE_SEC):
    """当日の全ラップ（laplog.LapLog.laps: 受信器 → 古い順の Lap、起点も入る）からランキングを計算する。

    (ベストラップの順位, 3周連続の順位, 耐久の順位, ベストラップの通過時刻, 印) を返す。
    順位はどれも [(パイロット名, 値…)]、通過時刻は パイロット名 → そのラップの wall（メイン画面で緑にする）。
    印はメイン画面の一覧で記録のラップが分かるようにするもので、{"consecutive": 3周連続の記録の3周の wall の集合,
    "endurance": 耐久の記録になった通過（周回数とトータルがそのまま記録）の wall の集合}。
    無効にしたラップ（void）は数えない。3周連続はそこで途切れ、耐久はそのセッションを数えない。
    起点より前のラップ（日付を跨いだセッションなど）はベストラップだけに数える。
    """
    best = {}           # パイロット名 → (ラップ, wall)
    consecutive = {}    # パイロット名 → (3周の合計, その3周の wall)
    endurance = {}      # パイロット名 → (周回数, トータル, その通過の wall)
    for laps in laps_by_rx.values():
        recent = None   # この受信器の、いまのセッションの直近のラップ（起点を見るまでは None）
        done = True     # このセッションの耐久を数え終えたか
        broken = False  # このセッションに無効にしたラップがあったか（耐久を数えない）
        for lap in laps:
            if lap.is_start:
                recent = collections.deque(maxlen=CONSECUTIVE_LAPS)
                done, broken = False, False
                continue
            if lap.void:
                if recent is not None:
                    recent.clear()
                broken = True
                continue
            b = best.get(lap.pilot)
            if b is None or (lap.lap, lap.wall) < b:
                best[lap.pilot] = (lap.lap, lap.wall)
            if recent is not None:
                recent.append(lap)
                if len(recent) == CONSECUTIVE_LAPS:
                    t = round(sum(r.lap for r in recent), 2)
                    c = consecutive.get(lap.pilot)
                    if c is None or t < c[0]:
                        consecutive[lap.pilot] = (t, tuple(r.wall for r in recent))
                if endurance_sec > 0 and not done and lap.total >= endurance_sec:
                    done = True
                    e = endurance.get(lap.pilot)
                    if not broken and (e is None or (lap.count, -lap.total) > (e[0], -e[1])):
                        endurance[lap.pilot] = (lap.count, lap.total, lap.wall)
    best_rows = sorted(((p, v[0]) for p, v in best.items()), key=lambda r: (r[1], r[0]))
    cons_rows = sorted(((p, v[0]) for p, v in consecutive.items()), key=lambda r: (r[1], r[0]))
    end_rows = sorted(((p, v[0], v[1]) for p, v in endurance.items()), key=lambda r: (-r[1], r[2], r[0]))
    marks = {"consecutive": frozenset(w for v in consecutive.values() for w in v[1]),
             "endurance": frozenset(v[2] for v in endurance.values())}
    return best_rows, cons_rows, end_rows, {p: v[1] for p, v in best.items()}, marks


# ---------- 描画 ----------
def _put(img, s, x, y, color, thick=1, right=False):
    """x は左端（right なら右端）。"""
    if right:
        x -= cv2.getTextSize(s, FONT, SCALE, thick)[0][0]
    cv2.putText(img, s, (x, y), FONT, SCALE, color, thick, cv2.LINE_AA)


def _fit(s: str, width: int) -> str:
    """幅に収まらなければ末尾を .. にして切る。"""
    if cv2.getTextSize(s, FONT, SCALE, 1)[0][0] <= width:
        return s
    while s and cv2.getTextSize(s + "..", FONT, SCALE, 1)[0][0] > width:
        s = s[:-1]
    return s + ".."


def laps_time_str(laps: int, total: float) -> str:
    """周回数とタイムを「10/1:33.50」の形にする（RC カーの結果の書き方）。"""
    cs = int(round(total * 100))            # 59.999… が 0:60.00 にならないよう 1/100 秒の整数にしてから分ける
    m, cs = divmod(cs, 6000)
    return f"{laps}/{m}:{cs // 100:02d}.{cs % 100:02d}"


def _table(img, width, y_top, y_end, title, heads, rows, title_color=None):
    """y_top〜y_end に1つの表を描く。heads は [(見出し, x, 右寄せか)]。rows は [[文字列…]]（heads と同じ並び）。
    title_color は表の名前の色（メイン画面の一覧で、その記録のラップに付ける印と同じ色）。"""
    t = theme()
    row_thick = 1 if t.name == "dark" else 2     # 明るい配色では日光の下でも読めるよう太字
    img[y_top:y_top + TITLE_H + HEAD_H] = t.panel
    _put(img, title, 10, y_top + TITLE_H - 11, title_color or t.text, 2)
    for (h, x, right) in heads:
        _put(img, h, x, y_top + TITLE_H + HEAD_H - 9, t.sub, 2, right)
    top = y_top + TITLE_H + HEAD_H
    cv2.line(img, (0, top - 1), (width, top - 1), t.line, 2)
    n = max(0, (y_end - top) // ROW_H)
    for i, r in enumerate(rows[:n]):
        y0 = top + i * ROW_H
        if i % 2 == 0:
            img[y0:y0 + ROW_H] = t.stripe
        base = y0 + ROW_H // 2 + 7
        for k, (s, (_, x, right)) in enumerate(zip(r, heads)):
            color = t.accent if k == 1 else t.text    # パイロット名は目立つ色（ダークではバンド名と同じ黄色）
            _put(img, s, x, base, color, row_thick, right)
    if not rows and n:
        _put(img, "No record yet", 10, top + ROW_H // 2 + 7, t.sub)


def _time_table(img, width, y_top, y_end, title, head, rows, title_color=None):
    """パイロットと秒数の順位表（ベストラップ、3周連続）。"""
    pw = width - 46 - 110                      # パイロット名に使える幅
    _table(img, width, y_top, y_end, title, [("#", 30, True), ("Pilot", 46, False), (head, width - 14, True)],
           [[str(i + 1), _fit(ascii_text(p), pw), f"{t:.2f}"] for i, (p, t) in enumerate(rows)], title_color)


def render_ranking(best, consecutive, endurance, endurance_sec: float = ENDURANCE_SEC,
                   width: int = WIN_W, height: int = WIN_H) -> np.ndarray:
    """上からベストラップ、3周連続、耐久の順位表（高さを3等分）。入りきらない分は出さない。"""
    img = np.full((height, width, 3), theme().tile, np.uint8)
    y1, y2 = height // 3, height * 2 // 3
    # 表の名前は、メイン画面の一覧でその記録のラップに付ける印と同じ色（緑・mark3・mark90）
    _time_table(img, width, 0, y1, "BEST LAP", "Lap[s]", best, theme().good)
    _time_table(img, width, y1, y2, f"FASTEST {CONSECUTIVE_LAPS} CONSECUTIVE LAPS", "Time[s]", consecutive,
                theme().mark3)
    # 決まった時間内の周回数。RC カーの結果と同じ「周回数/時間」（10/1:33.50）の1列にする
    pw = width - 46 - 140
    _table(img, width, y2, height, f"MOST LAPS IN {endurance_sec:g}s",
           [("#", 30, True), ("Pilot", 46, False), ("Laps/Time", width - 14, True)],
           [[str(i + 1), _fit(ascii_text(p), pw), laps_time_str(n, t)] for i, (p, n, t) in enumerate(endurance)],
           theme().mark90)
    for y in (y1, y2):
        cv2.line(img, (0, y), (width, y), theme().line, 2)
    return img
