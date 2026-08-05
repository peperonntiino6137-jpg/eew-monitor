"""実際の発報シーケンスを再現するデモ (実際の地震ではありません)。

実行:  python -m eew.demo                        (三陸沖 M8.8 深さ10km)
       python -m eew.demo 9.4 30                 (M・深さ指定)
       python -m eew.demo 7.9 25 sagami          (震源プリセット指定)

プリセット: sanriku (三陸沖・海溝型) / sagami (相模湾北西部・関東直下型)

本番常駐 (ポート8720) とは別ポート 28720/28721 で動くため、監視を止めずに実行できる。
ブラウザは最初は開かず、「警報」発表の瞬間に自動で開く — 実際の挙動と同じ。
"""
from __future__ import annotations

import asyncio
import math
import sys
from datetime import timedelta

from . import display, notifier, region
from .aggregator import Aggregator
from .estimate import (VP, VS, haversine_km, pgv_to_intensity,
                       si_midorikawa_pgv600, site_amplification)
from .main import load_config
from .mapserver import MapServer
from .models import EEWEvent, now_jst
from .sources.kmoni_image import load_points
from .sources.p2p import P2PSource

EVENT_ID_SUFFIX = "880000"  # デモ用固定ID

PRESETS = {
    "sanriku": {
        "lat": 38.5, "lon": 143.5, "name": "三陸沖",
        "warn1": ["岩手県", "宮城県", "青森県", "秋田県", "山形県", "福島県"],
        "warn2": ["岩手県", "宮城県", "青森県", "秋田県", "山形県", "福島県",
                  "北海道太平洋沿岸", "茨城県", "栃木県", "群馬県",
                  "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県"],
        "warn3_extra": ["長野県", "山梨県", "静岡県", "愛知県", "岐阜県",
                        "三重県", "大阪府", "京都府"],
        "tsunami": {
            "MajorWarning": ["岩手県", "宮城県", "青森県太平洋沿岸", "福島県"],
            "Warning": ["北海道太平洋沿岸中部", "茨城県", "千葉県九十九里・外房"],
            "Watch": ["東京湾内湾", "相模湾・三浦半島", "伊豆諸島"],
        },
    },
    "nankai": {
        "lat": 33.1, "lon": 136.1, "name": "紀伊半島南東沖",
        "warn1": ["静岡県", "愛知県", "三重県", "和歌山県", "徳島県", "高知県"],
        "warn2": ["静岡県", "愛知県", "三重県", "和歌山県", "徳島県", "高知県",
                  "大阪府", "奈良県", "京都府", "兵庫県", "香川県", "愛媛県",
                  "岐阜県", "山梨県", "長野県", "神奈川県", "東京都", "千葉県",
                  "大分県", "宮崎県"],
        "warn3_extra": ["埼玉県", "群馬県", "栃木県", "茨城県", "滋賀県",
                        "岡山県", "広島県", "山口県", "福岡県", "熊本県",
                        "鹿児島県", "石川県", "福井県"],
        "tsunami": {
            "MajorWarning": ["静岡県", "愛知県外海", "三重県南部", "和歌山県",
                             "徳島県", "高知県", "宮崎県", "伊豆諸島"],
            "Warning": ["千葉県内房", "東京湾内湾", "相模湾・三浦半島",
                        "大阪府", "兵庫県瀬戸内海沿岸", "愛媛県宇和海沿岸",
                        "大分県", "鹿児島県東部"],
            "Watch": ["千葉県九十九里・外房", "小笠原諸島", "有明・八代海"],
        },
    },
    "sagami": {
        "lat": 35.3, "lon": 139.25, "name": "相模湾",
        "warn1": ["神奈川県", "東京都", "千葉県", "埼玉県", "静岡県"],
        "warn2": ["神奈川県", "東京都", "千葉県", "埼玉県", "静岡県",
                  "山梨県", "茨城県", "群馬県", "栃木県"],
        "warn3_extra": ["長野県", "新潟県", "福島県", "愛知県"],
        "tsunami": {
            "MajorWarning": ["相模湾・三浦半島", "伊豆諸島"],
            "Warning": ["静岡県", "千葉県内房", "東京湾内湾"],
            "Watch": ["千葉県九十九里・外房"],
        },
    },
}


