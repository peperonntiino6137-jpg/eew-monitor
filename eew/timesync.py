"""NICT (情報通信研究機構) の日本標準時と同期する。

日本標準時を配信する公式 NTP サーバ ntp.nict.jp に SNTP で問い合わせ、
往復遅延を補正した PC 時計とのオフセットを models.now_jst に反映する。
カウントダウン・地図の時計・波円描画のすべてがこの補正済み時刻を使う。

NICT への負荷を避けるため同期は起動時 + 1時間ごとのみ。
"""
from __future__ import annotations

import asyncio
import socket
import struct
import time
from datetime import timedelta

from . import display
from .models import set_clock_offset

NTP_SERVERS = ["ntp.nict.jp", "time.windows.com"]  # NICT 優先、予備は Windows 既定
NTP_DELTA = 2208988800  # NTP 元期 (1900) と UNIX 元期 (1970) の差
SYNC_INTERVAL = 3600    # 1時間
SAMPLES = 3             # 複数回測って往復遅延最小のサンプルを採用


def _sntp_query(server: str, timeout: float = 3.0) -> tuple[float, float]:
    """SNTP 1 回問い合わせ。(オフセット秒, 往復遅延秒) を返す。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        packet = b"\x1b" + 47 * b"\0"  # LI=0, VN=3, Mode=3 (client)
        t1 = time.time()
        s.sendto(packet, (server, 123))
        data, _ = s.recvfrom(512)
        t4 = time.time()
    if len(data) < 48:
        raise ValueError("short NTP packet")

    def ts(off: int) -> float:
        sec, frac = struct.unpack("!II", data[off:off + 8])
        return sec - NTP_DELTA + frac / 2**32

    t2 = ts(32)  # サーバ受信時刻
    t3 = ts(40)  # サーバ送信時刻
    offset = ((t2 - t1) + (t3 - t4)) / 2
    delay = (t4 - t1) - (t3 - t2)
    return offset, delay


def _best_offset() -> tuple[float, str] | None:
    """複数サンプルから往復遅延最小のオフセットを選ぶ (ブロッキング)。"""
    for server in NTP_SERVERS:
        results = []
        for _ in range(SAMPLES):
            try:
                results.append(_sntp_query(server))
            except (OSError, ValueError):
                continue
        if results:
            offset, _ = min(results, key=lambda r: r[1])
            return offset, server
    return None


async def run() -> None:
    first = True
    while True:
        result = await asyncio.to_thread(_best_offset)
        if result is not None:
            sec, server = result
            set_clock_offset(timedelta(seconds=sec))
            color = display.GREEN if abs(sec) < 5 else display.YELLOW
            msg = f"時刻同期 ({server}): PC時計との差 {sec:+.3f}秒"
            if abs(sec) >= 5:
                msg += " ※PC時計が大きくズレています (Windows の時刻同期を推奨)"
            if first or abs(sec) >= 1:
                display.log_system(msg, color)
            first = False
        elif first:
            display.log_system(
                "時刻同期失敗 (NICT): PC時計をそのまま使用します", display.YELLOW)
            first = False
        await asyncio.sleep(SYNC_INTERVAL)
