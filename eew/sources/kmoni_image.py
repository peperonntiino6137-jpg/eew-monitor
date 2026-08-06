"""強震モニタのリアルタイム震度画像を解析する。

役割は2つ:
  1. 揺れ検知 (EEW発表前の予兆) -> GroundMotionEvent
  2. 観測震度の地図配信 -> intensity_grid ペイロード (色別表示用)

- 全画素は走査せず、観測点ピクセル (eew/data/intensity-points.json) のみ読む
- 色 -> リアルタイム震度は JQuake 方式の HSV 連続変換式
  (有効画素条件 s > 0.75 かつ v > 0.1 が背景・海岸線の除去を兼ねる)
- 揺れ検知の誤検知対策 3 条件 AND:
    (a) リアルタイム震度 >= trigger_intensity
    (b) 半径 cluster_km 内に min_points 観測点以上
    (c) 2 フレーム連続
  + クールダウン、EEW 発報中は抑制 (地図配信は継続する)
"""
from __future__ import annotations

import asyncio
import colorsys
import json
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image

from .. import display
from ..estimate import haversine_km
from ..models import GroundMotionEvent
from .kmoni import IMAGE_URL, POLL_INTERVAL, TIME_JUMP_THRESHOLD, KmoniClient

POINTS_PATH = Path(__file__).resolve().parent.parent / "data" / "intensity-points.json"

GRID_MIN_INTENSITY = 0.5   # 震度1未満 (震度0) は地図に表示しない
# 上限は全観測点数より大きく取る。巨大地震で強震域だけに枠を使い切ると
# 遠方 (震度3-4) の地点が表示されなくなるため、実質無制限にする
GRID_MAX_POINTS = 2000


def load_points() -> list[dict]:
    """観測点リスト (休止中を除く)。x/y: 画像ピクセル座標。"""
    with open(POINTS_PATH, encoding="utf-8-sig") as f:  # BOM 付きに対応
        raw = json.load(f)
    points = []
    for p in raw:
        if p.get("is_suspended"):
            continue
        cp = (p.get("point") or {}).get("center_point") or {}
        loc = p.get("location") or {}
        if cp.get("x") is None or loc.get("latitude") is None:
            continue
        points.append({
            "x": int(cp["x"]),
            "y": int(cp["y"]),
            "lat": float(loc["latitude"]),
            "lon": float(loc["longitude"]),
            "name": f"{p.get('region', '')}{p.get('sub_region', '')}" or p.get("name", ""),
        })
    return points


def pixel_to_intensity(r: int, g: int, b: int) -> float | None:
    """RGB -> リアルタイム震度。無効画素 (背景等) は None。"""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if not (s > 0.75 and v > 0.1):
        return None
    if h > 0.1476:
        p = (280.31 * h**6 - 916.05 * h**5 + 1142.6 * h**4
             - 709.95 * h**3 + 234.65 * h**2 - 40.27 * h + 3.2217)
    elif h > 0.001:
        p = 151.4 * h**4 - 49.32 * h**3 + 6.753 * h**2 - 2.481 * h + 0.9033
    else:
        p = -0.005171 * v**2 - 0.3282 * v + 1.2236
    p = max(p, 0.0)
    return 10 * p - 3  # リアルタイム震度


def find_cluster(hits: list[dict], min_points: int, cluster_km: float) -> list[dict] | None:
    """半径 cluster_km 内に min_points 以上集まる塊を返す (なければ None)。

    中心候補は震度上位に絞る (全点を中心にすると O(n^2) で、広域地震時に
    イベントループを最大0.9秒ブロックすることが実測されたため)。
    """
    if len(hits) < min_points:
        return None
    centers = (sorted(hits, key=lambda q: -q["intensity"])[:60]
               if len(hits) > 60 else hits)
    best = None
    for center in centers:
        members = [q for q in hits
                   if haversine_km(center["lat"], center["lon"], q["lat"], q["lon"])
                   <= cluster_km]
        if len(members) >= min_points and (best is None or len(members) > len(best)):
            best = members
    return best


