"""通知音の設計方針サンプルを4方向で生成する (方向性の聞き比べ用)。

実行:  python scripts/sound_directions.py        (生成のみ)
       python scripts/sound_directions.py play   (生成して順に再生)

出力: sounds/candidates/direction_a.wav 〜 direction_d.wav
いずれも「予報」相当の中程度の緊急度で作成 (方向性が決まったら全音種に展開する)。
"""
from __future__ import annotations

import math
import struct
import sys
import time
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "sounds" / "candidates"


def save(name: str, samples: list[float]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767))))
            for s in samples))
    print(f"generated: {path} ({len(samples) / RATE:.1f}s)")


def silence(dur: float) -> list[float]:
    return [0.0] * int(RATE * dur)


# ---------------------------------------------------------------- A: 原曲追従
def direction_a() -> list[float]:
    """A. 原曲追従型: 警報音源の 5600/3000Hz 系の鋭い音色を流用し、
    緊急度に応じて音程・回数をスケール。家族としての統一感は最強。"""
    out: list[float] = []
    for _ in range(2):
        dur = 0.45
        n = int(RATE * dur)
        for i in range(n):
            t = i / RATE
            s = (math.sin(2 * math.pi * 4480 * t)
                 + 0.8 * math.sin(2 * math.pi * 2400 * t)
                 + 0.4 * math.sin(2 * math.pi * 880 * t)) / 2.2
            env = min(t / 0.006, 1.0, max((dur - t) / 0.05, 0.0))
            out.append(s * env * 0.75)
        out += silence(0.14)
    return out


# ------------------------------------------------------------ B: 電子チャイム
def direction_b() -> list[float]:
    """B. 放送チャイム型: 明確な音程の二音モチーフ (下降形) を繰り返す。
    緊急放送らしい「聞き取りやすく記憶に残る」音。刺さる高周波は控えめ。"""
    out: list[float] = []
    for _ in range(2):
        for f in (988, 784):  # B5 -> G5 の下降二音
            dur = 0.32
            n = int(RATE * dur)
            for i in range(n):
                t = i / RATE
                # 鐘寄りの音色: 倍音が時間とともに減衰
                decay = math.exp(-t * 6)
                s = (math.sin(2 * math.pi * f * t)
                     + 0.5 * decay * math.sin(2 * math.pi * 2 * f * t)
                     + 0.25 * decay * math.sin(2 * math.pi * 3 * f * t)) / 1.6
                env = min(t / 0.004, 1.0) * math.exp(-t * 2.2)
                out.append(s * env * 0.85)
        out += silence(0.12)
    return out


# ---------------------------------------------------------------- C: サイレン
def direction_c() -> list[float]:
    """C. サイレン型: 連続的な周波数スイープの上下。
    「音程の変化」で切迫感を出す。離れていても気づきやすい。"""
    out: list[float] = []
    phase = 0.0
    for cycle in range(2):
        for updown in (0, 1):
            dur = 0.55
            n = int(RATE * dur)
            for i in range(n):
                t = i / RATE
                p = t / dur
                f = 620 + (1240 - 620) * (p if updown == 0 else 1 - p)
                phase += 2 * math.pi * f / RATE
                s = (math.sin(phase) + 0.5 * math.sin(2 * phase)
                     + 0.3 * math.sin(3 * phase)) / 1.65
                out.append(s * 0.8)
    # 終端をフェード
    for i in range(int(RATE * 0.06)):
        out[-i - 1] *= i / (RATE * 0.06)
    return out


# -------------------------------------------------------------- D: 加速パルス
def direction_d() -> list[float]:
    """D. 加速パルス型: 一定音程の短いパルスが徐々に速くなる。
    リズムの加速で「迫ってくる」感覚を出す。音自体は耳に優しめ。"""
    out: list[float] = []
    gap = 0.26
    total = 0.0
    while total < 2.0:
        dur = 0.09
        n = int(RATE * dur)
        for i in range(n):
            t = i / RATE
            s = (math.sin(2 * math.pi * 1047 * t)
                 + 0.5 * math.sin(2 * math.pi * 2094 * t)) / 1.5
            env = min(t / 0.003, 1.0) * max((dur - t) / 0.03, 0.0) if t > dur - 0.03 \
                else min(t / 0.003, 1.0)
            out.append(s * env * 0.8)
        out += silence(gap)
        total += dur + gap
        gap = max(gap * 0.82, 0.07)  # 徐々に加速
    return out


BUILDERS = {"A": direction_a, "B": direction_b, "C": direction_c, "D": direction_d}
LABELS = {
    "A": "原曲追従型 (警報音源と同じ鋭い音色)",
    "B": "放送チャイム型 (明確な二音モチーフ)",
    "C": "サイレン型 (連続スイープの上下)",
    "D": "加速パルス型 (リズムが速くなり迫る)",
}

if __name__ == "__main__":
    files = {}
    for key, fn in BUILDERS.items():
        name = f"direction_{key.lower()}.wav"
        save(name, fn())
        files[key] = OUT / name

    if len(sys.argv) > 1 and sys.argv[1] == "play":
        import ctypes
        mci = ctypes.windll.winmm.mciSendStringW
        for key, path in files.items():
            print(f"--- {key}: {LABELS[key]} ---")
            mci(f'open "{path}" alias d{key}', None, 0, None)
            mci(f"play d{key} wait", None, 0, None)
            mci(f"close d{key}", None, 0, None)
            time.sleep(0.8)
        print("再生終了")
