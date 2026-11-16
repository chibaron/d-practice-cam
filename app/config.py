"""config.yaml と firebase.yaml の読み込みと保存。

Firebase の設定（firebase: の部分）だけは config.yaml と同じフォルダの firebase.yaml に分けて保存する。
どちらも PC ごとのファイルで Git の管理外（ひな形は *.example.yaml）。
"""
from __future__ import annotations

import copy
import os
import re

import yaml

# lap2firebase.py が !trackSetting を初期化するときの値と同じ
DEFAULT_TRACK_SETTING = {
    "minLap": 10,
    "betterLap": 1.07,
    "lapMeterMax": 28,
    "lapMeterPass1": 15,
    "lapMeterPass2": 20,
    "sessionLaps1": 4,
    "sessionTime1": 90,
    "sessionTime2": 0,
    "totalLapAnnounce": True,
}

DEFAULT_CONFIG = {
    "receiver": {
        # 受信器として採用するデバイス名（部分一致、大文字小文字は区別しない、複数可）
        "device_names": ["USB2.0 PC CAMERA", "USB Video"],
        "max_count": 4,
        "auto_scan_sec": 3,    # 待機中に受信器の抜き差しを確認する間隔（秒）。0 で無効
    },
    # 受信器ごとのバンド（キー: 受信器のデバイスパス = つないでいるUSBポート）。画面から変更すると保存される
    "receiver_bands": {},
    # 上に無い受信器は、ch番号順にこのバンドを使う
    "channels": [
        {"band": "F1"},
        {"band": "F4"},
        {"band": "E1"},
        {"band": "A8"},
    ],
    "capture": {"width": 640, "height": 480, "fps": 30},
    # メイン画面の描画（取り込み・ArUco 検出は間引かない）
    "display": {
        "fps": 30,             # ふだんの描画の上限
        "low_fps": 10,         # どれかの ch の取り込み FPS が fps_threshold を下回ったら、この fps に間引く
        "fps_threshold": 28,
        "theme": "dark",       # 配色。dark（濃い地）/ light（白い地、日光の下で見やすい）。T キーで切り替え
    },
    "aruco": {
        "marker_type": "fgm",    # fgm（FpvGateMarker。左右2枚、円形ゲートの輪も）/ aruco（ArUco、TinyViewPlus と共通）
        "dictionary": "DICT_4X4_50",
        "min_size_pct": 5,     # マーカー一辺（FGM は幅）の最小サイズ（フレーム高さに対する%）
        "min_markers": 1,      # 同一フレームでこの個数以上見えたときだけ有効（ArUco のみ）
        "fgm_line_step": 4,    # FGM: 何画素おきの横線で探すか
        "fgm_min_lines": 3,    # FGM: この本数の横線で続けて見つかったらマーカーとする
        "fgm_tol": 0.25,       # FGM: 縞の間隔のずれの許容（マス）。小さいほど誤検出が減るが、遠くのマーカーは読みにくい
        "appear_ms": 30,       # この時間続けて見えたら「見えた」とする（誤検出対策。30fps で1フレーム分）。0 ですぐ
        "flicker_ms": 200,     # マーカー消失後、通過を確定するまでの待ち時間
    },
    # ドローン起動時に映像へ出る QR コードをパイロット名として読む（ArUco とは別スレッド）
    "qr": {
        "enabled": True,
        "interval_ms": 500,        # QR を探す間隔（短いほど CPU を使う）
        "pilot_timeout_sec": 60,   # この秒数ゲート通過が無ければ次の通過を起点にし、バンド名に戻す。0 でしない
    },
    # 映像が無い（砂嵐、受信器の単色の画面、フレームが来ない）間はマーカーを探さず、続いたらクリアする
    "no_video": {
        "enabled": True,
        "clear_ms": 3000,      # この時間映像が無ければ起点とパイロット名を消してクリアする。0 でしない
    },
    # 通過を LapRF プロトコルで知らせる TCP サーバー（LiveTime などが接続する）。ファイアウォールの確認が出るので既定はオフ
    "laprf": {
        "enabled": False,     # ポートは 5403 に固定（app.laprf.DEFAULT_PORT。設定はしない）
    },
    # firebase.yaml に保存する（config.yaml には書かない）
    "firebase": {
        "credentials": "./serviceAccount.json",
        "database_url": "",    # https://<プロジェクト>-default-rtdb.<リージョン>.firebasedatabase.app/
        "root": "practice",
        "auth_uid": "python-script",
        "timeout_sec": 10,     # 送信をあきらめるまでの秒数（オフラインのとき）。失敗したラップは再送しない
    },
    "track_setting": copy.deepcopy(DEFAULT_TRACK_SETTING),
    # ラップ確定時の音（wav が無ければビープ音）
    "sound": {
        "enabled": True,
        "lap": "./sound/lap.wav",       # ラップ確定時（各chの最初の通過＝起点でも鳴らす）
        "best": "./sound/bestlap.wav",  # ベストラップを更新したラップ確定時（lap の代わり）
        "error": "./sound/errorlap.wav",  # minLap 以下で計測しなかった通過
        "boot": "./sound/boot.wav",       # 待機中から計測中になったとき（lap2firebase の boot.wav）
    },
    # QR でパイロット名を読んだときの読み上げ（Windows の音声合成。lap 音が来たら止めて lap 音を優先）
    "speech": {
        "enabled": True,
        # パイロットの「名前.wav」の置き場（Git の管理外）。ユーザーが VOICEVOX などで作って置く。無い名前は Windows の声で作る
        "pilot_dir": "./sound/pilot",
        "text": "{name}、エントリー",   # QR を読んだとき。{name} はパイロット名、{band} はバンド名
        "start_text": "{name}、スタート",   # 起点の通過（lap 音の後）。QR を読んでいない ch はバンド名
        "lap_text": "{name}、{time}",      # 周回ラップ（lap 音の後）。{time} は 0.1 秒単位のタイム
        "total_text": "{lap}周、トータル{total}秒",  # 周回ラップの続き（!trackSetting の totalLapAnnounce のとき）
        "clear_text": "{band}、クリア",     # 通過が無く起点をリセットしたとき（wav は「バンド_clear.wav」）
        "best_text": "ベストラップ更新",     # ベストラップを更新したとき、周回ラップの最後（wav は newbest.wav）
        "voice": "",                       # 声の名前の一部（例: Haruka）。空なら日本語の声
        "language": "en",                  # アナウンスの言語。en / ja（en は wav を使わず音声合成だけ）
        # language: en のときの文と声。上の text などは日本語用
        "en": {
            "text": "{name}, entry",
            "start_text": "{name}, start",
            "lap_text": "{name}, {time}",
            "total_text": "lap {lap}, total {total} seconds",
            "clear_text": "{band}, clear",
            "best_text": "new best lap",
            "voice": "",                   # 空なら英語の声
        },
    },
}

