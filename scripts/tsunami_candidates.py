"""津波音の候補を3案生成する (聴き比べ用)。

実行:  python scripts/tsunami_candidates.py        (生成のみ)
       python scripts/tsunami_candidates.py play   (生成して順に再生)

出力: sounds/candidates/tsunami_1.wav 〜 tsunami_3.wav (+mp3)
確定済みの警報 (鋭い加速スイープ) / 予報 (上下サイレン) と同じスイープ言語で設計。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_sounds import RATE, silence, siren, sustained, sweep, save as _save  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "sounds" / "candidates"


def save(name: str, samples: list[float]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    import struct
    import wave
    path = OUT / name
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(
            struct.pack("<h", max(-32767, min(32767, int(s * 32767))))
            for s in samples))
    print(f"generated: {path} ({len(samples) / RATE:.1f}s)")


def tsunami_1() -> list[float]:
    """案1: 予報と同型のサイレンを少し低く・鋭い音色で・3往復。
    予報との差は音域と長さのみ。家族性が最も強い。"""
    return siren(440, 880, half_cycle=0.6, cycles=3, vol=0.9)


def tsunami_2() -> list[float]:
    """案2: 警報と同型の上昇スイープを低め・一定間隔で繰り返し、
    最後に長い上昇。警報の「弟分」で、重さは音域の低さで表現。"""
    s: list[float] = []
    for _ in range(4):
        s += sweep(500, 1000, 0.4, vol=0.85)
        s += silence(0.16)
    s += sweep(500, 1500, 1.0, vol=0.9)
    return s


def tsunami_3() -> list[float]:
    """案3: 広い音域 (330→1000Hz) をゆっくり大きくうねるサイレン。
    防災無線的な重厚さ。上下の振れ幅で「大きな水のうねり」を連想させる。"""
    s = siren(330, 1000, half_cycle=0.9, cycles=2, vol=0.9)
    s += sustained(660, 0.8, vol=0.9, rise_to=1000)
    return s


if __name__ == "__main__":
    builders = {"1": tsunami_1, "2": tsunami_2, "3": tsunami_3}
    files = {}
    for k, fn in builders.items():
        name = f"tsunami_{k}.wav"
        save(name, fn())
        files[k] = OUT / name

    if len(sys.argv) > 1 and sys.argv[1] == "play":
        import ctypes
        mci = ctypes.windll.winmm.mciSendStringW
        labels = {"1": "予報系サイレン低め3往復", "2": "警報系スイープ低め+長い上昇",
                  "3": "広い音域の重厚うねり"}
        for k, path in files.items():
            print(f"--- 案{k}: {labels[k]} ---")
            mci(f'open "{path}" alias t{k}', None, 0, None)
            mci(f"play t{k} wait", None, 0, None)
            mci(f"close t{k}", None, 0, None)
            time.sleep(0.8)
        print("再生終了")
