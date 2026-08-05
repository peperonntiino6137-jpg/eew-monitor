"""通知・警報音・表示・予想震度・カウントダウン・地図の動作確認用。

実行:  python -m eew.test_alert            (予報 -> 警報格上げ -> 最終報 のシナリオ)
       python -m eew.test_alert warn       (いきなり警報)
       python -m eew.test_alert map        (地図サーバも起動してブラウザで確認)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

from . import display, location, notifier
from .aggregator import Aggregator
from .main import load_config, save_config
from .mapserver import MapServer
from .models import EEWEvent, now_jst


def _make(cfg: dict, serial: int, *, warn: bool, final: bool = False,
          intensity: str = "4") -> EEWEvent:
    now = now_jst()
    home = cfg.get("home", {})
    # 自宅から少し離れた地点を震源にする (カウントダウンが数十秒になる距離)
    lat = (home.get("latitude") or 34.7) + 1.2
    lon = (home.get("longitude") or 135.5) + 0.9
    return EEWEvent(
        source="test",
        event_id=now.strftime("%Y%m%d") + "990000",  # テスト用の固定ID
        serial=serial,
        hypocenter="テスト震源",
        magnitude=6.8,
        depth_km=10,
        max_intensity=intensity,
        latitude=lat,
        longitude=lon,
        origin_time=now - timedelta(seconds=5),  # 5秒前に発生した想定
        announced_time=now,
        is_warn=warn,
        is_final=final,
        warn_areas=["テスト県北部", "テスト県南部"] if warn else [],
    )


async def amain() -> None:
    cfg = load_config()
    display.banner()
    display.log_system("=== テストモード: 実際の地震ではありません ===", display.YELLOW)
    notifier.configure(cfg)  # カスタム音源

    await location.resolve_home(cfg, save_config)

    mapsrv = None
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "map":
        mapsrv = MapServer(cfg)
        await mapsrv.start()
        display.log_system("ブラウザで地図を確認してください。5秒後に発報します…")
        await asyncio.sleep(5)

    agg = Aggregator(cfg, publish=mapsrv.broadcast if mapsrv else None)

    if mode == "warn":
        agg.handle_eew(_make(cfg, 1, warn=True, intensity="5強"))
        await asyncio.sleep(3)
        agg.handle_eew(_make(cfg, 2, warn=True, final=True, intensity="5強"))
    else:
        display.log_system("第1報: 予報 (震度4)")
        agg.handle_eew(_make(cfg, 1, warn=False, intensity="4"))
        await asyncio.sleep(4)
        display.log_system("第2報: 警報へ格上げ (震度5強)")
        agg.handle_eew(_make(cfg, 2, warn=True, intensity="5強"))
        await asyncio.sleep(4)
        display.log_system("第3報: 最終報")
        agg.handle_eew(_make(cfg, 3, warn=True, final=True, intensity="5強"))

    # カウントダウンの進行を見せる
    wait = 60 if mode == "map" else 30
    display.log_system(f"カウントダウン確認のため {wait}秒 待機…")
    await asyncio.sleep(wait)
    display.log_system("テスト完了")


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nテスト中断", flush=True)


if __name__ == "__main__":
    main()
