"""Tk の画面（設定画面・バンドの選択・起動時の確認）の日本語訳。

英語の文をキーにして日本語を引く（訳の無い文は英語のまま出る）。言語は config.yaml の display.language
（en / ja。設定画面の General で選ぶ）。メイン画面とランキング（OpenCV は日本語を描けない）とログは英語のまま。
英語の文を変えたら、ここのキーも直す（tests の test_i18n_covers_dialogs で確かめる）。
"""
from __future__ import annotations

LANGUAGES = ("en", "ja")
_lang = "en"


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in LANGUAGES else "en"


def language() -> str:
    return _lang


def tr(s: str, **kw) -> str:
    """s を今の言語にする。kw があれば訳した後に format する（{name} などを残したい文は kw を渡さない）。"""
    if _lang == "ja":
        s = JA.get(s, s)
    return s.format(**kw) if kw else s


JA = {
    # ---------- 起動時の確認 ----------
    "Lap records": "ラップの記録",
    "Lap records from before are in\n{path}\n({passes} passes).\n\n"
    "OK: delete them and start empty\nCancel: keep them and add new laps":
        "前に計測したラップの記録があります。\n{path}\n（通過 {passes} 件）\n\n"
        "OK: 消して空から始める\nキャンセル: 残して続きから記録する",
    # ---------- バンドの選択 ----------
    "CH{n} band": "CH{n} のバンド",
    "Band": "バンド",
    "Cancel": "キャンセル",
    # ---------- 設定画面 ----------
    "d-practice-cam {version} Settings": "d-practice-cam {version} 設定",
    "General": "全般",
    "QR & Announcements": "QR・アナウンス",
    "Course": "コース",
    "Save": "保存",
    "Language of this window": "この画面の言語",
    "Root name (Firebase)": "ルート名（Firebase）",
    "Color theme (T key)": "配色（T キー）",
    "Dark": "ダーク",
    "Light (for sunlight)": "ライト（日光の下向け）",
    "Receiver device names (comma separated)": "受信器のデバイス名（カンマ区切り）",
    "Marker type": "マーカーの種類",
    "Minimum marker size (%)": "マーカーの最小サイズ（%）",
    "Markers required at once (ArUco)": "同時に必要なマーカーの数（ArUco）",
    "FGM scan line interval (px)": "FGM の走査線の間隔（px）",
    "FGM lines required for a marker": "FGM のマーカーに必要な線の数",
    "FGM stripe tolerance (modules; smaller = fewer false detections)":
        "FGM の縞の許容誤差（モジュール。小さいほど誤検出が減る）",
    "Wait after marker appears (ms; 0 = none)": "マーカーが見えてからの待ち（ms。0 で待たない）",
    "Wait after marker is lost (ms)": "マーカーが消えてからの待ち（ms）",
    "Seconds until the frequency is released (clear; 0 = never)":
        "周波数を空けるまでの秒数（クリア。0 でしない）",
    "Clear when video is lost for (ms; 0 = never)": "映像が無いときにクリアするまで（ms。0 でしない）",
    "Send passes with the LapRF protocol (for LiveTime)": "通過を LapRF プロトコルで送る（LiveTime 用）",
    "Firebase key file path": "Firebase の鍵ファイルのパス",
    "* Key file changes take effect after a restart": "※ 鍵ファイルの変更は、起動し直すと反映します",
    "Read pilot names from QR codes": "QR コードからパイロット名を読む",
    "QR scan interval (ms)": "QR を読む間隔（ms）",
    "Announce pilot names": "アナウンスする",
    "Announce lap count and total (totalLapAnnounce)": "周回数とトータルもアナウンスする（totalLapAnnounce）",
    "Announcement language": "アナウンスの言語",
    "Japanese": "日本語",
    "English": "英語",
    "Speech synthesis reads the text below (English always; Japanese when a WAV is missing)":
        "下の文を音声合成で読みます（英語はいつも。日本語は wav が無いときだけ）",
    "QR read ({name} pilot, {band} band)": "QR を読んだとき（{name} パイロット名、{band} バンド名）",
    "Start pass (after the lap sound)": "スタートの通過（lap 音の後）",
    "Lap (after the lap sound; {time} lap time)": "ラップ（lap 音の後。{time} ラップタイム）",
    "Lap count and total ({lap} laps, {total} sec)": "周回数とトータル（{lap} 周回数、{total} 秒）",
    "New best lap (at the end of the lap)": "ベストラップ更新（ラップの最後）",
    "Clear (frequency released)": "クリア（周波数を空けた）",
    "Speech voice (empty = a voice for the language)": "音声合成の声（空ならその言語の声）",
    "Test name and time (sec)": "試す名前とタイム（秒）",
    "Test QR": "QR を試す",
    "Test start": "スタートを試す",
    "Test lap": "ラップを試す",
    "Test best": "ベストを試す",
    "Test clear": "クリアを試す",
    "* After a QR read (drone restarted), the next pass on that channel is the start. "
    "A lap sound stops any announcement":
        "※ QR を読んだら（機体を起動し直したら）、その ch の次の通過がスタートです。"
        "lap 音が鳴るとアナウンスは止まります",
    "minLap (minimum lap, sec)": "minLap（最小ラップ、秒）",
    "betterLap (multiplier)": "betterLap（倍率）",
    "sessionTime1 (sec)": "sessionTime1（秒）",
    "sessionTime2 (sec)": "sessionTime2（秒）",
    "Saving writes to config.yaml and today's !trackSetting (Firebase)":
        "保存すると config.yaml と今日の !trackSetting（Firebase）に書きます",
    "Saving writes to config.yaml": "保存すると config.yaml に書きます",
    # ---------- 入力エラー ----------
    "Input error": "入力エラー",
    "Please check the values.": "値を確かめてください。",
    "Enter at least one receiver device name": "受信器のデバイス名を1つ以上入れてください",
    "Marker type must be aruco or fgm": "マーカーの種類は aruco か fgm です",
    "FGM line interval and line count must be 1 or more": "FGM の線の間隔と数は 1 以上にしてください",
    "FGM stripe tolerance must be 0 or more": "FGM の縞の許容誤差は 0 以上にしてください",
    "Wait after marker appears must be 0 or more": "マーカーが見えてからの待ちは 0 以上にしてください",
    "Clear when video is lost must be 0 or more": "映像が無いときにクリアするまでは 0 以上にしてください",
    "QR scan interval must be 50 ms or more": "QR を読む間隔は 50 ms 以上にしてください",
    "Seconds until the frequency is released must be 0 or more": "周波数を空けるまでの秒数は 0 以上にしてください",
    "An announcement text is empty": "アナウンスの文が空です",
    "Announcement texts can only use {name} {band} {time} {lap} {total}":
        "アナウンスの文に使えるのは {name} {band} {time} {lap} {total} だけです",
    "The root name is empty": "ルート名が空です",
    "The root name cannot contain . $ # [ ] /": "ルート名に . $ # [ ] / は使えません",
    "Please enter a number": "数を入れてください",
}
