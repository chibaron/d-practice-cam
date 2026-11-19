"""d-practice-cam: USB受信器の映像からマーカーでラップを計測する（PC だけで動くスタンドアローン）。

Firebase への送信は隠し機能（firebase.yaml と鍵を置いたときだけ動く。docs/firebase.md）。

使い方:
    python main.py                      # 受信器を自動検索して起動
    python main.py --video a.mp4 b.mp4  # 受信器の代わりに動画ファイルで試す
    d-practice-cam.exe --debug        # exe でコンソールを出してログを見る（debug.bat）
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

import cv2

from app.capture import ChannelWorker, PassEvent
from app.config import FIREBASE_BUILT, Config
if FIREBASE_BUILT:
    from app.firebase_sync import FirebaseSync
else:       # 公開用の Firebase なしの exe。送信のコード（app/firebase_sync.py）と firebase-admin は入っていない
    from app.localsync import LocalSync as FirebaseSync
from app.laprf import DEFAULT_PORT as LAPRF_PORT, LapRFServer
from app.laptimer import make_detector
from app import ranking as rk
from app import laplog as laplog_mod
from app.i18n import set_language
from app.laplog import LAPS_DIR, LapLog
from app.ranking import render_ranking
from app.sound import SoundPlayer
from app.receivers import find_receivers, has_device_names, signature
from app import ui
from app.version import VERSION

APP_NAME = "d-practice-cam"
WIN = APP_NAME
RANK_WIN = "Ranking"
# config.yaml・ログ・sound/ を置く場所。exe（PyInstaller）なら exe のあるフォルダ
BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
log = logging.getLogger(APP_NAME)
LAP_ANNOUNCE_MAX = 60.0    # これ以上のラップはタイムをアナウンスしない
ERROR_SOUND_MIN = 3.0      # minLap 以下の通過でも、これ以下（ゲートの二重検出など）は errorlap の音を鳴らさない
DELETE_KEYS = (0x2E0000, 8)  # 選んだラップを無効にする/戻すキー（cv2.waitKeyEx の Delete、Backspace）


class App:
    def __init__(self, config: Config, videos: list[str] | None = None):
        self.config = config
        self.videos = videos or []
        set_language(config.ui_language())      # 設定画面・バンドの選択（Tk）の言語
        self.fb = FirebaseSync(config)
        self.sound = SoundPlayer(config)
        self.laprf = None                   # LapRF で通過を知らせるサーバー（laprf.enabled のときだけ）
        self.workers: list[ChannelWorker] = []
        self.measuring = False
        self.started_at = 0.0
        self.fullscreen = False
        self.message = None
        self.clicked_ch = None              # マウスでクリックされたタイル
        # 全ラップ（laps/laps.jsonl。日付では分けない）。映像の下のタイムの一覧とランキングはここから作る
        self.laplog = LapLog(laps_folder(config))
        self.ch_scroll: dict[str, int] = {}     # 受信器ごとの表示位置（0 = 最新が一番上）。マウスホイールで動かす
        self.ch_laps_lock = threading.Lock()    # ch_scroll と selected を守る
        self.selected = None                # 一覧でクリックして選んだラップ (受信器のキー, 通過時刻)。Delete で無効にする
        self.mouse_xy = (0, 0)              # メイン画面のマウスの位置（ホイールでどの列か決める）
        self.sb_drag = None                 # スクロールバーのつまみをドラッグ中なら (ch, つまみの上端からつかんだ所までの画素)
        # ランキングの別ウィンドウ（ファイルの全ラップから計算する）
        self.show_ranking = True            # K キーで開く/閉じる
        self.current_sig = None             # いま使っている受信器の組み合わせ
        self.rescan_requested = threading.Event()
        self._quit = threading.Event()

    # ---------- 受信器 ----------
    def scan(self):
        # 検索し直しても、同じ受信器で読んだパイロット名は引き継ぐ
        pilots = {w.path or str(w.source): w.pilot_state() for w in self.workers}
        self.stop_workers()
        aru = self.config.aruco
        flicker = aru.get("flicker_ms", 200)
        sources = []
        if self.videos:
            sources = [(v, None, os.path.basename(v), "") for v in self.videos[:4]]
        else:
            rx = self.config.receiver
            found = find_receivers(rx.get("device_names", []), int(rx.get("max_count", 4)))
            self.current_sig = signature(found)
            sources = [(r.index, r.backend, r.name, r.path) for r in found]
            self.message = None if found else \
                f"No receiver found (device names: {', '.join(rx.get('device_names', []))})"
        for ch, (src, backend, name, path) in enumerate(sources):
            det = make_detector(aru)
            w = ChannelWorker(ch, self.config.band_for_receiver(ch, path), src, backend,
                              self.config.capture, det, flicker, self.on_lap, self.fb.min_lap,
                              name, path, self.on_ignored, self.config.qr, self.on_pilot, self.on_clear,
                              aru.get("appear_ms", 30), self.config.data.get("no_video"))
            if (path or str(src)) in pilots:
                w.restore_pilot(*pilots[path or str(src)])
            w.start()
            self.workers.append(w)
            log.info("CH%d %s <- %s", ch + 1, w.band, name)

    def auto_scan_loop(self):
        """待機中、受信器の抜き差しを一定間隔で確認し、変わっていたら検索し直す。"""
        interval = float(self.config.receiver.get("auto_scan_sec", 3) or 0)
        if interval <= 0 or self.videos:
            return
        named = has_device_names()
        while not self._quit.wait(interval):
            # 名前で列挙できない環境では、デバイスを開いて調べるため使用中は確認しない
            if self.measuring or self.rescan_requested.is_set() or (not named and self.workers):
                continue
            try:
                rx = self.config.receiver
                found = find_receivers(rx.get("device_names", []), int(rx.get("max_count", 4)),
                                       verbose=False)
            except Exception as e:
                log.debug("Auto scan failed: %s", e)
                continue
            if signature(found) != self.current_sig:
                log.info("Receiver connections changed (%d found). Scanning again", len(found))
                self.rescan_requested.set()

    @staticmethod
    def rx_key(w: ChannelWorker) -> str:
        """受信器（USBポート）を表すキー。検索し直して並びが変わっても、タイムの表示が入れ替わらないように。"""
        return w.path or str(w.source)

    def lap_lists(self) -> list:
        """各 ch の (表示位置からの行, 表示位置, 全部の行数, 選んだラップの通過時刻)。
        画面に入るぶんだけ切り出す（全部はコピーしない）。"""
        n = len(self.workers)
        out = []
        with self.ch_laps_lock:
            for w in self.workers:
                key = self.rx_key(w)
                total = self.laplog.count(key)
                scroll = min(max(0, self.ch_scroll.get(key, 0)), ui.list_max_scroll(total, n))
                self.ch_scroll[key] = scroll
                sel = self.selected[1] if self.selected and self.selected[0] == key else None
                out.append((self.laplog.newest(key, scroll, ui.list_visible_rows(n)), scroll, total, sel))
        return out

    def scroll_laps(self, ch: int, rows: int):
        """ch のタイムの一覧を rows 行スクロールする（正なら古い方へ）。"""
        if ch >= len(self.workers):
            return
        key = self.rx_key(self.workers[ch])
        with self.ch_laps_lock:
            total = self.laplog.count(key)
            self.ch_scroll[key] = min(max(0, self.ch_scroll.get(key, 0) + rows),
                                      ui.list_max_scroll(total, len(self.workers)))

    def _list_state(self, ch: int):
        """ch のタイムの一覧の (受信器のキー, 全部の行数, いまの表示位置)。ch_laps_lock の中で呼ぶ。"""
        key = self.rx_key(self.workers[ch])
        total = self.laplog.count(key)
        scroll = min(max(0, self.ch_scroll.get(key, 0)), ui.list_max_scroll(total, len(self.workers)))
        return key, total, scroll

    def click_scrollbar(self, x: int, y: int) -> bool:
        """スクロールバーの上でボタンを押したら True。つまみならドラッグを始め、上下なら1画面ずつ動かす。"""
        n = len(self.workers)
        ch = ui.list_at(x, y, n)
        if ch is None:
            return False
        with self.ch_laps_lock:
            key, total, scroll = self._list_state(ch)
            hit = ui.scrollbar_hit(ch, x, y, n, total, scroll)
            if hit is None:
                return False
            part, grab = hit
            if part == "thumb":
                self.sb_drag = (ch, grab)
            else:
                page = ui.list_visible_rows(n)
                self.ch_scroll[key] = min(max(0, scroll + (page if part == "below" else -page)),
                                          ui.list_max_scroll(total, n))
        return True

    def set_scroll_from_bar(self, ch: int, y: int, grab: int):
        """つまみをドラッグしている位置 y（つまみの上端から grab 画素の所をつかんでいる）から表示位置を決める。"""
        if ch >= len(self.workers):
            self.sb_drag = None
            return
        with self.ch_laps_lock:
            key, total, _ = self._list_state(ch)
            self.ch_scroll[key] = ui.scroll_from_bar(ch, y - grab, len(self.workers), total)

    def select_lap(self, ch: int, row):
        """一覧の row 行目（表示している中で上から）のラップを選ぶ。選んでいるラップならやめる。起点や空の行ならやめる。"""
        with self.ch_laps_lock:
            key, _, scroll = self._list_state(ch)
            rows = self.laplog.newest(key, scroll + row, 1) if row is not None else []
            if not rows or rows[0][1] >= 999 or self.selected == (key, rows[0][4]):
                self.selected = None
            else:
                self.selected = (key, rows[0][4])

    def void_selected(self):
        """選んだラップの無効と有効を切り替える（ミス計測の取り消し）。ランキングは計算し直す。"""
        with self.ch_laps_lock:
            sel = self.selected
        if not sel:
            return
        lap = self.laplog.toggle_void(*sel)
        if lap is None:             # 見つからない（念のため）
            with self.ch_laps_lock:
                self.selected = None
            return
        log.info("Lap %s: %s %s %.2fs (%s)", "voided" if lap.void else "restored", lap.band, lap.pilot,
                 lap.lap, time.strftime("%H:%M:%S", time.localtime(lap.wall)))

    def stop_workers(self):
        for w in self.workers:
            w.stop()
        self.workers = []

    # ---------- LapRF ----------
    def restart_laprf(self):
        """設定に合わせて LapRF のサーバーを起動し直す。"""
        old, self.laprf = self.laprf, None
        if old:
            old.stop()
        cfg = self.config.data.get("laprf") or {}
        if cfg.get("enabled"):
            server = LapRFServer(LAPRF_PORT, lambda: [w.band for w in self.workers])
            if server.start():
                self.laprf = server

    # ---------- 計測 ----------
    def on_lap(self, ev: PassEvent):
        label = "START" if ev.lap >= 999 else f"{ev.lap:.2f}s"
        log.info("LAP %s %s %s%s", ev.band, ev.pilot_name, label,
                 f" (lap {ev.lap_count}, total {ev.total:.2f}s)" if ev.lap < 999 else "")
        # ラップのファイルに書き、ベストラップを更新したかを音とアナウンスに使う
        workers = self.workers
        key = self.rx_key(workers[ev.ch]) if ev.ch < len(workers) else f"ch{ev.ch + 1}"
        with self.ch_laps_lock:
            new_best = self.laplog.add(key, ev.band, ev.pilot_name, ev.lap, ev.lap_count, ev.total, ev.wall)
            if self.ch_scroll.get(key, 0) > 0:     # 古い所を見ている間は、新しい行が来ても表示をずらさない
                self.ch_scroll[key] += 1
        self.sound.play("best" if new_best else "lap")      # 最初の通過（起点）でも lap 音を鳴らす
        if ev.lap >= 999:           # 起点: lap 音の後に「〇〇、スタート」
            self.sound.announce(ev.band, ev.pilot_name, "start_text")
        elif ev.lap < LAP_ANNOUNCE_MAX:     # 周回: lap 音の後に「〇〇、11.0」。60秒以上は lap 音だけ
            # !trackSetting の totalLapAnnounce なら、続けて「3周、トータル35秒」（lap2firebase と同じ）
            # ベストラップを更新したら、最後に「ベストラップ更新」
            with_total = bool(self.fb.track_setting().get("totalLapAnnounce", True))
            self.sound.announce(ev.band, ev.pilot_name, "lap_text", lap=ev.lap,
                                lap_count=ev.lap_count if with_total else None, total=ev.total, best=new_best)
        self.fb.push_lap(ev.pilot_name, ev.lap, ev.wall)
        laprf = self.laprf
        if laprf:                   # 起点も含めて、通過をそのまま知らせる（周回の計算は受け取る側がする）
            laprf.passing(ev.ch, ev.t_pass)

    def on_clear(self, ch: int, band: str, name: str):
        """通過が無く起点をリセットした（パイロット名があればバンド名に戻した）。「F1、クリア」"""
        self.sound.announce(band, name, "clear_text")

    def on_pilot(self, ch: int, band: str, name: str):
        """QR で新しいパイロット名を読んだ。名前を読み上げる。"""
        self.sound.announce(band, name, "text")

    def on_ignored(self, ch: int, band: str, lap: float = 0.0):
        """minLap 以下で無視した通過。lap2firebase と同じく errorlap の音を鳴らす。
        ERROR_SOUND_MIN（3秒）以下はゲートの二重検出などなので鳴らさない。"""
        if lap > ERROR_SOUND_MIN:
            self.sound.play("error")

    def toggle_measuring(self):
        if not self.workers:
            return
        self.measuring = not self.measuring
        if self.measuring:
            self.fb.ensure_day_node()        # 日付ノードが無ければ作る
            self.started_at = time.monotonic()
            self.sound.play("boot")
            log.info("Measuring started%s", "" if self.fb.local else f": {self.fb.laps_path}")
        else:
            log.info("Measuring stopped")
        for w in self.workers:
            w.set_measuring(self.measuring)

    # ---------- バンドの手動変更 ----------
    def change_band(self, ch: int):
        """指定chの受信器のバンドを変える（待機中のみ）。受信器（USBポート）ごとに保存する。"""
        if self.measuring or ch >= len(self.workers):
            return
        from app.settings_dialog import ask_band
        w = self.workers[ch]
        try:
            band = ask_band(ch, w.band, w.name)
        except Exception as e:      # tkinter が無い環境など
            log.error("Cannot open the band dialog: %s", e)
            return
        if not band or band == w.band:
            return
        w.band = band
        self.config.set_band(ch, band, w.path)
        self.config.save()
        log.info("CH%d band changed to %s (%s)", ch + 1, band, w.name)

    def on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_MOUSEMOVE:
            self.mouse_xy = (x, y)
        if event == cv2.EVENT_MOUSEWHEEL:   # タイムの一覧を3行ずつスクロール（上へ回すと新しい方へ）。計測中もできる
            # ホイールの座標は環境によって当てにならないので、最後に動かした位置で列を決める
            ch = ui.list_at(*self.mouse_xy, len(self.workers))
            if ch is not None:
                # 回した量は flags の上位16ビット（符号付き）。Python 版には getMouseWheelDelta が無い
                delta = (flags >> 16) & 0xFFFF
                if delta >= 0x8000:
                    delta -= 0x10000
                self.scroll_laps(ch, -3 if delta > 0 else 3)
            return
        # スクロールバー: つまみをドラッグ、つまみの上下をクリックで1画面ずつ。計測中もできる
        if event == cv2.EVENT_MOUSEMOVE and self.sb_drag:
            if flags & cv2.EVENT_FLAG_LBUTTON:
                ch, grab = self.sb_drag
                self.set_scroll_from_bar(ch, y, grab)
            else:
                self.sb_drag = None         # ウィンドウの外で離した
            return
        if event == cv2.EVENT_LBUTTONUP:
            self.sb_drag = None
            return
        if event == cv2.EVENT_LBUTTONDOWN and self.click_scrollbar(x, y):
            return
        if event == cv2.EVENT_LBUTTONDOWN:  # タイムの一覧の行: ラップを選ぶ（Delete で無効に）。計測中もできる
            ch = ui.list_at(x, y, len(self.workers))
            if ch is not None:
                self.select_lap(ch, ui.list_row_at(ch, x, y, len(self.workers)))
                return
        if event == cv2.EVENT_LBUTTONDOWN and not self.measuring:
            for ch, (tx, ty, tw, th) in enumerate(ui.view_tile_rects(len(self.workers))):
                if tx <= x < tx + tw and ty <= y < ty + th:
                    self.clicked_ch = ch    # ダイアログはメインループで開く

    # ---------- 設定 ----------
    def open_settings(self):
        from app.settings_dialog import apply_general, apply_qr_speech, open_settings
        from app.sound import list_voices
        for w in self.workers:      # いま表示中のバンドを設定画面の初期値にする
            self.config.set_band(w.ch, w.band)
        try:
            res = open_settings(self.config, self.fb.track_setting(), max(4, len(self.workers)),
                                lambda band, name, key, cfg, lap=None, lap_count=None, total=None, best=False:
                                    self.sound.announce(band, name, key, cfg, force=True, lap=lap,
                                                        lap_count=lap_count, total=total, best=best),
                                list_voices())
        except Exception as e:      # tkinter が無い環境など
            log.error("Cannot open the settings dialog: %s", e)
            return
        if not res:
            return
        changed = apply_general(self.config, res["general"])
        qr_changed = apply_qr_speech(self.config, res["qr_speech"])
        for w in self.workers:      # 設定画面で変えたバンドを、いまの受信器に結びつけて保存
            band = self.config.band_for(w.ch)
            self.config.set_band(w.ch, band, w.path)
            w.band = band
        if changed["root"]:
            self.fb.ensure_day_node()        # 新しいルートの日付ノードを確認・作成
        if res["track"]:
            self.config.track_setting.update(res["track"])
            self.fb.update_track_setting(res["track"])
        self.config.save()
        log.info("Settings saved: %s", self.config.path)
        if changed["restart"]:
            log.warning("Key file changes take effect after a restart")
        if changed["laprf"]:
            self.restart_laprf()
        ui.set_theme(self.config.data.get("display", {}).get("theme", ui.DEFAULT_THEME))
        if changed["receivers"] or changed["aruco"] or qr_changed:
            self.scan()             # QR の設定は受信器を開き直すと反映（パイロット名は引き継ぐ）

    def toggle_theme(self):
        """配色（light / dark）を切り替えて config.yaml に保存する。"""
        name = "dark" if ui.theme().name == "light" else "light"
        ui.set_theme(name)
        self.config.data.setdefault("display", {})["theme"] = name
        self.config.save()
        log.info("Color theme: %s", name)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        cv2.setWindowProperty(WIN, cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN if self.fullscreen else cv2.WINDOW_NORMAL)

    def endurance_sec(self) -> float:
        """耐久の秒数（!trackSetting の sessionTime1、無ければ90）。"""
        try:
            return float(self.fb.track_setting().get("sessionTime1", rk.ENDURANCE_SEC))
        except (TypeError, ValueError):
            return rk.ENDURANCE_SEC

    def update_ranking_window(self, state: dict):
        """ランキングの別ウィンドウを開く・描き直す。× で閉じられたら K キーまで開かない。"""
        if not self.show_ranking:
            if state.get("open"):
                cv2.destroyWindow(RANK_WIN)
                state.clear()
            return
        if state.get("open") and cv2.getWindowProperty(RANK_WIN, cv2.WND_PROP_VISIBLE) < 1:
            self.show_ranking = False       # × で閉じた
            state.clear()
            return
        if not state.get("open"):
            cv2.namedWindow(RANK_WIN, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(RANK_WIN, rk.WIN_W, rk.WIN_H)
            state["open"] = True
        try:
            _, _, w, h = cv2.getWindowImageRect(RANK_WIN)
        except cv2.error:
            w = h = 0
        if w < 300 or h < 100:
            w, h = rk.WIN_W, rk.WIN_H
        sec = self.endurance_sec()
        key = (self.laplog.ver, w, h, sec, ui.theme().name)
        if state.get("key") == key:
            return
        best, consecutive, end = self.laplog.ranking(sec)[:3]
        cv2.imshow(RANK_WIN, render_ranking(best, consecutive, end, sec, w, h))
        state["key"] = key

    # ---------- メインループ ----------
    def run(self):
        self.fb.connect()
        self.fb.ensure_day_node()            # 起動時にも日付ノードを確認
        self.scan()
        self.restart_laprf()
        threading.Thread(target=self.auto_scan_loop, name="auto-scan", daemon=True).start()
        cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
        cv2.setWindowTitle(WIN, f"{APP_NAME} {VERSION}")
        cv2.resizeWindow(WIN, ui.CANVAS_W, ui.CANVAS_H)
        cv2.setMouseCallback(WIN, self.on_mouse)
        rank_state = {}
        dc = self.config.data.get("display", {})
        ui.set_theme(dc.get("theme", ui.DEFAULT_THEME))
        pacer = ui.RenderPacer(float(dc.get("fps", 30)), float(dc.get("low_fps", 10)),
                               float(dc.get("fps_threshold", 28)))
        try:
            while True:
                if self.rescan_requested.is_set() and not self.measuring:
                    self.scan()
                    self.rescan_requested.clear()
                # 取り込みが遅れたら描画を間引き、CPU を ArUco 検出に回す（キー操作は間引かない）
                now = time.monotonic()
                if pacer.update(now, [w.fps for w in self.workers if not w.no_signal]):
                    if pacer.low:
                        log.info("Capture FPS fell below %.0f. Limiting drawing to %.0f fps",
                                 pacer.threshold, pacer.low_fps)
                    else:
                        log.info("Capture FPS recovered. Drawing at %.0f fps again", pacer.fps)
                if pacer.due(now):
                    # ウィンドウの大きさで描く（タイムの一覧は文字の大きさを変えず、高さに入るだけの行を出す）
                    try:
                        _, _, win_w, win_h = cv2.getWindowImageRect(WIN)
                    except cv2.error:
                        win_w = win_h = 0
                    if win_w < 320 or win_h < 240:     # 最小化中など
                        win_w, win_h = ui.view().w, ui.view().h
                    ui.set_view(win_w, win_h, len(self.workers))
                    rank = self.laplog.ranking(self.endurance_sec())
                    img = ui.render(self.workers, self.measuring, self.started_at, self.fb.status,
                                    self.fb.pending, self.config.firebase["root"], self.message, self.fb.local,
                                    pacer.low_fps if pacer.low else None, self.lap_lists(),
                                    rank[3], VERSION, rank[4])
                    cv2.imshow(WIN, img)
                self.update_ranking_window(rank_state)
                key_ex = cv2.waitKeyEx(15)          # Delete は下位8ビットに入らないので waitKeyEx
                key = key_ex & 0xFF
                if key_ex in DELETE_KEYS:           # 選んだラップを無効にする/戻す。計測中もできる
                    self.void_selected()
                if key in (ord("q"), ord("Q")):     # Esc では終了しない
                    break
                if key == 27 and self.fullscreen:   # Esc は全画面の解除だけ
                    self.toggle_fullscreen()
                if key == 32:
                    self.toggle_measuring()
                elif key in (ord("f"), ord("F")):
                    self.toggle_fullscreen()
                elif key in (ord("t"), ord("T")):     # 配色（日光の下は light）。計測中も切り替えられる
                    self.toggle_theme()
                elif key in (ord("k"), ord("K")):     # ランキングの別ウィンドウを開く/閉じる
                    self.show_ranking = not self.show_ranking
                elif key in (ord("r"), ord("R")) and not self.measuring:
                    self.scan()
                elif key in (ord("s"), ord("S")) and not self.measuring:
                    self.open_settings()
                elif ord("1") <= key <= ord("4") and not self.measuring:
                    self.change_band(key - ord("1"))
                if self.clicked_ch is not None:
                    ch, self.clicked_ch = self.clicked_ch, None
                    self.change_band(ch)
                if cv2.getWindowProperty(WIN, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            self._quit.set()
            if self.laprf:
                self.laprf.stop()
            self.stop_workers()
            cv2.destroyAllWindows()


def main(argv=None):
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument("--config", default=os.path.join(BASE_DIR, "config.yaml"))
    p.add_argument("--video", nargs="+", help="video files to use instead of receivers (for testing, up to 4)")
    p.add_argument("--log", default="INFO")
    p.add_argument("--debug", action="store_true", help="open a console and show the log (for the exe; log level DEBUG)")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    args = p.parse_args(argv)

    if args.debug and sys.stdout is None:
        open_console()
    handlers = [logging.FileHandler(os.path.join(BASE_DIR, f"{APP_NAME}.log"), encoding="utf-8")]
    if sys.stdout is not None:      # コンソールの無い exe では画面には出さない
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if args.debug else getattr(logging, args.log.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    log.info("%s %s", APP_NAME, VERSION)
    try:
        run_app(args)
    except Exception:
        log.exception("Exited with an error")
        raise


def open_console():
    """コンソールの無い exe（--debug）でコンソールを開き、print とログを出せるようにする。"""
    import ctypes
    ctypes.windll.kernel32.AllocConsole()
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout


def laps_folder(config) -> str:
    """ラップのファイルを置くフォルダ（config.yaml と同じフォルダの laps/）。"""
    return os.path.join(os.path.dirname(os.path.abspath(config.path)), LAPS_DIR)


def confirm_clear_laps(folder: str, ask=None) -> None:
    """起動時にラップのファイルがあれば、消すかどうかを聞く。OK なら消して空から始める。
    ask は (通過の数, ファイルのパス) → 消すなら True（テストで差し替える。既定は Tk のダイアログ）。"""
    n = laplog_mod.stored_passes(folder)
    if n is None:
        return
    if ask is None:
        from app.settings_dialog import ask_clear_laps as ask
    try:
        ok = ask(n, laplog_mod.laps_path(folder))
    except Exception as e:      # tkinter が無い環境など: 消さずに続ける
        log.error("Cannot open the dialog to delete the lap file: %s", e)
        return
    if ok:
        laplog_mod.delete_file(folder)
    else:
        log.info("Keeping the lap file (%d pass(es))", n)


def run_app(args):
    config = Config(args.config).load()
    set_language(config.ui_language())      # 起動時の確認から Tk の画面の言語にする
    confirm_clear_laps(laps_folder(config))
    App(config, args.video).run()


if __name__ == "__main__":
    main()
