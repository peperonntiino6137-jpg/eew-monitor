"""Wolfx API (wss://ws-api.wolfx.jp/jma_eew) から JMA 緊急地震速報を受信。

予報・警報の両方が流れる。接続直後に直近の EEW が 1 件送られてくるが、
古いものは Aggregator 側の stale 判定で弾かれる。
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

import websockets

from .. import display
from ..models import EEWEvent, parse_jst

URL = "wss://ws-api.wolfx.jp/jma_eew"
SOURCE = "wolfx"


def _parse(data: dict) -> EEWEvent | None:
    if data.get("type") != "jma_eew":
        return None

    def _f(key):
        v = data.get(key)
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    depth = data.get("Depth")
    try:
        depth = int(depth) if depth is not None else None
    except (ValueError, TypeError):
        depth = None

    warn_areas = []
    for wa in data.get("WarnArea") or []:
        name = wa.get("Chiiki") if isinstance(wa, dict) else None
        if name:
            warn_areas.append(name)

    return EEWEvent(
        source=SOURCE,
        event_id=str(data.get("EventID", "")),
        serial=int(data.get("Serial") or 0),
        hypocenter=data.get("Hypocenter") or "",
        magnitude=_f("Magunitude"),  # Wolfx API 側のスペルが Magunitude
        depth_km=depth,
        max_intensity=str(data.get("MaxIntensity") or ""),
        latitude=_f("Latitude"),
        longitude=_f("Longitude"),
        origin_time=parse_jst(str(data.get("OriginTime", "")), "%Y/%m/%d %H:%M:%S"),
        announced_time=parse_jst(str(data.get("AnnouncedTime", "")), "%Y/%m/%d %H:%M:%S"),
        is_warn=bool(data.get("isWarn")),
        is_final=bool(data.get("isFinal")),
        is_cancel=bool(data.get("isCancel")),
        is_training=bool(data.get("isTraining")),
        warn_areas=warn_areas,
    )


async def run(on_eew: Callable[[EEWEvent], None]) -> None:
    backoff = 1
    while True:
        try:
            async with websockets.connect(URL, ping_interval=30, ping_timeout=15) as ws:
                display.log_system("Wolfx: 接続しました", display.GREEN)
                backoff = 1
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "heartbeat":
                        continue
                    ev = _parse(data)
                    if ev and ev.event_id:
                        on_eew(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            display.log_system(f"Wolfx: 切断 ({type(e).__name__}: {e}) {backoff}秒後に再接続",
                               display.YELLOW)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
