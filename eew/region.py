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


# 県名を文字列に含まない津波予報区 -> 対象都道府県 (気象庁 66 予報区のうち該当分)。
# 名称は eew/web/data/tsunami_areas.geojson の properties.name と一致させること
_AREA_PREFS = {
    "オホーツク海沿岸": ("北海道",),
    "陸奥湾": ("青森県",),
    "佐渡": ("新潟県",),
    "東京湾内湾": ("東京都", "千葉県", "神奈川県"),
    "伊豆諸島": ("東京都",),
    "小笠原諸島": ("東京都",),
    "相模湾・三浦半島": ("神奈川県",),
    "伊勢・三河湾": ("愛知県", "三重県"),
    "淡路島南部": ("兵庫県",),
    "隠岐": ("島根県",),
    "壱岐・対馬": ("長崎県",),
    "有明・八代海": ("福岡県", "佐賀県", "長崎県", "熊本県", "鹿児島県"),
    "種子島・屋久島地方": ("鹿児島県",),
    "奄美群島・トカラ列島": ("鹿児島県",),
    "沖縄本島地方": ("沖縄県",),
    "大東島地方": ("沖縄県",),
    "宮古島・八重山地方": ("沖縄県",),
}


def matches_area(pref: str, area_name: str) -> bool:
    """警報対象地域/津波予報区の名称が自宅都道府県に該当するか。

    例: 東京都 -> "東京都23区" / "東京湾内湾" に一致。
        千葉県 -> "千葉県九十九里・外房" / "東京湾内湾" に一致。
    短縮名の部分一致は使わない (「京都」が「東京都」に誤マッチするため)。
    """
    if not pref or not area_name:
        return False
    extra = _AREA_PREFS.get(area_name)
    if extra is not None:
        return pref in extra
    if pref in area_name:  # 都道府県フルネームを含む名称 ("石川県能登" など)
        return True
    # 接尾辞なし表記 ("東京23区" 等) の保険。先頭一致のみ許す
    return area_name.startswith(short_pref(pref))
