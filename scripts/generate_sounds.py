"""スイープ/サイレン系で統一した通知音ファミリーを合成する。

実行:  python scripts/generate_sounds.py
出力:  sounds/warning.wav, forecast.wav, tsunami.wav, info.wav

設計 (ユーザー選定: warning + direction_c の2音を軸に家族化):
  警報:   鋭い上昇スイープが加速しながら繰り返され、最後は長い上昇で突き抜ける
  予報:   上下サイレン (direction_c そのまま。620〜1240Hz の連続スイープ)
  津波:   警報と同型の上昇スイープを低め (500→1000Hz) で繰り返し + 長い上昇
  情報:   短い単発の上昇スイープ x2
共通言語は「周波数が動く音」。緊急度は音程・鋭さ・長さで表現する。
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parent.parent / "sounds"


def save(name: str, samples: list[float]) -> None:
    OUT.mkdir(exist_ok=True)
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


def sweep(f0: float, f1: float, dur: float, vol: float = 0.9,
          sharp: bool = True) -> list[float]:
    """上昇 (または下降) スイープ。sharp で倍音を厚くして刺さる音色に。"""
    n = int(RATE * dur)
    out = []
    phase = 0.0
    for i in range(n):
        t = i / RATE
        f = f0 + (f1 - f0) * (t / dur)
        phase += 2 * math.pi * f / RATE
        if sharp:
            s = (math.sin(phase) + 0.6 * math.sin(2 * phase)
                 + 0.4 * math.sin(3 * phase) + 0.25 * math.sin(4 * phase))
            s /= 2.25
        else:
            s = (math.sin(phase) + 0.5 * math.sin(2 * phase)
                 + 0.3 * math.sin(3 * phase)) / 1.65
        env = min(t / 0.004, 1.0)
        if t > dur - 0.04:
            env *= max((dur - t) / 0.04, 0.0)
        out.append(s * env * vol)
    return out


def siren(f_lo: float, f_hi: float, half_cycle: float, cycles: int,
          vol: float = 0.8) -> list[float]:
    """上下サイレン (direction_c 系)。連続的に f_lo <-> f_hi を往復する。"""
    out: list[float] = []
    phase = 0.0
    for _ in range(cycles):
        for updown in (0, 1):
            n = int(RATE * half_cycle)
            for i in range(n):
                p = i / n
                f = f_lo + (f_hi - f_lo) * (p if updown == 0 else 1 - p)
                phase += 2 * math.pi * f / RATE
                s = (math.sin(phase) + 0.5 * math.sin(2 * phase)
                     + 0.3 * math.sin(3 * phase)) / 1.65
                out.append(s * vol)
    # 終端フェード
    fade = int(RATE * 0.06)
    for i in range(fade):
        out[-i - 1] *= i / fade
    return out


def sustained(freq: float, dur: float, vol: float = 0.85,
              rise_to: float | None = None) -> list[float]:
    """連続音 (終端表現)。"""
    n = int(RATE * dur)
    out = []
    phase = 0.0
    for i in range(n):
        t = i / RATE
        f = freq if rise_to is None else freq + (rise_to - freq) * (t / dur)
        phase += 2 * math.pi * f / RATE
        s = (math.sin(phase) + 0.5 * math.sin(2 * phase)
             + 0.3 * math.sin(3 * phase)) / 1.65
        env = min(t / 0.01, 1.0)
        if t > dur - 0.12:
            env *= max((dur - t) / 0.12, 0.0)
        out.append(s * env * vol)
    return out


def warning() -> list[float]:
    """警報: 鋭い上昇スイープが加速しながら繰り返され、
    最後は長い上昇で突き抜ける (約3.4秒)。"""
    s: list[float] = []
    gap = 0.18
    for _ in range(6):
        s += sweep(900, 1800, 0.28, vol=0.9)
        s += silence(gap)
        gap = max(gap * 0.68, 0.04)  # 加速
    s += sweep(900, 2400, 0.85, vol=0.95)
    s += sustained(2400, 0.35, vol=0.9)
    return s


def forecast() -> list[float]:
    """予報: 上下サイレン (direction_c そのまま、約2.2秒)。"""
    return siren(620, 1240, half_cycle=0.55, cycles=2, vol=0.8)


def tsunami() -> list[float]:
    """津波: 警報と同型の上昇スイープを低めで繰り返し、最後に長い上昇 (約3.2秒)。
    警報の「弟分」。重さは音域の低さで表現する (ユーザー選定: 案2)。"""
    s: list[float] = []
    for _ in range(4):
        s += sweep(500, 1000, 0.4, vol=0.85)
        s += silence(0.16)
    s += sweep(500, 1500, 1.0, vol=0.9)
    return s


def info() -> list[float]:
    """情報・揺れ検知: 短い単発の上昇スイープ x2 (約0.6秒)。"""
    s = sweep(620, 1240, 0.22, vol=0.6, sharp=False)
    s += silence(0.12)
    s += sweep(620, 1240, 0.22, vol=0.6, sharp=False)
    return s


if __name__ == "__main__":
    save("warning.wav", warning())
    save("forecast.wav", forecast())
    save("tsunami.wav", tsunami())
    save("info.wav", info())