HEADER = (
    "# d-practice-cam settings\n"
    "# Saving in the settings dialog (S key) overwrites this file.\n"
    "# Firebase settings are in firebase.yaml in the same folder.\n\n"
)
FIREBASE_HEADER = (
    "# d-practice-cam Firebase settings (not in Git; template: firebase.example.yaml)\n"
    "# Saving in the settings dialog (S key) overwrites this file.\n"
    "# credentials is only the path to the key file (never the key itself).\n\n"
)
FIREBASE_FILE = "firebase.yaml"


def _firebase_built() -> bool:
    """この exe に Firebase の送信を入れたか。tools/build_exe.py の公開用（Firebase なし）は
    app/_build_version.py に FIREBASE = False と書き、firebase-admin も入れない。ソースから動かすときは True。"""
    try:
        from ._build_version import FIREBASE
    except ImportError:
        return True
    return bool(FIREBASE)


# False なら firebase.yaml を置いても読まず・書かず、Firebase には一切つながない
FIREBASE_BUILT = _firebase_built()

# Firebase のキーに使えない文字
_INVALID_KEY_CHARS = re.compile(r"[.$#\[\]/]")


def validate_root(root: str) -> str | None:
    """書き込み先ルート名をチェックし、問題があればエラーメッセージを返す。"""
    if root is None or root.strip() == "":
        return "The root name is empty"
    if _INVALID_KEY_CHARS.search(root):
        return "The root name cannot contain . $ # [ ] /"
    return None