def build_scenario(mag: float, preset: dict) -> list:
    """最終マグニチュードから続報シーケンスを組み立てる。
    (経過秒, 報数, 警報?, M, 最大震度, 最終?, 警報地域)"""
    warn_full = preset["warn2"] + (preset["warn3_extra"] if mag >= 9.0 else [])
    int_final = "7" if mag >= 7.8 else "6強"
    return [
        (0.0,  1, False, round(mag - 1.2, 1), "5強",     False, []),
        (4.0,  2, True,  round(mag - 0.6, 1), "6強",     False, preset["warn1"]),
        (8.0,  3, True,  round(mag - 0.2, 1), int_final, False, warn_full),
        (12.0, 4, True,  mag,                 int_final, True,  warn_full),
    ]


def _make(origin, serial, warn, mag, intensity, final, areas, depth,
          preset: dict) -> EEWEvent:
    return EEWEvent(
        source="demo",
        event_id=origin.strftime("%Y%m%d") + EVENT_ID_SUFFIX,
        serial=serial,
        hypocenter=preset["name"],
        magnitude=mag,
        depth_km=int(depth),
        max_intensity=intensity,
        latitude=preset["lat"],
        longitude=preset["lon"],
        origin_time=origin,
        announced_time=now_jst(),
        is_warn=warn,
        is_final=final,
        warn_areas=list(areas),
    )


class GridSim:
    """観測震度の色別表示デモ: 波の到達に合わせて観測点が色づいていく。

    複数の地震を登録でき、各観測点は全地震の中で最大の震度を表示する
    (実運用では強震モニタの実画像1枚に全地震の揺れが写るため、この合成は
    kmoni_image がそのまま実現する)。
    """

    def __init__(self, mapsrv: MapServer):
        self.mapsrv = mapsrv
        self.points = load_points()
        self.quakes: list[dict] = []

    def add(self, origin, mag: float, depth: float,
            epi_lat: float, epi_lon: float) -> None:
        hypos = []
        for p in self.points:
            epi = haversine_km(p["lat"], p["lon"], epi_lat, epi_lon)
            hypo = math.sqrt(epi ** 2 + depth ** 2)
            hypos.append((hypo, pgv_to_intensity(
                si_midorikawa_pgv600(mag, depth, hypo) * site_amplification(300))))
        self.quakes.append({"origin": origin, "hypos": hypos})

    async def run(self) -> None:
        active = True
        while active:
            await asyncio.sleep(1)
            if not self.quakes:
                continue
            now = now_jst()
            active = any((now - q["origin"]).total_seconds() <= 420
                         for q in self.quakes)
            pts = []
            for i, p in enumerate(self.points):
                best = 0.0
                for q in self.quakes:
                    elapsed = (now - q["origin"]).total_seconds()
                    hypo, final_int = q["hypos"][i]
                    if elapsed < hypo / VP or elapsed > 420:
                        continue               # 未到達 or 収束済み
                    inten = final_int
                    if elapsed < hypo / VS:
                        inten -= 2.0           # P波のみ到達 (初期微動)
                    best = max(best, inten)
                if best >= 0.5:
                    pts.append([round(p["lat"], 3), round(p["lon"], 3),
                                round(min(best, 7.0), 1)])
            pts.sort(key=lambda x: -x[2])
            # 上限なし: 巨大地震でも遠方の弱い揺れ (震度1-4) まで全点表示する
            self.mapsrv.broadcast({"type": "intensity_grid", "points": pts})
        self.mapsrv.broadcast({"type": "intensity_grid", "points": []})


async def _setup() -> tuple[dict, MapServer, Aggregator]:
    cfg = load_config()
    # 本番常駐と共存するためポートを変える
    cfg["map"] = {**cfg.get("map", {}),
                  "http_port": 28720, "ws_port": 28721,
                  "open_on_start": False,   # 最初は開かない
                  "open_on_warning": True}  # 警報の瞬間に自動で開く
    notifier.configure(cfg)  # カスタム音源

    home = cfg.get("home", {})
    if home.get("latitude") is not None:
        home["_pref"] = region.nearest_pref(home["latitude"], home["longitude"])
        display.log_system(f"自宅地域: {home['_pref']}")

    mapsrv = MapServer(cfg)
    await mapsrv.start()
    agg = Aggregator(cfg, publish=mapsrv.broadcast)
    return cfg, mapsrv, agg


