"""現在地の地域判定 (オフライン)。

強震モニタ観測点データ (eew/data/intensity-points.json) の region (都道府県) を
最近傍探索して、自宅座標の都道府県を求める。
警報対象地域・津波予報区の名称と自宅都道府県の照合にも使う。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .estimate import haversine_km

POINTS_PATH = Path(__file__).resolve().parent / "data" / "intensity-points.json"

# 都道府県接尾辞を落とした短縮名 ("東京都"->"東京") で部分一致させる
_SUFFIXES = ("都", "道", "府", "県")


@lru_cache(maxsize=1)
def _pref_points() -> list[tuple[float, float, str]]:
    with open(POINTS_PATH, encoding="utf-8-sig") as f:
        raw = json.load(f)
    out = []
    for p in raw:
        loc = p.get("location") or {}
        pref = p.get("region") or ""
        if loc.get("latitude") is None or not pref:
            continue
        out.append((float(loc["latitude"]), float(loc["longitude"]), pref))
    return out


def nearest_pref(lat: float, lon: float) -> str:
    """座標から最寄り観測点の都道府県名を返す。"""
    best, best_d = "", 1e18
    for plat, plon, pref in _pref_points():
        d = haversine_km(lat, lon, plat, plon)
        if d < best_d:
            best, best_d = pref, d
    return best


def short_pref(pref: str) -> str:
    if pref.endswith(_SUFFIXES) and len(pref) > 2:
        return pref[:-1]
    return pref


def matches_area(pref: str, area_name: str) -> bool:
    """警報対象地域/津波予報区の名称が自宅都道府県に該当するか。

    例: 東京都 -> "東京都" / "東京湾内湾" に一致。
        千葉県 -> "千葉県九十九里・外房" に一致。
    """
    if not pref or not area_name:
        return False
    return short_pref(pref) in area_name or area_name in pref
