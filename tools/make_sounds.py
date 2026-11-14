"""効果音（sound/lap.wav・bestlap.wav・errorlap.wav・boot.wav）を正弦波・矩形波から作る。

    python tools/make_sounds.py              # sound/ に上書き
    python tools/make_sounds.py --out tmp/   # 別のフォルダに

どれもこのスクリプトで一から作る音なので、ほかの音源は使っていない（このアプリと同じ扱いで使える）。
音の高さや長さを変えたいときは、下の lap() / best() / error() / boot() を直して作り直す。
形式は 16bit モノラル 44.1kHz。ラップの音が遅れないよう、頭に無音は入れない。
"""
import argparse
import os
import wave

import numpy as np

SR = 44100


def tone(freq: float, dur: float, harmonics=(1.0,), attack=0.005, decay=None, square=False,
         hold: float = 0.0) -> np.ndarray:
    """1音。harmonics は倍音の強さ（基音から）。decay（秒）を指定すると指数で減衰する（hold 秒は減衰させない）。
    square なら矩形波（角を少し丸める）。始めと終わりは短くフェードして、プツッという音を防ぐ。"""
    t = np.arange(int(SR * dur)) / SR
    if square:
        x = np.tanh(6 * np.sin(2 * np.pi * freq * t))
    else:
        x = sum(a * np.sin(2 * np.pi * freq * (k + 1) * t) for k, a in enumerate(harmonics))
    env = np.minimum(1.0, t / attack) if attack > 0 else np.ones_like(t)
    if decay:
        env = env * np.exp(-np.maximum(0.0, t - hold) / decay)
    fade = min(len(t), int(SR * 0.01))
    env[-fade:] *= np.linspace(1, 0, fade)
    return x * env


def place(parts, length: float) -> np.ndarray:
    """parts = [(開始秒, 音)] を重ねて length 秒の音にする。"""
    out = np.zeros(int(SR * length))
    for start, x in parts:
        i = int(SR * start)
        n = min(len(x), len(out) - i)
        out[i:i + n] += x[:n]
    return out


def lap() -> np.ndarray:
    """ラップ確定: 上がる2音の「ピコン」（B5 → E6）。"""
    bell = (1.0, 0.25, 0.08)
    return place([(0.00, tone(988, 0.09, bell, decay=0.12)),
                  (0.08, tone(1319, 0.22, bell, decay=0.09))], 0.30)


def best() -> np.ndarray:
    """ベストラップ更新のラップ確定（lap.wav の代わり）: 「ピッ・ピッ・ピッ・ポーン」。
    A5 の短い音を 0.1 秒おきに3回、続けて D6 を少し伸ばして減衰させる（sound/sample_bestLap.wav の音の並びを参考に、
    ここで一から作る）。最後の音は倍音を強めて、前の3つより明るく大きく聞こえるようにする。"""
    beep = tone(880, 0.075, (1.0, 0.33, 0.08), attack=0.003)
    last = tone(1175, 0.26, (1.0, 0.57, 0.13), attack=0.003, decay=0.05, hold=0.1)
    return place([(0.00, beep * 0.7), (0.10, beep * 0.7), (0.20, beep * 0.7), (0.30, last)], 0.56)


def error() -> np.ndarray:
    """minLap 以下の通過: 低い「ブブッ」（220Hz の矩形波を2回）。"""
    buzz = tone(220, 0.09, square=True, attack=0.003)
    return place([(0.00, buzz), (0.13, buzz)], 0.24)


def boot() -> np.ndarray:
    """計測開始: 上がる和音（G5・B5・D6・G6）を順に鳴らし、最後の音を伸ばす。"""
    bell = (1.0, 0.3, 0.1)
    notes = [784, 988, 1175, 1568]
    parts = [(0.11 * k, tone(f, 0.5, bell, decay=0.18)) for k, f in enumerate(notes[:-1])]
    parts.append((0.33, tone(notes[-1], 0.9, bell, decay=0.3)))
    return place(parts, 1.25)


def write(path: str, x: np.ndarray, peak: float = 0.7) -> None:
    """最大が peak になるようにそろえて 16bit で書く。"""
    x = x / np.abs(x).max() * peak
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sound"))
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for name, fn, peak in (("lap.wav", lap, 0.7), ("bestlap.wav", best, 0.7), ("errorlap.wav", error, 0.5),
                           ("boot.wav", boot, 0.6)):
        path = os.path.join(a.out, name)
        write(path, fn(), peak)
        print(path)


if __name__ == "__main__":
    main()
