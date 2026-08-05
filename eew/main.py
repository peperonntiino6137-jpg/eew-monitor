"""緊急地震速報マルチソース受信モニタ - エントリポイント。

実行:  python -m eew.main [--headless]
  --headless : コンソール出力を logs/eew.log へ (スタートアップ常駐用)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import display, location, notifier, region, timesync
from .aggregator import Aggregator
from .mapserver import AlreadyRunningError, MapServer
from .sources import kmoni, kmoni_image, p2p, wolfx

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "logs" / "eew.log"

DEFAULT_CONFIG = {
    "notify": {
        "forecast_min_intensity": "3",
        "alarm_on_warning": True,
        "toast_enabled": True,
        "sound_enabled": True,
        "use_home_intensity": True,
        "sounds": {"warning": "", "forecast": "", "tsunami": "",
                   "tsunami_watch": "", "info": ""},
    },
    "sources": {"wolfx": True, "kmoni": True, "p2p": True},
    "home": {"auto_locate": True, "name": "", "latitude": None,
             "longitude": None, "avs30": 300, "method": ""},
    "map": {"enabled": True, "http_port": 8720, "ws_port": 8721,
            "open_on_start": True, "open_on_warning": True},
    "kmoni_image": {"enabled": True, "trigger_intensity": 2.5, "min_points": 3,
                    "cluster_km": 50, "cooldown_seconds": 60},
    "stale_seconds": 180,
}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    except json.JSONDecodeError as e:
        display.log_system(f"config.json の読み込み失敗 ({e})、デフォルト設定で継続",
                           display.YELLOW)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    merged = {**DEFAULT_CONFIG, **cfg}
    for key in ("notify", "sources", "home", "map", "kmoni_image"):
        merged[key] = {**DEFAULT_CONFIG[key], **(cfg.get(key) or {})}
    return merged


def save_config(cfg: dict) -> None:
    try:
        # "_" 始まりのキーは実行時専用 (保存しない)
        clean = {k: ({kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                     if isinstance(v, dict) else v)
                 for k, v in cfg.items()}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=4)
            f.write("\n")
    except OSError as e:
        display.log_system(f"config.json の保存失敗: {e}", display.YELLOW)


async def amain(headless: bool = False) -> None:
    cfg = load_config()
    display.banner()
    notifier.configure(cfg)  # カスタム音源の読み込み

    await location.resolve_home(cfg, save_config)

    # 自宅の都道府県を判定 (警報対象/津波予報区の自分事判定に使う)
    home = cfg.get("home", {})
    if home.get("latitude") is not None:
        pref = region.nearest_pref(home["latitude"], home["longitude"])
        home["_pref"] = pref  # 実行時のみ (config.json には保存しない)
        display.log_system(f"自宅地域: {pref}")

    # 地図サーバ (ポートバインドが単一インスタンスガードを兼ねる)
    mapsrv = None
    if cfg.get("map", {}).get("enabled", True):
        mapsrv = MapServer(cfg)
        if headless:
            # 常駐起動のたびにブラウザを開かない (警報時のみ開く)
            mapsrv.open_on_start = False
        await mapsrv.start()

    agg = Aggregator(cfg, publish=mapsrv.broadcast if mapsrv else None)

    if mapsrv is not None:
        def on_set_home(lat: float, lon: float) -> None:
            home = cfg.setdefault("home", {})
            home.update({"latitude": round(lat, 4), "longitude": round(lon, 4),
                         "method": "map"})
            if not home.get("name"):
                home["name"] = "自宅"
            save_config(cfg)
            agg.set_home(lat, lon)
            display.log_system(
                f"自宅地点を地図から更新: ({lat:.4f}, {lon:.4f}) [確定]", display.GREEN)
        mapsrv.on_set_home = on_set_home

    async def supervised(name: str, factory) -> None:
        """ソースタスクが予期しない例外で死んでも全体を巻き込まず再起動する。"""
        while True:
            try:
                await factory()
                display.log_system(f"{name}: タスクが終了、10秒後に再起動", display.YELLOW)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                display.log_system(
                    f"{name}: 異常終了 ({type(e).__name__}: {e})、10秒後に再起動",
                    display.RED)
            await asyncio.sleep(10)

    src_cfg = cfg.get("sources", {})
    tasks: list[asyncio.Task] = []
    kmoni_client = None

    # NICT 日本標準時との時刻同期 (カウントダウン・地図時計の基準)
    tasks.append(asyncio.create_task(
        supervised("timesync", timesync.run), name="timesync"))

    if src_cfg.get("kmoni", True) or cfg.get("kmoni_image", {}).get("enabled", True):
        kmoni_client = kmoni.KmoniClient()

    if src_cfg.get("wolfx", True):
        tasks.append(asyncio.create_task(
            supervised("wolfx", lambda: wolfx.run(agg.handle_eew)), name="wolfx"))
    if src_cfg.get("kmoni", True):
        tasks.append(asyncio.create_task(
            supervised("kmoni", lambda: kmoni.run(kmoni_client, agg.handle_eew)),
            name="kmoni"))
    if cfg.get("kmoni_image", {}).get("enabled", True):
        tasks.append(asyncio.create_task(
            supervised("kmoni-image",
                       lambda: kmoni_image.run(kmoni_client, cfg,
                                               agg.handle_ground_motion,
                                               agg.eew_active,
                                               mapsrv.broadcast if mapsrv else None)),
            name="kmoni-image"))
    if src_cfg.get("p2p", True):
        tasks.append(asyncio.create_task(
            supervised("p2p", lambda: p2p.P2PSource(
                agg.handle_eew, cfg,
                mapsrv.broadcast if mapsrv else None).run()),
            name="p2p"))

    if not tasks:
        display.log_system("有効なソースがありません (config.json を確認)", display.RED)
        return

    display.log_system(f"受信開始: {', '.join(t.get_name() for t in tasks)}")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        if kmoni_client is not None:
            await kmoni_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="緊急地震速報マルチソース受信モニタ")
    parser.add_argument("--headless", action="store_true",
                        help="コンソール出力をログファイルへ (常駐用)")
    args = parser.parse_args()

    if args.headless:
        display.enable_headless(LOG_PATH)

    try:
        asyncio.run(amain(headless=args.headless))
    except AlreadyRunningError as e:
        display.log_system(f"起動中止: {e}", display.RED)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n終了します。", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
