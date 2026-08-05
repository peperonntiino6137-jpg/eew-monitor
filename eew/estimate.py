"""自宅地点の予想震度・波到達時刻の推定。

  PGV:      司・翠川 (1999) 距離減衰式 (基準地盤 Vs=600m/s)
  地盤増幅: 松岡・翠川 (1994)  log10(ARV) = 1.83 - 0.66*log10(AVS30)
  震度換算: 翠川ほか (1999)    I = 2.68 + 1.72*log10(PGV)
  走時:     S波 3.5 km/s, P波 7.0 km/s の単純モデル

第1報の M・深さは続報で大きく変わるため、結果はあくまで「暫定の目安」。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import EEWEvent, rank_label

VS = 3.5  # S波速度 km/s
VP = 7.0  # P波速度 km/s

EARTH_R = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2地点間の大円距離 (km)。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def si_midorikawa_pgv600(mag: float, depth_km: float, hypo_dist_km: float) -> float:
    """司・翠川(1999): 基準地盤上の PGV [cm/s]。"""
    x = max(hypo_dist_km, 3.0)  # 至近距離での発散防止
    log_pgv = (0.58 * mag + 0.0038 * depth_km - 1.29
               - math.log10(x + 0.0028 * 10 ** (0.50 * mag))
               - 0.002 * x)
    return 10 ** log_pgv


def site_amplification(avs30: float) -> float:
    """松岡・翠川(1994): AVS30 -> 地表増幅率 ARV。"""
    avs30 = min(max(avs30, 100.0), 1500.0)
    return 10 ** (1.83 - 0.66 * math.log10(avs30))


def pgv_to_intensity(pgv: float) -> float:
    """翠川ほか(1999): PGV [cm/s] -> 計測震度相当値。"""
    if pgv <= 0:
        return -3.0
    return 2.68 + 1.72 * math.log10(pgv)


def instrumental_to_rank(i: float) -> int:
    """計測震度 -> 震度階級の順序値 (models._RANK_LABEL に対応)。"""
    if i < 0.5:
        return 0
    if i < 1.5:
        return 1
    if i < 2.5:
        return 2
    if i < 3.5:
        return 3
    if i < 4.5:
        return 4
    if i < 5.0:
        return 5   # 5弱
    if i < 5.5:
        return 6   # 5強
    if i < 6.0:
        return 7   # 6弱
    if i < 6.5:
        return 8   # 6強
    return 9       # 7


@dataclass
class HomeEstimate:
    """自宅地点での揺れの推定結果。"""
    epicentral_km: float          # 震央距離
    hypo_km: float                # 震源距離
    instrumental: float           # 計測震度相当値
    intensity_rank: int           # 震度階級 (順序値)
    intensity_label: str          # "5弱" など
    pgv: float                    # 地表 PGV cm/s
    p_arrival: datetime | None    # P波到達時刻
    s_arrival: datetime | None    # S波到達時刻

    def s_remaining(self, now: datetime) -> float | None:
        """S波到達までの残り秒。到達済みは負値。"""
        if self.s_arrival is None:
            return None
        return (self.s_arrival - now).total_seconds()


def estimate_home(ev: EEWEvent, home_lat: float, home_lon: float,
                  avs30: float = 300.0) -> HomeEstimate | None:
    """EEW 1 報から自宅地点の揺れを推定。震源情報が不足なら None。"""
    if ev.latitude is None or ev.longitude is None or ev.magnitude is None:
        return None
    depth = float(ev.depth_km if ev.depth_km is not None else 10.0)

    epi = haversine_km(home_lat, home_lon, ev.latitude, ev.longitude)
    hypo = math.sqrt(epi ** 2 + depth ** 2)

    pgv = si_midorikawa_pgv600(ev.magnitude, depth, hypo) * site_amplification(avs30)
    inst = pgv_to_intensity(pgv)
    rank = instrumental_to_rank(inst)

    p_arr = s_arr = None
    if ev.origin_time is not None:
        p_arr = ev.origin_time + timedelta(seconds=hypo / VP)
        s_arr = ev.origin_time + timedelta(seconds=hypo / VS)

    return HomeEstimate(
        epicentral_km=epi,
        hypo_km=hypo,
        instrumental=inst,
        intensity_rank=rank,
        intensity_label=rank_label(rank),
        pgv=pgv,
        p_arrival=p_arr,
        s_arrival=s_arr,
    )
