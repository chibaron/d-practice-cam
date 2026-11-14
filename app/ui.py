"""メイン画面の描画（ch を横に並べ、上に映像、下にタイム）。

配色は dark（濃い地。既定）と light（白い地に黒い文字。日光の下で見やすい）。`display.theme`、T キーで切り替える。
画面はウィンドウの大きさで描く。上の帯と映像の段は 1280×720 の配置（CANVAS_W/H）で描いて拡大縮小し、
その下のタイムの一覧は文字の大きさを固定して、ウィンドウの高さに入るだけの行を並べる（set_view）。
映像に重ねる文字（バンド名・FPS・タイマー）は黒い縁取りで描くので、どちらの配色でも同じ色。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

CANVAS_W, CANVAS_H = 1280, 720     # 上の帯と映像の段を描く配置（ウィンドウにはこれを拡大縮小して出す）
BAR_H = 44
MIN_LIST_ROWS = 3                   # ウィンドウが低くても、タイムの一覧はこの行数は出す（映像の段を縮める）

# 映像に重ねる文字・枠の色（BGR。配色によらない）
WHITE = (255, 255, 255)
GRAY = (160, 160, 160)
GREEN = (60, 220, 60)
YELLOW = (0, 220, 255)
RED = (60, 60, 230)
BLACK = (0, 0, 0)
CYAN = (255, 200, 0)
FONT = cv2.FONT_HERSHEY_SIMPLEX


@dataclass(frozen=True)
class Theme:
    """映像の外（上の帯、映像の下のタイム、ランキング）の配色（BGR）。"""
    name: str
    bg: tuple           # 映像の下のタイム・何も無い所の地
    panel: tuple        # 上の帯、ランキングの見出し
    tile: tuple         # 映像の入らない所の地、ランキングの地
    stripe: tuple       # ランキングの縞
    line: tuple         # 枠・区切りの線
    text: tuple         # ふつうの文字
    sub: tuple          # 控えめな文字（周回数とトータル、見出し、キーの案内）
    accent: tuple       # ランキングのパイロット名
    good: tuple         # RUNNING、Firebase OK、セッションのベストラップ
    warn: tuple         # STANDBY、描画の間引き
    bad: tuple          # Firebase のエラー
    mark3: tuple        # 3周連続の記録（一覧の縦線、ランキングの表の名前）
    mark90: tuple       # 90秒の周回数の記録（一覧の周回数とトータル、ランキングの表の名前）


THEMES = {
    "dark": Theme("dark", bg=(20, 20, 20), panel=(25, 25, 25), tile=(30, 30, 30), stripe=(40, 40, 40),
                  line=(70, 70, 70), text=WHITE, sub=GRAY, accent=YELLOW, good=GREEN, warn=YELLOW, bad=RED,
                  mark3=CYAN, mark90=(255, 130, 255)),
    # 日光の下では、白い地に黒い文字がいちばん読みやすい。色の付いた文字は濃くする（明るい黄色は白い地で見えない）
    "light": Theme("light", bg=(255, 255, 255), panel=(215, 215, 215), tile=(245, 245, 245),
                   stripe=(228, 228, 228), line=(110, 110, 110), text=BLACK, sub=(70, 70, 70),
                   accent=(150, 50, 0), good=(0, 130, 0), warn=(0, 100, 200), bad=(0, 0, 200),
                   mark3=(170, 120, 0), mark90=(170, 0, 170)),
}
DEFAULT_THEME = "dark"
_theme = THEMES[DEFAULT_THEME]


def set_theme(name: str) -> Theme:
    """配色を選ぶ（知らない名前なら既定の dark）。"""
    global _theme
    _theme = THEMES.get(str(name).lower(), THEMES[DEFAULT_THEME])
    return _theme


def theme() -> Theme:
    return _theme


def _text(img, s, org, scale, color, thick=2, outline=True):
    if outline:
        cv2.putText(img, s, org, FONT, scale, BLACK, thick + 3, cv2.LINE_AA)
    cv2.putText(img, s, org, FONT, scale, color, thick, cv2.LINE_AA)


def _text_size(s, scale, thick=2):
    (w, h), _ = cv2.getTextSize(s, FONT, scale, thick)
    return w, h


VIDEO_MAX_H = int((CANVAS_H - BAR_H) * 0.55)    # 映像の高さの上限（残りをタイムの一覧にする）
LIST_ROW_H = 28
LIST_SCALE = 0.65
LIST_MAX_W = 480            # 1ch のときに一覧が横に間延びしないよう、この幅で中央に置く


def _video_h(n: int) -> int:
    return min((CANVAS_W // max(1, n)) * 3 // 4, VIDEO_MAX_H)


@dataclass(frozen=True)
class View:
    """ウィンドウの大きさと、そこへの上の段（帯と映像。CANVAS_W 幅の配置）の置き方。"""
    w: int = CANVAS_W
    h: int = CANVAS_H
    scale: float = 1.0      # 上の段の拡大率
    x0: int = 0             # 上の段の左端（ウィンドウが横長すぎるときは中央に寄せる）


_view = View()


def set_view(w: int, h: int, n: int) -> View:
    """ウィンドウの大きさ（画素）に合わせて配置を決める。上の段は幅いっぱいに拡大し、
    タイムの一覧が MIN_LIST_ROWS 行より少なくなるほど低いウィンドウでは、上の段を縮めて中央に置く。"""
    global _view
    w, h = max(320, int(w)), max(240, int(h))
    top = BAR_H + _video_h(n)
    scale = min(w / CANVAS_W, max(0.1, (h - 8 - MIN_LIST_ROWS * LIST_ROW_H) / top))
    _view = View(w, h, scale, (w - round(CANVAS_W * scale)) // 2)
    return _view


def view() -> View:
    return _view


def _top_h(n: int) -> int:
    """ウィンドウの上の段（帯と映像）の高さ（画素）。"""
    return round((BAR_H + _video_h(n)) * _view.scale)


def _col_x(n: int):
    """ウィンドウでの各列の左端と幅（上の段の映像と揃える）。"""
    n = max(1, n)
    cw = CANVAS_W // n
    return [(_view.x0 + round(i * cw * _view.scale), round(cw * _view.scale)) for i in range(n)]


def tile_rects(n: int):
    """ch数に応じた映像の位置 (x, y, w, h)。上の段の配置（CANVAS_W×CANVAS_H）での位置。各列の上が映像（4:3）。"""
    n = max(1, n)
    cw, vh = CANVAS_W // n, _video_h(n)
    return [(i * cw, BAR_H, cw, vh) for i in range(n)]


def view_tile_rects(n: int):
    """ウィンドウでの映像の位置 (x, y, w, h)（クリックの判定用）。"""
    y = round(BAR_H * _view.scale)
    h = _top_h(n) - y                   # 一覧の上端（_top_h）と揃える
    return [(x, y, w, h) for x, w in _col_x(n)]


def list_rects(n: int):
    """ウィンドウでの各列の映像の下（タイムの一覧）の位置 (x, y, w, h)。"""
    y = _top_h(n)
    return [(x, y, w, _view.h - y) for x, w in _col_x(n)]


def list_visible_rows(n: int) -> int:
    """タイムの一覧に入る行数（文字の大きさは固定なので、ウィンドウが高いほど多い。どの列も同じ）。"""
    return max(1, (list_rects(n)[0][3] - 8) // LIST_ROW_H)


def list_max_scroll(total: int, n: int) -> int:
    """スクロールできる最大の行数（いちばん古い行が一番下に来るところまで）。"""
    return max(0, total - list_visible_rows(n))


def list_at(x: int, y: int, n: int):
    """画面の (x, y) がどの ch のタイムの一覧の上か（無ければ None）。"""
    for i, (lx, ly, lw, lh) in enumerate(list_rects(n)):
        if lx <= x < lx + lw and ly <= y < ly + lh:
            return i
    return None


def list_row_at(ch: int, x: int, y: int, n: int):
    """画面の (x, y) が ch のタイムの一覧の何行目（表示している中で上から0始まり）か。行の外なら None。"""
    lx, ly, lw, lh = list_rects(n)[ch]
    if not (lx <= x < lx + lw and ly + 6 <= y < ly + lh):
        return None
    i = (y - ly - 6) // LIST_ROW_H
    return i if i < max(0, (lh - 8) // LIST_ROW_H) else None


def session_str(lap_count: int, total: float) -> str:
    """起点からの周回数とトータル「[05] 1:17.25」。"""
    cs = int(round(total * 100))            # 59.999… が 0:60.00 にならないよう 1/100 秒の整数にしてから分ける
    m, cs = divmod(cs, 6000)
    return f"[{lap_count:02d}] {m}:{cs // 100:02d}.{cs % 100:02d}"


def start_label(wall) -> str:
    """セッションの区切り（起点）の行「----HH:MM:SS----」。時刻は起点の通過の時刻（ローカル時刻）。"""
    if wall is None:
        return "---"
    return "----" + time.strftime("%H:%M:%S", time.localtime(wall)) + "----"


def lap_list_lines(entries, best_walls=None, marks=None) -> list:
    """entries は新しい順の (pilot, lap, lap_count, total, wall[, void])。lap >= 999 は起点（セッションの始まり）。
    void はミス計測として無効にしたラップ（灰色にして線で消す）。

    (名前, ラップ, 周回数とトータル, ラップの色, 無効か, 3周連続の記録の周か, 90秒の記録の通過か) のリストを返す。
    marks は ranking.compute の印（3周連続の記録の3周と、90秒の周回数の記録になった通過の wall）。起点は ("----HH:MM:SS----"（通過の時刻）, "", "", 控えめな色)。
    ランキングの BEST LAP に載っているラップ（best_walls: パイロット名 → そのラップの通過時刻）を緑にする。
    同じタイムが2回あっても、通過時刻で見分けるので緑は1つ。色はいまの配色のもの。
    """
    t = _theme
    best_walls = best_walls or {}
    in3 = (marks or {}).get("consecutive", ())
    in90 = (marks or {}).get("endurance", ())
    lines = []
    for pilot, lap, count, total, wall, *rest in entries:
        void = bool(rest and rest[0])
        if lap >= 999:
            lines.append((start_label(wall), "", "", t.sub, False, False, False))
        elif void:
            lines.append((ascii_text(pilot), f"{lap:.2f}", session_str(count, total), t.sub, True, False, False))
        else:
            is_best = wall is not None and best_walls.get(pilot) == wall
            lines.append((ascii_text(pilot), f"{lap:.2f}", session_str(count, total),
                          t.good if is_best else t.text, False, wall in in3, wall in in90))
    return lines


def draw_lap_list(canvas, rect, entries, best_walls=None, scroll=0, total=None, selected=None, marks=None):
    """映像の下に、その ch のタイムを新しい順に「名前  ラップ  [周回数] トータル」で並べる。

    entries は scroll 行目からのもの（main がスクロールの位置から切り出して渡す）。total は全部の行数。
    selected はクリックで選んだラップの通過時刻（その行に枠を付ける。Delete キーで無効にする）。
    ランキングの記録のラップが分かるようにする（色はランキングの表の名前と同じ）: BEST LAP はラップを緑、
    3周連続の記録の3周はラップの右に縦線（mark3）、90秒の周回数の記録になった通過は周回数とトータルを mark90 の色。
    入りきらなければ右端にスクロールバーを出す。
    """
    x, y, w, h = rect
    t = _theme
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), t.bg, -1)
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), t.line, 1)
    lw = min(w, LIST_MAX_W)
    x0, x1 = x + (w - lw) // 2 + 12, x + (w + lw) // 2 - 12
    sess_w = _text_size("[00] 0:00.00", LIST_SCALE, 2)[0]
    x_lap = x1 - sess_w - 14                # ラップの右端（周回数とトータルの列の左）
    n_rows = max(0, (h - 8) // LIST_ROW_H)
    entries = entries[:n_rows]
    for i, (name, lap_s, sess, color, void, c3, e90) in enumerate(lap_list_lines(entries, best_walls, marks)):
        row_y = y + 6 + i * LIST_ROW_H
        base = row_y + LIST_ROW_H - 8
        if selected is not None and entries[i][4] == selected:     # 選んだラップ
            cv2.rectangle(canvas, (x0 - 8, row_y + 1), (x1 + 6, row_y + LIST_ROW_H - 2), t.warn, 2)
        if not lap_s:               # 起点（セッションの区切り）
            _text(canvas, name, (x0, base), LIST_SCALE, t.sub, 2, outline=False)
            continue
        lap_w = _text_size(lap_s, LIST_SCALE, 2)[0]
        while name and _text_size(name, LIST_SCALE, 2)[0] > x_lap - lap_w - 10 - x0:
            name = name[:-1]
        _text(canvas, name, (x0, base), LIST_SCALE, t.sub if void else t.text, 2, outline=False)
        _text(canvas, lap_s, (x_lap - lap_w, base), LIST_SCALE, color, 2, outline=False)
        sw = _text_size(sess, LIST_SCALE, 2)[0]
        _text(canvas, sess, (x1 - sw, base), LIST_SCALE, t.mark90 if e90 else t.sub, 2, outline=False)
        if c3:                      # 3周連続の記録の周: ラップの右に縦線（3行つながる）
            cv2.rectangle(canvas, (x_lap + 5, row_y), (x_lap + 8, row_y + LIST_ROW_H - 1), t.mark3, -1)
        if void:                    # 無効にしたラップは線で消す
            my = base - LIST_ROW_H // 2 + 6
            cv2.line(canvas, (x0, my), (x1, my), t.sub, 2)
    total = len(entries) + scroll if total is None else total
    bar = _scrollbar(rect, total, scroll)
    if bar:                         # スクロールバー（古い所を見ている間はつまみを目立つ色に）
        bx0, bx1, top, span, thumb, ty = bar
        cv2.rectangle(canvas, (bx0, top), (bx1, top + span), t.stripe, -1)
        cv2.rectangle(canvas, (bx0, ty), (bx1, ty + thumb), t.warn if scroll else t.line, -1)


SCROLLBAR_W = 8         # スクロールバーの幅（画素）
SCROLLBAR_HIT = 10      # クリックを受け付ける幅を、バーの左にこれだけ広げる（細いので押しやすく）


def _scrollbar(rect, total: int, scroll: int):
    """タイムの一覧 rect のスクロールバーの位置 (左, 右, 上, 長さ, つまみの長さ, つまみの上端)。
    入りきっていれば None。描くのとクリックで同じ位置を使う。"""
    x, y, w, h = rect
    n_rows = max(0, (h - 8) // LIST_ROW_H)
    if not total > n_rows > 0:
        return None
    bx1 = x + w - 3
    top, span = y + 4, h - 8
    thumb = max(16, span * n_rows // total)
    ty = top + (span - thumb) * scroll // max(1, total - n_rows)
    return bx1 - SCROLLBAR_W, bx1, top, span, thumb, ty


def scrollbar_hit(ch: int, x: int, y: int, n: int, total: int, scroll: int):
    """画面の (x, y) が ch のスクロールバーの上なら ("thumb" / "above" / "below", つまみの上端からの画素)。
    違えば None。"""
    rect = list_rects(n)[ch]
    bar = _scrollbar(rect, total, scroll)
    if not bar:
        return None
    bx0, bx1, top, span, thumb, ty = bar
    if not (bx0 - SCROLLBAR_HIT <= x <= rect[0] + rect[2] and top <= y <= top + span):
        return None
    if ty <= y <= ty + thumb:
        return "thumb", y - ty
    return ("above" if y < ty else "below"), 0


def scroll_from_bar(ch: int, thumb_top: int, n: int, total: int) -> int:
    """つまみの上端を thumb_top にしたときの表示位置（0 = 最新が一番上）。"""
    bar = _scrollbar(list_rects(n)[ch], total, 0)
    if not bar:
        return 0
    _, _, top, span, thumb, _ = bar
    max_scroll = list_max_scroll(total, n)
    return min(max(0, round((thumb_top - top) * max_scroll / max(1, span - thumb))), max_scroll)


def format_elapsed(sec) -> str:
    """最後の通過からの経過時間。10ms 未満は切り捨て（ラップと同じ）。60秒以上は「1:05.30」。無ければ --.--。"""
    if sec is None:
        return "--.--"
    cs = int(max(0.0, sec) * 100)
    m, cs = divmod(cs, 6000)
    if m:
        return f"{m}:{cs // 100:02d}.{cs % 100:02d}"
    return f"{cs // 100}.{cs % 100:02d}"


def ascii_text(s: str) -> str:
    """cv2.putText は日本語を描けないので、英数字以外は ? にする。"""
    return s.encode("ascii", "replace").decode("ascii")


def draw_tile(canvas, rect, band, frame, detection, no_signal, last_pass, measuring, fps=0.0,
              pilot=None, qr_pts=None, now=None):
    """last_pass は最後に数えた通過（起点を含む）の time.monotonic()。右下にそこからの経過時間を数え上げる。"""
    x, y, w, h = rect
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), _theme.tile, -1)
    if frame is not None and not no_signal:
        fh, fw = frame.shape[:2]
        s = min(w / fw, h / fh)
        nw, nh = int(fw * s), int(fh * s)
        ox, oy = x + (w - nw) // 2, y + (h - nh) // 2
        canvas[oy:oy + nh, ox:ox + nw] = cv2.resize(frame, (nw, nh))
        # 見つけたマーカー。「見えた」なら緑、まだなら黄色（FGM の左右が組になっていない、ArUco の min_markers に足りない）
        marker_color = GREEN if detection.visible else YELLOW
        for pts in detection.corners:
            p = (np.array(pts) * s + (ox, oy)).astype(np.int32)
            cv2.polylines(canvas, [p], True, marker_color, 2, cv2.LINE_AA)
        if qr_pts is not None:   # パイロット名を読んだ QR
            p = (np.array(qr_pts) * s + (ox, oy)).astype(np.int32)
            cv2.polylines(canvas, [p], True, CYAN, 2, cv2.LINE_AA)
        if detection.visible:   # マーカー検出中は枠を緑に
            cv2.rectangle(canvas, (x + 2, y + 2), (x + w - 3, y + h - 3), GREEN, 4)
    else:
        msg = "NO SIGNAL"
        tw, th = _text_size(msg, 1.2, 3)
        _text(canvas, msg, (x + (w - tw) // 2, y + (h + th) // 2), 1.2, RED, 3)

    scale = max(0.8, min(w / 640, h / 480) * 1.4)
    # バンド名。QR でパイロット名を読んでいれば "F1:TARO"（ラップはこの名前で送る）
    label = f"{band}:{ascii_text(pilot)}" if pilot else band
    ls = scale * 1.1
    while _text_size(label, ls, 3)[0] > w - 160 and ls > 0.5:   # 右上の FPS と重ならないよう縮める
        ls *= 0.9
    _text(canvas, label, (x + 14, y + int(42 * scale)), ls, YELLOW, 3)
    # 取り込み・検出のフレームレート（28fps以上は緑、20fps以下は赤、その間は黄色）
    fps_v = 0.0 if no_signal else fps
    fps_s = f"{fps_v:.1f} fps"
    fps_color = GREEN if fps_v >= 28 else (RED if fps_v <= 20 else YELLOW)
    fw_, _ = _text_size(fps_s, 0.7, 2)
    _text(canvas, fps_s, (x + w - fw_ - 14, y + 32), 0.7, fps_color, 2)
    if measuring:       # ゲート通過で 0 に戻り、数え上げるタイマー
        now = time.monotonic() if now is None else now
        timer_s = format_elapsed(None if last_pass is None else now - last_pass)
        # 数字の幅で文字が左右に揺れないよう、全部 0 にしたときの幅で左端を決める
        tw, _ = _text_size("".join("0" if c.isdigit() else c for c in timer_s), scale * 1.6, 4)
        _text(canvas, timer_s, (x + w - tw - 18, y + h - 20), scale * 1.6, YELLOW, 4)
    cv2.rectangle(canvas, (x, y), (x + w - 1, y + h - 1), _theme.line, 1)


class RenderPacer:
    """メイン画面の描画の間隔を決める。取り込み・ArUco 検出（ch ごとのスレッド）は間引かない。

    ふだんは fps まで。どれかの ch の取り込み FPS が threshold を下回ったら low_fps に間引き、
    全 ch が threshold 以上の状態が recover_sec 続いたら戻す（境目でちらつかないように）。
    """

    def __init__(self, fps: float = 30, low_fps: float = 10, threshold: float = 28, recover_sec: float = 3):
        self.fps, self.low_fps, self.threshold, self.recover_sec = fps, low_fps, threshold, recover_sec
        self.low = False
        self._ok_since = None
        self._next = 0.0             # 次に描く時刻

    def update(self, now: float, fps_values) -> bool:
        """各 ch の取り込み FPS（映像の来ている ch だけ）で間引くか決める。切り替わったら True。"""
        vals = [v for v in fps_values if v > 0]
        if vals and min(vals) < self.threshold:
            self._ok_since = None
            if not self.low:
                self.low = True
                return True
        elif self.low:
            if self._ok_since is None:
                self._ok_since = now
            elif now - self._ok_since >= self.recover_sec:
                self.low = False
                self._ok_since = None
                return True
        return False

    def due(self, now: float) -> bool:
        """描く時刻になっていれば True。次に描く時刻を一定間隔で進める（ループの刻みが粗くても fps を保つ）。"""
        if now < self._next:
            return False
        iv = 1.0 / (self.low_fps if self.low else self.fps)
        self._next += iv
        if self._next <= now:       # 大きく遅れたら追いつこうとせず、今から数え直す
            self._next = now + iv
        return True


def draw_bar(canvas, measuring, started_at, fb_status, fb_pending, root, n_rx, fb_local=False,
             thinned_fps=None):
    t = _theme
    cv2.rectangle(canvas, (0, 0), (CANVAS_W, BAR_H), t.panel, -1)
    cv2.line(canvas, (0, BAR_H - 1), (CANVAS_W, BAR_H - 1), t.line, 1)
    if measuring:
        el = int(time.monotonic() - started_at)
        state, color = f"RUNNING  {el // 60:02d}:{el % 60:02d}", t.good
    else:
        state, color = "STANDBY", t.warn
    _text(canvas, state, (14, 31), 0.85, color, 2, outline=False)
    # Firebase は隠し機能。firebase.yaml・鍵が無い（local、スタンドアローン）ときは何も出さない
    if not fb_local:
        fb = f"Firebase: {fb_status}"
        if fb_pending:
            fb += f" (queued {fb_pending})"
        fb_color = t.good if fb_status == "OK" else t.bad     # つながらない・エラーなら赤
        _text(canvas, fb, (330, 31), 0.65, fb_color, 2, outline=False)
        _text(canvas, f"root: {root}", (640, 31), 0.6, t.sub, 1, outline=False)
    if thinned_fps:     # 取り込みの FPS が下がり、描画を間引いている
        _text(canvas, f"DISPLAY {thinned_fps:g}fps", (760, 31), 0.55, t.warn, 1, outline=False)
    help_s = "Space:start  1-4/click:band  S:settings  K:ranking  T:theme  F:full  Q:quit" if not measuring \
        else "Space:stop  click lap+Del:void  K:ranking  T:theme  F:full  Q:quit"
    hw, _ = _text_size(help_s, 0.5, 1)
    _text(canvas, help_s, (CANVAS_W - hw - 12, 30), 0.5, t.sub, 1, outline=False)


def render(workers, measuring, started_at, fb_status, fb_pending, root, message=None, fb_local=False,
           thinned_fps=None, lap_lists=None, best_walls=None, version=None, marks=None):
    """lap_lists は workers と同じ並びで、各 ch のタイム (entries, scroll, total)。
    entries は scroll 行目からの新しい順の (pilot, lap, lap_count, total, wall, void)、total は全部の行数。
    各 ch に4つ目があれば、選んだラップの通過時刻（その行に枠を付ける）。
    best_walls はランキングの BEST LAP（パイロット名 → そのラップの通過時刻）。そのラップを緑にする。
    version はアプリのバージョン（待機中と受信器が無いときに出す）。
    大きさは set_view で決めたウィンドウの大きさ。上の帯と映像は CANVAS_W×CANVAS_H の配置で描いて拡大縮小し、
    タイムの一覧はウィンドウの大きさのまま描く（文字の大きさを変えない）。"""
    canvas = np.full((CANVAS_H, CANVAS_W, 3), _theme.bg, np.uint8)
    draw_bar(canvas, measuring, started_at, fb_status, fb_pending, root, len(workers), fb_local, thinned_fps)
    v = _view
    if not workers:
        lines = [message or "No receiver found.", "Connect receivers. They are detected automatically."]
        for i, s in enumerate(lines):
            tw, _ = _text_size(s, 1.0, 2)
            _text(canvas, s, ((CANVAS_W - tw) // 2, 330 + i * 50), 1.0, _theme.text, 2, outline=False)
        if version:
            vs = version_str(version)
            vw, _ = _text_size(vs, 0.6, 1)
            _text(canvas, vs, ((CANVAS_W - vw) // 2, 330 + 2 * 50 + 10), 0.6, _theme.sub, 1, outline=False)
        if (v.w, v.h) != (CANVAS_W, CANVAS_H):
            canvas = cv2.resize(canvas, (v.w, v.h), interpolation=cv2.INTER_AREA if v.scale < 1 else cv2.INTER_LINEAR)
        return canvas
    n = len(workers)
    lap_lists = lap_lists or [((), 0, 0, None)] * n
    for w, rect in zip(workers, tile_rects(n)):
        frame, det, _, last_pass, fps, pilot, qr_pts = w.snapshot()
        draw_tile(canvas, rect, w.band, frame, det, w.no_signal, last_pass, measuring, fps,
                  pilot, qr_pts)
    if not measuring:
        draw_standby_banner(canvas, n, version)
    top = canvas[:BAR_H + _video_h(n)]
    out = np.full((v.h, v.w, 3), _theme.bg, np.uint8)
    th = _top_h(n)
    if (v.w, v.h) == (CANVAS_W, CANVAS_H) and v.scale == 1.0:
        out[:th] = top
    else:
        tw = round(CANVAS_W * v.scale)
        out[:round(BAR_H * v.scale)] = _theme.panel        # 上の段を中央に寄せたときも、帯は幅いっぱい
        out[:th, v.x0:v.x0 + tw] = cv2.resize(top, (tw, th),
                                              interpolation=cv2.INTER_AREA if v.scale < 1 else cv2.INTER_LINEAR)
    for lrect, entries in zip(list_rects(n), lap_lists):
        entries, scroll, total, *sel = entries
        draw_lap_list(out, lrect, list(entries), best_walls, scroll, total, sel[0] if sel else None, marks)
    return out


def version_str(version: str) -> str:
    return f"d-practice-cam ver. {version}"


def draw_standby_banner(canvas, n: int = 1, version=None):
    """待機中は Space で計測を始めることを映像の段の中央に大きく出す（映像が透けるよう半透明の帯）。
    帯の右下にバージョンを小さく出す（全画面ではウィンドウのタイトルが見えないため）。"""
    bh = 130
    y0 = BAR_H + max(0, (_video_h(n) - bh) // 2)
    band = canvas[y0:y0 + bh]
    band[:] = (band * 0.35).astype(np.uint8)
    main_s, sub_s = "PRESS SPACE TO START", "STANDBY - laps are not recorded"
    tw, th = _text_size(main_s, 2.0, 5)
    _text(canvas, main_s, ((CANVAS_W - tw) // 2, y0 + 20 + th), 2.0, YELLOW, 5)
    sw, sh = _text_size(sub_s, 0.8, 2)
    _text(canvas, sub_s, ((CANVAS_W - sw) // 2, y0 + bh - 18), 0.8, WHITE, 2)
    if version:
        vs = version_str(version)
        vw, _ = _text_size(vs, 0.5, 1)
        _text(canvas, vs, (CANVAS_W - vw - 14, y0 + bh - 10), 0.5, GRAY, 1, outline=False)