def _merge(base: dict, override: dict) -> dict:
    """既定値に、ファイルの値を重ねる（足りないキーは既定値で補う）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _rename_bar_to_fgm(aruco: dict) -> None:
    """旧名（バーマーカー: marker_type: bar、bar_*）を FGM の名前に読み替える。"""
    if aruco.get("marker_type") == "bar":
        aruco["marker_type"] = "fgm"
    for old, new in (("bar_line_step", "fgm_line_step"), ("bar_min_lines", "fgm_min_lines")):
        if old in aruco:
            aruco.setdefault(new, aruco.pop(old))


class Config:
    def __init__(self, path: str):
        self.path = path
        self.firebase_path = os.path.join(os.path.dirname(os.path.abspath(path)), FIREBASE_FILE)
        self.data = copy.deepcopy(DEFAULT_CONFIG)

    def has_firebase_file(self) -> bool:
        """firebase.yaml を置いてあり、この exe で Firebase を使えるか（FIREBASE_BUILT）。"""
        return FIREBASE_BUILT and os.path.exists(self.firebase_path)

    def load(self) -> "Config":
        """ファイルを読む。無ければ既定値で新しく作る。"""
        if not os.path.exists(self.path):
            # 初めて起動したとき、日本語音声（sound/voice の wav）が入っていれば（日本語版の zip）日本語で始める
            voice = os.path.join(os.path.dirname(os.path.abspath(self.path)), "sound", "voice")
            if os.path.isdir(voice) and any(fn.lower().endswith(".wav") for fn in os.listdir(voice)):
                self.data["speech"]["language"] = "ja"
            if self.has_firebase_file():
                self.data["firebase"] = _merge(DEFAULT_CONFIG["firebase"], self._read(self.firebase_path))
            self.save()
            return self
        loaded = self._read(self.path)
        loaded.pop("firebase", None)        # Firebase の設定は firebase.yaml からだけ読む
        _rename_bar_to_fgm(loaded.get("aruco") or {})
        if self.has_firebase_file():
            loaded["firebase"] = self._read(self.firebase_path)
        self.data = _merge(DEFAULT_CONFIG, loaded)
        # 旧形式の device_names 文字列にも対応
        names = self.data["receiver"].get("device_names")
        if isinstance(names, str):
            self.data["receiver"]["device_names"] = [names]
        self.data["speech"].pop("readings", None)     # 廃止した読み方の辞書
        self.data["speech"].pop("wav_dir", None)      # 廃止した音声パックのフォルダ（sound/voice に固定）
        self.data["laprf"].pop("port", None)           # 廃止した LapRF のポート（5403 に固定）
        self.data["aruco"].pop("ids", None)           # 廃止したマーカーID（0〜3 に固定）
        return self

    @staticmethod
    def _read(path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _write(path: str, header: str, data: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(header)
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, path)

    def save(self) -> None:
        """config.yaml と firebase.yaml に分けて書く。firebase.yaml は自分で作ったときだけ書き戻し、
        アプリからは作らない（無い = オフラインで動かす、のままにする）。"""
        self._write(self.path, HEADER, {k: v for k, v in self.data.items() if k != "firebase"})
        if self.has_firebase_file():
            self._write(self.firebase_path, FIREBASE_HEADER, self.data["firebase"])

    # よく使う値へのショートカット
    @property
    def receiver(self) -> dict:
        return self.data["receiver"]

    @property
    def channels(self) -> list:
        return self.data["channels"]

    @property
    def capture(self) -> dict:
        return self.data["capture"]

    @property
    def aruco(self) -> dict:
        return self.data["aruco"]

    @property
    def qr(self) -> dict:
        return self.data["qr"]

    @property
    def firebase(self) -> dict:
        return self.data["firebase"]

    @property
    def track_setting(self) -> dict:
        return self.data["track_setting"]

    def band_for(self, ch_index: int) -> str:
        if ch_index < len(self.channels):
            return str(self.channels[ch_index].get("band", f"CH{ch_index + 1}"))
        return f"CH{ch_index + 1}"

    def band_for_receiver(self, ch_index: int, path: str = "") -> str:
        """受信器のバンド。その受信器（USBポート）に保存したバンドがあればそれ、無ければ ch 番号順の既定。"""
        bands = self.data.get("receiver_bands") or {}
        if path and path in bands:
            return str(bands[path])
        return self.band_for(ch_index)

    def set_band(self, ch_index: int, band: str, path: str = "") -> None:
        if path:
            self.data.setdefault("receiver_bands", {})[path] = band
        chs = self.data["channels"]
        while len(chs) <= ch_index:
            chs.append({"band": f"CH{len(chs) + 1}"})
        chs[ch_index] = {"band": band}

    def resolve_path(self, p: str) -> str:
        """設定ファイルの場所を基準に相対パスを解決する。"""
        if os.path.isabs(p):
            return p
        return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(self.path)), p))
