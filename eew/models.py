"""EEW イベントの共通データモデルと震度ユーティリティ。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 震度文字列 -> 順序値 (比較用)
_INTENSITY_RANK = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
    "5-": 5, "5弱": 5,
    "5+": 6, "5強": 6,
    "6-": 7, "6弱": 7,
    "6+": 8, "6強": 8,
    "7": 9,
    "5弱以上": 5,  # 震度計欠測時の「震度5弱以上と推定」(P2P scale 46)
}

# 順序値 -> 表示用震度文字列
_RANK_LABEL = ["0", "1", "2", "3", "4", "5弱", "5強", "6弱", "6強", "7"]

# P2P地震情報の scale 値 -> 順序値
_P2P_SCALE_RANK = {
    10: 1, 20: 2, 30: 3, 40: 4,
    45: 5, 46: 5, 50: 6, 55: 7, 60: 8, 70: 9,
}


def intensity_rank(s: str | None) -> int:
    """震度文字列を順序値に変換。不明は -1。"""
    if not s:
        return -1
    return _INTENSITY_RANK.get(str(s).strip(), -1)


def rank_label(rank: int) -> str:
    if 0 <= rank < len(_RANK_LABEL):
        return _RANK_LABEL[rank]
    return "不明"


def p2p_scale_label(scale: int | None) -> str:
    if scale is None:
        return "不明"
    if scale == 46:  # 詳細未入電の大地震で使われる推定値
        return "5弱以上"
    return rank_label(_P2P_SCALE_RANK.get(scale, -1))


def parse_jst(s: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(s, fmt).replace(tzinfo=JST)
    except (ValueError, TypeError):
        return None


# NICT 日本標準時との差 (timesync が設定)。PC時計のズレを全機能で補正する
_clock_offset = timedelta(0)


def set_clock_offset(offset: timedelta) -> None:
    global _clock_offset
    _clock_offset = offset


def clock_offset() -> timedelta:
    return _clock_offset


def now_jst() -> datetime:
    """現在時刻 (JST)。NICT 同期済みならその補正を含む。"""
    return datetime.now(JST) + _clock_offset


@dataclass
class EEWEvent:
    """ソース非依存の緊急地震速報 1 報分。"""
    source: str                     # "wolfx" / "kmoni" / "p2p"
    event_id: str                   # 例 "20260806123456" (発生時刻ベースの共通ID)
    serial: int = 0                 # 第何報
    hypocenter: str = ""            # 震源地名
    magnitude: float | None = None
    depth_km: int | None = None
    max_intensity: str = ""         # "5弱" など
    latitude: float | None = None
    longitude: float | None = None
    origin_time: datetime | None = None
    announced_time: datetime | None = None
    is_warn: bool = False           # 警報か (予報=False)
    is_final: bool = False
    is_cancel: bool = False
    is_training: bool = False
    is_assumption: bool = False     # 仮定震源要素 (PLUM法)。M・震源は当てにならない
    warn_areas: list[str] = field(default_factory=list)
    received_at: datetime = field(default_factory=now_jst)

    @property
    def intensity_rank(self) -> int:
        return intensity_rank(self.max_intensity)

    def latency_ms(self) -> int | None:
        """発表時刻から受信までの遅延 (ms)。"""
        if self.announced_time is None:
            return None
        return int((self.received_at - self.announced_time).total_seconds() * 1000)


@dataclass
class GroundMotionEvent:
    """強震モニタ画像解析による揺れ検知 (EEW発表前の予兆)。

    EEWEvent とは別物: 震源・M・報数を持たないため aggregator の
    重複排除には乗せず、専用ハンドラで扱う。
    """
    region: str                     # 検知エリアの代表地点名
    max_realtime_intensity: float   # リアルタイム震度の最大値
    point_count: int                # 閾値を超えた観測点数
    latitude: float                 # 代表観測点の座標
    longitude: float
    detected_at: datetime = field(default_factory=now_jst)
