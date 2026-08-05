"""強震モニタ (防災科研 新強震モニタ) クライアント。

強震モニタのデータは実時刻から 1〜2 秒遅れて公開されるため、
latest.json でサーバ時刻とのオフセットを求めてからポーリングする。
EEW JSON と画像解析 (kmoni_image) で httpx クライアントと時刻同期を共有し、
リクエストを最小限に保つ。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Callable

import httpx

from .. import display
from ..models import JST, EEWEvent, parse_jst

BASE = "http://www.kmoni.bosai.go.jp"
LATEST_URL = f"{BASE}/webservice/server/pros/latest.json"
EEW_URL = f"{BASE}/webservice/hypo/eew/{{ts}}.json"
IMAGE_URL = f"{BASE}/data/map_img/RealTimeImg/jma_s/{{ymd}}/{{ts}}.jma_s.gif"
SOURCE = "kmoni"

POLL_INTERVAL = 1.0
RESYNC_INTERVAL = 300      # 5 分ごとに時刻オフセット再同期
MIN_SYNC_GAP = 5.0         # 再同期の最短間隔 (連打防止)
TIME_JUMP_THRESHOLD = 5.0  # スリープ復帰検知 (秒)


class KmoniClient:
    """時刻オフセット同期と HTTP クライアントの共有。"""

    def __init__(self):
        self.http = httpx.AsyncClient(timeout=5)
        self.offset: timedelta | None = None
        self._last_sync: float = -1e9

    async def sync(self, force: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if not force and self.offset is not None and now - self._last_sync < RESYNC_INTERVAL:
            return True
        if now - self._last_sync < MIN_SYNC_GAP:
            return self.offset is not None
        self._last_sync = now
        try:
            r = await self.http.get(LATEST_URL)
            r.raise_for_status()
            latest = parse_jst(r.json().get("latest_time", ""), "%Y/%m/%d %H:%M:%S")
            if latest is None:
                return self.offset is not None
            first = self.offset is None
            self.offset = latest - datetime.now(JST)
            if first:
                display.log_system(
                    f"強震モニタ: 時刻同期 (オフセット {self.offset.total_seconds():+.1f}秒)",
                    display.GREEN)
            return True
        except Exception:
            return self.offset is not None

    def target(self) -> datetime | None:
        """現在ポーリングすべきデータ時刻。"""
        if self.offset is None:
            return None
        return datetime.now(JST) + self.offset

    async def close(self) -> None:
        await self.http.aclose()


def _parse(data: dict) -> EEWEvent | None:
    result = data.get("result") or {}
    if result.get("status") != "success":
        return None
    if result.get("message"):  # "データがありません" など
        return None
    report_id = str(data.get("report_id") or "")
    if not report_id:
        return None

    def _f(key):
        v = data.get(key)
        try:
            return float(v) if v not in (None, "", "/") else None
        except (ValueError, TypeError):
            return None

    depth = None
    d = str(data.get("depth") or "").replace("km", "").strip()
    if d.isdigit():
        depth = int(d)

    try:
        serial = int(data.get("report_num") or 0)
    except (ValueError, TypeError):
        serial = 0

    intensity = str(data.get("calcintensity") or "").strip()
    if intensity in ("不明", "/"):
        intensity = ""

    return EEWEvent(
        source=SOURCE,
        event_id=report_id,
        serial=serial,
        hypocenter=str(data.get("region_name") or ""),
        magnitude=_f("magunitude"),  # kmoni 側もスペルが magunitude
        depth_km=depth,
        max_intensity=intensity,
        latitude=_f("latitude"),
        longitude=_f("longitude"),
        origin_time=parse_jst(str(data.get("origin_time", "")), "%Y%m%d%H%M%S"),
        announced_time=parse_jst(str(data.get("report_time", "")), "%Y/%m/%d %H:%M:%S"),
        is_warn=str(data.get("alertflg") or "") == "警報",
        is_final=str(data.get("is_final")).lower() == "true",
        is_cancel=str(data.get("is_cancel")).lower() == "true",
        is_training=str(data.get("is_training")).lower() == "true",
    )


async def run(client: KmoniClient, on_eew: Callable[[EEWEvent], None]) -> None:
    """EEW JSON の 1 秒ポーリング。"""
    loop = asyncio.get_running_loop()
    miss_streak = 0
    prev_tick = loop.time()

    while True:
        # スリープ復帰検知 -> 強制再同期
        tick = loop.time()
        if tick - prev_tick > POLL_INTERVAL + TIME_JUMP_THRESHOLD:
            display.log_system("強震モニタ: 時刻ジャンプ検知、再同期します", display.YELLOW)
            await client.sync(force=True)
        prev_tick = tick

        if not await client.sync(force=miss_streak >= 5):
            display.log_system("強震モニタ: 時刻同期失敗、10秒後に再試行", display.YELLOW)
            await asyncio.sleep(10)
            continue
        if miss_streak >= 5:
            miss_streak = 0

        target = client.target()
        try:
            r = await client.http.get(EEW_URL.format(ts=target.strftime("%Y%m%d%H%M%S")))
            if r.status_code == 200:
                miss_streak = 0
                ev = _parse(r.json())
                if ev:
                    on_eew(ev)
            else:
                miss_streak += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            miss_streak += 1

        await asyncio.sleep(POLL_INTERVAL)
