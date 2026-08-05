"""sounds/ 以下の WAV を MP3 に変換する (聴き比べ用)。

実行:  python scripts/wav_to_mp3.py
出力:  sounds/mp3/*.mp3
"""
from __future__ import annotations

import wave
from pathlib import Path

import lameenc

SOUNDS = Path(__file__).resolve().parent.parent / "sounds"
OUT = SOUNDS / "mp3"


def convert(src: Path, dst: Path) -> None:
    with wave.open(str(src), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        pcm = w.readframes(w.getnframes())
    enc = lameenc.Encoder()
    enc.set_bit_rate(192)
    enc.set_in_sample_rate(rate)
    enc.set_channels(ch)
    enc.set_quality(2)
    mp3 = enc.encode(pcm) + enc.flush()
    dst.write_bytes(mp3)
    print(f"converted: {dst.name} ({len(mp3) // 1024} KB)")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for src in sorted(SOUNDS.glob("*.wav")):
        convert(src, OUT / (src.stem + ".mp3"))
    for src in sorted((SOUNDS / "candidates").glob("*.wav")):
        convert(src, OUT / (src.stem + ".mp3"))
