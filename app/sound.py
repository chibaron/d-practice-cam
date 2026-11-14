"""ラップ確定時の音とパイロット名のアナウンス。別スレッドで順番に鳴らすので、計測や画面は止まらない。

アナウンスは lap2firebase の wav（VOICEVOX）で「名前.wav → entry.wav / start.wav」と鳴らす。
決まった言葉（entry・start・タイム・周回数など）は sound/voice（VOICE_DIR。固定）、パイロットの名前.wav は
speech.pilot_dir（sound/pilot。Git の管理外）に置く。パイロットの名前.wav はユーザーが VOICEVOX などで作って置ける。
名前.wav が無ければアプリが作って pilot_dir に保存し、同じように wav をつないで鳴らす。この PC で VOICEVOX が
起動していれば VOICEVOX（sound/voice と同じ四国めたん）で、起動していなければ Windows の音声合成（SAPI）の声で作る。
一度作った wav は作り直さない（VOICEVOX の声にしたいときは、その wav を消すか上書きする）。どちらも作れなければ文を読み上げる。
アナウンス中に lap 音・errorlap 音が来たら、アナウンスを止めて音を先に鳴らす。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

log = logging.getLogger(__name__)

# SAPI の Speak のフラグ
SVSF_ASYNC = 1          # 読み上げの終わりを待たない（その間に lap 音が来たら止められるように）
SVSF_PURGE = 2          # 読み上げ中・待ちの文を捨てる

DEFAULT_TEXT = {"text": "{name}、エントリー", "start_text": "{name}、スタート", "lap_text": "{name}、{time}",
                "total_text": "{lap}周、トータル{total}秒", "clear_text": "{band}、クリア",
                "best_text": "ベストラップ更新"}
DEFAULT_TEXT_EN = {"text": "{name}, entry", "start_text": "{name}, start", "lap_text": "{name}, {time}",
                   "total_text": "lap {lap}, total {total} seconds", "clear_text": "{band}, clear",
                   "best_text": "new best lap"}
# 声を指定しないときに探す SAPI の言語（en は米・英・豪の順）
VOICE_LANG = {"ja": ("411",), "en": ("409", "809", "C09")}
# アナウンスの種類ごとに、名前の wav の後に鳴らす lap2firebase の wav（ラップはタイムの wav）
WAV_AFTER_NAME = {"text": "entry.wav", "start_text": "start.wav"}
# ベストラップを更新したとき、周回ラップのアナウンスの最後に鳴らす wav（lap2firebase と同じ）
BEST_WAV = "newbest.wav"
_BAD_FILENAME_CHARS = set('\\/:*?"<>|')
# 非同期の再生は鳴り始めが遅れる（特に最初の1回）ので、次の音を始めるまで少し余分に待つ
PLAY_MARGIN_SEC = 0.3
SAFT_24KHZ_16BIT_MONO = 26      # SAPI の SpAudioFormat.Type（sound/voice の wav と同じ形式）
SSFM_CREATE_FOR_WRITE = 3
# SAPI の wav は前後の無音が長いので、sound/voice の VOICEVOX の wav（前 約0.12秒・後 約0.09秒）に合わせて切る
SAPI_TRIM = (0.1, 0.09)
SAPI_SILENCE = 300              # これより小さい値（16bit）は無音とみなす
# VOICEVOX（この PC で起動しているエンジンの HTTP API。固定で設定はしない）。lap2firebase の createwav.py と同じ作り方
VOICEVOX_URL = "http://127.0.0.1:50021"
VOICEVOX_SPEAKER = 2            # 四国めたん ノーマル（sound/voice の wav と同じ声）
VOICEVOX_SPEED = 1.2
VOICEVOX_CHECK_SEC = 1.0        # 起動しているかの確認で待つ秒数（起動していなければ、すぐ Windows の声で作る）
VOICEVOX_TIMEOUT_SEC = 10.0     # 音声を作るのに待つ秒数


def name_ok(name: str) -> bool:
    """ファイル名に使える名前か。"""
    return bool(name) and not (set(name) & _BAD_FILENAME_CHARS) and name.strip(". ") == name


def name_wav(name: str, pilot_dir: str = "", wav_dir: str = "") -> str | None:
    """名前.wav の場所。pilot_dir、無ければ wav_dir（バンド名の F1.wav や、前の置き方）を探す。"""
    if not name_ok(name):
        return None
    for d in (pilot_dir, wav_dir):
        if d and os.path.isfile(os.path.join(d, f"{name}.wav")):
            return os.path.join(d, f"{name}.wav")
    return None


def trim_silence(path: str, keep=SAPI_TRIM) -> None:
    """wav の前後の無音を、前 keep[0] 秒・後 keep[1] 秒だけ残して切る（16bit モノラル）。"""
    import numpy as np
    with wave.open(path) as w:
        params = w.getparams()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    loud = np.nonzero(np.abs(x.astype(np.int32)) > SAPI_SILENCE)[0]
    if params.nchannels != 1 or params.sampwidth != 2 or not len(loud):
        return
    sr = params.framerate
    x = x[max(0, loud[0] - int(sr * keep[0])):loud[-1] + int(sr * keep[1])]
    with wave.open(path, "wb") as w:
        w.setparams(params)
        w.writeframes(x.tobytes())


def voicevox_synth(text: str) -> bytes | None:
    """この PC で VOICEVOX が起動していれば、text を読んだ wav（16bit モノラル 24kHz。sound/voice と同じ形式）の
    バイト列を返す。起動していない・失敗したら None。"""
    try:
        with urllib.request.urlopen(f"{VOICEVOX_URL}/version", timeout=VOICEVOX_CHECK_SEC):
            pass
    except (OSError, ValueError):
        return None                 # 起動していない（ふつうのこと。ログには出さない）
    q = urllib.parse.urlencode({"text": text, "speaker": VOICEVOX_SPEAKER})
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{VOICEVOX_URL}/audio_query?{q}", method="POST"),
                                    timeout=VOICEVOX_TIMEOUT_SEC) as r:
            query = json.loads(r.read())
        query.update(speedScale=VOICEVOX_SPEED, outputSamplingRate=24000, outputStereo=False)
        req = urllib.request.Request(f"{VOICEVOX_URL}/synthesis?speaker={VOICEVOX_SPEAKER}", method="POST",
                                     data=json.dumps(query).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=VOICEVOX_TIMEOUT_SEC) as r:
            data = r.read()
    except (OSError, ValueError) as e:
        log.warning("VOICEVOX is running but could not make the voice: %s", e)
        return None
    if data[:4] != b"RIFF":
        log.warning("VOICEVOX did not return a wav")
        return None
    return data


def make_voicevox_name_wav(name: str, pilot_dir: str) -> str | None:
    """VOICEVOX が起動していれば、pilot_dir に「名前.wav」を作る。作れなければ None。"""
    if not pilot_dir or not name_ok(name):
        return None
    data = voicevox_synth(name)
    if not data:
        return None
    path = os.path.join(pilot_dir, f"{name}.wav")
    tmp = os.path.join(pilot_dir, f"{name}.tmp.wav")
    try:
        os.makedirs(pilot_dir, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Cannot save %s: %s", path, e)
        return None
    log.info("Created %s with VOICEVOX", path)
    return path


def make_sapi_name_wav(name: str, pilot_dir: str, voice) -> str | None:
    """Windows の音声合成（SAPI。voice はこのスレッドで作った SpVoice）で pilot_dir に「名前.wav」を作る。
    形式は sound/voice の wav と同じ 16bit モノラル 24kHz（つないで1回で鳴らせるように）。作れなければ None。"""
    if not pilot_dir or not name_ok(name) or not voice:
        return None
    path = os.path.join(pilot_dir, f"{name}.wav")
    tmp = os.path.join(pilot_dir, f"{name}.tmp.wav")
    try:
        import comtypes.client
        os.makedirs(pilot_dir, exist_ok=True)
        fmt = comtypes.client.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = SAFT_24KHZ_16BIT_MONO
        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        stream.Format = fmt
        stream.Open(tmp, SSFM_CREATE_FOR_WRITE, False)
        try:
            voice.AudioOutputStream = stream
            voice.Speak(name, 0)
        finally:
            stream.Close()
        trim_silence(tmp)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("Cannot create %s with speech synthesis: %s", path, e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    log.info("Created %s with speech synthesis", path)
    return path


VOICE_DIR = "./sound/voice"   # 日本語音声パック（lap2firebase の VOICEVOX の wav）。固定で設定はしない


def effective_speech(cfg: dict) -> dict:
    """言語に合わせた speech の設定。en は speech.en の文と声を使い、日本語の wav は使わない。"""
    if cfg.get("language") != "en":
        return cfg
    return {**cfg, **DEFAULT_TEXT_EN, "voice": "", **(cfg.get("en") or {}),
            "wav_dir": "", "pilot_dir": ""}


def lap_tenths(lap: float) -> int:
    """ラップを 0.1 秒単位に切り捨てる（11.06 → 110）。lap2firebase のタイムの wav 名と同じ。"""
    return int(round(lap * 100)) // 10


def speech_text(speech_cfg: dict, band: str, name: str, key: str = "text", lap: float | None = None,
                lap_count: int | None = None, total: float | None = None) -> str:
    """音声合成で読み上げる文。key は "text"（QR を読んだとき）、"start_text"（起点の通過）、
    "lap_text"（周回ラップ。{time} は 0.1 秒単位のタイム）、"total_text"（周回数 {lap} とトータル {total} 秒）、
    "clear_text"（通過が無くリセットしたとき）、"best_text"（ベストラップ更新。周回ラップの最後）。"""
    template = str(speech_cfg.get(key, DEFAULT_TEXT[key]))
    time_s = f"{lap_tenths(lap) / 10:.1f}" if lap is not None else ""
    try:
        return template.format(name=name, band=band, time=time_s, lap=lap_count if lap_count is not None else "",
                               total=int(total) if total is not None else "")
    except (KeyError, IndexError, ValueError):
        log.warning("Invalid format in speech.%s: %s", key, template)
        return name


def announce_parts(speech_cfg: dict, band: str, name: str, key: str, wav_dir: str = "",
                   lap: float | None = None, lap_count: int | None = None, total: float | None = None,
                   pilot_dir: str = "", best: bool = False) -> list:
    """アナウンスの中身。[("file", wavのパス), ...] か [("tts", 文)]。
    「名前.wav」（pilot_dir か wav_dir）と、wav_dir のその後の wav（entry.wav / start.wav / タイムの wav 例: 110.wav）が
    両方あれば wav、どちらか無ければ音声合成。クリアは lap2firebase と同じく「バンド_clear.wav」だけ。
    周回ラップで lap_count を渡すと、続けて周回数（lap3.wav）とトータル（total35.wav）も鳴らす
    （lap2firebase の totalLapAnnounce と同じ。その wav が無ければ、その部分だけ省く）。
    周回ラップで best なら、最後に「ベストラップ更新」（newbest.wav、音声合成では best_text）。"""
    who = band if key == "clear_text" else name
    if wav_dir and name_ok(who):
        if key == "clear_text":
            files = [os.path.join(wav_dir, f"{band}_clear.wav")]
        else:
            after = f"{lap_tenths(lap)}.wav" if key == "lap_text" else WAV_AFTER_NAME[key]
            files = [name_wav(name, pilot_dir, wav_dir) or os.path.join(pilot_dir or wav_dir, f"{name}.wav"),
                     os.path.join(wav_dir, after)]
        if all(os.path.isfile(f) for f in files):
            if key == "lap_text" and lap_count is not None:
                for extra in (f"lap{lap_count}.wav", f"total{int(total or 0)}.wav"):
                    f = os.path.join(wav_dir, extra)
                    if os.path.isfile(f):
                        files.append(f)
                    else:
                        log.info("%s not found, skipping that part", extra)
            if key == "lap_text" and best:
                f = os.path.join(wav_dir, BEST_WAV)
                if os.path.isfile(f):
                    files.append(f)
                    return [("file", f) for f in files]
                log.info("%s not found, using speech synthesis for it", BEST_WAV)
                return [("file", f) for f in files] + [("tts", speech_text(speech_cfg, band, name, "best_text"))]
            return [("file", f) for f in files]
        missing = [os.path.basename(f) for f in files if not os.path.isfile(f)]
        log.info("%s not found in %s, using speech synthesis", ", ".join(missing), wav_dir)
    text = speech_text(speech_cfg, band, name, key, lap)
    if key == "lap_text" and lap_count is not None:
        text += (", " if speech_cfg.get("language") == "en" else "、") + speech_text(speech_cfg, band, name, "total_text", lap, lap_count, total)
    if key == "lap_text" and best:
        text += (", " if speech_cfg.get("language") == "en" else "、") + speech_text(speech_cfg, band, name, "best_text")
    return [("tts", text)]


def wav_seconds(path: str) -> float:
    try:
        with wave.open(path) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 1.0


def join_wavs(files: list[str], out_path: str) -> float | None:
    """同じ形式の wav をつないで1つのファイルにし、長さ（秒）を返す。形式が違えば None。
    「名前.wav → entry.wav」を1回で鳴らし、名前が途中で切れたり間が空いたりしないようにする。"""
    params, chunks, frames = None, [], 0
    try:
        for f in files:
            with wave.open(f) as w:
                p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                if params and p != params:
                    return None
                params = p
                chunks.append(w.readframes(w.getnframes()))
                frames += w.getnframes()
        with wave.open(out_path, "wb") as w:
            w.setnchannels(params[0])
            w.setsampwidth(params[1])
            w.setframerate(params[2])
            w.writeframes(b"".join(chunks))
    except Exception as e:
        log.debug("Cannot join wavs: %s", e)
        return None
    return frames / float(params[2])


class SoundPlayer:
    def __init__(self, config):
        self.config = config
        self._fx: queue.Queue = queue.Queue()       # lap 音・errorlap 音（優先）
        self._say: queue.Queue = queue.Queue()      # アナウンス
        self._wake = threading.Event()
        self._voice = None          # SAPI は使うスレッドで作る。作れなければ False
        self._voice_want = None     # いま選んでいる声（speech.voice）
        self._tmpdir = tempfile.mkdtemp(prefix="d-practice-voice-")    # つないだ wav の置き場
        self._tmp_n = 0
        self._gen: queue.Queue = queue.Queue()      # 名前.wav を作ってから鳴らすアナウンス
        self._gen_voice = None      # 名前.wav を作るスレッドの SAPI。作れなければ False
        self._gen_voice_want = None
        threading.Thread(target=self._run, name="sound", daemon=True).start()
        threading.Thread(target=self._gen_run, name="name-wav", daemon=True).start()

    def play(self, kind: str) -> None:
        """kind: "lap"（ラップ確定）、"best"（ベストラップを更新したラップ確定。lap の代わり）、
        "error"（minLap 以下で計測しなかった通過）、"boot"（計測開始）"""
        cfg = self.config.data.get("sound", {})
        if not cfg.get("enabled", True):
            return
        if self._fx.qsize() > 4:    # 一度に大量に溜まったら古い音は捨てる
            return
        path = cfg.get(kind, "./sound/bestlap.wav" if kind == "best" else "")
        self._fx.put(self.config.resolve_path(path) if path else "")
        self._wake.set()

    def announce(self, band: str, name: str, key: str, cfg: dict | None = None,
                 force: bool = False, lap: float | None = None, lap_count: int | None = None,
                 total: float | None = None, best: bool = False) -> None:
        """アナウンスする。key は "text"（QR を読んだとき）、"start_text"（起点の通過）、"lap_text"（周回ラップ）、
        "clear_text"（通過が無くリセットしたとき）。
        cfg は speech の設定（None なら config.yaml の値）。force は設定画面の「試す」用。
        lap_count / total を渡すと、周回ラップの後に周回数とトータルも読む。best なら最後に「ベストラップ更新」。"""
        cfg = effective_speech(self.config.data.get("speech", {}) if cfg is None else cfg)
        if (not cfg.get("enabled", True) and not force) or not name:
            return
        if self._say.qsize() > 4:
            return
        wav_dir = cfg.get("wav_dir", VOICE_DIR)    # 英語は effective_speech で ""（wav を使わない）。テストでは差し替える
        wav_dir = self.config.resolve_path(wav_dir) if wav_dir else ""
        pilot_dir = self.config.resolve_path(cfg["pilot_dir"]) if cfg.get("pilot_dir") else ""
        item = (cfg, band, name, key, wav_dir, pilot_dir, lap, lap_count, total, best)
        # 名前.wav が無ければ、Windows の声で pilot_dir に作ってから鳴らす（作るのは別スレッド。lap 音は待たせない）。
        # 次からはその wav を使う
        # 日本語音声（wav_dir）が無ければ entry.wav なども無く音声合成で読むので、作らない
        if key != "clear_text" and wav_dir and os.path.isdir(wav_dir) and pilot_dir \
                and name_ok(name) \
                and not name_wav(name, pilot_dir, wav_dir):
            self._gen.put(item)
            return
        self._enqueue(*item)

    def _enqueue(self, cfg, band, name, key, wav_dir, pilot_dir, lap, lap_count, total, best=False):
        parts = announce_parts(cfg, band, name, key, wav_dir, lap, lap_count, total, pilot_dir, best)
        self._say.put((parts, str(cfg.get("voice", "") or ""), str(cfg.get("language", "ja"))))
        self._wake.set()

    def _gen_run(self):
        """名前.wav を pilot_dir に作ってからアナウンスを鳴らす（作れなければ音声合成で文を読む）。
        VOICEVOX が起動していれば VOICEVOX で、起動していなければ Windows の声（SAPI）で作る。
        名前.wav が無いパイロットのアナウンスはここを順に通るので、そのパイロットの中で順番は入れ替わらない。"""
        while True:
            item = self._gen.get()
            cfg, band, name, key, wav_dir, pilot_dir = item[:6]
            try:
                if not name_wav(name, pilot_dir, wav_dir) and not make_voicevox_name_wav(name, pilot_dir):
                    make_sapi_name_wav(name, pilot_dir,
                                       self._get_gen_voice(str(cfg.get("voice", "") or ""),
                                                           str(cfg.get("language", "ja"))))
            except Exception as e:
                log.warning("Cannot create the name wav: %s", e)
            self._enqueue(*item)

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            while True:
                try:
                    try:
                        self._play(self._fx.get_nowait())
                        continue
                    except queue.Empty:
                        pass
                    try:
                        self._say_parts(*self._say.get_nowait())
                        continue
                    except queue.Empty:
                        break
                except Exception as e:
                    log.warning("Cannot play sound: %s", e)

    def _say_parts(self, parts: list, want: str, language: str = "ja"):
        """アナウンスを順に鳴らす。途中で lap 音が来たら止めて、残りは捨てる。"""
        files = [arg for kind, arg in parts if kind == "file"]
        if len(files) > 1 and len(files) == len(parts):
            self._tmp_n ^= 1    # 2つのファイルを交互に使う（直前に鳴らしたファイルを書き換えないように）
            joined = os.path.join(self._tmpdir, f"announce{self._tmp_n}.wav")
            if join_wavs(files, joined) is not None:
                parts = [("file", joined)]
        for kind, arg in parts:
            if not self._fx.empty():
                return
            if kind == "file":
                if not sys.platform.startswith("win"):
                    self._play(arg)
                    continue
                import winsound
                winsound.PlaySound(arg, winsound.SND_FILENAME | winsound.SND_ASYNC)
                end = time.monotonic() + wav_seconds(arg) + PLAY_MARGIN_SEC
                while time.monotonic() < end:
                    if not self._fx.empty():
                        winsound.PlaySound(None, 0)     # 止める
                        return
                    time.sleep(0.02)
            else:
                voice = self._get_voice(want, language)
                if not voice:
                    continue
                voice.Speak(arg, SVSF_ASYNC)
                while not voice.WaitUntilDone(20):
                    if not self._fx.empty():
                        voice.Speak("", SVSF_ASYNC | SVSF_PURGE)
                        return

    def _get_voice(self, want: str, language: str = "ja"):
        if self._voice is None:
            self._voice = self._create_voice()
        if self._voice and (want, language) != self._voice_want:
            self._select_voice(self._voice, want, language)
            self._voice_want = (want, language)
        return self._voice

    def _get_gen_voice(self, want: str, language: str = "ja"):
        """名前.wav を作るスレッドの SAPI（COM はスレッドごとに作る）。"""
        if self._gen_voice is None:
            self._gen_voice = self._create_voice()
        if self._gen_voice and (want, language) != self._gen_voice_want:
            self._select_voice(self._gen_voice, want, language)
            self._gen_voice_want = (want, language)
        return self._gen_voice

    @staticmethod
    def _create_voice():
        """Windows の音声合成（SAPI）。"""
        try:
            import comtypes
            import comtypes.client
            comtypes.CoInitialize()
            return comtypes.client.CreateObject("SAPI.SpVoice")
        except Exception as e:
            log.warning("Speech is not available (needs comtypes and Windows speech synthesis): %s", e)
            return False

    @staticmethod
    def _select_voice(voice, want: str, language: str = "ja"):
        """声を選ぶ。want が空なら、アナウンスの言語の声。"""
        lists = [voice.GetVoices()] if want else \
            [voice.GetVoices(f"Language={c}") for c in VOICE_LANG.get(language, VOICE_LANG["ja"])]
        for voices in lists:
            for i in range(voices.Count):
                token = voices.Item(i)
                if not want or want.lower() in token.GetDescription().lower():
                    voice.Voice = token
                    log.info("Speech voice: %s", token.GetDescription())
                    return
        log.warning("Speech voice %s not found, using the current voice", want or f"({language})")

    @staticmethod
    def _play(path: str):
        if sys.platform.startswith("win"):
            import winsound
            if path and os.path.exists(path):
                winsound.PlaySound(path, winsound.SND_FILENAME)
            else:
                winsound.Beep(1500, 150)        # wav が無ければビープ音
        else:
            try:
                from playsound import playsound
                if path and os.path.exists(path):
                    playsound(path)
            except ImportError:
                print("\a", end="", flush=True)


def list_voices() -> list[str]:
    """インストールされている声の名前（設定画面の選択肢用）。使えなければ空。"""
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        voices = comtypes.client.CreateObject("SAPI.SpVoice").GetVoices()
        return [voices.Item(i).GetDescription() for i in range(voices.Count)]
    except Exception as e:
        log.debug("Cannot list voices: %s", e)
        return []