async def run_event(cfg: dict, mapsrv: MapServer, agg: Aggregator,
                    mag: float, depth: float, preset_key: str,
                    suffix: str = EVENT_ID_SUFFIX,
                    with_tsunami: bool = True,
                    grid: GridSim | None = None) -> None:
    """1 つの地震の発報〜S波到達までを再現する (連発デモ用に並行実行可能)。"""
    preset = PRESETS[preset_key]
    origin = now_jst() - timedelta(seconds=3)  # 3秒前に破壊開始した想定
    if grid is not None:
        grid.add(origin, mag, depth, preset["lat"], preset["lon"])
    prev_t = 0.0
    for t, serial, warn, m, intensity, final, areas in build_scenario(mag, preset):
        await asyncio.sleep(t - prev_t)
        prev_t = t
        ev = _make(origin, serial, warn, m, intensity, final, areas, depth, preset)
        ev.event_id = origin.strftime("%Y%m%d") + suffix
        agg.handle_eew(ev)

    # ---- 津波警報 (M7.5以上の海域地震で発表される想定) ----
    if with_tsunami and mag >= 7.5:
        await asyncio.sleep(4)
        display.log_system(f"気象庁が津波警報を発表した想定 ({preset['name']}, デモ)",
                           display.YELLOW)
        p2p_demo = P2PSource(agg.handle_eew, cfg, publish=mapsrv.broadcast)
        p2p_demo.handle({
            "code": 552,
            "cancelled": False,
            "areas": [{"grade": g, "name": n}
                      for g, names in preset["tsunami"].items()
                      for n in names],
        })

    # 自宅への S波到達までカウントダウンを流し、到達後も少し表示を残す
    st = agg.events.get(origin.strftime("%Y%m%d") + suffix)
    est = st.estimate if st else None
    if est is not None and est.s_arrival is not None:
        remain = est.s_remaining(now_jst())
        if remain and remain > 0:
            await asyncio.sleep(remain + 20)
        else:
            await asyncio.sleep(30)  # 直下型: 既に到達済みでも余韻を表示
    else:
        await asyncio.sleep(60)


async def amain(mag: float, depth: float, preset_key: str) -> None:
    preset = PRESETS[preset_key]
    display.banner()
    display.log_system(
        f"=== デモ: {preset['name']} M{mag:.1f} 深さ{depth:.0f}km "
        f"(実際の地震ではありません) ===", display.YELLOW)
    cfg, mapsrv, agg = await _setup()
    grid = GridSim(mapsrv)
    grid_task = asyncio.create_task(grid.run())
    display.log_system("3秒後に発生します。地図はまだ開きません — 警報発表の瞬間に自動で開きます")
    await asyncio.sleep(3)
    await run_event(cfg, mapsrv, agg, mag, depth, preset_key, grid=grid)
    grid_task.cancel()
    display.log_system("デモ終了 (地図タブは閉じて構いません)")


async def amain_double() -> None:
    """連発シナリオ: 南海トラフ M9.1 の 20 秒後に三陸沖 M9.1。"""
    display.banner()
    display.log_system("=== 連発デモ: 紀伊半島南東沖 M9.1 -> 20秒後に三陸沖 M9.1 "
                       "(実際の地震ではありません) ===", display.YELLOW)
    cfg, mapsrv, agg = await _setup()
    grid = GridSim(mapsrv)
    grid_task = asyncio.create_task(grid.run())
    display.log_system("3秒後に1つ目が発生します")
    await asyncio.sleep(3)

    ev1 = asyncio.create_task(run_event(
        cfg, mapsrv, agg, 9.1, 20, "nankai", suffix="880000",
        with_tsunami=True, grid=grid))
    await asyncio.sleep(20)
    display.log_system(f"{display.BOLD}=== 2つ目の地震が発生 (三陸沖 M9.1) ==={display.RESET}",
                       display.YELLOW)
    ev2 = asyncio.create_task(run_event(
        cfg, mapsrv, agg, 9.1, 30, "sanriku", suffix="770000",
        with_tsunami=False,  # 2つ目の津波警報は省略 (発表内容が重複するため)
        grid=grid))          # 観測震度は両地震の最大値を合成表示
    await asyncio.gather(ev1, ev2)
    grid_task.cancel()
    display.log_system("連発デモ終了")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "double":
        try:
            asyncio.run(amain_double())
        except KeyboardInterrupt:
            print("\nデモ中断", flush=True)
        return
    mag = float(sys.argv[1]) if len(sys.argv) > 1 else 8.8
    depth = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    preset = sys.argv[3] if len(sys.argv) > 3 else "sanriku"
    if preset not in PRESETS:
        print(f"不明なプリセット: {preset} (候補: {', '.join(PRESETS)})")
        sys.exit(1)
    try:
        asyncio.run(amain(mag, depth, preset))
    except KeyboardInterrupt:
        print("\nデモ中断", flush=True)


if __name__ == "__main__":
    main()