class KmoniImageAnalyzer:
    def __init__(self, client: KmoniClient, cfg: dict,
                 on_detect: Callable[[GroundMotionEvent], None],
                 eew_active: Callable[[], bool],
                 publish: Callable[[dict], None] | None = None):
        icfg = cfg.get("kmoni_image", {})
        self.client = client
        self.on_detect = on_detect
        self.eew_active = eew_active
        self.publish = publish
        self.trigger = float(icfg.get("trigger_intensity", 2.5))
        self.min_points = int(icfg.get("min_points", 3))
        self.cluster_km = float(icfg.get("cluster_km", 50))
        self.cooldown = float(icfg.get("cooldown_seconds", 60))
        self.points = load_points()
        self._prev_cluster: list[dict] | None = None
        self._cooldown_until = 0.0
        self._grid_active = False  # 直前の配信に点があったか (消去メッセージ用)

    # ------------------------------------------------------------- 画像解析

    def read_frame(self, image_bytes: bytes) -> list[dict]:
        """画像 1 フレームから全観測点のリアルタイム震度を読む。"""
        img = Image.open(BytesIO(image_bytes)).convert("RGB")  # パレットGIF対策
        px = img.load()
        w, h = img.size
        frame = []
        for p in self.points:
            if not (0 <= p["x"] < w and 0 <= p["y"] < h):
                continue
            r, g, b = px[p["x"], p["y"]]
            inten = pixel_to_intensity(r, g, b)
            if inten is not None:
                frame.append({**p, "intensity": inten})
        return frame

    # ------------------------------------------------------------- 地図配信

    def publish_grid(self, frame: list[dict]) -> None:
        """観測震度 (震度1以上) を地図へ配信する。揺れが収まったら空配信で消す。"""
        if self.publish is None:
            return
        pts = [q for q in frame if q["intensity"] >= GRID_MIN_INTENSITY]
        if not pts and not self._grid_active:
            return
        pts.sort(key=lambda q: -q["intensity"])
        pts = pts[:GRID_MAX_POINTS]
        self._grid_active = bool(pts)
        self.publish({
            "type": "intensity_grid",
            "points": [[round(q["lat"], 3), round(q["lon"], 3),
                        round(q["intensity"], 1)] for q in pts],
        })

    # ------------------------------------------------------------- 揺れ検知

    def detect(self, frame: list[dict]) -> GroundMotionEvent | None:
        hits = [q for q in frame if q["intensity"] >= self.trigger]
        cluster = find_cluster(hits, self.min_points, self.cluster_km)
        prev = self._prev_cluster
        self._prev_cluster = cluster
        if cluster is None or prev is None:
            return None

        # 2 フレーム連続 + 前フレームの塊と同じ地域 (100km 以内) か
        top = max(cluster, key=lambda q: q["intensity"])
        prev_top = max(prev, key=lambda q: q["intensity"])
        if haversine_km(top["lat"], top["lon"], prev_top["lat"], prev_top["lon"]) > 100:
            return None

        return GroundMotionEvent(
            region=top["name"],
            max_realtime_intensity=top["intensity"],
            point_count=len(cluster),
            latitude=top["lat"],
            longitude=top["lon"],
        )

    # --------------------------------------------------------------- ループ

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        display.log_system(f"強震モニタ画像解析: 開始 ({len(self.points)}観測点)", display.GREEN)
        miss_streak = 0
        prev_tick = loop.time()

        while True:
            await asyncio.sleep(POLL_INTERVAL)

            tick = loop.time()
            if tick - prev_tick > POLL_INTERVAL + TIME_JUMP_THRESHOLD:
                await self.client.sync(force=True)
                self._prev_cluster = None
            prev_tick = tick

            if not await self.client.sync(force=miss_streak >= 5):
                continue
            if miss_streak >= 5:
                miss_streak = 0
            target = self.client.target()
            if target is None:
                continue

            try:
                url = IMAGE_URL.format(ymd=target.strftime("%Y%m%d"),
                                       ts=target.strftime("%Y%m%d%H%M%S"))
                r = await self.client.http.get(url)
                if r.status_code != 200:
                    miss_streak += 1
                    continue
                miss_streak = 0
                frame = self.read_frame(r.content)

                # 観測震度の地図配信は常に行う (地震の最中こそ見たい情報)
                self.publish_grid(frame)

                # 揺れ検知は EEW 発報中・クールダウン中はスキップ
                if self.eew_active():
                    self._prev_cluster = None
                    continue
                if tick < self._cooldown_until:
                    continue
                gm = self.detect(frame)
                if gm is not None:
                    self._cooldown_until = loop.time() + self.cooldown
                    self._prev_cluster = None
                    self.on_detect(gm)
            except asyncio.CancelledError:
                raise
            except Exception:
                miss_streak += 1


async def run(client: KmoniClient, cfg: dict,
              on_detect: Callable[[GroundMotionEvent], None],
              eew_active: Callable[[], bool],
              publish: Callable[[dict], None] | None = None) -> None:
    await KmoniImageAnalyzer(client, cfg, on_detect, eew_active, publish).run()
