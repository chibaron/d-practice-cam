"""日本語のアナウンスの声（sound/voice/ の wav）を VOICEVOX で作る。

    （VOICEVOX を起動しておく）
    python tools/make_voice.py              # sound/voice/ に無い wav だけ作る
    python tools/make_voice.py --force      # 全部作り直す
    python tools/make_voice.py --out tmp/   # 別のフォルダに

声は四国めたん ノーマル（speaker 2）、話速 1.2、16bit モノラル 24kHz（app.sound.voicevox_synth と同じ。
lap2firebase の createwav.py と同じ作り方）。wav はリポジトリに入れず、公開用の日本語版 zip にだけ入れる。
作った音声を使うときは「VOICEVOX:四国めたん」のクレジットを書く（README と zip の README.txt に書いてある）。

作るもの（アプリが使う名前。app.sound.announce_parts）:
    50.wav〜599.wav     ラップのタイム（0.1秒単位。「5.0」〜「59.9」。60秒以上はアナウンスしない）
    lap1.wav〜lap99.wav 周回数（「1周」）
    total0.wav〜total599.wav  トータル（「35秒」「1分5秒」）
    entry.wav・start.wav・newbest.wav  「エントリー」「スタート」「ベストラップ更新」
    バンド名.wav・バンド名_clear.wav   QR を読んでいない ch の名前（「F1」）と「F1、クリア」（設定画面で選べるバンド全部）
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import sound                                   # noqa: E402
from app.settings_dialog import BAND_CHOICES            # noqa: E402

MIN_TENTHS = 50         # 5.0秒より短いラップの wav は作らない（lap2firebase と同じ。無ければ音声合成で読む）


def voice_items() -> list[tuple[str, str]]:
    """(ファイル名, 読ませる文) のリスト。"""
    items = [(f"{t}.wav", f"{t / 10:.1f}") for t in range(MIN_TENTHS, 600)]
    items += [(f"lap{n}.wav", f"{n}周") for n in range(1, 100)]
    items += [(f"total{s}.wav", f"{s // 60}分{s % 60}秒" if s >= 60 else f"{s}秒") for s in range(600)]
    items += [("entry.wav", "エントリー"), ("start.wav", "スタート"), (sound.BEST_WAV, "ベストラップ更新")]
    for band in BAND_CHOICES:
        items += [(f"{band}.wav", band), (f"{band}_clear.wav", f"{band}、クリア")]
    return items


def make_voice(out: str, force: bool = False, log=print) -> tuple[int, int]:
    """out に wav を作る。(作った数, 作れなかった数)。VOICEVOX が起動していなければ (0, -1)。"""
    if not sound.voicevox_synth("テスト"):
        return 0, -1
    os.makedirs(out, exist_ok=True)
    made = failed = 0
    items = [(f, s) for f, s in voice_items() if force or not os.path.exists(os.path.join(out, f))]
    for i, (fn, text) in enumerate(items):
        data = sound.voicevox_synth(text)
        if not data:
            failed += 1
            log(f"作れませんでした: {fn}（{text}）")
            continue
        path = os.path.join(out, fn)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        made += 1
        if made % 100 == 0:
            log(f"{i + 1}/{len(items)}")
    return made, failed


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=os.path.join(ROOT, "sound", "voice"))
    p.add_argument("--force", action="store_true", help="今ある wav も作り直す")
    args = p.parse_args(argv)
    made, failed = make_voice(args.out, args.force)
    if failed < 0:
        sys.exit(f"VOICEVOX に繋がりません。VOICEVOX を起動してから実行してください（{sound.VOICEVOX_URL}）")
    print(f"{args.out} に {made} 個作りました" + (f"（{failed} 個は作れませんでした）" if failed else ""))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
